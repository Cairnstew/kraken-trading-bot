# RESEARCH-3.md — Phase 2 research for gaps G-D, G-E, G-F

**Researcher 3. Read-only. 2026-10-03. HEAD = `f6d9118`.**
Slice: `AUDIT.md` §4 gaps **G-D** (order-book imbalance), **G-E** (retry/backoff),
**G-F** (store holes), plus §1.2 (call-site table), §1.3 (timers), §2.4 (widths), §5
(category coverage). Nothing was modified; the only file written is this one.

**Evidence tiers**, used exactly as `AUDIT.md` §0 defines them: **[M]** measured here
(command given), **[C]** cited at a `file:line` or URL, **[I]** inferred. Every figure is
dated. `~/Projects/kraken-market-data/store` **does not exist on this host**
(`AUDIT.md` §1.4), so G-F's inherited `158 bars / 28 gaps / 39h` is **not re-derived and not
re-quoted as fact** anywhere below — §5.4 uses synthetic data instead and says so.

**Measurement environment:** `nix develop --command python …` for every Python figure
(pandas **3.0.4**, numpy, urllib3 **2.7.0**, requests **2.34.2** — the shell's versions, not
PyPI's; both differ, see §4.1). Scripts in `/tmp/opencode/gap/`.

---

## 0. The three findings that change the decision, up front

| # | finding | tier |
|---|---|---|
| **R1** | **A 60 s REST poll cannot carry a book signal into a 60 min bar.** Measured: ETH/USD top-10 imbalance autocorrelation is **+0.51 at a 4 s lag and +0.01 at a 16 s lag**. One poll per bar estimates the bar-mean imbalance with **SE 0.390**; 60 polls per bar give **SE 0.050**. A persistent WebSocket feed is not a nice-to-have here, it is what makes the feature mean anything. | [M] §2 |
| **R2** | **`order_book_imbalance` is currently a warm-up-bearing column, not an exogenous one.** Widening `_SIGNAL_COLUMNS` for `bid_vol`/`ask_vol` costs **exactly +1 observation column**, but on a sparse producer it moved `first_tradable_index` **24 → 54** on a 200-bar frame. Any G-D slice that produces the column sparsely will silently shorten the episode unless the column is declared point-in-time. | [M] §1.5 |
| **R3** | **The upstream half of G-E is a mis-classification bug, not a missing-retry bug.** `kraken_api/transport.py:45-48` matches only `"EAPI:Rate limit exceeded"` and `"EGeneral:Too many requests"`. Kraken's *documented* concurrency-throttle string is `EService: Throttled: [UNIX timestamp]`, which matches neither and therefore arrives as `APIError`. So a wrapper that retries `RateLimitError` alone would miss the second documented rate-limit path. | [M]/[C] §4.2 |

---

## 1. G-D — order-book imbalance as a feature

### 1.1 What the repo already has, read at the line

**[C]** The consumer is `kraken_trading_bot/rl/features.py:1018-1025`:

```python
if {"bid_vol", "ask_vol"}.issubset(df.columns):
    bid_vol = df["bid_vol"].astype(float)
    ask_vol = df["ask_vol"].astype(float)
    denom = (bid_vol + ask_vol).replace(_NON_FINITE_INPUTS, np.nan)
    out["order_book_imbalance"] = (bid_vol… - ask_vol…) / denom
```

So **the formula is already decided and is not the design question**: it is the canonical
`(V_bid − V_ask) / (V_bid + V_ask)` over whatever rows `bid_vol`/`ask_vol` arrive on. The
design questions are (a) what those two numbers mean, (b) where they come from, (c) at what
rate, and (d) how they reach the frame.

**[M]** `grep -rn bid_vol --include='*.py' --include='*.yaml'` over the tree returns **6 hits,
all of them either this branch or tests**:

- `kraken_trading_bot/rl/features.py:1018,1019,1022,1024` — the consumer.
- `tests/test_feature_nonfinite_guards.py:306,310,398,635` and
  `tests/test_rl_environment.py:222` — hand-written fixtures.

**No producer exists in any repo**, confirming `AUDIT.md` §3/G1 and §4/G-D. `bid_vol`/`ask_vol`
are also **absent from `_SIGNAL_COLUMNS`** (`features.py:94-125`), so the merge seam's
column-presence intersection (`data.py:786-790`) would drop them even if a signal file carried
them — the audit's point, verified by the grep above.

**[C]** The live fetch is `kraken_trading_bot/engine.py:72`
`data["order_book"] = self.manager.order_book(pair, count=10)` inside
`fetch_market_data` (`engine.py:46-76`), which **swallows its own exceptions**
(`engine.py:73-74`, `except Exception` → `_LOGGER.warning`). `run_iteration`
(`engine.py:124-149`) passes the dict to `strategy.tick()`; the only strategy,
`strategies/sma.py`, reads `candles` and `ticker`. `strategies/base.py:92` is the docstring.
**[I]** So `order_book` is fetched, allocated, and dropped every `interval=60.0`
(`engine.py:36`) — a genuinely free-to-keep cost, and a genuinely wasted one.

**[C]** The pinned sibling already wraps far more than this:

| surface | file:line | what it actually returns |
|---|---|---|
| `manager.order_book` | `~/Projects/kraken-python/kraken_api/manager.py:185-190` | `OrderBook` from `client.depth(pair, count=…)` |
| `client.depth` | `client.py:116-121` | `GET /0/public/Depth?pair=…&count=…` — **no `since`** |
| `models.OrderBook` | `models.py:214-238` | `.asks`/`.bids` as `list[BookLevel]`; `best_bid/best_ask/spread` |
| `models.BookLevel` | `models.py:189-210` | `price: str`, `volume: str`, `timestamp: int` — **all raw strings** |
| `manager.recent_trades` | `manager.py:192-198` | wrapped, **0 call sites** (`AUDIT.md` §1.2) |
| `manager.spread` (time series) | `manager.py:200-206` | wrapped, **0 call sites** |
| `ws_token` / `public_ws_url` / `private_ws_url` | `manager.py:365,369,374` | the whole WS surface, **0 call sites** |

**[C]** The WS surface is more than three URL getters — `kraken_api/websocket.py` carries a
complete `SpotWebSocket` (`connect_public` `:105`, `subscribe` `:138-166` with `depth=` and
`snapshot=` params, `await_ack` `:183-209`, `iter_messages` `:230-261`) and
`kraken_api/cli.py:179-188` already exposes a **`ws` subcommand with `--output`**, wired to
`kraken_api/export.py:290-303 iter_ws_jsonl` → `write_jsonl`. **[C]** So a raw book-tape
recorder *already exists upstream as a CLI*; see §1.7 for why it is not yet the answer.

### 1.2 Kraken's three book shapes, from the primary docs

**A. v1 REST `/0/public/Depth`** — [C] `https://docs.kraken.com/api/docs/rest-api/get-order-book`
(auto-generated from Kraken's own OpenAPI, `openapi: 3.0.0`, spec version `1.1.0`):

- `count`: *"Maximum number of asks/bids"*, `minimum: 1`, **`maximum: 500`, `default: 100`**.
  ⇒ **`engine.py:72`'s `count=10` is one twentieth of the documented ceiling.** It was not
  chosen for a reason recorded anywhere in either repo. [I]
- Each row is a **3-tuple** `[<price>, <volume>, <timestamp>]` — the schema is
  `minItems: 3, maxItems: 3` with the third element typed `integer`, and the worked example
  shows `1688671659` (unix **seconds**). **[I]** This is a genuinely useful field: it is the
  per-level last-update time, so a snapshot carries information about *how stale each level
  is*. I did **not** find Kraken documenting its semantics, so treat the reading as [I].
- No `since` cursor. **[C]** `client.py:116-121` passes only `pair` and `count`; the spec's
  parameter list for this path is `pair`, `assetVersion`, `count`, `asset_class`.

**[M] Live, 2026-10-03 10:52 UTC**, `GET https://api.kraken.com/0/public/Depth?pair=XBTUSD&count=2`
→ `HTTP/2 200`, 194 bytes, and the response headers include **`cache-control: public, max-age=2`**
plus `server: cloudflare`. **[I]** `max-age=2` is consistent with the depth endpoint being
cached at the edge, which would make a 60 s poll see a book at most ~2 s stale — better than
feared, but it is a cache TTL, not a documented freshness guarantee, and it is not something
the repo may rely on. No `Retry-After` header was present on this success response.

**B. v1 REST `/0/public/GroupedBook`** — [C] same OpenAPI spec
(`https://docs.kraken.com/openapi/spot-rest.yaml`, fetched 2026-10-03, 652 KB;
`/public/GroupedBook` at line 747). **This endpoint is in the pinned spec and is wrapped by
nothing in either repo.** Its parameters: `depth` enum `10, 25, 100, 250, 1000` (default 10)
and **`grouping`**, enum `1, 5, 10, 25, 50, 100, 250, 500, 1000` (default 1) — *"Specifies how
many tick levels should be within each price level. Bids and asks between grouped price
levels are accumulated to the nearest passive level (asks rounded up, bids down)."*
**[M]** `GET /0/public/GroupedBook?pair=XBTUSD&depth=2` returns `HTTP 200` and the grouped
shape `{"pair":…,"grouping":…,"bids":[{"price","qty"}],"asks":[…]}`.

**[I]** This is the most interesting finding for G-D and nobody has noticed it: **`grouping` is
exactly a price-band aggregation**, i.e. Kraken will hand you top-of-book *volume summed over
a price band*, which is the numerator `features.py:1018` wants, computed **server-side**, in
**one request**, at **one bar's resolution**. It is not a replacement for the level-2 book
(it destroys the price dimension) but it is the cheapest possible *coarse* imbalance source
and it removes the "which N" question into a single documented parameter.

**C. v2 WebSocket `book` channel** — [C]
`https://docs.kraken.com/api/docs/websocket-v2/book`:

- Subscribe params: `channel: "book"`, `symbol: string[]`, **`depth`: one of `10, 25, 100, 500,
  1000`, default `10`**, **`snapshot`: bool, default `true`**.
- `snapshot` type: `data[]` of `{bids:[{price,qty}], asks:[{price,qty}], checksum:int,
  symbol, timestamp(RFC3339)}`.
- `update` type: *"The data contains the updates of the bids and asks"* — i.e. **deltas, not
  the full book** — and *"Note, it is possible to have multiple updates to the same price level
  in a single update message. **Updates should always be processed in sequence.**"*

**[C]** `https://docs.kraken.com/exchange/guides/websockets/book-checksum-v2` is the
maintenance guide, and it is where the real cost lives. Verbatim from that page:

> *"Process all price level updates in a message before calculating the checksum. An update
> with `"qty": 0` means that price level should be removed. **After each update, truncate your
> book to the subscribed depth — you will not receive `"qty": 0` for levels that fall out of
> scope.**"*

> *"Parse `price` and `qty` fields using a decimal or string decoder to preserve full precision
> through deserialisation."* (worked example uses `json.loads(bytes, parse_float=Decimal)`)

> *"Checksum verification is optional. … The checksum is always calculated over the **top 10
> price levels** regardless of subscription depth."*

**Three consequences for this repo, all [I] but all direct readings of the above:**

1. **A WS book recorder must implement book state.** Snapshot → ordered-delta application →
   depth truncation. Neither repo has it.
2. **The checksum is a top-10 checksum**, so it pins exactly the region an `N=10` imbalance
   reads and *cannot* validate a deeper book's aggregate. If a producer wants `N=100`, the
   checksum buys it nothing.
3. **[C]** `kraken_api/models.py:760-782 WsBook.from_ws` parses `bids`/`asks`/`snapshot`/
   `checksum` **and nothing else**. `kraken_api/websocket.py:296-298 decode_message` routes
   `book` through `WsBook.from_ws(item, snapshot=…)` and emits the raw levels. **There is no
   delta application and no CRC32 verification anywhere in the pinned sibling** — I grepped
   for it and read `models.py:760-802`. So `kraken-python ws book … --output x.jsonl`
   (§1.7) writes a **raw event tape**, not a book, and the checksum field is carried but never
   checked.

### 1.3 The canonical formula, and what N is defensible

**Provenance, from the primary source, not memory.** Cont, Kukanov & Stoikov, *"The Price
Impact of Order Book Events"*, arXiv:1011.6402. Verbatim from the abstract:

> *"We show that, over **short time intervals**, price changes are mainly driven by the **order
> flow imbalance, defined as the imbalance between supply and demand at the best bid and ask
> prices**. Our study reveals a **linear** relation between order flow imbalance and price
> changes, with a slope **inversely proportional to the market depth**. These results are
> shown to be robust to seasonality effects, and **stable across time scales and across
> stocks**."*

Three facts from that paper that bear directly on G-D, all **[C]** with page numbers from
`https://arxiv.org/pdf/1011.6402`:

| p. | verbatim | why it matters here |
|---|---|---|
| 3 | *"We focus on **'Level I order book'**: the limit orders sitting at the best bid and ask. Every observation of the bid and the ask consists of the bid price P^B, the size q^B of the bid queue …, the ask price P^A and the size q^A of the ask queue"* | The canonical construction is **top-of-book**, `N=1`. |
| 3, 16 | *"the average **R² for order flow imbalance is 65%** compared to **32% for the trade imbalance**"*; p.22: *"∆P^k are the **10-second** mid-price changes"* | The headline predictive result is at a **10-second** horizon and uses an **event-summed flow** (`OFI = Σ e_n` over the interval), not a sampled level. |
| 6 | *"limit orders and cancelations occur **at all levels** of the order book. The distribution of depth across price levels often has **humps, gaps** and is itself a separate object of study … there may be **hidden orders** in the book"* | The paper explicitly declines to model deep-book aggregation as a clean quantity. |

**[I] Critical distinction the repo currently conflates.** CKS's `OFI` is an **event-summed
flow** — a signed count of size added/removed at the best bid and ask, accumulated over an
interval. It is *not* the level imbalance `(V^b − V^a)/(V^b + V^a)` that
`features.py:1018-1025` computes. A snapshot poller can produce the **level** imbalance; it
**cannot** produce OFI, because OFI requires seeing every event (including cancels) between
two points. So the column this repo already computes is the weaker of the two measures even in
principle. That is a fact about the code, not a criticism of it — but the architect should
know the 65% R² belongs to the neighbour, not to this column.

**Now measure, rather than argue, which N is defensible.** [M] I took **10 snapshots of
`/0/public/Depth?pair=ETHUSD&count=500` at 8 s intervals (72.8 s wall, 10 requests,
≈0.14 req/s)** and computed the imbalance over the first *N* levels of each side **from the
same snapshots** (a paired comparison; no extra requests, no extra rate-limit pressure).
`/tmp/opencode/gap/book_depth.py`, 2026-10-03 ≈11:05 UTC:

| N | mean | sd | range observed | ac lag1 (8 s) | ac lag2 (16 s) | ac lag3 (24 s) |
|---:|---:|---:|:---|---:|---:|---:|
| 1 | **−0.749** | 0.383 | −0.999 … +0.171 | +0.18 | −0.11 | −0.17 |
| 5 | +0.119 | 0.473 | −0.650 … +0.897 | +0.58 | +0.49 | −0.14 |
| **10** | +0.078 | 0.439 | −0.491 … +0.642 | **+0.72** | **+0.51** | +0.20 |
| 25 | −0.312 | 0.175 | −0.580 … −0.063 | +0.10 | −0.50 | −0.13 |
| 100 | −0.169 | 0.085 | −0.397 … −0.094 | +0.43 | +0.46 | +0.43 |
| 500 | +0.067 | **0.0071** | +0.060 … +0.082 | +0.50 | +0.45 | +0.48 |

**Caveats, stated because they matter: n=10 snapshots, one pair, one 73-second window,
2026-10-03. The lag-2 and lag-3 autocorrelations use 8 and 7 pairs and are noise-dominated.
Read this table as an ordering and an upper bound on persistence, not as a parameter
estimate.**

What survives those caveats:

1. **`N=10` is the best-supported default, and it is the one already in the code.**
   `engine.py:72` uses `count=10`; Kraken's own WS v2 `book` channel **defaults to
   `depth: 10`** [C, docs]; and it is the only N in the measured set whose lag-1
   autocorrelation is clearly positive *and* whose lag-2 is still positive. Two independent
   sources and the existing code converge on 10.
2. **Large N destroys the signal.** `sd(imb500) = 0.0071` — the top-500 imbalance is
   **effectively a constant**. **[I]** Summing 500 levels per side averages the pressure out;
   the deep book is near-balanced by construction. An observation column with sd 0.0071
   z-scores to a near-constant, which is *exactly* the degenerate-column failure mode
   `AUDIT.md` §2.4 already measures (10 degenerate columns from the funding backfill, 5 from a
   hand-made file). **This is a hard, measured argument against any N above ~100, and against
   any "just use more depth" proposal.**
3. **`N=1` is the worst choice despite being the literature's canonical level.** Measured lag-1
   autocorrelation **+0.18** — essentially unpersistent — with the largest relative
   excursions (mean −0.749, reaching −0.999, i.e. a single side of the top level is
   effectively empty). **[I]** One level is dominated by queue-refill noise; this is the same
   object CKS sums *over many events*, which a snapshot cannot do.
4. **Beyond N=10 the variance falls roughly like `1/√N`** (0.439 → 0.175 → 0.085 → 0.007),
   which is the signature of averaging *noise* rather than extracting *depth-weighted
   pressure*. [I]

**Depth-weighted variant.** I searched the primary literature for an
`exp(−α·|p_i − mid|)`-weighted form and **did not find one in a primary source**
[unverified — absence of evidence, not evidence of absence]. What CKS p.6 says instead is the
opposite of depth weighting: depth *humps*, *gaps*, and *hidden orders* make any
fixed-`D` deep-book aggregate unreliable, and they explicitly model *"the number of shares at
each price level beyond the best bid/ask is equal to `D`"* (p.5) as a **stylized
assumption**, not a measurement. **[I] Recommendation: do not ship a depth-weighted variant on
this evidence.** If the architect wants more than a scalar, the honest multi-component
version is a **small vector over a fixed N ladder** (`imb_1, imb_5, imb_10, imb_25`) rather
than a re-weighted scalar, because it lets the model learn the decay profile instead of having
it hard-coded.

**Scalar or vector, against this repo's contract.** [C] `AUDIT.md` §2.3: the width authority
is `FeaturePipeline.compute()`'s cached `_last_feature_names`, mirrored into
`normalization.npz` and cross-checked by `check_feature_width` (`features.py:~487-495`, which
raises on same-width name swaps). The observation is a flat `(n_bars × n_features)` float32
matrix (`environment.py:470-495, 200`). So:

- **Both fit.** A scalar is 1 column; a 4-vector is 4 columns. The contract is
  *per-`(ticker, timestamp)` scalar-or-small-vector*, and neither reading breaks it.
- **[I] Recommendation: start with the scalar the code already computes** (`bid_vol`,
  `ask_vol` → one column). Reasons: (a) it requires widening `_SIGNAL_COLUMNS` by exactly
  **two** names, not six; (b) the seam already has a tested "builder input" pattern
  (`_SIGNAL_BUILDER_INPUT_COLUMNS`, `features.py:141`) that keeps raw levels out of the
  observation and is what `bid`/`ask` do today; (c) a 4-vector invalidates **every** existing
  artifact's width for a benefit that cannot be demonstrated without a live tape.
- **[I]** If the architect wants the ladder, it should be recorded as **+4 and the raw levels
  as builder inputs**, i.e. the same accounting the comment at `features.py:138-139` already
  keeps honest (*"Net accounting is therefore +1 for the funding bid/ask pair, not +3"*).

### 1.4 Aggregation: a 60 s book vs a 60 min bar

**[C] The cadences.** Bar interval 60 min (`configs/default.yaml:44` `feature_windows`
`[1,4,24]`; the interval itself is read at `train.py:208-209` as
`ohlcv_interval_minutes`). Paper-trade / engine loop tick: `engine.py:36` `interval: float =
60.0`, slept at `engine.py:168`.

**[M] Measured, on the same book, twice.**

*(i) How fast does the imbalance decorrelate?* 20 snapshots of
`/0/public/Depth?pair=ETHUSD&count=10` at 4 s intervals, 77.5 s wall
(`/tmp/opencode/gap/book_ac.py`, 2026-10-03 ≈11:00 UTC):

| measure | mean | sd | ac(4 s) | ac(8 s) | ac(12 s) | ac(16 s) | ac(20 s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| top-10 imbalance | −0.134 | **0.390** | +0.51 | +0.26 | +0.09 | **+0.01** | −0.39 |
| top-1 imbalance | −0.310 | 0.649 | +0.61 | +0.44 | +0.29 | +0.11 | −0.20 |

*(ii) How noisy is the estimate of the bar's mean pressure?* Using the measured
`sd = 0.390` of the top-10 imbalance:

| samples per 60 min bar | SE of the bar-mean estimate | ratio |
|---:|---:|---:|
| **1** — one 60 s REST poll | **0.390** | baseline |
| 4 — 15 s polling | 0.195 | 2.0× |
| **60** — 1 s WebSocket | **0.050** | **7.8×** |

**The aliasing risk, quantified rather than asserted.**

- [M] The 4-s autocorrelation of the top-10 imbalance is **+0.51**, and it has reached
  **+0.01 by a 16 s lag**. Read with the n=20 caveat (lag ≥ 4 uses ≤ 16 pairs), the honest
  statement is an **upper bound**: *the top-10 book imbalance decorrelates on a horizon of
  order tens of seconds.* It is emphatically **not** 60 s and certainly not 3 600 s.
- [I] A REST snapshot every 60 s therefore samples a series that has decorrelated and
  re-correlated many times between samples. Two consequences: (a) consecutive bars' `imb_10`
  values are **close to independent draws**, so any per-bar *change* in imbalance is mostly
  sampling noise; (b) a single sample is an unbiased draw from the *unconditional*
  distribution, so it carries **no information about when within the hour** the pressure
  occurred — only about the hour's typical level, and that with SE 0.390 against a
  between-bar spread that must then be ≥ 0.390 to be visible at all.
- [M] The book is also genuinely volatile in *volume*, not just in sign: top-10 bid volume
  across eight consecutive 4 s samples was `72.59, 71.97, 58.95, 18.22, 17.60, 35.43, 6.52,
  6.59` — a **11× swing in 28 seconds**. **[I]** A snapshot cannot distinguish "the offer side
  is thin because a large ask was just pulled" from "the offer side is thin".
- [C] The literature agrees on the horizon: CKS measure their result against *"the
  **10-second** mid-price changes"* (p.22). [I] There is no primary source I found claiming a
  level-imbalance signal survives tens of minutes.

**So: is a REST-polled book a viable producer?** **[I] Honest answer: no, not for a
60-minute bar, and the reason is arithmetic rather than taste.**

- A REST poller **cannot** observe intra-minute imbalance at all — it has no sample inside the
  minute. That is not a precision loss, it is an outright blind spot: any imbalance episode
  that opens and closes inside one minute is invisible by construction.
- It **can** produce a per-bar scalar, and that scalar is an unbiased but SE-0.390 estimate of
  the hour's mean pressure. **[I]** Whether that is worth an observation column depends
  entirely on whether the hour's *mean* imbalance predicts anything at the hour horizon — and
  the one primary result available (CKS, 65% R²) is at a 10-second horizon on an event-summed
  flow, so it does not transfer.
- **[I] A persistent WebSocket `book` feed is required** for the feature to be more than an
  expensive noise generator. That is the finding the architect needs, because it converts
  G-D from "a small recorder" into "a long-running process" — see §2.4.

**Sampling vs time-averaging, stated plainly.** [I]

- **Time-averaging within the bar is strictly better** than one instantaneous sample, for the
  measured reason above (SE 0.050 vs 0.390) and because it *removes the aliasing* rather than
  accepting it: the bar's value then means "the average book pressure over this hour", a
  quantity with a defined estimator and a computable error bar.
- **[I] An instantaneous sample is only defensible if** the producer takes the *last*
  snapshot of the bar (a "closing book" convention). That is a real convention in
  microstructure work — a bar stamped with its end-of-bar book is a state, not a flow — and it
  is what a REST poller can honestly deliver. **[C]** But note the v1 `Depth` response is
  `cache-control: max-age=2` [M], and Kraken does not document the row `timestamp` semantics
  [I], so "the last snapshot in the bar" is not guaranteed to be the last *state*.
- **[I] Recommendation:** record the **mean over the bar AND the last value** as two
  provenance-labelled columns, matching the seam's existing "log, not state" discipline
  (`AUDIT.md` §4/G-C). Two columns, both finite, both honest, and the pair lets a reviewer
  distinguish "average pressure" from "closing pressure" rather than guessing which one the
  model consumed.

### 1.5 Width contribution, per configuration — **measured**, and with a trap

**[M]** Measured with a runtime monkeypatch of the two module-level tuples (**nothing on disk
was edited**), on a synthetic 200-bar hourly frame that carries `vwap` and `count` and goes
through `add_derived_ohlcv_features` — i.e. the real read seam, avoiding the `AUDIT.md` §0 /
§2.4 F-15 trap. `/tmp/opencode/gap/width.py` + `probe3.py`, pandas 3.0.4:

| configuration | width | `first_tradable_index` |
|---|---:|---:|
| baseline, shipped `feature_groups`, no `bid_vol`/`ask_vol` | 59 | 24 |
| **+ `bid_vol`/`ask_vol` widened in, producer sparse (1 reading in 20 bars)** | **60 (+1)** | **54** ⚠ |
| + `bid_vol`/`ask_vol` widened in, producer dense (every bar) | **60 (+1)** | 24 |
| widened frame, `microstructure` group **off** | 59 | — (`order_book_imbalance` absent, as expected) |

**Read only the deltas, never my absolutes.** My baseline is **59** where `AUDIT.md` §2.4
reports **52 / 60 / 61** for its configurations; my fixture carries a 7-key funding shape, not
the 14-key backfill the auditor measured against, so **my absolute widths are not comparable
to the §2.4 table and must not be quoted as such.** The two transferable results are:

1. **The width delta is exactly +1**, in every configuration where the `microstructure` group
   is on — which is the **shipped default** (`configs/default.yaml:48`
   `feature_groups: ["price", "technical", "volume", "microstructure", "signals"]`). It is
   **+0** when that group is off. `bid_vol`/`ask_vol` themselves reach **zero** observation
   columns if they are added to `_SIGNAL_BUILDER_INPUT_COLUMNS` (`features.py:141`), exactly
   as `bid`/`ask` do today (`features.py:143-146`).
2. **⚠ The measured trap: `first_tradable_index` 24 → 54.** [M] Cause, also measured: excluding
   `order_book_imbalance` from the warm-up test restores it to **24** on the same frame.
   **[I]** `order_book_imbalance` is emitted by `_add_microstructure_features`, so it is a
   *derived* column; `first_tradable_index` (`features.py:176-190`) deliberately excludes only
   `POINT_IN_TIME_EXOGENOUS_COLUMNS` (`features.py:171-173`), which at HEAD is
   `_SIGNAL_COLUMNS ∪ _SIGNAL_BUILDER_INPUT_COLUMNS` and **does not contain
   `order_book_imbalance`**. So its NaNs are read as **warm-up** and eat tradable bars: on
   200 bars, 176 tradable → 146 tradable, a **30-bar / 150-hour** loss. A dense producer costs
   nothing. **[I]** This is the single most likely way a G-D implementation silently damages a
   run, and it is invisible until someone measures `first_tradable_index` — which is precisely
   the F-15/F-8 class of defect this repo has already shipped twice.

**Therefore [I]: any G-D producer must (a) add `bid_vol`/`ask_vol` to both
`_SIGNAL_COLUMNS` and `_SIGNAL_BUILDER_INPUT_COLUMNS`, (b) add `order_book_imbalance` to
`POINT_IN_TIME_EXOGENOUS_COLUMNS`, and (c) be measured for `first_tradable_index` before and
after.** Without (b) the feature group is a liability; with (b) the column costs +1 width and
nothing else. Note also that (b) changes the *semantics* the seam's `signal_observed` /
`signal_age_hours` pair exists to protect (`features.py:128-137`), so the choice has to be
made deliberately rather than defaulted.

### 1.6 What a REST-polled producer would have to be, versus a WS one — and the hosting question

**[I] Honest statement of the trade-off.** The `PLAN.md` §8.1 "depth recorder" that
`AUDIT.md` §3 records as **time-critical and not started** has two materially different shapes,
and the choice is not stylistic:

| | REST poller | WS subscriber |
|---|---|---|
| Requests/day (1/min) | 1 440 | 0 REST + a long-lived socket |
| Intra-minute imbalance | **invisible** [I] | observed |
| Bar-mean SE (measured) | 0.390 (n=1) | 0.050 (n=60) |
| Book state to maintain | none | snapshot → ordered deltas → depth truncation, `qty:0` deletes, **CRC32 verify** [C, WS docs] |
| Full-precision decode | `str` fields [C, `models.py:189-199`] | **`Decimal`** mandated by Kraken's own guide [C] |
| Lives as | a `systemd --timer` + `nix run …#recorder` — **exactly the shape `justfile:163` `funding-timer` already installs** | a **long-running service** |
| Fits the repo's NixOS module? | **yes**, `systemd.*` in a NixOS module | **no** — `nix/module.nix` installs no unit, and `configs/default.yaml:75-78` records that `systemd.user.*` cannot evaluate there |

**[I] The NixOS-module question in the house style.** [C] `AUDIT.md` §1.3 and
`configs/default.yaml:75-78` establish the constraint: `nix/module.nix` installs **no** unit
because it is a NixOS module; unit installation happens through `just funding-timer`
(`justfile:163`). A REST poller therefore **fits the existing mechanism unchanged**. A WS
subscriber does not: it is a service, so it needs either (a) a `systemd.services.*` block in
`nix/module.nix` — which requires the flake to be a flake input on a host, not a local clone,
or (b) the same `just`-installed, hand-run long-lived process the funding path avoided, which
means **no restart-on-boot and no supervision**. **[I] That is a real deployment decision for
the architect, not a detail**, and it is the single strongest argument that G-D is a *larger*
slice than `AUDIT.md` §4/G-D's "blast radius" paragraph implies.

### 1.7 The one thing that already exists, and why it is not enough

**[C]** `kraken-api ws book ETH/USD --output file.jsonl` (`kraken_api/cli.py:179-188`,
`:409-424`; `kraken_api/export.py:290-303`) writes an NDJSON tape of decoded `book` events
from a live WS v2 subscription. **[C]** But per `models.py:760-802` and
`websocket.py:296-298` that tape is **raw events with no book state, no delta application and
no checksum verification**. A downstream consumer would have to reimplement the whole
maintenance guide to read it. **[I]** So:

- Writing the raw tape is *cheap and already done* and has archival value.
- It is **not** a producer of `bid_vol`/`ask_vol` per bar; the derivation still has to be
  written, and it needs the checksum logic to be trustworthy at all.

**[I] Recommendation:** if the architect takes G-D at all, run **one** long-lived subscriber
that (i) maintains the book per Kraken's guide, (ii) verifies the CRC32 at a low frequency
(e.g. every 250 ms — the guide says verification "is optional" and "you can validate on every
update or periodically"), and (iii) appends **per-minute** `bid_vol`/`ask_vol` (top-10 sums,
plus the bar-closing value) to a JSONL in the seam's existing shape. Raw-tape archival is a
*separate, optional* follow-on.

---

## 2. (merged into §1.4 above — retained as the explicit R1 statement)

See **R1** at the top of this file and §1.4 for the full aliasing quantification. Summary:
a 60 s REST poll produces an unbiased but SE-**0.390** estimate of the hour's mean top-10
book pressure; a 1 s WebSocket sample stream produces SE-**0.050**; the underlying signal's
own autocorrelation has fallen to ~0.01 by a **16 s** lag. **[I]** The bar is 3 600 s. The
ratio is 2–3 orders of magnitude and no amount of downstream modelling recovers it.

---

## 3. The trade-off against what is already free

The brief is explicit: `AUDIT.md` §5 records **exactly one** market-microstructure candidate
(G-D), so I must not manufacture more, and must not dismiss the one without evidence. This
section therefore does **not** propose another microstructure feature. It places G-D on a
scale against what the repo already pulls.

### 3.1 The one candidate, honestly scored

**[C]** G-D's directness, per `AUDIT.md` §4/G-D: *"the only remaining candidate that adds a
genuine new dimension … a per-bar scalar in [-1, 1] computed from two numbers — exactly the
shape the observation wants."* My evidence adds three qualifications the auditor could not
have had:

| dimension | verdict | basis |
|---|---|---|
| Shape fit | **excellent** | scalar, bounded, finite; [M] +1 width only |
| Information novelty | **contested** | [C] CKS's predictive result is for an **event-summed flow**; the repo computes a **level** imbalance. The neighbour's 65% R² is not this column's. |
| Achievability | **poor today** | [C] forward-only (no historical depth endpoint); [I] needs a long-lived process (§1.6) |
| Data quality of the input | **poor** | [M] a snapshot cannot distinguish a pulled ask from a thin ask; [M] no fresh validation of level staleness |
| Cost | **+1 width, every artifact invalidated** | [M] §1.5 |

**[I] The candid summary: G-D is the only candidate in its category, it fits the observation
perfectly, and it is the *hardest* of the three gaps I was handed** — it needs new process
supervision, a new producer, a widened allow-list, a semantics declaration, and it buys a
signal whose primary published evidence is for a different quantity at a 360× shorter horizon.

### 3.2 What is already free — and the precise overlap

**[C] The repo already has OHLCV, `vwap` and `count`** (`/0/public/OHLC`, 8 columns per
`client.py:99-114`, carried by `candles_to_dataframe` `data.py:1053-1089`) and already
derives three more columns from them (`add_derived_ohlcv_features`, `data.py:989`):
`vwap_dev`, `volume_per_trade`, `trade_count_zscore_20` (`features.py:93-95`).

**[M] And it already ships a signed-volume imbalance at three horizons.** Read at
`features.py:985-995`:

```python
obv = (np.sign(close.diff()) * volume).fillna(0.0).cumsum()
out["obv"] = obv
for w in self.windows:
    out[f"obv_slope_{w}"] = obv.diff(w)
```

with `self.windows = (1, 4, 24)` (`configs/default.yaml:44`). **[I]** `obv` is
on-balance-volume: the cumulative sum of volume signed by the close-to-close direction. It is
**the OHLCV-derivable first cousin of order-book imbalance**: it measures *realised* net
aggressive flow, whereas `order_book_imbalance` measures *displayed* resting pressure. The
honest statement of the relationship:

| | `obv` / `obv_slope_{1,4,24}` (already shipped) | `order_book_imbalance` (G-D) |
|---|---|---|
| Source | OHLCV, already fetched | needs a live book feed |
| Measures | executed flow | displayed (unexecuted) intent |
| Horizon available | **all history**, including the seeded store | **forward-only** |
| Lead/lag vs price | concurrent-to-lagging (it *is* past volume) | **[C]** CKS: leading at 10 s |
| Cost | 0 (already in the width) | [M] +1 width, new process, new allow-list |
| Failure mode | cannot see resting orders that never trade | [I] cannot be reconstructed after the fact |

**[I] This is the strongest argument against G-D available, and it is not a dismissal of it:**
the repo *already carries* a net-flow signal at three horizons, for free, over all of
history, and it costs nothing. G-D's marginal value is specifically *that displayed liquidity
is a leading indicator of the flow that `obv` records late* — which is a real and published
effect (CKS) but which, per §1.4, this codebase cannot sample properly from REST.

**Named alternatives from the brief, and their honest status here.** The brief asked me to name
a simpler microstructure feature derivable from data already fetched if one exists.

- **Close-location value / Chaikin Money Flow** — **not present**; no `grep` hit in
  `features.py`. It would need ~10 lines in the existing `_add_volume_features`. **[I]** Cheap,
  but it is derived from `high`/`low`/`close`/`volume`, all of which `obv` and the existing
  range features already summarise, so its marginal information over the shipped set is
  unproven. **I am not recommending it**, and per the brief I am not filing it as a candidate.
- **Intradar / Parkinson / Garman-Klass volatility** — **not present**; the repo's volatility
  surface is `volume_zscore_20`, `bollinger_*` and `atr_*` (`features.py` technical/price
  groups per the class docstring `features.py:554-562`). **[I]** Also cheap. Same reasoning.
- **Kyle's lambda as a rolling regression** — not present. **[I]** Requires an OLS per window,
  and its input is executed flow, i.e. it is a *transform of `obv`*, not a new source.
- **Volume-clock imbalance** — **[I]** not present and not derivable from a 60-min OHLCV bar,
  which carries only aggregate volume. It needs trades, and `manager.recent_trades`
  (`manager.py:192`) is wrapped and uncalled (`AUDIT.md` §1.2) — so it is a *third* G-D, not a
  free alternative.

**[I] Bottom line for the architect.** Every free alternative is either already shipped
(`obv` family) or would be a new feature built from columns already in the frame — which makes
them *cheap*, but not obviously *informative*, and this pass is not the place to prove that.
The defensible ranking statement is: **G-D is the only candidate that adds a genuinely new
*source* of information; every alternative adds a new *derivation* of information the repo
already has.** That distinction is the whole argument, and it survives §1.4's aliasing result
(which is about *how well G-D can be produced*, not about whether the information is new).

---

## 4. G-E — retry, backoff and rate-limit handling

### 4.1 The current state, read at the line, and one version correction

**[C]** Verified in the pinned sibling `~/Projects/kraken-python` @ `81de597`:

| fact | file:line |
|---|---|
| `min_interval: float = 0.0` — **no throttle by default** | `kraken_api/transport.py:108` |
| `_throttle()` is a fixed-interval sleep, `if self.min_interval <= 0: return` | `:180-186` |
| `time.sleep(wait)` — the **only** sleep in the module, and it is the throttle | `:186` |
| `except requests.RequestException … raise` — **re-raises, no retry** | `:210-219` |
| `raise RateLimitError(errors, endpoint=path)` | `:250` |
| `session.get(...)`/`session.post(...)` with `timeout=30` | `:204, 206` |
| `self._last_call = time.monotonic()` in a `finally` — so the throttle clock advances **even on failure** | `:220-221` |
| `grep -rn 'retry\|backoff\|MAX_RETR' kraken_api/` → **0 hits** (re-confirmed) | — |
| `data.py:1126` `batch, last = manager.ohlc(pair, interval=interval, since=cursor)` — **no `try`** in `_page_candles` (`:1124-1133`) | `data.py:1126` |

**[C]** So `AUDIT.md` §3/G3 and §4/G-E are confirmed verbatim. The failure mode the audit
names is exact: a mid-loop failure propagates out of `_page_candles`, `collected` is lost,
`fetch_ohlc_dataframe` never reaches `candles_to_dataframe` (`data.py:1053`), and **every
candle already fetched is discarded**.

**[M] Version correction, and it matters for any patch written today.** The dev shell carries
**urllib3 2.7.0** and **requests 2.34.2** — *not* PyPI's current urllib3 **2.8.0**
(uploaded 2026-09-15). Anyone reading urllib3 docs and writing a patch must target **2.7.0**,
the version that will actually run. [M] `requests` is pinned only as `requests` in
`flake.nix:62` and `nix/default.nix:27`; `kraken-python`'s own `pyproject.toml:12` says
`requests>=2.31`.

### 4.2 Kraken's documented rate limits and error responses — verified, with a correction

**[C] `https://docs.kraken.com/exchange/guides/rest/ratelimits`** (fetched 2026-10-03):

> *"Every REST API user has a 'call counter' which starts at `0`. Ledger/trade history calls
> increase the counter by `2`. All other API calls increase this counter by `1` (except
> AddOrder, CancelOrder which operate on a different limiter detailed further below)."*

| Tier | Max API Counter | Decay |
|---|---:|---:|
| Starter | **15** | −0.33/sec |
| Intermediate | **20** | −0.5/sec |
| Pro | **20** | −1/sec |

> *"if the counter exceeds the maximum value, subsequent calls using that API key would be rate
> limited. If the rate limits are reached, additional calls will be restricted for a few seconds
> (or possibly longer **if calls continue to be made while the rate limits are active**)."*

> **Errors:** *"`EAPI:Rate limit exceeded` if the REST API counter exceeds the user's maximum."*
> *"`EService: Throttled: [UNIX timestamp]` if there are too many concurrent requests. **Try
> again after [timestamp].**"*

**[C] `https://docs.kraken.com/exchange/guides/general/errors`** — Kraken publishes a
per-error table with explicit retry guidance, which is unusually useful:

| error | Kraken's own guidance (verbatim) |
|---|---|
| `EService:Unavailable` | *"Implement backoff and retry logic."* |
| `EGeneral:Internal error` | *"**Retry with exponential backoff.**"* |
| `EService:Deadline elapsed` | *"Before retrying, check `OpenOrders` — **the order may have been placed despite the timeout**."* |
| `EAPI:Invalid key` / `Invalid signature` | *"**Do not retry** — fix the authentication logic first"* |
| `EAPI:Invalid nonce` | *"**Do not retry the same nonce** — generate a fresh one"* |
| `EOrder:Rate limit exceeded` | *"Back off and reduce order frequency"* |

**[M] Three corrections to assumptions in my brief, each verified:**

1. **The brief's `EAPI:RateLimitExceeded` is the wrong string for this API version.**
   [C] Kraken's v1 REST docs say **`EAPI:Rate limit exceeded`** (with spaces), and
   [C] `transport.py:45-48` matches exactly that. I confirmed [M] by string test:
   `'EAPI:RateLimitExceeded'` → `APIError`, **not** `RateLimitError`.
   → **[I] Any patch that greps for the camel-case spelling would silently never match.**
2. **[M] A bot-local wrapper keyed on `RateLimitError` misses the concurrency throttle.**
   String test against the pinned marker tuple:

   | server string | current classification |
   |---|---|
   | `EAPI:Rate limit exceeded` | `RateLimitError` ✅ |
   | `EGeneral:Too many requests` | `RateLimitError` ✅ |
   | **`EService: Throttled: 1788471234`** | **`APIError`** ❌ |
   | `EService:Unavailable` | `APIError` ❌ |
   | `EGeneral:Internal error:5` | `APIError` ❌ |

   [I] **R3:** the *concurrency* throttle — the one that bites a paper-trade loop and the one
   that carries a retry-after timestamp — is not classified as a rate limit at all.
3. **Kraken sends **no** documented `Retry-After` header, and does not document a 429.**
   [C] I downloaded Kraken's own published OpenAPI spec for Spot REST
   (`https://docs.kraken.com/openapi/spot-rest.yaml`, 652 243 bytes, 2026-10-03) and
   `grep` for `429`, `Retry-After`, `RateLimitExceeded`, `Rate limit`, `Too many requests`,
   `retry` returns **only** four incidental hits — `558`, `733`, `2455`, `13315` — none of
   which is a status code, header definition, or rate-limit string. [M] The live 200 response
   I captured (§1.2) carries no `Retry-After`.
   → **[I] Conclusion: Kraken signals rate limiting *in the JSON body*, as a string, on an
   HTTP 200.** That is why `transport.py:225-251` inspects `document["error"]` at all. Any
   retry design for this exchange must therefore be **string-matched, not status-matched** —
   and `urllib3.Retry(status_forcelist=[429])` is the wrong instrument here.
   [unverified] I did **not** trigger a live 429 to confirm the HTTP status Kraken returns;
   the docs only enumerate the body strings.

**Load arithmetic, measured.** [M] `data.py:1233` default `pages = 6`, so a full
`_page_candles` loop is ≤ **6 requests**. At Starter tier (max counter 15, decay 0.33/s), 6
back-to-back requests is ~6/15 of the budget and refills in ~18 s. **[I]** The *fetch* leg is
safe. **[M] The dangerous leg is paper-trade**: `AUDIT.md` §4/G-B notes it re-fetches the
whole window every 60 s, so 6 requests/60 s = 0.1 req/s against a 0.33/s decay — nominally
safe in isolation, but **[I]** a single 429 is enough to kill the tick, and the current code
has no path that survives one. That is the whole of G-E's urgency: not throughput, **fragility**.

### 4.3 Candidate libraries — measured maintenance status

**[M]** All four fetched live from `https://pypi.org/pypi/<pkg>/json` on 2026-10-03:

| package | version | license | requires-python | latest release | maintenance read |
|---|---|---|---|---|---|
| **urllib3** | 2.8.0 (**2.7.0 in this dev shell**) | MIT | `>=3.10` | **2026-09-15** | actively maintained; ~3 weeks old |
| **requests** | 2.34.2 | Apache-2.0 | `>=3.10` | 2026-05-14 | maintained |
| **tenacity** | 9.1.4 | Apache-2.0 | `>=3.10` | 2026-02-07 | maintained |
| **backoff** | 2.2.1 | MIT | `>=3.7,<4.0` | **2022-10-05** | **[I] stale — ~4 years**; stable-but-abandoned classifier |
| **httpx** | 0.28.1 | BSD-3 | `>=3.8` | 2024-12-06 | **[I] classifier still says `Development Status :: 4 - Beta`** |

**API shapes.** [C] `urllib3.util.Retry` (from
`https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html`):

```python
Retry(total=10, connect=None, read=None, redirect=None, status=None, other=None,
      allowed_methods=frozenset({'DELETE','GET','HEAD','OPTIONS','PUT','TRACE'}),
      status_forcelist=None, backoff_factor=0, backoff_max=120,
      raise_on_redirect=True, raise_on_status=True, history=None,
      respect_retry_after_header=True,
      remove_headers_on_redirect=frozenset({'Authorization','Cookie','Proxy-Authorization'}),
      backoff_jitter=0.0, retry_after_max=21600)
```

**[C] The three properties that decide the question, quoted from the same page:**

> *"`allowed_methods` — Set of uppercased HTTP method verbs that we should retry on. **By
> default, we only retry on methods which are considered to be idempotent.**"*

> *"`backoff_factor` … urllib3 will sleep for `{backoff factor} * (2 ** ({number of previous
> retries}))` seconds. **If `backoff_jitter` is non-zero, this sleep is extended by
> `random.uniform(0, {backoff jitter})` seconds.**"*

> *"`respect_retry_after_header` … Whether to respect Retry-After header on status codes defined
> as `Retry.RETRY_AFTER_STATUS_CODES`"*, where **`RETRY_AFTER_STATUS_CODES = frozenset({413,
> 429, 503})`** [M, read off the installed 2.7.0].

**[C] `tenacity`** (from `https://tenacity.readthedocs.io/en/stable/api.html`) exposes
`tenacity.retry(func, sleep=, stop=, wait=, retry=)` with, among others,
`stop_after_attempt`, `stop_after_delay`, `wait_exponential`, `wait_exponential_jitter`,
`wait_random`, `wait_random_exponential`, `wait_incrementing`, `retry_if_exception_type`,
`retry_if_result`, `retry_if_not_exception_message`, `before_sleep_log`. **[C]** `httpx`
transport retries are a thin `AsyncHTTPTransport(retries=N)` integer — **not** configurable
per-status, per-method, or per-error-type.

**[M] Idempotency, proven rather than asserted.** I stood up a local HTTP server that answers
**429** to everything, mounted
`HTTPAdapter(max_retries=urllib3.util.Retry(total=3, status_forcelist=[429],
backoff_factor=0.0, raise_on_status=False))`, and counted what actually went over the wire:

```
DEFAULT_ALLOWED_METHODS = ['DELETE', 'GET', 'HEAD', 'OPTIONS', 'PUT', 'TRACE']
POST in defaults? False
[MEASURED] 429 x4: GET sent 4x (1 + 3 retries) | POST sent 1x (1, NOT retried)
```

→ **[C]/[M] `urllib3.Retry` does not retry POST by default, which is exactly the required
behaviour**, because every Kraken order-placement call is `POST /0/private/AddOrder`
(`transport.py:163`, `private()`). This is a genuine safety property and it is the single
strongest argument for the `urllib3` route. **[I]** Note `total=3` on a GET means **4 wire
requests** — a retry count that reads as "3" costs 4.

### 4.4 Recommendations for G-E

**[I] Ranked, with the reasoning attached.**

1. **Do the bot-local consumer half first: a bounded retry around `data.py:1126`.**
   *Reasoning:* it is the only part reachable without a lock bump (§4.5); it addresses the
   precise waste `AUDIT.md` §4/G-E names (**"every candle already collected is
   discarded"**); and it is ~20 lines in one function, matching "Smallest candidate here."
   *Design:* retry **only** on the classified rate-limit exceptions
   (`kraken_api.errors.RateLimitError`) **plus** the two strings from §4.2 that are currently
   mis-classified (`EService: Throttled:`, `EService:Unavailable`, `EGeneral:Internal error`) —
   i.e. match on the *error message*, not on the exception type, because of R3.
   *Budget:* 3 attempts, **full jitter**, ceiling ~8 s, and **never** longer than one 60 s paper
   tick so a retry storm cannot stack across ticks.

2. **Then a retry *budget*, not a retry *count*, at the loop level.** **[C]** Kraken's own
   wording is the argument: *"possibly longer if calls continue to be made while the rate
   limits are active."* **[I]** A per-run budget (e.g. "at most 20% of calls may be retries,
   else stop retrying and fail") is the direct expression of that sentence; a plain `for` loop
   with `total=3` is not. **[I]** Retry budgets are the standard remedy for retry amplification
   and they cost nothing here because the workload is a 6-request loop.

3. **Full jitter vs urllib3's `backoff_jitter`.** **[C]/[I]** urllib3's model is
   `factor·2ⁿ + uniform(0, jitter)` — jitter **added** to a deterministic exponential. That
   is *decorrelated* backoff, not AWS-style *full jitter* (`uniform(0, factor·2ⁿ)`), which is
   the better-behaved of the two. **[I]** With `backoff_factor=0.4, backoff_jitter=0.4` you get
   `0.4·2ⁿ + U(0,0.4)`, i.e. a minimum sleep that grows exponentially — acceptable, and strictly
   better than nothing. If *true* full jitter is wanted, it needs `tenacity`'s
   `wait_random_exponential` or a 10-line hand-rolled loop. **[I]** For a 6-request loop I would
   not add a dependency for it.

4. **Idempotency: retry GET freely, never retry POST.** **[C]/[M]** Kraken's own error table
   says of `EService:Deadline elapsed`: *"Before retrying, check `OpenOrders` — the order may
   have been placed despite the timeout."* **[I]** So a POST retry on this exchange is not merely
   inadvisable, it is **ambiguous**: the safe response is to *not* retry and to reconcile. If
   the upstream fix lands on `urllib3.Retry`, its default `allowed_methods` already enforces
   this; if it is hand-rolled, this must be written down as an explicit invariant with a test.
   **[I] Nonce discipline matters too:** `transport.py:169-178` derives the nonce from
   `max(now_ms(), last+1)`, so a retried `private()` call gets a *fresh* nonce automatically —
   which satisfies Kraken's `EAPI:Invalid nonce` rule (*"Do not retry the same nonce"*) only
   because the nonce is regenerated per call, not because the code thought about it. Worth a
   comment; **[unverified]** whether a retried POST would also violate Kraken's own
   duplicate-order protections — I did not find documentation on that.

5. **Compose the throttle with the backoff rather than replacing it.** **[C]**
   `transport.py:180-186` is a *fixed*-interval throttle and `min_interval` defaults to
   `0.0`, i.e. **off**. **[I]** Setting a sane `min_interval` is a **strictly better first fix**
   than any retry: 0.33/s (Starter decay) ⇒ `min_interval ≈ 3.0` never trips the counter at
   all, which removes the common case rather than recovering from it. Note **[C]** the
   throttle clock is advanced in a `finally` (`:220-221`), so it already accounts for failed
   calls — the two mechanisms compose without double-counting. **[I]** Backoff should be
   layered *on top of* the throttle for the residual case (429 after a well-behaved client).

### 4.5 The upstream half: what the lock bump actually costs

**[C] The pin.** `flake.nix:6` declares
`kraken-python.url = "github:Cairnstew/kraken-python"`. `flake.lock` pins
`kraken-python` at rev **`81de5974ded7c17586eedc78158d1293d2f74c66`**, `narHash`
`sha256-GoR8qRBTz…`, `lastModified 2026-09-26T22:46:41Z`. (`kraken-market-data` at
`055d7f6f…`, `2026-09-28T11:28:39Z`.)

**[I] What a bump costs, concretely:**

- The fix lives in `~/Projects/kraken-python/kraken_api/transport.py` — a **different repo**,
  currently a flake input *and* a local checkout. It must be **pushed to GitHub** before a
  `nix flake lock --update-input kraken-python` can see it; a local unpushed commit is
  invisible to the lock.
- The bump pulls **the whole sibling**, not the fix. **[C]** The sibling is at `0.4.0`
  (`pyproject.toml`/HEAD message *"0.4.0: paper trading simulation, WS auth, credential
  module options"*). Any commit landed since `81de597` on `main` arrives too. **[I]** There
  is no branch or tag pinning to constrain the blast radius.
- It invalidates the `nix` build cache for `kraken-python-src` and every derivation depending
  on it (`flake.nix:24-25`, `nix/default.nix`), i.e. a full rebuild of the dev shell and
  package. **[I] In a repo whose CI runs `nix flake check`, that is a CI-time cost too.**
- **[I]** It is a **supply-chain-relevant** action: it changes what `nix develop` executes.
  `AUDIT.md` §3/G3 already frames G3's upstream half this way, and `DECISION.md` §5.3/§7.6 show
  this repo treats supply-chain gates as acceptance criteria, not nice-to-haves.

**[I] Is a bot-local wrapper a defensible interim? Yes, and on these grounds:**

- It is **additive**: `data.py` can catch `RateLimitError` and the mis-classified
  `EService: Throttled:` string *without touching the sibling*, so it does not conflict with a
  later upstream fix.
- It sits at the **only** place in this repo that actually suffers (`data.py:1126`, inside the
  6-request loop), so it fixes the real waste rather than a general case.
- It is **removable**: when the upstream fix lands, the wrapper's job reduces to the string
  matching Kraken's v1 API still needs, or disappears.
- **[I] What it must NOT do:** set a global `requests` retry policy, patch `KrakenTransport` at
  import time, or retry POST. Those are the three shapes that would turn an interim fix into a
  behavioural change the repo cannot see.

---

## 5. G-F — detecting holes in a bar series

### 5.1 The current state, read at the line

**[C]** `data.py:1402` `df = store.read(pair, interval, since=since, until=until)` —
`store.read` belongs to the pinned sibling `kraken-market-data` @ `055d7f6f`, which
**concatenates month files with no reindex and no gap assertion**. **[C]** `data.py:1407-1418`
then applies `add_derived_ohlcv_features` and the three exogenous merges, and
**[C]** `prepare_episode()` (`data.py:1513`) slices the trailing `episode_bars` **before** fit.
No reindex, no `date_range`, no gap check anywhere on the read path.

**[C] `configs/default.yaml:198-211`** documents the damage and states the correct
epistemics verbatim: *"Measured on the shipped ETH/USD 60m store: 158 missing bars across 28
gaps, largest 39h, including a 38-bar hole at the SEED/LIVE-APPEND SEAM … Across that hole
`return_1` reports a 39-hour return as though it were 1-hour, and the 24-row warm-up can span
more than a day. **No guard here can catch it: every value is finite and correctly computed
from the rows given.** Run `just store-verify` … **DETECTED AND LABELLED, not fixed; the real
fix is time-aware feature windows over a reindexed bar grid (DECISION.md §14).**"*

**[M] I could not re-derive those figures.** `~/Projects/kraken-market-data/store` does not
exist on this host (`AUDIT.md` §1.4: *"The market-data store is absent on this host"*). Every
number in §5 below is therefore from a **synthetic** frame with a **constructed** hole, and is
used to prove *mechanism*, not to re-measure the shipped store. **Do not read §5's numbers as
a re-measurement of the 158/28/39h figures.**

### 5.2 What a count-based window does across a hole — measured

**[M]** 200 hourly bars, `rows[100:139]` deleted (**39 bars absent**), pandas 3.0.4
(`/tmp/opencode/gap/probe.py`):

| quantity | measured | what it means |
|---|---:|---|
| rows before → after | 200 → 161 | 39 absent |
| `close.diff()` at the row after the hole | **40.0** | a **40-step** change presented as a 1-step change |
| `close.pct_change()` at the same row | **+40.404 %** | a 40-hour return labelled as 1 hour |
| `rolling(24)` wall-clock span at that row | **62 hours** | a "24-bar" window covering 2.6 days |

**[C] The arithmetic, stated so it is not misread again:** *N* missing bars ⇒ the two surviving
bars are *N*+1 steps apart. So "38 missing bars" and "39 elapsed hours" are **both correct**,
which is exactly the clarification `tools/store_gap_scan.py:60-73` and `DECISION.md` §14.1
already recorded (*"this exact gap got quoted two ways and read as a contradiction"*). My
synthetic 39-bar hole gives a 40-step diff, which is the same arithmetic.

**[C] And the repo already contains a working detector.** `tools/store_gap_scan.py` (433
lines) implements `find_gaps()` (`:243-307`), `GapReport.expected_bars` (`:172-177`), the
`misaligned` off-grid flag (`:100-104`, `:285`), the seam-by-name logic (`:52-58`, `:279-305`)
and `format_report()` (`:347-389`). It is wired to `just store-verify` (`justfile:355`,
`justfile:349`) and its docstring says it *"detects and labels only … It does not repair,
interpolate or reindex anything, and it does not fail the run."* **[I]** So G-F is **not a
missing-machinery gap. It is a missing-plumbing gap**: ~300 lines of tested detection logic
exist and **nothing consumes its verdict** — the same shape as the funding seam
(`AUDIT.md` §4/G-C: producer exists, key is null).

### 5.3 The concrete approaches, and a verified recipe

**[M] Verified on pandas 3.0.4** (the version in this dev shell), against the synthetic frame
above (`missing 39`, one run, no off-grid bars):

```python
step  = pd.Timedelta(minutes=60)                       # the config's interval
grid  = pd.date_range(idx[0], idx[-1], freq=step, tz="UTC")   # expected bar grid
missing = grid.difference(idx)                        # -> DatetimeIndex of absent stamps
runs = []                                              # group consecutive absent stamps
for t in missing:
    if runs and t - runs[-1][-1] == step: runs[-1].append(t)
    else: runs.append([t])
```

Output [M]: `expected 200 present 161 missing 39`; `runs [39]`; the single run spans
`2026-01-05 04:00Z → 2026-01-06 18:00Z`.

The specific APIs, and what each does **[C]** from the pandas documentation:

| API | behaviour that matters here |
|---|---|
| `pd.date_range(start, end, freq, inclusive=)` | builds the **expected** grid; `inclusive` controls whether the endpoints are included |
| `Index.difference(other)` | `grid.difference(idx)` — **exactly the "which labels are absent" query**; this is the right primitive, not a reindex |
| `DataFrame.reindex(grid)` | inserts the absent labels with **NaN by default** (`fill_value` is NaN); reindexing a **non-unique** index raises, which is itself a useful duplicate guard |
| `Series.asfreq(step)` | same idea, series-oriented, with `method=` for forward/backward fill |
| `Series.diff` / `pct_change` | **[M] operate on row position, not on the index** — measured above: a 40-step hole yields a `40.0` diff. **This is the crux of G-F.** |
| `Series.rolling(window=N)` | **[C]** `N` is rows; `closed`/`step` exist; there is **no** time-aware window — the fix must come from reindexing, not from a flag |
| `Index.get_indexer` | returns **−1** for absent labels; useful for a boolean mask without materialising a reindexed frame |
| `pd.Timedelta`, `resample` | for re-gridding onto a canonical wall-clock origin |

**[I] Two traps the recipe must handle explicitly, both of which this repo's own tool already
flags:**

1. **Off-grid bars.** `tools/store_gap_scan.py:100-104` documents a real off-grid surviving
   bar (an archive bar at `09:27:14` for hourly data) and its consequence: `missing` becomes a
   **truncation of a fractional step, i.e. a lower bound**, and the tool flags `misaligned`
   rather than presenting an exact-looking number. **[I]** The recipe above must do the same:
   test `grid`-alignment per bar *before* computing `missing`, or the count is wrong.
2. **Duplicates.** `find_gaps()` collapses via `sorted(set(...))`
   (`store_gap_scan.py:257, 337`). **[I]** If duplicates are silently collapsed, a
   double-written bar looks like a shorter series. The duplicate count should be reported, not
   swallowed.

### 5.4 The seam between an archive seed and a live append leg

**[C]** The prior pass measured a **38-bar hole exactly at the SEED/LIVE-APPEND SEAM**
(`configs/default.yaml:201-204`: `2026-08-31 23:00 → 2026-09-02 14:00`;
`store_gap_scan.py:52-58`, `:279-305`). **[C]** The mechanism is understood and recorded:
`kraken-deep-history` writes **whole months** (archive seed), while this repo's live leg
appends at **the very end** of the series (`data.py:1385-1387` `store.upsert`), so the month
boundary is where seed hands over to live. **[C]** `store_gap_scan.py:273-278` states the rule
precisely: *"the **LATEST** such hole is the seam proper … Any **earlier** boundary hole is an
ordinary archive gap and **must not** be labelled the seam."*

**[I] Yes — the seam deserves its own assertion, and here is why it is not redundant with a
global gap check.** A generic "are there gaps?" check has no way to distinguish *an archive
hole* from *the handover between two different data sources*, which is a categorically
different failure: the first is upstream data quality, the second is **this repo's own
join point**, and only the second is something this codebase can fix (by seeding the tail of
the archive month before the live leg takes over, or by recording the handover explicitly).
Reporting them with the same label loses that distinction.

**[I] The cheapest correct assertion, which the repo already has the pieces for:** in
`_read_from_store` (`data.py:~1360-1419`), after `store.read` at `:1402` and **before**
`add_derived_ohlcv_features` at `:1407`, run the §5.3 recipe and assert:

- `n_missing == 0` **or** an explicitly-declared, quantified tolerance (the honest options are
  a count, a fraction of bars, and a max-gap-in-bars — **all three**, since a large total can
  hide one big hole);
- `no gap straddles the final month boundary`, i.e. the seam is asserted **separately and more
  strictly** than archive holes;
- `no off-grid bar` (§5.3 trap 1);
- `bars == expected_bars` when the span is a whole number of intervals.

**[I] Placement matters and there is only one correct place:** before `add_derived_ohlcv_features`
(`:1407`) and before `prepare_episode` (`data.py:1513`). Anywhere later, the features already
exist and carry the defect; `DECISION.md` §14.2 is explicit that *"After z-scoring, a 39-bar jump
is indistinguishable from a 1-bar jump. **No guard in this repo can detect it**, because every
value involved is finite and correctly computed from the rows it was given. **Only the
timestamps know.**"*

### 5.5 Should a gap be an error, a flag, or a repair?

**[I] Recommendation: flag loudly by default, repair **never** in the read path, and make
"fail" a declared opt-in with a documented default.**

Reasoning, one clause per claim:

1. **Repair in the read path is not available to us, and mostly not desirable.**
   **[C]** The store is a sibling's month files read by `store.read`
   (`data.py:1402`); rewriting it means writing to `kraken-market-data`'s store — a different
   repo, reached through a pinned flake input (`flake.nix:7`), so again a lock bump. **[I]**
   Independently: forward-fill across a 39-hour hole fabricates a price series that says "the
   price did not move for 39 hours", and any feature computed over it is confidently wrong in a
   new way. **Interpolating a hole converts a detectable absence into an undetectable lie** —
   strictly worse than the current state, where at least the timestamps are intact.
2. **A hard error is defensible but is a bigger decision than a bug-fix.** **[C]**
   `DECISION.md` §14.3 declined it for exactly this reason: *"Adding a hard failure would have
   broken the store arm on data that is otherwise usable, which is a bigger decision than a
   bug-fix pass should make."* **[M]** My §5.2 measurement supports that: a 200-bar frame with a
   39-bar hole still produces 161 perfectly finite bars and a trainable episode. The data is
   *usable*; it is *mislabelled*. Failing the run discards usable data to fix a labelling
   problem.
3. **A flag is the honest option — but "flag" must be loud in the artifact, not just the
   console.** **[C]** This repo's own precedent is `market_data_store_venue`: a label recorded
   in `models/{ticker}/{model}/config.yaml` *"on the artifact a reviewer already opens"*
   (`configs/default.yaml:212-231`, `AUDIT.md` §2.3). **[I]** A gap report belongs in the same
   place — the **artifact**, not only the log — because the entire failure mode is that the
   defect is invisible *after* the run.
4. **[I] The interaction the brief asked about — why `fillna` is not an option.** The audit's
   standing finding is that **a NaN from absence and a NaN from warm-up are not separable from
   the pattern**. Concretely, in this repo: **[C]**
   `TradingEnvironment._raw_feature_array()` (`environment.py:470-495`) materialises
   `features.ffill().fillna(0.0)`, **[C]** `first_tradable_index` (`features.py:176-190`) is
   **positional** and can only skip a *leading* prefix, and **[C]**
   `POINT_IN_TIME_EXOGENOUS_COLUMNS` (`features.py:171-173`) exists precisely to declare which
   columns' NaNs mean "no reading" rather than "not yet warm". **[M]** §5.2 shows the
   consequences concretely: after the hole, `diff` = 40.0 and `pct_change` = +40.4 %, both
   *finite* — so a NaN-filling policy would not even fire; the damage is in the **values**.
   → **[I] Therefore a gap cannot be `fillna`'d, and cannot even be `fillna`'d usefully: the
   correct repair is not a value, it is a *relabelling of what the window means*.** The fix
   named by `DECISION.md` §14.4 — *"compute features over a **reindexed, gap-filled bar grid**,
   so a window means N **hours** rather than N **rows**"* — is the right shape **only if the
   gap-filled rows are made to be NaN in the observation columns and are then carried by the
   existing `ffill().fillna(0.0)` policy**, so that a window spanning a hole yields NaN →
   repaired → and `first_tradable_index` (if it were time-aware) stops counting them.
   **[I] But that only half-works, for a reason worth stating: `ffill()` across a gap is itself
   a fabrication.** The honest version of "gap-filled grid" is **grid with NaNs + an explicit
   gap flag**, not "grid with carried-forward values". A `gap_bars` / `gap_span_hours`
   observation column makes the hole *visible to the model* instead of silently smoothing it.
   That is a **+1 (or +2) width** cost, and it must be stated per configuration like any other
   (§1.5's method).

5. **[I] What I would actually ship, in order:**
   (a) the §5.3 recipe called from the read path, emitting a `GapReport`;
   (b) the report into the **artifact** (`config.yaml`), alongside `market_data_store_venue`;
   (c) the store seam assertion in §5.4 as the **strictest** part of that check;
   (d) a `store_max_missing_bars` / `store_fail_on_gap` config pair with **flag as the
   default**, so "error" is available without a code change;
   (e) **not** the time-aware windows yet — [I] they are a much larger change (they change what
   `first_tradable_index` means, which `environment.py` and the artifacts depend on) and they
   should follow a measured gap report, so the architect can see how many bars are actually
   affected before committing to the rewrite.

---

## 6. What I could not verify — read this before quoting anything above

| claim | status |
|---|---|
| G-F's `158 bars / 28 gaps / 39h`, `76,563/76,564 bars`, `106 month files` | **[M] NOT re-derived.** Store absent on this host. Quoted only as *"the prior pass measured"*, never as fact. |
| The per-level `timestamp` field in `/0/public/Depth` = "when that level last updated" | **[I]** Kraken's spec types it as an integer and gives unix-second examples; the *semantics* are not documented. |
| A depth-weighted (`exp(−α·|p−mid|)`) order-book imbalance in the primary literature | **[unverified]** Not found. Read §1.3's argument as *absence of evidence*. |
| What HTTP status Kraken returns on a rate limit, and whether it ever sends `Retry-After` | **[unverified]** Not triggered live. Evidence for the negative: Kraken's published OpenAPI spec contains no 429/Retry-After definition, and the live 200 response carries no such header. |
| Whether a retried Kraken `AddOrder` POST violates any duplicate-order protection | **[unverified]** No Kraken documentation found. |
| Whether an imbalance signal has *any* predictive power at a **3600 s** horizon | **[unverified] and unevidenced either way.** CKS's result is at 10 s on an event-summed flow; I found no primary source extending it to the hour. Stated as a gap in the evidence, not a negative result. |
| My §1.5 absolute widths (59/60) | **Not comparable** to `AUDIT.md` §2.4 (52/60/61). Different funding shape. Only the **deltas** transfer. |
| Book autocorrelation and depth figures (§1.3, §1.4) | **[M]** Real, dated, single-pair, single-session, n=10–20. Lags ≥ 3 are noise. Orderings and upper bounds only — **not** parameter estimates. |

---

## 7. Source index

**Primary sources fetched (2026-10-03):**

| # | source | used for |
|---|---|---|
| S1 | `https://docs.kraken.com/api/docs/rest-api/get-order-book` | v1 `/public/Depth`: `count` max 500 / default 100; 3-tuple row with integer timestamp; no `since` |
| S2 | `https://docs.kraken.com/openapi/spot-rest.yaml` (652 243 B) | `/public/GroupedBook` (`depth` 10/25/100/250/1000, `grouping` 1…1000); **no** 429 / Retry-After / rate-limit string anywhere |
| S3 | `https://docs.kraken.com/api/docs/websocket-v2/book` | WS v2 `book`: `depth` enum, `snapshot` default true, delta semantics, "process in sequence", top-10 CRC32 |
| S4 | `https://docs.kraken.com/exchange/guides/websockets/book-checksum-v2` | `qty:0` deletion, depth truncation, `Decimal` decoding, checksum over top 10, "verification is optional" |
| S5 | `https://docs.kraken.com/exchange/guides/rest/ratelimits` | call counter, tiers 15/20/20, decays, `EAPI:Rate limit exceeded`, `EService: Throttled: [ts]` |
| S6 | `https://docs.kraken.com/exchange/guides/general/errors` | per-error retry guidance; "Do not retry" for auth; "check `OpenOrders` before retrying" for Deadline elapsed |
| S7 | `https://docs.kraken.com/llms.txt`, `https://docs.kraken.com/sitemap.xml` | docs index (four 404'd URL guesses resolved from here) |
| S8 | `https://arxiv.org/pdf/1011.6402` (Cont, Kukanov, Stoikov) | OFI definition; **R² 65% vs 32%** (p.3, 16); **10-second** changes (p.22); Level-I focus (p.3); depth humps/gaps/hidden orders (p.6) |
| S9 | `https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html` | full `Retry` signature; idempotent `allowed_methods` default; `backoff_factor·2ⁿ + uniform(0, jitter)`; `RETRY_AFTER_STATUS_CODES` |
| S10 | `https://tenacity.readthedocs.io/en/stable/api.html` | `wait_random_exponential`, `stop_after_attempt`, `retry_if_exception_type`, `before_sleep_log` |
| S11 | `https://pypi.org/pypi/{urllib3,requests,tenacity,backoff,httpx}/json` | versions, licenses, requires-python, release dates (§4.3) |

**Repo files read this pass:** `AUDIT.md` (§0–§7), `DECISION.md` (§14.1–14.6),
`engine.py` (whole), `features.py:46-190, 985-1060`, `data.py:1085-1140, 1360-1420`,
`configs/default.yaml:40-60, 190-240`, `tools/store_gap_scan.py` (whole, 433 lines),
`tools/store_guard.py:1-45`, `justfile:163, 280-355`, `flake.nix:1-30`, `flake.lock`,
and in the sibling `~/Projects/kraken-python` @ `81de597`: `kraken_api/transport.py` (whole,
261 lines), `client.py:85-145`, `manager.py:1-21, 185-206, 355-385`, `models.py:1-80, 189-238,
760-805`, `websocket.py:130-304`, `cli.py:170-200, 400-440`, `export.py:290-303`,
`pyproject.toml:11-15`.

**Measurement scripts (scratch only, `/tmp/opencode/gap/`):** `probe.py` (hole mechanics +
gap-detection recipe), `book_ac.py` (imbalance autocorrelation, 20×4 s), `book_depth.py`
(imbalance by depth N, 10×8 s), `width.py` + `probe3.py` (observation width and
`first_tradable_index`, monkeypatched at runtime), `probe2.py` (urllib3 `Retry` method
filtering against a local 429 server, and the aliasing SE arithmetic). **No repo file was
modified; nothing was committed.**