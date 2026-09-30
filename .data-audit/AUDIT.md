# DATA PIPELINE AUDIT — kraken-trading-bot (FRESH, 2026-09-30 pass 2)

Read-only auditor pass. Written against the **code on disk right now** — `master`
at `17899e1`, working tree clean apart from this artifact. This document
replaces the earlier 2026-09-30 AUDIT.md in full; the prior artifacts
(DECISION / RESEARCH / PLAN / VALIDATION) were read for context and their claims
were re-verified against current code, not copied.

## Verification performed this pass (facts, not assertions)

| Check | Command | Result |
|---|---|---|
| Test suite | `nix develop --command bash -c "pytest tests/ -q"` | **106 passed**, 20 warnings, 21.76 s — **0 failed** |
| Prior pass's red test | `tests/test_rl_export.py::test_build_export_frame_normalized_block_is_z_scored` | **GREEN** (prior AUDIT §3 said FAIL) |
| Feature width | `FeaturePipeline().n_features()` | **49** baseline / **52** with 3 signals / **58** with all 9 |
| Microstructure group | `FeaturePipeline().compute(df)` on OHLCV + 9 signals | **0** of 49/58 columns come from `microstructure` |
| Signal mis-join | own-venv repro of `merge_extra_features` | BTC_USD JSONL merges onto ETH/USD frame, **no error** (§3 A1) |
| Duplicate-hour JSONL | own-venv repro | `ValueError: cannot reindex on an axis with duplicate labels` (§3 A2) |
| Unbounded ffill | own-venv repro | one record at h0 propagates unchanged to h1, h2 (§3 A3) |
| `bid`/`ask` activation | `FeaturePipeline().compute(df + bid/ask/bid_vol/ask_vol)` | `spread` + `order_book_imbalance` **do** appear (§4 B2) |
| `models/` | `ls models/` | `.gitkeep` only — **no trained model, no provenance** |
| Store root | `ls ~/Projects/kraken-market-data/store` | **does not exist on this host** |
| Signal JSONLs in repo | `find . -name '*.jsonl'` | **none**; no `signals/` dir |

---

## 1. PIPELINE MAP, end to end (current code)

Nine entry points touch data. **A** is the classical strategy loop; **B–E** are
the RL family; **F** is the export stage; **G** is the model registry; **H** is
latent/unused surface.

```
              kraken-python  (kraken_api/manager.py)  — REST https://api.kraken.com/0/public/*
              transport.py:108,113  min_interval default 0.0  ->  _throttle() no-op (transport.py:180-186)
              transport.py:210-219,249-250  every RequestException / RateLimitError RAISES, never retried
              NO cache anywhere in this repo (grep lru_cache|cache|ttl over kraken_trading_bot/ -> 0 hits)
   │
   ├─ A. ENGINE LOOP  engine.run()            interval = 60 s   (engine.py:151-172)
   │     fetch_market_data() per pair, 3 UNPAGED calls, ZERO retry  (engine.py:46-76)
   │        manager.ticker(pair)                          -> engine.py:59
   │        manager.ohlc(pair, interval=60)               -> engine.py:65   (no `since` => newest ~720 bars ≈ 30 d @1h)
   │              ...then candles[-100:] DISCARDS ~620     -> engine.py:66
   │        manager.order_book(pair, count=10)            -> engine.py:72   top-10 depth
   │     run_iteration() passes {ticker, candles[-100:], order_book} to strategy.tick()   -> engine.py:136
   │        sma.py:68-69 reads ONLY candles + ticker;  order_book is never read  (sma.py:57-163)
   │     *** the engine NEVER touches the store, never merges a signal, never sees a feature  ***
   │
   ├─ B. TRAIN   train_ticker()                        train.py:128-242
   │     build_train_config()  (configs/default.yaml)      train.py:92-125 / cli.py:449-478
   │     read_ohlc_dataframe(pages=6, market_data_store=cfg) train.py:185-194
   │     prepare_episode(df, features, episode_bars)        train.py:202-207 -> data.py:505-567  (SLICE-then-FIT)
   │     TradingEnvironment(...)                             train.py:209-219
   │     agent.train(total_timesteps) -> model.zip            train.py:221-234 -> agent.py:72-118
   │     features.save_normalization(...) -> normalization.npz train.py:237-238
   │     register_model(ticker, name, cfg) -> config.yaml    train.py:240 -> registry.py:121-152
   │     *** cfg NEVER receives pages / episode_bars / seed / total_timesteps  ***
   │
   ├─ C. BACKTEST  backtest_model()                    backtest.py:84-262
   │     data=None -> read_ohlc_dataframe(pages=6, SAME window)  backtest.py:129-147
   │     scan_model() TWICE (backtest.py:134 and :152)  — the second overwrites the first
   │     pipeline rebuilt ONLY if normalization.npz exists     backtest.py:154-161
   │     deterministic replay, metrics                       backtest.py:191-262
   │     *** IN-SAMPLE BY CONSTRUCTION: no since/until, no holdout, window never recorded  ***
   │
   ├─ D. PAPER TRADE  PaperTrader.step()                paper_trade.py:343-380
   │     _fetch_data(): read_ohlc_dataframe(pages=2) EVERY 60 s   paper_trade.py:280-298
   │        -> 2 REST calls + 3 JSONL re-parses (merge_extra_features is called 3× : data.py:457-459)
   │        -> store branch re-pages AND re-upserts AND reads the WHOLE store (data.py:437-460)
   │     _build_observation(): compute().ffill().fillna(0) then stats.normalize()  paper_trade.py:300-323
   │        -> runs on the FULL frame unless context_bars set (default None, paper_trade.py:133)
   │     agent.predict(deterministic=True) -> PaperSignal -> manager.buy/sell   paper_trade.py:365-370
   │
   ├─ E. ENVIRONMENT  TradingEnvironment               environment.py:113-537
   │     __init__:180-197  fit stats if absent -> compute -> _raw_feature_array()
   │     _raw_feature_array()  environment.py:445-464
   │            features = compute(df).ffill().fillna(0.0)
   │            features = stats.normalize(features)      <-- the z-score IS applied (prior pass landed this)
   │            -> float32 (n_bars, 49)
   │     _first_valid_index()  environment.py:440-443  first row with NO NaN in any column (= 24 @ 60 m)
   │     _observe()  environment.py:367-375   row = _feature_matrix[step]  -> PPO(MlpPolicy)  agent.py:179
   │     reset(options=...)  environment.py:211-224  *** DECLARED, DOCUMENTED AS UNUSED, NEVER CALLED ***
   │
   ├─ F. EXPORT  build_export_frame()                  export.py:124-224
   │     composes train's stages, stops one step short of the env
   │     computed.ffill().fillna(0) written as the `features` block   export.py:189-196
   │     opt-in `z_` block = features.transform() (the observation)   export.py:201-207
   │     `warmup` bool from _warmup_mask()                            export.py:108-121,196
   │     CLI: cli.py:267-313 (parser), cli.py:597-643 (dispatch)
   │
   ├─ G. REGISTRY  models/{TICKER_ID}/{model_name}/{model.zip,normalization.npz,config.yaml}
   │       register_model registry.py:121-152 | scan_model registry.py:186-226 | is_trained registry.py:80-82
   │       EMPTY on disk this pass (.gitkeep only)
   │
   └─ H. LATENT / DORMANT  (exists, feeds nothing)
          kraken-python uncalled by this repo: recent_trades() manager.py:192, spread() manager.py:200,
             assets() :212, asset_pairs() :221, known_pairs() :131, server_time() :127
          features.py:389-402 _add_microstructure_features -> spread / order_book_imbalance: 0 columns emitted
             because no input column ever supplies spread / bid / ask / bid_vol / ask_vol
          features.py:249-262 fit_transform()  -> 0 call sites in the repo
          environment.py:211-224 reset(options=...)  -> 0 call sites
```

### EXACTLY where each input enters the RL observation

Two — and only two — doors:

1. **`_SIGNAL_COLUMNS` allow-list.** `features.py:35-45` defines the 9 names;
   `features.py:404-417` (`_add_signals_features`) copies each one that is
   present on the frame into the feature matrix; `features.py:25` puts
   `signals` in the default `_FEATURE_GROUPS`; `default.yaml:48` keeps it on.
   Columns are added to the frame by `merge_extra_features` (`data.py:79-176`),
   called three times per read (`data.py:323-325` live path, `data.py:457-459`
   store path). `data.py:147,156` keep **only** `_SIGNAL_COLUMNS`; every other
   field in a signal record is silently dropped.
2. **The microstructure branch.** `features.py:389-402` will emit `spread` if
   the frame has `spread`, or `bid`+`ask`; and `order_book_imbalance` if it has
   `bid_vol`+`ask_vol`. **No producer of those columns exists in the pipeline.**

Everything else that reaches the observation is computed from raw OHLCV
(`price`/`technical`/`volume` groups, `features.py:341-387`).

### EXACTLY where each input enters a strategy `tick()`

`engine.py:136` passes `{ticker, candles[-100:], order_book}`. `sma.py:68-69`
reads `candles` and `ticker` only. `order_book` is fetched every 60 s at
`engine.py:72` and **discarded**. No exogenous column, no feature, and no store
read ever reaches `tick()` — the classical loop and the RL loop are two
completely disjoint data worlds sharing only `KrakenManager`.

---

## 2. DATA-INVENTORY TABLE

| # | Input | Source / endpoint | Cadence today | Storage | Coverage / depth | Consumer path (`file:line`) |
|---|---|---|---|---|---|---|
| 1 | Ticker | Kraken `/0/public/Ticker` (keyless) | engine tick, 60 s | none | instant | `engine.py:59` → `engine.py:136` → `sma.py:69,140,154` |
| 2 | OHLC 60 m, 1 unpaged call | Kraken `/0/public/OHLC` (keyless) | engine tick, 60 s | none | ≤720 bars ≈ 30 d; **620 discarded** | `engine.py:65-66` → `sma.py:68` |
| 3 | Order book, depth 10 | Kraken `/0/public/Depth` (keyless) | engine tick, 60 s | none | instant, **discarded** | `engine.py:72` → `engine.py:136` → unread |
| 4 | OHLC paged, `pages=6` | Kraken `/0/public/OHLC` (keyless) | train / backtest | optional store upsert | ≤4320 bars ≈ 180 d @1h | `data.py:218-259` `_page_candles`; `train.py:185`, `backtest.py:138`, `export.py:166` |
| 5 | OHLC paged, `pages=2` | Kraken `/0/public/OHLC` (keyless) | **paper tick, 60 s** | optional store upsert | ≤1440 bars | `data.py:218-259`; `paper_trade.py:289` (`_FETCH_PAGES=2`, `paper_trade.py:56`) |
| 6 | Local parquet store | sibling `kraken-market-data` | systemd `*:*:30` (the ONLY timer in the family) | `{PAIR_ID}/{interval}/{YYYY-MM}.parquet` + `_meta.json` cursor | unbounded (forward-accumulating) | `data.py:426-460`; key `default.yaml:97` = **null**; store root **absent on this host** |
| 7 | Deep OHLCV seed | Binance public archive `data.binance.vision` (keyless) via `kraken-deep-history` | **manual one-shot `seed`** | writes the same store root | 2018→, ETH/BTC/SOL/XRP only | `kraken_deep_history/client.py:51,144-162`; `utils.py:23-28`; `configs/deep-history.example.yaml:71`; **never run** |
| 8 | News sentiment | GNews search/topic (key w/ free tier) via `ticker-news-signals` | **manual `cli.py pull`** (1 h lookback) | append-only JSONL | 1 h per pull, no history | record `sentiment_score`,`article_count`,`novelty_flag` (`ticker_news_signals/models.py:97-104`); key `default.yaml:57` = **null**; merged `data.py:323` |
| 9 | Funding / basis / OI | Kraken Futures `derivatives/api/v3/tickers` (keyless) via `kraken-funding-rates` | **manual `cli.py pull`** | JSONL | instant snapshot per pull | record emits **11** fields (`kraken_funding_rates/models.py:53-66`); 3 forwarded, **8 dropped**; key `default.yaml:65` = **null**; merged `data.py:324` |
| 10 | Social + Fear&Greed | StockTwits v2 (optional token, keyless otherwise) + `api.alternative.me/fng` (keyless) via `kraken-social-signals` | **manual `cli.py pull`** | JSONL | StockTwits stream = recent; F&G = daily | record `stt_mention_count`,`stt_tilt`,`fng_index` (`kraken_social_signals/models.py:34-40`); key `default.yaml:73` = **null**; merged `data.py:325` |
| 11 | Per-ticker z-stats | computed, not fetched | at fit | `models/{T}/{N}/normalization.npz` | 49 cols | `features.py:64-106`; saved `train.py:238`; loaded `backtest.py:161`, `paper_trade.py:188`; **applied** `environment.py:462-463`, `paper_trade.py:320-321` |
| 12 | Model policy | PPO / stable-baselines3 | per train | `models/{T}/{N}/model.zip` (gitignored `.gitignore:38`) | — | `agent.py:106-110`; **no model exists on disk** |
| 13 | Training config provenance | this repo | per train | `models/{T}/{N}/config.yaml` (tracked) | — | `registry.py:146-150`; **omits pages/episode_bars/seed/timesteps** |
| 14 | `(latent)` recent_trades / spread | Kraken `/0/public/Trades`,`/Spread` (keyless) | never | — | — | `kraken_api/manager.py:192,200` — **0 call sites in this repo** |
| 15 | `(latent)` bid/ask/funding extras | already inside JSONL #9 | every manual pull | JSONL | — | dropped by `data.py:147,156` |
| 16 | `(dormant)` microstructure features | computed only | — | — | 0 of 49 features | `features.py:389-402` |

---

## 3. GAP ENUMERATION (evidence-first, no category pre-filter)

### A. IMPROVE-EXISTING — the merge seam is the only door to non-OHLC data and it is unsound

**A1. `merge_extra_features` never filters by ticker — a BTC signal file silently poisons an ETH frame.**
Evidence: `data.py:134-166`. The function builds `signal_df` from the records,
requires only a `timestamp` column (`data.py:135-137`), floors the index
(`data.py:144`), and reindexes onto the OHLC index (`data.py:161`). The `ticker`
field is present in **every** record shape — `ticker_news_signals/models.py:97-104`,
`kraken_social_signals/models.py:34-40`, `kraken_funding_rates/models.py:53-66` —
and is never read. Reproduced this pass: an ETH/USD 3-hour frame merged with a
`BTC_USD` signal file yields `sentiment_score = -0.9/-0.8/-0.7` on ETH bars,
**no error, no log**. All eight merge tests (`tests/test_rl_data_store.py:211-374`)
write records with **no `ticker` field at all**, so the join key is untested.
Directness: **high** — it corrupts 9 of 49+ observation columns.
Sourcing cost: **zero** (pure code). Risk: **low**.

**A2. Two records at the same floored hour crash the merge — and the sibling's own documented cadence produces exactly that.**
Evidence: `data.py:161` `signal_df.reindex(ohlc_index)` on a non-unique index.
Reproduced this pass: (i) a two-ticker file and (ii) a single ticker with two
records in one hour both raise `ValueError: cannot reindex on an axis with
duplicate labels`. The documented usage is an hourly cron that **appends**:
`ticker-news-signals/INTEGRATION.md:114-120`
`0 * * * * ... python cli.py pull --ticker ETH/USD --output signals/eth_usd.jsonl >> signals/eth_usd.jsonl`
and `fetch_signals(lookback_hours=1)` (`ticker_news_signals/pipeline.py:96`)
re-emits the current hour on every run. A second pull inside the same hour
duplicates the floored timestamp and kills the merge. Directness: **high** —
this is a hard failure of all three signal seams. Sourcing cost: **zero**.
Risk: **low**.

**A3. Unbounded forward-fill and no freshness signal — a stale exogenous reading is observationally identical to a fresh one.**
Evidence: `data.py:165-166` `df[col] = merged[col].ffill().fillna(0.0).values`
— `ffill()` is unbounded and there is **no age/asof/staleness column anywhere**
(grep `stale|age_hours|_age|asof|freshness|last_seen` over `kraken_trading_bot/`
returns one hit, an unrelated error string at `paper_trade.py:314`).
Reproduced this pass: one record at h0 propagates unchanged to h1 and h2.
Funding settles ~8-hourly, news/social are pulled by hand, so gaps of hours to
days are the normal case; the agent cannot tell a 3-day-old `funding_rate` from
a current one. `novelty_flag` has the same defect — it is a property of *pull
time*, not of the bar (`ticker_news_signals/pipeline.py:36,88-92`), so a
forward-filled `novelty_flag=True` says "brand new information" for hours.
Directness: **high**. Sourcing cost: **zero** (~1 column). Risk: **low**.

**A4. Encoding collisions: absence and a genuine extreme read the same number.**
Evidence: `kraken_social_signals/pipeline.py:38` `_FNG_MISSING = 0`, applied at
`:136` `fng_index = int(fng_by_day.get(hour.date(), _FNG_MISSING))` — 0 is also
the low end of the real 0-100 scale ("extreme fear").
`kraken_social_signals/pipeline.py:52-62` `_tilt` returns `0.0` when no message
carries a Bullish/Bearish tag, and `0.0` is also "perfectly balanced". The bot's
own `fillna(0.0)` (`data.py:166`) then makes *no record at all* identical to
*neutral*. `ticker_news_signals/models.py:100-102` similarly defaults
`sentiment_score=0.0` / `article_count=0`. Directness: **medium** (2-3 of 9
columns). Sourcing cost: **zero** (encoding choices). Risk: **low**.

**A5. Single-point-of-truth drift: the observation-defining defaults are written out 4×, not read from the canonical constant.**
Evidence: the feature-group list exists canonically as `_FEATURE_GROUPS`
(`features.py:25`) and is nevertheless re-spelled in
`train.py:198-199`, `backtest.py:158-159`, `paper_trade.py:184-185` and, as a
*fifth* spelling, `export.py:73-79` `_DEFAULT_FEATURE_GROUPS`. The
`feature_windows` default `[1, 4, 24]` is re-spelled in `train.py:197`,
`backtest.py:157`, `paper_trade.py:183`, `export.py:178`. The `pages = 6`
default appears 5× (`data.py:265`, `data.py:333`, `train.py:133`, `backtest.py:91`,
`export.py:128`) plus 3 CLI defaults (`cli.py:156,231,290`). Any divergence
changes observation width between train / backtest / paper / export, and the
only detector is the runtime `ValueError` at `paper_trade.py:329-337`.
Directness: **high** (a silent width change invalidates `model.zip`).
Sourcing cost: **zero**. Risk: **low**.
*Correction to the prior pass:* `_SIGNAL_COLUMNS` is **no longer duplicated** —
`data.py:37-41` imports it from `features.py:35-45`. Prior AUDIT §5 and PLAN
§4.1.2 are stale on that point.

**A6. No cache and no retry on any fetch; the transport's throttle is a no-op by default.**
Evidence: grep `lru_cache|cache|memo|_CACHE|ttl` over `kraken_trading_bot/` →
0 hits. grep `retry|backoff` → 0 hits. `kraken_api/transport.py:108,113`
`min_interval: float = 0.0`; `:180-186` `_throttle()` returns immediately when
`min_interval <= 0`; `auth.py:76-79` reads `KRAKEN_MIN_INTERVAL` and falls back
to `0.0`; `transport.py:208-219` re-raises any `RequestException` and
`:249-250` raises `RateLimitError` — no retry, no backoff, no jitter anywhere.
The engine's 3 un-paged calls per pair per 60 s (`engine.py:59,65,72`) are
unguarded, and each failure is only logged (`engine.py:60-61,67-68,73-74`) and
retried on the next tick. The paper trader re-fetches 2 pages **and re-parses 3
append-only JSONLs** (`data.py:119-131`, called 3× at `data.py:457-459`) **and
recomputes the whole feature frame** (`paper_trade.py:318`) every 60 s; with
`context_bars=None` (`paper_trade.py:133`) that is O(store size) per tick.
Directness: **medium** (reliability, not signal). Sourcing cost: **zero** —
and it is the headroom every new source needs. Risk: **low** in-repo;
`transport.py` is a sibling-repo change.

**A7. The store branch re-fetches, re-upserts and whole-store-reads on every single call; the "append + tail" optimisation is a documented to-do, not code.**
Evidence: `data.py:437` `_page_candles(...)` is unconditional;
`data.py:439` `store.upsert(...)` re-writes the same 1440 bars into parquet
every 60 s; `data.py:454` `store.read(pair, interval, since=since, until=until)`
with `since`/`until` defaulting to `None` returns the **whole store** (see
`data.py:414-425` "since/until default to None so `store.read` returns the
whole store"). `data.py:396-400` carries the open todo verbatim: *"let backtest
skip the re-fetch and paper trade collapse to append + tail read"*.
`kraken-market-data/INTEGRATION.md:55-57` nevertheless advertises the collapse
as if it existed. Consequence on a deep store: a 7-year 1 h store is ~61 k
rows, and `paper_trade._build_observation` (`paper_trade.py:316-318`) computes
49 features over all of it once a minute. Directness: **medium**.
Sourcing cost: **zero**. Risk: **low**.

**A8. `since`/`until` are plumbed, store-tested, and called by nobody — so there is no train/eval split and backtest is in-sample by construction.**
Evidence: `read_ohlc_dataframe` accepts and forwards both
(`data.py:335-336,454`) and the store path is covered
(`tests/test_rl_data_store.py:170 test_read_ohlc_dataframe_honors_since_until_window`),
but **no production caller passes them**: `train.py:185-194`,
`backtest.py:138-147`, `paper_trade.py:289-298`, `export.py:166-175` all omit
both. `TradingEnvironment.reset(options=...)` is the declared mechanism and is
explicitly documented as unused (`environment.py:217-222` "Unused; reserved for
future start-bar overrides"). `backtest_model` therefore re-fetches the same
`pages=6` window seconds after training, and the justfile says so in a comment
(`justfile:74-79` "the numbers are in-sample"). Directness: **high** — it
determines what every reported return/Sharpe/drawdown number means.
Sourcing cost: **zero** (the seam is already written and tested).
Risk: **low**.

**A9. The training run itself is not recorded, so `config.yaml` cannot describe the model it sits next to.**
Evidence: `register_model(ticker_id, model_name, cfg)` writes only `cfg`
(`registry.py:144-150`), and `cfg` is built by `build_train_config`
(`train.py:92-125`) from the YAML plus the CLI override dict
(`cli.py:455-465`, which carries only `ohlcv_interval_minutes`, `action_space`,
`initial_balance`, `fee_rate`, `slippage`). `pages` (`train.py:133`),
`episode_bars` (`train.py:134`), `seed` (`train.py:136`) and `total_timesteps`
(`train.py:135`) are named parameters that **never enter `cfg`**. Consequence:
`backtest`/`paper-trade` cannot know which window or episode length to replay,
and a `config.yaml` cannot be used to reproduce its own run. The prior pass
measured the cost: two fetches of the *same* 721-bar span disagreed by **16 %**
on `rsi_24`'s fitted std, and it concluded "a trained model's
`normalization.npz` is not reconstructible after the fact"
(VALIDATION §4). Directness: **high**. Sourcing cost: **zero**. Risk: **low**.

### B. MARKET MICROSTRUCTURE — a dormant feature group whose raw material is already being pulled

**B1. `microstructure` is enabled everywhere and contributes 0 of 49 features.**
Evidence: `features.py:25` includes `"microstructure"` in `_FEATURE_GROUPS`;
`default.yaml:48` keeps it in `feature_groups`; `features.py:293-294` calls
`_add_microstructure_features`. Verified this pass: 49 columns baseline,
58 with all 9 signals, and **0** of them are `spread` or
`order_book_imbalance`. The reason is at `features.py:389-402`: the branch needs
`spread` **or** `bid`+`ask` (and `bid_vol`+`ask_vol`), and **no producer of any
of those columns exists** — OHLCV rows are exactly
`time,open,high,low,close,vwap,volume,count` (`data.py:48`), and
`_SIGNAL_COLUMNS` (`features.py:35-45`) contains no bid/ask. This is
"effective-but-unapplied machinery": fully coded, fully enabled, zero output.

**B2. `bid` and `ask` already arrive in a sibling's JSONL and are thrown away at `data.py:147`.**
Evidence: `kraken_funding_rates/models.py:47-48,62-63` — `FundingSnapshot` carries
`bid` and `ask`, and `to_dict()` (`:53-66`) emits **11** fields:
`symbol, spot_pair, timestamp, funding_rate, funding_rate_prediction,
mark_price, index_price, basis, open_interest, bid, ask, vol24h`.
`merge_extra_features` keeps only `[c for c in _SIGNAL_COLUMNS if c in
signal_df.columns]` (`data.py:147`) and selects `signal_df[available_cols]`
(`data.py:156`). So **8 of 11 fields are silently discarded on every pull**,
including `bid`/`ask` — the exact pair `_add_microstructure_features` looks for.
Verified this pass: adding `bid`/`ask` to a frame makes `spread` appear
immediately. Directness: **high** (observation width 49→51 with **zero new API
calls** — the data is already on disk in a file the bot already reads).
Sourcing cost: **zero**. Risk: **low**.
The other discarded fields are forward-looking or scale-relevant:
`funding_rate_prediction`, `index_price`, `mark_price`, `vol24h`.

**B3. `order_book_imbalance` needs `bid_vol`/`ask_vol`, which nothing carries — but the endpoint is already being called and discarded.**
Evidence: `features.py:398-402` needs `bid_vol`+`ask_vol`; the funding JSONL
does not have them. `kraken_api/manager.py:185 order_book(pair, count)` is
keyless and **is** called — at `engine.py:72`, every 60 s — and the result is
handed to `strategy.tick()` (`engine.py:136`) where `sma.py:57-163` never reads
it. `kraken_api/manager.py:192 recent_trades(pair, since)` and
`:200 spread(pair, since)` exist, are keyless, and have **0 call sites** in
this repo. Directness: **medium** (needs a depth/trade recorder, i.e. real ops).
Sourcing cost: **keyless**, medium operational cost. Risk: **medium** (a
top-10 snapshot is not book history; a 10-level imbalance sampled at 60 s is a
weak estimator).

**B4. Cross-exchange: two venues already sit in one store.**
Evidence: `configs/deep-history.example.yaml:71` points `market_data_store` at
the same root the poller writes; `kraken-deep-history/client.py:51` pulls
`https://data.binance.vision` (keyless S3-style archive) for
`ETH/BTC/SOL/XRP USDT` (`kraken_deep_history/utils.py:23-28`) and writes it into
the Kraken-shaped store. So a Binance-vs-Kraken divergence vector is derivable
today from one store. Two caveats that are *evidence*, not speculation:
it is a **venue substitution** (Binance spot USDT bars labelled `ETH_USD`,
`utils.py:23-28`), so any cross-venue feature inherits a real basis; and
Binance klines have no `vwap` (`kraken_deep_history/client.py:1-3`), so the
seeded history is not column-identical to the live path. Directness:
**medium** (pair-joined, needs a code change to surface at all).
Sourcing cost: **zero** incremental — the seeder exists. Risk: **medium**
(venue basis, unverifiable divergence, seeder never run on this host).

### C. OPERATIONS — nothing is scheduled, and three configured sources have no producer

**C1. One timer exists in the whole family, and it is the wrong one.**
Evidence: `kraken-market-data` ships a NixOS module + systemd timer
`OnCalendar=*-*-* *:*:30` running `kraken-market-data update`
(`kraken-market-data/INTEGRATION.md:40-44`, `nix/checks.nix:54`,
`README.md:19,95,114`). `ticker-news-signals`, `kraken-funding-rates`,
`kraken-social-signals` and `kraken-deep-history` ship **no timer, no cron, no
daemon** — a grep for `systemd|timer|cron|schedule|while True` across all four
returns doc text only, and `ticker-news-signals/INTEGRATION.md:114-120` presents
a cron line as a suggestion the user wires themselves. Meanwhile
`kraken-market-data/INTEGRATION.md:40` explicitly names the gap it does *not*
cover: the signal projects. Directness: **medium-high** (it is what makes the
three completed seams live at all). Sourcing cost: **zero**.

**C2. The bot's flake cannot run three of its own configured data sources.**
Evidence: `flake.nix:4-8` lists only `nixpkgs`, `kraken-python`,
`kraken-market-data` as inputs; `flake.nix:71` puts only
`kraken-market-data.outPath` on `PYTHONPATH`; `flake.nix:52-64` installs no
dependency for the GNews / StockTwits / Futures clients (no `requests` for them
beyond the market-data closure, no sentiment backend). All three signal keys are
`null` in both shipped configs (`default.yaml:57,65,73`;
`deep-history.example.yaml:58-60`). No `*.jsonl` exists anywhere in the repo.
Directness: **medium**. Sourcing cost: **zero**. Risk: **low**.

**C3. `models/` is empty.** Only `.gitkeep`. Every `backtest`/`paper-trade`
today would fall back to `default.yaml` defaults, and there is no on-disk
`config.yaml` carrying a real provenance record. `.gitignore:38-39` gitignores
`model.zip` and `normalization.npz` but tracks `config.yaml`, so provenance
*could* be versioned once a model exists. Directness: indirect (blocks
reproducibility, not features). Sourcing cost: **zero** (a training run).

**C4. The engine loop has no failure isolation for the data world.** Every fetch
is individually try/except'd and logged (`engine.py:60-61,67-68,73-74`), so a
total outage degrades to `{"candles": []}` → `sma.py:72-76` "insufficient data"
→ silent `hold`, forever, with no counter and no alert. Not a data *source* gap;
an observability gap on the path that fetches.

### D. NEW-SOURCE DIRECTIONS WITH NO REPRESENTATION ANYWHERE

Enumerated for completeness. Each is verified absent from this repo, from
`_SIGNAL_COLUMNS`, from both configs, and from all six sibling projects.

**D1. On-chain / crypto-native.** Nothing. No exchange netflow, whale
movement, exchange wallet balance, stablecoin supply, network activity, gas, or
on-chain volume — not in `_SIGNAL_COLUMNS` (`features.py:35-45`), not in any
config, not in any sibling. Directness: **low-medium** (asset- rather than
pair-oriented, so it needs an asset→pair mapping layer that does not exist).
Sourcing cost: **mostly API-key or paid**, with scraped explorers as the
keyless fallback; free keyless tiers are sparse and shallow. Risk: **high**
(cost, licensing, and the pair-mapping indirection).

**D2. Macro / economic calendars.** Nothing. No FOMC/CPI/NFP/PCE event
timestamps, no surprise values, no cross-asset rates. Note explicitly:
`kraken-social-signals` carries alternative.me's Fear & Greed
(`kraken_social_signals/client.py:37,280`), which is a **sentiment** index on a
daily cadence — it is not a macro calendar and carries no event times. Directness:
**low** (macro events move this strategy on a weekly/daily horizon; the
observation is hourly). Sourcing cost: **keyless** (public economic-calendar
feeds exist) or paid for consensus-surprise values. Risk: **medium**.

**D3. Research / academic.** Nothing, and the one sibling that could plausibly
supply it (`/home/seanc/Projects/researcher-python`, which aggregates
OpenAIRE / Semantic Scholar / arXiv / Crossref) is **not** a bot data source and
is **not** in the command's registry table
(`.opencode/commands/audit-pipeline.md:78-86`). Directness: **very low** — no
plausible route from a paper to a per-(ticker, hour) numeric column.
Sourcing cost: keyless. Risk: **high** (directness).

**D4. Order-flow / trade tape.** `recent_trades()` and `spread()`
(`kraken_api/manager.py:192,200`) are keyless and uncalled; no recorder exists.
Listed separately from B3 because a trade tape is a *different* artefact from a
depth snapshot. Directness: **medium**. Sourcing cost: keyless, high ops.
Risk: **medium** (storage volume at useful cadence).

**D5. Social / sentiment.** **Already built** — `kraken-social-signals`
(StockTwits + F&G). Do not re-propose.
**D6. Text / news.** **Already built** — `ticker-news-signals` (GNews + VADER).
Do not re-propose.
**D7. Market-data depth / history.** **Already built** —
`kraken-market-data` (store + timer) and `kraken-deep-history` (Binance
seeder). The gap is *populating* them, not writing them.

---

## 4. RANKED CANDIDATES (≥3, spanning >1 category)

Ranked by directness into the RL observation / `tick()`, then sourcing cost,
then differentiation. Categories are stated explicitly; the list deliberately
spans five.

---

### CANDIDATE 1 — Make the three existing signal seams sound before anything is added to them
**Category: IMPROVE-EXISTING / data-quality**

- **Evidence:** A1 (`data.py:134-166`, ticker-blind join — reproduced), A2
  (`data.py:161`, duplicate-hour `ValueError` — reproduced, and produced by the
  sibling's own documented cron at `ticker-news-signals/INTEGRATION.md:114-120`),
  A3 (`data.py:165-166`, unbounded ffill, no age column — reproduced; grep for
  `stale|age|asof|freshness` over the package returns 1 unrelated hit),
  A4 (`kraken_social_signals/pipeline.py:38,62,136` + `data.py:166`).
- **Feed path: high.** These 9 columns are the *only* non-OHLC inputs that
  reach the observation (`features.py:404-417`), and today they can be wrong
  (A1) or absent-but-pretending-to-be-fresh (A3, A4), or the merge can hard-fail
  (A2). Every one of the four consumers re-parses and re-merges them
  (`data.py:323-325`, `data.py:457-459`), so the defect is repeated on every
  train, every backtest and every 60 s paper tick.
- **Sourcing cost: zero** — pure code plus an encoding decision.
- **Risk: low.** The only behavioural change is that a mis-configured or
  duplicated file now fails loudly instead of silently.
- **What building this would concretely change in this repo:** the 9 columns
  already declared in `default.yaml:48` would become trustworthy enough to
  schedule, and `kraken-trading_bot/rl/data.py:79-176` would gain the join key,
  the de-duplication and the freshness column that every future exogenous source
  then inherits for free.

---

### CANDIDATE 2 — Activate the dormant `microstructure` group from data already on disk
**Category: MARKET MICROSTRUCTURE (new feature, zero new fetching)**

- **Evidence:** B1 — `microstructure` is in `_FEATURE_GROUPS` (`features.py:25`),
  in `default.yaml:48`, and called at `features.py:293-294`, yet contributes
  **0 of 49** verified columns because `features.py:389-402` needs
  `spread`/`bid`/`ask`/`bid_vol`/`ask_vol` and nothing supplies them. B2 —
  `kraken_funding_rates/models.py:47-48,53-66` already emits `bid` and `ask` (plus
  `mark_price`, `index_price`, `funding_rate_prediction`, `vol24h`) in every
  pull, and `data.py:147,156` discards all eight. Verified this pass: `bid`+`ask`
  on a frame makes `spread` appear.
- **Feed path: high** — it is the observation vector, and the seam is the
  existing `_SIGNAL_COLUMNS` allow-list, so no new plumbing is needed for
  `spread`. It also is the only candidate that *widens* the observation today
  without a single new HTTP call.
- **Sourcing cost: zero** for `spread` (reuse the funding JSONL). Keyless +
  medium ops for `order_book_imbalance` (`bid_vol`/`ask_vol` are not in any
  sibling's output; `kraken_api/manager.py:185 order_book` is already being
  called and thrown away at `engine.py:72`).
- **Risk: medium** for the imbalance leg — a top-10 snapshot at 60 s is a weak
  estimator, and a book-history recorder is a real ops commitment. Low for the
  `bid`/`ask` leg.
- **What building this would concretely change in this repo:** observation width
  49 → 51 with zero new API traffic (and 49 → 58 once the three JSONLs are
  populated, of which 8 fields are already paid for), and
  `_add_microstructure_features` (`features.py:389-402`) stops being dead code.

---

### CANDIDATE 3 — Record the training window, then split it
**Category: IMPROVE-EXISTING / evaluation integrity + reproducibility**

- **Evidence:** A8 — `since`/`until` are accepted at `data.py:335-336,454` and
  store-tested at `tests/test_rl_data_store.py:170`, but **no caller passes
  them** (`train.py:185-194`, `backtest.py:138-147`, `paper_trade.py:289-298`,
  `export.py:166-175`); `reset(options=...)` is documented as unused
  (`environment.py:217-222`); `justfile:74-79` states the backtest is in-sample.
  A9 — `pages`/`episode_bars`/`seed`/`total_timesteps` (`train.py:133-136`,
  `cli.py:455-465`) never reach the `cfg` that `register_model` writes
  (`registry.py:144-150`). Prior pass measured the reproducibility cost
  (VALIDATION §4): 16 % fitted-std drift on `rsi_24` between two snapshots of the
  same 721-bar span.
- **Feed path: high** — it does not add a feature, it determines whether any
  feature's measured value is real. Every `backtest` number in the repo today
  is in-sample by construction, and A7 (whole-store read + re-upsert per call,
  `data.py:437-460`) makes the store path *worse*, not better: backtest would
  read a strictly larger window than training and z-score it with stats fitted
  on a smaller one.
- **Sourcing cost: zero** — the store seam and the `since`/`until` plumbing
  already exist and are already tested; this is call-site plumbing plus
  provenance fields.
- **Risk: low.** It is additive. The one thing to be careful about is fitting
  stats on the training slice only — `prepare_episode` already does
  slice-then-fit (`data.py:550-559`) and must keep doing so.
- **What building this would concretely change in this repo:**
  `models/{TICKER}/{NAME}/config.yaml` would describe its own run (window
  bounds, `pages`, `episode_bars`, `seed`, `total_timesteps`), `backtest` would
  gain a real holdout instead of re-fetching the training window, and
  `normalization.npz` would become reconstructible after the fact.

---

### CANDIDATE 4 — Schedule the three signal projects and put them in the flake
**Category: OPERATIONS**

- **Evidence:** C1 — only `kraken-market-data` has a timer
  (`kraken-market-data/INTEGRATION.md:40-44`); the other three have none, and
  `ticker-news-signals/INTEGRATION.md:114-120` offers a cron line as a manual
  suggestion. C2 — `flake.nix:4-8,71` lists only `kraken-python` +
  `kraken-market-data`, so `nix develop` cannot even run the other three CLIs;
  all three signal keys are `null` (`default.yaml:57,65,73`) and no `*.jsonl`
  exists in the repo. Depends on Candidate 1 (A2 otherwise makes the scheduled
  cadence crash the merge) and on Candidate 3 (A3 otherwise fills the frame with
  stale ffills that are indistinguishable from fresh ones).
- **Feed path: medium-high** — zero new columns, but it is what makes the nine
  existing columns populate at all.
- **Sourcing cost: zero.** **Risk: low.**

---

### CANDIDATE 5 — Retry/backoff/throttle plus a bar cache in the 60 s paper tick
**Category: RELIABILITY / IMPROVE-EXISTING**

- **Evidence:** A6 — no cache and no retry anywhere in the package;
  `kraken_api/transport.py:108,113,180-186,210-219,249-250` raises on every
  failure with `min_interval` defaulting to `0.0`; A7 — the paper tick re-fetches,
  re-upserts, whole-store-reads and full-recomputes every 60 s
  (`data.py:437-460`, `paper_trade.py:280-298,316-318`).
- **Feed path: medium** (reliability and cost, not signal content). It is the
  headroom Candidates 1-4 all consume, and a sibling fix in `transport.py`
  benefits every sibling project.
- **Sourcing cost: zero** in-repo; the transport half is a sibling-repo change.
  **Risk: low**; the `transport.py` half is out of this repo's boundary.

---

### CANDIDATE 6 — Order-book depth + trade-tape recorder
**Category: MARKET MICROSTRUCTURE (genuine new source)**

- **Evidence:** B3 — `order_book_imbalance` (`features.py:398-402`) is the only
  feature in the pipeline with no producer; `kraken_api/manager.py:185,192,200`
  are keyless and uncalled. B4 — cross-exchange is available today in the store
  but inherits a real venue basis (`kraken-deep-history/utils.py:23-28` maps
  USDT spot onto a USD pair; `client.py:1-3` notes Binance klines lack `vwap`).
- **Feed path: medium.** **Sourcing cost: keyless, high ops** (poller +
  consolidation + storage, and the store layout is month-sliced OHLCV, not a
  book). **Risk: medium-high** — a 10-level snapshot is not book history.

---

### CANDIDATE 7 — On-chain / crypto-native signals
**Category: ON-CHAIN / CRYPTO-NATIVE (new source)**

- **Evidence:** D1 — nothing anywhere: not in `_SIGNAL_COLUMNS`
  (`features.py:35-45`), not in `default.yaml`, not in any of the six siblings.
- **Feed path: low-medium** — asset-oriented feeds need an asset→pair mapping
  that does not exist in this repo, and `data.py:144`'s hour-floor join assumes a
  per-(ticker, hour) vector.
- **Sourcing cost: mostly API-key or paid**, scraped explorers as fallback.
  **Risk: high** — cost, terms of use, and the mapping indirection.

---

### CANDIDATE 8 — Macro / economic calendar
**Category: MACRO**

- **Evidence:** D2 — nothing. Explicitly *not* the existing Fear & Greed
  (`kraken_social_signals/client.py:37,280`), which is a daily sentiment index
  with no event times.
- **Feed path: low** — the observation is hourly and the strategy is intraday;
  macro events land on a weekly/daily horizon. **Sourcing cost: keyless** for
  event dates, paid for consensus surprises. **Risk: medium.**

---

### CANDIDATE 9 (lowest) — Research / academic as a data source
**Category: RESEARCH**

- **Evidence:** D3 — nothing, and `/home/seanc/Projects/researcher-python` is not
  a bot data source and is absent from the registry at
  `.opencode/commands/audit-pipeline.md:78-86`. No plausible route from a paper
  to a per-(ticker, hour) numeric column. **Feed path: very low. Risk: high.**

---

## 5. ALREADY DONE — do not re-pick (verified against current code)

| Item | Where it landed | Verified by |
|---|---|---|
| **The `normalization.npz` stack is applied to the observation** (prior pass's Candidate 1) | `environment.py:445-464` `_raw_feature_array` calls `stats.normalize(filled)`; `paper_trade.py:318-322` applies the identical transform; `features.py:207,241` fit/transform on the **ffilled** frame; `data.py:550-559` slices the episode **before** fitting | 106 tests green, incl. `test_rl_export.py::test_build_export_frame_normalized_block_is_z_scored`; `test_rl_environment.py:300,335`; `test_rl_paper_trade.py:267` |
| `export-data` CLI + staged CSV frame | `rl/export.py` (whole file), `cli.py:267-313`, `cli.py:597-643`, `justfile:74-83` | `tests/test_rl_export.py` (11 tests) |
| `market_data_store` seam (fetch → upsert → read) | `data.py:329-460`, `_resolve_store` `data.py:463-502`; key `default.yaml:97`; `configs/deep-history.example.yaml:71` | `tests/test_rl_data_store.py` (11 tests) |
| All three exogenous merge seams (news / funding / social) | `data.py:79-176`, called 3× at `data.py:323-325` and `data.py:457-459`; keys `default.yaml:57,65,73` | `tests/test_rl_data_store.py:211-374` (8 merge tests) |
| `_SIGNAL_COLUMNS` single source of truth | `features.py:35-45` defined once; `data.py:37-41` **imports** it | grep: no second definition. **The prior AUDIT §5 / PLAN §4.1.2 claim that it is duplicated is stale.** |
| Deep-history seeder + example config | `configs/deep-history.example.yaml`; `kraken-deep-history` `seed`/`verify` CLI | not run on this host |

## 6. ALREADY DEFERRED — do not re-pick as if new (with what changed since)

| Deferred item | Status this pass |
|---|---|
| Market-data depth / store seeding | Seam + example config shipped; **store root still absent on this host**, `market_data_store: null` everywhere, seeder never run. Candidate 3 above now covers the *plumbing* half; the *ops* half (running `seed`) is unchanged. |
| `kraken-python` retry/backoff (`transport.py:208-219,249-250`) | Unchanged: still no retry, `min_interval` still defaults to `0.0` (`transport.py:108,113`). Now Candidate 5. |
| Scheduler + staleness + forwarding `funding_rate_prediction` | Unchanged: no timers in the three signal repos; no age column. Split across Candidates 1 (staleness) and 4 (timers); `funding_rate_prediction` folds into Candidate 2's allow-list widening. |
| Microstructure / on-chain as *new projects* | Now Candidates 2, 6 and 7 — but re-scoped: Candidate 2 is **not** a new project, the data is already on disk. |
| A convergence A/B for the normalization fix | Still blocked on a store (VALIDATION §5) and now on Candidate 3. |

## 7. BOTTOM LINE FOR THE DECISION PHASE

Reading the code rather than assuming a category, the dominant finding is that
**this repo's data gap is not "we need another source" — it is that the three
sources already wired in cannot currently be trusted, and the one window the
model was trained on is not recorded.** Candidates 1 and 3 are both
IMPROVE-EXISTING, cost zero new sourcing, and are direct on the observation;
Candidate 1 is a hard prerequisite for Candidate 4 (the sibling's own documented
cadence crashes the merge) and a strong enabler for Candidate 2. Candidate 2 is
the only item that *widens* the observation today without a single new HTTP
call, because the columns are already sitting in a JSONL the bot already reads.
The genuinely-new-source directions (Candidates 6-9: book/trade recorder,
on-chain, macro, academic) all lose on directness-vs-cost and should be weighed
as a group against the two zero-cost in-repo items. **The Decision phase must
not treat NEW-DATA-SOURCE as the default outcome** — on current code evidence
the cheapest high-directness work is not a new project.

AUDIT COMPLETE
