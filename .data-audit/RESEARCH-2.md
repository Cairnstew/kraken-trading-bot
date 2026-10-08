# RESEARCH-2.md — G-2: trade tape + realized spread are wrapped but never called

**Researcher-2, read-only, 2026-10-08.** Repo `/home/seanc/Projects/kraken-trading-bot`.
**Not committed. No source file modified.**

This file supersedes the prior RESEARCH-2.md (which covered G-A/G-B window gaps from a
different audit pass). My assignment is **G-2** (market-microstructure / NEW-DATA-SOURCE):
the Kraken trade-tape and spread wrappers exist but have zero call sites, and
`_SIGNAL_COLUMNS` has no trade-flow / realized-spread column.

## 0. Bottom line

| # | Candidate | Auth | History depth | Reduces to RL seam | Score |
|---|---|---|---|---|---|
| **1** | **Kraken WS `trade` channel (v2) + `spread` channel (v1)** — build a tape recorder over time | keyless | forward-only (live) | yes — per-bar groupby | **9/10** |
| **2** | **Binance aggTrades archive** (`data.binance.vision`) — seed deep history, sibling pattern | keyless | **years (since 2017)** | yes — per-bar groupby | **9/10** |
| 3 | Kraken REST `/0/public/Trades` + `/0/public/Spread` | keyless | **last 1000 trades / ~200 spreads** | yes | 6/10 |
| 4 | ccxt `fetch_trades` / `fetch_agg_trades` (unified sourcing) | keyless | exchange-dependent | yes (normalizes shape) | 7/10 |
| 5 | pandas-ta / ta-lib | — | — | no (no microstructure) | 3/10 |

**Top recommendation: #1 (Kraken WS trade channel) for the live tape + #2 (Binance
aggTrades archive) for the deep-history seed.** Together they give a complete tape:
the WS channel builds forward from go-live, the Binance archive backfills years of
history using the exact pattern the sibling `kraken-deep-history` already proved.
The reduction to per-bar scalars is ~20 lines of pandas groupby — **no specialized
microstructure library is needed or warranted.**

---

## 1. Kraken REST endpoints (already wrapped, zero call sites)

### 1.1 `GET /0/public/Trades` — `manager.recent_trades` (manager.py:192)

- **Auth:** keyless (`security: []` in OpenAPI spec).
- **Rate limit:** by IP + currency pair. **1 req/s or less** stays within limits;
  exceeding → restricted for a few seconds (Kraken support article, "Public REST
  Market Data", last updated 2026-10-01).
- **History depth:** **last 1000 trades** (default `count=1000`, max 1000). For
  BTC/USD this is minutes of tape, not hours. `since` is a cursor (nanosecond
  timestamp string) for incremental updates within the 1000-trade window.
- **Output shape:** structured JSON.
  ```json
  {"error": [], "result": {"XXBTZUSD": [
    ["30243.40000", "0.34507674", 1688669597.8277369, "b", "m", "", 61044952],
    ...
  ], "last": "1688671969993150842"}}
  ```
  Trade row: `[price, volume, timestamp(float sec), side(b/s), ord_type(m/l), misc, trade_id]`.
- **Pagination:** `since` + `last` cursor. Walk forward only; cannot page back before
  the 1000-trade window.
- **Verdict:** too shallow to seed history, but correct for incremental top-ups of a
  live recorder. Already wrapped.

### 1.2 `GET /0/public/Spread` — `manager.spread` (manager.py:200)

- **Auth:** keyless.
- **Rate limit:** by IP only (not per-pair).
- **History depth:** **last ~200 top-of-book spreads**. `since` is "intended for
  incremental updates within available dataset (does not contain all historical
  spreads)".
- **Output shape:** structured JSON.
  ```json
  {"error": [], "result": {"XXBTZUSD": [
    [1688671834, "30292.10000", "30297.50000"],
    ...
  ], "last": 1688672106}}
  ```
  Spread row: `[timestamp(int sec), best_bid, best_ask]`.
- **Verdict:** extremely shallow (~200 samples). Useful only as a live top-of-book
  spread feed. Already wrapped.

---

## 2. Kraken WebSocket channels (already wrapped, zero call sites)

### 2.1 WS v2 `trade` channel — the right live tape source

- **Endpoint:** `wss://ws.kraken.com/v2` (public, keyless). This is exactly what
  `manager.public_ws_url()` returns (manager.py:369-372).
- **Subscribe:**
  ```json
  {"method": "subscribe", "params": {"channel": "trade", "symbol": ["BTC/USD"], "snapshot": true}}
  ```
- **Snapshot:** most recent **50 trades**. Updates stream on every match.
- **Update shape (normalized JSON object):**
  ```json
  {"channel": "trade", "type": "update", "data": [{
    "symbol": "BTC/USD", "side": "sell", "qty": 40.0, "price": 0.5117,
    "ord_type": "market", "trade_id": 4665906,
    "timestamp": "2023-09-25T07:49:37.708706Z"}]}
  ```
  `side` is the **taker** side (`buy`/`sell`) — exactly what signed volume / OFI needs.
- **Rate limit:** WS public channels are not call-limited the way REST is; the
  constraint is connection management (ping every 60 s, resubscribe on reconnect).
- **Wrapper status:** `SpotWebSocket` (websocket.py:54) has `connect_public`
  (:105), `subscribe` (:138-166 with `depth=` and `snapshot=`), `await_ack`
  (:183-209), `iter_messages` (:230-261). `kraken_api/cli.py:179-188` already
  exposes a `ws` subcommand with `--output`. **The full recorder exists upstream
  as a CLI; nothing calls it.**
- **Verdict:** this is the correct way to build a trade tape forward from go-live.
  The wrapper is complete; a recorder that subscribes, appends to JSONL, and
  reduces per-bar is the missing piece.

### 2.2 WS v1 `spread` channel — live top-of-book spread

- **Endpoint:** `wss://ws.kraken.com` (v1, keyless). Channel `spread` streams
  `[timestamp, best_bid, best_ask]` updates.
- **Verdict:** complements the REST `/Spread` with a live stream. Lower priority
  than the trade channel (the trade channel's tape can derive effective spread from
  the book channel, and the REST `/Spread` already covers point samples).

### 2.3 WS v2 `book` channel — for effective spread / microprice

- **Endpoint:** `wss://ws.kraken.com/v2`, channel `book`. L2 snapshots + incremental
  updates with CRC32 checksums. Gives top-of-book for effective-spread and
  microprice calculations.
- **Verdict:** optional add-on if effective spread (trade price vs. mid) is wanted
  alongside realized spread. Not required for the core G-2 signal.

---

## 3. Libraries that reduce trade tape to per-bar scalars

**Key finding: no specialized microstructure library is needed.** The reduction from
a trade list to per-(ticker, bar) scalars is a pandas `groupby` + aggregation. The
repo already depends on pandas 3.0.4. Adding a dependency for ~20 lines of groupby
is the wrong trade (same conclusion the prior RESEARCH-2.md reached for predicate
pushdown).

### 3.1 The reduction (what the recorder must emit)

Given a trade list with `(timestamp, price, qty, side)`, per bar:

| Scalar | Formula | RL column name |
|---|---|---|
| Signed volume | `sum(qty * sign(side))` | `signed_volume` |
| Taker buy-sell ratio | `sum(qty where side=buy) / sum(qty)` | `taker_buy_ratio` |
| Order-flow imbalance | `(buy_vol - sell_vol) / (buy_vol + sell_vol)` | `order_flow_imbalance` |
| Realized spread (bps) | `mean(2 * \|trade_price - mid\| / mid * 1e4)` | **`realized_spread_bps`** (reserved, features.py:145-146) |
| Effective spread (bps) | `mean(2 * \|trade_price - mid_at_trade\| / mid * 1e4)` | `effective_spread_bps` |
| VPIN | volume-synchronized OFI (Easley et al.) | `vpin` |

All are per-(ticker, timestamp) scalars — exactly the shape `merge_extra_features`
(data.py:633) consumes. The output is a JSONL with `timestamp`, `ticker`, and the
scalar columns, merged onto the floored bar hour.

### 3.2 Library survey

| Library | Maintained | License | Auth | Helps? |
|---|---|---|---|---|
| **ccxt** | **yes** — v4.5.85, 1623 releases, latest upload 2026-10-01 | MIT | keyless public | **Yes** — normalizes trade shape across Kraken/Binance/Coinbase; `fetch_trades` / `fetch_agg_trades`. Best unified sourcing layer. |
| pandas-ta | yes | MIT | — | No — technical indicators only, no microstructure (no OFI/VPIN/realized spread). |
| ta (ta-lib) | yes | MIT | — | No — same as pandas-ta. |
| vectorbt | yes | AGPL/GPL | — | No — backtesting, not tape reduction. |
| py-vpin / orderflow | sparse | varies | — | No — unmaintained or niche; the groupby is simpler. |

**ccxt is the only library worth adding**, and only if the recorder should source
from multiple exchanges through one API. If the recorder is Kraken-only (live) +
Binance-only (archive), the existing `kraken-python` wrapper + a stdlib Binance
client (the `kraken-deep-history` pattern) is enough — **no ccxt needed**.

---

## 4. Cross-exchange alternatives (deep history)

Kraken's REST tape is capped at 1000 trades — useless for seeding. For deep history:

### 4.1 Binance aggTrades archive — the proven sibling pattern

- **URL:** `https://data.binance.vision/data/spot/monthly/aggTrades/<SYMBOL>/<SYMBOL>-aggTrades-YYYY-MM.zip`
- **Auth:** keyless, official, no ToS key.
- **History depth:** **years (since 2017)**. One month of BTCUSDT aggTrades is
  ~567 MB (measured, HEAD request 2026-10-08).
- **Live API:** `GET /api/v3/aggTrades?symbol=BTCUSDT&limit=1000` — keyless,
  `since` (trade ID) cursor. Sample row:
  ```json
  {"a": 4084878972, "p": "83321.15000000", "q": "0.00364000",
   "f": 6745831698, "l": 6745831698, "T": 1791417519561,
   "m": false, "M": true}
  ```
  `m` = is buyer maker (`false` = taker buy). `T` = ms timestamp.
- **Rate limit:** live API ~1200 req/min weight; archive is CDN-backed (one-time
  seeds effectively unthrottled).
- **Sibling proof:** `kraken-deep-history` already uses this exact archive for
  OHLCV — keyless, stdlib-only client (`urllib` + `zipfile` + `csv`), no ccxt,
  no pandas in the core. A trade-tape seeder is a sibling of a sibling.
- **Verdict:** the correct deep-history source. Same venue-agnostic store seam.

### 4.2 Coinbase

- `GET /products/<id>/trades` — keyless, but only recent trades (no deep archive
  on the public API). Weaker than Binance for seeding.

### 4.3 Why not Kraken for deep history

Kraken's `/0/public/Trades` is hard-capped at 1000 trades with no archive download.
There is no keyless way to get more than 1000 trades from Kraken. This is the same
physics as the OHLC 720-bar cap that motivated `kraken-deep-history` in the first
place.

---

## 5. How the output reduces to the RL seam

The RL observation takes per-(ticker, timestamp) scalars merged via
`merge_extra_features` (data.py:633) onto the floored bar hour. The path:

1. **Recorder** subscribes to Kraken WS `trade` (live) and/or downloads Binance
   aggTrades archive (seed). Appends raw trades to a JSONL tape.
2. **Reducer** reads the tape, groups by `(ticker, bar_hour)`, computes the §3.1
   scalars, writes a JSONL with `timestamp`, `ticker`, `signed_volume`,
   `taker_buy_ratio`, `order_flow_imbalance`, `realized_spread_bps`,
   `effective_spread_bps`.
3. **Merge** — `merge_extra_features` joins onto the OHLCV frame's floored hour.
4. **Observation** — add the new columns to `_SIGNAL_COLUMNS` (features.py:94-125).
   Use `realized_spread_bps` (already reserved at features.py:145-146), never
   `spread` (collides with the funding-derived `spread`).

The freshness columns (`signal_age_hours`, `signal_observed`) are written by the same
merge seam, so a bar with no tape is distinguishable from a balanced one.

---

## 6. Evidence appendix

| Claim | Source | Type |
|---|---|---|
| `/0/public/Trades` returns last 1000 trades, `since` cursor, keyless | Kraken OpenAPI spec (`docs.kraken.com/openapi/spot-rest.yaml`, `/public/Trades`) | [C] |
| Trade row = `[price, volume, ts, side, ord_type, misc, trade_id]` | same spec, example | [C] |
| `/0/public/Spread` returns last ~200 spreads, keyless | same spec, `/public/Spread` | [C] |
| Spread row = `[ts, best_bid, best_ask]` | same spec, example | [C] |
| Public REST rate limit ~1 req/s, by IP+pair for Trades/OHLC | Kraken support article "What are the API rate limits?" (2026-10-01) | [C] |
| WS v2 `trade` channel: snapshot of 50, taker side, normalized JSON | `docs.kraken.com/exchange/api-reference/spot-websocket-v2/trade` | [C] |
| WS v2 public endpoint `wss://ws.kraken.com/v2` | `docs.kraken.com/exchange/api-reference/spot-websocket` | [C] |
| `manager.recent_trades` / `manager.spread` / `public_ws_url` / `SpotWebSocket` wrapped, 0 call sites | `kraken-python/kraken_api/manager.py:192,200,369`; `websocket.py:54`; AUDIT.md §G-2 | [C] |
| `realized_spread_bps` reserved name | `kraken_trading_bot/rl/features.py:145-146` | [C] |
| `_SIGNAL_COLUMNS` has no trade-flow column | `features.py:94-125` | [C] |
| `merge_extra_features` merges per-(ticker, hour) JSONL onto floored bar | `data.py:633` | [C] |
| ccxt v4.5.85, 1623 releases, latest 2026-10-01, MIT | PyPI JSON API | [M] |
| Binance aggTrades archive exists, ~567 MB/month for BTCUSDT | HEAD request to `data.binance.vision` | [M] |
| Binance live aggTrades keyless, `m` = is-buyer-maker | live API sample | [M] |
| `kraken-deep-history` uses Binance archive, keyless, stdlib-only | `~/Projects/kraken-deep-history/README.md` | [C] |

---

RESEARCH COMPLETE
