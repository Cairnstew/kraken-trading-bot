# RESEARCH-1 — CAND-1: market microstructure, and what `/0/public/Trades` actually buys

**Pass:** audit-pipeline-1002 · **Role:** Researcher 1 (research only; no source/config/test files modified)
**Gap:** CAND-1 (`AUDIT.md` §3) + the G2-R2 refutation in §2
**HEAD at start:** `ddd293f` · **Measured against:** live Kraken Spot REST, keyless, 2026-10-02 ~12:51–14:10Z

> **Slot reuse.** This file previously held the *prior* pass's G1 research
> (signal seam), committed at `444fe1f`. The same slot was reused this run —
> `RESEARCH-3.md` was overwritten identically at `ddd293f`. Prior content is
> still in git history; nothing was deleted.

---

## 0. The answer in five lines

1. **`/0/public/Trades` is keyless, paginable, and reaches each pair's entire listing
   history** — XBT/USD to 2013-10-06, ETH/USD to 2015-08-07, SOL/USD to 2021-06-17.
   Measured, not remembered. The floor is per-pair listing, *not* a retention clamp.
2. **It is a strict superset of the OHLCV frame.** A fully-paged 1-hour ETH/USD bar,
   deduplicated on `trade_id`, reproduces the candle's `volume`, `count` and `vwap` with
   **delta exactly 0.0000000000**. There is no join-key, unit, or basis problem.
3. **6 of the 7 derived features the audit named are point-in-time** (present on bar 0, zero
   marginal warm-up) and need nothing but `/public/Trades`.
4. **The 7th — realized spread — does not work on Kraken spot at any resolution I tested.**
   Roll is undefined on 13/16 one-minute bars and absurd where defined; Corwin-Schultz is
   negative (floored to 0) on 11/15. The quoted spread is **0.47 bps median**; the classic
   estimators assume a market where that is large. Honest answer: **drop it**, or take it
   forward-only from `/public/Spread` where it is *measured*, not estimated.
5. **`/public/Spread` ignores `since` entirely** — stronger than the audit's "bounded". It is
   not backfillable at all. So the answer splits: **tape = historical, spread + depth =
   forward-only recorder.**

---

## 1. Q1 — What is on `/0/public/Trades`?

### 1.1 Documented contract

From `https://docs.kraken.com/api/docs/rest-api/get-recent-trades` (fetched 2026-10-02):

| Property | Value |
|---|---|
| Auth | `security: []` — **no API key, no signature, no tier** |
| Row shape | `[<price>, <volume>, <time>, <buy/sell>, <market/limit>, <misc>, <trade_id>]` |
| `buy/sell` | `b` / `s` (abbreviated — **not** `buy`/`sell`) |
| `market/limit` | `m` / `l` (abbreviated) |
| Page size | `count`, 1–1000, default 1000 |
| `last` | *"ID to be used as `since` when polling for new trade data"* |
| `since` | documented `type: string`, example `'1616663618'` |

### 1.2 Two traps the docs understate, both measured

**Trap 1 — `last` is in NANOSECONDS.** Measured: `last = 1790945549598264546`;
`/1e9 = 1790945579.934603` = 0.065 s before the `Date` header. So the cursor round-trips
**only if you multiply by 1e9 on the way back in**. `kraken-python`'s
`manager.recent_trades` returns it as a bare `str` (`manager.py:198`) and its own docstring
implies seconds. Passing it back verbatim lands you in 1970 and re-fetches the pair's
listing date.

**Trap 2 — rows are 7 fields, not 6.** `Trade.from_public_row` (`models.py:374-385`)
reads indices 0-5 and **silently drops `trade_id`** — the single field a backfiller needs
for idempotent dedup. Measured: 3656 rows collected across 4 pages carried only 3653 unique
`trade_id`s (3 boundary duplicates), which inflated derived volume by exactly 7.278.
Deduplicating on `trade_id` closed the gap to **0.0000000000**. This is the only
correctness defect on the whole path.

### 1.3 Retention — measured, exhaustively

Probing `since` at ages 1 h → 12 y against live XBTUSD:

| Age | Rows | Oldest returned | Reached `since`? |
|---|---|---|---|
| 1 h … 30 d | 1000 | as requested | yes |
| 1 y / 2 y / 3 y | 1000 | as requested | **yes** |
| 4 y … 15 y | 1000 | as requested | **yes** |
| `since`=2013-01-01 | 1000 | 2013-01-01 01:44 | yes |
| `since`=2011-06-01 | 1000 | **2013-10-06 21:34:16** | clamped |
| `since`=2012-06-01 | 1000 | **2013-10-06 21:34:16** | clamped |
| `since`=2013-10-07 | 1000 | 2013-10-07 20:50 | yes |

`since` is honoured exactly; 2011 and 2012 both clamp to the *same* instant, which is the
archive floor for XBTUSD. Crucially the floor is **per-pair listing date, not a retention
window** — every pair clamps to its own launch:

| Pair | Oldest trade on record | Pair | Oldest trade on record |
|---|---|---|---|
| XBTUSD | 2013-10-06 21:34:16 | ADAUSD | 2018-09-28 13:25:00 |
| ETHUSD | 2015-08-07 14:03:25 | DOGEUSD | 2019-12-19 18:20:38 |
| XRPUSD | 2017-05-18 15:46:56 | DOTUSD | 2020-08-18 17:02:07 |
| | | SOLUSD | 2021-06-17 15:32:57 |

**Answer: Trades does NOT share the ~721-bar OHLC ceiling. It has no practical ceiling at
all** — it goes back to the beginning of the market. This is the single most consequential
measured fact in this document.

### 1.4 Rate limits — documented *and* measured

Documented (`https://docs.kraken.com/api/docs/guides/spot-rest-ratelimits`): tier-based call
counter. **Starter (keyless) max 15, decay −0.33/sec.** Ledger/trade-history calls cost 2;
all other calls cost 1 — so `Trades`/`Spread`/`Depth`/`OHLC` are all cost **1**.

Measured, cache-busted with unique `since` values:

| Probe | Result |
|---|---|
| 30 calls, no sleep, same URL | 30/30 OK — **Cloudflare served these** (`CF-Cache-Status: HIT`, `max-age=2`) |
| 25 calls, unique `since` | 25/25 OK |
| 60 calls, unique `since`, 0.1 s apart | **throttle at call 31** |
| repeated burst | throttle at call **30**, **30** (reproducible) |
| sustained 1 req/s × 20 | **20/20 OK** |
| sustained 0.5 req/s × 20 | **20/20 OK** |
| after 30 s idle | clean |

**Verbatim throttle response — and it is a trap:**

```
HTTP/2 200        <-- NOT 429
{"error":["EGeneral:Too many requests"]}
```

Three consequences:

- `kraken-python` gets this right: `transport._request` checks the `error` array on a 200 and
  `_RATE_LIMIT_MARKERS` includes `"EGeneral:Too many requests"` → raises `RateLimitError`
  (`transport.py`, `errors.py`). **No fix needed there.**
- But `KrakenTransport.__init__` defaults `min_interval=0.0` and its docstring says
  *"~15-20 calls/s depending on tier"* — **that figure did not reproduce.** Measured ceiling
  is ~30 burst, safe sustained ~1 req/s. A backfiller must set `min_interval` explicitly
  (~0.8–1.0) or it will be throttled silently on a 30-call page burst.
- `manager.recent_trades` has **no retry**. `CAND-4`'s finding (no retry/backoff on the read
  path) applies verbatim to any tape backfiller.

### 1.5 Backfill cost — priced

`OHLCV.count` reconciles exactly with the tape (§2.1), so trades/day is one call per pair
from the OHLCV frame — no need to page the tape just to size the job:

| Pair | trades/day | pages/day | 30 d @1 rps | 30 d @3 rps | 90 d @3 rps | 365 d @3 rps |
|---|---|---|---|---|---|---|
| XBTUSD | 111,666 | 111.7 | 55.8 h | 18.6 min | 55.8 min | 3.8 h |
| ETHUSD | 54,340 | 54.3 | 27.2 h | 9.1 min | 27.2 min | 1.8 h |
| XRPUSD | 44,029 | 44.0 | 22.0 h | 7.3 min | 22.0 min | 1.5 h |
| SOLUSD | 40,907 | 40.9 | 20.5 h | 6.8 min | 20.5 min | 1.4 h |
| LTCUSD | 17,741 | 17.7 | 8.9 h | 3.0 min | 8.6 min | 36 min |
| ADAUSD | 14,525 | 14.5 | 7.3 h | **2.4 min** | 7.3 min | 30 min |
| DOGEUSD | 13,377 | 13.4 | 6.7 h | 2.2 min | 6.7 min | 27 min |
| DOTUSD | 12,133 | 12.1 | 6.1 h | **2.0 min** | 6.1 min | 25 min |

**30 days of full-depth tape for ADAUSD is a 2.4-minute job.** This is a minutes-scale
backfill, not a project.

**Stated honestly:** this backfills *features*, not *bars*. CAND-3's ~721-bar training
ceiling is untouched by any of this — `train` still only gets 721 OHLCV bars. The tape makes
microstructure features available at any depth; whether a model can *train* on them is
CAND-3's question. Two of them interact: a tape-derived store keyed on the tape's own
timestamps could in principle carry a feature frame deeper than 721 bars, which would
interact with CAND-3's store work rather than duplicate it.

---

## 2. Q2 — Which derived features are worth building?

### 2.1 First, the load-bearing fact: the tape *is* the OHLCV frame

Full pagination of ETH/USD bar `2026-10-02 12:00Z`, deduplicated on `trade_id`:

| Quantity | From tape | From candle | Delta |
|---|---|---|---|
| `volume` | 2681.28626882 | 2681.28626882 | **0.0000000000** |
| `count` | 3653 | 3653 | **0** |
| `vwap` | 2753.315371 | 2753.31 | exact at 2 dp |
| `open` | 2745.72 | 2745.72 | exact |
| `high` / `low` / `close` | 2765.75 / 2738.20 / 2755.00 | identical | exact |

Every derived feature below is therefore **consistent by construction** with the frame the
RL pipeline already loads. There is no basis, no unit conversion, no join key — the trade's
timestamp and the bar's timestamp are the same integer.

### 2.2 The feature table

Measured on the same bar: `n=3653`, `V=2681.2863`, `vwap=2753.3154`,
`O=2745.72 H=2765.75 L=2738.20 C=2755.00`, quote volume 7,382,426.70 USD.

| # | Feature | Exact formula | Look-back | Warm-up | Measured |
|---|---|---|---|---|---|
| 1 | `signed_volume_imbalance` | `(Σ_{b}v − Σ_{s}v) / (Σ_b v + Σ_s v)` | none | **PIT** | **+0.008434** |
| 2 | `vwap_close_gap` | `close / vwap − 1` | none | **PIT** | **+0.00061185** |
| 3 | `trade_size_p50/p90/p99` | quantile of bar trade volumes | none | **PIT** | 0.0295 / 1.7892 / 12.3312 |
| 4 | `trade_size_tail_ratio` | `p99 / p50` | none | **PIT** | **418.2×** |
| 5 | `kyle_lambda` (per bar) | `Σ(p−p̄)·(q·v) / Σv²` , `q=+1/−1` | none | **PIT** | **−1.300e−02** |
| 6 | `large_trade_fraction` | `Σ{v : v > 5·p50} / V` | none | **PIT** | **0.975688** |
| 7 | `amihud_illiquidity` | `\|close/close₍ₜ₋₁₎ − 1\| / (V·vwap)` | 1 bar | 1 bar | quote-vol 7.38e6 |
| 8 | `realized_spread` | *see §2.3* | — | **FAILS** | see below |

**6 of 7 build, and 6 of 7 are point-in-time.** That is the answer to the question that
mattered: **a point-in-time microstructure column can be present on bar 0.** Only `amihud`
costs one bar, which is dominated anyway.

### 2.3 The honest negative: realized spread does not work here

The audit lists "realized spread" first among the Trades-derived families. It is the one
that **fails**, and the reason is structural, not a data gap.

The *effective* spread `2·|p − m|/m` needs a mid `m`. **Trades has no book** — it gives
price, volume, side, type. And the mid is only available from `/public/Spread`, which
(§3.2) **ignores `since`**. So the effective spread has no historical route.

The two tape-only substitutes were tested at both 1-hour and 1-minute resolution:

**Roll (1984)** — `S = 2·Var(Δp) / −Cov(Δp, Δq)`, `q` = trade sign, which Trades *does*
provide:

| Resolution | Bars where `Cov(Δp,Δq) < 0` (defined) | Value where defined |
|---|---|---|
| 1 h × 3 | **0 of 3** | undefined everywhere |
| 1 min × 15 | 2 of 15 | **+260,551 bps** and **+13,450 bps** (2600 %, 134 %) |

**Corwin-Schultz** — `S = 2(e^α−1)/(1+e^α)` from the tape's own per-bar H/L:

| Resolution | Result |
|---|---|
| 1 h × 2 pairs | −106.25 bps, −95.04 bps → floor 0 |
| 1 min × 15 | negative on **11 of 15** → floor 0 |

**Why:** the quoted spread is currently **0.47 bps median** (`/public/Spread`, min 0.04 bps)
on a 0.2759 tick. A 1-hour ETH bar spans ~98 bps, swamping the spread ~100×. Even 1-minute
bars (~2–5 bps) are at the same order as the spread, where these estimators are known to be
unstable. Both estimators assume a market where microstructure noise is a material fraction
of the price variation. Kraken ETH/USD is not that market.

**Recommendation: drop realized spread from the historical feature set.** If the architect
wants a spread column at all, take the **quoted** spread forward-only from `/public/Spread`
(or from `Depth`) — directly measured, no estimator, no failure mode. Note the codebase has
already reserved the name for this at `features.py:96-98`: *"A future tick-level tape
recorder must use `realized_spread_bps` instead of competing for this name"* — that comment
is now shown to be aspirational, and the name is free to use for something else.

### 2.4 What is already in the frame, for free

Two of the four strongest items are **already computed** and just not consumed as
microstructure:

- `vwap_close_gap` ≡ the shipped `vwap_dev` (`data.py:859`). My measured +0.00061185 is
  that column. **Zero new work**; it is already on the allow-list and in the observation.
- `amihud_illiquidity`'s denominator is the quote volume the tape already reconciles.

So the genuinely new, non-duplicating wins are **#1 (signed imbalance), #4 (size-tail ratio),
#5 (Kyle's lambda), #6 (large-trade fraction)** — all point-in-time, all needing only the
tape's `volume` and `side` columns.

---

## 3. Q3 — The three sources, compared honestly

### 3.1 `/public/Trades` — keyless, historical, **the winner**

- Auth: none. Rate cost 1. Shape: `[[price, vol, time, side, type, misc, trade_id], ...]` + ns cursor.
- History: **full, per-pair from listing.** Backfillable today.
- Reduces to a per-(ticker, timestamp) vector: yes — 6 scalars per bar.
- Parsing: **already done.** `Trade.from_public_row` + `to_dict()` exist; the only gap is
  the dropped `trade_id`.

### 3.2 `/public/Spread` — keyless, **not backfillable at all**

This **strengthens** the audit's G2-R2 correction. The audit says *"`since` accepted but
bounded"*. Measured, the truth is stronger and simpler:

| `since` passed (XBTUSD) | Rows | Oldest returned |
|---|---|---|
| *(none)* | 250 | 12:53:41 |
| −30 m | 250 | **12:53:41** |
| −1 h / −6 h / −1 d / −3 d / −7 d / −30 d / −90 d / −400 d | 250 | **12:53:41** |
| −1 h in **seconds** | 250 | **12:53:41** |
| −7 d in seconds | 250 | **12:53:41** |
| 2013-01-01 in seconds | 250 | **12:53:41** |

**`since` is accepted without error and completely ignored**, in either unit. Every response
is the trailing ~250 change-driven samples — **24 s to 98 s of wall clock** depending on the
pair's volatility (XBTUSD 38 s, DOTUSD 98 s, XRPUSD/XLMUSD 24 s). It is change-driven, not
time-driven, so the window is not even a fixed duration.

Also: **`Spread.last` is in SECONDS** (`1790945670`) while **`Trades.last` is in
NANOSECONDS**. Two adjacent endpoints, same parameter name, incompatible units. A generic
paginator over `manager.spread` and `manager.recent_trades` will corrupt one of them.

→ **Requires a forward-only recorder + timer. Cannot be backfilled.**

### 3.3 `/public/Depth` — keyless snapshot, **forward-only, and the only source for the
dormant consumer's actual columns**

No `since` in `client.depth` (`client.py:116-121`). Measured: passing `since=30 days ago` is
**accepted silently and ignored** — Kraken drops unknown params rather than erroring, so this
fails silently rather than loudly.

`count` is honoured and goes deeper than the engine uses:

| `count` | bids | asks |
|---|---|---|
| 1 | 1 | 1 |
| 10 | 10 | 10 |
| 100 (default) | 100 | 100 |
| 500 | 500 | 500 |

Rows are `[price, volume, timestamp]`. **`BookLevel` already models all three** with a
comment *"0 when the API omits it"* — and it does omit it: measured `ask[0]` timestamp
`1790945670` vs `bid[0]` `1790945702`, a 32 s gap **within one supposedly atomic snapshot**.
Book-level timestamps are not coherent; do not build features on them.

Feeding `features.py:586-590` directly (`bid_vol`, `ask_vol` summed over N levels):

| Pair | `bid_vol` | `ask_vol` | → `order_book_imbalance` | quoted spread |
|---|---|---|---|---|
| ETHUSD | 34.007 | 64.211 | **−0.307520** | 0.036 bps |
| ADAUSD | 46954.18 | 28744.17 | **+0.240560** | 1.396 bps |
| DOTUSD | 11229.41 | 11048.74 | **+0.008110** | 3.261 bps |

The imbalances are non-degenerate and cross-pair-varied, so this is genuinely informative —
**but it is a single instantaneous snapshot of resting orders.** The ETHUSD top-1 depth ratio
was **144.6** (one 6.65-unit bid against a 0.046-unit best ask), which is a snapshot of one
resting order, not a market state. And the merge seam will `ffill` it, so the observation
will show a 10-level snapshot held constant for up to `signal_max_age_hours` (default 12).
**That is how a noisy instantaneous quantity becomes a stale-looking feature** — the same
failure mode as CAND-2's inert funding channel, one column-pair narrower.

→ **Requires a forward-only recorder + timer. Genuinely useful, but it is the weaker source.**

### 3.4 Verdict

| | Historical? | Needs recorder+timer? | Yields | Verdict |
|---|---|---|---|---|
| **`/public/Trades`** | **YES — full** | no | 6 PIT scalars + 1 rolling | **Build this first** |
| `/public/Spread` | **no** (`since` ignored) | yes | quoted spread (measured) | add later, forward-only |
| `/public/Depth` | no | yes | `bid_vol`/`ask_vol` → imbalance | lights the dormant consumer, forward-only |

The two forward-only sources are not alternatives to Trades; they are **complements for the
one feature Trades cannot deliver**. That is the clean architectural split.

---

## 4. Q4 — Keyless vs paid

**Everything in this recommendation is keyless. No paid tier, no API key, no account.**

- `/public/Trades`: OpenAPI `security: []`. Verified with **no `API-Key` header at all** —
  every measurement in this document was made unauthenticated.
- `/public/Spread`, `/public/Depth`, `/public/OHLC`: same, all verified keyless.
- Documented Starter tier (keyless) counter: max 15, decay −0.33/s, cost 1 per market-data
  call. Measured ceiling ~30 burst, ~1 req/s sustained safe.

**Priced and rejected — Kraken L3 market data** (the `/api/v3/marketdata` "level3"
channel, the only route to a *true* per-trade mid and hence a *true* effective spread, and
`kraken-python`'s `websocket.py` already lists `level3` in `_CHANNELS`):

- Requires a registered Kraken account and an authenticated WS v2 token
  (`client.get_websockets_token`, which is a **private** call → `RateLimitError`-style
  `AuthenticationError` without credentials).
- Kraken gates L3 behind institutional/negotiated access; it is not a self-serve key toggle.
- Kraken's REST API does **not** bill market data by call on the Spot endpoints, so there is
  no metered price to compare against.
- **Justification for rejecting:** the only thing it buys that Trades does not is a
  per-trade mid, i.e. an effective spread — and §2.3 shows the spread family is not
  actionable on these markets anyway (0.47 bps quoted). Paying institutional-gated access and
  a credentialed runtime dependency to obtain a number that then fails its own sanity check
  is not defensible.

**Also noted, not priced (CAND-3's territory, not mine):** Kraken publishes downloadable
OHLCVT CSV ZIPs — `https://support.kraken.com` article *"Downloadable historical OHLCVT
(Open, High, Low, Close, Volume, Trades) data"*, last updated 2026-09-16, stating data is
available *"from the beginning of each market up to the present"* at 1/5/15/30/60/240/720/1440
minute intervals. Keyless bulk, **venue-consistent**, and it includes a Trades count — the
natural cross-check on any tape backfiller. I did **not** verify the download URL or the
archive's actual coverage; treat that as unconfirmed and let researcher 3 / the architect
price it.

---

## 5. Q5 — Candidate records

| Candidate | Status / last release | License | Auth | Rate limit | Output shape | Parsing needed? |
|---|---|---|---|---|---|---|
| **`/public/Trades`** (recommended) | Kraken production, actively served (OpenAPI v1.1.0, spot-rest.yaml) | venue ToS; no redistribution without agreement | **none** (`security: []`) | cost 1; Starter max 15, decay −0.33/s; measured ~30 burst / 1 rps sustained | **structured JSON**, array-of-arrays + ns `last` | **No** — `Trade.from_public_row` exists; add `trade_id` (1 line) |
| `/public/Spread` | same | same | none | cost 1 | structured JSON, `[time, bid, ask]` + **seconds** `last` | No — `SpreadPoint.from_row` exists |
| `/public/Depth` | same | same | none | cost 1 | structured JSON, `{asks:[[p,v,t]], bids:[...]}` | No — `OrderBook.from_kraken` / `BookLevel` exist |
| Kraken WS v2 `trade` | same; `wss://ws.kraken.com/v2` is the **recommended** protocol per Kraken's own index | same | none for public channels | connection-based | streamed JSON | No — `WsTrade` exists; `kraken-python` already wraps it |
| Kraken WS v2 `level3` | same | same | **token required** (private `GetWebSocketsToken`) | connection-based | streamed JSON | `WsBook` exists |
| Kraken L3 REST | same | same | institutional-gated | cost-per-request | structured JSON | — |
| Kraken OHLCVT CSV ZIPs | active (support article 2026-09-16) | same | none | n/a (bulk) | **CSV in ZIP — needs parsing** | **Yes** — real parsing, est. 40–80 lines |
| `kraken-python` (repo-local wrapper) | 0.3.0, in-repo, last touched 2026-09-24 | in-repo | keyless for public | `min_interval` default **0.0** — must be set | typed dataclasses, `to_dict()`, JSON-safe | **No — already the house style** |
| `krakenex` (PyPI) | 2.2.2, uploaded **2024-07-01** (~2 yr stale) | **LGPLv3** | none | as venue | dict | — |
| `krakenapi` (PyPI, FuturBroke) | 1.0.2, uploaded **2024-02-17** (~2.5 yr stale) | **GPLv3+** | none | as venue | dict | — |
| `ccxt` (PyPI) | **4.5.85, uploaded 2026-10-01** (current) | MIT | none | as venue | dict | — |

**On the PyPI options:** all three are worse than the in-repo `kraken-python` for this job.
The two Kraken-specific wrappers are GPL/LGPL and ~2 years stale; `ccxt` is current but it is
an exchange-**abstraction** layer, so it would erase the exact endpoint-level detail (§1.2's
ns cursor, the 7-field row, the seconds-vs-nanoseconds `last` split) that this feature
depends on, and it would drag a very large dependency into a 6-dependency package. The
house style is a first-party wrapper with typed dataclasses; extending `kraken-python` by
one field is strictly cheaper than any of these.

**On the OHLCVT CSVs:** the only candidate in this table whose output is *raw needing
parsing*. `kraken-python` has nothing for it. If chosen it is a real CSV reader — zip
extraction, per-pair/interval file discovery, header handling, decimal-safe float parse.
Roughly 40–80 lines plus tests. Flagged for completeness; **not** recommended for CAND-1.

---

## 6. Scoring — reduction to what the RL pipeline consumes

The brief asks me to score partly on how cheaply this reduces to a per-ticker,
per-timestamp scalar or small vector. Measured:

| Candidate | Reduces to | Parsing cost | Verdict |
|---|---|---|---|
| `/public/Trades` | **6 PIT scalars + 1 rolling per (ticker, bar)** | **0 lines** — typed models exist; add `trade_id` (1 line) | **A+** |
| `/public/Depth` | 2 scalars (`bid_vol`,`ask_vol` → 1 imbalance) | **0 lines** — `OrderBook` exists | **B** (forward-only, snapshot-noisy) |
| `/public/Spread` | 1 scalar (quoted spread, bps) | **0 lines** — `SpreadPoint` exists | **C** (forward-only, `since` ignored) |
| WS v2 `trade` | same as Trades, streaming | **0 lines** — `WsTrade` exists | **B** (streaming is a different program; no history) |
| OHLCVT CSVs | 7 columns/bar | **40–80 lines, new code** | **C** |

**Explicitly: none of the three REST endpoints need parsing.** Every one is structured JSON
with a typed model already in `kraken-python`. The only code delta is adding the dropped
`trade_id` field to `Trade` — one line in `models.py`, one in `to_dict()`. That is the
whole reason Trades wins, and it is worth being blunt about: the audit's framing ("a richer
feature family") undersells it. The cost is not "parse a raw feed", it is "read one more
field off a dataclass that already exists".

### The consumer-side contract, verified

I traced the exact wiring, because "zero consumer-code change" is the load-bearing claim:

- `features._add_microstructure_features` (`features.py:577-590`) reads `spread` / `bid`,`ask`
  / `bid_vol`,`ask_vol` **off the dataframe** — not off the signals allow-list.
- `data.merge_extra_features` **allow-lists**: `available_cols = [c for c in _SIGNAL_COLUMNS
  if c in signal_df.columns and c not in _SIGNAL_FRESHNESS_COLUMNS]` (`data.py:609-613`).
  **A producer emitting `bid_vol`/`ask_vol` through the merge seam has them silently
  dropped.**
- `features._SIGNAL_COLUMNS` (`features.py:46-77`) currently contains `spread`, `bid`, `ask`
  — and **`bid_vol` / `ask_vol` are absent.** Confirms the audit's grep result. The gate
  itself is `features.py:586-590`.
- **Adding a name to `_SIGNAL_COLUMNS` does two things at once**, which is the cheap part:
  1. the merge seam forwards it to the frame;
  2. it is automatically swept into `POINT_IN_TIME_EXOGENOUS_COLUMNS`
     (`features.py:123-125`, defined as `frozenset(_SIGNAL_COLUMNS) | _SIGNAL_BUILDER_INPUT_COLUMNS`),
     which `first_tradable_index` (`features.py:128`, windowed filter at `:150-152`) **excludes
     from the warm-up gate**.
- So the new signed-imbalance / size-tail / Kyle / large-trade columns, added to
  `_SIGNAL_COLUMNS`, are declared point-in-time **automatically** — no second edit, and no
  risk of a warm-up gate moving from 24 to 720 (the exact regression documented at
  `features.py:132-148`).

**Precedent to copy:** `data.add_derived_ohlcv_features` (`data.py:812-873`) is already the
in-repo pattern for exactly this — presence-gated, writes on a copy, derives from columns the
frame already carries, called on **both** read legs (`data.py:1037` and `data.py:1196`), and
documented as *"presence-gated, not required-column, exactly like the microstructure
builder"*. A tape-derived builder mirrors it line for line.

**One caveat on the seam:** `merge_extra_features` floors both indices to the hour
(`data.py:603`, `data.py:625`) and forward-fills under an age bound. A **tape** producer is
naturally per-**trade**-timestamp, not per-hour. Either aggregate to the bar interval in the
producer (recommended — then it is per-(ticker, bar), point-in-time, and needs no carry at
all), or let the floor do it and accept the `signal_max_age_hours` carry. **Producing one
record per bar is strictly better** and removes the carry question entirely.

### Costs the architect should price

| Item | Cost |
|---|---|
| `Trade` gains `trade_id` | 1 line in `models.py` + 1 in `to_dict()` |
| `_SIGNAL_COLUMNS` allow-list entries | 1 line each; auto-marks point-in-time |
| Ticker-recorded `bid_vol`/`ask_vol` (Depth leg) | forward-only; needs recorder + timer |
| `KrakenTransport(min_interval=…)` for the backfiller | **must be set** — default `0.0` and measured ceiling is ~30 burst |
| Retry on `RateLimitError` | `manager.recent_trades` has none (same defect as CAND-4) |
| A `systemd.user` timer | audit §4.8: the repo has exactly one timer, it is funding-only, it is **absent from `nix/module.nix`**, and it is **not installed on this host** — so the timer pattern is not yet proven here |
| A new sibling repo | `kraken-funding-rates` is the working template: 8 modules, 3 test files, `pyproject.toml` + `flake.nix`, 3 commits |

---

## 7. Recommendation

**One gap, two sequenced halves. The first half is minutes of work and needs no timer.**

**Phase 1 — backfillable, keyless, zero new parsing.** A `kraken-tape` sibling (or a
`microstructure` builder beside `add_derived_ohlcv_features`) that pages
`/public/Trades` with a `trade_id`-deduped cursor walk, aggregates **per bar interval**,
and emits the six point-in-time columns:

`trade_signed_imbalance`, `trade_size_p50`, `trade_size_p90`, `trade_size_p99`,
`trade_size_tail_ratio`, `kyle_lambda_bar`, `large_trade_fraction`
(+ `amihud_illiquidity`, 1-bar warm-up).

Four of these are new information; `vwap_close_gap` ≡ the already-shipped `vwap_dev`.
Backfill 30 days in **2–19 minutes per pair** depending on volume. No timer, no key, no
credential, no new dependency. It works today against `HEAD`.

**Phase 2 — forward-only, only if a spread/depth column is wanted.** A recorder + timer
writing `spread` (from `/public/Spread`, quoted and directly measured) and `bid_vol`/`ask_vol`
(from `/public/Depth`, summing N levels) so the **existing dormant consumer at
`features.py:586-590` lights up with zero consumer-code change** — which is CAND-1's actual
headline claim. Cost: a timer the repo has not yet proven it can install, plus a real staleness
discipline (10-level snapshots ffilled up to 12 h will look stale; sample frequently or set a
tighter `signal_max_age_hours` for that channel).

**Do not build:** realized spread (fails, §2.3), and any of the paid/gated L3 routes (§4).

**The strongest single argument, if only one line survives this document:** the trade tape
reconstructs the OHLCV candle's `volume`, `count` and `vwap` to **zero** error, so
microstructure features derived from it are consistent with the training frame by
construction — and the consumer for them is already written, already gated on column names,
and currently fed nothing.

---

## 8. Live checks I could not complete

| Check | Status |
|---|---|
| Behaviour of `/public/Trades` under a *very* old `since` > 15 y, e.g. 2013-09 for a pair listed 2013-10 | clamped to listing date; consistent, not separately re-confirmed per-pair beyond the 7 pairs in §1.3 |
| Per-pair listing floors for pairs outside the 7 probed | inferred from the clamp being per-pair; not exhaustively swept |
| Whether the Starter counter differs when an `API-Key` header *is* present | not tested (no key available); keyless path is what the recommendation uses |
| Kraken L3 exact pricing / eligibility | not obtainable without an account; concluded "institutional-gated, no self-serve price" from the docs index and rejected on §4 reasoning |
| OHLCVT CSV archive URL, coverage and format | article text confirmed; **URL and contents unverified** — flagged to researcher 3 / architect |
| Whether `EGeneral:Too many requests` counter state persists across IPs/processes | single-process single-IP only |
| Multi-resolution feature behaviour across all 8 feature_windows | not swept; §2.3 establishes the direction for the spread family only |

---

*Research only. No source, config, or test file was modified; nothing was committed or
deleted. This file is the only write.*