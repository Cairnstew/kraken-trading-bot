# RUN LOG — audit-pipeline-1002 (CAND-3a + CAND-5)

The working record of this pass: what was decided, what was measured, what the gate actually
established, and what is still open. Appended in order; nothing here is retrospective.

---

## 1. Outcome

**Decision: IMPROVE-EXISTING. Gap: CAND-3a** (activate the already-configured market-data store),
**with CAND-5** (dispersion gate) as the acceptance instrument rather than as the outcome.

The bot could now reach bars Kraken REST will never serve: a store read of **76,562 bars**, disjoint
train/evaluation splits, and a width-neutral observation matrix. The honest out-of-sample result is
**−34.3% return, Sharpe −0.568** before costs. The gate verdict is **`NOT SEPARATED`** — recorded in
DECISION.md §9A.2 and **superseded** by the cost-aware re-derivation in §13.2/§14.0, where all four
metrics come out RESOLVED while **both arms lose money in all six cells**.

**No claim of improvement is made in either direction, and there is no clean-pass language anywhere
in this record.** CAND-3a delivered exactly one thing, which is orthogonal to the return figures:
*an out-of-sample measurement became possible at all.*

## 2. Gate verdict

**`NEEDS_FIX on R1, resolved by docs-only repair, confirmed by the reviewer`** — DECISION.md §15.

Deliberately **not** a clean pass. Phase 6 returned `NEEDS_FIX` with seven findings. F1 was the only
MAJOR *result* finding and was resolved by re-running the gate under costs. F2 carried a MAJOR
*truthfulness* finding whose repair took **four review rounds** to finish — see §4 and §5 below.

Resting on: the reviewer's independent 12-clause probe, plus four structural checks that hold —
AST identical with docstrings stripped **and** string constants blanked; width hash byte-identical
(`obs 93edc733…`, `arr 6a88a379…`, 60 features, `start_index` 24); 455 tests passing; `tools/` zero
diff against the pre-registration commit `97a2a52`.

## 3. Phase 7 candidates — LISTED, NOT STARTED

None of these was started. Each is a decision, not a task, and several need a call before work.

1. **The live-vs-store matrix guard (§9A.1).** A live-vs-store pair inside ONE matrix is structurally
   uncomparable: the two arms replay different bar counts, so the comparison is an artefact of the
   harness rather than a property of the data. Fixed by running two separate matrices for the
   cost-aware result; the underlying defect in `tools/model_matrix.py` is still there.
2. **Whether the policy trades on noise at all.** Churn dominates return. Measured bars-per-re-entry:
   live ~1.22 (median of 1.216 / 1.191 / 1.234), store ~1.43 (median of 1.649 / 1.429 / 1.139) — a
   round trip roughly every bar. Whether that is the policy failing to learn, or the action space
   being mis-specified for this data, is unexamined.
3. **F6 bar-count-vs-wall-clock (CAND-3b).** Windows are N *rows*, not N *hours*, so a window spanning
   a store gap means something different from a contiguous one. The real fix is a reindexed,
   gap-filled bar grid with an explicit decision about what a gap-spanning window means. Recorded in
   §14; it needs its own id because CAND-3b is taken (§10 item 4, a deferred efficiency item).
4. **CAND-2, the recommended next pass** — exogenous coverage: Kraken funding backfill (hyphen path,
   ~366 days available), Fear & Greed, and the timer. **Its look-ahead handling must be settled
   first**, including the 24h F&G publication lag. Closest call of the runners-up.
5. **`configs/default.yaml` `since`/`until: null` — STILL OPEN, STILL THE LEAD'S TO MAKE.** Currently
   kept deliberately: a fresh clone must not error, and since/until null stays coupled with
   `market_data_store: null`. But it means the shipped default trains and evaluates on the *same*
   object, so no OOS number exists on the default path at all. That is a product decision about what
   the default should be, not something this pass should decide by omission.

## 4. Evidence kept: the checker was proved INERT by mutation

Worth preserving because it is the actual basis for trusting the verdict.

A clause-checking script was written during the R1 repair to assert that every falsifiable clause in
`_rsi`'s docstring was true. An earlier draft of §15 cited it as covering the final state. **That
citation was withdrawn.** The reviewer cloned the final commit, restored *both* round-4 defects in the
docstring while leaving the executable body untouched — `avg_gain is -0.0` with its **correct** outcome
clause intact, and "the whole frame" instead of "every bar after the first" — and ran the script:

> **11 of 11 reason checks PASS, exit 0, "ALL CLAUSES AND ALL STATED REASONS MEASURED TRUE"** —
> against a docstring asserting both defects.

Two structural reasons, and the second is the sharper one:

- **No binding.** Every assertion measured *pandas' behaviour*; none was bound to any word of the
  docstring. "avg_gain is never -0.0" passed *because pandas behaves that way* — orthogonal to what
  the docstring *says*. Measuring the right things is not checking the text.
- **The extraction never found the clause.** The clause-splitter broke on the `.` inside `-0.0`,
  `0.0` and `1.001`, printing mid-sentence fragments, and it required the literal "so that" — so the
  one clause whose stated reason had been wrong the previous round was one it never displayed.

This is the pass's own failure mode, one level up: a verification artifact reporting green while the
defect it was built to catch is present. It is recorded rather than quietly deleted, because being
*proved* inert — at the reviewer's initiative — is the evidence. A gate whose evidence is only ever
green is worth nothing. The `/tmp` script is withdrawn and is **not part of the evidence**; the
bounded replacement is specified in §15.1 as an OPEN follow-up with a trigger.

## 5. Evidence kept: the reviewer corrected its own measurement

Also worth keeping, because both parties made the same mistake in opposite directions.

The reviewer claimed `avg_loss` **persists** at `0.0` "well after the rising run ended" (29 bars). The
lead measured that it does not: through real down bars after a rising run, `avg_loss` is `0.0` at **0
of 39** bars — it decays immediately, since `y[i] = (1-a)·y[i-1] + a·loss[i]`. It holds 39 of 39 only
through *flat* bars.

The reviewer then re-read its own probe output and conceded: the output had said *"avg_loss == 0.0 at
29 bars, from 1 to 29"* while the first down bar was at index 30. Bars 1–29 were **inside** the run.
It had read its own output and written the opposite conclusion — and said so plainly.

The lead had made the mirror-image error earlier, writing "the chain breaks at its **first real link**"
into a file while describing it as "second" in a message; only a reviewer reading the **source** rather
than the description caught it.

**The shared lesson: both sides reasoned from a summary instead of the artifact.** Two independent
instances of the same failure, which is a much stronger argument for reading the source than either
would have been alone.

## 6. Pre-registration integrity

`DISPERSION_RATIO_THRESHOLD = 1.0` was committed in `97a2a52` ("PRE-REGISTER the dispersion rule")
*before* the cost-aware run, and is a direct ancestor of the result commit `ae8bdd5`. It was never
tuned. `tools/model_matrix.py` is **provably zero-diff** against `97a2a52` — the entire measurement
track is untouched, with `tools/store_gap_scan.py` (F6's new tool) the only addition under `tools/`.

The pre-registered magnitude prediction was **wrong** and that is recorded rather than smoothed:
predicted ≈1.4, measured **82.91**. §14.4 corrects it, and §14.5 sharpens the correction — the pooled
half was not merely stale but a **definitional mix** (11.68pp was Phase 6's store-arm-*only* IQR on a
76,539-bar snapshot; 3.97pp is the shipped estimator's median of *both* arms on 76,540 bars). The gap
half is like-for-like. **The verdict is unaffected**: both figures sit on the same side of a threshold
fixed in advance, and reporting the larger number is what a non-self-serving correction looks like.

## 7. Open, by design

- **§15.1** — docstring gate bound to the text it defends. OPEN with a trigger; not claimed as done.
- **§3 item 5** — `configs/default.yaml` `since`/`until: null`. Open, lead's call.
- **CAND-4** — retry only if a store-backed run logs a `_page_candles` failure. Deferred with a trigger.
- **§9A.1 harness defect** — see §3 item 1.
---

## 2026-10-02 (later) — IMPROVE-EXISTING / G2 — funding-history backfill

**Gate: PASS.** Commits `e027621..6bef125`, pushed. 455 → 457 tests. Sibling
`kraken-funding-rates` @ `dc49847` (no remote — see open items).

### What was measured, not quoted

Live keyless `GET futures.kraken.com/derivatives/api/v3/historical-funding-rates?symbol=PF_ETHUSD`
→ HTTP 200, 1,013,076 bytes, **8,791 records, hourly, 366.58 d, 0 duplicate timestamps**, field
union exactly `['fundingRate','relativeFundingRate','timestamp']`. The checked-in one-line file's
`0.02527185133308243` matches **exactly one** record, at the checked-in hour — so the live
`/tickers` snapshot and the history endpoint are **the same series, not a proxy**. The endpoint is
kebab-case; five other spellings 404.

| arm (live-fetch leg, 721 bars @ 60m) | baseline | backfilled |
|---|---|---|
| n_features | 60 | **60 unchanged** |
| bars replayed | 697 | **697** |
| trades | 558 | **546** |
| return / Sharpe / maxDD | 7.10% / 0.827 / 6.57% | 7.52% / 0.987 / 5.58% |
| funding_rate distinct | **2** | **721** |
| signal_observed | 13/721 | **721/721** |

|Δreturn| **0.42 pts**, same sign — inside the ~5 pt band, **not** gated on exact equality. All 11
cumulative fitted STDs agree to ratio **1.000** (|log ratio| < 1e-5) and both exports have
byte-equal OHLCV, so the arms fitted the same window and the difference is attributable to the
funding channel. Compared STDs only, never MEANs (the 2026-09-30 lesson).

### Retracted and superseded claims — this is the part that matters

- **DECISION §6.1's hour-dedup direction was inverted.** It claimed the appended *live* record wins
  the overlap hour. `data.py:758-763` runs `duplicated(keep="last")` **before** the hour-`floor()`
  and the `groupby(level=0).last()`, so the loser's row is discarded by **file order** and `last()`
  never sees it — whoever is **last in the file** wins wholesale. Measured without the fix: 13
  real `spread` readings → `NaN`, +13 non-finite cells. Fixed by `--append` skipping held hours.
- **The builder's "non-finite identical in both arms" was WRONG** — same slice gives 684 baseline
  vs 697 backfilled. **The integrator's "spread byte-identical both arms" was WRONG** — the
  backfilled arm is 0/721 (the file has no bid/ask anywhere). Both were caught by re-derivation.
- **§7.3's width constants (57 / 49) are 3 low**; the fixture lacked `vwap`/`count`. Sane range
  **52–60**. A gate asserting the literals fails on a correct implementation.
- **`features.py`'s docstring claimed the shipped default "composes 52"** — 52 is the *all-null*
  width; shipped composes **60**.

### Two guard-chain defects found at close-out (F-16, F-17)

`just audit-findings` **had never fired**: it scanned `\bF(\d+)\b` and looked up `F{n}`, but the
convention is hyphenated `F-1`, so every run reported "no identifiers" and returned 0.
`just audit-evidence` **crashed** on a legitimate absolute cross-repo citation. Both repaired; the
repair of `audit-findings` was proven biting on its first real run (16 findings, exit 1 on a
genuine `F-16 MISSING`). `audit_checks.py` is not in `PY_FILES`, so `executable-ast` still MATCHes
and `model_matrix.py` remains byte-identical to `069a826`.

### Honest cost

**1 of 6 columns recovered.** All 8,792 backfilled records carry `null` for
`basis` / `open_interest` / `funding_rate_prediction` / `vol24h` / `bid` / `ask`. Five columns that
the single live snapshot gave 13 real readings on now read **constant zero**, and degenerate-STD
count rose **3 → 10**. `signal_observed` no longer distinguishes absence per column (F-2). Both
arms are **IN-SAMPLE** (shipped default; the bot warns itself) — no predictive claim is supported.

### Open items for the next pass

- **G1 is next and is un-backfillable *by physics***: Kraken's book is a live snapshot with no
  historical endpoint, and a `count=10` snapshot at *t* does not encode *t−1h*. Start the recorder
  **before** writing the feature work or the depth is unrecoverable.
- **G4 stays the binding constraint on episode length**: funding is 366 d deep but the price frame
  is still the 721-bar live ceiling.
- `kraken-funding-rates` has **no remote**. Offer `gh repo create` — not done unilaterally.
- G3, G5, G6, G7 from `AUDIT.md` remain deferred and unranked against a fresh look.
