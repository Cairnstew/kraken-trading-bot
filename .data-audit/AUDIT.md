# DATA PIPELINE AUDIT — kraken-trading-bot (FRESH, 2026-09-30)

Read-only auditor pass. Written against the **code on disk right now** —
uncommitted working tree included. This document replaces the prior-pass
AUDIT.md (2026-09-29, the `kraken-deep-history` run). Prior artifacts
(AUDIT/DECISION/RESEARCH/PLAN/VALIDATION, RESEARCH-1/2/3) were read for
context but not copied.

Key deltas since the 2026-09-29 pass:
- The working tree now carries **uncommitted WIP**: new untracked
  `kraken_trading_bot/rl/export.py` + `tests/test_rl_export.py`, modified
  `cli.py` (new `export-data` subcommand), `rl/__init__.py` (exports
  `.export`), `rl/train.py` (`resolve_default_config_path` CWD fallback),
  `justfile` (new `bench` + `export-data` recipes), `.gitignore`
  (`exports/`). This WIP is the subject of the audit as much as the
  committed tree.
- Verification run this pass (`nix develop --command bash -c "pytest tests/ -q"`):
  **100 passed, 1 failed** — `test_rl_export.py::test_build_export_frame_normalized_block_is_z_scored`
  FAILS on the current tree (it is the new untracked export test).
- **The market-data store root does not exist on disk**
  (`~/Projects/kraken-market-data/store`: No such file or directory), so
  `market_data_store:` points nowhere in any config; deep history is still
  unreachable. `kraken-deep-history` was scaffolded and shipped last pass but
  nothing has seeded a store this host's bot reads.
- **`models/` is empty** (only `.gitkeep`): there are no trained models,
  no on-disk `models/*/config.yaml` provenance, so every `backtest` /
  `paper-trade` today would fall back to `default.yaml` defaults.
- The 3 sibling signal projects (`ticker-news-signals`, `kraken-funding-rates`,
  `kraken-social-signals`) each ship only a manual `cli.py pull`; **none ships
  a scheduler/timer** (only `kraken-market-data` has a systemd timer). The
  bot flake lists only `kraken-python` + `kraken-market-data` as inputs.
- `kurosearch` (Rule34 image search app, Svelte+API) and `researcher-python`
  (academic paper aggregation over OpenAIRE/SemanticScholar/arXiv/Crossref)
  exist as siblings but are **not in the data-source registry** and are
  unrelated to this bot's pipeline as they stand.

---

## 1. PIPELINE MAP (end to end, current code)

Eight entry points consume data. A is the classical strategy loop; B–F are the
RL family; G/H are the new WIP export and the empty registry.

```
                        kraken-python (KrakenManager, REST /0/public/*)
                        transport.min_interval = 0.0 DEFAULT; NO retry/backoff
                        (transport.py:108,113,180-190; errors raised, never retried)
   A. ENGINE LOOP (run)       B. TRAIN / C. BACKTEST        D. PAPER TRADE
   engine.run(): interval=60s   train_ticker()/backtest      PaperTrader.step()
   fetch_market_data per pair   read_ohlc_dataframe(pages)   every interval=60s
   ├─ manager.ticker  ──────┐   │  _page_candles loops pages=6 │ _FETCH_PAGES=2
   ├─ manager.ohlc(60,1call)┤   │   << 720 bars/call ceiling >>│  2 OHLC calls/tick
   │    keeps candles[-100:]  ──┤  store?.upsert -> parquet     │  re-fetches EVERY tick
   └─ manager.order_book(10) ───┘  store?.read -> DeepHistory  │  recomputes full
       3 calls/pair/iter UNCACHED  (since/until always None:    │  pipeline on window
                                   whole store, or live ≤pages  │
       strategy.tick({ticker,      * 720 bars)                  │
        candles[-100:], order_book})  merge_extra_features x3    │
       SMA uses ONLY candles+ticker  news / funding / social     │
       order_book = DEAD             (hour-floor ffill, zero-fill)│
                                     ▼                           ▼
                                  FeaturePipeline.compute() -> 49-core feature frame
                                  price|technical|volume|microstructure|=0|signals
                                  microstructure group yields NOTHING (OHLC only)
                                  signals group = _SIGNAL_COLUMNS allow-list (9 cols)
                                     (all 3 config defaults null -> none merged)
                                     ▼
                                  TradingEnvironment
                                  _raw_feature_array() = compute + ffill + fillna(0)
                                  NO z-score. transform() not on any obs path.
                                  obs row = raw[step], PPO(MlpPolicy)

   E. ENGINE/SMA tick(): exogenous signal columns NEVER reach tick(); the engine
      builds {ticker, candles[-100:], order_book} only (engine.py:57-76).

   F. Model registry: models/{TICKER}/{NAME}/{model.zip, normalization.npz, config.yaml}
      EMPTY on disk this pass.

   G. EXPORT-DATA (WIP, untracked rl/export.py)  — the ONLY caller of transform()
      rebuilds train pipeline + writes CSV (+ opt-in --normalized z_ columns).
      The z-scored block it emits disagrees with the raw feature block, and a
      test verifying the alignment FAILS (see §3, failing test note).

   H. Decoys/latent: kraken-python exposes recent_trades()/spread() (manager.py:192,200)
      but nothing in the bot calls them; feature code for spread / bid / ask /
      bid_vol / ask_vol exists (features.py:380-393) but feeds nothing.
```

**Where each input enters the RL observation exactly:** `TradingEnvironment`
(`environment.py:180-197`) — if the pipeline has no stored stats it `fit()`s
on the whole frame, then `compute()` + `_raw_feature_array()`
(`environment.py:186-188,445-448`, ffill/zero-fill to `float32`); observed per
step via `_observe()` (`environment.py:367-375`). The `signals` group
(`features.py:395-408`) forwards columns in `_SIGNAL_COLUMNS` present in the
frame. Nothing else can enter the vector without editing code.

**Where data enters a strategy tick():** `engine.py:136` passes
`{ticker, candles[-100:], order_book}`; SMA reads candles + ticker only
(`sma.py:68-69`); the order book is fetched every loop and discarded. No
exogenous column ever reaches the engine loop.

---

## 2. CURRENT DATA SOURCES CATALOG

| # | Resource | Cadence | Consumer | Persistence | Evidence |
|---|---|---|---|---|---|
| 1 | Ticker REST | every engine tick (60 s) | SMA limit prices | none | `engine.py:59`, `sma.py:138-158` |
| 2 | OHLC 60 m (1 call, last ~720) | every engine tick | SMA closes (keeps 100) | none | `engine.py:65-66`; SMA ignores ~620 bars |
| 3 | OrderBook depth=10 | every engine tick | fetched, passed, **unused** | none | `engine.py:72`; SMA ignores |
| 4 | OHLC paged (`pages`, ≤720/call) | per train/backtest/paper tick | FeaturePipeline + env | optional store-upsert | `data.py:223-264 _page_candles`; ~30 d at 1 h |
| 5 | Local parquet store (sibling) | poller `*:*:15` **never run — store dir absent** | train/backtest/paper via `read_ohlc_dataframe` | month-sliced parquet | `kraken-market-data` module timer; **no store root on disk** |
| 6 | News sentiment JSONL | **manual (`cli.py pull`)** | `merge_extra_features` → signals | JSONL sibling | `data.py:328`, `default.yaml:57` |
| 7 | Funding/basis/OI JSONL | **manual (`cli.py pull`)** | `merge_extra_features` → signals | JSONL sibling | `data.py:329`, `default.yaml:65` |
| 8 | Social (StockTwits + F&G) JSONL | **manual (`cli.py pull`)** | `merge_extra_features` → signals | JSONL sibling | `data.py:330`, `default.yaml:71-77` |
| 9 | Deep OHLCV (Binance archive seeder) | **manual one-shot seed; never run** | store only | parquet | `kraken-deep-history`; store dir absent |
| 10 | (latent) `recent_trades`, `spread` | never called | — | — | `kraken-python/manager.py:192,200` |
| 11 | (latent) micro columns spread/bid/ask/bid_vol/ask_vol | — | code present, feeds nothing | — | `features.py:380-393` |

---

## 3. VERIFICATION FACTS (this pass, in the dev shell)

- `pytest tests/ -q`: **100 passed, 1 failed**.
  `test_build_export_frame_normalized_block_is_z_scored` fails because `z_*`
  (from `transform()`, stats fitted on the raw NaN-warmup frame) disagrees
  with a naive (x-mean)/std re-derivation of the ffilled observation columns
  (`test_rl_export.py:133-161`, assertion at line 157-161). This is exactly
  the Candidate-1 (normalization) mismatch bubbling up as a red test.
- Feature width: **49 features** with no exogenous columns merged; **58** with
  all 9 signal columns. `_SIGNAL_COLUMNS` in `data.py:50` and `features.py:36`
  currently agree (verified equal), but nothing tests that agreement.
- `export.py` is the ONLY caller of `FeaturePipeline.transform` in the repo.
- `models/` registry is empty; store root absent; no model config on disk
  carries `market_data_store` or any signal file.

---

## 4. RANKED CANDIDATE GAPS (≥3, spanning >1 category)

Ranked by (a) directness into the RL observation or `tick()`, (b) sourcing
cost, (c) differentiation. Category of each item explicitly stated. **The
highest-directness finding is a pure-code IMPROVE-EXISTING item — not a new
project.**

### CANDIDATE 1 — normalization.npz never shapes the observation; the new export test is RED on it *(IMPROVE-EXISTING, data-quality)*
- **Evidence:** `transform()` is reachable only from WIP `export.py:195`
  (`--normalized`) and `features.fit_transform` (never called). The policy
  trains and infers on `_raw_feature_array()`
  (`environment.py:445-448` = compute + ffill + fillna(0)), not z-scored
  output; `paper_trade.py:300-319` rebuilds the same raw row. Yet `train.py:238`
  **saves** and `backtest.py:161` / `paper_trade.py:188` **load**
  `normalization.npz`. Dollar-denominated `sma_24/ema_24/bb_upper_*/atr_*`
  (>1e3) sit beside unit-scale `return_*/volume_zscore` in the same Box; the 9
  signal columns are even more heteroscaled. And the new WIP test FAILS
  because the stats in `.npz` are fitted on the raw (NaN-warmup) frame while
  the agent's observation is the ffilled one.
- **Feed path:** direct — it IS the observation vector (`environment.py:186`).
  Fix = apply `transform()` (or fitted stats) before `_observe`, or delete the
  dead persistence. **Caution:** `prepare_episode` fits stats on the full
  frame before slicing (`data.py:534`), so z-scoring on leaks the held-out
  tail into stats (look-ahead); fit on the training slice only.
- **Sourcing cost:** zero (pure code). A precondition for any new-source
  column (Candidates 3/5) to be usable at comparable scale.

### CANDIDATE 2 — Market-data depth: the 720-bar / ~30-day REST clamp plus an absent store leaves nothing compensating *(data-quality + new-source)*
- **Evidence:** Kraken OHLC returns ≤720 bars/call regardless of `since`
  (`INTEGRATION.md`; RESEARCH-1 verified); single-call depth ≈30 d at 1 h.
  Engine loop: `engine.py:65` one un-paged `ohlc(60)` call → ~30 d, then
  `candles[-100:]` discards ~620 bars/tick. RL `pages=6` ≈ ~180 d at 1 h. The
  store seam fixes this but **the store root does not exist on disk** and
  `market_data_store:` is `null` everywhere (default.yaml:97); the
  `kraken-deep-history` seeder shipped last pass has not been run against a
  bot-visible root. `read_ohlc_dataframe` never passes `since`/`until` to the
  store (`data.py:407-453`); the documented INTEGRATION to-do (train/eval
  split, walk-forward via `reset(options=...)`) is open.
- **Feed path:** direct and big — deeper history + clean train/eval splits
  change what the feature pipeline and every train/backtest sees. Enabling the
  store + seeding it (or 1 m/5 m intervals for more bars) is the concrete fix;
  the seam is already written.
- **Sourcing cost:** low once the store is populated; seeding deep history is
  the only paid/scraped part (Binance archive, keyless). Note the poller is
  systemd-scheduled only on the store host, not in this bot's config.

### CANDIDATE 3 — Exogenous seams: manual pulls, no staleness signal, no scheduler, not flake inputs *(operations)*
- **Evidence:** news/funding/social pulls are all manual `cli.py pull` (each
  README); none of the three sibling projects ships a timer — only
  `kraken-market-data` has a systemd module+timer. The bot's `flake.nix:5-8`
  lists **only** `kraken-python` + `kraken-market-data` as inputs, so
  `nix develop` cannot even run the other three CLIs. `merge_extra_features`
  ffill + zero-fills missing hours (`data.py:168-171`), so a stale
  `funding_rate` / `sentiment_score` / `fng_index` is identical to a fresh one;
  no age/asof column, and `novelty_flag` is computed only at pull time.
- **Feed path:** indirect but cheap — systemd/cron to re-pull all three hourly,
  plus a `signal_age_hours` column so the agent (and the WIP export CSV) can
  discount staleness. Protects the three completed seams.
- **Sourcing cost:** zero for timers; ~1 column for freshness.
  **Sub-evidence:** funding settles ~8-hourly (`kraken-funding-rates
  models.py:25`); the model also emits `funding_rate_prediction` and
  `index_price`, but `_SIGNAL_COLUMNS` only forwards `funding_rate/basis/
  open_interest` — a sibling column already exists that the bot does not see.

### CANDIDATE 4 — Fetch-layer reliability: no retry, no throttle, uncached re-fetch every tick *(reliability, operations)*
- **Evidence:** `kraken-python/transport.py` min_interval defaults 0.0
  (`transport.py:108,113`) and has no retry/backoff — any
  `RequestException`/`RateLimitError` is raised immediately
  (`transport.py:210-219,249-250`); `KRAKEN_MIN_INTERVAL` exists (auth.py:42)
  but defaults to 0.0. The engine's 3 public calls/pair/60 s and the paper
  trader's 2 OHLC calls/tick are unthrottled; each failure is logged and only
  retried next tick (`engine.py:57-74`). The engine re-fetches the same
  ~100-bar window every 60 s with no cache, and the paper trader refetches +
  recomputes the whole episode window on every tick
  (`paper_trade.py:280-298`) — the append+tail optimization in
  `data.py:401-406` is a to-do.
- **Feed path:** cadence/reliability of every existing fetch; a small ttl-60 s
  bar cache and retry+backoff on the transport would give headroom for
  Candidates 2/3 without new requests.
- **Sourcing cost:** zero (pure code, same kraken-python fix benefits every
  sibling).

### CANDIDATE 5 — Market-microstructure / cross-exchange recorder: dormant feature group + dead order_book + unused recent_trades/spread *(microstructure)*
- **Evidence:** `_add_microstructure_features` (`features.py:380-393`) is ready
  (`spread`, `order_book_imbalance` coded) but emits nothing from the OHLC-only
  path; the engine fetches top-10 depth every loop and discards it
  (`engine.py:72`); `manager.recent_trades`/`spread` (`manager.py:192,200`)
  are unused. The store (Candidate 2) is the natural home for a
  book/trades recorder. Cross-exchange comparison is possible now that
  `kraken-deep-history` (Binance) feeds the store — Binance-vs-Kraken
  divergence for the same asset is one more per-(ticker, hour) vector.
- **Feed path:** a joined `spread`/`imbalance` column set would activate the
  group that today contributes 0 of 49 features — a genuine observation-width
  change.
- **Sourcing cost:** keyless endpoints, but medium-high operationally (poller
  + consolidation + storage); ride on Candidates 2/3.
  Sub-point: 1 m/5 m OHLC from the store is a cheaper "microstructure proxy"
  (bar-internal range/volume shape) than a true book recorder.

### CANDIDATE 6 — Foot-gun qualities in the merged exogenous columns *(data-quality, small)*
- **Evidence:** social's "missing F&G day" is emitted as `fng_index: 0`
  (`kraken-social-signals/pipeline.py:38,136` `_FNG_MISSING=0`), and 0 is also
  the genuine "extreme fear" end of the 0-100 scale — after the bot's zero-fill
  the agent cannot distinguish absence from panic. Raw-scale mention counts
  dwarf z-scored price features whenever Candidate 1 is ignored and Schedule 3
  enabled.
- **Feed path:** feature logic in `data.py:168-171` + sibling emitters.
- **Sourcing cost:** zero (encoding decisions).

### CANDIDATE 7 — On-chain / crypto-native signals *(new-source; lowest on directness-vs-cost)*
- **Evidence:** no on-chain source exists anywhere in the repo or the registry.
  Nothing in `_SIGNAL_COLUMNS`, configs, or the store covers chain activity
  (flows, whale movements, stablecoin supply, network/gas). The pipeline's
  only non-OHLC inputs today are news/funding/social JSONL.
- **Feed path:** a per-(ticker, hour) or per-(asset, day) vector joined the same
  way as the existing three seams; most on-chain feeds are asset- rather than
  pair-oriented, so directness is lower than Candidates 2-4.
- **Sourcing cost:** mostly API-key/paid (Glassnode, CryptoQuant) or scraped
  explorers; keyless free tiers exist but are sparse. Lowest priority by
  directness/cost.

---

## 5. SMALLER FINDINGS (not ranked)

- **New failing test in the tree:** `test_build_export_frame_normalized_block_is_z_scored`
  (untracked WIP) fails under `just test`; either the test or the export must be
  fixed before CI is green — and it is a live manifestation of Candidate 1.
- **`_SIGNAL_COLUMNS` duplicated** in `data.py:50` and `features.py:36`;
  currently in sync (verified), but nothing tests that agreement, and a new
  sibling column added to one is silently dropped from the observation.
- **All nine signal columns are three modalities in one allow-list** — the seam
  cannot widen without touching both tuples plus config docs.
- **`n_features()` is stateful** (`features.py:411-424`): returns the last
  computed width; after `load_normalization` it can disagree with the `.npz`
  until a fresh compute.
- **Engine and RL remain fully disjoint:** the strategy `tick()` and the RL
  observation share no exogenous feed and no store; two data worlds. `order_book`
  is fetched and thrown away each engine tick.
- **Paper trader observation is near-constant between hourly bars** yet the
  whole pipeline (2 live OHLC pages + 3 JSONL reparses + full-window compute)
  is redone every 60 s; `merge_extra_features` reparses each sibling JSONL on
  every call (3x per load, `data.py:328-330`), O(file) per paper tick.
- **Model provenance is absent:** `models/` is empty, so `backtest` /
  `paper-trade` against a freshly trained model records what it trained against,
  but no model on disk carries the seam keys; the two-key pre-`build_train_config`
  shape (only `ticker` + `model_name`) appears in earlier passes' history.
- **CLI help drift:** `paper-trade --help` mentions `rl-train` / `rl.train_ticker`
  in an error string (`cli.py:427-430`) while the actual subcommand is `train`;
  the `--models-root` default of `models` is hard-coded in two subcommands.
- **`.`data-audit/` is tracked** contrary to the command's "do not commit it"
  note; it includes this run's overwrite and prior RESEARCH-1/2/3 files.

---

## 6. BOTTOM LINE FOR THE DECISION PHASE

The biggest, cheapest, most observation-impacting finding is a pure-code
**IMPROVE-EXISTING** item: the normalization/normalization.npz stack is saved
and loaded but never applied, and the current working tree holds a *red test*
on exactly that seam (Candidate 1). Market-data depth (Candidate 2) remains
the stakeholder-level gap and the machinery already exists — a store seam +
Binance-archive seeder — but nothing has populated a store this host reads.
Operations (Candidate 3, scheduling + staleness for the 3 signal seams) and
fetch-layer reliability (Candidate 4, retry/throttle/cache) are zero-cost
improvements that protect every other item. Microstructure/cross-exchange
(Candidate 5) and on-chain (Candidate 7) are the two genuinely new-source
directions; both are lower directness and cost more. **No single category has
been committed to; the Decision phase must weigh an IMPROVE-EXISTING outcome
(Candidates 1/2/4) as highly as a NEW-DATA-SOURCE one.**

AUDIT COMPLETE