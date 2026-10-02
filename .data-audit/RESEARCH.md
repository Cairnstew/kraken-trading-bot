# RESEARCH — assembled from RESEARCH-1/2/3

**Pass:** PHASE 2 (team `audit-pipeline-1002`). Input: `.data-audit/AUDIT.md` @ `ca107fa`.
**Sources (each written by its own researcher, to avoid write races):**
[`RESEARCH-1.md`](RESEARCH-1.md) 528 lines — CAND-1 microstructure ·
[`RESEARCH-2.md`](RESEARCH-2.md) 618 lines — CAND-2 exogenous coverage ·
[`RESEARCH-3.md`](RESEARCH-3.md) 791 lines — CAND-3 store + CAND-4/5 sizing.

All three researchers were instructed to make **real keyless network calls** rather than
recall API behaviour. Every number below is measured against live endpoints or real archives,
not asserted. That turned out to matter: **six of the audit's premises did not survive
measurement**, and every correction moved in the direction of *more* work being available
than the audit claimed — except two, which found latent defects the audit missed.

---

## 1. The audit was refuted six times. Corrections, with the correction's evidence.

| # | Audit's claim | Researcher finding | Evidence |
|---|---|---|---|
| 1 | G2-R1 (`vwap_close_gap_zscore_20` + `count_per_range`) is an open gap | **Already landed** under other names — `vwap_dev`, `trade_count_zscore_20`, `volume_per_trade` | AUDIT §2 (auditor) |
| 2 | `/public/Spread` is "bounded" and a viable partial route | **`since` is ignored entirely.** Every probe −30m…−400d returned the same trailing ~250 change-driven samples. Not backfillable at all. | R1 §3 |
| 3 | `/public/Depth` has no `since`; treat it as snapshot-only | Correct in effect, **wrong mechanism**: Depth **accepts `since` silently and ignores it** (Kraken drops unknown params). Fails without any error. | R1 §3 |
| 4 | Kraken Futures funding is "not backfillable; inherently forward-only" | **FALSE.** `GET /derivatives/api/v3/historical-funding-rates` (note the **hyphens** — the concatenated path 404s and the capability looks nonexistent) is **keyless, 8781 hourly records / 366 days**, verified on 4 symbols. `fundingRate` is byte-identical to the `/tickers` value already in the log. | R2 §1 |
| 5 | News "needs an API key" | **FALSE.** `ticker-news-signals` is keyless by design. The real cap is **exactly 100 items per query**, opaque sampling. Accumulate-only. | R2 §1 |
| 6 | `data_window` "clips to empty → `NotEnoughDataError`" | True for the non-store leg, **overstated for the store leg** — with a store there is no clipping-to-empty mode, only wasted work. | R3 §3 |
| 7 | CAND-2 = "8 of 60 columns carry no information" | **Understated.** 7 of the 8 exogenous columns are **Spearman ρ = 1.000 exactly** — one bit replicated eight times, each a **+7.4σ step** lasting 13 bars. A correctness item, not a depth nicety. | R2 §3 |

Correction 4 is the most consequential: it is a *path typo* (`hyphens` vs concatenated) that
made a keyless 366-day history appear not to exist. Correction 2 removes the fallback the
prior pass had leaned on.

---

## 2. What the three candidates are actually worth

### CAND-1 — a keyless, historical trade tape (`/0/public/Trades`)

| Property | Measured |
|---|---|
| Retention | **No practical ceiling** — reaches each pair's *full listing history* (XBT/USD 2013-10-06, ETH/USD 2015-08-07, SOL/USD 2021-06-17). Floor is the listing date, not a retention clamp. Does **not** share the ~721-bar OHLC ceiling. |
| Auth | **Keyless** (OpenAPI `security: []`), confirmed by probe with no API-Key header |
| Backfill cost | **2.0–18.6 min/pair** for 30d of full-depth tape, sized **for free** from the OHLCV `count` column, which reconciles to the tape at delta exactly **0.0** |
| Reduction | **6 point-in-time scalars + 1 rolling**, per (ticker, bar), with **0 lines of parsing** — typed models already exist in `kraken-python` |
| Warm-up cost | **None.** 6 of 7 features are point-in-time (present on bar 0). Contrast CAND-3's push-down, which costs 19 bars of warm-up. |

**Three undocumented traps, all load-bearing:**
- `Trades.last` is a **nanosecond** cursor returned as a bare `str`; passed back verbatim it lands in **1970**. (`Spread.last` is in *seconds* — same param name, incompatible units. A generic paginator silently corrupts one of them.)
- Rows are **7 fields, not 6**: `Trade.from_public_row` silently drops `trade_id`, the field needed for dedup. Measured **7.278× volume inflation**.
- Throttle arrives as **HTTP 200 with `{"error":["EGeneral:Too many requests"]}`**, not 429, at ~30 burst. `KrakenTransport.min_interval` defaults to **0.0**.

**Honest negative — realized spread fails and should be dropped.** Trades carries no book, the
only mid source ignores `since`, and a 1h ETH bar spans ~98bps against a 0.47bps quoted
spread. Roll was **+260,551 bps** where defined and undefined on 13/16 one-minute bars;
Corwin-Schultz went negative on 11/15. This is structural, not a bug.

**The consumer is dormant but alive.** `_add_microstructure_features` (`features.py:577-590`)
is keyed on `bid_vol`/`ask_vol` — names **no producer emits**. And `merge_extra_features`
**allow-lists at `data.py:609-613`**, so a new column is dropped until added to
`_SIGNAL_COLUMNS`. One line each does two jobs: it forwards *and* it is auto-swept into
`POINT_IN_TIME_EXOGENOUS_COLUMNS`, which `first_tradable_index` excludes from the warm-up
gate — so no risk of the documented 24→720 regression. In-repo precedent to copy verbatim:
`add_derived_ohlcv_features` (`data.py:812-873`).

### CAND-2 — 366 days of **on-venue** funding, and 8.66y of daily macro

| Channel | Recoverable? | Depth | Mechanism |
|---|---|---|---|
| **Funding** | **Backfillable** | **366d hourly, one keyless call** | `historical-funding-rates`; drop-in, byte-identical to `/tickers` |
| **F&G** | **Backfillable** | **3162 daily records, 2018-02-01 → today (8.66y)**, one keyless call | already fetched on every social pull and discarded |
| **News** | Accumulate-only | ~100 articles/pull | 100-item cap, opaque sampling |
| **StockTwits** | **Effectively dead** — see defects below | 30 messages ≈ 3.9h | — |

End-to-end through the **real seam** at shipped settings:

| | coverage | distinct `funding_rate` values | `signal_observed` |
|---|---|---|---|
| **shipped** (1 record) | **13/721 = 1.80%** | **1** | z-scores to a +7.4σ step |
| **funding backfilled** | **721/721 = 100%** | **721** (lag-1 autocorr 0.75) | correctly **0.000** |

**NEW DEFECT (audit missed it): the F&G daily value is final-at-fetch.** Emitting it hourly at
00:00 of day D asserts day D's *close* — up to **24h of look-ahead**. Measured: 19 polls over
9 min held the value at 72 while `time_until_update` counted down 40017→39472s. **Fix: stamp
at D+1 00:00Z.** This *inflates* rather than flattens results, so "the numbers looked
reasonable" will not catch it. R2 also notes hourly-expanding a daily series buys *coverage*
(54%→100%) but **not information** — distinct values stay 15, and the wider z-range is a
normalization artifact.

**Two live sibling defects in `kraken-social-signals`** (both measured):
- Its shipped `Mozilla/5.0 (compatible; …)` UA **loses the Cloudflare gate** → HTTP 403, and
  `_get` treats 403 as non-fatal → **silently produces nothing**. A full Chrome UA → 200.
- Pagination reads a cursor key **the API never returns** (`cursor.after` vs actual
  `{more, since, max}`); pages 1/2/3 returned byte-identical results. Hard ceiling **30
  messages ≈ 3.9h**, so the `--lookback-hours 24` default is **unreachable**.

⇒ **The F&G backfill must not depend on StockTwits succeeding**, or enabling that channel
yields nothing at all. Also: `nix/module.nix` declares **zero timers** and `ExecStart` is a
hard-coded host path, so the funding timer is non-hermetic *and* has never been run (6 timers
on this host, none of them that one).

### CAND-3 — the store is a **config flip**; the Kraken archive is the wrong answer

| Measurement | Result |
|---|---|
| Store build | **158 s, 13 MB, 204,245 bars, 280 months**, 3 commands, **zero code** |
| 60 bytes/bar | disk cost is a non-issue |
| Bot read, **unmodified binary** | `read_ohlc_dataframe(…, market_data_store=…)` → **shape (76561, 11) in 0.48 s** = **106× the 721 ceiling**, 2018-01-01 → 2026-10-02 |
| Flake wiring | the bot's dev shell **already carries** the sibling (`flake.nix:62,74`) |
| Is 721 a REST ceiling? | **Yes, hard.** Kraken OpenAPI verbatim: *"Returns up to 720 of the most recent entries (older data cannot be retrieved, regardless of the value of `since`)"*, and `since` is *"intended for incremental updates"*. Confirmed: `since` = None / −30d / −2y / 2018 → **identical `last=1790938800`**. |
| So the store is for | **the only route to bars older than 30 days** — not an optimisation |

**The central judgement, made not hedged: do NOT build a Kraken OHLCVT reader.** Reasons, all
measured rather than argued:
- **Publication lag, not basis.** MANIFEST `coverage.end = 2026-06-30`, `generated = 2026-08-17`;
  today is 2026-10-02 → newest Kraken archive is **94 days stale**, Q3 unpublished. Binance is
  monthly. A Kraken reader **does not replace the forward poller** — the cross-venue seam
  *moves to the tail* rather than disappearing.
- **Kraken's bulk OHLCVT has no `vwap`** (MANIFEST columns is 7 long; the store contract is 8;
  Kraken's own REST row schema *does* carry it). Strictly poorer on shape than what it replaces,
  and a Kraken-seeded store would **silently lose `vwap_dev`** through the presence gate.
- **Basis is a level shift, not noise** — measured on 2,184 real matched hourly bars (2184/2184
  matched): close **+5.41 bps mean**, 6.52 bps std, against a **58.30 bps** hourly sigma. Yes
  there is a *drift* (−0.72 → +6.63 → +9.97 bps monthly), but z-scoring removes the level.
  Hourly-return corr **0.999081**; 24h-return corr **0.999931**, sign disagreement 0.37%.
  The real gap is **liquidity** (Binance trade count 126×, notional 18.6×) — and that is a
  property of the **venue**, not of the archive choice. Kraken's archive has Kraken's own tiny
  counts too.
- ⇒ Correct fix is to **label the venue in `_meta.json`** (6 lines), not buy 10.5 GB.
  Footnote: a ranged GET of the ZIP central directory makes a targeted single-symbol
  extraction **54 KB**, not 538 MB — the only venue-exact route is for pairs Binance never listed.

**Build trap:** deep-history's own `nix develop` has only pytest — no pandas/pyarrow — so
`_open_store` silently returns `FallbackStoreWriter` and writes `.csv` that `MarketDataStore.read`
(which globs `*.parquet`) **cannot see**. Only the report's `store_mode` field distinguishes them.
Docs fix, not code. This is the second time a pass has hit it.

**Bonus finding: `pages` is a replay loop, not merely non-scaling.** `last` never advances past
page 1 on a live pair, so calls 2..N are byte-identical: `pages=6` → 6 calls, **721 unique
bars, identical to `pages=1`**, 10 dup rows discarded at `data.py:1031`.

---

## 3. The two no-new-data gaps (R3) — and why one of them gates everything

**CAND-4 — retry/backoff/salvage, ~40 lines, NO new dependency.** The audit missed that the
dependency already ships: **`market_data.client.KrakenClient` already implements retry + capped
exponential backoff + rate limiting** (`client.py:114-147`, `utils.py:180-211`, env-tunable via
`KRAKEN_RETRY_BACKOFF`) and already satisfies the exact duck type `_page_candles` consumes.
`read_ohlc_dataframe` already has a `market_data_source` parameter documented as accepting "the
store's own thin client" → the fix is a **1-line swap per call site**. Adding `tenacity` or an
`HTTPAdapter` would be *wrong*: they hide policy from the repo's `log_event` convention and
neither can do partial salvage. Design: retry with capped backoff; **fail loud on page 0,
salvage+WARN after** (mirroring `engine.py:57-74`'s existing pattern); parse
`EService: Throttled: [timestamp]`; `if last == cursor: break`; a gap-detection log line so
salvage is honest about what it dropped. **Refinement of scope:** Kraken's published rate
limits (counter max 15) did **not** reproduce on the keyless path (16 rapid + 8 at 2.5/s both
100% OK) — the counter is per-API-key and the RL read is keyless. So scope CAND-4 as
**transient-failure resilience, not rate-limit compliance**.

**CAND-5 — dispersion-aware gating, ~50 lines.** Every input is *already computed and printed*;
only the comparison is missing. Demonstrated with the harness's own `summarize()`:

| Case | Gap | Within-group spread | Ratio | Correct verdict |
|---|---|---|---|---|
| 1 | 4 pp | 0.075 pp | **53×** | RESOLVED |
| 2 | 3 pp | 19 pp | 0.16× | **today's `len(seeds)>=3` PASSES this** |
| 3 | 2 pp | 2.2 pp | 0.91× | NOT SEPARATED |

Case 2 is the whole argument: a difference that is *noise-ranked* is tabulated as a finding.
Fix: `pooled_within_spread` = **median** of per-group IQRs (15 lines) + per-arm-pair verdict in
`_build_claims` (25) + a pooled-IQR line in `_print_group_table` (10). **Design constraint found
the hard way:** at n=1 per arm the pooled IQR is **0**, so the gate would report "5 pp = inf →
RESOLVED" — blessing exactly the anecdote its prose exists to kill. The gate must sit **behind**
the count gate and require `n>=3` **and** `pooled_iqr>0`. **Output a RATIO, not a boolean** — the
threshold is a judgement call, the ratio is a fact. Documented limitation: at n=3 a group IQR is
a percentile of two values, so this is necessary-not-sufficient; it stops a noise-ranked
difference being *presented* as a finding, it does not manufacture power.

**Gating, stated explicitly for the architect:**
- **CAND-5 IS a precondition for gating ANY change on a claimed effect — including CAND-3's
  own.** The moment a store is seeded the first question is "do 8 years beat 721?", a headline
  two-arm comparison the harness cannot adjudicate. Seed and evaluate without it and you produce
  a number you cannot defend. But it gates the **CLAIM**, not the **BUILD** → land first
  (50 lines, no data, cheapest gate in the repo).
- **CAND-4 is NOT a precondition** for the CAND-3 push-down (correctness unaffected — R3
  measured identical output post-hoc-clip vs pushed-down read, agreeing to 1.5e-13). But
  **land them together**: seeding the store is the moment `_page_candles` starts running
  per-tick on the store leg, after which a read failure means "the tail of my training window is
  missing".

**R3's recommended order:** CAND-5 (50 ln) → CAND-4 (40 ln) → **CAND-3a store flip (0 lines,
158 s, 13 MB)** → CAND-3b push-down + venue label (~21 ln) → *(deferred)* Kraken reader.
**CAND-3a is the cheapest item in the entire audit and should not wait behind anything.**

---

## 4. Cross-cutting notes for the architect

1. **Both researchers independently refused cross-venue sources**, from opposite directions —
   R3 refused Binance *bars* (keep on-venue tails), R2 refused Deribit/Binance *funding*
   (on-venue beats 7× shallower because it introduces no unmodelled basis). This is a
   consistent position, not an accident, and it should hold for this pass.
2. **The single biggest lever across all three candidates is scheduling, not sourcing.**
   R2: one `just funding-timer` that has never been run takes coverage **1.80% → 100%**.
   R1: the `systemd.user` timer pattern is **unproven in this repo** — exactly one exists,
   funding-only, absent from `nix/module.nix`, not installed on this host.
3. **Every candidate lands on a seam that already exists.** No candidate requires an RL
   consumer rewrite; the repeated pattern is a producer whose output needs one allow-list line
   (`_SIGNAL_COLUMNS`) to be forwarded *and* auto-swept as point-in-time.
4. **Two silent-failure guards the previous passes established must be respected:** a
   present-but-non-overlapping signal file is only a WARNING (`data.py:662-671`), and the
   width guard checks width, not NaN. R1's depth/spread columns are single snapshots that would
   be ffill'd up to 12h — precisely CAND-2's stale-feature failure mode.

## 5. Candidate library verdicts

| Library | Verdict |
|---|---|
| `kraken-python` (in-repo) | **Wins for all three candidates.** Already has typed models for Trades/Spread/Depth, the retry client for CAND-4, and is the store's consumer. |
| `krakenex` 2.2.2 (LGPL) | Rejected — last release 2024-07-01, ~2y stale |
| `krakenapi` 1.0.2 (GPL) | Rejected — last release 2024-02-17, stale |
| `ccxt` 4.5.85 | Rejected — current, but coarser; would erase the exact endpoint detail this work depends on |
| `tenacity` / `urllib3.HTTPAdapter` | **Rejected** for CAND-4 — the retry client already exists in-repo and they hide policy from `log_event` |
| Kraken L3 (credentialed) | **Rejected and priced** — needs institutional gating; its only unique product is a per-trade mid, i.e. the effective spread, which R1 showed is not actionable |

**Parsing cost:** none of the three REST endpoints need new parsing. Only the Kraken OHLCVT
CSVs would (~40–80 lines), and R3 recommends not building them.

## 6. Unresolved / explicitly incomplete

- R1 §8 lists **7 live checks it could not complete**: Kraken L3 exact pricing; OHLCVT URL and
  coverage unverified (R3 independently verified the archive, so treat R3 as authoritative
  there); no key so the authenticated rate counter is untested; 7 of N pair listing floors
  unswept.
- **No sibling repo has a LICENSE file**, and `kraken-social-signals` has **one commit** —
  consistent with the two live defects R2 found there.
- **No model artifact exists anywhere** (`models/` contains only `.gitkeep`), so every width and
  coverage figure in all four artifacts is computed from the code path rather than read off a
  shipped model. Phase 6 must train a real model before any width claim is accepted.
- R1's honesty flag worth repeating: **the tape backfills *features*, not *bars*** — CAND-3's
  721-bar training ceiling is untouched by CAND-1.