# DATA PIPELINE AUDIT — kraken-trading-bot (FRESH, Phase 8)

Read-only auditor pass against the **actual code on disk** (not READMEs).
Prior work acknowledged and re-read: the Phase 1 audit in this file's history
identified 6 gaps; **Gap 1 (local parquet store) and Gap 2 (funding-rates /
perp-basis) are now built and integrated** as sibling projects
`kraken-market-data` and `kraken-funding-rates`. This audit re-examines the
whole pipeline in that new state and re-ranks what remains.

Files read (this run): `kraken_trading_bot/{engine,cli}.py`,
`kraken_trading_bot/rl/{data,features,environment,train,backtest,paper_trade,agent,registry}.py`,
`kraken_trading_bot/strategies/{base,sma}.py`, `configs/default.yaml`,
`tests/*.py`, `flake.nix`, `nix/module.nix`, `.data-audit/{DECISION,PLAN,VALIDATION}.md`,
plus sibling source at `../kraken-market-data/market_data/{store,client,cli,export}.py`,
`../kraken-funding-rates/kraken_funding_rates/{client,models,export,cli}.py`,
`../ticker-news-signals/ticker_news_signals/{client,pipeline,cli}.py`.

Verification run this pass: `pytest tests/ -q` → **82 passed** (73 pre-existing
+ 7 store-seam + 2 funding-seam); live feature-width probes → **49 core features,
55 with both news + funding signal columns merged**.

---

## 1. PIPELINE MAP (post Gap 1 + Gap 2)

Five data paths now; only Path A is the classical engine loop. Paths B–D are
the RL family. Exogenous signals now feed the RL paths through **two** sibling
JSONL files; the market store backs every RL fetch when configured (it is
**off by default** — `market_data_store: null`).

```
                          kraken-python (KrakenManager, REST /0/public/*, paper transport)
                          transport.throttle min_interval = 0.0 BY DEFAULT   (auth/transport)
        A. ENGINE LOOP (run)           B. RL TRAIN / C. BACKTEST             D. PAPER TRADE
        engine.run_iteration          train_ticker()/backtest_model()       PaperTrader.step()
        every interval=60s              │                                      every interval=60s
        ├─ manager.ticker ────────────  │ read_ohlc_dataframe(pages, since/until)
        ├─ manager.ohlc(60) ────────────┤     │ store?.upsert  → parquet {PAIR}/{interval}/{YYYY-MM}
        └─ manager.order_book(count=10)─┤     │ store?.read    → DeepHistory (>~720-bar REST ceiling)
          3 calls/pair/iter, UNCACHED   │     └―market_data_store null ──► fetch_ohlc_dataframe (≤~720 bars)
            │                           │            merge_extra_features(extra_features_file)   ← news JSONL
            ▼                           │            merge_extra_features(funding_features_file) ← funding JSONL
        strategy.tick({ticker,candles[-100:],                                            (hour-floor left-join, ffill, zero-fill)
         order_book})                   │
        SMA uses ONLY candles+ticker    ▼
        order_book fetched & DISCARDED  FeaturePipeline.compute() → feature groups
                                       price|technical|volume|microstructure|signals
                                       microstructure group yields NOTHING (OHLC-only: Gap 4)
                                       signals group = fixed allow-list → 6 cols (news+funding)
                                       ▼
        EXOGENOUS SEAMS (sibling JSONL)   TradingEnvironment (raw compute()+ffill → obs row)
        ../ticker-news-signals  cli.py pull → signals/eth_usd.jsonl (sentiment/article/novelty)
        ../kraken-funding-rates cli.py pull → signals/eth_usd_funding.jsonl (funding/basis/OI)
           BOTH MANUAL — no systemd/cron in bot or siblings (news+funding)
           ↓ configs/default.yaml extra_features_file / funding_features_file / market_data_store
                                     (all null → all seams OFF by default)
        Obs width: 49 core [+3 news][+3 funding] = 52 / 55   —   PPO(MlpPolicy)
        artifacts: model.zip, normalization.npz (⚠ UNUSED by the policy, see Finding A),
                   config.yaml
```

**Where data enters the RL observation exactly:** `TradingEnvironment.__init__`
(`environment.py:186-188`) runs `pipeline.compute(self.data)` and feeds the
agent `_raw_feature_array()` (ffill + zero-fill, **no z-score**), observed
per-step at `environment.py:367-375`. The `signals` group
(`features.py:388-401`) forwards only the fixed `_SIGNAL_COLUMNS` allow-list
(`features.py:32`). **Nothing else can enter the vector without editing code.**

**Where data enters a strategy tick() exactly:** `engine.py:136` passes
`{ticker, candles[-100:], order_book}` from `fetch_market_data`
(`engine.py:46-76`). SMA reads candles + ticker only; order_book is dead;
**no exogenous signal ever reaches the engine loop.**

---

## 2. CURRENT DATA SOURCES CATALOG

| # | Resource | Cadence | Consumer | Persistence | Evidence |
|---|---|---|---|---|---|
| 1 | Ticker (REST) | every engine tick 60 s | SMA `tick()` limit prices | none | `engine.py:59`, `sma.py:138-158` |
| 2 | OHLC 60 m, last ~100 | every engine tick | SMA closes | none | `engine.py:65` `candles[-100:]` |
| 3 | OrderBook depth count=10 | every engine tick | fetched, passed, **unused** | none | `engine.py:72`; SMA ignores it |
| 4 | OHLC paged (≤ ~720 bars, ~30 d) | per train/backtest | FeaturePipeline + env | **now optional store-upsert** | REST ceiling verified (`DECISION.md`); `data.py:263 fetch`, `data.py:324 read` |
| 5 | Local parquet store (Gap 1 ✅) | systemd poller in sibling repo (`OnCalendar *:*:30`) | any RL fetch via `read_ohlc_dataframe` | month-sliced parquet + `_meta.json` cursor | `../kraken-market-data/market_data/store.py:147 read,217 upsert,257 update`; `INTEGRATION.md`; **OFF by default (`default.yaml:78 null`)** |
| 6 | News sentiment JSONL (prior pass) | **hourly, MANUAL** | `merge_extra_features` → `signals` | JSONL sibling repo | `data.py:80`, `default.yaml:57`; `ticker-news-signals/pipeline.py:100` |
| 7 | Funding/basis/OI JSONL (Gap 2 ✅) | **hourly, MANUAL** | `merge_extra_features` → `signals` | JSONL sibling repo | `data.py:49 _SIGNAL_COLUMNS`, `default.yaml:65`; `kraken-funding-rates/export.py:120 write_jsonl` (timestamp floored `:148`) |
| 8 | (latent, unused) `recent_trades`, `spread`, futures depth | n/a | never called | n/a | `kraken-python/manager.py:192,200` |

Feature pipeline width today (verified live): **49 core / 52 with news / 55 with
news+funding**. Stored on-disk models (`models/ETH_USD`, `models/XRP_USD`) were
trained at **49 features** — before either exogenous seam existed.

Network physics unchanged: default throttle OFF (`min_interval=0.0`), no retry
in the bot (the sibling clients add retry/backoff; the bot does not).

---

## 3. RANKED CANDIDATE GAPS

Ranked by (a) directness into RL features or tick(), (b) sourcing cost
(keyless → paid → scraped), (c) differentiation from completed passes. Each says
Evidence → Feed path → Sourcing. Categories spanned: social/sentiment,
**data-quality (NEW)**, **operations (NEW)**, microstructure, on-chain, macro.

### CANDIDATE 1 — Social / search-trend signal (original Gap 3, still open)  *(social/sentiment)*
- **Evidence:** unchanged — only GNews sentiment + Kraken-Futures funding feed
  the `signals` group. `PLAN.md:109-111` and `DECISION.md:82` explicitly
  deferred StockTwits v2 (keyless, per-ticker mention velocity/tilt) +
  alternative.me **Fear & Greed** (free, 2018+ history, aggregate sentiment —
  the missing *market-wide* risk axis). Genuinely new modality, orthogonal to
  headline VADER.
- **Feed path:** direct through `merge_extra_features`, **but the seam is a
  fixed allow-list**: `_SIGNAL_COLUMNS` (`data.py:49`, `features.py:32`) — a new
  source's columns (e.g. `mention_velocity`, `fear_greed`) are **dropped unless
  added to both tuples** (a 2-file edit that must stay in sync). `PLAN.md:151`
  already proposes reading `_SIGNAL_COLUMNS` from config. No pipeline redesign
  otherwise.
- **Sourcing cost:** low-medium. StockTwits REST unauthenticated (keyless,
  throttled) + alt.me F&G JSON (keyless). Best new-modality value of the
  remaining list. **Pair with Candidate 2's normalization fix** so the raw
  mention-count/scaled-F&G columns enter the observation properly scaled.

### CANDIDATE 2 — Normalization.npz never shapes the observation  *(data-quality, NEW)*
- **Evidence:** the policy is trained and run on **raw computed features**, not
  the z-score output. `environment.py:186-188` uses `pipeline.compute()` +
  `_raw_feature_array()` (`environment.py:445-448`, ffill/zero-fill only);
  `transform()`/`NormalizationStats.normalize()` are called **nowhere** in
  train/backtest/paper (grep: only `fit_transform`, which itself is unused).
  Yet `train.py:209` **saves** normalization.npz and `backtest.py:160` /
  `paper_trade.py:188` **load** it. The `paper_trade.py:314` docstring confirms
  the raw-scale design ("same scale as training") — parity, but on an
  un-normalized scale: dollar-denominated `sma_24`/`ema_24`/`bb_upper_*`/`atr_*`
  dominate unit-scale `return_*`/`volume_zscore_*`. The z-score machinery is
  effectively **ceremonial**.
- **Feed path:** direct — it IS the observation vector. Fix = apply
  `transform()` (with per-model stats) in both `environment.py` and
  `_build_observation` (`paper_trade.py:299-318`), or delete the dead
  persistence. Caution: `prepare_episode` fits stats on the full frame **before**
  slicing the episode (`data.py:514-524`) — if z-scoring is switched on, those
  tail bars leak into the normalization stats (look-ahead).
- **Sourcing cost:** zero (pure code). Highest directness of any remaining item;
  grows more important as Candidates 1/3 add heteroscaled columns.

### CANDIDATE 3 — Exogenous scheduling + staleness visibility  *(operations, NEW)*
- **Evidence:** two of the three exogenous feeds (news, funding) are pulled
  **manually** (`VALIDATION.md:69` "deferred", `PLAN.md:117-120`); only
  `kraken-market-data` ships its own NixOS timer. The bot's `nix/module.nix` is
  credentials-only — it schedules nothing. `merge_extra_features`
  forward-fills and zero-fills missing hours (`data.py:164-167`), so a stale
  `sentiment_score`/`funding_rate` is **indistinguishable** from a fresh one;
  no age/freshness column exists, and `novelty_flag` is computed only at pull
  time (`ticker-news-signals/pipeline.py:39,91-97`).
- **Feed path:** indirect but cheap — systemd/cron set to re-pull both JSONLs
  hourly; optionally add a `signal_age_hours` (now − last-merged-hour) feature
  so the agent (and any human reading feature values) can discount staleness.
- **Sourcing cost:** zero for timers; one extra column if freshness is added.
  Protects the information value of the two completed passes.

### CANDIDATE 4 — Order-book / trades microstructure recorder (orig. Gap 4)  *(market microstructure)*
- **Evidence:** `_add_microstructure_features` (`features.py:373-386`) is ready
  (`spread`, `order_book_imbalance` columns already coded) but **outputs
  nothing** from the OHLC-only path; the engine fetches top-10 depth every loop
  and discards it (`engine.py:72`); `recent_trades`/`spread` REST endpoints
  exist unused (`kraken-python/manager.py:192,200`). Previously blocked on the
  store (ephemeral snapshots) — **the store now exists**, so a book/trades
  recorder is the unlock to activate the dormant group.
- **Feed path:** direct once recorded — a `microstructure`-group input
  (`bid/ask/bid_vol/ask_vol/spread`) joined onto bars; needs its own poller +
  consolidation component.
- **Sourcing cost:** keyless (existing endpoints), **medium-high operationally**
  (polling + storage + alignment). Ride on top of Candidate 3's timers.

### CANDIDATE 5 — On-chain (orig. Gap 5) / CANDIDATE 6 — Macro calendar (orig. Gap 6)
- **Evidence (both):** unchanged — no on-chain or macro source anywhere
  (`Gap 5`, `Gap 6` in the prior audit, reaffirmed by `DECISION.md:83`).
- **Feed:** on-chain slots into `merge_extra_features` (per asset-hour) but is
  backfill-limited (large-transfer/whale feeds are paid or scraped); macro
  events aren't natural hourly scalars — event-envelope encoding is
  feature-design-heavy.
- **Sourcing:** medium-high (on-chain), low-medium (macro) but lowest
  directness of the list. Park until the store has depth worth joining onto.

---

## 4. NOTABLE SMALLER FINDINGS (not ranked)

- **`_SIGNAL_COLUMNS` is duplicated** in `data.py:49` and `features.py:32`;
  nothing tests that both agree. A column added to one and not the other is
  silently excluded from the observation (merge warns; `_add_signals_features`
  just skips).
- **Historical models have thin provenance**: on-disk `config.yaml` for
  ETH/XRP carry only `ticker`, `model_name`, `action_space` (verified), so
  backtest/paper reconstruct pipeline settings from `default.yaml` defaults —
  and those models were trained at 49 features, before either signal seam. A
  fresh `train_ticker` today would record a full config (`build_train_config`
  merges all of `default.yaml`), but **no model has been retrained** since the
  seams landed.
- **`n_features()` is stateful, not config-derived** (`features.py:404-417`):
  returns the column count of the *last* `compute()`, not the loaded
  normalization.npz — after `load_normalization` it can under-report until a
  fresh compute.
- **`merge_extra_features` reparses the whole JSONL every call and is invoked
  twice per load** (news + funding) — fine for batch train, wasteful for the
  paper trader that rebuilds the window every 60 s.
- **Paper trader still hits the network on every tick even with the store**
  (`paper_trade.py:56,280-297`): the adapter appends, but the fetch leg isn't
  skipped; `data.py:382-386` todo (collapse to append + tail read) not done.
- **Engine and RL are still fully disjoint**: the `signals` groups exist only
  in the RL feature space; `strategy.tick()` has no exogenous feed.
- **Funding columns are near-constant**: funding settles ~8-hourly while bars
  are hourly and the merge ffill's — `funding_rate`/`basis`/`open_interest`
  are quasi-constant within a day; effectively one scalar/basis value per
  8 h × related columns. z-scoring (Candidate 2) matters here more than for
  price features.
- **Basis is mark-vs-index, not spot-vs-perp** (`..kraken-funding-rates/
  models.py:101`): `(mark−index)/index` is intravenue (Kraken Futures index),
  a slightly weaker "perp premium" reading than spot-vs-mark; acceptable, but
  worth knowing when interpreting the column.
- **`kraken-funding-rates` is not a flake input of the bot** (`flake.nix:6-8`
  lists only kraken-python + kraken-market-data), so `nix develop` cannot run
  the funding CLI; `VALIDATION.md:69` flagged this as deferred.

---

## 5. BOTTOM LINE FOR THE DECISION PHASE

Two of the six original gaps are **done and verified** (store, funding — both
off by default and unretrained). The next vector with the most differentiation
per effort is **Gap 3 social/search-trend (StockTwits + Fear&Greed)** via the
existing signal seam — with the caveat that the seam is a hard-coded allow-list
and needs the 2-file `_SIGNAL_COLUMNS` widening. Before/alongside it, two cheap
foundation items newly in scope: **actually apply (or remove) the normalization
stack** and **schedule + timestamp the exogenous pulls** so the completed news +
funding seams stop going stale silently. Gap 4 (microstructure recorder) is now
operational on top of the store; on-chain and macro stay parked.

AUDIT COMPLETE