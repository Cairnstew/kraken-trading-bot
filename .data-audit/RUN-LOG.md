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