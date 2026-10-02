# RESEARCH-1 — Gap G1: `microstructure` group is enabled but has no producer

**Author:** researcher1 · **Pass:** Phase 2 (read-only research) · **Date:** 2026-10-02
**Gap:** G1 (`AUDIT.md` §2 🥇) · **Questions:** `AUDIT.md` §5 items 2 (and partly 1)

> Nothing in the source tree was modified. Verified claims are literal lines in
> `/home/seanc/Projects/kraken-trading-bot` or `/home/seanc/Projects/kraken-*`, a live
> HTTP call, or a URL. Anything I could not verify is marked **UNVERIFIED**.

---

## 0. TL;DR for the architect

Three findings change the shape of G1 relative to how `AUDIT.md` frames it.

1. **Nothing needs to be sourced from a third party.** Kraken's keyless
   `/0/public/Depth` was **called live and returns HTTP 200 with no API key**, and it
   reduces to `bid_vol`/`ask_vol` in **two lines of arithmetic**. Verified empirically, not
   read off a doc page.

2. **The producer code already exists in-tree — twice.** `kraken-python` already ships
   `manager.order_book(pair, count)` → an `OrderBook` dataclass of `BookLevel(price, volume,
   timestamp)`, *and* a keyless `wss://ws.kraken.com/v2` client whose `subscribe()` already
   takes a `depth` argument and already whitelists the `book` channel. The bot's
   `engine.py:72` calls it every 60 s and discards the result. **A new sibling repo
   (`kraken-order-book`) would be re-implementing a library that is already a sibling.**
   The missing piece is ~40 lines of *policy* (stamp, sum, append), not plumbing.

3. **`G1 cannot be backfilled. Ever, from any free source.** Kraken's entire public archive
   is OHLCVT — no depth. And Binance Vision has **no depth dataset either** (verified by
   S3 `KeyCount=0` on `bookDepth` and `depth` prefixes), which kills the obvious fallback of
   importing the deep-history pattern from Binance. Historical L2 depth is a paid commodity
   everywhere. **So G1 is structurally forward-only, exactly like G2's funding channel** —
   and it carries G2's exact failure mode: for a training window predating the recorder,
   `order_book_imbalance` will read as a zero-fill. The architect should treat "when does
   the recorder start" as part of the acceptance criterion, not an afterthought.

**Recommendation in one line:** add `bid_vol`/`ask_vol` to an existing configured signal
JSONL via a thin recorder in `kraken-funding-rates` (or `kraken-python`), and widen **two**
tuples in `features.py` — a one-line-of-code change on the consumer side.

---

## 1. G1 evidence re-verified against the current tree

AUDIT's claims all hold. Re-derived independently:

| Claim | Verified at | Status |
|---|---|---|
| Imbalance branch guarded on `bid_vol`/`ask_vol` | `kraken_trading_bot/rl/features.py:1007-1015` | ✅ exact |
| `bid_vol`/`ask_vol`/`order_book_imbalance` appear nowhere in src | repo-wide `grep` (excl. `.venv`, `.git`) | ✅ only `features.py:1007-1014` |
| …except four tests fabricating the columns | `tests/test_rl_environment.py:222-223`; `tests/test_feature_nonfinite_guards.py:306-307, 398-399, 635-636` | ✅ 3 sites / 2 files |
| `_SIGNAL_COLUMNS` omits `bid_vol`/`ask_vol` | `features.py:94-125` | ✅ |
| Merge seam reduces to that allow-list | `kraken_trading_bot/rl/data.py:765-769` | ✅ |
| `order_book` fetched every tick, discarded | `kraken_trading_bot/engine.py:72` | ✅ |
| `order_book` advertised as a `tick()` key | `kraken_trading_bot/strategies/base.py:92` | ✅ |
| only `tick()` reads `candles` + `ticker` | `kraken_trading_bot/strategies/sma.py:68-69` | ✅ |

**Bonus finding AUDIT understates.** `features.py:146-148` already anticipates this work:

> *"A future tick-level tape recorder must use `realized_spread_bps` instead of competing
> for this name."*

Someone already designed around the arrival of a depth/tape producer. G1 is not a
speculative proposal; it is a reserved slot.

---

## 2. The MINIMAL change to make `bid_vol`/`ask_vol` reachable today

**This is the deliverable question. It is two tuple edits, not one.**

### 2.1 What must change in the consumer (both edits are required)

```python
# features.py:94-125
_SIGNAL_COLUMNS = (
    ...
    "bid",
    "ask",
    "bid_vol",      # ADD  — so data.py:765-769 carries them off the JSONL
    "ask_vol",      # ADD
)

# features.py:149
_SIGNAL_BUILDER_INPUT_COLUMNS = ("bid", "ask", "spread", "bid_vol", "ask_vol")  # ADD
```

**Why the second edit is not optional** — this is the part AUDIT.md's "widen
`features.py:94-125`" glosses over. `_add_signals_features` (`features.py:1052-1059`) is:

```python
for col in _SIGNAL_COLUMNS:
    if col in _SIGNAL_BUILDER_INPUT_COLUMNS:
        continue          # <-- builder inputs never become observation columns
    if col in df.columns:
        out[col] = df[col].astype(float).replace(_NON_FINITE_INPUTS_NO_ZERO, np.nan)
```

Adding `bid_vol`/`ask_vol` to `_SIGNAL_COLUMNS` **alone** makes the merge seam carry them —
*and* copies both into the observation as raw columns, two more unnormalised size-scale
columns, in the same way `features.py:139-144` explicitly argues against for `bid`/`ask`:

> *"`bid`/`ask` are raw dollar prices that only exist as *builder inputs* […] whitelisting
> them raw would put two more dollar-scale columns into the observation for information the
> price group already carries."*

Adding them to `_SIGNAL_BUILDER_INPUT_COLUMNS` as well routes them to
`_add_microstructure_features` and nowhere else. `POINT_IN_TIME_EXOGENOUS_COLUMNS`
(`features.py:171-173`) is `frozenset(_SIGNAL_COLUMNS) | frozenset(_SIGNAL_BUILDER_INPUT_COLUMNS)`
so it picks both up automatically — no third edit.

Net effect: `microstructure` goes from a maximum of **1** column (`spread`) to **2**
(`spread` + `order_book_imbalance`), and the observation width grows by **1**, not 3.

### 2.2 The cheapest integration: reuse an *already-configured* channel

This is the decisive simplification. `_SIGNAL_CHANNELS` (`data.py:144-156`) has three
entries, and **both** read legs iterate it positionally:

```python
# data.py:1194-1196 (fetch leg) and data.py:1387-1389 (store leg) — identical
for config_key, signal_file, _producer in _signal_channels(
    extra_features_file, funding_features_file, social_features_file
):
```

Adding a **4th** channel means touching `_SIGNAL_CHANNELS` *and* both call sites *and*
threading a new kwarg through `fetch_ohlc_dataframe` and `read_ohlc_dataframe` — and the
two legs are duplicated, so there is a real drift hazard (`data.py:1385`: *"the two must
never drift"*).

**Therefore: have the depth recorder append `bid_vol`/`ask_vol` into an already-configured
file** (`configs/default.yaml:92` `funding_features_file` is non-null and already merged).
Then the entire consumer-side change is §2.1's two tuple edits — **no `_SIGNAL_CHANNELS`
change, no signature changes, no new config key.**

| Option | Consumer edits | Coupling |
|---|---|---|
| **A.** append into the funding JSONL | 2 tuple edits | depth lives/dies with funding |
| **B.** new `microstructure_features_file` channel | 2 tuple edits + `_SIGNAL_CHANNELS` + 2 call sites + 2 signatures + config key | independently switchable |

Recommend **A** for the minimal build; **B** if the lead wants an independent kill-switch
(justified, because a depth recorder's cadence/availability is operationally independent of
funding).

### 2.3 What still has to be BUILT after that (do not treat §2.2 as done)

1. **A recorder.** Nothing writes `bid_vol`/`ask_vol`. ~40 lines using plumbing that already
   exists — see §4.
2. **A cadence decision.** The merge seam floors to the hour (`data.py:757`) and
   `signal_max_age_hours` defaults to 12 (`configs/default.yaml:126`), so hourly is the
   natural rate. That is already what the funding channel does.
3. **Aggregation semantics.** A `/Depth` response is an *instant*, not a bar, and carries no
   timestamp of its own (each *level* carries its own order-placement timestamp). See §5.2 —
   last-sample-per-hour vs mean-per-hour is a real modelling choice, not a detail.
4. **A timer.** Funding has `systemd/…-funding.timer`; a depth recorder needs its own, or it
   piggybacks.
5. **Acceptance for the cold-start.** See §6 — this is the part most likely to be forgotten.

---

## 3. Question 2 — keyless Kraken order-book/depth sources

### 3.1 Kraken REST `/0/public/Depth` — **KEYLESS. Verified by live call.** ⭐ recommended

<figure>
<b>Verified live, unauthenticated, 2026-10-02:</b>
</figure>

```
$ curl -s -D - "https://api.kraken.com/0/public/Depth?pair=XBTUSD&count=10"
HTTP/2 200
content-type: application/json

error: []
pair key: XXBTZUSD
  asks: 10 levels; first 3 raw = [['84496.70000','0.174',1790980147], ...]
  bids: 10 levels; first 3 raw = [['84496.60000','0.395',1790980147], ...]
  --> bid_vol = 7.599
  --> ask_vol = 7.964
  --> order_book_imbalance = -0.023453061749020124
```

| Property | Value | Source |
|---|---|---|
| Auth | **none** — `security: []` in the OpenAPI spec | [docs.kraken.com/api/docs/rest-api/get-order-book](https://docs.kraken.com/api/docs/rest-api/get-order-book) |
| `count` | 1–**500**, default 100 | same |
| Output | raw L2 levels `[price, volume, timestamp]` as **strings**, `asks`/`bids` arrays | same |
| Reduction cost | **2 lines**, no parsing | see below |
| Rate limit | Spot REST "call counter (max 15–20 depending on tier)" | docs.kraken.com/llms.txt |
| Observed headers | no `RateLimit-*` headers on this 200 response | my own `curl -D` |

```python
bid_vol = sum(float(l[1]) for l in book["bids"])
ask_vol = sum(float(l[1]) for l in book["asks"])
```

**The honest caveat on rate limits:** I could not locate the canonical Kraken rate-limit
page — four candidate URLs 404'd (`/hc/en-us/articles/360000426082-api-rate-limits`,
`…/360000446646-…`, `docs.kraken.com/api/docs/guides/rest-ratelimits`,
`docs.kraken.com/api/docs/guides/rest-rate-limit`). The 15–20 figure is Kraken's own
summary in `llms.txt`, so it is first-party but **the exact page path is UNVERIFIED**. This
matters little here: at hourly cadence a single `Depth` call is far inside any plausible
tier. The sibling's own fixed `min_interval` throttle
(`kraken-python/kraken_api/transport.py:181-186`) is the binding constraint anyway.

### 3.2 Kraken WS v2 `book` channel — **KEYLESS**, better data, more machinery

| Property | Value |
|---|---|
| Endpoint | `wss://ws.kraken.com/v2` — listed as **Public Data** (auth endpoint is the separate `ws-auth.kraken.com/v2`) |
| `depth` | 10 / 25 / 100 / 500 / **1000**, default 10 |
| Snapshot + incremental `update`, CRC32 `checksum` over top 10 each side |
| Docs | [docs.kraken.com/api/docs/websocket-v2/book](https://docs.kraken.com/api/docs/websocket-v2/book) |
| Cost | a long-lived socket + book maintenance + checksum validation + reconnect/backoff |

At `depth=1000` this is a genuinely richer source than the REST snapshot. But for
`bid_vol`/`ask_vol` it buys nothing extra — the sum over 1000 levels instead of 10 is a
*materially different number*, so if you take this route you must **pick the depth once and
record it**, because the feature is not comparable across a depth change. For a first
implementation the 2-line REST sum is the better trade.

### 3.3 Kraken WS v2 `level3` channel — **AUTHENTICATED. Not keyless.** ❌

Discovered while checking WS auth. Unlike `book`, L3 is on a **different host**
(`ws-l3.kraken.com/v2`), its subscribe/unsubscribe payloads mark `token` **required**, and
the docs state plainly:

> *"The `level3` channel is authenticated (i.e. it requires an API token to subscribe) and
> there are restrictions of the number of symbols and the subscription rate."*

Limits: 200 symbols/connection; rate counter 200/s standard, 500/s pro; counter increase per
symbol 5/25/100 for depths 10/100/1000. ([docs.kraken.com/api/docs/websocket-v2/level3](https://docs.kraken.com/api/docs/websocket-v2/level3))

Excluded — it would add a secret to a pipeline that is currently keyless end to end, for a
column that `features.py:1007` reads as a plain scalar.

### 3.4 Kraken public archive — **NO ORDER BOOK. OHLCVT ONLY.** ❌ (the big negative)

This is the single most decision-relevant negative in this report.

Kraken's downloadable history ([support.kraken.com/…/downloadable-historical-ohlcvt-data](https://support.kraken.com/hc/en-us/articles/360000426082-deprecated-api-keys),
"Last updated: September 16, 2026") is:

> *"OHLCVT data […] Each row is: `timestamp, open, high, low, close, volume, trades`"*

Full history `…Kraken_OHLCVT_Full_2026Q2.zip.part00..04` (~2 GB parts, `assets.kraken.com/marketing/institutions/`) plus
quarterly increments. **No depth, no L2, no book snapshots — nothing.**

**Conclusion: `bid_vol`/`ask_vol` history for Kraken does not exist publicly. G1 is
forward-only by the venue's own offering, not by our engineering.** Any plan that assumes
backfill is wrong.

### 3.5 Binance Vision — **NO DEPTH EITHER** (kills the obvious fallback) ❌

The pipeline already has a Binance-archive precedent (`kraken-deep-history`), so "just use
Binance depth like we use Binance klines" is the obvious fallback. **It is not available.**

`binance-public-data` (README, MIT, 2.5k stars) documents exactly three Spot data types —
**AggTrades, Klines, Trades** — and the same three for futures. Verified directly against
the bucket rather than trusting the README:

```
data/spot/monthly/bookDepth/BTCUSDT/   -> KeyCount = 0
data/spot/monthly/depth/BTCUSDT/       -> KeyCount = 0
data/spot/daily/bookDepth/BTCUSDT/     -> KeyCount = 0
data/spot/monthly/klines/BTCUSDT/1h/   -> KeyCount = 2   (control: data exists)
```

**No free historical L2 depth exists for Kraken *or* Binance.** G1 cannot be backfilled by
any keyless route.

### 3.6 `cryptofeed` — best-of-breed Python L2, but **AGPL-3.0**

| | |
|---|---|
| Version | 3.0.1, released **2026-09-27** |
| Repo | [bmoscon/cryptofeed](https://github.com/bmoscon/cryptofeed) — 2,915★, 1 open issue, pushed **2026-10-02** (today) |
| **License** | **AGPL-3.0-or-later** (verified by fetching `LICENSE`: "GNU Affero General Public License … or (at your option) any later version") |
| Python | **>=3.13** (this repo + all siblings declare `>=3.11`) |
| Kraken support | ✅ verified in `cryptofeed/exchanges/kraken.py` |

The Kraken class is genuinely first-class:

```python
# cryptofeed/exchanges/kraken.py
websocket_endpoints = [WebsocketEndpoint('wss://ws.kraken.com/v2', limit=20)]   # public
rest_endpoints = [RestEndpoint('https://api.kraken.com',
    routes=Routes('/0/public/AssetPairs', l2book='/0/public/Depth?pair={}&count={}'))]
websocket_channels = { L2_BOOK: 'book', ... }
```

It already handles `valid_depths`/`book_depth`, book maintenance, **and CRC32 checksum
validation** (`checksum_format='KRAKEN'`). Output is a `cryptofeed.types.OrderBook` whose
`.book` is an `_OrderBook` with `.bids`/`.asks` as ordered `price → size` maps, so:

```python
bid_vol = sum(ob.book.bids.values())
ask_vol = sum(ob.book.asks.values())
```

**Two blockers, and the lead must decide, not me:**
- **AGPL-3.0** is network-copyleft. Note none of the four sibling repos I checked even
  declares a license (no `LICENSE` file, no `license` key in any `pyproject.toml`) — so
  there is no existing permissive grant to conflict with, but there is equally no stated
  policy. Adopting AGPL into a keyless, private, local pipeline is defensible; it is a
  decision to make deliberately, not a default to slip into.
- It would raise the Python floor from 3.11 to 3.13 across a sibling chain for a 2-line
  arithmetic result.

### 3.7 `ccxt` — MIT, actively maintained, and the lowest-friction fallback

| | |
|---|---|
| Version | 4.5.85, released **2026-10-01**; repo pushed **2026-10-02**; 44,220★ |
| License | **MIT** (GitHub API `spdx_id`) |
| Kraken `fetch_order_book` | ✅ verified present — `ccxt/async_support/kraken.py:1002` |

MIT + alive + already the de-facto standard. If the lead wants a maintained library that
sidesteps both the AGPL question and the raw-WS maintenance burden, this is it. Against it:
CCXT returns a normalised `OrderBook` per exchange with no checksum discipline, and it is a
very large dependency to take for two sums that `kraken-python` already supports natively.

### 3.8 Paid / commercial depth history

| Provider | Kraken L2 coverage | Access | Notes |
|---|---|---|---|
| **Tardis.dev** — ✅ **verified** | ✅ Spot `book` channel, snapshots + updates, ≤1000 levels, **from 2019-06-04** to present | **Paid.** No ongoing free plan. 1-month trial = randomly-selected 7–14 recent days. Without a key: CSV for the **first day of each month** only. Prices unpublished. | Python client (`pip install tardis-dev`); 50+ exchanges; tick-level CSV + replay API. **The only verified route to real Kraken L2 history.** |
| Kaiko | L1/L2 feeds listed; **per-venue coverage UNVERIFIED** (coverage explorer is behind login) | Sales-gated, "Request a Trial", 200+ enterprise clients, no public pricing | |
| Amberdata / CoinAPI / dxFeed / crypto Lake | **UNVERIFIED** — not checked in depth | — | |

Tardis is the answer *if and only if* the lead decides to pay. Two facts worth pricing in:
its Kraken symbols **changed on 2026-07-10** (v1 `XBT/USD` before, v2 `BTC/USD` after), so a
backfill spanning that date needs a symbol map; and the free CSV sample (1st of each month)
is enough for a *plumbing spike* but not a training set.

### 3.9 Summary table

| Source | Auth | Rate | Output shape | → `bid_vol`/`ask_vol` | Verdict |
|---|---|---|---|---|---|
| **Kraken `/Depth`** | **keyless** ✅live | REST tier 15–20 | raw L2 `[p,v,ts]` strings | **2 lines** | ⭐ **use this** |
| Kraken WS `book` | keyless | tier-dependent | L2 + updates + checksum | 2 lines after book maint. | Later, if depth>10 matters |
| Kraken WS `level3` | **API token** ❌ | 200/s (500 pro) | individual orders | — | ❌ not keyless |
| Kraken archive | public | — | OHLCVT only | **impossible** | ❌ no depth exists |
| Binance Vision | public | — | trades/klines/aggTrades | **impossible** | ❌ `KeyCount=0` |
| cryptofeed | keyless | tier-dependent | normalised L2 + checksum | 2 lines | ⚠️ **AGPL-3.0**, py≥3.13 |
| ccxt | keyless | tier-dependent | normalised L2 | 2 lines | ✅ MIT, alive, heavy |
| Tardis | **paid** | plan | historical L2 from 2019 | 2 lines | 💰 only backfill route |

---

## 4. Question 1 — where should a depth/tape recorder live?

### 4.1 First: the in-tree option AUDIT's framing misses

AUDIT §2 G1 offers a binary (new sibling vs. new grid in `kraken-market-data`). There is a
**third option that is cheaper than both**, because `kraken-python` already ships the client:

```python
# kraken-python/kraken_api/models.py:213-237   — already in tree
@dataclass(slots=True)
class OrderBook:
    pair: str
    asks: list[BookLevel]
    bids: list[BookLevel]

# kraken-python/kraken_api/models.py:189-207
@dataclass(slots=True)
class BookLevel:
    price: str
    volume: str
    timestamp: int = 0        # 0 when the API omits it
    @property
    def volume_decimal(self) -> Decimal: ...

# kraken-python/kraken_api/manager.py:185-190   — the call engine.py:72 already makes
def order_book(self, pair: str, count: int | None = None) -> OrderBook:
    data = self.client.depth(pair, count=count)     # -> keyless /0/public/Depth
    ...

# kraken-python/kraken_api/client.py:116-121
def depth(self, pair, count=None): return self.transport.public("Depth", params=params)
```

So the entire producer is:

```python
ob = manager.order_book(pair, count=100)
bid_vol = float(sum(l.volume_decimal for l in ob.bids))
ask_vol = float(sum(l.volume_decimal for l in ob.asks))
```

`engine.py:10` and `strategies/base.py:12` already import `OrderBook` from `kraken_api.models`,
so the shape is pinned and in use. **A new `kraken-order-book` repo would re-wrap this.**
If a separate repo is wanted for deployment reasons, it should be a ~150-line CLI *over*
`kraken-python`, not a new library.

`kraken-python` also already has the streaming path, should depth>10 ever matter:

```python
# kraken-python/kraken_api/websocket.py:40, 43, 137-166
DEFAULT_PUBLIC_URL = "wss://ws.kraken.com/v2"     # public, keyless
_CHANNELS = ("ticker", "book", "trade", "ohlc", "executions", "balances", "level3")
def subscribe(self, channel, symbols=None, *, snapshot=True, depth=None, req_id=None): ...
```

`subscribe("book", ["BTC/USD"], depth=100)` already emits a spec-correct v2 message. Nothing
needs writing to stream the book either.

### 4.2 Why a new grid inside `kraken-market-data` is the **worst** of the three

The store's identity is a **shape-preserving read contract** — return *exactly* what the
bot's `candles_to_dataframe` produces. Every method is welded to that:

| Constraint | Location | Consequence for a depth grid |
|---|---|---|
| `_OHLCV_COLUMNS` is a hardcoded 8-tuple | `store.py:53` | a second dataset needs a parallel column tuple, or breaks the contract the bot depends on |
| `read()` always returns `_frame_shape(...)` of those 8 columns | `store.py:146-181` | a depth `read()` returns a *different* frame shape from the same class |
| `upsert()` builds `pd.DataFrame(..., columns=list(_OHLCV_COLUMNS))` | `store.py:214` | silently drops `bid_vol`/`ask_vol` unless rewritten |
| `_validate_interval` restricts to `OHLC_INTERVALS` | `utils.py:16` = `(1,5,15,30,60,240,1440,10080,21600)` | includes **10080 (weekly)** and **21600 (30-day)** — meaningless for a book snapshot; a depth grid would want its own interval domain |
| `update()` duck-types `ohlc(pair, interval, since)` | `store.py:245` | a depth poller would need a parallel `update_depth()` |
| `_candles_to_rows` emits exactly 8 keys from `Candle` | `store.py:67-84` | needs an `OrderBook`→row converter the store has no concept of |

So "another grid" means a second dataset kind, a second interval domain, a second read/upsert
contract, and a widened or duplicated column tuple — inside a module whose README sells
*"Shape-preserving store"* as its headline property. And `INTEGRATION.md` §1 states the
consumer contract is literally *"returns exactly the DataFrame `candles_to_dataframe`
produces"*. Diluting that is a real cost paid for a grid the bot would not use.

### 4.3 The actual answer: **the existing signal JSONL seam**

The store is the wrong shape (§4.2) and a new sibling is mostly re-wrapping (§4.1). What
`bid_vol`/`ask_vol` actually want is a **forward-only append log keyed by wall-clock fetch
time** — which is *precisely* what `kraken-funding-rates` already is. From its own service
unit (`systemd/kraken-trading-bot-funding.service.in`):

> *"The file grows by ~1 line per hour, so it is a log, not state."*

That is the same operational shape as a depth recorder: append one JSON line per interval,
let the hardened merge seam (`data.py:591-842`) do dedup/ffill/freshness/absence-fill, and
let the already-written builder compute the feature.

**Ranking:**

| Option | Cost | Verdict |
|---|---|---|
| Append into the **existing funding JSONL**, recorder lives in `kraken-funding-rates` or a small CLI over `kraken-python` | consumer = 2 tuple edits; producer ~40 lines over existing plumbing | ⭐ **recommended** |
| New sibling `kraken-order-book` | + repo/flake/Nix module/CI, all to re-wrap `kraken-python` | only if deployment isolation is required |
| New grid in `kraken-market-data` | breaks its shape-preserving contract | ❌ |

Note this also makes the two legs structurally incapable of drift: no new channel means no
edits to the duplicated `_signal_channels(...)` calls at `data.py:1194` and `data.py:1387`.

### 4.4 Is a book snapshot even a time-series row? **No — and it doesn't need to be.**

This is the design question the lead should settle explicitly:

* A `/Depth` response is an **instantaneous point-in-time state**, not a bar. It carries **no
  timestamp of its own** — each *level* carries a timestamp, and that is the **order's
  placement time**, not the observation time. Confirmed live: levels at `1790980147`,
  `1790980143`, `1790980143`.
* So a REST poll yields **one `(bid_vol, ask_vol)` pair per fetch instant**, stamped by the
  recorder's own clock.
* Landing it on a bar grid therefore requires an **aggregation choice**, and the honest
  options are *last-sample-per-bar* (what `ffill` + the hour-floor already approximate) or
  *mean/sum-over-fetches-per-bar*.
* Consequence: **store the raw fetch instants, not the aggregated bars.** Aggregate at read
  time. Otherwise the choice is frozen into the data and cannot be revised.

Two related points:
* `data.py:757` floors the signal index to the hour (`signal_df.index.floor("h")`), so with
  the default `signal_max_age_hours: 12` a reading is carried at most 12 hourly bars.
  An hourly recorder therefore satisfies the seam natively; a 60 s recorder would be
  floored to one row per hour anyway, so **poll at a cadence finer than hourly only if you
  intend to change that flooring** — otherwise it is 60× the API calls for one kept row.
* Depth is **not** comparable across a `count` change (500 levels sums to a different number
  than 10). Record the depth in the JSONL alongside the values, or the series silently
  becomes non-comparable across the file's lifetime.

---

## 5. Acceptance criteria the builder/validator should hold to

1. **`order_book_imbalance` actually appears** in `FeaturePipeline.compute` output when the
   seam carries the columns — and **observation width grows by exactly 1** (§2.1's second
   edit prevents +3). `tools/width_check.py` already exists to catch drift.
2. **NaN semantics survive.** `features.py:1008-1015` maps non-finite→NaN and divides by
   `(bid_vol + ask_vol)`. `POINT_IN_TIME_EXOGENOUS_COLUMNS` (`features.py:171-173`) is
   what tells `first_tradable_index` that a NaN here means *absence*, not *warm-up*. Do not
   let a depth NaN truncate the tradable window.
3. **A zero denominator must not appear.** The guard exists and is tested
   (`tests/test_feature_nonfinite_guards.py:622-645`); a `0/0` from an empty book must stay
   NaN.
4. **Cold-start is honest.** If the training window predates the recorder's first line,
   `order_book_imbalance` reads `0.0` flagged by `signal_observed == 0` — G2's exact trap.
   State the recorder's start date next to the result, or do not quote the feature.

---

## 6. Recommendations, in priority order

1. **Do not build a new data-source library.** Kraken `/Depth` is keyless (verified live),
   the client is already in `kraken-python`, and the reduction is 2 lines. G1 is
   ~40 lines of policy, not a sourcing project. This is the cheapest gap in the audit.
2. **Do the two-tuple `features.py` edit first**, and validate the +1 width. It is
   independently reviewable, tests green in minutes, and de-risks everything after it.
3. **Land the recorder into the existing funding JSONL** (option A in §2.2) unless an
   independent kill-switch is required.
4. **Decide the AGPL question explicitly or just don't use cryptofeed.** If MIT-and-alive
   matters more than minimalism, `ccxt` is the pick. Neither is necessary.
5. **Budget for a paid Tardis subscription only if a trained model must consume real L2
   history.** There is no free backfill for Kraken *or* Binance depth — that is a venue-level
   fact, not a gap in our tooling. Do not let a plan assume otherwise.
6. **Consider folding G1 and G2 together.** Both are forward-only, both land on the same
   JSONL seam, and both share the zero-fill cold-start caveat. A single "record what Kraken
   publishes about the perpetual" recorder would serve both.

---

## 7. Honest gaps in this research

* **Kraken REST rate-limit page path: UNVERIFIED.** Four URLs 404'd; the 15–20 figure is
  Kraken's own `llms.txt` summary. Immaterial at hourly cadence, but do not quote it as a
  documented tier without re-checking.
* **Kaiko / Amberdata / CoinAPI / dxFeed / crypto Lake per-venue Kraken L2 coverage:
  UNVERIFIED** — behind sales logins. Only Tardis's Kraken coverage was positively verified.
* **Tardis subscription prices: not published** (verified as unpublished, not guessed).
* **cryptofeed 2.x Python floor** — I verified 3.0.1 requires ≥3.13; I did not audit whether
  an older 2.x supports the repo's ≥3.11 floor. If the lead wants cryptofeed's checksum
  validation without the AGPL/py-floor cost, that is the one open question worth answering.
* I did not measure `order_book_imbalance`'s **predictive** value — out of scope here, and per
  the repo's own `model-matrix` skill, no single backtest number should be quoted before a
  matrix run.

---

**RESEARCH COMPLETE**