# RESEARCH.md — assembled from RESEARCH-{1,2,3}.md

**Pass:** 2026-10-02 · **Phase:** 2 · **Assembled by:** lead

Three researchers wrote in parallel, one per gap, each to its own file to avoid a write
race on a shared artifact. This file is an **index, a cross-cutting synthesis, and the
lead's independent verification receipt** — it is deliberately NOT a paraphrase of the
1,693 lines below. Cite the per-researcher file and its section for any load-bearing claim.

| Gap | File | Lines | Headline |
|-----|------|-------|----------|
| G1 `microstructure` dead machinery | [`RESEARCH-1.md`](RESEARCH-1.md) | 537 | Producer code **already exists twice in-tree**; consumer is two tuple edits; **un-backfillable from any free source, ever** |
| G2 one-record signal log | [`RESEARCH-2.md`](RESEARCH-2.md) | 587 | Fixable on **Kraken's own keyless API, bit-identical** to the checked-in record — but **1 of 6 columns** |
| G3 no retry/backoff/rate-limit | [`RESEARCH-3.md`](RESEARCH-3.md) | 569 | Retry goes in the **caller** first; `urllib3.Retry` is **structurally blind** to a Kraken rate limit; throttle is **off by default** |

Each file also records what it could **not** verify and its own counter-evidence. Those
sections are load-bearing, not disclaimers — read them before trusting a recommendation.

---

## §1 Cross-cutting findings — the things that only appear when all three are read together

These are the lead's synthesis. None is stated in any single file, and two of them
constrain the architect's choice.

### 1.1 G1 and G2 are BOTH forward-only, and share one cold-start trap
Not a coincidence — it is structural. Kraken's public archive is OHLCVT only, no L2
(RESEARCH-1 §…), and `historical-funding-rates` reaches back **366 days** (RESEARCH-2).
So a recorder starting today leaves every earlier bar reading `0.0` for both features.

> **Consequence for the architect:** "when does the recorder start" is an **acceptance
> criterion**, not a footnote. The diagnostic already exists — `signal_observed` and
> `signal_age_hours` — and the repo's own policy is *absence ≠ neutral* (`data.py:806-816`).
> Neither feature may be gated on `signal_observed`; that is the G1 feature itself.

### 1.2 G1 needs no new library and no new repo
`kraken-python` already ships the producer: `manager.py:185 order_book()`, `client.py:116
depth()` → `transport.public("Depth")`, `models.py:213 OrderBook`, and `websocket.py:43`
already has the keyless `book` channel. A new `kraken-order-book` sibling would be
**re-wrapping a library that is already a sibling**. Scope is ~40 lines of policy.

Both candidate libraries were rejected on evidence, not taste: `cryptofeed` is
**AGPL-3.0-or-later** (LICENSE fetched) and needs Python ≥3.13 vs the repo's ≥3.11;
`ccxt` (MIT) is the hedge but is unnecessary. `urllib3.Retry` is irrelevant here.

### 1.3 G1's minimal change is TWO tuple edits — AUDIT.md says one and is wrong
`features.py:1052-1054` is `for col in _SIGNAL_COLUMNS: if col in
_SIGNAL_BUILDER_INPUT_COLUMNS: continue`. Widening `_SIGNAL_COLUMNS` alone would both
carry `bid_vol`/`ask_vol` off the JSONL **and** copy them raw into the observation as
size-scale columns — the exact thing the adjacent comment (`features.py:139-144`) argues
against for bid/ask.

1. `_SIGNAL_COLUMNS` += `bid_vol`, `ask_vol` — so `data.py:765-769` carries them
2. `_SIGNAL_BUILDER_INPUT_COLUMNS` (`("bid","ask","spread")`) += both — so they stay builder inputs

`POINT_IN_TIME_EXOGENOUS_COLUMNS` is `frozenset(_SIGNAL_COLUMNS) | frozenset(
_SIGNAL_BUILDER_INPUT_COLUMNS)` (`features.py:171-173`), so it needs **no** third edit.
Net: +1 observation column, not +3.

### 1.4 A 4th signal channel is BLOCKED, not merely awkward
`_SIGNAL_CHANNELS` (`data.py:144-156`) has exactly 3 entries and is iterated
**positionally** at two duplicated call sites (`data.py:1194`, `:1387`) that each pass
exactly three values — `data.py:1385` even warns the two "must never drift". Adding a
channel means the tuple + both call sites + 2 signatures + a config key.

> **Cheaper path (RESEARCH-1):** append `bid_vol`/`ask_vol` into the **already-configured**
> funding file (`configs/default.yaml:92`, non-null) — the consumer change is then just the
> two tuple edits. Note the design is already anticipated: `features.py:146-148` reserves
> `realized_spread_bps` for "a future tick-level tape recorder".

### 1.5 G2 is genuinely fixable keylessly — and the match is the SAME series, not a proxy
`GET futures.kraken.com/derivatives/api/v3/historical-funding-rates?symbol=PF_ETHUSD` is
HTTP 200 with no auth, **8,791 records, hourly, 366 days, zero duplicate timestamps**.
Kebab-case; six other spellings 404. The field union across all 8,791 records is
**exactly three fields**. Verified live by the lead — see §2.

### 1.6 G2's real hazard is FEATURE WIDTH, not episode truncation
AUDIT.md's implied risk was that a dense series re-exposes the 2026-10-01 silent defect.
It does **not** — `first_tradable_index` (`features.py:178-206`) deliberately excludes
`POINT_IN_TIME_EXOGENOUS_COLUMNS`, and its own docstring records that exact bug ("a
one-record funding file moved the start index from 24 to 720 of 721 and left a trained
policy replayed for a single bar, with no error and a width guard that still passed").

The hazard that IS live: `available_cols = [c for c in _SIGNAL_COLUMNS if c in
signal_df.columns]` (`data.py:765-769`). A backfill writing **only** `funding_rate` leaves
`bid`/`ask` off the frame → no `spread` → the observation gets **narrower than 49→55** and
`tools/width_check.py` fires. **Mitigation is mandatory: emit the full 12-field record and
append**, so the `pd.DataFrame(records)` column union — and hence width — is unchanged.

### 1.7 G2 recovers 1 of 6 columns exactly. Say so; do not paper over it.

| Column | Recoverable from Kraken history? |
|---|---|
| `funding_rate` | **YES, exactly** — bit-identical, hourly, 366 d |
| `basis` | **NO as basis.** `relativeFundingRate` = `fundingRate / indexPrice` (measured ratio ≈ ETH price), i.e. funding-as-fraction-of-notional, **not** `(mark-index)/index`. Writing it into `basis` is a silent unit substitution. Add as a NEW column. |
| `funding_rate_prediction` | **NO, provably** — a forward estimate cannot be in a history |
| `open_interest` | **NO** — every spelling 404s on Kraken |
| `vol24h` | **NO at sane cost** — trade history is keyless but **100 trades/request**; a 24h roll is ~10⁵–10⁶ requests |
| `spread` (bid/ask) | **NO** — trade payload has no bid/ask at all |

Only Binance/Bybit supply historical OI, and Binance's `openInterestHist` retention is
**~30 days** (measured) on a different venue. Do **not** widen to Binance/Bybit in the same
change: 8h→1h resampling plus a 1000× scale conversion buys depth on one column that is a
different venue's number.

### 1.8 G2 magnitude: 12/721 → 721/721
`configs/default.yaml:11` is `ohlcv_interval_minutes: 60`, so a Kraken backfill maps **1:1
onto the bar grid** with no resampling; max inter-record gap 3 h < the 12 h bound, so every
bar goes live. This is the number the Phase 6 gate must show.

### 1.9 G2's endpoint is NOT paginated — the backfill is a client-side filter
`since`/`from`/`startTime`/`end`/`limit`/`count` are **all silently ignored** (6 probes,
byte-identical payloads); the series is a ~1-year rolling cap. So `--backfill` filters one
whole-window response client-side and must warn when `--since` predates the earliest record.
Cost: **1 HTTP request** — so G3's missing-retry problem is *not* on this path.

### 1.10 G3's placement answer is a supply-chain fact, not a style preference
`flake.nix:6` pins `kraken-python`; `flake.lock` rev `81de5974…` **== local HEAD**. The
`.venv` has it **editable**, so pytest sees a working tree while `just`/`nix` builds from
the lock. An upstream-only fix would therefore be **live under pytest and invisible to
`just bench`** — i.e. it would pass the suite and not reach the actual run path. Only the
caller knows `cursor`/`collected`/`pages`, so "page 4 of 6 raised and pages 1-3 were
discarded" is irreducibly a caller bug. → caller now, upstream separately (needs a
`flake.lock` bump).

### 1.11 G3 found the throttle is OFF — a shared prerequisite for G1/G2 recorders
`transport.py:108 min_interval: float = 0.0`, guarded at `:181 if self.min_interval <= 0`,
and `auth.py:73 ... or 0.0`; both legs construct via `from_env()` (`data.py:1174-1177`,
`:1359-1362`). So there is **zero deployed request spacing** today. "Make upstream throttling
adaptive" is a no-op under this config. Any recorder G1/G2 introduces shares this exposure.

### 1.12 `except RateLimitError` alone is silently wrong — three distinct paths
1. `EAPI` → `RateLimitError` (`transport.py:249-250`)
2. **`EService: Throttled` → generic `APIError`** — one of Kraken's two *officially
   documented* rate-limit errors, and `EService` appears **nowhere** in `kraken_api/`
   (verified: 0 hits), absent from `_RATE_LIMIT_MARKERS` (`:45-48`)
3. a real HTTP **429** → `requests.HTTPError`, because `raise_for_status()`
   (`transport.py:208`) fires **before** the envelope branch

Correct tuple: `(RateLimitError, APIError, requests.RequestException)`.

### 1.13 Kraken signals the limit as HTTP 200 — so `urllib3.Retry` cannot see it
Proven by the wrapper's own flow: `raise_for_status()` succeeds at `:205`, and detection
happens later at `:248-250` by string-matching the JSON body. `urllib3.Retry.is_retry(...)`
is the only gate (`util/retry.py:424`) and there is **no body inspection anywhere** — a 200
is, to urllib3, a success. Its jitter is also **optional and additive**, and `backoff_factor`
defaults to `0` (`:232`), `backoff_jitter` to `0.0` (`:241`). `urllib3` is already
installed, so the cost is zero — but it cannot carry this.

`backoff` is **archived** (last release 2022-10-05) → reject. `httpx` own docs say
`retries=1` covers ConnectError/ConnectTimeout only and to "consider tenacity" for 503.
**`tenacity` 9.1.4** (Apache-2.0, zero deps) with `wait_random_exponential` = AWS Full
Jitter is the recommended option.

### 1.14 Jitter is load-bearing because `just bench` self-inflicts a herd
`justfile:76-77` runs train then backtest = two full re-pages back to back (its own
comment), each a 6-page burst, against Kraken's documented **Starter counter max 15,
decay −0.33/sec** (recovery ≈45 s). Kraken documents the penalty itself: *"restricted for a
few seconds (or possibly longer if calls continue to be made while the rate limits are
active)."* AWS Architecture Blog (Marc Brooker, 2015-03-04, updated 2023): *"The solution
isn't to remove backoff. It's to add jitter."* / *"The no-jitter exponential backoff
approach is the clear loser."* → **Full Jitter, never `wait_fixed`.** Don't share an RNG
stream across the two legs.

### 1.15 ⚠️ A POST-retry hazard constrains option (b) upstream
`transport.py:157` + `_next_nonce()` (`:170-178`, verified: `max(now_ms(),
self._last_nonce + 1)`, strictly-increasing per API key) mean a session-level retry must be
`allowed_methods={"GET"}` — an auto-retried POST risks replaying an order against a stale
nonce (a duplicate-order bug). This is the strongest reason the upstream change, if any,
must not be a blanket session-level `Retry`.

### 1.16 Two comments are factually wrong about the cadence
`configs/default.yaml:118-124` (and the `.timer` unit) justify `signal_max_age_hours: 12`
with *"kraken-funding-rates settles ~8-hourly"*. Kraken's own series is **hourly, measured**.
So 12 is ~12× looser than needed — and would be exactly right for the Binance/Bybit 8h
series §1.7 rejects. Free fix alongside the backfill. (The same comment is otherwise
disciplined: it already retracts an earlier "76 of 721 bars" over-claim.)

### 1.17 G2 couples to G4 and must say so
366 d of funding ≈ 8,766 hourly bars — ample for the 721-bar live ceiling. But if G4 lands a
multi-year store at finer intervals, the older span would have no funding and would
**silently zero-fill**. Honest framing: *"funding covers the most recent year;
`signal_observed`/`signal_age_hours` are how you tell"* — which is precisely what those two
columns exist for.

---

## §2 Lead's verification receipt

Every claim above that the decision rests on was independently re-derived from the source
or from a live call. **This is the falsification record** — the checks that would have
changed the outcome if they had failed.

| # | Claim | Method | Verdict |
|---|---|---|---|
| 1 | `signals/eth_usd_funding.jsonl` is 1 line | `wc -l` | **1** |
| 2 | Imbalance branch gated on `{"bid_vol","ask_vol"}` | `sed -n '1005,1016p' features.py` | confirmed at `:1007` |
| 3 | Neither name is in `_SIGNAL_COLUMNS` | grep over `:94-125` | **0 hits** |
| 4 | Retry/backoff/sleep absent in the fetch path | `grep -cE 'time\.sleep\|backoff\|retry' data.py` | **0** across the 70 KB file |
| 5 | Paging loop has no `try`/`except` | `sed -n '1104,1112p' data.py` | confirmed |
| 6 | `market_data_store: null` | `sed -n '185p' configs/default.yaml` | confirmed |
| 7 | `_SIGNAL_CHANNELS` has exactly 3 entries | `sed -n '144,156p'` | 3 |
| 8 | Book fetched and discarded | `sed -n '72p' engine.py` | `data["order_book"] = ...order_book(pair, count=10)` |
| 9 | `first_tradable_index` excludes exogenous cols | `sed -n '178,206p' features.py` | `:203` confirms the set-difference; docstring names the 2026-10-01 bug |
| 10 | Seam masks on explicit per-column age, not ffill | `sed -n '795,818p' data.py` | `:798-799` `_signal_ages` + `.where(ages <= bound_hours)` |
| 11 | `kraken-funding-rates` has no historical endpoint | grep `tickers\|since\|until` in `client.py` | only `_get("/tickers")`; no `since`/`until` |
| 12 | **`/historical-funding-rates` is keyless** | **live `curl`** | **HTTP 200, 1,013,076 bytes** |
| 13 | …and returns the SAME series, not a proxy | **live: exact float match** | checked-in `0.02527185133308243` matches **exactly 1** of 8,791 records, at `2026-10-02T00:00:00Z` = the checked-in hour. **0 duplicate timestamps**, span `2025-10-01T08:00:00Z → 2026-10-02T22:00:00Z` |
| 14 | Field union is exactly 3 | live, over all 8,791 records | `['fundingRate','relativeFundingRate','timestamp']` |
| 15 | `_SIGNAL_BUILDER_INPUT_COLUMNS` = `("bid","ask","spread")` | `sed -n '139,150p'` | confirmed; `:146-148` reserves `realized_spread_bps` |
| 16 | `_add_signals_features` skips builder inputs | `sed -n '1050,1062p'` | `:1052-1054` `if col in _SIGNAL_BUILDER_INPUT_COLUMNS: continue` |
| 17 | `POINT_IN_TIME_EXOGENOUS_COLUMNS` unions both tuples | `sed -n '169,175p'` | confirmed — needs no third edit |
| 18 | `available_cols` is an intersection with file columns | `sed -n '763,770p' data.py` | confirmed → the width hazard |
| 19 | `kraken-python` is flake-pinned at local HEAD | `flake.nix:6` + `flake.lock` + `git rev-parse` | lock rev `81de5974…` **== local `81de597`**; `.venv` has `__editable__.kraken_python-0.4.0.pth` |
| 20 | Throttle off by default | `grep min_interval` in `transport.py`/`auth.py` | `:108 = 0.0`, `:181 if <= 0: return`, `auth.py:73 or 0.0` |
| 21 | `raise_for_status()` precedes envelope handling | `sed -n '203,210p' transport.py` | confirmed at `:208` |
| 22 | `EService` unknown to the wrapper | `grep -rc EService kraken_api/` | **0 hits** |
| 23 | Per-key monotonic nonce | `sed -n '168,178p' transport.py` | `max(now_ms(), self._last_nonce + 1)` |
| 24 | Config comment says "~8-hourly" | `sed -n '116,126p' configs/default.yaml` | confirmed wrong vs §1.5's measured hourly |

**AUDIT.md supersession (recorded, not silent).** A prior pass's `AUDIT.md` was already
committed (`ca107fa` lineage) with `rl/features.py` line numbers pinned *before* the
non-finite-guard pass moved the microstructure builder — it cited `features.py:586-590`,
now `:1007-1015`. Phase 1 re-derived every citation against the current tree and recorded
the supersession. Receipt #2 above is that re-derivation, independently repeated by the lead.

---

## §3 Corrections this phase forced on AUDIT.md

Carried forward so the architect does not reason from the superseded text:

1. **G1's minimal change is two tuple edits, not one** (§1.3). AUDIT.md said widen
   `_SIGNAL_COLUMNS`; that alone widens the observation by 3, not 1.
2. **G1 is un-backfillable from any free source** (§1.1). RESEARCH-1 falsified the obvious
   fallback directly against the S3 bucket (`bookDepth/BTCUSDT` → `KeyCount=0`) with a
   klines control returning 2 — so the `kraken-deep-history` archive pattern does **not**
   transfer.
3. **G2's episode-truncation risk is already fixed** (§1.6); the live hazard is width.
4. **G2 recovers 1 of 6 columns, not 6** (§1.7).
5. **G3's throttle is nominally present but disabled** (§1.11) — AUDIT.md's "upstream
   `_throttle()` is fixed spacing" understates it: there is no spacing at all by default.
6. **`except RateLimitError` is insufficient** (§1.12) — three distinct failure paths.

---

## §4 Open questions the architect must answer (deliberately NOT answered here)

Per the phase split, these are decisions, not findings:

1. **Which gap?** G1, G2, G3, or a different one from AUDIT.md §3 G4–G7. Note G1 and G2 are
   both forward-only and share a seam (§1.1); G3 is a prerequisite-quality fix that
   unblocks neither feature but de-risks every recorder.
2. **Outcome type** — `NEW-DATA-SOURCE` or `IMPROVE-EXISTING`. §1.2 pushes hard against a
   new repo for G1; §1.5/§1.9 make G2 a sibling-repo *addition*, not a new one.
3. **G2: does the backfill append to the existing funding file, and how is the 366-day cap
   communicated to the user?** (§1.9)
4. **G1: recorder home** — a new sibling, a new store grid (RESEARCH-1 argues **against**:
   `kraken-market-data`'s identity is a shape-preserving contract, `_OHLCV_COLUMNS` is a
   hardcoded 8-tuple, `read()` always returns that shape), or the **existing JSONL seam**
   (RESEARCH-1's recommendation, and the cheapest of the three).
5. **G3: does `engine.py:57-74`'s bare `except` get fixed in the same slice?** RESEARCH-3
   argues `RateLimitExhaustedError` belongs in `data.py` (not `errors.py`, which holds none
   of the three data errors) and that key *presence* must stop meaning "no data".