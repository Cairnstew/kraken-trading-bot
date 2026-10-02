# Data-pipeline audit — `kraken-trading-bot`

**Pass:** Phase 1 (read-only audit; the only file written is this one).
**Checkout:** `/home/seanc/Projects/kraken-trading-bot`, branch state at `4fd3b77`.
**Scope:** every place a market or exogenous datum is fetched, stored, and admitted to the
RL observation or a strategy `tick()`.

> **Supersedes** the previous `.data-audit/AUDIT.md` (also committed). That file's line
> numbers were pinned to `rl/features.py` **before** the non-finite-guard pass moved the
> microstructure builder (it cited `features.py:586-590`; the same code is now at
> `features.py:1007-1015`). Every `file:line` below was re-derived against the current tree.

---

## 0. Method note — what "verified" means here

Nothing below is inferred from the README. Each claim is a literal line in the tree
(`grep`/`read` output at the time of writing) or a sibling-repo file read directly. Claims I
could not evidence are not in this document.

---

## 1. End-to-end pipeline map

### 1.1 The RL path (train / backtest / paper)

| # | Source | Resource called | Cadence | Storage / cache | Enters observation at |
|---|--------|-----------------|---------|-----------------|------------------------|
| 1 | Kraken REST | `OHLC` via `KrakenManager.ohlc(pair, interval, since)` — paged in a hand-written loop, `pages` (default 6) | **Per invocation** of `train`/`backtest`/`paper` — no timer, no cache check | Live: none (discarded after the frame). With store: `store.upsert()` at `data.py:1366` | `data.py:1179` (fetch) / `data.py:1364` (fetch leg) → `candles_to_dataframe` (`data.py:1032`) → `add_derived_ohlcv_features` (`data.py:968`) → `FeaturePipeline.compute` (`features.py:772`) |
| 2 | Kraken REST (via `kraken-python`) | `/0/public/AssetPairs`, `/0/public/Assets` | once, cached in the sibling's `catalog.py` | sibling-internal | pair normalisation only |
| 3 | Local `kraken-market-data` store | `store.read(pair, interval, since, until)` | per invocation, **only when `market_data_store` is non-null** (`data.py:1304`) | itself (parquet months `{PAIR_ID}/{interval_min}/{YYYY-MM}.parquet`, `data.py:1404-1405`) | same as #1 |
| 4 | `kraken-deep-history` (Binance public archive) | monthly klines ZIPs | **one-shot manual**: `just store-plan` / `just store-seed` (`justfile:210`, `justfile:218`) | writes into #3's root | feeds #3 |
| 5 | `kraken-funding-rates` | `PF_*` public futures ticker | **hourly, timer-enforced**: `just funding-timer` → `systemd/kraken-trading-bot-funding.timer` (`OnCalendar=*-*-* *:17:00`, `Persistent=true`) | append-only JSONL `signals/eth_usd_funding.jsonl` (`justfile:158` `--append`) | `merge_extra_features` (`data.py:591`) → `_SIGNAL_COLUMNS` allow-list (`features.py:94-125`) → `_add_signals_features` (`features.py:1017`) and `_add_microstructure_features` (`features.py:986`) |
| 6 | `ticker-news-signals` | news → per-ticker sentiment | **manual only** — no timer, no `just` recipe for it | JSONL, user-managed | same merge seam |
| 7 | `kraken-social-signals` | StockTwits + Fear & Greed | **manual only** | JSONL, user-managed | same merge seam |
| 8 | Kraken Futures | open interest / basis | inside #5's pull | same JSONL | `features.py:99-100, 114-115` |

### 1.2 The engine / strategy path (a *separate, disjoint* program)

| # | Source | Resource | Cadence | Storage | Enters `tick()` at |
|---|--------|----------|---------|---------|---------------------|
| 9 | Kraken REST | `manager.ticker(pair)` — `engine.py:59` | every `engine.interval` seconds (default 60, `engine.py:36`, loop `engine.py:166-168`) | none | `sma.py:69` `ticker = data.get("ticker")` |
| 10 | Kraken REST | `manager.ohlc(pair, interval=60)` — `engine.py:65`, truncated `candles[-100:]` at `engine.py:66` | same | none | `sma.py:68` `candles = data.get("candles", [])` |
| 11 | Kraken REST | `manager.order_book(pair, count=10)` — `engine.py:72` | same | none | **nowhere.** Documented as a `tick()` key at `strategies/base.py:92`; the only `tick()` in the repo reads only `candles` and `ticker` |

`engine.py` and `strategies/` are **not imported by `rl/`** — grep for `TradingEngine`
across `kraken_trading_bot/rl/` returns nothing. The two halves of the repo share no data.

### 1.3 Feature-group → column budget

`features.py:25` — `_FEATURE_GROUPS = ("price", "technical", "volume", "microstructure", "signals")`,
and `configs/default.yaml:48` enables **all five**. What each can actually emit:

| Group | Columns it can emit | Producers that exist today |
|---|---|---|
| `price` | `return_*`, `log_return_*`, `range_1`, `price_ratio_sma_*` | OHLCV (#1) |
| `technical` | `sma/ema/rsi/macd/bb/atr_*` | OHLCV (#1) |
| `volume` | `volume_change_1`, `volume_zscore_20`, `obv*` | OHLCV (#1) |
| `microstructure` | `spread`; **or** `order_book_imbalance` | `spread` only — needs `bid`/`ask` from #5. **Nothing anywhere emits `bid_vol`/`ask_vol`.** |
| `signals` | the 15 columns of `features.py:94-125` less the builder inputs | #5 only (#6/#7 are `null` in the shipped config) |

---

## 2. Ranked candidate gaps

Ranked by (directness to a live observation column) × (evidence strength) ÷ (build cost).
Categories are tagged so the architect can see the spread. **Seven candidates, five
distinct categories** — the list is deliberately not single-category.

---

### 🥇 G1 — The `microstructure` group is enabled but has **no producer**: `order_book_imbalance` is unreachable, and the order book is already being fetched and thrown away
**Category: improve-what-exists (dead / ceremonial machinery, effective-but-unapplied)**

**Evidence.**

`features.py:1007-1015` — the imbalance branch is guarded on column names no one emits:
```python
1007:        if {"bid_vol", "ask_vol"}.issubset(df.columns):
1008:            bid_vol = df["bid_vol"].astype(float)
1009:            ask_vol = df["ask_vol"].astype(float)
1012:            out["order_book_imbalance"] = (
1013:                bid_vol.replace(_NON_FINITE_INPUTS, np.nan)
1014:                - ask_vol.replace(_NON_FINITE_INPUTS, np.nan)
1015:            ) / denom
```

Repo-wide grep for `bid_vol` / `ask_vol` / `order_book_imbalance` outside `.venv/` hits
**only** `features.py:1007-1015` and four test files that fabricate the columns by hand
(`tests/test_rl_environment.py:222-223`, `tests/test_feature_nonfinite_guards.py:306-307`,
`:398-399`, `:635-636`). There is no producer in this repo and none in any of the six
sibling repos.

Worse, the merge seam **structurally cannot carry them**: `data.py:765-769` reduces every
signal file to the canonical allow-list,
```python
765:    available_cols = [
766:        c
767:        for c in _SIGNAL_COLUMNS
768:        if c in signal_df.columns and c not in _SIGNAL_FRESHNESS_COLUMNS
769:    ]
```
and `features.py:94-125` `_SIGNAL_COLUMNS` contains `bid`, `ask`, `spread` — but **not**
`bid_vol` / `ask_vol`. A sibling that emitted them through today's seam would have them
silently dropped at `data.py:765`.

Meanwhile the data is **already on the wire and discarded**: `engine.py:72`
`data["order_book"] = self.manager.order_book(pair, count=10)` runs every 60 s, and
`strategies/base.py:92` advertises `"order_book"` as a `tick()` input — but `sma.py:68-69`
reads only `candles` and `ticker`. Net: an L2 book is fetched, handed to the strategy
interface, and never consumed by anything.

**Directness — maximum.** The consumer is finished, guarded, tested, and keyed on two
column names. Nothing on the RL side needs writing; the `microstructure` group goes from
a maximum of **1** possible column (`spread`) to 2, and gains a genuine order-book-depth
reading that no rolling-window indicator can express (contemporaneous liquidity skew vs.
lagged price/volume statistics).

**Sourcing cost — lowest available.** `engine.py:10` and `strategies/base.py:12` already
import `OrderBook` from `kraken_api.models`, so the object shape is in-tree and pinned.
Kraken's public `/0/public/Depth` and `/0/public/Trades` are keyless; rate-limited by the
sibling transport's fixed `min_interval` throttle (`/home/seanc/Projects/kraken-python/kraken_api/transport.py:181-186`).

**Landing point.** A store adapter / recorder writing `bid_vol` + `ask_vol` (and a
forward-only depth log with a timer), **plus** widening `features.py:94-125` so the merge
seam admits them. Feature-engineering step is already written; config key would be a new
`microstructure_features_file` alongside `funding_features_file` (`configs/default.yaml:92`).

---

### 🥈 G2 — The one checked-in exogenous signal log is **one record deep**: 6 observation columns are structurally zero across the entire training window, and no code path can ever deepen them
**Category: improve-what-exists (coverage / "silent absence at scale") + data reliability**

**Evidence.**

```
$ wc -l signals/eth_usd_funding.jsonl
1 signals/eth_usd_funding.jsonl
```
That single line is `"timestamp": "2026-10-02T00:00:00+00:00"` — one reading, at the right
edge of the frame. `configs/default.yaml:92` points `funding_features_file` at it, and the
key is non-null, which `configs/default.yaml:85-91` explicitly documents as "a DECLARED
INTENT to use the channel".

Columns this one record feeds, per `features.py:94-125`:
`funding_rate`, `basis`, `open_interest`, `funding_rate_prediction`, `vol24h`, plus
`bid`/`ask` → `spread` (`features.py:999-1006`) — **6 observation columns of the ~55 the
pipeline produces** (`features.py:92` — "49 -> 55").

With `configs/default.yaml:126` `signal_max_age_hours: 12`, the single record is carried at
most 12 bars (`data.py:787` `bound_hours`, applied at `data.py:799-800`). The training
frame is the ~721-bar live REST ceiling (`data.py:28`, `data.py:1232`, `data.py:1321`). So **≈709 of
721 bars read these six columns as their historical `0.0` fill** (`data.py:816`
`filled[col].fillna(0.0)`), flagged only by `signal_observed == 0` (`data.py:812`).

The producer is forward-only by construction. `justfile:158` runs the sibling with
`--append`; the unit's own comment says it:
> *"The file grows by ~1 line per hour, so it is a log, not state."*
(`systemd/kraken-trading-bot-funding.service.in`)

And there is **no backfill recipe anywhere in `justfile`** — the only two signal recipes are
`funding-timer` (`justfile:135`) and `funding-pull` (`justfile:152`), both of which append
*the current hour*. There is no `funding-backfill`, no rotation, no archival fetch.

**Directness — very high, but not new plumbing.** The columns already flow end to end; they
are simply empty almost everywhere. Fixing this changes the *values* of 6 existing
observation columns across a whole training run, which is the largest single change to what
the agent actually sees available anywhere in this repo.

**Sourcing cost — keyless.** `kraken-funding-rates` already authenticates nothing; the
only open question (Phase 2's to answer, not mine) is whether a *settlement history* is
reachable from it or from Kraken Futures' published history — that determines backfill vs.
forward-only-forever.

**Landing point.** A backfill/replay step for the JSONL seam (or a store adapter that keeps
historical funding on the same parquet grid as `kraken-market-data`), plus possibly a config
key for the history window. No change to `features.py`.

---

### 🥉 G3 — No retry, no backoff, and no rate-limit handling anywhere on the OHLCV fetch path; a partial page-loop failure discards every candle already collected
**Category: data quality & reliability of what is already pulled**

**Evidence.**

`data.py:1104-1111` — the paging loop, verbatim:
```python
1104:    for _ in range(pages):
1105:        batch, last = manager.ohlc(pair, interval=interval, since=cursor)
1106:        if batch:
1107:            collected.extend(batch)
1108:        if last == 0 or not batch:
1109:            _LOGGER.debug("OHLC history exhausted at cursor %s", cursor)
1110:            break
1111:        cursor = last
```
No `try`. No `except`. No `time.sleep`. No attempt counter. An exception on page 4 of 6
propagates out of `fetch_ohlc_dataframe` and the 3 already-collected pages are lost.

The upstream library confirms there is nothing below to catch: in
`/home/seanc/Projects/kraken-python/kraken_api/transport.py`, `_request`'s
`except requests.RequestException` block (`transport.py:210-221`) **logs and re-raises**,
and a rate-limit envelope becomes a terminal `raise RateLimitError(errors, endpoint=path)`
at `transport.py:250`. `_throttle()` (`transport.py:181-186`) is a *fixed* inter-call
spacing, not adaptive, and there is no retry loop anywhere in `transport.py`.

Amplification:
* `data.py:1364` — the store leg calls the same `_page_candles` on **every** read, so a
  transient 429 aborts a read even though the store already holds the bars it wanted.
* `justfile:76-77` — `just bench` runs `train` then `backtest`, i.e. two full independent
  re-pages of Kraken back to back.
* `engine.py:57-74` — three separate bare `except Exception` → `_LOGGER.warning`. A
  rate-limited engine tick returns a dict that is *silently missing* keys; to `sma.py` that
  is indistinguishable from "no data this minute", and `sma.py:72-76` answers `hold` with
  reason `"insufficient data"`.

**Directness — protective rather than additive.** It does not add a column; it is the only
thing standing between a transient Kraken error and a truncated or zero-length frame
(`data.py:1181-1182` raises `NotEnoughDataError`, `data.py:1383` the same). Every one of
the ~55 columns depends on it.

**Sourcing cost — self-hosted, no new data source at all.** Purely this repo (plus a
decision about whether the retry belongs in `data.py` or upstream in `transport.py`).

**Landing point.** `data.py:1071-1112` (`_page_candles`), which both the fetch leg
(`data.py:1179`) and the store leg (`data.py:1364`) already share — one edit covers both.

---

### 🏅 G4 — The default config disables the only route past the ~720-bar ceiling, and nothing appends to the store on a schedule
**Category: improve-what-exists (thin historical depth vs. what the models need)**

**Evidence.**

* `configs/default.yaml:185` — `market_data_store: null`. `data.py:1304-1316` therefore
  short-circuits the whole store branch to the pure live fetch, and the deep-history
  branch documented at `data.py:1318-1329` never executes.
* The ceiling is stated three times in-tree: `data.py:28`, `data.py:1232`, `data.py:1321` ("this branch serves years of bars instead of Kraken's ~720-bar REST
  ceiling").
* `data.py:93` — `STORE_SEED_HINT` puts the seeding cost at "~158s and ~13MB of
  Binance-archive monthly klines" for one pair/interval. `justfile:210` / `justfile:218`
  make it one-shot and manual.
* **No appender.** `data.py:1364-1372` (`_page_candles` → `store.upsert`) is the *only* code
  that writes live bars into the store, and it is downstream of the `market_data_store`
  null check at `data.py:1304`. So on the default config the store never grows, and on the
  store config it only grows to the ~720-bar ceiling — **an archive seed that can never be
  topped up with "now"**.
* Unused machinery behind it: `data.py:1298-1302` (a `.. todo::` naming it):
  > *"honour `since`/`until` from config; drive train/eval split and walk-forward through
  > the currently-unused `TradingEnvironment.reset(options=...)`"*
  and `data.py:1325-1329` confirms `since`/`until` still default to `None`.
  `prepare_episode` then `.tail(episode_bars)`s the whole store (`data.py:1540`).
* `configs/default.yaml:256` `data_window:` exists and *is* read by train
  (`train.py:232` `window = resolve_data_window(cfg)`, `train.py:21` import) — so the split
  machinery is live; it is the **depth** under it that is missing.

**Directness — broad.** Not one column but the length of every rolling window. With
`configs/default.yaml:43` `feature_windows: [1, 4, 24]`, the deepest look-back is 24 bars,
so 721 bars is workable for one episode — but `data_window`'s train/eval split and
`tools/model_matrix.py`'s multi-seed sweep both want more independent windows than a single
720-bar read provides.

**Sourcing cost — keyless, ~158 s / ~13 MB per pair** (`data.py:91-99`). No paid feed.

**Landing point.** A store-append scheduler (a timer next to
`systemd/kraken-trading-bot-funding.timer`), plus honouring `since`/`until` at
`data.py:1381`.

---

### 🏅 G5 — Dead machinery on the strategy side, and a wrong comment about the bar interval
**Category: improve-what-exists (dead code paths, duplicated/incorrect constants)**

**Evidence.**

* `engine.py:72` fetches an L2 book every tick and `strategies/base.py:92` advertises it as
  a `tick()` key, but the only `tick()` in the repo reads `candles` + `ticker` only
  (`sma.py:68-69`). `strategies/` contains exactly `base.py` and `sma.py`.
* `engine.py:65` — the comment is **wrong**:
  ```python
  64:            # Fetch recent candles (1-hour candles, last 100)
  65:            candles, _ = self.manager.ohlc(pair, interval=60)
  66:            data["candles"] = candles[-100:] if candles else []
  ```
  `interval=60` is **60 minutes = 1 hour**, so the comment is accidentally right about size
  and wrong about the API's unit convention (the same key is read as
  `ohlcv_interval_minutes` at `train.py:211` and `paper_trade.py:174-176`, where 60 is
  explicitly minutes). A reader cross-checking against the RL side gets no cue about which
  unit convention applies here. The `100` at `engine.py:66` is unrelated to
  `long_period=30` (`sma.py:32`) and undocumented.
* Duplicated constant, same name, two different tuples, two modules:
  `data.py:58` `_OHLCV_COLUMNS = ("time", "open", "high", "low", "close", "vwap", "volume", "count")`
  vs `features.py:23` `_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")`.
  Same for the signal list, which *was* done correctly by import (`data.py:46-51`) — the
  inconsistency is local to the OHLC tuple.
* Two overlapping derivations of the same set that must agree:
  `features.py:149` `_SIGNAL_BUILDER_INPUT_COLUMNS` and `features.py:171-173`
  `POINT_IN_TIME_EXOGENOUS_COLUMNS` (which unions `_SIGNAL_BUILDER_INPUT_COLUMNS` again).

**Directness — indirect for the RL path** (`rl/` never imports `engine.py`), but it is the
reason the *contract* for "what a strategy is fed" is undocumented, and it is the natural
place for any non-RL data source to land. **Sourcing cost: n/a** — this is repo hygiene.

**Landing point.** `engine.py:46-76`, `strategies/base.py:85-96`, and the two `_OHLCV_COLUMNS`
declarations.

---

### 🏅 G6 — The shipped config trains and backtests at **zero fees and zero slippage**
**Category: improve-what-exists (config that exists but understates; silent optimism)**

**Evidence.**

```
configs/default.yaml:17:fee_rate: 0.0         # fractional fee per executed order
configs/default.yaml:18:slippage: 0.0         # fractional adverse price move on fills
```
and the *code* defaults agree, so a config that simply omits the key is equally free:
```
train.py:262:        fee_rate=float(cfg.get("fee_rate", 0.0)),
train.py:263:        slippage=float(cfg.get("slippage", 0.0)),
```
A dispersion/cost-aware acceptance gate exists (`tools/cost_aware_gate.py`, prior pass #8)
and `tools/width_check.py` guards feature width — so costs are represented in the *tooling*
and absent from the *default training run*, which is the one most people actually execute.

**Directness — global.** Every trained artifact produced from the default config learned a
policy under a frictionless market. `configs/matrix.example.yaml` is what should be scanned
for whether the swept cells cost anything.

**Sourcing cost — n/a.** Config values.

**Landing point.** `configs/default.yaml:17-18` and the `train.py:262-263` fallbacks.

---

### 🏅 G7 — Whole data categories with zero presence anywhere in the tree
**Category: alternative / crypto-native / macro (absence-only, listed for completeness)**

Evidence of absence is structural and easy to verify because this repo keeps single-source
allow-lists:

* `features.py:94-125` `_SIGNAL_COLUMNS` is the **complete** exogenous allow-list. It has no
  column for on-chain (gas, L2 throughput, exchange netflow, stablecoin supply), no macro
  calendar (CPI/FOMC/NFP), no options surface (IV/skew/term structure), no cross-exchange
  basis or cross-venue volume, no research/academic signal.
* `data.py:144-156` `_SIGNAL_CHANNELS` has exactly **three** entries
  (`extra_features_file`, `funding_features_file`, `social_features_file`) and
  `data.py:139-143` documents that *both* the fetch and store legs iterate this one list —
  so it is genuinely the single door, and anything new is additive to this tuple plus
  `_SIGNAL_COLUMNS`.

**Directness — none *today*** (no consumer exists), but the landing point is unusually
cheap for anything additive: add to two tuples and the existing hardened seam
(`data.py:591-842`) does the rest.

**Sourcing cost — varies widely** across this set: on-chain and exchange netflow are
keyless-but-heavy; a macro calendar is keyless (public economic-release feeds); options
IV/skew on Kraken is likely unavailable and would need a different venue (paid or
scraped); research/academic signal is not a data feed at all.

---

## 3. Category coverage check (self-check required by the brief)

| Category | Candidates |
|---|---|
| Improve what exists (dead machinery / unapplied code / config nothing reads / thin depth / silent absence) | G1, G2, G3, G4, G5, G6 |
| Market microstructure (order-book depth, trade tape) | G1, G5 |
| Data quality & reliability (retry, backoff, backfill, validation) | G2, G3 |
| Alternative / crypto-native / macro | G7 |

Not a single-category list. **Four** distinct categories across **seven** candidates.

---

## 4. What I checked and found clean (so nobody re-audits it)

* The three exogenous seams are genuinely hardened — ticker filter (`data.py:845-932`),
  hour dedup (`data.py:755-760`), bounded ffill + freshness columns
  (`data.py:780-828`, `features.py:94-132`), and absence ≠ neutral (`data.py:806-816`).
  No gap claimed here.
* The `_SIGNAL_COLUMNS` single-source-of-truth pattern works: `data.py:46-51` imports it
  from `features.py` rather than restating it.
* Store misconfiguration is caught loudly — `_store_root_status` (`data.py:1401-1431`)
  distinguishes "csv-only fallback seed" from "empty", which is a real failure mode
  handled well.
* `features.py` non-finite guarding is thorough and self-documented, including its own
  known gap (`features.py:820-826`, the `ewm` family).
* Signal staleness is visible to the agent, not silent (`signal_observed` /
  `signal_age_hours`, `features.py:104-108`).

## 5. Open questions deliberately left to Phase 2

1. Is a historical funding-settlement series reachable keylessly (G2's backfill), or is the
   channel structurally forward-only?
2. Does the depth/tape fetch belong in a new sibling recorder or inside
   `kraken-market-data` as another grid (G1)?
3. Should the retry live in `data.py:_page_candles` or upstream in `kraken-python`'s
   `transport.py` (G3)?