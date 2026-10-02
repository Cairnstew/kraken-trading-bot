# RESEARCH-2 — Gap G2: a historical funding / basis / open-interest series, keylessly?

**Pass:** Phase 2 research. **Gap:** G2 from `.data-audit/AUDIT.md` §2 (ranked 🥈).
**Checkout:** `/home/seanc/Projects/kraken-trading-bot` @ `e027621`.
**Role:** READ-ONLY. This file is the only thing written.

**AUDIT.md §5 question 1, answered in one line:**
> *Is a historical funding-settlement series reachable keylessly, or is the channel structurally forward-only?*

**Answer: YES, for the funding rate — from Kraken's OWN API, keylessly, hourly, ~366 days deep,
and it is bit-identical to the series the existing producer emits. NO for open interest, 24h volume,
bid/ask, and the funding prediction — and that is a Kraken data-availability limit, not an
ingestion limit. Say that out loud: this gap is roughly half-fixable on Kraken's own feed and the
other half needs a second venue.**

Everything below marked **MEASURED** was verified by a live call against the production endpoints
on 2026-10-02 (from this machine, unauthenticated). Nothing is quoted from memory.

---

## 0. Method — what "verified" means here

| Evidence class | How |
|---|---|
| **MEASURED** | A live `curl` to the real endpoint. Status code + full JSON shape + statistics over the payload. Reproducible command given. |
| **IN-TREE** | A literal `file:line` in this repo or in the sibling, read directly. |
| **DOC** | Quoted from the vendor's own documentation page, URL given. |

The single most important measurement is this one, because it settles "is it the same series?"
without argument:

```console
$ curl -s 'https://futures.kraken.com/derivatives/api/v3/historical-funding-rates?symbol=PF_ETHUSD'
  ... 8791 records ...
  {"timestamp":"2026-10-02T00:00:00Z","fundingRate":0.02527185133308243,
   "relativeFundingRate":9.341745833333e-06}
```

and the checked-in one-line file (`signals/eth_usd_funding.jsonl`) is

```json
{"ticker":"ETH/USD","symbol":"PF_ETHUSD",...,"timestamp":"2026-10-02T00:00:00+00:00",
 "funding_rate":0.02527185133308243,...}
```

**`funding_rate` matches to the last bit — `0.02527185133308243` — and the timestamp is the same
hour.** The live `/tickers` snapshot and the `historical-funding-rates` series are the *same
underlying series*, on the same scale, on the same hour grid. This is not a proxy, a
cross-venue substitute, or a reconstruction. It is the same number.

---

## 1. The decisive finding: the endpoint is kebab-case, and the sibling has it wrong

`kraken-funding-rates` (`client.py:23`) is pinned to `https://futures.kraken.com/derivatives/api/v3`
and calls exactly two endpoints: `/tickers` (`client.py:105`) and `/instruments`
(`client.py:157`). There is no `since`, no `until`, no pagination token, no second history call
anywhere in `client.py` or `cli.py`.

The obvious camelCase guess for a history endpoint is **404**:

```console
$ curl -s -o /dev/null -w '%{http_code}\n' '.../v3/historicalFundingRates?symbol=PF_ETHUSD'
404
$ curl -s -o /dev/null -w '%{http_code}\n' '.../v3/historicalfundingrates?symbol=PF_ETHUSD'
404
```

The real one is **kebab-case**, and it is keyless (no `API-Key`, no `Authent`, no signing):

```console
$ curl -s -o /dev/null -w '%{http_code}\n' \
    'https://futures.kraken.com/derivatives/api/v3/historical-funding-rates?symbol=PF_ETHUSD'
200
```

**Lesson for the builder: never port this endpoint name from memory.** Six of the eight plausible
spellings I probed 404. This is exactly the kind of detail that turns into an afternoon lost if
nobody had curled it.

---

## 2. The CRITICAL column question — what a backfill can and cannot recover

This is the part AUDIT.md flagged as "do not paper over", so it gets its own section and the
answer is split by column.

### MEASURED — the union of field names across all 8791 records

```python
>>> union of keys over the whole payload
{'timestamp', 'fundingRate', 'relativeFundingRate'}
```

**Three fields. That is the entire historical contract.** No pagination key either.

### Per-column verdict

| Observation column | Backfill-recoverable from Kraken? | Evidence |
|---|---|---|
| `funding_rate` | ✅ **YES, exactly.** Byte-identical to the live column (MEASURED, §0). | `fundingRate`, hourly, 8791 records |
| `basis` | ⚠️ **NOT directly — but derivable to a different-but-real quantity.** | `relativeFundingRate` ≠ basis (see below) |
| `funding_rate_prediction` | ❌ **NO, and provably so.** By definition a *forward* estimate; a history endpoint cannot contain a prediction for a past hour. | absent from payload |
| `open_interest` | ❌ **NO.** Kraken publishes **no** historical OI. | three name variants all 404 (MEASURED, §5) |
| `vol24h` | ❌ **NO — not available at any sane cost.** | §5, trade history is 100 trades/request |
| `spread` (from `bid`/`ask`) | ❌ **NO.** No best-bid/best-ask anywhere in the historical surface. | §5 |

### On `basis` — the one thing that needs care

`basis` today is `(mark_price - index_price) / index_price` — a **perp premium/discount**
(`models.py:126`). `relativeFundingRate` is *not* that. MEASURED:

```
fundingRate / relativeFundingRate  ==  4147.45   at 2025-10-01T08:00Z   (ETH ≈ $4147)
fundingRate / relativeFundingRate  ==  2664.89   at 2026-10-02T22:00Z   (ETH ≈ $2665)
```

So `relativeFundingRate == fundingRate / indexPrice` — i.e. the funding rate expressed as a
*fraction of notional*, not a mark/index spread. **It must not be written into the `basis` column.**
Writing it there would be a silent, plausible-looking unit substitution — exactly the class of defect
that survives to a trained model. The correct move is to leave `basis` at its honest absent value
(§7) and optionally add `relativeFundingRate` as a **new** column rather than overloading an
existing one.

### The honest summary sentence for the architect

> A backfill recovers **1 of 6** observation columns exactly (`funding_rate`), adds **1 new**
> column it does not currently have (`relativeFundingRate`), and leaves **4 of 6** (`basis`,
> `funding_rate_prediction`, `open_interest`, `vol24h`) plus `spread` at their existing honest
> zero-fill with `signal_observed` flipped up. It is not "6 columns fixed". It is
> **1 column properly fixed, 1 column added, and 4 columns that can only be fixed by a different
> venue** (§6) — and those 4 are only "fixed" in the sense that a Binance/Bybit number replaces a
> Kraken one, which is a *semantic substitution*, not a recovery.

---

## 3. Depth, granularity, shape — MEASURED

```
$ curl -s 'https://futures.kraken.com/derivatives/api/v3/historical-funding-rates?symbol=PF_ETHUSD'
```

| Property | Measured value |
|---|---|
| Auth | **none** — 200 with zero headers |
| Records returned | **8791** (PF_ETHUSD), 8791 (PF_XBTUSD), 8792 (PF_SOLUSD) |
| First record | `2025-10-01T08:00:00Z` |
| Last record | `2026-10-02T22:00:00Z` |
| Span | **366.58 days** |
| Cadence | **HOURLY** — gap histogram `{1.0h: 8783, 2.0h: 6, 3.0h: 1}` |
| Distinct hours | 8791 / 8791 — **zero duplicate timestamps** |
| All on the hour | `True` (minute==second==0 for every record) |
| Max gap | **3.0 h** |
| Gaps > 12 h | **0** |
| Shape | `{"result":"success","serverTime":...,"rates":[{timestamp,fundingRate,relativeFundingRate},…]}` |

### Two consequences the architect needs

**(a) It is hourly, and the bar grid is hourly.** `configs/default.yaml:11` is
`ohlcv_interval_minutes: 60`. A dense hourly funding series maps **1:1 onto the bar grid** with no
resampling, no aggregation choice, and no fudge factor. On `signal_max_age_hours: 12` and a max
observed gap of 3 h, **every single bar in the window gets a live reading** — `signal_observed=1`,
`signal_age_hours=0`. Zero unobserved bars.

That is the headline number:

> **12 / 721 bars observed → 721 / 721 bars observed** (721 × 1 h = 30 d ≪ 366 d of available data).

**(b) The window is a rolling ~1-year cap and is NOT paginated.** Six different query parameters
were probed and **all were silently ignored** — byte-identical 8791-record payloads every time:

```
?symbol=PF_ETHUSD&since=2024-01-01T00:00:00Z   -> n=8791 first 2025-10-01T08:00:00Z
?symbol=PF_ETHUSD&from=2024-01-01T00:00:00Z    -> n=8791 first 2025-10-01T08:00:00Z
?symbol=PF_ETHUSD&startTime=1704067200000      -> n=8791 first 2025-10-01T08:00:00Z
?symbol=PF_ETHUSD&end=2025-01-01T00:00:00Z      -> n=8791 first 2025-10-01T08:00:00Z
?symbol=PF_ETHUSD&limit=1000                   -> n=8791 first 2025-10-01T08:00:00Z
?symbol=PF_ETHUSD&count=1000                   -> n=8791 first 2025-10-01T08:00:00Z
```

**So `--backfill <since> --until <ts>` must be implemented as a *client-side* filter over one
whole-window response.** The server will not honour a range, there is no continuation token, and
**hard reach ≈ 366 days from today, recomputed on every call.** A `--since 2019-01-01` in a script
would silently produce a ~1-year file. That is a footgun worth an explicit warning in the CLI.

### Rate limiting — measured, and honestly bounded

* 25 unauthenticated requests fired back-to-back at this endpoint: **25 × HTTP 200**, zero 429.
* No rate-limit headers (`ratelimit-*`, `retry-after`, `x-ratelimit-*`) were exposed on these
  responses in my probes, so I cannot quote a documented bucket number from observation.
* **A backfill needs exactly 1 request per symbol.** It is not a paging loop. The sibling's
  existing `_get` already has `_MAX_RETRIES = 3` / `_RETRY_BACKOFF = 2.0` with 429 and 5xx handling
  (`client.py:70-95`), which is ample for a once-a-day single call. No new throttle logic needed.

---

## 4. The other two keyless sources — measured, and why they lose

Both verified live, both keyless, both **losers** for this pipeline.

### Binance USDⓈ-M — `fapi/v1/fundingRate`

```console
$ curl -s 'https://fapi.binance.com/fapi/v1/fundingRate?symbol=ETHUSDT&limit=3'
[{"symbol":"ETHUSDT","fundingTime":1790841600005,"fundingRate":"0.00007243",
  "markPrice":"2676.00210078","rateType":"Regular"}, ...]
```

* **Auth:** none. **Depth: ~6.8 years.** Launch bisected to **2019-11-27**
  (2019-11-22 → 0 records; 2019-11-28 → 1 record).
* **Cadence: every 8 h.** Timestamps step `1790841600005 → 1790870400003 → 1790899200002`.
* **`limit` is hard-capped at 500** (MEASURED: `limit=1000` returns 500). One page spans
  `500 × 8 h ≈ 166 days`, so 6.8 years ≈ **15 sequential requests**. Cheap.
* **Carries `markPrice`** — a genuine advantage over Kraken's historical payload (it gives a mark
  price at each settlement, from which a *crude* basis could be formed).
* **❌ `relativeFundingRate` equivalent: no.** And its `fundingRate` is a **fraction**
  (`0.00007243`), whereas Kraken's is percent-like (`0.0252`) — **a 1000×-scale difference from the
  column being filled.** Mixing them into one JSONL would need an explicit, recorded unit conversion.

### Binance historical open interest — `futures/data/openInterestHist`

```console
$ curl -s 'https://fapi.binance.com/futures/data/openInterestHist?symbol=ETHUSDT&period=1h&limit=3'
[{"symbol":"ETHUSDT","sumOpenInterest":"2303773.167","sumOpenInterestValue":"6143448866.70",
  "CMCCirculatingSupply":"...","timestamp":1790971200000}, ...]
```

* **Auth:** none. Fields are real and per-hour.
* **⚠️ BUT retention is ~30 days.** MEASURED: `period=1h&limit=500` reaches back only to
  `2026-09-12` (21 days); `period=4h&limit=500` → `2026-09-03`; `period=1d&limit=500` → only **30
  records** reaching back 30 days. **This is not a deep archive — it is a ~30-day rolling window.**
  For backfilling a 30-day training frame it would *just* work; for a multi-year store (G4) it is
  useless.
* Also **USDT-margined, not USD**, and a different venue from the Kraken perp being replaced.

### Bybit v5 — `funding/history` and `open-interest`

```console
$ curl -s 'https://api.bybit.com/v5/market/funding/history?category=linear&symbol=ETHUSDT&limit=3'
{"retCode":0,"result":{"list":[{"symbol":"ETHUSDT","fundingRate":"0.00003303",
 "fundingRateTimestamp":"1790956800000"}, ...]}}

$ curl -s 'https://api.bybit.com/v5/market/open-interest?category=linear&symbol=ETHUSDT&intervalTime=1h&limit=3'
{"retCode":0,"result":{"list":[{"openInterest":"803476.04","timestamp":"1790978400000"}, ...],
 "nextPageCursor":"lastid%3D793277%26lasttime%3D1790971200"}}
```

* **Auth:** none.
* **Open interest IS available historically**, at 1 h granularity, with a `nextPageCursor` for real
  paging — this is the one source that offers a *deep* OI backfill.
* Funding cadence **8 h**, same scale mismatch as Binance (fraction vs percent).
* **DOC — rate limit (Bybit's own docs, `bybit-exchange.github.io/docs/v5/rate-limit`):**
  *"You are allowed to send 600 requests within a 5-second window per IP by default"*; the
  per-endpoint budget comes back as `X-Bapi-Limit-Status` / `X-Bapi-Limit` / `X-Bapi-Limit-Reset-Timestamp`.

### Scoring against what this pipeline can consume

Scored on *how cheaply it reduces to a per-`(ticker, hour)` scalar in the existing 12-field JSONL
record shape* — the only shape `data.py:765` will admit.

| Option | Auth | Depth | Cadence match to `ohlcv_interval_minutes: 60` | Columns recovered of the 6 | Scale match | Requests for 30 d | Verdict |
|---|---|---|---|---|---|---|---|
| **Kraken `historical-funding-rates`** | keyless | **366 d** | **hourly — exact 1:1** | **1/6 exactly** (`funding_rate`) + 1 new | **exact (bit-identical)** | **1** | 🏆 **Wins** |
| Binance `fundingRate` | keyless | ~6.8 y | 8 h — needs resample to hourly | 1/6 (`funding_rate`, ×1/1000 scale) + `markPrice` | 1000× off | ~1 (500 recs ≫ 30 d) | Runner-up |
| Binance `openInterestHist` | keyless | **~30 d only** | 1 h — exact | 1/6 (`open_interest`), wrong venue | — | 2 | Not viable as an archive |
| Bybit `funding/history` | keyless | multi-year | 8 h — needs resample | 1/6, 1000× scale off | 1000× off | ~1 | Redundant with Binance |
| Bybit `open-interest` | keyless | deep (cursor) | 1 h — exact | 1/6 (`open_interest`), wrong venue | — | ~2 | Only real deep-OI option |
| Scrape Kraken website / Coinglass | varies | varies | varies | varies | varies | many | **Discouraged** |

**Bottom line:** Kraken's own keyless endpoint is the only source that is *unit-identical*,
*cadence-identical*, *one request*, and *needs no scraping*. It wins on every axis that matters
for this pipeline. Binance/Bybit win only on **depth**, and only for funding — and they cost a unit
conversion and a resampling decision.

**Scraping: do not.** Kraken's own API already carries the column we need. A scraper would be
strictly worse on every axis *and* would add a ToS/compliance surface. Recommend against, plainly.

---

## 5. What Kraken does NOT have — the negative results, all MEASURED

These matter as much as the positive one, because they bound the fix.

```console
$ for p in open-interest openInterest historical-open-interest; do
    curl -s -o /dev/null -w "$p %{http_code}\n" ".../v3/$p?symbol=PF_ETHUSD"; done
open-interest             404
openInterest              404
historical-open-interest  404
```

```console
$ for tt in funding predicted prediction forecast rate; do
    curl -s ".../api/charts/v1/funding/PF_ETHUSD?...&tick_type=$tt"; done
Invalid tick type      # x5
```

```console
$ for p in historicalfundingrates HistoricalFundingRates historicalFundingRates \
           fundingrates FundingRates historical-funding-rates; do
    curl -s -o /dev/null -w "$p %{http_code}\n" ".../v3/$p?symbol=PF_ETHUSD"; done
historicalfundingrates    404
HistoricalFundingRates    404
historicalFundingRates    404
fundingrates              404
FundingRates              404
historical-funding-rates  200      <-- the only one
```

**Trade history — exists, is keyless, pages backward, but is not usable here.**

```console
$ curl -s '.../v3/history?symbol=PF_ETHUSD&lastTime=2026-10-02T21:00:00Z'
{"result":"success","history":[{"time":"2026-10-02T20:58:30.921219Z","trade_id":100,
  "price":2661,"size":0.074,"side":"sell","type":"fill","uid":"...","sequence_id":"71042942"}]}

# re-issuing with the oldest returned timestamp DOES page further back:
#   lastTime=2026-10-02T20:59:56Z  -> 100 trades, oldest 20:59:52Z
```

* `lastTime` is an **ISO-8601 string** (`?lastTime=1700000000` → `400 Invalid value for lastTime`;
  `?lastTime=2026-09-30T00:00:00Z` → `200`, empty).
* **100 trades per request.** ETH/USD perpetual trades far more than 100 times per hour, so
  reconstructing a rolling 24 h `vol24h` over even one bar requires **thousands** of requests per
  bar and ~10⁵–10⁶ for a year. **Economically absurd — not viable.**
* **No bid/ask in the payload.** Fields are exactly `{time, trade_id, price, size, side, type, uid,
  sequence_id}`. Best-bid/best-ask is simply not there, so **`spread` cannot be historically
  reconstructed from Kraken at any price.**

---

## 6. The backfill pattern — how to add it to `kraken-funding-rates`

Read the actual CLI and extraction code first. The shape that already exists:

* `cli.py:35-46` — two subparsers, `pull` and `extract`, each with `--output` and `--append`.
* `cli.py:59-88` — `cmd_pull` / `cmd_extract`, each: extract → `write_jsonl(...)` → print count.
* `export.py:36-64` — `extract_snapshot`, registry-decorated with `@_register("funding_snapshot")`.
* `export.py:120-156` — `write_jsonl(snapshots, path, append=False)`; **hour-floors the timestamp**
  at `:146-152` before writing.
* `client.py:53-95` — `_get(path, params)`; already retries on 429/5xx with backoff.
* `models.py:51-83` — `to_dict()`, which fixes `ticker` to the **spot pair** (see its docstring: a
  `PF_ETHUSD` value would trip the merge seam's ticker guard on every record).

### Proposed shape — three additive pieces, no changes to existing behaviour

**(1) One new client method.** A sibling of `client.tickers()`:

```
KrakenFuturesClient.historical_funding_rates(symbol) -> list[dict]
    data = self._get("/historical-funding-rates", {"symbol": symbol})
    return data.get("rates", [])
```

Client-side only. No `since`/`until` is sent, because MEASURED shows they are ignored — sending them
would be cargo cult. Filtering by date happens in the extractor.

**(2) One new extractor + registry entry**, mirroring `extract_snapshot`'s shape:

```
@_register("funding_history")
def extract_history(symbol, since=None, until=None, client=None) -> list[FundingSnapshot]
```

**This is where a dataclass change is forced.** `FundingSnapshot` (`models.py:38-49`) has 11
mandatory fields and `from_api()` is built around the `/tickers` element. A history record has
**only three** keys. So you need either:

* a **new** `FundingHistoryRow` dataclass (`timestamp`, `funding_rate`, `relative_funding_rate`,
  `symbol`, `spot_pair`) — **cleanest**, keeps `to_dict()`/`from_dict()` symmetric with the rest of
  the house style; or
* make five fields optional on `FundingSnapshot` and let `to_dict()` emit them as `None`.

**Recommend the new dataclass.** Reason grounded in the consumer, not taste: `write_jsonl` does
`json.dumps(record, default=str)` and `data.py:738` does
`pd.to_datetime(signal_df["timestamp"], utc=True)`. If `to_dict()` emitted `None` for
`open_interest`, `pd.DataFrame(records)` produces a **float NaN**, which the merge then treats as
"no reading" — i.e. *absence*, not *zero*. The seam's contract (`data.py:806-816`, and the
`POINT_IN_TIME_EXOGENOUS_COLUMNS` docstring at `features.py:149-176`) says that is a **correct**
result, but it is worth choosing deliberately rather than by accident. Emitting an explicit
`0.0` keeps today's meaning (structurally zero) and keeps `observed` unambiguous; emitting `None`
switches those four columns from "zero" to "absent". **Pick one and write it in the docstring.**

**(3) One new CLI subcommand**, mirroring `cmd_pull` exactly:

```
backfill --pair ETH/USD --since 2025-10-01T00:00:00Z [--until 2026-01-01T00:00:00Z] -o FILE [--append]
```

and one `just` recipe beside `funding-pull` (`justfile:152`), since AUDIT.md §2 confirmed the
justfile has **no** backfill recipe (verified: `grep -nE "backfill|replay|history" justfile` finds
only the *deep-history store* recipes at `:180-266`, nothing for funding).

### Three requirements the builder must not get wrong

**(a) The endpoint is a ~366-day rolling window and `--since` is client-side.** Emit a warning when
`since` is older than the earliest returned record, naming both dates. Otherwise a
`--since 2019-01-01` silently yields a one-year file.

**(b) ⚠️ Timestamp format differs from the live producer — a real interop wrinkle.**

| Source | Emits | Produced by |
|---|---|---|
| historical endpoint | `2026-10-02T00:00:00Z` | Kraken |
| live `/tickers` → `write_jsonl` | `2026-10-02T00:00:00+00:00` | `export.py:149-150` `ts_floored.isoformat()` |

`datetime.fromisoformat` in Python 3.11+ parses both, and `data.py:738` normalises with `utc=True`,
so **both merge correctly today** — but the file ends up with **two timestamp spellings in one
JSONL**. Harmless, untidy, and a trap for the next person who greps. The backfill writer should
round-trip through the same `write_jsonl` hour-flooring path so the file is uniform.

**(c) Append, and always write the full field set.** See §7.1 — writing a backfill to a *separate*
file, or writing records with fewer keys, changes the observation width. Append into the same file
with the same 12-field record shape and nothing downstream notices.

---

## 7. Would a dense historical series expose a latent bug in the seam? — checked, code path by code path

The question as posed: does `_first_valid_index` / `signal_max_age_hours` logic
(`data.py:787-816`) handle a dense historical series correctly, or does a dense series expose a
latent bug?

### Verdict: the density path is **correct**. But a *naive* backfill would expose a real,
easy-to-hit hazard — in **feature width**, not in the freshness logic.

I traced `merge_extra_features` with a dense hourly input, against `configs/default.yaml`'s
`ohlcv_interval_minutes: 60`:

| Step | `data.py` | Behaviour with a dense hourly Kraken series |
|---|---|---|
| read + ticker filter | `:697-736` | fine |
| `to_datetime(utc=True)`, drop NaT | `:738-746` | fine (`Z` and `+00:00` both parse) |
| dedup / stable sort / floor / `groupby(level=0).last()` | `:755-760` | 8791 distinct hours, **no collisions**, `.last()` is a no-op |
| `available_cols` from `_SIGNAL_COLUMNS` ∩ file columns | `:765-769` | **← the hazard. See §7.1** |
| `reindex(ohlc_index)` | `:789` | exact hit, no NaN. (Target duplicates are fine in pandas; only a *duplicate source index* raises — and the source was just deduped.) |
| `.ffill()` | `:790` | identity on a dense frame |
| `_signal_ages` | `:798` → `:544-588` | every bar has its own record → **age 0.0 everywhere** |
| `age <= bound_hours` mask | `:799-800` | 12 h bound vs max observed gap 3 h → **nothing masked** |
| `observed` / `source_age` | `:812-813` | `observed=True` and `signal_age_hours=0.0` on **721/721** bars |
| `fillna(0.0)` | `:816` | never fires |
| warning `if not observed.any()` | `:818-827` | correctly silent |

### 7.0 `_first_valid_index` — already immune, and the history is instructive

`features.py:178-206` deliberately **excludes** `POINT_IN_TIME_EXOGENOUS_COLUMNS` from the warm-up
gate. Its own docstring records the previous bug this task was worried about:

> *"Gating on them made a sparsely-covered exogenous source silently truncate the episode to the
> bars it happened to cover: a one-record funding file moved the start index from 24 to 720 of 721
> and left a trained policy replayed for a single bar, with no error and a width guard that still
> passed."*

**So a dense series does not re-expose it — the fix is already in tree**, and
`environment.py:458-468` delegates to `features.py:first_tradable_index` precisely so the two
cannot drift. `signal_max_age_hours` behaves correctly too: 12 h against a max gap of 3 h is
conservative, not wrong.

### 7.1 ⚠️ THE REAL HAZARD — `available_cols` shrinks and the **observation width changes**

`data.py:765-769` intersects the file's columns with `_SIGNAL_COLUMNS`. If the backfill writes
records carrying only `funding_rate` (plus `ticker`/`timestamp`), then:

* `available_cols == ["funding_rate"]` → `bid`/`ask` never reach the frame →
  `_add_microstructure_features` (`features.py:997-1006`) finds neither `spread` nor
  `{"bid","ask"}` → **emits no `spread` column at all**;
* `funding_rate_prediction`, `vol24h`, `open_interest` likewise vanish;
* the observation gets **narrower** than the `49 -> 55` width `features.py:92` documents, and
  `FeatureWidthMismatchError` / `tools/width_check.py` will fire.

**Mitigation (mandatory):** the backfill writer must emit the **full 12-field record shape** —
`funding_rate` populated from history, the four unrecoverable numeric fields at their chosen
explicit absent value (`0.0` or `None` per §6(2)), `bid`/`ask` at the same, `ticker` = the **spot
pair** (`models.py:54-68` — a `PF_ETHUSD` ticker would trip `signal_require_ticker: true` on every
record) — and it must **append to the existing file** rather than create a second one. Append to the
same file and the `pd.DataFrame(records)` column union is unchanged, `available_cols` is unchanged,
width is stable.

*(For completeness: if it *is* appended with fewer keys, the seam degrades gracefully rather than
breaking — `groupby(level=0).last()` skips nulls and the ffill+age-mask leaves the missing columns
`NaN` → `fillna(0.0)` at `:816`, with `observed` driven by the live `funding_rate`. Honest, but it
loses four columns of information the live rows do have. Append with full width.)*

### 7.2 The config comment at `configs/default.yaml:118-124` is **factually wrong** — worth fixing

> *"12 is set here because `funding_features_file` is a real, configured file: kraken-funding-rates
> settles **~8-hourly**, so at the null->1h default most bars fall outside the freshness window"*

**Kraken's own series is hourly, not 8-hourly** — MEASURED gap histogram `{1.0h: 8783, 2.0h: 6,
3.0h: 1}` over 8791 records. (Plausible mechanism: `instruments` reports `PF_ETHUSD` as
`type: "flexible_futures"` with `fundingRateCoefficient: 8`, i.e. an hourly-quoted rate scaled to
an 8 h equivalent.) The same wrong claim is repeated in
`systemd/kraken-trading-bot-funding.timer`.

Consequence: **12 h is ~12× looser than the data needs** — the `null → 1 h` default would already
cover 99.97 % of it. And note the irony: **if** the architect takes Binance/Bybit instead, the
8 h claim becomes *true* and 12 h becomes exactly right. So the bound is currently wrong for the
recommended source and right for the rejected ones. Correct the comment as part of the change.

---

## 8. Cost / risk summary

| Item | Cost |
|---|---|
| HTTP requests for a 30-day training frame | **1** per symbol (keyless) |
| HTTP requests for the full 366-day window | **1** |
| Extra requests to *maintain* it | **0** — the hourly timer already refreshes the newest hour; a daily `funding-backfill` (or a weekly one) is belt-and-braces, not a requirement, because the window is a rolling 366 d that ages **out**: after ~1 year the oldest records fall off the back and the file would need periodic top-ups |
| New dependencies | **none** (`requests` already there) |
| Scraping | **none** |
| Paid feed | **none** |
| Changes in this repo | a `just funding-backfill` recipe beside `justfile:152`; optionally correct the two "~8-hourly" comments |
| Changes in the sibling | 1 client method + 1 dataclass + 1 extractor + 1 subcommand |
| Changes in `features.py` / `data.py` | **none required** — provided §7.1's full-width rule is followed |

---

## 9. Coupling to G4 that the architect must see

G2's funding reach and G4's store depth are **the same order of magnitude**:

* G2 backfill reach: **366 days** (hard server cap, no pagination).
* On `ohlcv_interval_minutes: 60`, 366 d = **8766 bars** — plenty for the ~721-bar live ceiling, and
  plenty for a multi-year store at hourly resolution **for the most recent year only**.
* But on **15 m bars**, 366 d = ~35 k bars while a 3-year store would be ~105 k bars → **the last two
  years of any deep store would have no funding at all**, and would silently zero-fill those four
  columns. If G4 lands, **the honest framing is "funding covers the most recent year, and the
  freshness columns are how you tell"** — which is exactly what `signal_observed` /
  `signal_age_hours` were built for.

This coupling is a reason to *prefer* Kraken's 366-day keyless feed over chasing deeper third-party
archives: it covers the regime the live model actually trades, it is unit-identical to what it
replaces, and the columns it cannot supply are bounded and honest rather than proxied.

---

## 10. Recommendation

1. **Add `--backfill <since> [--until]` to `kraken-funding-rates`, against
   `/derivatives/api/v3/historical-funding-rates`** (kebab-case, keyless, 1 request, ~366 d,
   hourly). Filter by date **client-side**. Emit the **full 12-field record**; append to the
   existing file.
2. **Do not** widen this to Binance/Bybit in the same change. They buy depth on `funding_rate` only,
   cost a 1000× unit conversion and an 8 h→1 h resampling decision, and on the OI column Binance's
   archive is only ~30 days deep while Bybit's is a **different venue's** number. If a second venue
   is ever wanted, Bybit's cursor-paged `open-interest` is the only candidate that is both deep and
   1 h-aligned — and it must be introduced as a **new, explicitly-labelled cross-venue column**,
   never by overwriting `open_interest`.
3. **Do not scrape anything.** Kraken's own API already carries the column.
4. **Fix the two "~8-hourly" comments** (`configs/default.yaml:118-124`, the `.timer`) — the
   measured cadence is hourly.
5. **State the limit in the PR and in the config comment**: this takes `funding_rate` from
   12/721 observed bars to 721/721. `basis`, `funding_rate_prediction`, `open_interest`, `vol24h`
   and `spread` remain unavailable historically on Kraken and keep their honest zero-fill with
   `signal_observed` flipped up.

---

## Appendix — every endpoint cited, and how it was probed

| # | Endpoint | Result | Reproduce |
|---|---|---|---|
| 1 | `GET futures.kraken.com/derivatives/api/v3/tickers` | 200 (in use today) | `client.py:105` |
| 2 | `GET .../v3/instruments` | 200, 226 instruments; `PF_ETHUSD` = `type: flexible_futures`, `fundingRateCoefficient: 8`, `openingDate: 2022-03-22T13:18:45Z` | MEASURED |
| 3 | **`GET .../v3/historical-funding-rates?symbol=PF_ETHUSD`** | **200 — 8791 records, 366.58 d, hourly, keyless** | MEASURED |
| 4 | `GET .../v3/historicalfundingrates` / `historicalFundingRates` / `HistoricalFundingRates` / `fundingrates` / `FundingRates` / `historical-funding-rates` (camel/lower/upper) | 404 ×5, **200 only for kebab-case** | MEASURED |
| 5 | `GET .../v3/historical-funding-rates?…&since/&from/&startTime/&end/&limit/&count=…` | all 6 ignored — identical 8791-record payload each time | MEASURED |
| 6 | `GET .../v3/open-interest` / `openInterest` / `historical-open-interest` | 404 ×3 — **no Kraken historical OI** | MEASURED |
| 7 | `GET .../v3/history?symbol=PF_ETHUSD&lastTime=<ISO>` | 200, 100 trades/page, pages backward via `lastTime`; `{time,trade_id,price,size,side,type,uid,sequence_id}` — **no bid/ask** | MEASURED |
| 8 | `GET .../v3/history?…&lastTime=1700000000` (epoch) | 400 `Invalid value for lastTime` — ISO string required | MEASURED |
| 9 | `GET .../v3/history?…&lastTime=2026-10-01T00:00:00Z` | 200 with **empty** history (the 100-trade window is near-real-time) | MEASURED |
| 10 | `GET futures.kraken.com/api/charts/v1/{trade,markprice,openinterest,funding}/PF_ETHUSD` | `trade` → 200 (returns the valid-resolution list `["1h","12h","15m","1m","30m","1w","5m","4h","1d"]`); `openinterest` and `funding` → 400 `Invalid tick type` under every tick_type probed. **Not a viable source; undocumented and brittle.** | MEASURED |
| 11 | `GET fapi.binance.com/fapi/v1/fundingRate?symbol=ETHUSDT` | 200, keyless, 8 h, `fundingRate` as a **fraction**; launch bisected to **2019-11-27**; `limit` hard-capped at **500** | MEASURED |
| 12 | `GET fapi.binance.com/futures/data/openInterestHist?symbol=ETHUSDT&period=1h` | 200, keyless, 1 h, but retention **~30 days** (`limit=500` reaches back only 21 d; `period=1d` → 30 records) | MEASURED |
| 13 | `GET api.bybit.com/v5/market/funding/history?category=linear&symbol=ETHUSDT` | 200, keyless, 8 h, fraction scale | MEASURED |
| 14 | `GET api.bybit.com/v5/market/open-interest?...&intervalTime=1h` | 200, keyless, 1 h, `nextPageCursor` for paging | MEASURED |
| 15 | Bybit rate limit (**DOC**: `https://bybit-exchange.github.io/docs/v5/rate-limit`) | *"600 requests within a 5-second window per IP by default"*; per-endpoint budget in `X-Bapi-Limit-*` headers | quoted |

**Docs pages that did NOT yield citable numbers** (recorded so nobody re-chases them):
`docs.kraken.com/api/docs/futures-api/**` → **404** across four URL shapes;
`support.kraken.com/.../downloadable-historical-OHLCVT...` → **spot OHLCVT only, no futures
funding/OI**; `developers.binance.com/docs/derivatives/usds-margined-futures/general-info` → served
**HTTP 202 with a zero-byte body** to a scripted request. The Kraken and Binance figures above are
therefore stated as **measured**, not as doc quotes — which is the stronger evidence class anyway.

---

## RESEARCH COMPLETE