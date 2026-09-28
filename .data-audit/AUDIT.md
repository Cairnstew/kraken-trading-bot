# DATA PIPELINE AUDIT — kraken-trading-bot

Phase 1 (read-only). Prior work acknowledged: `DECISION.md`, `RESEARCH.md`, `PLAN.md`
document the already-completed **ticker-news-signals** pass (GNews headlines + VADER →
per-(ticker, hour) `sentiment_score` / `article_count` / `novelty_flag` JSONL, merged via
`merge_extra_features` into the `signals` feature group). This audit treats that as done and
hunts for **new** gaps beyond it.

Files read: `engine.py`, `rl/{data,features,environment,train,backtest,paper_trade,registry,agent}.py`,
`cli.py`, `strategies/{base,sma}.py`, `configs/default.yaml`, `tests/*`, `flake.nix`, `justfile`,
`.env.example`, plus the `kraken-python` source at `../kraken-python` (`manager.py`, `client.py`,
`transport.py`, `auth.py`, `paper.py`, `websocket.py`).

---

## 1. PIPELINE MAP

There are four independent data paths; only Path A is the "trading loop", but the RL paths
dominate where data actually enters features. All four talk only to Kraken REST (public or
paper-wrapped) plus, optionally, one sibling JSONL file.

```
                         ┌──────────────────────────────────────────────────────────┐
                         │                     kraken-python                        │
                         │  KrakenManager (REST /0/public/*, plus paper transport)  │
                         │  transport.throttle  min_interval = 0.0 BY DEFAULT       │
                         └───────┬──────────────┬──────────────┬────────────────────┘
                                 │              │              │
        A. ENGINE LOOP (run)     │              │              │         B. RL TRAIN / C. BACKTEST
        ─────────────────────────│──────────────│──────────────│         ───────────────────────────
   engine.run_iteration          │              │              │         train_ticker() / backtest_model()
   every `interval`=60s          │              │              │           │
        │                        │              │              │           │ fetch_ohlc_dataframe(pages=6)
        ├── manager.ticker ──────┘              │              │           │  → ~4320 x 1h bars (~180 d)
        ├── manager.ohlc(60) ───────────────────┘              │           │  (paginate on `last` cursor)
        └── manager.order_book(count=10) ──────────────────────┘           │  + merge_extra_features(file)
   each 1 call/pair/iter — 3 calls/pair/iter, no throttle, no cache            v
        │                                                        FeaturePipeline.fit → normalization.npz
        v                                                          │   (per-ticker z-score stats)
   strategy.tick(dict: ticker, candles[-100:], order_book) ──────> │   prepare_episode → trailing window
   SMA uses ONLY `candles` + `ticker`. order_book UNUSED.          v
   Data discarded after each tick.                      TradingEnvironment(TradingEnv.DataFrame)
   State: prev SMAs only in StrategyState.                     per-step obs = feature_matrix[step]
                                                               ├── compute() build features
                                                               │     price|technical|volume|microstructure|signals
                                                               │     (microstructure yields NOTHING today: see Gap 4)
                                                               v
                                                               PPO(MlpPolicy)  ← obs width = n_features (52 w/ signals)
                                                               artifacts: model.zip + normalization.npz + config.yaml

        D. PAPER TRADE (live)
        ─────────────────────
   PaperTrader.step() every `interval`=60s
        ├── _fetch_data() → fetch_ohlc_dataframe(pages=2) → ~1440 x 1h bars (~60 d)
        │     (re-fetches + recomputes the FULL window EVERY tick; no incremental cache)
        ├── pipeline.compute(full df) → row[-1] → obs
        ├── agent.predict(obs)  →  PaperSignal
        └── manager.paper() (in-memory fills)

        EXOGENOUS FEATURE SEAM (prior pass, live)
        ─────────────────────────────────────────
   sibling ../ticker-news-signals  cli.py pull --ticker ETH/USD  (MANUAL/cron, not scheduled)
        → signals/eth_usd.jsonl  {ticker, timestamp, sentiment_score, article_count, novelty_flag}
        → rl/data.py:merge_extra_features()  hour-floor left-join, ffill, zero-fill neutral
        → rl/features.py:_add_signals_features()  → `signals` group → observation vector
   Not present anywhere in strategy tick() path (engine has no exogenous feed).
```

---

## 2. CURRENT DATA SOURCES CATALOG

| # | Resource (endpoint) | Cadence | Consumer | Persistence | Notes / evidence |
|---|---|---|---|---|---|
| 1 | Ticker (REST `Ticker`) | every engine loop iter (60 s) | SMA `tick()` (bid/ask for limit orders) | none | `engine.py:59`, consumed `sma.py:138-158` |
| 2 | OHLC 60-min, latest ~100 bars | every engine loop iter | SMA `tick()` closes | none | `engine.py:65` `candles[-100:]` |
| 3 | OrderBook depth, count=10 | every engine loop iter | fetched, passed to `tick()`, **unused** | none | `engine.py:72`; SMA ignores `order_book` |
| 4 | OHLC 60-min, up to `pages=6` (~720/pp → ~180 d) | per `train` / `backtest` invocation | `FeaturePipeline` + `TradingEnvironment` | none on disk — re-fetched each run | `rl/data.py:fetch_ohlc_dataframe`, default `pages=6` `rd/train.py:105`; `backtest.py:138` refetches when `data=None` |
| 5 | OHLC 60-min, `pages=2` (~60 d) | every paper-tick (60 s) | `PaperTrader._build_observation` (last row) | none — recomputed each tick | `rl/paper_trade.py:56`, `_FETCH_PAGES=2` |
| 6 | per-(ticker,hour) news signal JSONL | hourly, **manual** CLI pull | `merge_extra_features` → `signals` group | JSONL on disk (sibling project) | `configs/default.yaml:57` `extra_features_file`; no systemd/cron wired (`PLAN.md` deliberately deferred) |
| 7 | (latent, unused) `recent_trades`, `spread`, futures/funding | n/a | n/a — endpoints exist in kraken-python, never called | n/a | `kraken-python/manager.py:187,199` |

Network physics: default transport throttle is OFF. `kraken-python/auth.py:73-75`
`min_interval = float(_env(...) or 0.0)`; `transport.py:108` default `0.0`. The bot never sets
it. `KRAKEN_MIN_INTERVAL` documented in `.env.example` but unset by default.

Where data enters the RL feature space **exactly**: `TradingEnvironment.__init__`
(`environment.py:186`) calls `pipeline.compute(self.data)`; `compute()` (`features.py:239`)
builds feature groups; the `signals` group (`features.py:379`) passes merged columns through.
The per-step observation is `self._feature_matrix[idx]` (`environment.py:367`). Width today
≈ 52 when signals active (49 core + 3 signal), 49 without. Nothing else can enter the
observation vector without touching `compute()`/`merge_extra_features()`.

Where data enters a strategy `tick()` **exactly**: `engine.py:136` passes the dict built in
`fetch_market_data` (`ticker`, `candles[-100:]`, `order_book`). SMA reads `candles` + `ticker`
only.

---

## 3. RANKED CANDIDATE GAPS

Ranked by (a) directness into RL features or `tick()`, (b) sourcing cost (keyless → paid →
scraped), (c) differentiation from the completed news pass. Each entry: evidence → feed path →
sourcing difficulty. **Nothing is committed yet.** Categories spanned: data-quality, market
microstructure, social/sentiment, on-chain, macro.

### GAP 1 — No data persistence / cache; no backfill or replay.  *(data quality)*
- **Evidence:** every path hits the network on every run. `fetch_ohlc_dataframe` (`data.py:200`)
  has no disk layer; `backtest_model` refetches when `data=None` (`backtest.py:138`); paper
  trader re-fetches 60 days every 60 s (`paper_trade.py:284-290`). `since` cursor exists
  (`data.py:205,247`) but no CLI surface exposes it, so a specific historical window cannot be
  backtested. `TradingEnvironment.reset(options=...)` is **unused** (`data.py:274` docstring) —
  the whole data frame is one contiguous episode: no train/eval split, no walk-forward, single
  trailing window per run. And 100% of fetched data is garbage-collected after each tick/run.
- **Feed path:** direct — a cached parquet/csv of OHLC (and later extras) IS the DataFrame the
  environment consumes; backfill/replay enables controlled OOS backtests and deep history >
  180 d (Kraken doesn't serve unlimited history in one page; exchange-side depth is the real
  ceiling without a local store).
- **Sourcing cost:** very low — pure code, keyless (already-fetched data), no new API. This is
  the cheapest, highest-leverage fix and is a **prerequisite** that makes every other historical
  gap (news backfill, funding history, etc.) tractable.

### GAP 2 — Funding-rates / perpetual-basis signal (derivatives microstructure).  *(market microstructure)*
- **Evidence:** the `microstructure` feature group exists (`features.py:364`) but is computed only
  when spread/bid/ask cols are present — and **the OHLC fetcher never produces them**, so the
  group contributes zero features in the RL path. Meanwhile `recent_trades`/`spread` endpoints
  exist (`kraken-python/manager.py:187,199`) and are unused. There is zero funding-rate or
  perpetual-basis awareness anywhere (spot-only REST).
- **Feed path:** direct — funding rate / basis is a per-(ticker, hour) scalar; it slots into the
  exact `merge_extra_features` → `signals` seam already proven, or into a widened microstructure
  group feeding `compute()`.
- **Sourcing cost:** low-medium. Keyless public APIs (Binance fapi, Bybit, OKX; Kraken's own
  futures API). Aggregation to hourly is mild. Distinct signal from news.
- **Note/contrast with Gap 4:** unlike order-book snapshots, funding is a level scalar that
  doesn't need its own store — cacheless hourly is fine.

### GAP 3 — Social / search-trend signal (search volume, social mention velocity).  *(social/sentiment)*
- **Evidence:** the seam for exogenous per-(ticker,hour) vectors exists and is proven
  (`data.py:merge_extra_features` → `features.py:_add_signals_features`), but only one source
  feeds it. Social mention volume / search interest is a genuinely different modality from
  news-headline sentiment and is strongly leading for retail-heavy crypto.
- **Feed path:** direct — 1–2 extra columns through the same `signals` group (e.g. normalized
  search-interest, mention-count z-score). Zero feature-pipeline changes needed.
- **Sourcing cost:** medium. Google Trends scraping (pytrends) is keyless but ToS-grey and
  rate-limited; crypto-native aggregates (LunarCrush, Santiment, Socialgrep) are paid or limited
  free tiers. A keyless fallback (public dashboards/feeds per asset) is partial.

### GAP 4 — Order-book / trades microstructure history (deeper book, spread, trade flow).  *(market microstructure)*
- **Evidence:** the engine already fetches top-10 depth every loop (`engine.py:72`) and throws it
  away; the RL microstructure group is dead-on-arrival (see Gap 2 evidence). Neither the book
  depth nor `recent_trades`/`spread` data ever reaches a feature or a strategy.
- **Feed path:** direct once persisted — book imbalance/spread/trade-flow enter the
  `microstructure` group (`features.py:364-377`, cols already coded: `spread`, `bid`, `ask`,
  `bid_vol`, `ask_vol`), which today outputs nothing. Requires a snapshot recorder (ties to Gap
  1) because books are ephemeral.
- **Sourcing cost:** low data-wise (keyless, existing endpoint) but **medium-high operationally**:
  needs its own polling + storage + alignment component. Bundled with Gap 1 as a prerequisite.

### GAP 5 — On-chain / network-native metrics (exchange flows, whale transfers, gas, stablecoin supply).  *(on-chain)*
- **Evidence:** no on-chain source anywhere — the pipeline is entirely off-chain REST. The RL
  signal seam is crypto-native-ready and expects exactly this shape.
- **Feed path:** direct through `merge_extra_features` per asset-hour (e.g. net inflow to
  exchanges, large-transfer count, avg gas). Heavier assets need their own extraction.
- **Sourcing cost:** medium-high. Blockchair (keyless, crypto) and Etherscan (free-key tier) cover
  parts; whale-alert-style data is paid or scraped; free on-chain *history* for backfill is
  limited — makes Gap 1 (local store) more urgent if this is ever built.

### GAP 6 — Macro / economic calendar features (FOMC, CPI, scheduled events).  *(macro)*
- **Evidence:** zero macro awareness anywhere; the observation space is pure price/volume/signal.
  Crypto is macro-sensitive, so scheduled-event windows are plausibly informative for the agent.
- **Feed path:** indirect/harder — events aren't a natural hourly scalar; would need event-envelope
  encoding (hours-to-event, post-event impulse) merged onto bars. Not a drop-in `signals` column.
- **Sourcing cost:** low-medium (keyless RSS/ICS calendars), but **feature design cost** is the
  real blocker; lowest directness of the list.

---

## 4. NOTABLE SMALLER FINDINGS (not ranked)

- **News signal has no committed test coverage in this repo.** Zero hits for `merge_extra_features` /
  `extra_features` / `sentiment_score` / `novelty` in `tests/` (grep across `tests/*.py`), despite
  `PLAN.md` claiming a 52-feature integration check. The seam is green-field — unguarded against
  signal-schema drift from `ticker-news-signals`.
- **Model config provenance is thin.** Trained `models/*/config.yaml` carry only 3 keys
  (`ticker`, `model_name`, `action_space`) — feature_windows/feature_groups/reward settings are
  not recorded, so backtest/paper reconstruct the pipeline from coupled defaults and any drift
  between config and a future default.yaml silently changes inference.
- **No operational scheduler.** The hourly news pull is manual (`PLAN.md` deferred a systemd
  timer). Without one, `extra_features_file` is a stale snapshot, silently forward-filled.
- **Unthrottled by default** (`min_interval=0.0`), no retry/backoff anywhere in this repo; a
  transient `RateLimitError` drops the tick's data (engine logs and moves on).

---

## 5. BOTTOM LINE FOR THE DECISION PHASE

Top three NEW vectors, spanning **three** categories:
1. **Local data store + backfill/replay** (data quality) — unlocker, cheapest, prerequisite.
2. **Funding-rate / perps-basis signal** (microstructure) — keyless, per-hour scalar, direct seam.
3. **Social/search-trend signal** (social/sentiment) — new modality, direct seam, medium cost.

Runner-ups: order-book/trades microstructure recorder (needs Gap 1), on-chain metrics
(backfill-limited), macro calendar (feature-design-heavy). Each evidence-backed above; no
commitment made here.

AUDIT COMPLETE