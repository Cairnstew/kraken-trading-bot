# AUDIT-PIPELINE.md — RL data-pipeline audit (2026-10-08)

Auditor: `auditor-pipeline` (team `audit-pipeline-1008`). READ-ONLY pass.
Scope: the RL data path end to end — what is fetched, how it is stored, where
each input enters the feature space, which config keys drive it, and the
data-quality gaps. Prior artifact: `.data-audit/AUDIT.md` (2026-10-03).

Evidence tags: `[C]` read in code this pass, `[M]` measured this pass on this
host, `[S]` stated in a prior artifact and not re-measured. Line numbers are
HEAD `1c63007`.

> **Headline:** the 2026-10-03 audit's structural findings have largely been
> *built* since — the store is seeded, all three signal channels are populated,
> an order-book recorder is running, and four hourly producers now have timers.
> The remaining high-value gaps are no longer "nothing fetches this"; they are
> **fetched-but-not-consumed** and **consumed-but-degenerate**:
> the recorded order-book depth has **no RL reader at all**, and the
> `microstructure` feature group's only reachable column is empty for ~99% of
> funding history.

---

## 1. Pipeline map (end to end)

### 1.1 The RL path

```
config YAML ──> build_train_config                         train.py:120
    │
    ├─ cfg["ticker"], cfg["ohlcv_interval_minutes"]        train.py:221-222
    │
    ├─ resolve_data_window(cfg)                            train.py:232
    │     └─ DataWindow{since,until,eval_split}            data_window.py:104-143
    │        (shipped default: since=until=null -> unpinned, eval_split INERT)
    ├─ refresh_for_window(...)                             train.py:237 / data.py:1495
    │     └─ pinned + ends <= store tail -> {"refresh": False, "coverage": ...}
    │        else {"refresh": True}                        data.py:1554-1599
    │
    ├─ read_ohlc_dataframe(...)   ← the ONE read seam       data.py:1625
    │     │
    │     ├─ market_data_store is None (shipped default)    data.py:1769
    │     │     └─> fetch_ohlc_dataframe()                  data.py:1164
    │     │           └─> _page_candles()                   data.py:1120
    │     │                 └─> manager.ohlc(pair,interval,since=cursor)
    │     │                       └─> Kraken /0/public/OHLC  (~720-bar ceiling)
    │     │           └─> candles_to_dataframe()            data.py:1081
    │     │           └─> add_derived_ohlcv_features()      data.py:1017
    │     │                 +vwap_dev, +trade_count_zscore_20, +volume_per_trade
    │     │
    │     └─ store configured (deep-history.example.yaml)   data.py:1803
    │           ├─ refresh=True  -> _page_candles + store.upsert  data.py:1858-1880
    │           ├─ refresh=False -> ZERO API calls (pinned window) data.py:1881
    │           ├─ store.read(pair, interval, since=None, until=None)  data.py:1890
    │           │      (whole store; window clip happens later, callers omit since/until)
    │           ├─ _guard_refresh_coverage (bar-for-bar proof)  data.py:1895
    │           └─ add_derived_ohlcv_features()
    │
    ├─ three exogenous JSONL seams, merge order news->funding->social  data.py:1908-1918
    │     for key,file in _signal_channels(extra,funding,social)   data.py:226-243
    │       merge_extra_features()                          data.py:633
    │         (ticker filter, hour-floor dedupe, bounded ffill,
    │          +signal_observed +signal_age_hours; absence != zero)
    │
    ├─ training_frame(df, window)  (clip + eval_split; no-op if unpinned)  train.py:265
    ├─ prepare_episode(df, features, episode_bars)          data.py:2013
    │     slice trailing episode_bars FIRST, then FeaturePipeline.fit()  data.py:2061-2067
    │
    └─ TradingEnvironment(...)                              train.py:319 / environment.py:184
          self._features = pipeline.compute(data)           environment.py:247
          self._feature_matrix = _raw_feature_array()       environment.py:249 / :617
                compute -> ffill -> fillna(0) -> NormalizationStats.normalize -> float32
          observation_space = Box(shape = feature_matrix.shape[1])  environment.py:256

artifacts: models/{TICKER_ID}/{model_name}/                    registry.py:1-9
    model.zip            (PPO, RLAgent)
    normalization.npz    (ticker_id, feature_names, means, stds; pickle-free)
    config.yaml          (whole resolved config + n_features + n_bars + train_timesteps)
```

### 1.2 What is fetched, from where, at what cadence

| input | source | storage | cadence | reaches observation? |
|---|---|---|---|---|
| OHLCV (o/h/l/c/vwap/volume/count) | Kraken `/0/public/OHLC` (live) or seeded store | in-memory DataFrame; store = month-sliced parquet | per read | yes (price/technical/volume groups) |
| vwap_dev, trade_count_zscore_20, volume_per_trade | derived at read seam from `vwap`/`count` | in-memory | per read | yes (`signals` group) |
| news: sentiment_score, article_count, novelty_flag | sibling `ticker-news-signals` | `signals/eth_usd_news.jsonl` | hourly timer `:23` | yes |
| funding: funding_rate, basis, open_interest, funding_rate_prediction, vol24h, bid, ask | sibling `kraken-funding-rates` (+ `funding-backfill`) | `signals/eth_usd_funding.jsonl` | hourly timer `:17` | funding_rate yes; basis/OI/pred/vol24h/bid/ask only on 86/8914 live records |
| social: stt_mention_count, stt_tilt, fng_index | sibling `kraken-social-signals` | `signals/eth_usd_social.jsonl` | hourly timer `:29` | yes, but stt_tilt/fng_index degenerate (§P-C) |
| order-book depth (bids/asks/best_bid/best_ask/mid/spread) | Kraken `/0/public/Depth`, keyless | `signals/eth_usd_orderbook.jsonl` (append-only) | hourly timer `:41` | **NO — no RL reader** (§P-A) |
| store hole report | `tools/store_gap_scan.py` / `store_guard.py` | stdout | manual / `store-verify` | no (operational) |

Measured artifact state on this host `[M]`:

| artifact | state |
|---|---|
| `~/Projects/kraken-market-data/store/ETH_USD/60/` | **present**, 4.7 MB, **106 month files, 2018-01 .. 2026-10** |
| `signals/eth_usd_funding.jsonl` | 8,914 records, 2025-10-01T08:00Z .. 2026-10-07T23:00Z |
| `signals/eth_usd_news.jsonl` | 166 records |
| `signals/eth_usd_social.jsonl` | 337 records |
| `signals/eth_usd_orderbook.jsonl` | 103 records, 2026-10-03T19:37Z .. 2026-10-07T23:41Z; status `ok:false`, 1 hole |
| `models/` | **empty** (only `.gitkeep`) |

The store is **not** what `configs/default.yaml` reads — the shipped default
still sets `market_data_store: null` (`configs/default.yaml:288`); the store is
reached only through `configs/deep-history.example.yaml:127` or
`configs/deep-ab.base.yaml:62`.

### 1.3 Where each input enters the feature space

- `FeaturePipeline.compute` (`features.py:783`) dispatches five groups
  (`features.py:853-862`); `_FEATURE_GROUPS` at `features.py:25`.
- `_SIGNAL_COLUMNS` (`features.py:94-125`) is the single canonical allow-list
  shared by the merge seam (`data.py:786-790`) and the observation passthrough
  (`features.py:1063-1069`). Width is coupled to *which columns are present in
  the producer file*, not to their values.
- `feature_names` in `normalization.npz` is the width authority; `check_feature_width`
  (`features.py:483`) is non-self-referential.
- `TradingEnvironment._raw_feature_array` (`environment.py:617`) is the one place
  the z-scored observation is materialised; `paper_trade._build_observation`
  (`paper_trade.py:330-359`) re-derives the same row live.

### 1.4 Config keys and their readers

Shipped `configs/default.yaml` is fully live except `model_name` (§P-I).
Drivers per input: `ticker`, `ohlcv_interval_minutes`, `feature_windows`,
`feature_groups`, the three `*_features_file`, `signal_max_age_hours`,
`signal_require_ticker`, `market_data_store`, `market_data_store_venue`,
`data_window.{since,until,eval_split}`.

---

## 2. Ranked gaps

Ranked by **directness to the RL observation / `tick()`** then feasibility.
Nothing here is committed to; the architect chooses.

### 🥇 P-A — The order-book depth recorder has **no RL reader**: recorded data never reaches the observation, and `order_book_imbalance` is structurally unreachable

- **Evidence.** `features.py:1018-1026` is the *only* place `order_book_imbalance`
  is computed, gated on `{"bid_vol","ask_vol"}.issubset(df.columns)`. Neither
  `bid_vol`/`ask_vol` nor `order_book_imbalance` is in `_SIGNAL_COLUMNS`
  (`features.py:94-125`), so no merge can supply them; `data.py:786-790`'s
  presence-intersection would drop them anyway. `depth_recorder.py:45-51`
  states it outright: *"writes data and reads none of it back … no
  `_SIGNAL_COLUMNS` entry, no observation-width change."* The only consumers of
  the depth file are the recorder's own gap scanner (`cli.py:838`,
  `depth_recorder.py:558`) — operational, not features. `engine.py:72` still
  fetches a book that no strategy reads.
- **Measured.** `signals/eth_usd_orderbook.jsonl` holds **103 snapshots** `[M]`;
  each carries `bids`/`asks`/`best_bid`/`best_ask`/`mid`/`spread`/`depth`.
- **Why it is #1.** `feature_groups` ships with `"microstructure"` **enabled**
  (`configs/default.yaml:53`), so the pipeline reserves width for a
  microstructure reading it can never populate from depth. The data is already
  on disk and the feature column is already coded — only the merge seam and one
  `_SIGNAL_COLUMNS` entry are missing.
- **Directness:** highest — a new observation column feeding `_raw_feature_array`
  directly, with no new fetch. **Effort:** medium — the JSONL is a nested
  `bids`/`asks` array, not flat per-hour columns; its record carries `spread`
  which **collides** with the funding-derived `spread` (`features.py:1008-1017`);
  `depth_recorder.py` already reserves `realized_spread_bps` as the non-colliding
  name.

### 🥈 P-B — The `microstructure` group is effectively empty: `spread` is absent on 8,828 of 8,914 funding records

- **Evidence.** `[M]` on `signals/eth_usd_funding.jsonl`: `basis`,
  `open_interest`, `funding_rate_prediction`, `vol24h`, `bid`, `ask` are each
  **null on 8,828 / 8,914 records** (only the 86 live-appended records carry
  them). `configs/default.yaml:181-183` says so: the keyless backfill recovers
  **1 of 6** columns (`funding_rate`). `features.py:1008-1017` derives `spread`
  from `bid`/`ask`; with both zero-filled, `(0-0)/0` -> NaN -> observation
  0-fill. So the group enabled by default contributes **one column that is 0
  for ~99% of history**.
- **Directness:** high — a default-on group whose only reachable column is
  degenerate. **Effort:** small — either source a real bid/ask/basis series,
  or drop `spread` from the default feature set and consume the recorded
  order-book spread instead (ties to P-A).

### 🥉 P-C — Two of the nine signal columns are inert by construction

- **Evidence.** `configs/default.yaml:139-143`: `stt_tilt` is *"permanently 0.0
  on the live StockTwits v2 feed"* and `fng_index` is a *daily* value stamped
  onto each hour. A zero-variance column normalises to exactly 0.0, so both
  reach the observation as constant-zero dead weight (`_SIGNAL_COLUMNS` entries
  `stt_tilt`, `fng_index`, `features.py:102-103`).
- **Directness:** medium-high — two observation columns that can never carry
  information. **Effort:** small (drop them from `_SIGNAL_COLUMNS` / replace the
  `stt_tilt` source), but it changes observation width -> invalidates existing
  artifacts (none exist yet: `models/` is empty).

### 4️⃣ P-D — `signal_observed` overclaims: it is a per-channel OR, not per-column

- **Evidence.** `configs/default.yaml:184-191` documents it: `observed =
  filled.notna().any(axis=1)`, so once the funding backfill covers a window,
  `signal_observed == 1.0` on every bar *while 5 of 6 funding columns are 100%
  zero-fill with no reading behind them*. `data.py:833-849` is the merge that
  writes it. The config itself warns *"Do NOT gate any feature on
  `signal_observed`"* — but the column is still handed to the agent as if it
  meant "this bar has data".
- **Directness:** medium — an observation column that misinforms. **Effort:**
  medium (make it per-column, or drop it and keep `signal_age_hours`).

### 5️⃣ P-E — Silent-drop failure mode: features are computed on **bar counts, not wall-clock**, so a store hole reads as a 1-hour return

- **Evidence.** `configs/default.yaml:274-287`: measured **158 missing bars
  across 28 gaps, largest 39h**, including a 38-bar hole at the seed/live-append
  seam; *"across that hole `return_1` reports a 39-hour return as though it were
  1-hour."* `store_gap_scan.py` / `store_guard.py` detect and label it
  (`tests/test_store_gap_scan.py`), but nothing fixes it. With the store now
  seeded and deep (P-E host state), this path is live.
- **Directness:** high data-quality (corrupts `return_*`/`range_1` directly),
  but **detected not fixed**. **Effort:** large — time-aware feature windows over
  a reindexed bar grid (`DECISION.md §14`).

### 6️⃣ P-F — Unretried, unthrottled fetch path

- **Evidence.** `_page_candles` (`data.py:1145-1155`) has no `try/except`; a
  raised `RequestException`/`RateLimitError` aborts the whole read. The store
  leg then logs *"fetch returned no candles … reading whatever the store already
  has"* (`data.py:1874-1880`) — a silent shorten on `refresh=True`. Transport
  `min_interval` defaults to `0.0` and there is no retry/backoff `[S]`
  (`AUDIT.md` G3: `kraken_api/transport.py:108,186,250`); the sibling
  `kraken-funding-rates` retries 3x — this repo does not.
- **Directness:** medium — a network blip silently shortens the training frame.
  **Effort:** small.

### 7️⃣ P-G — `data_window.eval_split` is inert on the shipped default; the default run has no holdout

- **Evidence.** `configs/default.yaml:376-379` ships `since: null, until: null`
  with `eval_split: 0.7`; `data_window.py:25-35,127-137` makes `eval_split`
  inert while unpinned (`has_split` requires `is_pinned`). So the shipped
  default trains on the whole trailing live frame and reports no out-of-sample
  number — documented, but the default is the in-sample case.
- **Directness:** medium (validity precondition for any quoted return).
  **Effort:** small (pin a window in the default or a matrix base).

### 8️⃣ P-H — `TradingEnvironment.reset(options=...)` is unused -> no walk-forward; one episode = the whole frame

- **Evidence.** `environment.py:272-286` (*"options: Unused; reserved"*);
  `data.py:2022` repeats it. Length control is `episode_bars` only
  (`data.py:2058-2061`). Deep store + `since/until` make walk-forward possible
  but nothing drives it.
- **Directness:** medium. **Effort:** medium.

### 9️⃣ P-I — Dead config key: `model_name`

- **Evidence.** `configs/default.yaml:384` `model_name: "ppo_eth_01"`;
  `build_train_config` overwrites it (`train.py:144`) and `export` uses the
  inert `_EXPORT_MODEL_NAME` (`export.py:71`). Nothing reads the YAML value.
- **Effort:** trivial.

### 🔟 P-J — A second, divergent feature path exists: `_builtin_features`

- **Evidence.** `environment.py:645-673` builds an 8-feature set with different
  names (`return_5`, `rsi_14`, `price_to_sma_20`, `position_ratio`, …) when
  `feature_pipeline is None` (`environment.py:251-253`). Production always
  passes a pipeline (`train.py:319`, `paper_trade.py:203`); the path is
  test-only. It duplicates feature logic that can drift from
  `FeaturePipeline`.
- **Effort:** small.

### 1️⃣1️⃣ P-K — Doc/config drift (minor)

- `export.py:20-22` still says *"All three are `None` in `configs/default.yaml`"*
  — all three are now non-null (`configs/default.yaml:89,124,144`).
- `data.py:1762-1767` `.. todo::` still says *"honour since/until from config"*,
  though `refresh_for_window` (`data.py:1495`) now does the material part.
- **Effort:** trivial.

---

## 3. What I checked and found clean

- `add_derived_ohlcv_features` (`data.py:1017`) is genuinely read: `vwap`/`count`
  are no longer dropped (49 -> 55 widening).
- The three signal channels are all two-state and **non-null** in the shipped
  default, with loud `SignalFileNotFoundError` on a fresh clone
  (`configs/default.yaml:61-69`); `tests/test_gc_channel_activation.py` pins it.
- Non-finite guards are extensive and measured (`features.py:55-73,301,846-849`;
  `NonFiniteFeatureError` at `features.py:209`), including the `ewm`-skips-a-bad-bar
  hazard pinned in `tests/test_feature_nonfinite_guards.py`.
- `prepare_episode` slice-first-then-fit (`data.py:2058-2067`) prevents
  normalization leaking future bars.
- Venue provenance is honest-by-default (`unknown` + warning,
  `data.py:1816-1840`), no longer asserting a venue nothing established.
- `allow_short: true` is refused at training (`train.py:288-317`), not silently
  recorded.
- Producers/timers now exist for news (`:23`), funding (`:17`), social (`:29`),
  order-book (`:41`), plus a weekly depth checkpoint and an hourly
  signal-gap gate — closing the 2026-10-03 audit's "no timer" finding.

---

## 4. Method caveat

I did not run `nix develop` (read-only budget), so no observation-width or
`return_1` figure is re-measured here; widths and the store's gap arithmetic are
cited `[S]`/`[C]`. All file-level measurements (`[M]`) are shell-only
(line counts, JSON keys, null counts, parquet month listing).
