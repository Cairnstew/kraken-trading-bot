# Data Pipeline Audit — 2026-10-01

Pass 5+ on `kraken-trading-bot`. Read-only audit; the only file written is this
one. Prior-pass artifacts (`PLAN.md`, `RESEARCH*.md`, `DECISION.md`,
`VALIDATION.md`) were read first; this audit is scoped to what is *still* missing
after the two IMPROVE-EXISTING passes landed (`2b3e065`/`5df99ef` normalization
wiring, `5951f72`/`1e404ba` the A1–A4 signal-seam repair).

---

## Repo state

- **Commit**: `ca360a8` ("docs: audit-pipeline run log + guidance fixes
  2026-10-01"), branch `master`, clean except untracked `models/{ETH,SOL,XRP}_USD/`.
- **Prior AUDIT.md** is preserved at `18343ce`.
- **Gate state last pass**: `VALIDATION.md` §5 **GATE: PASS**, 119 tests green,
  `nix flake check` green.
- **Layout** (verified):
  ```
  kraken_trading_bot/
    cli.py            (22.5K) — 7 subcommands
    engine.py         (5.7K)  — SMA-strategy loop, NOT the RL path
    errors.py, __init__.py
    rl/
      agent.py backtest.py data.py (45K) environment.py export.py
      features.py paper_trade.py registry.py train.py __init__.py
    strategies/
      base.py sma.py __init__.py
  configs/default.yaml  configs/deep-history.example.yaml
  nix/module.nix  nix/default.nix  flake.nix
  tests/  (11 files, 119 tests)
  ```
- **Notable**: `models/{ETH,SOL,XRP}_USD/` are now **untracked but present** —
  6 trained models exist on disk (2 seeds × 3 tickers), all with
  `extra_features_file: null`. This is new since the last pass, which recorded
  `models/` as empty. It does not create a retrain burden (all 6 are null-signal),
  but it does mean the *width-guard* deferral now has real artifacts to protect.

---

## Pipeline map (end to end)

### The RL path (what actually feeds the model)

```
CLI (train/backtest/export-data/paper-trade)
  └─ cli.py:322-398,449-530,597-...            KrakenManager.from_env() / .paper()
      └─ rl/train.py:185-194   read_ohlc_dataframe(pair, interval, pages=6, ...)
          └─ rl/data.py:790  read_ohlc_dataframe
              ├─ [market_data_store is None → data.py:875-887 pass-through]
              │     └─ data.py:706  fetch_ohlc_dataframe
              │         ├─ data.py:770 → _page_candles (data.py:662)
              │         │     └─ data.py:696  manager.ohlc(pair, interval, since=cursor)   ← THE ONLY DATA CALL
              │         │        kraken-python manager.py:163 → client.depth? no → client.ohlc
              │         └─ data.py:779-786  merge_extra_features ×3 (news/funding/social)
              └─ [market_data_store set → data.py:901-940]
                    ├─ _resolve_store(data.py:943) → market_data.MarketDataStore (lazy import, data.py:969)
                    ├─ data.py:912-914  _page_candles → store.upsert(pair, interval, candles)
                    ├─ data.py:929      store.read(pair, interval, since=, until=)
                    └─ data.py:932-939  merge_extra_features ×3
          └─ rl/features.py:280  FeaturePipeline.compute(df)
              ├─ :303 price         _add_price_features     (:357)
              ├─ :305 technical     _add_technical_features (:371)
              ├─ :307 volume        _add_volume_features    (:393)
              ├─ :309 microstructure _add_microstructure_features (:405)  ← DEAD (see Gap 3)
              └─ :311 signals       _add_signals_features   (:420)  ← the only exogenous door
          └─ rl/environment.py:445 _raw_feature_array → ffill → fillna(0) → stats.normalize
              └─ environment.py:367 _observe → PPO policy
```

### The strategy path (separate, and it does NOT touch the RL feature space)

```
cli.py:318 cmd_run → engine.py:151 TradingEngine.run  (sleep(interval), default 60 s)
  └─ engine.py:130 run_iteration → engine.py:46 fetch_market_data(pair)
      ├─ engine.py:59   manager.ticker(pair)          → data["ticker"]
      ├─ engine.py:65   manager.ohlc(pair, interval=60)→ data["candles"][-100:]
      └─ engine.py:72   manager.order_book(pair, count=10) → data["order_book"]   ← DEAD (see Gap 3)
      └─ strategies/sma.py:62 tick(data) → Signal
          reads ONLY data["candles"] and data["ticker"] (sma.py:68-69)
```

**Critical structural fact**: `TradingEngine` is imported only by `cli.py:11`
(and `tests/test_engine.py`). Nothing under `kraken_trading_bot/rl/` imports it.
There are **two disjoint data pipelines in one repo**: the RL path fetches OHLCV
via `_page_candles`; the engine path fetches ticker+candles+book via
`TradingEngine.fetch_market_data`. They share no code, no cache, no column.

### Merge seam (the one door every exogenous column passes through)

`data.py:281 merge_extra_features` — repaired by `5951f72`/`1e404ba`. Now:
ticker filter (`data.py:391`→`_filter_ticker:500`), hour dedup
(`data.py:410-415`), bounded carry + freshness (`data.py:441-459`,
`_signal_ages:234`), absence≠neutral (`data.py:467-471`). Allow-list at
`data.py:420-424` intersected with `features._SIGNAL_COLUMNS` (`features.py:42-54`).
Called **3× per read** on both legs (`data.py:779-786`, `data.py:932-939`) — so
a 60 s paper tick re-parses 3 JSONL files and re-reads the whole store.

### Store / cache layer

- `kraken-market-data` → `MarketDataStore(path)`, duck-typed at `data.py:963-966`
  (`upsert` + `read`). Month-sliced parquet + `_meta.json` cursor sidecar.
- `kraken-deep-history` → seeds **Binance archive** OHLCV into that same root.
- Fetch→upsert→read is implemented (`data.py:912-929`) and reads deeper than
  Kraken's ~720-bar REST cap when seeded.
- **No cache in the bot itself.** Every tick is a fresh `_page_candles` +
  `store.upsert` + `store.read`, and 3 full JSONL parses. `paper_trade.py:56`
  `_FETCH_PAGES = 2` bounds the fetch but nothing bounds the re-read.

---

## Feature / observation surface (what the model actually sees today)

`_SIGNAL_COLUMNS` (`features.py:42-54`) is the complete exogenous allow-list —
11 names, 9 values + 2 freshness:

| column | source sibling | consumed? |
|---|---|---|
| `sentiment_score`, `article_count`, `novelty_flag` | ticker-news-signals | yes |
| `funding_rate`, `basis`, `open_interest` | kraken-funding-rates | yes |
| `stt_mention_count`, `stt_tilt`, `fng_index` | kraken-social-signals | yes |
| `signal_age_hours`, `signal_observed` | the seam itself | yes |

Observation width with all five groups and no signal files: **49** (matches
`PLAN.md` Leg A). With three fixtures: **60**.

**On this host, all three `*_features_file` keys are `null` in both configs
(`configs/default.yaml:57,65,73`, `configs/deep-history.example.yaml:56-58`) and
in all 6 trained model configs (`models/ETH_USD/ppo_eth_s7/config.yaml:25-27`).**
So the exogenous seam is *code-complete and test-covered but produces zero
columns in every artifact currently on disk*. The `signals` feature group
(`features.py:311`) is a no-op at runtime.

**Fields the siblings already write that the bot throws away** (verified by
diffing sibling record shapes against `_SIGNAL_COLUMNS`):

| sibling | emitted | dropped at `data.py:420-424` |
|---|---|---|
| `kraken_funding_rates/models.py:41-49` | `funding_rate`, `funding_rate_prediction`, `mark_price`, `index_price`, `basis`, `open_interest`, `bid`, `ask`, `vol24h` | **`funding_rate_prediction`, `mark_price`, `index_price`, `bid`, `ask`, `vol24h`** |
| `ticker_news_signals/models.py:73-77` | `sentiment_score`, `article_count`, `novelty_flag` | nothing |
| `kraken_social_signals/models.py` | `stt_mention_count`, `stt_tilt`, `fng_index` | nothing |

`bid`/`ask` → `spread` is prior Candidate 2 (deferred, one line). **`vol24h`,
`mark_price`, `index_price`, `funding_rate_prediction` are four more numeric,
per-(ticker, hour), already-on-disk scalars that nobody has ever proposed** —
they ride the identical allow-list, identical freshness machinery, zero new
calls. `mark_price`/`index_price` additionally give a *spot-side* basis
(the merged `basis` is futures-vs-index from the sibling, so `mark_price`
relative to the OHLCV `close` is a genuinely different number).

**Also fetched, stored, and read by no feature** (verified: zero reads in
`features.py` / `environment.py`):
- `vwap` — `data.py:58` `_OHLCV_COLUMNS`, populated at `data.py:649`,
  round-tripped by the store (`market_data/models.py:48`), exported
  (`export.py`), and **never referenced by any `_add_*_features`**.
- `count` (trade count per bar) — same path, `data.py:651`. Also never read.
- `order_book` — `engine.py:72` fetches a 10-level snapshot every 60 s and
  passes it to `tick()`; `sma.py` never reads `data["order_book"]`.

So there is a **second dead-column class**: raw OHLCV columns that arrive free
with every bar and are discarded at feature-construction time. `vwap` in
particular is a per-bar scalar that is standard in crypto microstructure
features and is *already* in the parquet store for every historical bar.

---

## Existing data sources inventory

| project | produces | cadence | where consumed | state |
|---|---|---|---|---|
| `kraken-python` | OHLCV, Ticker, OrderBook, Trades, Spread | on-demand REST, no scheduler in bot | `data.py:696` (ohlc only), `engine.py:59,65,72`, `cli.py:358,377` | no retry/backoff in `transport.py` (prior Candidate 5, sibling-repo) |
| `kraken-market-data` | month-sliced parquet store + cursor sidecar | has a `systemd` timer in its own `nix/module.nix` | `data.py:901-940` via `_resolve_store:943` | `market_data_store: null` everywhere; store root absent on this host |
| `kraken-deep-history` | seeds Binance archive OHLCV into the same root | one-shot ops | (same store) | never run on this host |
| `ticker-news-signals` | `sentiment_score`, `article_count`, `novelty_flag` | documented hourly cron (`INTEGRATION.md:114-120`) | `data.py:779,882` via `extra_features_file` | no timer in its `nix/`; key is null |
| `kraken-funding-rates` | 9 numeric futures fields incl. `bid`/`ask`/`vol24h`/`mark_price` | ~8-hourly (settlement) | `data.py:779,883` via `funding_features_file` | no timer; key null; 6 of 9 fields dropped |
| `kraken-social-signals` | `stt_mention_count`, `stt_tilt`, `fng_index` | daily (F&G) / on-pull | `data.py:779,884` via `social_features_file` | no timer; key null |

**Flake/module coverage**: `flake.nix:6-7` inputs only `kraken-python` and
`kraken-market-data`. `nix/module.nix` ships exactly one systemd unit —
`kraken-trading-bot-env` (`module.nix:220-233`), a credential env file. **No
timer, no train service, no paper-trade service, no fetch scheduler.** The bot
is only ever run by hand.

---

## Candidate gaps (ranked)

Ranked by (directness to the observation) ÷ (sourcing difficulty). Categories
are explicit. Prior-pass candidates that are still deferred are marked
**[prior]** so they are not re-proposed as new work — they appear for
completeness but the ranking prefers items no pass has raised.

---

### **Gap 1 — Six on-disk columns that the feature pipeline throws away**
**Category: IMPROVE-EXISTING / dead machinery** (highest-directness class;
explicitly the class prior `DECISION.md` §1 says must not be pre-filtered away)

**(a) Evidence**

*Allow-list truncation* — `data.py:420-424`:
```python
available_cols = [
    c for c in _SIGNAL_COLUMNS
    if c in signal_df.columns and c not in _SIGNAL_FRESHNESS_COLUMNS
]
```
`_SIGNAL_COLUMNS` (`features.py:42-54`) holds 9 value names. The funding sibling
writes 9 value fields (`kraken_funding_rates/models.py:41-49`: `funding_rate`,
`funding_rate_prediction`, `mark_price`, `index_price`, `basis`, `open_interest`,
`bid`, `ask`, `vol24h`) and serializes all of them
(`models.py:54-66`). Verified drop set: **`funding_rate_prediction`,
`mark_price`, `index_price`, `bid`, `ask`, `vol24h`** — six scalars.
`bid`/`ask` is prior Candidate 2 **[prior]**; the other four are **new**.

*`vol24h` is a particularly good fit*: it is futures 24-h turnover, already on
the same `(ticker, hour)` grid as everything else, and the OHLCV side has
`volume` only at bar granularity — a rolling-24h volume ratio is a different
quantity from `volume_zscore_20` (`features.py:399`).

*OHLCV dead columns* — `vwap` and `count` are populated on every bar
(`data.py:649,651`, in `_OHLCV_COLUMNS` at `data.py:58`, persisted by
`market_data/models.py:48-50`, emitted to CSV by `export.py`), but **grep for
`vwap` and `count` across `features.py` and `environment.py` returns nothing but
an unrelated docstring word at `features.py:444`.** The `_add_microstructure_features`
block (`features.py:405-418`) already proves the pattern is accepted: it reads
optional columns and skips silently when absent. `vwap`/`count` have no such
reader at all.

*`_add_microstructure_features` is entirely dead on the RL path* — `spread`
needs a `spread`/`bid`+`ask` column (`features.py:408-413`) and
`order_book_imbalance` needs `bid_vol`+`ask_vol` (`:414-418`). Neither is ever
placed on the frame: the merge allow-list (`data.py:420-424`) drops `bid`/`ask`,
and nothing anywhere produces `bid_vol`/`ask_vol`. Confirmed: `test_rl_environment.py:205-215`
is the only place `microstructure` is exercised, with a hand-built frame.

**(b) Feed-directness**: **Highest available.** Each is a per-(ticker, bar)
scalar — exactly the shape `_add_signals_features` (`features.py:434-436`) and
`_add_microstructure_features` already handle with a two-line loop. `vwap`,
`count`, `vol24h`, `mark_price`, `index_price`, `funding_rate_prediction` →
**6 columns**; plus `bid`/`ask` → `spread` **[prior]** = 7. They ride the
freshness machinery built in `5951f72` unchanged. No schema change, no new
merge, no new file, no new sibling.

**(c) Sourcing difficulty**: **Trivial.** Zero API calls. The bytes are already
in the JSONL on disk (funding) and in every parquet bar and CSV column (OHLCV).
Only `_SIGNAL_COLUMNS` and one new reader method need to change.

**(d) Scores**: directness **5/5**, difficulty **1/5**.

**Caveats to state honestly**: (i) because every `*_features_file` key is
`null`, this ships 6 columns that still produce 0 until a funding file is
configured — it needs Gap 5 to actually turn on; (ii) widening the observation
invalidates all 6 existing `models/` artifacts on this host (they must be
deleted, not backtested — the prior pass's standing item 4); (iii) it is
**orthogonal to** the prior Candidate 2 deferral and should ship with it or
after it, since both edit the same tuple.

---

### **Gap 2 — Nothing in the repo runs on a clock**
**Category: Reliability / operational (missing scheduler)**

**(a) Evidence**

- `nix/module.nix` defines exactly one unit: `kraken-trading-bot-env`
  (`module.nix:220-233`, `wantedBy = ["multi-user.target"]`), a credential env
  file. Grep for `timer|OnCalendar|OnUnitActiveSec|ExecStart` over `nix/` finds
  no scheduled unit. **No timer ships.**
- `flake.nix:4-7` has inputs for `kraken-python` and `kraken-market-data` only.
  `ticker-news-signals`, `kraken-funding-rates`, `kraken-social-signals` and
  `kraken-deep-history` are **not flake inputs**, so `nix build` cannot run
  their CLIs at all. Their JSONL files cannot be produced by anything the flake
  knows about.
- Sibling-side: only `kraken-market-data/nix/module.nix` has a timer. The three
  signal projects have **no `systemd` timer in their `nix/`** (verified by
  grep across all four sibling trees).
- Runtime consequence, measured in code: `paper_trade._fetch_data`
  (`paper_trade.py:289-300`) calls `read_ohlc_dataframe(..., pages=2)` **every
  60 s** (`paper_trade.py:121` `interval: int = 60`, sleep at
  `paper_trade.py:640`), and there is **no bar-close gate**: `step()`
  (`paper_trade.py:360-365`) unconditionally re-fetches, rebuilds the
  observation and re-predicts on the last bar even when that bar is unchanged.
  On hourly bars that is ~59 of 60 ticks acting on an identical observation.
  Combined with 3 full JSONL parses per tick (`data.py:779-786` looped over 3
  files), this is the "3 O(file) parses per tick + same window re-requested
  60×/hour" cost prior `RESEARCH-3.md` §F3 measured.
- And because no cron produces the signal files, `signal_observed` will be
  `0.0` on every bar of a live run even with the keys configured — the freshness
  pair from `5951f72` reports "nothing was ever reported", permanently.

**(b) Feed-directness**: **Indirect but load-bearing.** It adds no column. It is
what makes Gap 1's 6 columns and the existing 9 non-zero in production, and it
is what makes `signal_observed` a real signal instead of a constant. Without
it the entire exogenous half of the feature space is dead in production while
looking alive in tests.

**(c) Sourcing difficulty**: **Low–medium.** One `systemd.user` timer per source
(RESEARCH-1 §5 already picked the systemd timer over the sibling NixOS module,
with reasons: no `market` subcommand, DynamicUser store-root mismatch). Adding
the three signal CLIs as flake inputs is mechanical. Not a data-source
question at all.

**(d) Scores**: directness **2/5** (no new columns), difficulty **2/5**.

**[prior]** — this is prior Candidate 4 / `PLAN.md` §4.3, previously "blocked
by A2". **A2 is fixed (`5951f72`), so the stated blocker is gone** and this is
now unblocked and is the natural acceptance test for it. Ranked here because it
gates the *value* of every exogenous column, not because it is new.

---

### **Gap 3 — Two disjoint pipelines; the strategy path's data is 100 % discarded**
**Category: IMPROVE-EXISTING / dead machinery**

**(a) Evidence**

- `TradingEngine` is imported **only** by `cli.py:11` and `tests/test_engine.py`
  (grep across the repo). No `rl/` module imports it. The RL model and the
  strategy engine never share a fetch, a cache, a frame, or a column.
- `engine.py:72` calls `self.manager.order_book(pair, count=10)` every 60 s and
  assigns it to `data["order_book"]`, which is documented as a `tick()` input at
  `strategies/base.py:92`. **`sma.py` never reads it** — grep for `order_book`
  in `strategies/` returns only the `base.py:92` docstring. One REST call per
  pair per minute, thrown away.
- `engine.py:65` hardcodes `interval=60` and takes `candles[-100:]`. That window
  is *not* what the RL path sees (RL reads `_page_candles` with `pages=6`,
  `data.py:770`). Two different OHLCV horizons for the same pair in one process.
- `engine.py:59` fetches the full `Ticker` (9 fields incl. 24-h `vwap`,
  `trade_count`, `low`/`high`, 24-h volume — `kraken_api/models.py` `class
  Ticker`). `sma.py` uses **only** `ticker.decimal("ask")` and
  `ticker.decimal("bid")`, and only when `use_limit_orders`
  (`sma.py:135,156`). The other 7 fields are discarded. `Ticker` carries
  `(today, last_24h)` pairs — a 24-h vwap and 24-h volume are exactly the
  rolling-horizon quantities the RL feature set lacks and which Gap 1 notes are
  absent on the OHLCV side.
- `cli.py:377` calls `manager.tickers(args.pair)` for the `ticker` subcommand
  only.

**(b) Feed-directness**: **Medium, and it is the only route to two useful
things.** (i) `order_book` is the natural producer for `bid_vol`/`ask_vol`,
which `features.py:414-418` already reads for `order_book_imbalance` and which
**nothing in the repo produces** — this is the missing half of prior Candidate
6 **[prior]**. (ii) `Ticker`'s 24-h fields are per-pair scalars that would fit
the same optional-column pattern as Gap 1. But both require either wiring the
engine into the RL read path or building a recorder — not a one-liner.

**(c) Sourcing difficulty**: **Medium.** `order_book` and `ticker` are keyless
public REST, already called. The cost is *ops* (a recorder + storage) and the
architectural decision of whether the RL path grows an engine dependency.
Prior `DECISION.md` §6.5 rejected the book-depth recorder on directness-vs-cost;
this audit does **not** overturn that, but it does note the discriminator the
prior audit did not surface: **the machinery is already running and already
discarding the data**, so the marginal cost of *keeping* it is far below the
prior estimate.

**(d) Scores**: directness **3/5**, difficulty **3/5**.

---

### **Gap 4 — Data quality: no cache, no replay, no recorded window**
**Category: Data quality / reliability** (mostly **[prior]** — RESEARCH-1/2/3,
fully specced; carried for completeness with the new measurements)

**(a) Evidence (the parts that are new this pass)**

- **No cache, no backfill**: `_page_candles` (`data.py:662-703`) is called on
  every read, live leg at `data.py:770` and store leg at `data.py:912`. Nothing
  memoizes the parsed signal frames, so a 60 s tick does 3 full `json.loads`
  passes over 3 files (`data.py:368-376` inside the 3-call loop at
  `data.py:779-786`).
- **`until` is still silently dropped on the live path** — **re-confirmed this
  pass.** `read_ohlc_dataframe` accepts `until` (`data.py:797`) and passes it
  only to `store.read` (`data.py:929`). The null-store branch
  (`data.py:875-887`) forwards `since` but **not `until`** to
  `fetch_ohlc_dataframe`, whose signature (`data.py:706-717`) has no `until`
  parameter at all. So on the default config `until` is accepted and ignored.
- **No caller ever passes `since`/`until`** — grep for `since=`/`until=` across
  `train.py`, `backtest.py`, `export.py`, `paper_trade.py` returns **nothing**
  outside docstrings. Only `data.py` itself and the store test
  (`test_rl_data_store.py:173`) exercise them. A deep seeded store
  (`configs/deep-history.example.yaml`) is therefore read *whole* by every
  consumer, and `paper_trade.py` deliberately takes no `since` (locked by
  `test_rl_signal_config_wiring.py:523`) — correct for the live tail, but
  nothing bounds the other three.
- **`TradingEnvironment.reset(options=...)` is documented as unused**
  (`data.py:869-873`, `environment.py:221`) — the walk-forward/train-eval split
  mechanism exists and is dead.
- **No provenance**: `registry.ModelRecord.config_summary`
  (`registry.py:69-78`) records `feature_windows`/`feature_groups` but **not the
  feature count the npz was fitted on**, and `train.py:238-242` saves
  `normalization.npz` + `config.yaml` without `n_features`. The only guard is
  `paper_trade._validate_observation` (`paper_trade.py:327-340`) — which
  RESEARCH-2 §G2 showed is **tautological** for the stale-width threat (both
  sides come from the same `normalize` drop).
- **NEW measurement this pass**: `feature_names` IS already persisted
  (`features.py:94,117`) and round-tripped, so the width guard is an assertion,
  not a format change. And **`models/` is no longer empty** — 6 trained
  artifacts exist on disk (2 seeds × ETH/SOL/XRP), all null-signal. The prior
  pass recorded "no retrain burden today"; that is now stale in the sense that
  there *are* artifacts, so a real guard matters.
- **[prior]** retry/backoff: `transport.py` raises immediately on
  `RateLimitError`; Kraken rate-limits in the *body* with HTTP 200, so
  `urllib3.Retry(429)` never fires. Sibling-repo boundary.
- **[prior]** `min_interval=` is a silent no-op at `kraken_api/auth.py:73`.

**(b) Feed-directness**: **Indirect.** No columns. It governs whether any
measured feature value is trustworthy and whether two runs are comparable.

**(c) Sourcing difficulty**: **Low.** No new dependency; stdlib + the pandas
already required (`flake.nix:58-64`). Fully specced in RESEARCH-1 (window
block + provenance) and RESEARCH-2 (width guard).

**(d) Scores**: directness **2/5**, difficulty **2/5**.

---

### **Gap 5 — Every `*_features_file` key is null: the seam is on, the signal is off**
**Category: IMPROVE-EXISTING / activation** **[prior]** (`PLAN.md` §5)

**(a) Evidence** — `configs/default.yaml:57,65,73` (`extra_features_file`,
`funding_features_file`, `social_features_file`) all `null`;
`configs/deep-history.example.yaml:56-58` likewise; and all 6 model configs on
disk (`models/ETH_USD/ppo_eth_s7/config.yaml:25-27`). `_SIGNAL_COLUMNS`
(`features.py:42-54`) declares 11 exogenous names; **0 reach the observation in
any existing artifact.** `read_ohlc_dataframe` is passed `None` for all three at
every call site (`train.py:188-192`, `backtest.py:138-145`,
`export.py:166-174`, `paper_trade.py:294-296`), and `merge_extra_features`
returns `df` unchanged on a falsy path (`data.py:356-357`).

This is *ops* (fetch one ticker per sibling CLI into a scratch dir, point the
keys at it), not code — but it is the precondition that makes Gap 1's six new
columns non-zero, and `PLAN.md` §5 item 2 records the coupled decision that is
still unmade: `signal_max_age_hours` is **one global bound for all three files**
(`data.py:442`, applied per file at `data.py:455`), so a single value must serve
an hourly news pull and an 8-hourly funding settle. `PLAN.md` §4.8 / VALIDATION §4
measured that at the `null` default an 8-hourly funding source contributes a
live reading to only **76/721 bars**.

**Scores**: directness **4/5** (it is what turns 9 existing + 6 new columns on),
difficulty **1/5** but gated on Gap 2 for production cadence.

---

## Category coverage check

The ranked list spans **five** categories, exceeding the multi-category rule:

| # | Gap | Category | directness | difficulty |
|---|---|---|---|---|
| 1 | Six on-disk columns discarded (4 new + `vwap`/`count`; `bid`/`ask` prior) | IMPROVE-EXISTING / dead machinery | 5/5 | 1/5 |
| 2 | No scheduler anywhere; flake cannot run 3 of 5 signal CLIs | Reliability / operational | 2/5 | 2/5 |
| 3 | Two disjoint pipelines; `order_book` + 7 of 9 `Ticker` fields discarded | IMPROVE-EXISTING / dead machinery | 3/5 | 3/5 |
| 4 | No cache, no replay, `until` dropped, no width guard | Data quality / reliability | 2/5 | 2/5 |
| 5 | All signal keys null — seam on, signal off | IMPROVE-EXISTING / activation | 4/5 | 1/5 |

**Categories deliberately NOT ranked, with reasons carried forward from
`DECISION.md` §6.6-6.8** (this audit found no evidence that changes them):

- **On-chain** (exchange flows, whale activity, stablecoin supply): the funding
  sibling already consumes Kraken Futures `/tickers` `openInterest`
  (`kraken_funding_rates/models.py:112`) — the one crypto-native field that is
  cheap. Genuine on-chain metrics are paid/API-keyed and need an asset→pair
  mapping layer that does not exist. Directness low, cost high.
- **Macro calendar** (CPI, rate decisions): moves this on a weekly/daily
  horizon; the observation is hourly (`configs/default.yaml:11`
  `ohlcv_interval_minutes: 60`). Directness low.
- **Research/academic signal**: no route from a paper to a per-(ticker, hour)
  numeric column. Directness very low.
- **New text/news source**: a *fourth* text source would need its own
  sentiment pipeline and adds no new column shape — strictly worse than the
  three already wired. Rejected on the evidence.

---

## Deferred-from-prior-passes summary

Carried from `PLAN.md` §4 and `DECISION.md` §7, with **current** status:

| Prior item | Status as of `ca360a8` |
|---|---|
| Candidate 2 — activate `spread` from `bid`/`ask` | **Still deferred, now safe** (A3 fixed). One line in `_SIGNAL_COLUMNS`. This audit finds **four more fields** alongside it (Gap 1). |
| Candidate 3 — record the training window + `until` fix | **Still open.** `until` re-confirmed dropped on the live path (`data.py:875-887`); no caller passes `since`/`until`. RESEARCH-1 fully specced. |
| Candidate 4 — schedulers + flake | **Still open, now unblocked.** A2 is fixed so the hourly cron no longer crashes. Gap 2 above. |
| Candidate 5 — fetch reliability (retry/backoff + cache) | **Still open, sibling-repo boundary.** RESEARCH-3's measured answer stands. |
| Candidate 6 — order-book depth + trade-tape recorder | **Still open.** Gap 3 reframes the cost: the `order_book` fetch already runs and is already discarded. |
| A4 source-side in `kraken-social-signals` | Still open, harmless from the bot side (`signal_observed` disambiguates). |
| Run the `kraken-deep-history seed` | **Still un-run.** Store root absent; `market_data_store: null` in both configs and all 6 model configs. |
| Widen the `signal_max_age_hours` doc | Still open (one comment line). Now compounded by Gap 5's unmade per-source-bound decision. |
| A5 — defaults drift (`_FEATURE_GROUPS` ×5, `pages=6` ×5, `[1,4,24]` ×4) | **Re-confirmed.** `_FEATURE_GROUPS` at `features.py:25` and the `_DEFAULT_FEATURE_GROUPS` list at `export.py:73-79` are two spellings; `pages: int = 6` appears at `train.py:133`, `backtest.py:91`, `data.py:709`, `data.py:794`, `export.py:128` (`paper_trade` uses `_FETCH_PAGES = 2` at `paper_trade.py:56`, a *different* magic number for the same knob). Cheap, orthogonal. |
| Width guard / `n_features` provenance | Still open. `feature_names` already in the npz (`features.py:94,117`) so this is an assertion. **New pressure: `models/` now holds 6 trained artifacts** (was empty last pass). |
| Real convergence A/B | Still open. 721 bars / 1 seed is not an A/B. |
| Ticker resolution (`--ticker USD_SOL` fails) | Still open. |
| Re-fit pre-`5951f72` models | **Superseded.** All 6 existing models are null-signal, so none predates the seam repair with signal columns. But any future widening (Gap 1) invalidates them. |

---

## What this audit adds beyond the prior passes

1. **Four new discarded funding fields** (`vol24h`, `mark_price`, `index_price`,
   `funding_rate_prediction`) on top of prior Candidate 2's `bid`/`ask` — and a
   second dead-column class the prior passes never named: **`vwap` and `count`
   arrive on every bar and are read by no feature**.
2. **The two pipelines are disjoint.** No prior artifact notes that
   `TradingEngine` is unreachable from `rl/` — so the engine's per-minute
   `order_book` + 9-field `Ticker` is a standing, already-paid-for, already-discarded
   data source, and the natural producer for `bid_vol`/`ask_vol`.
3. **The missing feature width is 7, not 2**, all of it per-(ticker, bar) scalars.
4. **`models/` is no longer empty** — 6 artifacts, which changes the standing
   "no retrain burden" note into a live guard requirement.
5. **A5 re-confirmed with a new instance**: `paper_trade`'s `_FETCH_PAGES = 2`
   is a sixth, differently-valued spelling of the `pages` knob.

AUDIT COMPLETE