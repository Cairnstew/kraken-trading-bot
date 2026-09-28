# DECISION — Phase 3: one (gap, library) pair for this pass

Author: Architect, audit-pipeline team. Read-only phase — this document decides; it does
not scaffold. House-style mirror: `kraken-python` + the completed `ticker-news-signals` pass.

## 1. THE DECISION

- **Chosen gap: GAP 1 — Local market-data store / backfill / replay** (data quality).
- **Libraries: pandas + pyarrow** (parquet, month-sliced) with a `since`-cursor poller.
  duckdb stays an *optional* verification layer (it reads the same parquet in place) — not
  a v1 dependency. No new API, no new venue, no key, no ToS surface.
- **Project name: `kraken-market-data`.**
- **Path: `../kraken-market-data/` — a sibling git repo**, not nested in this repo and not
  a submodule (same placement as `ticker-news-signals`).

Name rationale: it is Kraken/crypto-specific (it accumulates the bot's own venue
consistent Kraken market for the *kraken-trading-bot* pipeline), so it belongs in the
`kraken-*` family — consistent with `kraken-python` and `ticker-news-signals`. A
source-oriented name (e.g. `ohlcv-store`) would understate that it is the bot's
single source of truth for its own market.

## 2. WHY THIS PAIR, AGAINST THE PIPELINE'S REAL SHAPE

The audit's own data assumption is **factually wrong**, and that single fact is
decisive. RESEARCH.md verified live (2026-09-28):

- `GET /0/public/OHLC?pair=XBTUSD&interval=60&since=0` → **721 rows** (most recent ~720
  bars, ~30 days at 1 h). `since=2017` returns the **identical** 721 rows. Kraken's own
  docs: *"Returns up to 720 of the most recent entries (older data cannot be retrieved,
  regardless of the value of `since`)."* Following the returned `last` cursor (the bot's
  `pages=6` loop, `rl/data.py:238-253`) returns only the tail again.
- Consequence: **`train_ticker(pages=6)` does NOT give ~180 d**. Every RL run today —
  train, backtest (refetches on `data=None`, `rl/backtest.py:138`), and paper trade
  (re-fetches `pages=2` every 60 s, `rl/paper_trade.py:284-290`) — sees **at most
  ~30 days of 1 h bars**, regardless of knob values.

Where the new feature must land, per ticker (`configs/default.yaml` → `train.py` →
`models/{TICKER_ID}/{model_name}/`):

- A feature-engineering step reads market data **exactly once**: `fetch_ohlc_dataframe`
  (`rl/data.py:200`) pages Kraken into a pandas DataFrame with UTC `DatetimeIndex` and
  columns `time, open, high, low, close, vwap, volume, count`, then
  `FeaturePipeline.compute()` (`rl/features.py:239`) builds the groups and
  `TradingEnvironment` slices the observation. Everything below it — `NormalizationStats`
  → `normalization.npz`, `PPO`, `model.zip`, per-model `config.yaml` — is downstream of
  that one DataFrame.
- So the store's contract is *shape-preserving*: **`read(pair, interval, since, until)`
  returns precisely that DataFrame**, so `fetch_ohlc_dataframe` becomes a thin read-through
  (fetch → `upsert` → `read`). Train, backtest and paper call sites change by one adapter
  (`read_ohlc_dataframe`) whose signature is unchanged. The store lands the feature directly
  in the exact seam `train.py:155`, `backtest.py:138` and `paper_trade.py:287` already use.
- It also activates dormant machinery: `prepare_episode` (`rl/data.py:265`) and the unused
  `TradingEnvironment.reset(options=...)` become real train/eval split + walk-forward, and
  `since`/`until` surfaces on the CLI for controlled replays.

Gap 1 is the **unlocker**, not just the cheapest fix:

1. **It is the only way the bot ever sees more than ~30 days of its own market.** Deep
   history is obtainable *nowhere* else — Kraken REST is capped, period.
2. **Gap 2 and Gap 3 are depth-capped WITHOUT it.** Their signal vectors join onto the OHLCV
   frame (`merge_extra_features`), whose window is bounded by the OHLC frame length. A
   Binance-since-2020 funding backfill joined onto a 720-bar Kraken frame is a 30-day slice
   — the exact same data capacity as today, with cross-venue noise thrown in. The exogen
   columns ride on the store's depth, so the store must come first.
3. **Zero sourcing risk.** Pure code over already-fetched keyless data. No US-geo-block
   (Binance/Bybit/OKX all self-cert-restricted in RESEARCH.md), no rate ceiling, no new
   venue, no ToS surface. The guardrail "prefer keyless" is satisfied maximally.

Signal-per-effort verdict: a funding-rate column adds *one* differentiated scalar but only
inside a 30-day frame; the store raises the information *capacity* of **every** current and
future feature (market, technical, `signals`) and fixes ops redundantly (the paper trader's
60-day refetch every 60 s collapses to append + tail read). One vector per pass — this pass
is the market-data foundation; the next two passes ship the exogenous vectors on top of it.

## 3. DECISION INPUTS WEIGHED (from RESEARCH.md)

| Option | Score | Verdict |
|---|---|---|
| **Gap 1 store — pandas+pyarrow parquet** | cheapest, keyless, prerequisite | **CHOSEN** — verified 720-bar ceiling means it is the only route to deep venue-consistent history; every other gap joins onto its output. |
| Gap 2 — Binance FAPI direct (funding + basis, since 2020) | 8.4 best data fit | Runner-up. Deepest history + cleanest surface, but (a) value capped by the 720-bar OHLC frame it joins onto before the store exists, (b) **US IP geo-block + US-person self-cert** must be host-jurisdiction-confirmed before commit, (c) basis from Binance mark vs Kraken spot = cross-venue noise until the store anchors venue-consistent prices. |
| Gap 2 alt — Kraken-Futures poller (`PF_ETHUSD`) | 7.6 best house-fit | **Becomes the natural `kraken-market-data` companion poller** (same venue, USD-quoted) for a later funding/basis column — it has no public history, so it *requires* a forward store; reinforces Gap-1-first. |
| Gap 3 — StockTwits v2 + alt.me F&G | 9.0 best new modality | Runner-up. Genuinely new modality, fully keyless, live-verified. But medium sourcing cost (unauthenticated rate ceiling) and it, too, joins onto the OHLC frame → depth-capped without the store. Second exogenous axis for a future pass; its history belongs in the store eventually. |
| Gap 4 — book/trades recorder, Gap 5 on-chain, Gap 6 macro | ≤6 | Explicitly or implicitly require Gap 1 (book recorder is ephemeral-by-nature; on-chain backfill is limited; macro is feature-design-heavy). Not candidates for this pass. |

## 4. INTEGRATION SKETCH (how the bot consumes it)

**Output contract (the one thing that matters):** `MarketDataStore.read(pair, interval,
since=None, until=None) → pd.DataFrame` with a UTC `DatetimeIndex` and columns
`time, open, high, low, close, vwap, volume, count` — byte-for-byte the shape
`candles_to_dataframe` produces today (`rl/data.py:161-197`). Prices stay float in the
DataFrame; raw `Decimal`/string fidelity lives upstream in `kraken-api.models.Candle`
(as the house style mandates) and is cast only at the DataFrame boundary, exactly as now.

- **File location:** `store/{PAIR_ID}/{interval_min}/YYYY-MM.parquet` month-sliced, with a
  per-(pair, interval) `since`-cursor sidecar `store/_meta.json`. `PAIR_ID` uses the same
  `normalize_ticker_id` rule as the model tree (`ETH/USD` → `ETH_USD`), so `models/ETH_USD/`↔
  `store/ETH_USD/` line up by construction. Store root configurable (`.env` → `MARKET_DATA_DIR`);
  keyless, so the NixOS module needs no secret, just `StateDirectory` + `DynamicUser`.
- **Merge point in this repo:** a thin `read_ohlc_dataframe(pair, interval, since, until,
  extra_features_file)` adapter beside `fetch_ohlc_dataframe` in `rl/data.py`:
  fetch→`upsert`→`read`. `train.py:155`, `backtest.py:138` and `paper_trade.py:287` keep
  their signatures and simply pass a store. The exogenous `signals` seam
  (`merge_extra_features` → `_add_signals_features`) is untouched and joins onto the deeper
  frame, so Gap 2/3 land later with a one-line `_SIGNAL_COLUMNS` widening, with no pipeline
  redesign.
- **Scheduler:** NixOS module + systemd timer (`OnCalendar=*-*-* *:*:30`) running
  `kraken-market-data market update --pair ETH/USD --interval 60`; the JSON-logged, redacted
  poller appends incremental bars and persists them. This also answers the audit's
  "no operational scheduler" finding for the data substrate (the news/signals hourly pull
  scheduling is a separate, deferred concern).
- **What it unlocks immediately:** deep history > 30 d by accumulation, `since`/`until`
  replay, train/eval split + walk-forward through the currently-unused
  `TradingEnvironment.reset(options=...)`, backtest without a re-fetch, and a paper trader
  whose per-tick cost drops to append + tail read.

## 5. RUNNER-UPS AND WHY THEY LOST

1. **Gap 2 — funding-rate / perp-basis (Binance FAPI primary, Kraken-Futures poller
   secondary).** Lost because its information value is bounded by the very 720-bar ceiling
   this pass exists to break: the funding/basis columns join onto the OHLC frame, so on top
   of today's data capacity they add one scalar inside a 30-day slice. It also carries the
   one real risk set in the whole candidate list (US-friendly geo/ToS, confirmed only by
   host-jurisdiction check) and, sourced from Binance, introduces cross-venue basis noise
   against Kraken spot. **It ships on top of this store in the next pass**, most cleanly via
   the Kraken-Futures poller for a live column, optionally Binance for deep backfill.
2. **Gap 3 — social/search-trend (StockTwits v2 + alt.me Fear&Greed).** Lost on the same
   depth-capping logic, plus a medium sourcing ceiling (unauthenticated StockTwits rate cap).
   The most differentiated *modality* of the three (retail mention velocity, orthogonal to
   the finished GNews+VADER pass) — a strong candidate for the pass after the funding column,
   again dumping its history into the store.
3. **Gap 4 / 5 / 6.** Gap 4 (book/trades recorder) explicitly needs this store as its
   prerequisite; Gap 5 (on-chain) is backfill-limited; Gap 6 (macro) is feature-design-heavy.
   Not candidates this pass.

DECISION COMPLETE