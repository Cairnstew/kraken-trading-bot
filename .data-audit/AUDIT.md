# Data-pipeline audit — `kraken-trading-bot`

**Pass:** PHASE 1 re-audit (audit-pipeline-1002, agent `auditor`)
**HEAD audited:** `ca5364270a10aebf81681591825a698917256c2a` ("docs: audit-pipeline run log 2026-10-02 (G1 pass)")
**Supersedes:** the `.data-audit/AUDIT.md` at `ca53642` (same commit, overwritten in place)
**Mode:** read-only except this file. No code written, nothing committed, nothing deleted.

---

## 0. Executive summary

The pipeline has a well-built **read seam** and a well-built **feature seam**, and almost nothing
behind them.

| Layer | State |
|---|---|
| OHLCV fetch + pagination | Works. One `ohlc` call site, paged client-side. Hard ~721-bar ceiling. |
| Local store / deep history | **Never built.** `market_data_store: null`; the store root does not exist on disk. |
| Exogenous signals | **~0.5% coverage.** One configured channel; its log holds a single record. Measured 4 of 721 bars observed. |
| Microstructure features | **Structurally dead.** One of five configured feature groups yields 0 columns, then 1 constant-zero column. |
| Order-book feed | **Fetched and discarded.** 1440×/day from `/0/public/Depth`, consumed by nothing. |
| Depth of history | ~30 days max. The pinning/split machinery exists but is unreachable and inert. |
| F&G 2018+ history | **Already downloaded on every social pull, then thrown away.** |
| Fetch reliability | No retry, no backoff, no partial salvage on the RL read path. |
| Models on disk | `models/` contains only `.gitkeep`. No trained artifact exists. |

**The single most consequential fact:** the shipped `configs/default.yaml` configures
`funding_features_file` to a non-null path (`configs/default.yaml:92`) while the file behind it
holds **one record**. `merge_extra_features` correctly refuses a *missing* or *empty* file
(landed as G1) but a **present-and-non-overlapping** file is only a WARNING
(`kraken_trading_bot/rl/data.py:662-671`). So the default path silently trains with **8 of 60
observation columns carrying no information at all**, and no validity gate anywhere notices.

---

## 1. Pipeline map, end to end

### 1.1 The two entry paths

There are **two disjoint pipelines** in this repo, and nothing connects them.

```
  LEG A — rule-based `run` (cli.py:348 → engine.py)
    KrakenManager.ticker(pair)          engine.py:59    every 60s per pair, keyless
    KrakenManager.ohlc(pair, int=60)    engine.py:65    every 60s per pair, keyless
    KrakenManager.order_book(p, 10)     engine.py:72    every 60s per pair, keyless  ← DISCARDED
      └─> TradingEngine.fetch_market_data -> dict{ticker, candles[-100:], order_book}
          └─> Strategy.tick(market_data)          engine.py:136
                └─> SMAcrossoverStrategy.tick     strategies/sma.py:57
                     reads data["candles"] + data["ticker"] ONLY   sma.py:68-69

  LEG B — RL train/backtest/paper (cli.py:479/564/450)
    KrakenManager.ohlc(pair, interval=, since=cursor)   data.py:949   paged, keyless
      └─> candles_to_dataframe              data.py:876
      └─> add_derived_ohlcv_features        data.py:812   vwap/count -> 3 cols
      └─> merge_extra_features x3           data.py:435   news/funding/social JSONL
      └─> [optional] MarketDataStore.upsert  data.py:1176
      └─> [optional] MarketDataStore.read    data.py:1191
      └─> resolve_data_window / clip        data_window.py:240-272
      └─> prepare_episode                   data.py:1255  tail-slice + fit
      └─> FeaturePipeline.compute           features.py:452
      └─> TradingEnvironment._raw_feature_array  environment.py:470  ffill/fillna/z-score
```

**There is no code path between Leg A and Leg B.** `engine.py` imports nothing from `rl/`;
`rl/*` imports nothing from `engine.py` or `strategies/`. The `order_book` fetched at
`engine.py:72` never reaches an RL feature, and the OHLCV that trains the policy is a different
fetch path with a different cadence from the OHLCV a `tick()` sees.

### 1.2 Every `kraken_api` call site in this repo (exhaustive)

| # | Call | Location | Cadence | Auth | Failure handling |
|---|---|---|---|---|---|
| 1 | `manager.ticker(pair)` | `engine.py:59` | every `--interval` (60 s) per pair | keyless | bare `except` → WARNING, key absent from dict (`:60-61`) |
| 2 | `manager.ohlc(pair, interval=60)` | `engine.py:65` | same | keyless | bare `except` → WARNING, `candles` absent (`:67-68`) |
| 3 | `manager.order_book(pair, count=10)` | `engine.py:72` | same | keyless | bare `except` → WARNING (`:73-74`) |
| 4 | `manager.ohlc(pair, interval=, since=)` | `data.py:949` (`_page_candles`) | once per `pages`, per read | keyless | **none** — propagates (see CAND-4) |
| 5 | `KrakenManager.from_env()` / `.paper()` | `engine`/`data.py:1019,1169`, `paper_trade.py:151`, `backtest.py:393`, `cli.py:356,358,385,404,428` | construction | key for `from_env` | n/a |

**Never called anywhere in this repo**, though all exist on `KrakenManager`
(`~/Projects/kraken-python/kraken_api/manager.py`) — and three of them are paginable:

| Method | manager.py | `since`? | Backfillable? |
|---|---|---|---|
| `recent_trades(pair, since)` → `(trades, last)` | `:192-198` | **yes** (client `:128-132`) | **yes** — `since` + `last` cursor |
| `spread(pair, since)` → `(points, last)` | `:200-210` | **yes** (client `:134-138`) | bounded (`since` accepted, default lookback ~360 s) |
| `order_book(pair, count)` | `:185-190` | **no** (client `:121-126`) | **no** — snapshot only |

### 1.3 Storage / caching

| Store | Config key | Default | Exists on this host? |
|---|---|---|---|
| `kraken-market-data` store root | `market_data_store` (`configs/default.yaml:161`) | `null` | **No** — `~/Projects/kraken-market-data/store` absent, 0 `.parquet` files |
| News signal log | `extra_features_file` (`:57`) | `null` | No file |
| Funding signal log | `funding_features_file` (`:92`) | non-null | **1 record** (`signals/eth_usd_funding.jsonl`, 364 B, `2026-10-02T00:00:00Z`) |
| Social signal log | `social_features_file` (`:100`) | `null` | No file |
| Model artifacts | `models/{TICKER}/{NAME}/` | — | **Empty** — `models/.gitkeep` only |

**OHLCV is not cached anywhere by default.** Every `train`, `backtest`, `paper-trade` tick and
`export-data` re-fetches from Kraken. With the store unset (`data.py:1137-1149` is a
pass-through), the trailing ~721 bars are re-pulled every time.

### 1.4 Where each input enters the RL observation

`FeaturePipeline.compute` (`features.py:452-487`) dispatches five groups. Measured widths on a
721-bar synthetic frame, `feature_windows: [1,4,24]`, all five groups (the shipped default,
`configs/default.yaml:43,48`):

| Group | Width | What it reads | Source |
|---|---|---|---|
| `price` | 10 | `close/high/low` | OHLCV frame |
| `technical` | 33 | `close/high/low` | OHLCV frame |
| `volume` | 6 | `close/volume` | OHLCV frame |
| `microstructure` | **0 → 1** | `spread`, or `bid`/`ask`; `bid_vol`/`ask_vol` | **nothing produces these** |
| `signals` | 3 → 11 | `_SIGNAL_COLUMNS` present in the frame | merge seam + `add_derived_ohlcv_features` |
| **total** | **52** bare → **60** with the funding file → **65** at the full allow-list | | |

Key mechanics:

- **Entry point for OHLCV:** `data.py:876` `candles_to_dataframe` → `data.py:1037`
  `add_derived_ohlcv_features` → `features.py:452`.
- **Entry point for exogenous signals:** `data.py:435` `merge_extra_features` — the *single* seam
  (`data.py:12-16`), joining JSONL onto the frame by hour-floored bar time
  (`data.py:603-604`), with a bounded forward-fill (`data.py:642-648`) and a freshness pair
  (`data.py:656-657`).
- **`vwap_dev` / `trade_count_zscore_20` / `volume_per_trade`** are derived at the read seam from
  columns every OHLCV frame already carries (`data.py:853-871`), then ride the `signals` group
  into the observation (`features.py:606-610`).
- **Absence policy:** every exogenous NaN is `ffill().fillna(0.0)` then z-scored
  (`environment.py:488-496`). `signal_observed` / `signal_age_hours`
  (`features.py:56-57`) exist so the zero-fill is unambiguous.
- **Warm-up boundary:** `features.py:128` `first_tradable_index` deliberately excludes
  point-in-time exogenous columns (`features.py:123-125`, `features.py:150-152`).

### 1.5 Sibling producers (what already exists)

| Sibling | Output | Wired into the bot? | Producer has history? |
|---|---|---|---|
| `kraken-market-data` | month-sliced OHLCV parquet store + `_meta.json` cursor | key exists, **store never built** | n/a |
| `kraken-deep-history` | seeds the above from the **Binance public archive** (ZIPs) | documented in `configs/deep-history.example.yaml`, **not run** | 2018→ for mapped tickers |
| `kraken-funding-rates` | hourly JSONL: `funding_rate`, `funding_rate_prediction`, `basis`, `open_interest`, `bid`, `ask`, `vol24h`, `mark_price`, `index_price` | **wired** (`default.yaml:92`) but log has 1 record | **forward-only**, no backfill |
| `ticker-news-signals` | hourly JSONL: `sentiment_score`, `article_count`, `novelty_flag` | key exists, `null` (`default.yaml:57`), **no timer** | `--lookback-hours` bounded; article window only |
| `kraken-social-signals` | hourly JSONL: `stt_mention_count`, `stt_tilt`, `fng_index` | key exists, `null` (`default.yaml:100`), **no timer** | **`fng_index` has full 2018+ daily history available** (`kraken-social-signals/kraken_social_signals/client.py:266-270`) |

---

## 2. Prior-pass hypotheses: confirmed / refuted

Re-derived from the code at the current HEAD, not assumed.

| # | Prior claim | Verdict | My evidence |
|---|---|---|---|
| **G1** | Signal seam now resolves the file its own config names and refuses by name | **LANDED** (not re-audited) | `data.py:529-538` (missing), `data.py:552-569` (empty), `data.py:239-306` (`SignalFileNotFoundError` names key + raw spelling + expanded path + producer command), `_SIGNAL_CHANNELS`/`_channel_producer` `data.py:118-130,176-191` |
| **G2-R1** | Derive `vwap_close_gap_zscore_20` + `count_per_range` from `vwap`/`count` (no new source) | **REFUTED as an open gap — already landed, under different names** | `data.py:812-873` `add_derived_ohlcv_features` derives `vwap_dev = close/vwap - 1`, `trade_count_zscore_20` (20-bar rolling z, ddof=0), `volume_per_trade = volume/count`; allow-listed at `features.py:58-60`; forwarded by `_add_signals_features` `features.py:606-610`; called on **both** read legs (`data.py:1037`, `data.py:1196`) and re-applied to caller-supplied frames (`backtest.py:426`, `paper_trade.py:_build_observation`). Measured: 3 of the 52 bare-observation columns are these. |
| **G2-R2** | A `kraken-microstructure` sibling: `/public/Depth` history; `/public/Spread` "claimed not backfillable" | **PARTLY REFUTED — re-scoped, and the prior pass's trade-off was wrong** | No such sibling exists. `/0/public/Depth` genuinely has **no** `since` (snapshot only, `kraken_api/client.py:121-126`, `manager.py:185-190`) so book history is not backfillable from Kraken. **But** `/0/public/Trades` **is** paginable — `client.py:128-132` (`since`) returns `last`, `manager.py:192-198` wraps it as `recent_trades(pair, since) -> (trades, last)`. And `/0/public/Spread` **also** accepts `since` (`client.py:134-138`, `manager.py:200-210`) — it is bounded, not absent. The prior pass therefore under-sold the option: a **trade-tape** recorder is keyless, historical, and yields a materially richer feature family (realized spread, trade-size distribution, VWAP-vs-close, Amihud illiquidity, signed-volume imbalance) than a snapshot `Depth` recorder. See CAND-1. |
| **G3** | Lift the ~721-bar historical ceiling (`kraken-deep-history` → Kraken's own OHLCVT bulk archive) | **CONFIRMED, and worse than stated** | Ceiling real and documented: `tools/model_matrix.py:233-239` — "`--pages 2` has been measured returning 721 bars, not 1440… does NOT scale". `configs/matrix.example.yaml:115-118` repeats it. Nothing lifted it: `market_data_store: null` (`default.yaml:161`) and the store root is **absent from disk** (0 parquet files). Two additions the prior pass missed: (a) `train.py:215-226` and `backtest.py:400-419` **never pass `since`/`until` to `read_ohlc_dataframe`**, so a `data_window` is only a post-hoc mask (`data_window.py:240-272`) on a trailing fetch — any pinned window older than the last ~721 bars clips to empty; (b) the seeder is **cross-venue** — `kraken-deep-history/kraken_deep_history/export.py:45-48`: "Kraken-style ticker → Binance \*USDT spot symbol". A Kraken-pair model trained on Binance-USDT bars inherits a basis the pipeline never models. |
| **G4** | `tools/model_matrix.py` hard-codes `MIN_REPLICATES_FOR_A_CLAIM = 3` and never compares it to observed spread | **CONFIRMED** | `model_matrix.py:243`. Both uses are pure counts: seeds `< 3` → WARN (`:1276-1283`); `(ticker, config)` groups with `< 3` cells → WARN (`:1339-1347`). The dispersion machinery already exists and is computed — `summarize()` returns `q1`/`q3`/`min`/`max` (`:328-339`), `quartile()` (`:310-325`), `fmt_iqr()` prints them (`:363-365`). `is_valid()` (`:963-978`) checks only process failure, required fields, `n_bars` vs a denominator, `num_trades`, and NaN metrics (`assess_cell` `:786-878`). Nothing anywhere compares a within-group IQR against a between-group median gap. See CAND-5. |
| **G5** | No retry/backoff/partial-failure on `data._page_candles`; one transient 5xx aborts train/backtest mid-read | **CONFIRMED** | `data.py:946-956` — a bare `for _ in range(pages)` loop containing `batch, last = manager.ohlc(...)`. No `try`, no retry, no backoff, no sleep, no partial-batch salvage. Pages 0..k-1 are discarded and the exception propagates through `fetch_ohlc_dataframe` → `read_ohlc_dataframe` → `train_ticker` → `cmd_train`'s blanket handler (`cli.py:511-516`, which prints `Error training …` and returns 1). Contrast `engine.py:57-74`, where all three fetches degrade to WARNING + a partial dict. No throttle either: `paper_trade.py` re-reads `_FETCH_PAGES` pages every tick (default `--interval 60`, `cli.py:110-115`). See CAND-4. |
| **G6** | The exogenous signal channel is forward-only (no history before the log's start) | **CONFIRMED and materially understated** | The log is not merely forward-only — it holds **one record**: `signals/eth_usd_funding.jsonl`, a single line dated `2026-10-02T00:00:00Z`. Measured over a 721-bar frame with the shipped `signal_max_age_hours: 12` (`default.yaml:126`): `signal_observed` mean **0.0055**, i.e. **4 of 721 bars observed**; `signal_age_hours` mean **−0.986** (i.e. the −1.0 sentinel from `data.py:100` on ~99% of bars). The 8 signal columns this channel adds to the observation are constant-zero or single-snapshot on 717 of 721 bars. And "no record overlaps the window" is only a WARNING (`data.py:662-671`) — the G1 refusal deliberately covers *missing*/*empty*, not *non-overlapping*. See CAND-2. |
| **G7** | `--ticker` is a blind `_`→`/` substitution (`USD_SOL` → `Unknown Kraken pair: 'USD_SOL'`) | **CONFIRMED** | `train.py:66` `pair_from_ticker_id` = `normalize_ticker_id(ticker_id).replace("_", "/")`, applied at `train.py:138` (`cfg["ticker"] = …`) before any validation. Same blind substitution at `backtest.py:401` and `paper_trade.py:173`. The failure surfaces many layers down in `~/Projects/kraken-python/kraken_api/catalog.py:220` — `f"Unknown Kraken pair: {ref!r}. …"`. CLI help only says "e.g. ETH_USD" (`cli.py:103, 141, 229, 303`). Partial mitigation exists but not for this: `paper_trade.py:177-183` validates the pair *contains* `/`, which catches shape but not order. `KrakenManager.known_pairs()` (`manager.py:131`) is the one-call fix. See CAND-6. |
| **G8** | Walk-forward unreachable — `reset(options=...)` is ceremonial; three episode-slicing mechanisms coexist | **CONFIRMED** | `environment.py:216-220` — the `options` parameter is documented in its own signature as "Unused; reserved for future start-bar overrides" and its body never reads it. Three coexisting slicers: (1) `prepare_episode(..., episode_bars=)` tail-slices and then fits stats (`data.py:1300-1309`); (2) `data_window.training_frame`/`evaluation_frame` split by `eval_split` (`data_window.py:293-330`); (3) `TradingEnvironment._start_index = first_tradable_index` warm-up skip (`environment.py:458-468`, `features.py:128-158`). A fourth: `paper_trade.py`'s `context_bars` → `df.tail(...)`. See CAND-7. |

---

## 3. Ranked candidate gaps

Ranked by (directness to the RL observation / `tick()`) × (evidence strength) ÷ (sourcing cost).
**Categories spanned: market microstructure, historical depth, dead config keys, data-quality/
reliability, and structural validity — five categories across seven candidates.**

---

### CAND-1 — The `microstructure` feature group is configured ON but structurally dead; a 1440×/day order-book feed is fetched and thrown away

**Category:** market microstructure + dead code path / effective-but-unapplied machinery

**Evidence**

| Fact | Where |
|---|---|
| `microstructure` is one of the five enabled groups | `configs/default.yaml:48`; `features.py:25` `_FEATURE_GROUPS` |
| The RL-side book consumer is the **only** consumer of book data in the whole repo, and it is keyed on column names **no producer anywhere emits** | `features.py:586-590` — `if {"bid_vol","ask_vol"}.issubset(df.columns)`. Repo-wide grep for `bid_vol`/`ask_vol` hits **only** `features.py:586-590`. No sibling producer writes them either. |
| `spread` is the group's other output, and it needs a `bid`/`ask` pair only the funding snapshot supplies | `features.py:580-585` |
| Measured: on a bare OHLCV frame the group yields **0 columns**; with the funding file it yields **exactly 1**, `spread`, which is a single ffill'd snapshot → `std == 0` → z-scores to identically `0.0` on every bar | computed at the shipped config; constant-column probe returned `['price_ratio_sma_1','bb_width_1','bb_pctb_1','spread']` |
| The engine fetches a **10-level `OrderBook`** every 60 s per pair and puts it in the tick dict | `engine.py:72` |
| `OrderBook` carries **full level lists** (`asks: list[BookLevel]`, `bids: list[BookLevel]`), from which depth-weighted imbalance, top-N depth, and a book slope are all derivable | `~/Projects/kraken-python/kraken_api/models.py:214-238` |
| **No `tick()` reads it.** The only `tick()` implementation reads `candles` and `ticker` and nothing else | `strategies/sma.py:68-69` (`data.get("candles", [])`, `data.get("ticker")`); `data["order_book"]` is documented as a common key at `strategies/base.py:92` and then never consumed |

**Directness to the RL observation:** **Highest of any candidate.** `_add_microstructure_features`
(`features.py:577`) already reads the frame and already knows the column names. A producer that
writes `bid_vol`/`ask_vol`/`spread` onto the frame lights the group up with **zero consumer-code
change** — the same presence-gated pattern `vwap_dev`/`trade_count_zscore_20` already use
(`data.py:853-871`, gated at `features.py:609`). This is the mirror image of G2-R1, which landed:
the machinery is written and waiting; only the input is missing.

**Directness to `tick()`:** also direct — `engine.py:72` already puts the object in the dict; a
strategy only has to read `data["order_book"]`.

**Sourcing difficulty:**
- `/0/public/Depth` snapshot — **keyless**, but **not backfillable** (`client.py:121-126` has no
  `since`). Needs a forward recorder like the funding one, plus a timer.
- `/0/public/Trades` — **keyless and paginable** (`client.py:128-132`, `manager.py:192-198`;
  returns a `last` cursor). Better value per call: realized spread, trade-size distribution,
  VWAP-vs-close, signed-volume imbalance, Amihud illiquidity — all derivable, none currently in the
  observation. **This is the option the prior pass missed** (see G2-R2 above).
- `/0/public/Spread` — keyless, `since` accepted but bounded (`client.py:134-138`).
- No paid tier and no API key required for any of the three.

**Cost:** a new sibling recorder + a `systemd.user` timer + an allow-list entry. The consumer side
is already built and dormant.

---

### CAND-2 — The exogenous channel has ~0.5% coverage, and the one piece of deep history a sibling already downloads is discarded on every pull

**Category:** thin historical depth + dead config keys + effective-but-unapplied machinery

**Evidence**

| Fact | Where |
|---|---|
| Funding channel is **non-null** — a declared intent — but its log holds **one record** | `configs/default.yaml:92`; `signals/eth_usd_funding.jsonl` (1 line, `2026-10-02T00:00:00Z`) |
| Measured over a 721-bar frame at the shipped `signal_max_age_hours: 12`: **4 / 721 bars observed**; `signal_observed` mean 0.0055; `signal_age_hours` mean −0.986 | computed from `data.py:435` with the shipped settings |
| "Records present but none overlapping" is only a **WARNING**, not a refusal — deliberate (`data.py:489-491` documents it as "the expected state of a young forward-only log") | `data.py:662-671` |
| The other two channels are `null` — off — in the shipped config **and** in the deep-history example | `configs/default.yaml:57, 100`; `configs/deep-history.example.yaml:58, 62` |
| **Exactly one timer exists in the whole repo**, for funding only, and it is **not declared in `nix/module.nix`** (which emits one service and zero timers) and **is not installed on this host** (`systemctl --user list-timers` lists 6 timers, none of them this) | `systemd/kraken-trading-bot-funding.{service,timer}`; `justfile:135-149`; `nix/module.nix:220` |
| **Fear & Greed 2018+ history is fetched on every social pull and thrown away.** `pipeline.py:123` calls `client.fetch_fear_greed(limit=0)`; `client.py:266-270` documents "`limit=0` returns the full 2018+ daily history in one keyless call". But `pipeline.py:130-144` builds records **only for hours present in the StockTwits `buckets`**, filtered to `--lookback-hours` (default **24**, `kraken-social-signals/kraken_social_signals/cli.py:64`), and uses F&G merely as a per-day *lookup* (`_fng_by_day`) for those hours. | `~/Projects/kraken-social-signals/kraken_social_signals/pipeline.py:118-144` |
| A **daily** series is emitted at hourly resolution and forward-filled up to 24× per day, so `fng_index` cannot vary within a day at all | same |
| `signal_observed` / `signal_age_hours` are written by the seam and read by **nothing except the observation** — `assess_cell` (`tools/model_matrix.py:786-878`) has no signal-coverage reason code, so an inert channel is invisible in matrix reports | repo-wide grep: `signal_observed` appears only in `data.py` and `features.py` |

**Directness to the RL observation:** **Very high.** The merge seam, the per-ticker filter, the
de-duplication, the bounded carry, the freshness pair and the observation allow-list all exist and
are all tested (`tests/test_rl_signal_config_wiring.py`, 1085 lines). **Only the input history is
missing.** Turning on a backfilled producer requires no consumer change at all.

**Directness to `tick()`:** none directly — this is the RL leg only.

**Sourcing difficulty:**
- `fng_index` full 2018+ daily history — **keyless, single call, already implemented and already
  being made.** Requires only a backfill path in the sibling and enabling `social_features_file`.
- StockTwits mentions — keyless but rate-limited (`client.py:14`, "the StockTwits rate limit");
  no credible deep history.
- News (`ticker-news-signals`) — **needs an API key** for the underlying article source; sparse
  and forward-only in practice.
- Funding (`kraken-funding-rates`) — keyless, but **Kraken Futures funding is not backfillable**;
  it is inherently forward-only. Depth must be *accumulated*, not backfilled.

**Cost:** the cheapest real win in the audit. A backfill branch in one sibling + one config flip +
one timer. The 8 currently-inert observation columns either become informative or should be dropped.

---

### CAND-3 — ~721 bars is the whole world: the store is unbuilt, `since` is never pushed into the fetch, and `eval_split` is inert in the shipped config

**Category:** thin historical depth + config keys nothing reads (inert by construction)

**Evidence**

| Fact | Where |
|---|---|
| Kraken's ceiling does not scale with `pages` | `tools/model_matrix.py:233-239`; `configs/matrix.example.yaml:115-118`; `configs/default.yaml:147` |
| The store that would lift it is **null and absent** | `configs/default.yaml:161` `market_data_store: null`; `~/Projects/kraken-market-data/store` does not exist, 0 `.parquet` files |
| **Neither RL caller ever passes `since`/`until` into the read.** So a `data_window` reaches the frame only as a post-hoc mask | `train.py:215-226` and `backtest.py:400-419` both omit `since`/`until`; the bounds are applied by `data_window.clip_to_window` (`data_window.py:240-272`) — whose own docstring says "the bounds are applied as a clip on the frame a caller already has" (`:40-45`) |
| ⇒ any pinned window older than the trailing ~721 bars clips to **empty** → `NotEnoughDataError` | `data.py:1297-1298` |
| `read_ohlc_dataframe` **already accepts and forwards** `since`/`until` to `store.read` | `data.py:1058-1059, 1191` — the plumbing exists, no caller uses it |
| **`eval_split: 0.7` in the shipped config is inert by construction.** `since: null, until: null` ⇒ `is_pinned` False ⇒ `has_split` False ⇒ both slicers return the whole frame | `configs/default.yaml:200-203`; `data_window.py:88, 98, 309-312, 327-330`; documented at `configs/matrix.example.yaml:182-184` |
| The deep-history example still leaves news + social `null` — i.e. deep OHLCV is offered bundled with an inert signal block | `configs/deep-history.example.yaml:58, 62` |
| The seeder is **cross-venue** (Binance \*USDT spot), a basis the pipeline never models | `kraken-deep-history/kraken_deep_history/export.py:45-48` |
| No `n_bars` floor anywhere in the default path: a run that trains on 12 bars succeeds | `train.py:298` records `n_bars` as *provenance* only; the floor lives in the matrix harness (`assess_cell` `:786-878`), not in `train` |

**Directness to the RL observation:** **Highest of any candidate** — this *is* the training
window. Every rolling feature, the 24-bar warm-up boundary, and the normalization stats are fitted
on whatever the read returns. 721 bars is roughly **one month** of 1-hour data: enough to fit
55–60 z-scored columns, not enough to train a policy that generalises, and not enough for a
meaningful out-of-sample split even if one were reachable.

**Directness to `tick()`:** indirect, but decisive — the store leg is the only documented route to
making `context_bars` (`paper_trade.py`) bounded rather than whole-history.

**Sourcing difficulty:**
- `kraken-deep-history` → Binance public archive ZIPs: **keyless, free, bulk, 2018→.** Already
  implemented (`client.py`, `seeder.py`, `cli.py`). Only needs running, plus a store root.
- Kraken's own bulk OHLCVT archive: keyless, venue-consistent, but a new reader.
- **Cost of the cheap route:** cross-venue basis. Must be priced in explicitly, or the
  `mark_price`/`index_price`/`basis` already present in the funding record
  (`signals/eth_usd_funding.jsonl`) is the only venue-gap handle the pipeline has.

---

### CAND-4 — No retry, backoff, throttle or partial salvage on the RL read path

**Category:** data-quality / reliability of what is already pulled

**Evidence**

| Fact | Where |
|---|---|
| `_page_candles` is a bare loop: no `try`, no retry, no backoff, no sleep, no salvage | `data.py:946-956` |
| Both read legs inherit it | `fetch_ohlc_dataframe` `data.py:1023`; store leg `data.py:1174` |
| A failure on page *k* discards pages 0..k-1 and propagates | through `train_ticker` to `cmd_train`'s blanket handler, `cli.py:511-516` |
| The engine does the opposite — all three fetches degrade to WARNING + a partial dict | `engine.py:57-74` |
| No throttle: `paper_trade` re-reads `_FETCH_PAGES` pages **every tick** (default 60 s) | `paper_trade.py:_fetch_data`; `cli.py:110-115` |
| No duplicate-bar / gap detection anywhere: `fetch_ohlc_dataframe` de-duplicates by timestamp and sorts, but never checks for **missing bars** | `data.py:1030-1031` |
| A store read that returns empty raises `NotEnoughDataError`, but a *short* store read (the common case: store not seeded) does not — it just trains on whatever is there | `data.py:1192-1193` |

**Directness:** not a feature — it is the difference between a run happening and not. A single
transient Kraken 5xx/timeout aborts a 10k-timestep training run, a matrix cell, or a paper session,
with an error message that names the *command*, not the transient network cause.

**Sourcing difficulty:** none — no new data. This is pure code.

---

### CAND-5 — The matrix gates claims on a replicate *count*, never on observed *dispersion*

**Category:** structural validity of the measurement pipeline

**Evidence**

| Fact | Where |
|---|---|
| `MIN_REPLICATES_FOR_A_CLAIM = 3`, used only as a count | `tools/model_matrix.py:243`; seeds at `:1276-1283`; groups at `:1339-1347` |
| The dispersion machinery is **already computed** and printed, never compared | `summarize()` → `q1`/`q3`/`min`/`max` at `:328-339`; `quartile()` `:310-325`; `fmt_iqr()` `:363-365`; rendered at `:1952` and `:2094` |
| `is_valid()` checks only process failure, required fields, `n_bars` vs a denominator, `num_trades`, NaN metrics | `:963-978`, `assess_cell` `:786-878` |
| The docstring's own framing ("separated from noise") is never operationalised | `:241-242`, `:1281-1282` |
| Motivating measurement already in the repo: two fetches of the same pair "disagree by 16% on a fitted std" | `data_window.py:5-8`; `configs/default.yaml:165-169` |

**Directness:** does not feed the RL features. It gates **whether a claimed effect of a data or
config change is believable** — which is precisely the question a data-pipeline audit is asked.
With n=3 the IQR is a 50th-percentile-of-2 estimate; nothing checks whether the median gap between
two arms exceeds the within-arm spread, so a matrix report can present a noise-ranked difference as
a finding. Two cells whose IQRs overlap completely look identical to two that barely separate.

**Sourcing difficulty:** none — no new data; `q1`/`q3` are already in the records file.

---

### CAND-6 — `pair_from_ticker_id` blind-substitutes, so a reversed ticker id dies three layers from the CLI flag

**Category:** data-quality / reliability

**Evidence**

| Fact | Where |
|---|---|
| `pair_from_ticker_id` = `normalize_ticker_id(ticker_id).replace("_", "/")` | `train.py:66`, applied at `train.py:138` **before any validation** |
| Same blind substitution in the two other consumers | `backtest.py:401`; `paper_trade.py:173` |
| The error surfaces deep in the catalog, naming the *reversed* pair | `~/Projects/kraken-python/kraken_api/catalog.py:220` |
| `--ticker` help gives only a positive example | `cli.py:103, 141, 229, 303` |
| Partial mitigation exists but misses this case: it checks the pair *contains* `/`, not that base/quote are in Kraken's order | `paper_trade.py:177-183` |
| The repo already normalises `XBT`↔`BTC` for *signal* matching — the knowledge exists, in the wrong layer | `data.py:105` `_TICKER_ALIASES`, `data.py:330` |
| `KrakenManager.known_pairs()` would resolve it in one keyless call | `~/Projects/kraken-python/kraken_api/manager.py:131` |

**Directness:** no feature effect. It silently costs runs and — because `models/{TICKER_ID}/` is
keyed on the *un*-validated id (`registry.py`, `normalize_ticker_id`) — a mistyped id creates a
model directory that can never be backtested against a correctly-spelled one.

**Sourcing difficulty:** none — no data, no key.

---

### CAND-7 — Walk-forward is structurally unreachable; four episode-slicing mechanisms coexist, none of them a walk

**Category:** structural (prior hypothesis G8, re-derived)

**Evidence**

| Fact | Where |
|---|---|
| `reset(options=...)` accepts a dict and never reads it — documented in its own signature | `environment.py:216-220` |
| Mechanism 1 — trailing tail slice, then fit | `data.py:1300-1309` (`prepare_episode(episode_bars=)`) |
| Mechanism 2 — single static train/eval split | `data_window.py:293-330` |
| Mechanism 3 — warm-up skip | `environment.py:458-468` → `features.py:128-158` |
| Mechanism 4 — context tail cap at inference | `paper_trade.py` `context_bars` |
| The environment treats the frame it is given as exactly one episode | `data.py:1263-1266` ("its `reset(options=...)` protocol is currently unused") |
| The docstring promises what is unreachable | `data.py:1131-1135` (`.. todo::`), `data_window.py:43-45` |

**Directness:** structural. Without it, "does this data change help?" is answered on a single
trailing window, which is why CAND-3 (721 bars) and CAND-5 (n=3, no dispersion check) compound: a
model is trained on ~1 month, validated on a few weeks of the same month, and tabulated across 3
seeds with no spread test.

**Sourcing difficulty:** none, but it depends on CAND-3 landing first — walk-forward needs bars.

---

## 4. What the prior pass missed

1. **The `microstructure` group yields 0 columns and is enabled anyway** (CAND-1). The prior pass
   discussed `/public/Depth` as a *new source* but did not notice that the consumer already exists
   and is unreachable on column names nothing emits — i.e. the same shape as G2-R1, which landed.
2. **`engine.py:72` fetches a 10-level order book 1440×/day that no `tick()` reads** — the
   `tick()`-side half of CAND-1, which the prior pass scoped only to the RL leg.
3. **The funding channel's *non-overlap* case is deliberately not a refusal** (`data.py:489-491`,
   `data.py:662-671`). G1 landed the missing/empty refusals; the third state — *present but
   covering ~0.5% of the window* — is still silent. This is the single biggest blind spot in the
   seam as shipped.
4. **`signal_observed` is written but read by nothing** — `assess_cell` has no coverage reason
   code, so an inert channel is invisible in matrix reports.
5. **The observation width is a function of runtime file contents, not config** — measured 52 bare
   / 60 with the funding file / 65 at the full allow-list (`features.py:606-610` is
   presence-gated). The same `configs/default.yaml` yields different `n_features` on different days.
   `check_feature_width` (`features.py:262`) is what stops this silently, but it errors rather than
   reporting, so the operator sees a hard failure instead of "your channel produced 2 of 14 columns".
6. **`train.py`/`backtest.py` never pass `since`/`until` into the read**, so `data_window` bounds
   can only clip a trailing fetch. This is the mechanism behind CAND-3 and was not identified.
7. **Fear & Greed 2018+ history is already being downloaded on every social pull and discarded**
   (`kraken-social-signals/.../pipeline.py:123` + `:130-144`).
8. **Exactly one timer exists, it is funding-only, it is absent from `nix/module.nix` (zero timers),
   and it is not installed on this host.** Two of three configured channels have no producer at all.
9. **`eval_split: 0.7` in the shipped config is inert by construction** — a config key that nothing
   reads, in effect.
10. **`models/` is empty.** There is no trained artifact, so no empirical baseline exists for any of
    the above; every width and coverage number here is computed from the code path, not read off a
    shipped model.
11. **The seeder is cross-venue** (Binance \*USDT spot, `kraken-deep-history/.../export.py:45-48`) —
    a data-quality caveat attached to the cheapest route out of CAND-3.
12. **`/0/public/Trades` is paginable** (`kraken_api/client.py:128-132`), which the prior pass's
    G2-R2 trade-off implicitly missed by treating the microstructure source as snapshot-only.

---

## 5. Suggested reading order for the researchers

1. **CAND-1** — cheapest structural win, and the only candidate whose consumer code already exists.
   Answer: what is on `/0/public/Trades`, and how much history, and what RL columns does it buy?
2. **CAND-2** — highest information-per-effort. Answer: exactly what history is recoverable per
   channel, and what does each contribute to the observation once recovered?
3. **CAND-3** — the one that changes every other number. Answer: store build cost, and the
   cross-venue basis question.
4. **CAND-4 / CAND-5 / CAND-6 / CAND-7** — no new data; land or defer cheaply, but CAND-5 gates how
   any of the above is *evaluated*.

---

*End of audit. Nothing was written, committed or deleted outside this file.*