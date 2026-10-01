# RESEARCH-2 — Gap **G2**: order book fetched every 60 s and discarded; microstructure has no path to the RL space

Researcher: **researcher2**, team `data-audit-1002`, pass of 2026-10-02.
**Read-only pass.** No source, config or test file was modified. The only file this
pass writes is this one.

Sources actually read: `.data-audit/AUDIT.md` (G2, the "Input seams" table, the exhaustive
`kraken_api` call-site list, and the `recent_trades`/`spread` lower-priority row);
`kraken_trading_bot/engine.py:46-76`; `kraken_trading_bot/strategies/sma.py:57-116`;
`kraken_trading_bot/strategies/base.py:84-97`; `kraken_trading_bot/rl/features.py:20-125`,
`:447-482`, `:524-605`; `kraken_trading_bot/rl/data.py:627-727`;
`configs/default.yaml:41-48`; and the whole of `~/Projects/kraken-python`
(`manager.py`, `client.py`, `models.py`, `websocket.py`, `watch.py`, `export.py`, `README.md`).
Plus live probes against Kraken's published OpenAPI and against Binance's public data bucket.

Everything in this file is cited. Where I could not verify something, it says so.

---

## 1. THE BACKFILL ASYMMETRY — THE VERDICT, UP FRONT

Because this gates every design choice below, it goes first.

### 1.1 Kraken's order book cannot be backfilled. This is now proven, not inferred.

Kraken's `/0/public/Depth` accepts **four** query parameters — `pair`, `assetVersion`,
`count`, `asset_class` — and **no `since`**. Its response body has **no `last` cursor**
either. I read the published OpenAPI for the endpoint directly:

> `get /public/Depth` — "Returns level 2 (L2) order book, which describes the individual
> price levels in the book with aggregated order quantities at each level."
> parameters: `$ref: pair`, `$ref: assetVersion`, `count` (integer, **minimum 1, maximum 500,
> default 100**), `$ref: asset_class`. `security: []`.
> — https://docs.kraken.com/api/docs/rest-api/get-order-book

There is nothing to page on. Kraken holds one live L2 snapshot and hands it to whoever asks,
capped at 500 levels per side. The local client agrees and offers no way to ask for more:

* `kraken-python/kraken_api/client.py:116-121` — `def depth(self, pair, count=None)` builds
  exactly `{"pair": …}` plus `count` when given. **No cursor parameter exists.**
* `kraken-python/kraken_api/manager.py:185-190` — `order_book(pair, count=None)` calls it and
  wraps the result in `OrderBook`. Nothing else is reachable.

**AUDIT.md's G2 claim is CONFIRMED.** The order book has no `since`, no pagination, and no
retention. There is no historical Kraken order book, and there never was one on the REST API.

### 1.2 The audit's contrast needs one correction, and it is a load-bearing one

AUDIT.md:277-284 says `recent_trades` and `spread` are "both take a `since` cursor and return
one … therefore historically reconstructible in a way Depth is not." That is **half right, and
the wrong half is `spread`.**

* **`/0/public/Trades`** — `since` (string) and `count` (1–1000, default 1000) accepted;
  returns `last` described as "ID to be used as since when polling for new trade data"; rows
  are `[price, volume, time, buy/sell, market/limit, miscellaneous, trade_id]`.
  — https://docs.kraken.com/api/docs/rest-api/get-recent-trades
  A backfill mechanism genuinely exists. Depth of *retention* is not documented (see §5).

* **`/0/public/Spread`** — the docs are explicit, and they contradict the audit:
  > "Returns the last ~200 top-of-book spreads for a given pair"
  > `since`: "Return spread data since given timestamp. Optional, intended for **incremental
  > updates within available dataset (does not contain all historical spreads)**."
  > — https://docs.kraken.com/api/docs/rest-api/get-recent-spreads

  So Spread is a ~200-sample **rolling in-memory buffer**, explicitly documented as *not* a
  historical archive. Backfilling spread from Kraken REST is impossible by design.
  Worse for the feature plan: `SpreadPoint` carries **no size at all** —
  `kraken-python/kraken_api/models.py:552-567` is `(pair, time, bid, ask)` and the doc's row
  is `[int time, string bid, string ask]`. **Spread data cannot produce an imbalance, ever**,
  because imbalance needs volumes.

  **Correction: the real asymmetry is Depth vs Trades, not Depth vs Spread.**

### 1.3 A forward-only microstructure tape has no training window. Quantified.

This is the question the brief calls most important, so I answer it flatly.

**A forward-only tape is not useless — but it is not a feature. It is an instrument.**

The arithmetic, from the repo's own shipped configuration:

| Quantity | Value | Source |
|---|---|---|
| Bar interval | **60 minutes** | `configs/default.yaml` `ohlcv_interval_minutes` (`train.py:189`, `backtest.py:397`) |
| Look-back windows | `[1, 4, 24]` bars | `configs/default.yaml:43` `feature_windows` |
| Warm-up before the first tradable bar | ~24+ bars (`first_tradable_index`, `features.py:128`) | `features.py:128-139` |
| Example matrix window | `2026-09-01 → 2026-09-20` = **456 bars** | `tools/model_matrix.py:191-194`, `configs/matrix.example.yaml:191-194` |
| Train / eval split at `eval_split: 0.7` | **319 train / ~137 eval** | `data_window.training_frame` / `evaluation_frame` |
| Models ever trained | **none** — `models/` is empty but for `.gitkeep` | AUDIT.md:143 |

So: a forward-only recorder accumulating one sample per bar produces **one usable bar per
hour**. To reach the repo's own *smallest* honest training run (456 bars) you need **456
uninterrupted hours ≈ 19 days** of uptime from a standing start, plus a separate wait for the
24-bar warm-up before the first tradable bar. And then:

* Any gap longer than `signal_max_age_hours: 12` (`configs/default.yaml:126`) causes
  `merge_extra_features`' bounded carry (`data.py:449-463`) to **drop** the record and
  zero-fill. So a **single 13-hour outage silently converts the entire microstructure column
  to `signal_observed = 0.0`** — which is precisely the degradation AUDIT.md G1 already
  documents happening today for all twelve exogenous columns. G1 would simply be reproduced
  in a second group.
* `market_data_store`, by contrast, reads back arbitrarily far into the past (`data.py:1003`).
  One channel is a **store**; a forward-only channel is a **tape**. That is G6 restated, and
  it is why a tape cannot stand in for history.

**Therefore, directly: adding the order book (or spread) to `feature_groups` at training time
is not viable.** It can only be (a) excluded from the fitted observation, in which case the
agent never learns it, or (b) included and silently zero on every historical bar, which is the
exact failure mode AUDIT.md G2 warns about and that the width guard cannot see. **A forward-only
microstructure channel must not enter `feature_groups` while it is forward-only.** Its
legitimate consumers are paper/live replay and — see §4 — an *accumulator that is seeded from a
historical archive*, which converts the tape into a store on day one.

### 1.4 But the asymmetry is NOT global. Order-book history IS obtainable for free — elsewhere.

This is the finding that changes the ranking, and it contradicts the natural reading of §1.1.
**Binance publishes historical order-book archives.** Verified live against the public S3
bucket, not from docs:

```
s3://data.binance.vision/data/futures/um/daily/bookDepth/          ← EXISTS (USD-M futures)
s3://data.binance.vision/data/futures/um/daily/bookTicker/         ← EXISTS (USD-M futures)
s3://data.binance.vision/data/spot/daily/                          ← aggTrades/ klines/ trades/ ONLY
s3://data.binance.vision/data/spot/monthly/                        ← aggTrades/ klines/ trades/ ONLY
```

`bookDepth` and `bookTicker` exist **only for USD-M futures**. There is **no** spot equivalent.
`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `DOGEUSDT` all have ≥500 daily files each; `ETHUSDT`
`bookDepth` runs from **2023-01-01** and is present at **2026-09-30** (3.75 years). The format,
read from an actual download:

```csv
timestamp,percentage,depth,notional
2026-09-15 00:00:06,-5.00,146088.12400000,359972092.84473000
2026-09-15 00:00:06,-4.00,132983.81600000,328490134.89757000
…
2026-09-15 00:00:06,+5.00,…,…            # (12 bands: ±0.2, ±1, ±2, ±3, ±4, ±5 %)
```

Measured on `ETHUSDT-bookDepth-2026-09-15.zip` (604 KB): **2880 snapshots/day = one every
30 seconds**, 12 rows each = 34 560 rows/day. `depth` is **cumulative outward from the mid**
(monotone: −5 % 146 088 > −4 % 132 983 > … > −0.2 % 5 836), so marginal band depth is a
difference of adjacent rows.

`bookTicker` is the full BBO stream. Format, from `ETHUSDT-bookTicker-2023-05-16.zip` (47 MB):

```csv
update_id,best_bid_price,best_bid_qty,best_ask_price,best_ask_qty,transaction_time,event_time
2849995377933,1817.70000000,139.31200000,1817.71000000,25.05800000,1684237787203,1684237787207
```

3 664 305 rows for one day — every BBO change. From 2023-05-16. **Coverage is sparse for
recent dates** (probes for 2025-06-01, 2026-01-01, 2026-06-01, 2026-09-20, 2026-09-30 all
returned absent), so it lags; see §5.

**So: `order_book_imbalance` — the branch `features.py:581-585` can never reach today — is
computable from three years of free, keyless, downloadable history.** At a 30 s cadence that
yields 120 observations per 1-hour bar to aggregate. The price is a venue discontinuity
(Binance USD-M perpetual vs Kraken spot), which is the *same* discontinuity `deep-history.example.yaml:9-19`
already introduces and which AUDIT.md G3 flags as a normalisation hazard.

**Net verdict:** the *API* asymmetry is real and Kraken-specific. The *sourcing* asymmetry is
not — a backfillable order book exists, it just is not on Kraken. Any design should therefore
separate "how do I get the data" (answered: Binance Vision, free) from "can it enter the
training observation" (answered: yes, once seeded).

---

## 2. PER-CANDIDATE RECORD

Scored on how cheaply each reduces to what the RL pipeline consumes — **a small scalar or
short vector per `(ticker, bar timestamp)`**, never a raw dump.

### Candidate A — Kraken `/0/public/Depth` (the status quo, already paid for)

| Attribute | Value |
|---|---|
| Maintenance | Kraken-operated, v1.1.0 spec, current |
| License / auth | Proprietary API · **`security: []` — keyless** |
| Rate limits | Spot REST call counter, max 15–20 depending on tier (https://docs.kraken.com/llms.txt) |
| Parameters | `pair`, `assetVersion`, `count` (1–500, default 100). **No `since`.** |
| Response | `{error, result:{PAIRKEY:{asks:[[price,vol,ts]…], bids:[[price,vol,ts]…]]}}}`, **no `last`** |
| Output shape | Nested dict of two lists of 3-element arrays — needs aggregation before it is observation-shaped |
| **Can it backfill?** | **NO. Structurally impossible.** |
| Client state | `client.py:116-121`, `manager.py:185-190`; `BookLevel` captures price/volume/**per-level timestamp** (`models.py:189-199`) |

One genuinely good property: the L2 rows carry a **per-level timestamp**, so a forward recorder
gets level age for free — useful for a "how stale is this level" feature that no other source gives.

Live consumption today: `engine.py:70-74`, `count=10`, once per pair per 60 s, result assigned to
`data["order_book"]` and **never read**. `strategies/base.py:92` documents `order_book` as one of
three canonical `tick()` inputs; `strategies/sma.py:57-116` reads only `candles` and `ticker`
(sma.py:68-69) and uses `ticker.ask`/`ticker.bid` for a limit price at sma.py:140,154.
`grep` across `kraken_trading_bot/strategies/` for `order_book` returns exactly that one docstring line.
**AUDIT.md G2 items 1-5 all verified true.**

### Candidate B — Kraken `/0/public/Trades`

| Attribute | Value |
|---|---|
| Maintenance | Kraken-operated, current |
| License / auth | Proprietary · **keyless** |
| Rate limits | same counter (15–20/tier) |
| Parameters | `pair`, `since` (string), `count` (**1–1000**, default 1000), `assetVersion` |
| Response | rows `[price, volume, time(float, fractional seconds), buy/sell, market/limit, misc, trade_id]`, plus `last` |
| Output shape | List of 7-element arrays → needs a per-bar aggregator |
| **Can it backfill?** | **Mechanically yes.** Retention depth **not documented** — unverified (§5) |

Client state: `client.py:123-128`, `manager.py:192-198`, `Trade.from_public_row` at
`models.py:374-385`.

> **Two code-verified defects that block backfill through the current facade.**
> 1. `Trade.from_public_row` **parses only 5 of the 7 wire fields** — it drops `misc` and
>    **drops `trade_id`**. `trade_id` is the monotonic per-row id (`61044952`, `61044953`,
>    `61044956` in the spec example) and is the only strictly-ordered key on a row.
> 2. It coerces `time` with `int(vals[2] or 0)` (`models.py:382`), **truncating the fractional
>    second** that the wire format supplies (`1688669597.8277369`).
>
> Consequence: paging `since` on `Trade.time` loses sub-second ordering, so trades sharing a
> second are silently dropped or re-fetched — a silent data-loss bug in any backfill built on
> `recent_trades`. A backfill pager must reach past the manager facade (`mgr.client.trades`,
> which the README explicitly sanctions: "everything Kraken's API offers is reachable via
> `mgr.client`") or `Trade` must gain `trade_id` and a float `time`.

Cost estimate for a real backfill: at 1000 trades/page and a 1 req/s keyless counter, a
6-week `ETH/USD` tape (order 2–3 M prints) is **~2 000–3 000 pages ≈ 35–50 minutes per pair,
one time.** Entirely feasible. Compare Candidate A: **impossible.**

### Candidate C — Kraken `/0/public/Spread`

| Attribute | Value |
|---|---|
| Maintenance | Kraken-operated, current |
| License / auth | Proprietary · **keyless** |
| Parameters | `pair`, `since` (integer), `assetVersion` |
| Response | ~200 rows `[int time, str bid, str ask]`, plus `last` |
| Output shape | 3-element arrays |
| **Can it backfill?** | **NO — documented as *not* containing all historical spreads**, and only ~200 samples are retained |
| Imbalance possible? | **NO — rows carry no volume** (`models.py:552-567`) |

Client state: `client.py:131-135`, `manager.py:200-206`.
**Verdict: a dead end for a training feature.** Only useful as a cheap live cross-check on
Candidate A's top-of-book, and only for the ~minutes it retains.

### Candidate D — Binance Vision `bookDepth` + `bookTicker` (USD-M futures, daily ZIPs) ← **the backfillable book**

| Attribute | Value |
|---|---|
| Maintenance | Binance-operated public data bucket; actively published (files present yesterday) |
| License | Free public data; published for research use (see https://github.com/binance/binance-public-data) |
| Auth | **None.** Anonymous HTTPS GET. No API key, no account, no ToS-sensitive surface |
| Rate limits | Ordinary S3 object GETs; no counter |
| Output shape | **Clean CSV.** `timestamp,percentage,depth,notional` (12 bands) and `update_id,best_bid_price,best_bid_qty,best_ask_price,best_ask_qty,transaction_time,event_time` |
| Volume | 604 KB/day (`bookDepth`), 47 MB/day (`bookTicker`) per symbol |
| History | `bookDepth` ETHUSDT **2023-01-01 → present**; `bookTicker` ETHUSDT from **2023-05-16**, sparse recently |
| **Can it backfill?** | **YES — 3.75 years, free, keyless.** |
| Cost | Venue discontinuity: Binance USD-M **perpetual futures**, not Kraken spot |

### Candidate E — Kraken WebSocket v2 `book` channel (forward only, but already implemented)

| Attribute | Value |
|---|---|
| Endpoint | `wss://ws.kraken.com/v2`, channel `book` |
| Auth | **None for the public book.** (`manager.ws_token()` at `manager.py:365` is the **private** token — not needed here) |
| Params | `depth` ∈ {**10**, 25, 100, 500, 1000} (default **10**), `snapshot` (default `true`) |
| Message | `{channel:"book", type:"snapshot"\|"update", data:[{symbol, bids:[{price,qty}], asks:[{price,qty}], checksum, timestamp}]}` |
| Integrity | CRC32 over the top 10 bids+asks, per message; updates **must be applied in sequence** |
| Output shape | Incremental delta stream → requires a book state machine |
| **Can it backfill?** | **NO.** No replay endpoint |
| Already built? | **Yes, substantially.** `websocket.py:138-166` `subscribe(channel, symbols, snapshot=, depth=)`; `_CHANNELS` includes `"book"` (`websocket.py:43`); `WsBook.from_ws` parses bids/asks/snapshot/checksum (`models.py:760-782`); `decode_message` handles the book channel (`websocket.py:296-298`); `export.iter_ws_jsonl` exists (`README.md:316-317`) |

Two things make this far cheaper than it looks:

1. **`depth` defaults to 10 — exactly the `count=10` the live loop already requests**
   (`engine.py:72`). The WS book is a shape-compatible replacement for the discarded call.
2. AUDIT.md:90 notes *"every `ws_*` / WebSocket entry point"* is unused. Verified: `grep` finds
   **zero** WS references in `kraken_trading-bot/`; the capability lives entirely in
   `kraken-python`. The README's own "Extending → Suggested next steps" list names it:
   *"a stream consumer: keep a live order book / trades record from `ws`"* (`README.md:339`).

**Architectural cost, stated honestly.** A streaming book is **not** a drop-in for a
request/response loop. It is a different process shape: a long-lived connection, a maintained
book state, sequence discipline, CRC32 validation, reconnect-with-resnapshot, and a consumer
that must **not** die with the parent. AUDIT.md G5 already documents that `run_paper_trader`
catches only `KeyboardInterrupt` (`paper_trade.py:648-671`) — a socket drop would kill the
session. And a WS recorder is still **forward-only** (§1.3): it does not solve backfill by
itself. Its value is as the *forward half* of a seeder that has already been backfilled with
Candidate D.

### Candidate F — CCXT

| Attribute | Value |
|---|---|
| Maintenance | Very active: 101 049 commits, 44.2 k stars, 100+ exchanges |
| License | **MIT** |
| Auth | Keyless for public market data |
| Kraken surface | `fetch_order_book` = snapshot; `fetch_trades` |
| **Can it backfill an order book?** | **NO.** |

**CCXT adds zero historical-replay capability.** `fetch_order_book` is a snapshot;
`watch_order_book` (CCXT Pro) is forward-only streaming with a managed book. CCXT does **not**
download Binance's `bookDepth`/`bookTicker` archives. Its only real value here would be a
uniform interface across venues — which is exactly what AUDIT.md:491 lists as *"structurally
the largest lift: no second venue in the pipeline at all"*. **Not worth the dependency for G2.**

### Candidate G — other venues (checked, not recommended)

* **Binance spot REST `/api/v3/depth`** — `limit` only (max 5000), no `since`. Forward-only.
  But Binance Vision **spot** `aggTrades` *is* a full historical trade archive (daily+monthly ZIPs,
  same bucket, keyless) — the best free historical *trade* source found, if the Kraken trades
  retention in §5 turns out to be shallow.
* **Coinbase** — `/products/{id}/book` is a snapshot; the Exchange WS `level2`/`full` channels
  are forward-only. No free archive. **(Unverified in this pass — see §5.)**
* **OKX** — `GET /api/v5/market/books` (400 depth) is a snapshot; the WS `books` channel pushes a
  400-depth snapshot then 100 ms increments. **No free downloadable order-book archive found.**
  *(Partially verified: endpoint and WS semantics read from the live OKX docs page; historical
  availability not confirmed — §5.)*
* **Bybit / BitMEX / Deribit / Hyperliquid** — not surveyed in this pass. All known to expose
  current-book-only REST plus streaming WS; none advertises a free historical book archive.
  **Stated as unverified rather than asserted (§5).**
* **Academic / commercial** — Tardis.dev and similar sell L2 archives; **not verified, and not
  free**, so it loses to Candidate D on every axis the brief cares about.

---

## 3. THE CONSTRUCTIBLE PER-BAR FEATURE LIST

The pipeline wants **one row per `(ticker, bar timestamp)`** with a small numeric vector, then
`environment._raw_feature_array` ffill → `fillna(0)` → z-score (`AUDIT.md:110-114`). Every entry
below is shaped for that. `B` = computed from the Binance archive, `T` = from Kraken trades,
`V` = **already in the existing OHLCV channel**.

### 3.1 From `bookDepth` (Candidate D) — the highest-value set

Source rows: 2880 snapshots/day × 12 cumulative bands. Aggregate the 120 snapshots inside each
1-hour bar.

| Feature | Formula over one bar | Shape | Source |
|---|---|---|---|
| `obi_0p2` | time-weighted mean of `(D(-0.2) − D(+0.2)) / (D(-0.2) + D(+0.2))` — **narrow-band book imbalance**, 120 samples/bar | scalar ∈ [−1,1] | B |
| `obi_1p0` | same at the ±1 % band | scalar | B |
| `obi_5p0` | same at the ±5 % band | scalar | B |
| `obi_band_slope` | `obi_0p2 − obi_5p0` — concentration of resting pressure near the mid vs far out | scalar | B |
| `depth_decay_ratio` | `D(-5%) / D(-0.2%)` — how much depth sits away from the touch; high ⇒ thin, easily pushed book | scalar | B |
| `depth_log_notional` | `log1p(notional(+0.2%) + notional(−0.2%))` — log-scaled, so a single fat-tailed bar cannot dominate the z-score | scalar | B |
| `obi_vol_20` | 20-bar rolling std of `obi_0p2` — imbalance instability | scalar | B |
| `obi_flips_per_bar` | sign changes of `obi_0p2` within the bar | scalar (int, log1p) | B |

**`obi_0p2` is the direct answer to `features.py:581-585`.** That builder computes
`(bid_vol − ask_vol) / (bid_vol + ask_vol)` from columns nobody produces; `bookDepth` supplies
`bid_vol` and `ask_vol` at ±0.2 % *at 30-second resolution*, i.e. **the first producer for that
branch that has history.** Note it is depth-band, not level-10, so it is a *better* imbalance
than the current builder's nominal intent (a ±0.2 % band is a wider, more robust notion of
"top of book" than 10 arbitrary ticks).

### 3.2 From `bookTicker` (Candidate D) — the spread channel, with history

| Feature | Formula over one bar | Shape |
|---|---|---|
| `realized_spread_bps` | `(ask − bid) / mid`, time-weighted mean over the bar | scalar |
| `spread_vol_20` | 20-bar rolling std of the above | scalar |
| `spread_p90_bps` | 90th pct of `(ask−bid)/mid` within the bar | scalar |
| `taker_buy_frac` | fraction of BBO-up events whose *ask* shrank (aggressive buy) | scalar ∈ [0,1] |
| `bbo_size_imbalance` | `(best_bid_qty − best_ask_qty) / (best_bid_qty + best_ask_qty)`, time-weighted | scalar ∈ [−1,1] |

**Naming matters here, and the codebase already says so.** `features.py:95-98` reserves the
name for exactly this: *"A future tick-level tape recorder must use `realized_spread_bps`
instead of competing for this name."* Use that name. Do **not** emit into `spread`, which
`_SIGNAL_BUILDER_INPUT_COLUMNS` (`features.py:101`) declares a builder-output column with a
single writer.

### 3.3 From Kraken `/0/public/Trades` (Candidate B) — venue-correct tape

| Feature | Formula over one bar | Shape |
|---|---|---|
| `signed_volume` | `Σ(+price·vol on 'b', −price·vol on 's') / bar_notional` — Kyle's lambda proxy | scalar ∈ [−1,1] |
| `order_flow_imbalance_5` | `(Σ buy vol − Σ sell vol) / (Σ buy vol + Σ sell vol)` | scalar |
| `amihud_illiq` | `mean(|ΔP| / notional)` over the bar's trades, × 1e6 (scale-free, log-able) | scalar |
| `trade_size_p50_log` | `log1p(median trade volume)` — institutional vs retail fill size | scalar |
| `trade_size_dispersion` | `log1p(p90/p10)` of trade volume within the bar | scalar |
| `vwap_dev_trade` | `bar vwap / close − 1` computed **from the tape**, independent of the OHLCV `vwap` field — a cross-check on §3.4 | scalar |
| `large_trade_share` | share of bar notional from prints > 10× the bar's median size | scalar ∈ [0,1] |

### 3.4 Already in the channel, already parsed, and already built — the under-used proxy

This is the AUDIT's own durability concern, and it deserves a sharper answer than "a risk".

`/0/public/OHLC` returns 8 fields: `[time, open, high, low, close, **vwap**, **volume**, **count**]`.
The pipeline parses `vwap` at `data.py:717` and `count` at `data.py:719`, persists both, and then
`add_derived_ohlcv_features` (`data.py:627-688`) derives three columns from them:

* `vwap_dev = close / vwap − 1` — `data.py:674`
* `trade_count_zscore_20` — 20-bar rolling z-score of `count` — `data.py:681-683`
* `volume_per_trade = volume / count` — `data.py:686`

So **the implied-vs-quoted VWAP gap and the trade count are ALREADY a per-bar
microstructure proxy, computed on the live Kraken path, with full history, at zero extra cost,
right now.** They are also in the canonical allow-list (`_SIGNAL_COLUMNS`, `features.py:58-60`)
and forwarded by the `signals` group (`features.py:601-605`).

Concretely, from the two fields the wire already carries, per bar:

| Feature | Formula | Shape | Provenance |
|---|---|---|---|
| `vwap_dev` | `close / vwap − 1` | scalar | **V** — exists today (`data.py:674`) |
| `trade_count_zscore_20` | `(count − mean₂₀) / std₂₀(count)` | scalar | **V** — exists today (`data.py:681`) |
| `volume_per_trade` | `volume / count` | scalar | **V** — exists today (`data.py:686`) |
| `vwap_close_gap_zscore_20` | 20-bar z-score of `vwap_dev` — is the buy pressure *unusual*? | scalar | **V** — **new, derivable from an existing column, no new source** |
| `count_per_range` | `count / ((high − low) / close)` — participation per unit of realised move | scalar | **V** — **new, derivable, no new source** |

`vwap_close_gap_zscore_20` and `count_per_range` are the cheapest wins on this entire page: two
rolling expressions over columns that are already parsed, persisted and normalised, adding no
provenance, no second venue, no new file, and no new seam. They should be sequenced **before**
anything that requires a new data source.

**Durability risk, confirmed and quantified.** AUDIT.md:122 notes feature widths 52 and 60 are
only reachable *because* `vwap`/`count` are parsed. I derived the widths from source and **could
not reproduce 49/52/60**; with `feature_windows: [1, 4, 24]` (`configs/default.yaml:43`) my count
is:

| Group | Columns | Source |
|---|---|---|
| `price` | 3 base + 3×3 windows = **12** | `features.py:524-536` |
| `technical` | 11 × 3 = **33** | `features.py:541-558` |
| `volume` | 3 base + 3 windows = **6** | `features.py:560-570` |
| `microstructure` | **0 … 2** | `features.py:572-585` |
| `signals` | 17 `_SIGNAL_COLUMNS` − 3 builder-inputs = **14** | `features.py:46-77`, `:601-605` |
| **total** | **51** (bare OHLCV, no `vwap`/`count`) · **54** (with `vwap`/`count`) · **67** (fully populated + `spread` + `order_book_imbalance`) | |

I could not execute the pipeline to confirm: the repo venv fails on
`libz.so.1: cannot open shared object file` when importing numpy, so a `FeaturePipeline.compute`
probe would not run. **Treat the AUDIT's 49/52/60 as unverified and re-derive before relying on
it.** The structural point stands regardless: the observation width is a function of which
*optional* columns happen to be present, which is exactly the fragility the non-self-referential
`check_feature_width` guard (`features.py:262`) exists to catch — and it does not catch a width
that is *stable but wrong for the source*.

---

## 4. RANKED RECOMMENDATION

Ranked on: can it enter the training observation, times how cheaply it gets there.

**R1 — Ship `vwap_close_gap_zscore_20` and `count_per_range` first.**
Two rolling expressions over `vwap` and `count`, which `data.py:717,719` already parse and
`data.py:627-688` already derive from. Keyless, venue-correct (Kraken spot), fully historical,
no new file, no new seam, no second venue. Follows AUDIT.md G3's rule: feature engineering must
not span a venue seam, and these do not. **This is the whole of the achievable
Kraken-spot-native microstructure story, and it is nearly free.**

**R2 — Build a `kraken-microstructure` sibling that seeds from Binance Vision, then records forward.**
The house style already exists in four siblings (`kraken-python`, `kraken-market-data`,
`kraken-funding-rates`, `ticker-news-signals`, `kraken-social-signals`): `pyproject.toml` + Nix
flake dev shell; small modules behind one manager facade; an `export.py` registry
(`extract`/`extract_many`/`extract_snapshot`, `write_json`/`write_jsonl`); typed dataclasses with
`to_dict()`/`from_*()`; thin `cli.py`; offline no-network tests plus a separate live script;
`.env.example`; structured JSON logging with secret redaction. Emit **one JSONL line per
`(ticker, bar_ts)`** carrying exactly the §3.1 + §3.2 scalars — **not** raw archives. That shape
lands directly on the existing merge seam (`data.py:285-501`), which already does ticker
filtering, hour-flooring, de-duplication, bounded carry and freshness provenance, and which
already reserves the `bid`/`ask`/`spread`/`vwap`/`count` names. The forward recorder rides the
**already-implemented** Kraken WS `book` channel (`depth=10`, matching today's `count=10`), so
the live half is a `SpotWebSocket.subscribe("book", …)` plus a JSONL emitter — `kraken-python`
already has `decode_message` and `iter_ws_jsonl` for it.
**Gate it correctly:** until the Binance seed covers the training window, the channel must be
**excluded from `feature_groups` at fit time** — never silently zero-filled. Say so in the
config comment, the way `AUDIT.md` G2 warns.

**R3 — Fix the `Trade` model before relying on `/0/public/Trades` for anything.**
`models.py:374-385` drops `trade_id` and truncates `time` to `int`. Any backfill pager built on
`KrakenManager.recent_trades` will silently lose sub-second-ordered prints. Add `trade_id: str`
and keep `time` as `float`, or page via `mgr.client.trades` directly. Cheap fix, and it is the
gate on R4.

**R4 — `/0/public/Trades` backfill, for venue-correct (Kraken spot) microstructure.**
The only route that is *both* Kraken-native *and* backfillable. Roughly 35–50 minutes of paging
per pair for six weeks, one time, keyless. Produces §3.3's seven scalars at full venue fidelity.
Blocked on the §5 retention question — if Kraken's `Trades` retention turns out to be shallow,
fall back to Binance Vision **spot** `aggTrades` and accept the venue seam.

**R5 — `/0/public/Spread`: do not use.** ~200-sample rolling buffer, **documented as not
historical**, and **no volumes** so it can never produce an imbalance. Keep it only as a live
cross-check that the recorder's own top-of-book matches Kraken's.

**R6 — Kraken WS `book` alone: not a solution.** Forward-only, so by §1.3 it has no training
window; it is the *live half of R2*, not a standalone answer.

**R7 — CCXT: skip.** MIT and excellent, but it adds no history (Candidate F) and no second venue
is wanted in this pipeline (AUDIT.md:491).

**Explicitly rejected — the failure mode this whole page is about.** Do **not** snapshot the book
once at load time and `forward_fill` it across a historical frame. `merge_extra_features`
(`data.py:449-463`) already forward-fills every other signal with a 12-hour bound, so that
mistake is one line away, and it would silently manufacture an observation column that looks
identical to a real one. **The `check_feature_width` guard cannot detect it** — width would be
stable and correct. It would show up only as a suspiciously smooth `obi` series.

---

## 5. WHAT I COULD NOT DETERMINE — AND WHAT NEEDS A LIVE CHECK

Ordered by how much it would change the ranking.

1. **How far back does Kraken's `/0/public/Trades` actually reach?** The docs describe `since`
   as "Return trade data since given timestamp" and `last` as "ID to be used as since when
   polling for new trade data" — polling language, not archive language, and **no retention is
   documented anywhere.** This is the single question that decides R4. **Live check:** call
   `/0/public/Trades?pair=ETH/USD&since=<30 days ago>&count=1000` and inspect the oldest
   returned `time`; then 90 days, then 1 year. ~3 calls.
2. **Kraken `/0/public/Spread`'s actual retention window.** "~200" samples is stated; how much
   *wall-clock* that is depends on the pair's update rate. **Live check:** two calls one minute
   apart, diff the timestamps.
3. **`bookTicker` daily coverage for recent dates.** `bookDepth` is current (2026-09-30 present);
   my `bookTicker` probes for 2025-06-01 / 2026-01-01 / 2026-06-01 / 2026-09-20 / 2026-09-30 all
   came back absent while 2023-05-16 exists, so either coverage is sparse or it lags by weeks.
   **Live check:** count the daily keys for `ETHUSDT/bookTicker/` over 2026 and find the newest.
4. **AUDIT.md's feature widths 49 / 52 / 60.** Not reproducible from source; my arithmetic gives
   51 / 54 / 67. The repo venv cannot import numpy (`libz.so.1` missing), so I could not execute
   a probe. **Live check:** run `FeaturePipeline.compute` on a synthetic frame with and without
   `vwap`/`count`/`bid`/`ask`/`bid_vol`/`ask_vol`.
5. **Coinbase, OKX, Bybit, BitMEX, Deribit, Hyperliquid historical order-book availability.** I
   read OKX's live docs (400-depth `books` + 100 ms WS increments confirmed; no archive found) and
   did **not** survey the others. I am asserting "no free historical L2 archive" from general
   knowledge of these APIs, **not** from a per-venue check in this pass. If the architect wants
   a venue other than Binance futures, this must be checked properly first.
6. **Tardis.dev and comparable commercial L2 archives** — not evaluated. Known to exist, not
   free; loses to Candidate D on cost unless venue fidelity to Kraken proves decisive.
7. **Whether Kraken's OHLC `vwap` and `count` are exact or rounded**, which bounds how much of
   §3.4 is real microstructure and how much is quantisation. `count` is an `int` on the wire, so
   `volume_per_trade` carries that granularity limit by construction.
8. **Whether the Kraken WS `book` channel's `depth=10` band is comparable to Binance's ±0.2 %
   `bookDepth` band.** They are different constructions (10 ticks vs a percentage band), so the
   forward recorder and the historical seeder will **not** produce identical values at the seam.
   Any training window that spans both will carry a discontinuity — the same class of problem as
   AUDIT.md G3's venue seam. **This is a design decision the architect must make explicitly**,
   not one to discover in a normalisation plot.

---

RESEARCH COMPLETE
