# AUDIT.md — data-pipeline audit (2026-10-08)

Team `audit-pipeline-1008`. Assembled by the lead from two read-only audits:
`AUDIT-PIPELINE.md` (RL path) and `AUDIT-SOURCES.md` (sourcing/ops layer).
Evidence tags: `[C]` read in code this pass, `[M]` measured on this host,
`[S]` stated in a prior artifact and not re-measured. Line numbers are HEAD
`1c63007`. **No gap is committed to here** — the architect chooses in Phase 3.

> **Headline.** The 2026-10-03 audit's structural findings have largely been
> *built* since: the OHLC store is seeded (106 months), all three signal channels
> are populated, an order-book recorder is running, and four hourly producers
> have timers. The remaining high-value gaps are no longer "nothing fetches
> this" — they are **fetched-but-not-consumed** and **consumed-but-degenerate**:
> the recorded order-book depth has **no RL reader at all**, and the
> `microstructure` feature group's only reachable column is empty for ~99% of
> funding history.

---

## 1. Pipeline map (end to end)

### 1.1 The RL path

```
config YAML ──> build_train_config                         train.py:120
    ├─ resolve_data_window(cfg) -> DataWindow{since,until,eval_split}   train.py:232 / data_window.py:104
    │     (shipped default: since=until=null -> unpinned, eval_split INERT)
    ├─ refresh_for_window(...)  data.py:1495  (fetch-skip when pinned+store-covered)
    │
    ├─ read_ohlc_dataframe(...)   ← the ONE read seam            data.py:1625
    │     ├─ market_data_store is None (shipped default)          data.py:1769
    │     │     └─> fetch_ohlc_dataframe -> _page_candles          data.py:1164 / :1120
    │     │           └─> manager.ohlc(pair,interval,since)  Kraken /0/public/OHLC (~720-bar ceiling)
    │     │           └─> add_derived_ohlcv_features              data.py:1017
    │     │                 +vwap_dev, +trade_count_zscore_20, +volume_per_trade
    │     └─ store configured (deep-history.example.yaml)         data.py:1803
    │           ├─ refresh=True  -> _page_candles + store.upsert   data.py:1858-1880
    │           ├─ refresh=False -> ZERO API calls (pinned window) data.py:1881
    │           ├─ store.read(pair, interval, since=None, until=None)  data.py:1890
    │           └─ add_derived_ohlcv_features()
    │
    ├─ three exogenous JSONL seams, merge order news->funding->social  data.py:1908
    │     merge_extra_features()  data.py:633
    │       (ticker filter, hour-floor dedupe, bounded ffill,
    │        +signal_observed +signal_age_hours; absence != zero)
    │
    ├─ training_frame(df, window)  (clip + eval_split; no-op if unpinned)  train.py:265
    ├─ prepare_episode(df, features, episode_bars)  data.py:2013
    │     slice trailing episode_bars FIRST, then FeaturePipeline.fit()  data.py:2058-2067
    │
    └─ TradingEnvironment(...)  train.py:319 / environment.py:184
          self._features   = pipeline.compute(data)              environment.py:247
          self._feature_matrix = _raw_feature_array()            environment.py:617
                compute -> ffill -> fillna(0) -> NormalizationStats.normalize -> float32
          observation_space = Box(shape = feature_matrix.shape[1])  environment.py:256

artifacts: models/{TICKER_ID}/{model_name}/{model.zip, normalization.npz, config.yaml}  registry.py:1-9
```

### 1.2 What is fetched, from where, at what cadence

| input | source | storage | cadence | reaches observation? |
|---|---|---|---|---|
| OHLCV (o/h/l/c/vwap/volume/count) | Kraken `/0/public/OHLC` (live) or seeded store | in-memory DF; store = month-sliced parquet | per read | yes (price/technical/volume groups) |
| vwap_dev, trade_count_zscore_20, volume_per_trade | derived at read seam from `vwap`/`count` | in-memory | per read | yes (`signals` group) |
| news: sentiment_score, article_count, novelty_flag | sibling `ticker-news-signals` | `signals/eth_usd_news.jsonl` | hourly timer `:23` | yes |
| funding: funding_rate, basis, open_interest, funding_rate_prediction, vol24h, bid, ask | sibling `kraken-funding-rates` (+ backfill) | `signals/eth_usd_funding.jsonl` | hourly timer `:17` | funding_rate yes; basis/OI/pred/vol24h/bid/ask only on 86/8914 live records |
| social: stt_mention_count, stt_tilt, fng_index | sibling `kraken-social-signals` | `signals/eth_usd_social.jsonl` | hourly timer `:29` | yes, but stt_tilt/fng_index degenerate (G-4) |
| order-book depth (bids/asks/best_bid/best_ask/mid/spread) | Kraken `/0/public/Depth`, keyless | `signals/eth_usd_orderbook.jsonl` (append-only) | hourly timer `:41` | **NO — no RL reader** (G-1) |
| store hole report | `tools/store_gap_scan.py` / `store_guard.py` | stdout | manual / `store-verify` | no (operational) |

Measured artifact state on this host `[M]`:

| artifact | state |
|---|---|
| `~/Projects/kraken-market-data/store/ETH_USD/60/` | **present**, 4.7 MB, **106 month files, 2018-01 .. 2026-10** |
| `signals/eth_usd_funding.jsonl` | 8,914 records, 2025-10-01T08:00Z .. 2026-10-07T23:00Z |
| `signals/eth_usd_news.jsonl` | 166 records (~7 d) |
| `signals/eth_usd_social.jsonl` | 337 records |
| `signals/eth_usd_orderbook.jsonl` | 103 records, 2026-10-03T19:37Z .. 2026-10-07T23:41Z; status `ok:false`, 1 hole |
| `models/` | **empty** (only `.gitkeep`) |

The store is **not** what `configs/default.yaml` reads — the shipped default still
sets `market_data_store: null` (`configs/default.yaml:288`); the store is reached
only through `configs/deep-history.example.yaml:127` or
`configs/deep-ab.base.yaml:62`.

### 1.3 Where each input enters the feature space

- `FeaturePipeline.compute` (`features.py:783`) dispatches five groups
  (`features.py:853-862`); `_FEATURE_GROUPS` at `features.py:25`.
- `_SIGNAL_COLUMNS` (`features.py:94-125`) is the single canonical allow-list
  shared by the merge seam (`data.py:786-790`) and the observation passthrough
  (`features.py:1063-1069`). Width is coupled to *which columns are present in
  the producer file*, not to their values.
- `feature_names` in `normalization.npz` is the width authority;
  `check_feature_width` (`features.py:483`) is non-self-referential.
- `TradingEnvironment._raw_feature_array` (`environment.py:617`) is the one
  place the z-scored observation is materialised; `paper_trade._build_observation`
  (`paper_trade.py:330-359`) re-derives the same row live.

### 1.4 Config keys and their readers

Shipped `configs/default.yaml` is fully live except `model_name` (G-16).
Drivers per input: `ticker`, `ohlcv_interval_minutes`, `feature_windows`,
`feature_groups`, the three `*_features_file`, `signal_max_age_hours`,
`signal_require_ticker`, `market_data_store`, `market_data_store_venue`,
`data_window.{since,until,eval_split}`.

### 1.5 Sourcing / ops layer

Five hourly `systemd.user` producers + one weekly checkpoint, all
`Persistent=true`, staggered so HTTP requests do not contend:

| minute | producer | source | sink |
|---|---|---|---|
| `:17` | funding | Kraken Futures public REST (keyless) | `signals/eth_usd_funding.jsonl` |
| `:23` | news | Google News RSS via GNews + VADER (keyless) | `signals/eth_usd_news.jsonl` |
| `:29` | social | StockTwits v2 + alternative.me F&G (keyless) | `signals/eth_usd_social.jsonl` |
| `:41` | order-book | Kraken `/0/public/Depth` (keyless) | `signals/eth_usd_orderbook.jsonl` |
| `:47` | signal-gaps gate | local files only | exit 1 on gap |
| Mon 04:23 | depth checkpoint | git push of depth log | off-machine copy |

Every producer is **forward-only** — one reading per fire, appended; nothing
backfills except funding (`just funding-backfill`, Kraken
`/historical-funding-rates`, ~366 d). **No market-data timer exists in this
repo's `systemd/`** — the store only advances on a read's upsert leg or a
manual seed.

Rate limiting: `kraken-python` transport is a **throttle only** — `min_interval`
default `0.0` (`kraken_api/transport.py:108`), one `time.sleep` at `:186`;
`_request` re-raises on `RequestException`/`RateLimitError` (`:250`). `grep
retry|backoff` over `kraken_api/` → **zero hits**. The bot consumer fetch
`_page_candles` (`data.py:1154`) has **no try/except**.

Sibling wiring: `kraken-python` and `kraken-market-data` are **flake inputs**;
`kraken-deep-history`, `kraken-funding-rates`, `ticker-news-signals`,
`kraken-social-signals` are invoked via `nix run <checked-out path>#<pkg>`
(deliberately — private repos, avoid freezing fixes into `flake.lock`).

---

## 2. Ranked gaps (category-spanning)

Ranked by **directness to the RL observation / `tick()`** then feasibility.
Categories are mixed on purpose. Nothing here is endorsed.

### G-1 — The order-book depth recorder has **no RL reader** (market microstructure / improve-existing) — 🥇 highest directness

- **Evidence.** `features.py:1018-1026` is the *only* place
  `order_book_imbalance` is computed, gated on
  `{"bid_vol","ask_vol"}.issubset(df.columns)`. Neither `bid_vol`/`ask_vol` nor
  `order_book_imbalance` is in `_SIGNAL_COLUMNS` (`features.py:94-125`), so no
  merge can supply them; `data.py:786-790`'s presence-intersection would drop
  them anyway. `depth_recorder.py:45-51` states it outright: *"writes data and
  reads none of it back … no `_SIGNAL_COLUMNS` entry, no observation-width
  change."* The only consumers of the depth file are the recorder's own gap
  scanner (`cli.py:838`, `depth_recorder.py:558`) — operational, not features.
  `engine.py:72` still fetches a book that no strategy reads.
- **Measured.** `signals/eth_usd_orderbook.jsonl` holds **103 snapshots** `[M]`;
  each carries `bids`/`asks`/`best_bid`/`best_ask`/`mid`/`spread`/`depth`.
- **Why it is #1.** `feature_groups` ships with `"microstructure"` **enabled**
  (`configs/default.yaml:53`), so the pipeline reserves width for a
  microstructure reading it can never populate from depth. The data is already
  on disk and the feature column is already coded — only the merge seam and one
  `_SIGNAL_COLUMNS` entry are missing.
- **Directness:** highest — a new observation column feeding
  `_raw_feature_array` directly, with no new fetch. **Effort:** medium — the
  JSONL is a nested `bids`/`asks` array, not flat per-hour columns; its record
  carries `spread` which **collides** with the funding-derived `spread`
  (`features.py:1008-1017`); `depth_recorder.py` already reserves
  `realized_spread_bps` as the non-colliding name.

### G-2 — Trade tape + realized spread are wrapped but never called (market microstructure / new-source-or-improve)

- **Evidence.** `kraken_api/manager.py:192 recent_trades`, `:200 spread`, and
  the whole WS surface (`:365 ws_token`, `:369 public_ws_url`; `SpotWebSocket`)
  have **zero call sites** anywhere in the bot (repo-wide grep → none). The only
  recorded microstructure is the depth snapshot, and `_SIGNAL_COLUMNS` has no
  trade-flow / realized-spread column. `features.py` even reserves the name:
  *"A future tick-level tape recorder must use `realized_spread_bps`"*.
- **Feeds RL/tick:** direct — a per-bar signed-volume / order-flow-imbalance and
  a realized-spread scalar are exactly the shape the observation already takes.
- **Sourcing:** keyless (`/0/public/Trades`, `/0/public/Spread`; WS public
  channels keyless). Forward-only for history (same physics as depth).
- **Effort:** medium — a new recorder + a seam column.

### G-3 — The `microstructure` group is effectively empty: `spread` absent on 8,828 of 8,914 funding records (data quality / improve-existing)

- **Evidence.** `[M]` on `signals/eth_usd_funding.jsonl`: `basis`,
  `open_interest`, `funding_rate_prediction`, `vol24h`, `bid`, `ask` are each
  **null on 8,828 / 8,914 records** (only the 86 live-appended records carry
  them). `configs/default.yaml:181-183` says so: the keyless backfill recovers
  **1 of 6** columns (`funding_rate`). `features.py:1008-1017` derives `spread`
  from `bid`/`ask`; with both zero-filled, `(0-0)/0` -> NaN -> observation
  0-fill. So the group enabled by default contributes **one column that is 0
  for ~99% of history**.
- **Directness:** high — a default-on group whose only reachable column is
  degenerate. **Effort:** small — either source a real bid/ask/basis series, or
  drop `spread` from the default feature set and consume the recorded
  order-book spread instead (ties to G-1).

### G-4 — Two of the nine signal columns are inert by construction (data quality / improve-existing)

- **Evidence.** `configs/default.yaml:139-143`: `stt_tilt` is *"permanently 0.0
  on the live StockTwits v2 feed"* and `fng_index` is a *daily* value stamped
  onto each hour. A zero-variance column normalises to exactly 0.0, so both
  reach the observation as constant-zero dead weight (`_SIGNAL_COLUMNS` entries
  `stt_tilt`, `fng_index`, `features.py:102-103`).
- **Directness:** medium-high — two observation columns that can never carry
  information. **Effort:** small (drop them from `_SIGNAL_COLUMNS` / replace the
  `stt_tilt` source), but it changes observation width -> invalidates existing
  artifacts (none exist yet: `models/` is empty).

### G-5 — `signal_observed` overclaims: per-channel OR, not per-column (data quality / improve-existing)

- **Evidence.** `configs/default.yaml:184-191` documents it: `observed =
  filled.notna().any(axis=1)`, so once the funding backfill covers a window,
  `signal_observed == 1.0` on every bar *while 5 of 6 funding columns are 100%
  zero-fill with no reading behind them*. `data.py:833-849` is the merge that
  writes it. The config itself warns *"Do NOT gate any feature on
  `signal_observed`"* — but the column is still handed to the agent as if it
  meant "this bar has data".
- **Directness:** medium — an observation column that misinforms. **Effort:**
  medium (make it per-column, or drop it and keep `signal_age_hours`).

### G-6 — Cross-exchange spot basis is never materialised (cross-exchange / crypto-native / new-source)

- **Evidence.** The store can hold a Binance-USDT seed *or* a Kraken live leg
  but never both at once (`data.py:85 SEEDED_STORE_VENUE` vs `LIVE_STORE_VENUE`),
  and no basis/spread column exists between them. `kraken-deep-history` already
  proves Binance archive access is keyless and cheap.
- **Feeds RL/tick:** direct — a per-bar venue basis is a scalar the seam
  accepts. **Sourcing:** keyless (Binance archive already used; or a second
  live REST leg). **Effort:** medium — new code + a second live leg.

### G-7 — Macro / economic calendar: zero presence (macro / new-source)

- **Evidence.** No macro/calendar reference in any `.py` or config (grep).
- **Feeds RL/tick:** per-event level/binary scalar per bar, but needs a new seam
  column + producer. **Sourcing:** FRED (free key), most economic calendars
  paid or scraped. **Effort:** medium.

### G-8 — On-chain / crypto-native: zero presence (on-chain / new-source)

- **Evidence.** No on-chain reference in the tree (grep).
- **Feeds RL/tick:** per-bar scalar (exchange netflow, active addresses, gas),
  new producer + seam required. **Sourcing:** mostly free-tier API keys
  (Etherscan free, Dune/Glassnode paid). **Effort:** medium.

### G-9 — Silent-drop failure mode: features computed on **bar counts, not wall-clock** (data quality / improve-existing)

- **Evidence.** `configs/default.yaml:274-287`: measured **158 missing bars
  across 28 gaps, largest 39h**, including a 38-bar hole at the seed/live-append
  seam; *"across that hole `return_1` reports a 39-hour return as though it were
  1-hour."* `store_gap_scan.py` / `store_guard.py` detect and label it, but
  nothing fixes it. With the store now seeded and deep, this path is live.
- **Directness:** high data-quality (corrupts `return_*`/`range_1` directly),
  but **detected not fixed**. **Effort:** large — time-aware feature windows
  over a reindexed bar grid.

### G-10 — Unretried, unthrottled fetch path (reliability / improve-existing)

- **Evidence.** `_page_candles` (`data.py:1145-1155`) has no `try/except`; a
  raised `RequestException`/`RateLimitError` aborts the whole read. The store leg
  then logs *"fetch returned no candles … reading whatever the store already
  has"* (`data.py:1874-1880`) — a silent shorten on `refresh=True`. Transport
  `min_interval` defaults to `0.0` and there is no retry/backoff `[S]`; the
  sibling `kraken-funding-rates` retries 3x — this repo does not.
- **Directness:** medium — a network blip silently shortens the training frame.
  **Effort:** small.

### Lower-priority / operational

- **G-11** — Coverage gate misses the depth channel; news/social holes are
  permanent (the gate checks only funding/news/social; order-book `.status.json`
  is already `ok:false`). Effort small.
- **G-12** — No scheduled refresh of the market-data store (no unit in
  `systemd/`). Effort small.
- **G-13** — Depth is thin and coarse: `count=100`, 1 snapshot/hour, 11 days
  deep (`depth_fraction 0.011`). Effort small (cadence/`count` trade disk for
  resolution).
- **G-14** — `data_window.eval_split` is inert on the shipped default; the
  default run has no holdout (`data_window.py:25-35,127-137`). Effort small.
- **G-15** — `TradingEnvironment.reset(options=...)` is unused -> no
  walk-forward; one episode = the whole frame. Effort medium.
- **G-16** — Dead config key `model_name` (`configs/default.yaml:384`; overwritten
  `train.py:144`). Effort trivial.
- **G-17** — A second, divergent feature path `_builtin_features`
  (`environment.py:645-673`), test-only. Effort small.
- **G-18** — Doc/config drift (`export.py:20-22`, `data.py:1762-1767`). Effort
  trivial.
- **G-19** — No real-time/WS path; paper leg re-fetches 2 pages/min
  (`paper_trade.py:65`). Effort medium.
- **G-20** — Research / academic signal: zero presence, hardest to
  operationalise. Effort large.

---

## 3. Already built — do NOT re-propose

- `ticker-news-signals` — news → VADER sentiment (Google News RSS), hourly
  timer, seam live.
- `kraken-market-data` — OHLC parquet store + `since`-cursor poller; flake
  input; read/write via `data.py` store seam.
- `kraken-funding-rates` — perp funding / basis / OI / vol24h / bid-ask; hourly
  timer + `just funding-backfill`.
- `kraken-social-signals` — StockTwits mentions/tilt + alternative.me Fear &
  Greed; hourly timer, seam live.
- `kraken-deep-history` — deep OHLCV seeder (Binance archive → store), via
  `just store-seed`.
- Order-book depth recorder (`record-depth`, `depth_recorder.py`) + weekly
  off-machine checkpoint — built and firing.
- Signal-coverage gate `tools/signal_gap_scan.py` + `signal-gaps.timer`.

## 4. Method caveat

Neither auditor ran `nix develop` (read-only budget), so no observation-width
or `return_1` figure is re-measured here; widths and the store's gap arithmetic
are cited `[S]`/`[C]`. All file-level measurements (`[M]`) are shell-only (line
counts, JSON keys, null counts, parquet month listing).
