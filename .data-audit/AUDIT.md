# DATA-PIPELINE AUDIT — kraken-trading-bot

**Pass:** 2026-10-02 (read-only audit of the code as it stands; no solutions proposed)
**Auditor role:** Phase 1 spec — enumerate and rank gaps only. Do NOT pre-filter toward a category.

> **Scope note.** This is a fresh pass. The stale `RESEARCH*.md`, `DECISION.md`, `PLAN.md`,
> `VALIDATION.md` in `.data-audit/` and the duplicates at the repo root were deliberately
> **not** read as inputs to any conclusion below. Everything here is derived from the
> current source, the current configs, and one live reproduction (`python -c`, below).
> Two artifacts were OVERWRITTEN by this pass: this file only.

---

## 0. What was read

Source (all of it, not just the README):
`cli.py`, `kraken_trading_bot/{cli,engine,errors,__init__}.py`,
`kraken_trading_bot/rl/{agent,backtest,data,data_window,environment,export,features,paper_trade,registry,train}.py`,
`kraken_trading_bot/strategies/{base,sma}.py`,
`configs/{default,deep-history.example,matrix.example}.yaml`,
`tools/model_matrix.py` (2383 lines), `tests/*` (contract assertions only),
`nix/{default,module}.nix`, `flake.nix`, `pyproject.toml`, `justfile`,
`systemd/*.{service,service.in,timer}`, `.env.example`, `models/` (empty but for `.gitkeep`),
`exports/SOL_USD.csv`.

Outside the repo, read-only, for sourcing assessment only: `~/Projects/kraken-python`
(manager surface), `~/Projects/ticker-news-signals`, `~/Projects/kraken-social-signals`,
`~/Projects/kraken-market-data`, `~/Projects/kraken-deep-history`,
`~/Projects/kraken-funding-rates`.

Live state probed (not assumed): filesystem existence of `signals/` and the store root;
`systemctl --user is-enabled/is-active kraken-trading-bot-funding.timer`.

---

## 1. PIPELINE MAP (end to end)

### 1.1 The two disjoint pipelines

The repo contains **two separate trading systems** that share only a package name and the
`kraken_api` client. There is no code path between them.

```
PIPELINE A — classic strategy loop            PIPELINE B — RL pipeline
──────────────────────────────                ─────────────────────
cli.py:cmd_run  (--strategy sma)              cli.py:cmd_train / cmd_backtest
  │                                                │
engine.TradingEngine.run()  :151-172              ├─ train.train_ticker            train.py:129
  │  while self._running:                            │    ├─ build_train_config      train.py:93
  │    run_iteration()  :124-149                      │    ├─ read_ohlc_dataframe    data.py:864
  │      for pair in self.pairs:                       │    │    ├─ _resolve_store     data.py:1020
  │        fetch_market_data()  :46-76                │    │    ├─ _page_candles      data.py:730
  │          manager.ticker(pair)         :59         │    │    ├─ store.upsert        data.py:988
  │          manager.ohlc(pair,60)        :65         │    │    ├─ store.read          data.py:1003
  │          manager.order_book(pair,10)  :72  ──┐    │    │    ├─ add_derived_ohlcv_  data.py:627
  │        strategy.tick(market_data)   sma.py:57│   │    │    └─ merge_extra_...  x3 data.py:285
  │          reads candles + ticker ONLY ────┘    │    ├─ resolve_data_window   data_window.py:174
  │        (order_book DISCARDED)                  │    ├─ training_frame       data_window.py:293
  │      execute_signal()  :78-122                 │    ├─ prepare_episode      data.py:1062
  │        manager.buy / manager.sell  :104,:110   │    ├─ FeaturePipeline.fit  features.py:372
  └─ time.sleep(self.interval)   :168             │    ├─ TradingEnvironment   environment.py:118
    (default interval = 60 s, cli.py:69)           │    ├─ RLAgent.train        agent.py:72
                                                   │    └─ register_model       registry
                                                   ├─ backtest.backtest_model  backtest.py:319
                                                   ├─ export.build_export_frame export.py:123
                                                   └─ paper_trade.PaperTrader  paper_trade.py:83
```

### 1.2 Every `kraken_api` call site in this repo (exhaustive)

| # | Call | Site | Cadence | Auth | Error handling |
|---|------|------|---------|------|----------------|
| 1 | `manager.ticker(pair)` | `engine.py:59` | every `--interval` (60 s) per pair | keyless | bare `except` → WARNING, partial dict (`:60-61`) |
| 2 | `manager.ohlc(pair, interval=60)` | `engine.py:65` | same | keyless | bare `except` → WARNING (`:67-68`) |
| 3 | `manager.order_book(pair, count=10)` | `engine.py:72` | same | keyless | bare `except` → WARNING (`:73-74`) |
| 4 | `manager.buy/sell` | `engine.py:104,110` | on signal | **key+secret** | `except` → ERROR, return False (`:120-122`) |
| 5 | `manager.ohlc(pair, interval=, since=)` | `data.py:764` (`_page_candles`) | once per read, `pages` deep | keyless | **none** — no try, no retry, no backoff |
| 6 | `manager.buy/sell` | `paper_trade.py:507,510` | per paper tick | paper account | `except Exception` → ERROR, return False (`:522-524`) |
| 7 | `manager.trade_balance()` | `cli.py:388` | once (CLI) | key+secret | `except` → stderr, rc 1 |
| 8 | `manager.tickers(pairs)` | `cli.py:407` | once (CLI) | keyless | `except` → stderr, rc 1 |
| 9 | `manager.open_orders()` | `cli.py:431` | once (CLI) | key+secret | `except` → stderr, rc 1 |
| 10 | `KrakenManager.from_env()` / `.paper()` | `engine.py:356-358`, `data.py:836,979`, `paper_trade.py:151`, `backtest.py:394`, `cli.py:358,385,404,428` | construction | — | n/a |

**Never called anywhere in this repo**, though all exist on `KrakenManager`
(`~/Projects/kraken-python/kraken_api/manager.py`):
`recent_trades` (`:192`), `spread` (`:200`), `asset_pairs` (`:221`), `assets` (`:212`),
`balances` (`:229`), `balance` (`:234`), `closed_orders` (`:308`), `order` (`:316`),
`cancel*` (`:324-334`), `trade_history` (`:343`), `ledger` (`:352`),
`server_time` (`:127`), `known_pairs` (`:131`), `settle` (`:115`), `ws_token` (`:365`),
and every `ws_*` / WebSocket entry point. **No streaming feed is used anywhere.**

### 1.3 The OHLCV fetch → observation path (the load-bearing chain)

```
Kraken /0/public/OHLC
  └─ data._page_candles            data.py:730-771   loop on `last` cursor, up to `pages`
      └─ data.candles_to_dataframe data.py:691-727   -> cols time,open,high,low,close,vwap,volume,count
          └─ data.add_derived_ohlcv_features        data.py:627-688
          │     vwap_dev = close/vwap-1  |  trade_count_zscore_20 (roll 20)  |  volume_per_trade
          └─ for file in (extra_features_file,       data.py:853-860 / 1009-1016
                          funding_features_file,
                          social_features_file):
                data.merge_extra_features           data.py:285-501
                  JSONL -> ticker filter -> hour-floor -> last-per-hour
                  -> bounded ffill (signal_max_age_hours) -> zero-fill
                  -> + signal_age_hours, signal_observed
                  └─ rl/data_window.training_frame  data_window.py:293   (train)
                     rl/data_window.evaluation_frame data_window.py:315  (backtest)
                      └─ data.prepare_episode        data.py:1062  slice-then-fit
                          └─ features.FeaturePipeline.compute   features.py:447
                              price | technical | volume | microstructure | signals
                              └─ environment._raw_feature_array   environment.py:470
                                  ffill -> fillna(0) -> NormalizationStats.normalize (z-score)
                                  └─ handed to PPO as the observation vector
```

### 1.4 Where each input enters — the exact seams and config keys

| Input | Config key | Shipped value | Read at | Enters observation via |
|-------|-----------|---------------|---------|------------------------|
| OHLCV (spot, Kraken) | `ohlcv_interval_minutes` | `60` | `train.py:189`, `backtest.py:397` | `price`/`technical`/`volume` groups (`features.py:470-479`) |
| — `vwap`, `count` (in the wire payload) | *(no key — always parsed)* | `data.py:717,719` | `data.py:668,676` | `signals` group (`vwap_dev`, `trade_count_zscore_20`, `volume_per_trade`) |
| News / sentiment | `extra_features_file` | **`null`** | `train.py:198`, `backtest.py:405`, `paper_trade.py:303`, `export.py:170` | `signals` (`sentiment_score`, `article_count`, `novelty_flag`) |
| Funding / basis | `funding_features_file` | **`~/Projects/kraken-trading-bot/signals/eth_usd_funding.jsonl`** | same four sites | `signals` (`funding_rate`, `basis`, `open_interest`, `funding_rate_prediction`, `vol24h`) + micro builder → `spread` (`features.py:575-580`) |
| Social / sentiment | `social_features_file` | **`null`** | same four sites | `signals` (`stt_mention_count`, `stt_tilt`, `fng_index`) |
| Staleness bound | `signal_max_age_hours` | `12` | `data.py:204-235` | via `signal_age_hours` (an observation column itself) |
| Ticker guard | `signal_require_ticker` | `true` | `data.py:504-591` | raises `SignalTickerMismatchError` |
| Deep OHLCV history | `market_data_store` | **`null`** | `train.py:203`, `backtest.py:412`, `paper_trade.py:308`, `export.py:175` | replaces the fetch leg entirely (`data.py:975-1005`) |
| Order book | **NO KEY, NO PATH** | fetched at `engine.py:72` | never | **never — discarded before `tick()`** |
| Ticker (live) | `NO KEY` | fetched at `engine.py:59` | `sma.py:69` → only `ask`/`bid` for a limit price (`sma.py:140,154`) | never reaches the RL space |

**Ticker normalisation path (verified):**
`--ticker ETH_USD` → `build_train_config` → `pair_from_ticker_id` (`train.py:57-66`) =
`normalize_ticker_id(...).replace("_","/")` (`features.py:691-703`) → `ETH/USD`.
`--ticker USD_SOL` → `USD/SOL` → Kraken rejects it.

### 1.5 Storage / caching — what is persisted and what is not

| Thing | Persisted? | Where | Evidence |
|-------|-----------|-------|----------|
| OHLCV bars | **only if** `market_data_store` is set | parquet month-slices + `_meta.json` cursor | `data.py:975-1005`; store root `~/Projects/kraken-market-data/store` **DOES NOT EXIST** |
| Signal records | forward-append-only JSONL, one line per pull | `signals/*.jsonl` | `systemd/*.service:34` — "The file grows by ~1 line per hour, so it is a log, not state." `signals/` **DOES NOT EXIST** |
| Trained model | yes, when a run happens | `models/{TICKER}/{NAME}/{model.zip,normalization.npz,config.yaml}` | `train.py:281-283`; `models/` is **empty but for `.gitkeep`** |
| Normalisation stats | yes | `normalization.npz` (18 `REQUIRED_BACKTEST_FIELDS`-adjacent contract) | `features.py:177-219` |
| Equity curve / trades | in-memory only, then one CSV row-set | `BacktestResult.equity_curve` | `backtest.py:165,187` |
| Fetched raw response | **never** | — | no HTTP cache, no memo, no backfill |

---

## 2. Verification of the prior-pass claims I was told to check

| Claim | Verdict | Evidence |
|-------|---------|----------|
| "Exogenous signals ride JSONL seams: news, funding/basis, social" | **TRUE as code, FALSE in practice.** All three seams exist and are well built; **none is populated.** `extra_features_file: null`, `social_features_file: null`, and `funding_features_file` points at a path under a `signals/` directory that **does not exist**. Worse — see G1 — that path is **structurally unreadable even once it exists.** | `configs/default.yaml:57,79,87`; `ls signals/` → *No such file or directory*; `data.py:363-368` |
| "`market_data_store` is an OHLCV parquet store that removes the ~720-bar REST ceiling" | **TRUE as an integration, FALSE as an operational state.** `_resolve_store` + fetch→upsert→read are real and tested. The default is `null` (live fetch) and the store root in `deep-history.example.yaml` does not exist on disk. | `data.py:864-1059`; `configs/default.yaml:142`; `configs/deep-history.example.yaml:81`; `ls ~/Projects/kraken-market-data/store` → *No such file or directory* |
| "`_SIGNAL_COLUMNS` is duplicated across >1 file; a prior pass proposed reading it from config" | **RESOLVED — no longer duplicated.** There is exactly **one** definition (`features.py:46-77`); `data.py:48` imports it. `grep` across the repo finds no second literal. **A drift is no longer possible through duplication.** However there is a *live* residual risk of the opposite kind — see G1. | `features.py:46`; `data.py:46-51,426-427`; `tests/test_rl_environment.py:435-438`; `tests/test_rl_signal_config_wiring.py:550-596` assert membership, so a *removal* would be caught by tests |
| "`tools/model_matrix.py` exists; prior run concluded no effect is statistically resolvable at 3 seeds / 178-bar eval slices" | **TRUE, and the mechanism is reproducible from the source.** See G4 — the harness encodes `MIN_REPLICATES_FOR_A_CLAIM = 3` as a *static constant* and never compares any observed spread. The 178-bar figure is the harness's own recorded measurement of a 672-bar window's eval slice. | `tools/model_matrix.py:230`, `:96-101`, `:540-579`, `:560-567` (the "178 replayed" measurement is quoted verbatim at `:563-566`) |
| "`--ticker` expects the real Kraken base asset: `ETH_USD` works, `USD_SOL` does not" | **TRUE, mechanism confirmed.** `pair_from_ticker_id` is a blind `replace("_","/")`; there is no quote/base reordering anywhere. | `train.py:57-66`; `features.py:691-703`; `tools/model_matrix.py:1067` repeats the same substitution for matrix cells |
| "Live training windows are NOT reproducible while `market_data_store: null`" | **TRUE, and it is worse than 'not reproducible'** — see G1: the *signal* half of the window is not merely unstable, it is **absent**, and deterministically so. | `data.py:949-961` (null store → byte-identical live fetch); `data_window.py:26-33` (unpinned is inert); `configs/default.yaml:181-184` (`since/until: null`) |

---

## 3. RANKED CANDIDATE GAPS

**Ranking key = DIRECTNESS**: how directly the fix lands in the RL observation vector or a
strategy's `tick()`. Secondary key = how cheap the input is to obtain.
Categories are deliberately **not** pre-filtered; the list spans improve-existing,
data-quality/reliability, market microstructure, historical depth, and measurement validity.

---

### G1 — The exogenous-signal seam is fully built, fully wired, and structurally unreachable
**Category:** IMPROVE-EXISTING (dead machinery) + data-quality/reliability · **Directness: 5/5** · **Sourcing: keyless**

This is the single highest-directness finding in the audit. Twelve observation columns are
already in the canonical allow-list, already forwarded by the `signals` feature group, and
already counted in the fitted width — and **not one of them ever receives a value.**

**Evidence.**

1. **`Path()` without `expanduser()` — the shipped path can NEVER resolve.**
   `data.py:363` is `path = Path(extra_features_file)`. `configs/default.yaml:79` ships
   `funding_features_file: ~/Projects/kraken-trading-bot/signals/eth_usd_funding.jsonl`.
   `yaml.safe_load` keeps the literal `~`, and `Path("~/…")` resolves against the **CWD**,
   not `$HOME`. Reproduced live:

   ```
   file really exists (expanded): True
   what data.py:363 does        : False   <-- merge seam SKIPS the file
   what data.py:363 needs        : True
   ```
   (probe: a real file at `~/tmp-audit-proof/probe.jsonl`, read through both spellings.)
   So even after `just funding-timer` creates and populates the file, `data.py:364`
   `if not path.is_file(): _LOGGER.warning(...); return df` **silently drops every funding
   record.** The codebase already knows to expanduser in the neighbouring call —
   `backtest.py:219` is `Path(config_path).expanduser()` — so this is an internal
   inconsistency, not an unfamiliar pattern.

2. **The `signals/` directory does not exist at all.**
   `ls /home/seanc/Projects/kraken-trading-bot/signals/` → *No such file or directory*.
   And the systemd timer that would create it is **not installed**:
   `systemctl --user is-enabled kraken-trading-bot-funding.timer` → **`not-found`**;
   `is-active` → `inactive`. The unit exists only as an unsubstituted template
   (`systemd/kraken-trading-bot-funding.service.in`) plus a `.timer` symlink target.

3. **One timer exists for three signal sources.**
   `systemd/` contains exactly `kraken-trading-bot-funding.{service,service.in,timer}`.
   There is **no news timer and no social timer**, and `justfile:135` provides only
   `funding-timer` / `funding-pull`. So even with G1.1 fixed, `extra_features_file: null`
   and `social_features_file: null` have no producer behind them.

4. **The affected columns are already wired end to end.**
   `_SIGNAL_COLUMNS` (`features.py:46-77`) already lists `sentiment_score`,
   `article_count`, `novelty_flag`, `funding_rate`, `basis`, `open_interest`,
   `stt_mention_count`, `stt_tilt`, `fng_index`, `funding_rate_prediction`, `vol24h`,
   `signal_age_hours`, `signal_observed`. `_add_signals_features`
   (`features.py:587-605`) forwards every one of them that is present. The merge seam
   (`data.py:285-501`) already does ticker filtering, hour-flooring, de-duplication,
   bounded carry and freshness provenance. **Zero feature-engineering work is required to
   move the observation from 49 to 55+ columns.**

5. **`spread` is collateral damage.** `spread` is built only from `bid`/`ask`
   (`features.py:575-580`), and `bid`/`ask` exist only because the *funding* sibling emits
   them (`configs/default.yaml:66-71`). With no funding file, the `microstructure` group
   produces **no columns at all** — the one feature group that is genuinely missing, as
   opposed to the other four which are fully populated.

**Consequence.** Every model trained or evaluated with the shipped default config saw a
49-wide observation in which all twelve exogenous columns were constant zero, and in which
`signal_observed` was uniformly 0.0 — i.e. the agent was *told* "no reading behind this"
on every single bar. This is not a thin-data problem; it is a silently-degraded pipeline
whose degradation is invisible in every downstream number.

**Sourcing difficulty: keyless, trivial.** The funding sibling's own service file states
it — "Keyless (public Kraken Futures ticker)" (`systemd/*.service:28`).
`ticker-news-signals` (Google News RSS + local VADER) and `kraken-social-signals`
(StockTwits v2 + alternative.me) are both documented as keyless-by-design with retry/backoff
already built in their own READMEs.

---

### G2 — Order-book depth is already being paid for and thrown away; the one feature that wants it has no producer
**Category:** market microstructure · IMPROVE-EXISTING · **Directness: 4/5** · **Sourcing: keyless (live only)**

**Evidence.**

1. `engine.py:70-74` — `data["order_book"] = self.manager.order_book(pair, count=10)` runs
   **inside the 60-second loop**, once per pair, forever.
2. `strategies/base.py:92` documents `order_book` as one of the three canonical `tick()`
   inputs.
3. `strategies/sma.py:57-116` — `tick()` reads `data.get("candles")` and `data.get("ticker")`
   and **nothing else**. `grep -rn order_book kraken_trading_bot/strategies/` returns
   exactly one hit: the `base.py` docstring line. Ten levels × two sides of live book depth
   per pair per minute reaches `tick()` and is never touched.
4. `features.py:581-585` — `order_book_imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)`
   is the **only** reader of `{bid_vol, ask_vol}` in the entire repository, and
   `grep -rn "bid_vol\|ask_vol"` across this repo **and all four sibling projects** returns
   **zero producers**. The branch is unreachable code: it can only fire if a caller
   hand-builds those two columns, which is exactly what `tests/test_rl_environment.py:222-225`
   does and nothing else does in production.
5. The RL side never sees the order book at all — `grep` for `order_book` in
   `rl/backtest.py`, `rl/export.py`, `rl/data.py`, `rl/features.py` returns only the
   `order_book_imbalance` builder. The two pipelines do not even share this resource.

**The honesty constraint that must shape any fix.** `manager.order_book` maps to Kraken's
`/0/public/Depth` (`~/Projects/kraken-python/kraken_api/manager.py:185-190`), which is a
**live snapshot with no `since` cursor and no pagination**. Depth therefore *cannot* be
reconstructed for a 6-week training window. Snapshotting the book once at load time and
forward-filling it across a historical frame would fabricate history — and because
`forward-fill` is exactly what the merge seam already does for every other signal
(`data.py:449-463`), that mistake is one line away. Any design here must either (a) build a
**forward-only** recorder whose output is consumed only by paper/live replay and never by
training, or (b) declare the feature live-only and excluded from `feature_groups` at
training time. Silently widening the historical width with a fabricated column is the
failure mode to rule out.

**Contrast, and it changes the ranking.** Two *other* unused Kraken resources —
`recent_trades` (`manager.py:192`) and `spread` (`manager.py:200`) — both take a `since`
cursor and return one (`-> tuple[list[Trade], str]`, `-> tuple[list[SpreadPoint], str]`).
They are keyless, cursor-paginated, and therefore **historically reconstructible** in a way
Depth is not. They have zero call sites here. On evidence, the already-running-but-unusable
Depth call is the cheaper producer for a *live-only* feature, while `recent_trades` /
`spread` are the better-sourced producers for anything that must appear in a training
window.

---

### G3 — Historical depth is capped at ~721 bars, and the machinery that lifts the cap is unseeded and under-wired
**Category:** thin historical depth vs. what the models need · **Directness: 4/5** · **Sourcing: free public bulk archives**

**Evidence.**

1. `configs/default.yaml:142` — `market_data_store: null`. `data.py:949-961` makes `null` a
   **byte-identical pass-through** to the live fetch, so the deep-history branch
   (`data.py:975-1017`) never executes in the shipped configuration.
2. The store root in `configs/deep-history.example.yaml:81`
   (`~/Projects/kraken-market-data/store`) **does not exist**. The sibling repo is present;
   its store has never been seeded.
3. The ceiling is real and the codebase knows it. `tools/model_matrix.py:220-226` carries
   `KRAKEN_REST_BAR_CEILING = 720` with the measured note "`--pages 2` has been measured
   returning 721 bars, not 1440", and `configs/matrix.example.yaml:115-120` repeats it.
   `data.py:943-947` carries a `.. todo::` naming the push-down as unfinished work.
4. **The window bounds are a CLIP, not a push-down.** `data_window._window_mask`
   (`data_window.py:212-237`) filters the frame *after* the read. So a pinned window still
   needs `--pages` large enough to reach `until`, and the harness warns about exactly this
   (`tools/model_matrix.py:1374-1397`, `pages_may_undershoot`).
5. **The arithmetic this creates.** `configs/matrix.example.yaml:191-194` pins
   `2026-09-01 → 2026-09-20` (456 bars) with `eval_split: 0.7`. Training takes the leading
   319, so the backtest's out-of-sample surface is ~137 bars. The harness's own recorded
   measurement of a real run is more pessimistic: a 672-bar window logged "202 replayable
   bars", "of which 178 were replayed after feature warm-up"
   (`tools/model_matrix.py:560-567`). One of those runs is *already below*
   `min_bar_ratio: 0.5` against a naive denominator, which is why that denominator was
   rewritten.
6. `rl/data.py:966-974` states the remaining wiring in its own comment: with a seeded store,
   "since/until slicing, train/eval split, and walk-forward … [becomes] possible — a
   follow-up pass".

**Directness.** `read_ohlc_dataframe` already forwards `since`/`until` straight to
`store.read` (`data.py:1003`), and `training_frame`/`evaluation_frame` already slice
correctly. The two remaining pieces are (a) seed the store once and (b) push the window
bounds into the read so `--pages` stops under-shooting. Both are plumbing on an existing,
tested seam (`tests/test_rl_data_store.py`).

**Caveat a researcher must carry.** `configs/deep-history.example.yaml:9-19` — the seeder
fills the store from **Binance** archives under a **Kraken**-labelled pair id
(`ETH/USD` ← Binance `ETHUSDT` spot). `DOGE/USD` has no mapping and is refused. Deep
history therefore introduces a **venue/basis discontinuity** at the seam between archive
bars and live Kraken bars. Any feature whose normalisation spans that boundary
(`obv`, `volume_zscore_20`, and especially the z-scored observation itself) is fitted on a
mixture of two venues.

---

### G4 — Measurement power is a hard-coded threshold, never a measurement; no aggregate is ever checked for overlap
**Category:** measurement validity · IMPROVE-EXISTING · **Directness: 3/5 to the feature space, 5/5 to "can any conclusion be drawn"** · **Sourcing: n/a (no new data)**

**Evidence.**

1. `tools/model_matrix.py:230` — `MIN_REPLICATES_FOR_A_CLAIM = 3`. Used at `:1239-1246`
   (a WARN when a spec has fewer seeds) and `:1302-1310` (a NOTE for thin per-config
   groups), and again in `_build_claims` prose at `:2210-2227`. It is a **static literal**.
   It is never compared against any *observed* spread.
2. The harness documents, in its own words, why a fixed 3 is not enough: "PPO on an
   IDENTICAL config has been measured replaying 576 trades in one run and 374 in another —
   a 1.5x swing from nothing but the seed" (`tools/model_matrix.py:96-101`, restated at
   `:2205-2208` and `:2218-2220`).
3. `summarize` (`:315-325`) computes `median, q1, q3, min, max` per group, and
   `_print_group_table` (`:1874-1898`) prints `median [q1, q3]` — **but no code anywhere
   compares two groups' intervals for overlap.** Two per-axis marginals whose IQRs sit on
   top of each other are rendered identically to two that are cleanly separated. The reader
   is asked to eyeball what the tool could compute.
4. This compounds with G3 rather than adding to it: at ~178 replayed bars, the bar-to-bar
   variance of the equity path is large relative to any plausible config effect, so the
   minimum detectable difference is high — and it is not printed. **Power is a function of
   depth; the tool knows the depth (`n_bars`) and the spread, and reports neither against
   the other.**
5. The one honest counter-measure that already exists is `_build_claims`'s prose
   (`:2105-2295`), which correctly refuses to endorse thin groups and in-sample cells. That
   is mitigation, not measurement.

**Why this belongs in a *data* audit.** It is the reason a data-quality finding like G1 or
G3 can go unnoticed for passes at a time: the harness will happily tabulate a median for a
configuration whose effect is smaller than its own seed noise, and print it in the same
typeface as a real one. **Measurement power is the load-bearing precondition for every
other finding in this document having any effect on a decision.**

---

### G5 — No retry, no backoff, no partial-failure accounting on any fetch path; and paper-trade is killed by the wrong failure
**Category:** data-quality / reliability of what is already pulled · **Directness: 2/5 to the feature space, 4/5 to trust in it** · **Sourcing: n/a**

**Evidence.**

1. `data._page_candles` (`data.py:763-771`) is a bare loop:
   `batch, last = manager.ohlc(...)`. **No `try`, no retry, no backoff, no sleep.** One
   transient 5xx or connection reset aborts `train`, `backtest` and `paper-trade`
   mid-read, with no partial result and no resumption. Contrast: the *sibling* producers
   (`ticker-news-signals`, `kraken-social-signals`) document retry/backoff-on-429 and
   non-fatal Cloudflare-403 handling in their own READMEs — this repo is the weaker half of
   the pipeline it depends on.
2. `engine.fetch_market_data` (`engine.py:57-74`) wraps each of its three calls in
   `except Exception: _LOGGER.warning(...)` and **continues**, returning a **partial dict**.
   `run_iteration` (`engine.py:134-149`) then calls `strategy.tick(market_data)` regardless.
   `sma.tick` does `candles = data.get("candles", [])` (`sma.py:68`) → empty →
   `Signal(action="hold", reason="insufficient data")` (`sma.py:72-76`). **A total API outage
   and a strategy with nothing to say are indistinguishable**: the loop runs forever,
   logging nothing above WARNING, trading nothing, reporting nothing.
3. `run_paper_trader` (`paper_trade.py:648-671`) catches **only `KeyboardInterrupt`**.
   `PaperTrader.step` raises `NotEnoughDataError` when the fetch returns nothing
   (`paper_trade.py:387-388`) — which propagates straight out of the `while` loop and ends
   the session. Meanwhile `_execute_signal` deliberately swallows *order* failures into
   `return False` (`paper_trade.py:522-524`). **The asymmetry is inverted**: an order
   failure is survivable and logged, a data failure kills the process. For a
   24/7 ticker loop that is exactly backwards.
4. `KRAKEN_MIN_INTERVAL` is documented as a rate-limit knob (`.env.example:10-11`,
   `nix/module.nix:149-153`) but is **never referenced anywhere in this repo** — it is
   delegated to the kraken-python client, which this repo never configures. The RL fetch
   loop's request rate is therefore whatever the library default is, with `pages` as the
   only lever.

---

### G6 — The signal channel is a forward-only append log; exogenous history cannot be backfilled, unlike OHLCV
**Category:** data-quality / improve-existing (architectural asymmetry) · **Directness: 3/5** · **Sourcing: sibling-dependent**

**Evidence.**

1. The funding service file states the design in its own words: "One reading per pull,
   appended… The file grows by ~1 line per hour, so **it is a log, not state**"
   (`systemd/kraken-trading-bot-funding.service:28-34`).
2. Consequence: the exogenous channels have **no history before the log's own start**. Any
   training window older than the first line has exactly zero funding coverage, and — per
   G1 — currently *all* windows have zero coverage.
3. **This is the opposite of the OHLCV channel.** The market-data store is a real
   parquet store with month-slice files and a `_meta.json` cursor, read back arbitrarily
   far into the past (`data.py:1003`). So one pipeline has a backfillable store and the
   other has a tape.
4. The merge seam is, structurally, a *forward* seam: `merge_extra_features`
   (`data.py:285-501`) does an hour-floored left-join of a file that is only ever appended
   to. Nothing in this repo can ask "what did the funding rate look like on 2025-03-04?"
   and get an answer unless the file already contains that line.
5. Practical consequence for the matrix: `tools/model_matrix.py:191-194` pins a window to
   **2026-09-01 → 2026-09-20**, i.e. the recent past. The pinned window is therefore
   *shorter than the exogenous log could ever be* unless the log is backfilled — so the
   example matrix and a backfillable signal channel are, today, mutually exclusive designs.

---

### G7 — `--ticker` is a blind `_`→`/` substitution; a base/quote reversal produces a Kraken error, not a config error
**Category:** improve-existing / data-entry reliability · **Directness: 2/5** · **Sourcing: n/a**

**Evidence.**

1. `train.py:57-66` — `pair_from_ticker_id` is
   `normalize_ticker_id(ticker_id).replace("_", "/")`. `normalize_ticker_id`
   (`features.py:691-703`) only upper-cases and rewrites `/`, `-`, `.` to `_`. There is no
   base/quote awareness anywhere in the repo.
2. `ETH_USD` → `ETH/USD` (correct) **only because the user happens to write base first**.
   `USD_SOL` → `USD/SOL` → Kraken answers `Unknown Kraken pair: 'USD/SOL'`. Confirmed
   against the known-context behaviour.
3. The substitution is duplicated in the matrix harness:
   `tools/model_matrix.py:1067` (`base["ticker"] = cell.ticker.replace("_", "/")`) and
   again at `:1077`, `:1083` — so a spec author writing `ticker: [USD_ETH]` produces
   twelve identically-broken cells.
4. `classify_process_failure` (`tools/model_matrix.py:868-923`) classifies
   `config_not_found`, `action_space_mismatch`, `no_tradable_bar` and generic
   `process_failed` — but has **no case for an unknown pair**, so twelve cells would all be
   recorded as opaque `process_failed` with a Kraken error string in `stderr_tail`.
5. CLI help says only "e.g. ETH_USD" (`cli.py:139-142`, `cli.py:104-109`) and never states
   the base-asset-first requirement.

---

### G8 — `TradingEnvironment.reset(options=...)` is ceremonial; walk-forward is unreachable and three episode-slicing mechanisms coexist
**Category:** improve-existing · **Directness: 3/5** · **Sourcing: n/a**

**Evidence.**

1. `environment.py:216-242` — `reset(*, seed=None, options: dict | None = None)` accepts
   `options` and explicitly discards it ("Unused; reserved for future start-bar
   overrides"). `options` is the only gymnasium-native way to start an episode mid-frame, so
   the environment **cannot walk forward**: every episode begins at `_start_index`
   (`environment.py:194, 232`) and runs to the end of the frame.
2. The codebase itself flags this in three separate places: `data.py:943-947` (`.. todo::`),
   `data.py:966-974`, and `data_window.py:93-98`.
3. Three unrelated slicing mechanisms now coexist with nothing reconciling them:
   * positional `df.tail(episode_bars)` in `data.prepare_episode` (`data.py:1107-1110`);
   * `eval_split` positional slicing in `data_window.{training,evaluation}_frame`
     (`data_window.py:293-330`);
   * the hard-coded `_FETCH_PAGES = 2` window that bounds paper-trade's live observation
     (`paper_trade.py:65, 300-301`) — a magic number that is **not** configurable and does
     not appear in any config file.
4. Anchored/rolling walk-forward is the natural next validation step once G3 supplies
   depth, and it is blocked on ~10 lines of already-signposted code.

---

## 4. CONSIDERED, EVIDENCED, RANKED LOWER

Recorded so the category coverage is explicit rather than an omission.

| Candidate | Category | Evidence in repo | Directness | Sourcing | Why ranked lower |
|-----------|----------|------------------|-----------|----------|------------------|
| Text/news signal | text/news | Seam + 3 columns exist (`features.py:47-49`); `extra_features_file: null`; **no news timer in `systemd/`** | 5/5 | keyless (Google News RSS + local VADER) | **Not a new gap** — it is G1's third unbacked channel |
| Social/sentiment | social | Seam + 3 columns exist (`features.py:53-55`); `social_features_file: null`; **no social timer** | 5/5 | keyless (StockTwits v2, alternative.me; F&G has full history from 2018 in one call) | Same — a G1 sub-case |
| `recent_trades` / `spread` tape | microstructure | Zero call sites; both cursor-returning and keyless (`manager.py:192,200`) | 3/5 | keyless | Strong *sourcing*, weaker *existing-machinery* case than G2; the historically-reconstructible option |
| Macro / economic calendar | macro | **No consumer, no config key, no seam anywhere** | 1/5 | keyless (public calendars) | Would require an entirely new seam + producer + timer for a signal with no existing plumbing |
| On-chain / crypto-native | on-chain | **No consumer, no config key, no seam anywhere** | 1/5 | keyless RPC / public explorers | Same |
| Research / academic signal | research | Sibling `~/Projects/researcher-python` exists on disk but **nothing in this repo references it** | 1/5 | mixed | No seam, no config key, no producer |
| Cross-exchange | microstructure | **Nothing**; the store is Kraken-or-Binance-archive only (`configs/deep-history.example.yaml:9-19`) | 2/5 | keyless | Structurally the largest lift: no second venue in the pipeline at all |

---

## 5. WHAT THE AUDIT FOUND, IN ONE PARAGRAPH

The pipeline's **feature engineering is in good shape** and the **exogenous-signal seam is
the best-built thing in the repo** — ticker filtering, hour-flooring, de-duplication,
bounded carry, freshness provenance and a non-self-referential width guard, all with tests
that pin the membership of the allow-list. The problem is not that the machinery is
missing. It is that **almost none of it is switched on**: the funding path shipped in
`configs/default.yaml` is unreadable by construction (`Path()` without `expanduser()`,
`data.py:363`, reproduced live), the directory it points at does not exist, the timer that
would create it is not installed, and the other two channels are `null`. Twelve allow-listed
observation columns and the entire `microstructure` feature group are therefore permanently
zero, and the agent is told `signal_observed == 0.0` on every bar. Behind that, historical
depth is pinned at ~721 bars because the store that lifts the ceiling has never been seeded
and the pinned window is clipped rather than pushed down. And behind *that*, the measurement
harness would tabulate a median for any of it anyway, because its replicate threshold is a
literal `3` that is never checked against the spread it is supposed to be separating from.

---

AUDIT COMPLETE