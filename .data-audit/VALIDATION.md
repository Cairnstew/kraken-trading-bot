# VALIDATION — independent review of the CAND-3a + CAND-5 pass

Reviewer: `reviewer` (team `audit-pipeline-1002`), Phase 6. Read-only on source.
Reviewed HEAD: **`aeace32`** (tree clean at review start).

# GATE VERDICT: `NEEDS_FIX`

The **functional** gate passes: 429 tests, flake green, the store arm runs
end to end, the honest OOS number reproduces exactly, and live-arm
width-neutrality is byte-identical so no `models/` artifact is invalidated.

The **claim** gate fails. Three shipped claims are false, and one of them —
the "+1800% seed" — is a degenerate artifact that invalidates the quantity the
CAND-5 gate is built to measure. Details, with the evidence:

| # | finding | severity |
|---|---|---|
| F1 | **The +1800% store seed is a friction artifact, not seed variance.** Same model, same bars, same trades: **+2015% → −88%** at Kraken taker costs. Across all 3 seeds the store-arm IQR collapses **1961pp → 11.7pp (168×)**. The gate's yardstick measures a cost assumption. | **MAJOR** |
| F2 | `test_compute_seam_is_the_only_guard_for_the_rolling_technical_group` is **vacuous and its stated mechanism is false**. Deleting the compute seam leaves **all 31** non-finite tests green. 5 docstrings state a pandas behaviour that is the *opposite* of what pandas does. | **MAJOR** |
| F3 | **`market_data_store: null` cannot turn the store off** for a store-trained model, yet three remedy messages advertise exactly that as the fix. | material |
| F4 | "zero-volume bar at each **partial-month boundary**" (§9A.3) is **factually wrong**. 0 of 106 month-first bars are zero-volume; all 4 are mid-month and sit **inside exchange-downtime gaps**. | material |
| F5 | `DECISION.md` is **internally inconsistent** on the bar count (§3/§5/§8/§11 say 76,561; §9A.3 says 76,562) and `RESEARCH.md` / `RESEARCH-3.md` carry 76,561 with no `NOT SEPARATED` labelling. | material |
| F6 | **157 missing hourly bars in 28 gaps**, including a **38-bar hole at the seed/live-append seam in the most recent month**. Features are computed on *bar counts*, so a 39-hour jump is z-scored as a 1-bar step. Not previously reported. | material |
| F7 | 4 guard sites + 1 `evaluate_scope` clause are redundant-and-unpinned (removing any one leaves the suite green). | minor |

`NOT SEPARATED` is a **valid, non-failing** outcome and is reported as-is.

---

## 1. Test matrix

| check | command | expected | observed | verdict |
|---|---|---|---|---|
| unit suite | `nix develop --command bash -c "python -m pytest -q"` | 429 passed | **429 passed**, 24 warnings, 34.46 s | **PASS** |
| flake | `nix flake check --no-build` | green | **all checks passed** | **PASS** |
| non-vacuity | 12 source reverts in `/tmp` clone (below) | tests fail without their fix | **5 of 12 revert cleanly; 4 expose redundancy; 3 expose a false claim** | **see §2** |

### 1a. Non-vacuity battery

Method: clone HEAD to `/tmp/krb-verify/vacuity/repo`, revert **only the source**
(never the test), run the pinned test, restore. Control run first and last:
**79 passed** on the three relevant files, **429 passed** on the full suite.

| # | source reverted | site | test run | result | non-vacuous? |
|---|---|---|---|---|---|
| 1 | `fit` hard finite gate | `features.py:641-646` | `test_fit_refuses_to_persist_a_non_finite_stat_and_names_the_column` | **FAILS** | **YES** |
| 2a | `transform` final float32 check | `features.py:745-750` | all 31 in `test_feature_nonfinite_guards.py` | 31 pass | **NO — vacuous** |
| 2b | `transform` in-sample-fallback check | `features.py:737-742` | `test_transform_in_sample_fallback_...` | 1 pass | redundant |
| 2c | `normalize`'s own check | `features.py:452-457` | `test_transform_fails_loudly_on_a_poisoned_stats_artifact` | 2 pass | redundant on this path |
| 2d | **all three** transform-path guards | 3 sites | all 31 | **21 FAIL** | **YES (collectively)** |
| 2e | final float32 check alone | `features.py:745-750` | **full suite, 429** | **429 pass** | **NO — vacuous** |
| 3 | passthrough-zero subset | `features.py:70` | `test_exogenous_passthrough_preserves_zero_and_the_minus_one_sentinel` | **FAILS** | **YES** |
| 4 | `evaluate_scope` `is_pinned` clause | `data_window.py:575-587` | `test_default_window_is_labelled_in_sample` | **FAILS** | **YES** |
| 4b | `evaluate_scope` `has_split` clause | `data_window.py:589-601` | 2 tests | 2 pass | redundant (overlap clause catches it) |
| 4c | `evaluate_scope` empty-halves clause | `data_window.py:603` | `test_pinned_window_with_disjoint_halves_...` | **FAILS** | **YES** |
| 5 | `_guard_pinned_coverage` → no-op | `data_window.py:467-514` | `test_pinned_without_a_store_names_the_fix_and_its_cost`, `test_pinned_window_covering_nothing_...` | **2 FAIL** | **YES** |
| 6 | **the compute seam itself** | `features.py:800-803` | all 31, incl. `test_compute_seam_is_the_only_guard_...` | **31 pass** | **NO — see F2** |

The five the brief named are all non-vacuous: **#1 fit gate, #2d transform
gate (collectively), #3 passthrough-zero, #4c IN-SAMPLE derivation, #5
`_guard_pinned_coverage`.**

Note on #5: `test_pinned_multi_year_window_on_a_short_frame_raises_not_enough_data`
correctly still **passed** with the guard no-op'd — that is the "ordinary short
frame must *not* fire" half, which is the behaviour `data_window.py:481-487`
documents. Correct, not a gap.

---

## 2. The five pre-gate items

### (a) Inf-fix coverage — **coverage is real; two claims are false**

**Is there a hard finite check after normalization on both `fit` and `transform`?**
**Yes**, and it is proven load-bearing:

| stage | site | what it checks | non-vacuous? |
|---|---|---|---|
| `fit` | `features.py:641-646` | the **ffilled observation frame**, before any stat is built | **YES** (#1) |
| `normalize` | `features.py:452-457` | the normalized output; the only site that catches a poisoned on-disk `normalization.npz` | only via #2d |
| `transform` fallback | `features.py:737-742` | in-sample branch input | redundant |
| `transform` return | `features.py:745-750` | the float32 array actually returned | **NO** (#2a/2e) |

`_require_finite` (`features.py:297-340`) does one vectorised `np.isfinite`
pass, names the offending column, reports stage/count/kind, and
`NonFiniteFeatureError` subclasses `ValueError` (`features.py:206`) so existing
handlers keep working. The `fit` check is transactional — `self._stats[...]` is
assigned only after the check (`features.py:657`). **This satisfies §12.5.**

**Is every ratio/log/pct_change site guarded or provably safe?** I audited every
builder. All 8 division sites and both `log` sites carry an explicit guard, and
the remaining rolling/indicator helpers are division-free or zero-guarded:

| site | file:line | guard |
|---|---|---|
| `return_1` numerator+denominator | `features.py:875-876` | `_NON_FINITE_INPUTS` |
| `log_return_1` **numerator** | `features.py:882` | `_NON_FINITE_INPUTS` (log(0) = −inf) |
| `range_1` both sides | `features.py:888-890` | both sides |
| `return_{w}` / `log_return_{w}` loop | `features.py:893-894` | numerator **and** denominator |
| `price_ratio_sma_{w}` numerator+denominator | `features.py:901-903` | both |
| `volume_change_1` | `features.py:930` | `_NON_FINITE_INPUTS` (0 **is** a hazard in a denominator) |
| `volume_zscore_20` | `features.py:938-940` | `.replace(0, 1.0)`; no inf guard needed (see F2) |
| `obv` | `features.py:946` | `np.sign(inf)=±1` finite; `inf*0.0=NaN` → `fillna(0.0)`; cumsum of finite stays finite |
| `spread` passthrough | `features.py:962-963` | `_NON_FINITE_INPUTS` |
| `spread` computed | `features.py:969-971` | both sides |
| `order_book_imbalance` | `features.py:976-980` | both sides, incl. cancellation |
| signals passthrough | `features.py:1017-1023` | `_NON_FINITE_INPUTS_NO_ZERO` |
| `_rsi` | `features.py:1063` | `avg_loss.replace(0, np.nan)` |
| `_bollinger` width / %B | `features.py:1107-1110` | `mid.replace(0,nan)`, `.where(\|denom\|>1e-12, 0.5)` |

**No unguarded site found.** The zero-is-a-denominator / zero-is-data split is
correct and is pinned by test #3.

#### (a)(i) — the compute-seam docstring: **NOT ACCURATE. Claim falsified.**

The module claims (`features.py:789-791`) that the seam "covers the rolling
builders too (`sma`/`ema`/`rsi`/`macd`/`bollinger`/`atr`), which no per-expression
guard reaches", and the test named for it says
(`tests/test_feature_nonfinite_guards.py:703-708`):

> "An infinite close therefore reaches `close.rolling(4).mean()` as `inf` and
> comes straight back out as `inf`"

**Measured on pandas 3.0.4** (one `±inf` at index 100 of 200, window 20):

| aggregation | inf out | NaN out | value at idx 100 vs clean |
|---|---|---|---|
| `rolling(20).mean()` | **0** | 39 | `nan` |
| `rolling(20).std(ddof=0)` | **0** | 39 | `nan` |
| `rolling(4).mean()` | **0** | 7 | `nan` |
| `ewm(span=20, adjust=False).mean()` | **0** | **0** | **finite and WRONG** (144.975 vs 145.478 clean) |
| `ewm(alpha=1/14, adjust=False).mean()` | **0** | **0** | finite and wrong |

Two corrections:

1. **`rolling()` MASKS inf → NaN.** It does not propagate. NaN is repairable by
   `ffill().fillna(0)`, so the seam is **not** what stops an infinity escaping
   the rolling builders — pandas does that on its own.
2. **`ewm()` neither masks nor propagates — it SKIPS the bar**, returning a
   finite, subtly-wrong number. The docstrings at `features.py:1054-1056`
   (`_rsi`), `1098-1101` (`_bollinger`), `1121-1123` (`_atr`) all say
   "pandas' `ewm` masks a non-finite input as NaN (**measured**)". They are
   wrong, and the claim they support is the wrong claim.

**Consequence, proven by revert #6:** removing the seam
(`features.py:800-803`) leaves **all 31** tests passing — including the test whose
docstring asserts it "cannot" pass with the seam removed. **The seam is
entirely redundant**; every builder carries its own guard.

This is *not* a functional break: the guards that work are proven non-vacuous
(#1, #2d), and the end-to-end store-arm fix is real (§4). But a false mechanism
is asserted in five places and pinned by a test that cannot fail — which is the
2026-10-01 "absence-vs-observed" lesson recurring on the *explanatory* axis. The
residual real hazard `ewm` does introduce — a **silently wrong but finite**
feature that passes the guard — is unmentioned anywhere.

#### (a)(ii) `log_return_1` / `return_1` dead code: **CONFIRMED (conditionally)**

`out["return_1"]` (`features.py:883`) and `out["log_return_1"]`
(`features.py:884`) are overwritten by the `for w in self.windows` loop at
`features.py:893-894`, because `1 ∈ windows` — the module default is
`(1, 4, 24)` (`features.py:558`) and the shipped config is `feature_windows:
[1, 4, 24]` (`configs/default.yaml:43`). AST dump of `_add_price_features` in
source order confirms the FOR follows both assignments, and the formulas are
identical (`pct_change()` ≡ `pct_change(1)`, `shift(1)` ≡ `shift(1)`) — both
verified equal.

**Refinement to the report as filed:** it is dead *under the shipped
configuration*, not unconditionally. If a user configured `feature_windows`
without `1`, the loop would not emit those names and lines 883-884 would become
load-bearing. So this is a latent redundancy that must stay, not code to delete.

### (b) Width-neutrality for the LIVE arm — **PASS, byte-identical**

Controlled experiment: the live frame was captured **once** through the
production read seam (`read_ohlc_dataframe(..., market_data_store=None)`, 721
bars, 2026-09-02 16:00 → 2026-10-02 16:00) and cached, then the *same* frame was
pushed through the `FeaturePipeline` at both `aeace32` and `bfe32aa`. Holding the
frame constant removes the live API as a confounder.

| | `aeace32` (post-fix) | `bfe32aa` (pre-fix) | |
|---|---|---|---|
| raw bars | 721 | 721 | = |
| **n_features** | **60** | **60** | = |
| **start_index** | **24** | **24** | = |
| usable bars | 697 | 697 | = |
| column names | 60 identical | 60 identical | identical |
| **obs sha256** | `93edc733…068fa8` | `93edc733…068fa8` | = |
| **arr sha256** (float32 transform) | `6a88a379…84eab` | `6a88a379…84eab` | = |
| `cmp` byte-diff | — | — | **BYTE-IDENTICAL** |
| non-finite obs cells | 0 | 0 | = |

The live frame carries **0 zero-volume bars and 0 zero-price bars** (only 19
warm-up NaNs in `trade_count_zscore_20`), confirming §9A.3's "the live arm was
unaffected — 721 Kraken bars contain no zero-volume bar".

**Conclusion: no `models/` artifact is invalidated.** Confirmed.

### (c) The 76,562 vs 76,561 discrepancy — **RESOLVED: benign accumulation**

There are **four** numbers in circulation, and they are all the same store at
different wall-clock times. Each figure is *pre-warm-up* unless stated.

| figure | what it is | source | when |
|---|---|---|---|
| **76,561** | store bars, pre-warm-up | `RESEARCH.md:107`, `RESEARCH-3.md:117` | Phase 2/3 |
| **76,562** | store bars, pre-warm-up | `matrix/cells.jsonl` train `n_bars`, DECISION §9A.3 | 15:08 today |
| **76,538** | 76,562 − **24** warm-up → bars **replayed** | `matrix/cells.jsonl` backtest `n_bars` | 15:08 today |
| **76,563** | store bars, pre-warm-up | my `train` JSON | 17:41 today |
| **76,539** | 76,563 − 24 → bars replayed | my `backtest` `n_steps` | 17:41 today |

**Leading hypothesis CONFIRMED, with the mechanism.** The store arm's live
upsert leg re-fetches the trailing Kraken window on *every* read
(`data.py:1349-1352`: `candles = _page_candles(...); store.upsert(...)`). The
newest hourly bar advances with wall-clock time, so each run appends ~1 bar:

- Partition mtimes prove the footprint: `2026-09.parquet` and `2026-10.parquet`
  were rewritten at **15:49, 17:21 and 17:48** (my runs), while the 103 older
  partitions still carry **15:49** from the seed.
- Every store-arm log line reads `Upserted 721 60-minute ETH/USD bars into
  market-data store`.
- `2026-10.parquet` = 41 rows ending 2026-10-02 16:00 — i.e. the live leg's
  newest bar, growing hourly.

**Verdict: benign accumulation, NOT a defect.** Explicitly ruled out:
- **no double-counting** — 76,563 rows, 76,563 unique timestamps, 0 duplicates;
- **no window-edge move** — the pinned window `2020-01-01…2026-01-01` reads
  `kept 52577 of 76563` on every run; the trailing bars are outside the pin, so
  growth does not shift the pinned edge.

**But the docs must stop quoting a live count as a fixed figure.** "76,561" and
"76,562" are both snapshots. Also note the §9A.3 wording says "76,562 bars" —
that was true at 15:08 and is 76,563 now. Recommend: quote the figure with its
timestamp, or quote `n_bars` from a named run.

### (d) The +1800% seed — **MAJOR FINDING: it IS a degenerate artifact**

First, located it. From `matrix/cells.jsonl`, store arm, seed 44:

| seed | arm | n_bars | num_trades | trades/bar | return | Sharpe | maxDD | **excess** |
|---|---|---|---|---|---|---|---|---|
| 42 | store | 76,538 | 46,413 | 0.606 | +225.13% | 1.626 | 58.46% | **−19.48%** |
| 43 | store | 76,538 | 53,579 | 0.700 | +53.17% | 0.954 | 66.97% | **−191.84%** |
| **44** | **store** | 76,538 | **67,183** | **0.878** | **+1800.10%** | 2.654 | 79.92% | +1555.06% |
| 42 | live | 697 | 575 | 0.825 | +4.07% | 0.574 | 5.03% | −3.23% |
| 43 | live | 697 | 587 | 0.842 | −0.44% | −0.043 | 4.79% | −7.72% |
| 44 | live | 697 | 565 | 0.811 | +8.87% | 1.262 | 3.32% | +1.44% |

I re-ran seed 44 and **reproduced it**: frictionless gives **+2015.13%**
(vs +1800.10%; the delta is the 25 bars the store gained since 15:08 — 76,539 vs
76,538 replayed). So the number is stable, not a one-off glitch.

**Then I tested the four hypotheses.**

| hypothesis | verdict | evidence |
|---|---|---|
| (i) zero-volume boundary bars | **NO** | all 4 zero-volume bars are 2019-06-07 21:00, 2020-12-21 14:00, 2021-02-11 03:00, 2023-03-24 12:00 — **none at a month boundary**; 0 of 106 month-first bars have zero volume. They also contribute **0** to seed 44's behaviour: only 2 of 4 fall inside the 2020-01-01…2026-01-01 pin. |
| (ii) NaN-vwap bar | **NO** | the 4 NaN-vwap bars are the *same 4* zero-volume rows; and `vwap` is not an allow-listed observation column, so it never reaches the feature matrix. |
| (iii) window-edge artifact | **NO** | the matrix cells are **unpinned** (`since`/`until` null) — there is no window edge at all. |
| (iv) **frictionless 0 fee / 0 slippage compounding over 76k bars** | **YES — CONFIRMED, DECISIVE** | see below |

**The decisive experiment.** Same trained models, same 76,539 bars, same trade
counts — only the cost assumption changes. `fee_rate`/`slippage` are `0.0` in
every config used (`configs/default.yaml` inherited).

| seed | trades | frictionless | **Kraken taker** (0.26% fee + 0.05% slip) | swing |
|---|---|---|---|---|
| 42 | 46,761 | +219.73% | **−99.79%** | 319.5 pp |
| 43 | 53,624 | +53.68% | **−99.86%** | 153.5 pp |
| 44 | 67,148 | **+2015.13%** | **−88.18%** | **2103.3 pp** |
| — | — | median **+219.73%**, IQR **1961.45pp** | median **−99.79%**, IQR **11.68pp** | **IQR falls 168×** |
| *live 42* | *471* | *+4.41%* | *−1.06%* | *5.5 pp* |

**This is a degenerate artifact.** Three independent signatures:

1. **Cost-sensitivity inversion.** 31 bp round-trip moves the store arm by up to
   2,103 pp while moving the live arm (100× fewer trades) by 5.5 pp. The return
   is the *churn*, not an edge — seed 44 pays 67,148 round trips ≈ 416 pp of
   drag and still returns −88%.
2. **Monotone in trade count.** Return tracks `trades/bar` almost perfectly:
   0.606 → +225%, 0.700 → +53%, 0.878 → +2015%, while Sharpe rises with it
   (1.626 / 0.954 / 2.654) and maxDD deepens (58% / 67% / 80%). Both "wins" of
   the store arm are mechanical consequences of trading more.
3. **The spread is manufactured.** The store arm's IQR of 8.73 — the entire
   yardstick of the CAND-5 gate — **collapses to 0.117 (11.7pp) at realistic
   costs.** The gate is measuring friction, not seed variance.

**So yes — plainly: the dispersion gate is measuring noise-that-isn't-seed-variance.**
Two consequences for §9A.2:

- The headline `NOT SEPARATED` verdict on `excess_return` (ratio 0.037) is
  **still correct**, but §9A.2's stated reason — "the between-arm gap sits
  entirely inside seed noise" — is **not the real reason**. The real reason is
  that the gated metric has no cost basis at all. The verdict survives; the
  explanation must change.
- The two **RESOLVED** rows (`sharpe` 1.400, `max_drawdown` 10.74) are **also
  artifacts of the same churn**, not merely "confounded by horizon" as §9A.2
  says. Under costs, seed 44's Sharpe goes 2.717 → −0.550. §9A.2's horizon
  confound is real but is the *smaller* of the two.

Minimum honest remedy: **re-run the gate with non-zero `fee_rate`/`slippage`** on
both arms, then quote the ratios. Do not quote the frictionless dispersion
numbers as a seed-variance measurement. `configs/default.yaml` ships
`fee_rate: 0.0`/`slippage: 0.0`, and `configs/matrix.example.yaml:124` already
asks "does more training help, or does it just cost more?" — the answer here is
*it costs more*.

### (e) Wording — **PASS, with two required corrections**

Required wording is present verbatim at **DECISION.md:537-539**:

> "The store arm reached **76,562 bars** with **disjoint** train/eval splits …
> Honest out-of-sample result: **−34.3% return, Sharpe −0.568**. The CAND-5 gate
> reports **NOT SEPARATED** on both return metrics. **No claim of improvement.**"

Every hit for the forbidden claims:

| file:line | text | status |
|---|---|---|
| `DECISION.md:539` | "**No claim of improvement.**" | **REQUIRED — present** |
| `DECISION.md:533` | "It did **not** deliver better returns" | negation, correct |
| `DECISION.md:318` | "It cannot **prove** 76,561 bars is *better* than 721" | negation, correct |
| `DECISION.md:325-328` | "No report may describe this as '106× more training'" | **explicit prohibition — correct** |
| `DECISION.md:128` | "76,561 bars buys *diverse* experience … **not** 106× more gradient steps" | negation, correct |
| `DECISION.md:75` | "the **only** candidate that changes it by **106×**" | about **bars**, not training — acceptable but unlabelled |
| `RESEARCH-3.md:117` | "**76,561 bars — 106× the 721-bar ceiling** — from one config value, 0.48 s, zero code changes." | ⚠ **pre-amendment artifact**: stale count, no `NOT SEPARATED` |
| `RESEARCH.md:107` | "**shape (76561, 11) in 0.48 s** = **106× the 721 ceiling**" | ⚠ same |
| `configs/matrix.example.yaml:124` | "does more training help, or does it just cost more?" | correct framing |

**No instance of "106× more training" exists**; all three near-misses are about
**bars** and two are explicit prohibitions. ✓

Two corrections required:
1. **F5 — `DECISION.md` is internally inconsistent.** §3 (`:47`, `:85`, `:86`),
   §5.1 (`:127`), §8 (`:318`, `:325`) and §11 (`:605`) all say **76,561**, while
   §9A.3 (`:537`) says **76,562**. A reader who lands on §11 — the builder's own
   verification checklist — sees the stale figure and none of the §9A
   qualifications. §11 should be struck or annotated as superseded by §9A.
2. **`RESEARCH.md` / `RESEARCH-3.md` are not self-labelling.** They quote
   76,561 with **no** `IN-SAMPLE` caveat, **no** `NOT SEPARATED` verdict and
   **no** −34.3% OOS number. A future agent reading RESEARCH.md alone gets the
   pre-amendment picture. Add a one-line header pointing at DECISION §9A.

---

## 3. The gate — two separate matrices (user-mandated)

### 3a. The §9A.1 harness defect STILL REPRODUCES

`python tools/model_matrix.py report /tmp/krb-verify/matrix-cand5.yaml` against
the real 6-cell results file:

```
cells: 6 recorded, 3 valid (0 out-of-sample, 3 IN-SAMPLE), 3 INVALID

INVALID CELLS (3) — excluded from every aggregate
  0396722e7a1a  market_data_store=None, pages=1, seed=42  [n_bars=697 trades=575]
      - n_bars:697<0.50x76538
  d76622e63d7c  ... seed=43  [n_bars=697 trades=587]   - n_bars:697<0.50x76538
  dfdd93543409  ... seed=44  [n_bars=565]              - n_bars:697<0.50x76538

PER-TICKER SUMMARY            <- (empty)
PER-CONFIG SUMMARY            <- (empty)
PER-AXIS MARGINALS            <- (no valid cells)

WHAT THIS SAMPLE SUPPORTS
  - 3 of 3 valid cells are IN-SAMPLE ... and are excluded from every aggregate
  - 6 recorded cells yielded 0 usable out-of-sample measurements.
```

**No dispersion section is emitted at all. The gate never fires.** Mechanism
confirmed at `tools/model_matrix.py:1059-1063`: `denominator = expected_bars
or reference_bars`, and `reference_bars` is the ticker's largest observed
`n_bars` (76,538), so the 0.5 guard demands ≥ 38,269 and the live arm's 697
fails by **55×**. Exactly as §9A.1 describes. **Not fixed — deferred to Phase 7
per user decision.** ✓

### 3b. The two-matrix method WORKS

Each arm scored against its **own** `min_bar_ratio` reference, then the shipped
`pooled_within_spread` (`tools/model_matrix.py:416`) and `dispersion_verdict`
(`:444`) applied across the two replicate sets. No threshold invented —
`DISPERSION_RATIO_THRESHOLD = 1.0` (`:376`) and
`MIN_REPLICATES_FOR_A_CLAIM = 3` (`:258`) both read from the module.

**Step 1 — per-arm guard, own reference:**

| arm | cells | observed `n_bars` | reference | 0.5× | survivors | OOS |
|---|---|---|---|---|---|---|
| LIVE | 3 | 697, 697, 697 | 697 | 348 | **3/3 VALID** | 0 (unpinned ⇒ IN-SAMPLE, correct) |
| STORE | 3 | 76,538 ×3 | 76,538 | 38,269 | **3/3 VALID** | 0 (unpinned ⇒ IN-SAMPLE, correct) |

**Step 2 — ratios (ratio ≥ 1.0 ⇒ RESOLVED):**

| metric | live median | store median | gap | live IQR | store IQR | pooled | **ratio** | **verdict** |
|---|---|---|---|---|---|---|---|---|
| `excess_return` (headline) | −3.23% | −19.48% | 16.24% | 0.0458 | **8.7345** | 4.3901 | **0.037** | **NOT SEPARATED** |
| `total_return` | +4.07% | +225.13% | 221.06% | 0.0465 | 8.7346 | 4.3906 | **0.503** | **NOT SEPARATED** |
| `sharpe` | +0.574 | +1.626 | 1.052 | 0.6524 | 0.8500 | 0.7512 | **1.400** | RESOLVED |
| `max_drawdown` | +4.79% | +66.97% | 0.6218 | 0.0085 | 0.1073 | 0.0579 | **10.736** | RESOLVED |

**Reproduces DECISION §9A.2 exactly** (0.037 / 0.503 / 1.400 / 10.74). The
method works and is the correct interim instrument.

**`NOT SEPARATED` is a valid, non-failing outcome** — reported as-is, and the
gate did its job: it refused to rank two arms whose gap sat inside their own
spread.

⚠ **But per finding (d) above, both RESOLVED rows and the headline yardstick are
friction artifacts.** The store IQR of 8.73 is 873 pp of *unpriced churn*, not
seed variance. This gate must be re-run with costs before any number in it is
quoted as evidence.

---

## 4. Integration test — **PASS** (magnitude clause satisfied)

Both arms trained + backtested end to end, plus the pinned store arm with a
disjoint split. `n_features == 60` and `start_index == 24` are hard asserts.

| arm | model | `n_bars` (train) | **bars replayed** | `n_features` | **`start_index`** | **`num_trades`** | trades/bar | return | Sharpe | maxDD | buy&hold | excess | **scope label** |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **LIVE**, unpinned | `vLive` | 721 | **697 / 697** ✅ | **60** ✅ | **24** ✅ | **471** ✅ | 0.676 | +4.41% | 0.567 | 5.05% | +7.86% | −3.44% | `IN-SAMPLE` |
| **STORE**, unpinned | `vStore` | 76,563 | **76,539 / 76,563** ✅ | **60** ✅ | **24** ✅ | **46,763** ✅ | 0.611 | +219.08% | 1.604 | 59.57% | +242.66% | **−23.59%** | `IN-SAMPLE` |
| **STORE**, pinned 2020→2026 | `vPinned` | 36,804 | **15,749 / 15,773** ✅ | **60** ✅ | **24** ✅ | **11,866** ✅ | 0.752 | **−34.30%** | **−0.568** | 54.22% | −19.46% | −14.84% | `OUT-OF-SAMPLE` |

**`start_index == 24` confirmed three independent ways:** every run reports
`bars_replayed == n_bars − 24` (697 = 721−24; 76,539 = 76,563−24; 15,749 =
15,773−24), and the feature pipeline reports `start_index = 24` directly
(`first_tradable_index`, `features.py:173-203`).

**Magnitude clause — satisfied.** No run silently replayed ~1 bar: 100.0%, 99.97%
and 99.85% of each frame. Trade counts are 471 / 46,763 / 11,866, all far from
the degenerate-cell floor. (`equity_curve` lengths 698 / 76,540 / 15,750
corroborate the step counts.)

**Disjoint train/eval confirmed in real output.** Log line:
`data_window [2020-01-01T00:00:00+00:00, 2026-01-01T00:00:00+00:00) eval_split=0.7
kept 52577 of 76563 bars` then
`-> 15773 replayable bars (36804 training bars held out) — OUT-OF-SAMPLE`.
15,749 + 36,804 + 24 = 52,577 ✓.

**The scope label DOES appear in real output** — three surfaces, all verified:
- machine: `"evaluation_scope_label": "OUT-OF-SAMPLE"`, `evaluation_is_out_of_sample: true`
- `--json`: same keys, plus `n_train_bars` / `n_eval_bars` / `n_overlapping_bars`
- human table + log stream: `Evaluation: OUT-OF-SAMPLE` and
  `[OUT-OF-SAMPLE]: 15749/15773 bars replayed, return=-8.50% …`

**§9A.3's honest-OOS table reproduces to the digit.** Mine vs §9A.3:
`total_return` −34.295% vs −34.30% · `sharpe` −0.5682 vs −0.568 ·
`max_drawdown` 54.22% vs 54.22% · `num_trades` 11,866 vs 11,866 ·
replayed 15,749/15,773 vs 15,749/15,773 · buy&hold −19.46% vs −19.46% ·
excess −14.84% vs −14.84%. **The builder's measurement was accurate.**

**Pinned-without-store fails loudly and names the seed recipe** ✅. Exit 1, and
the message contains: the requested range, the available span and bar count, the
cause, Kraken's ~721-bar ceiling, and the remedy —
`just store-plan` (zero-network dry run) then `just store-seed`, **~158s and
~13MB** of Binance-archive monthly klines, plus the `store_mode == "market-data"`
guard and the fallback-csv trap. Verbatim excerpt:

> "Fix: seed the store and point market_data_store at it -- seed it with `just
> store-plan` (a zero-network dry run) then `just store-seed` -- ~158s and ~13MB
> of Binance-archive monthly klines for ETH/USD, BTC/USD, SOL/USD or XRP/USD at
> 60-minute bars. `just store-seed` refuses to finish unless the report says
> store_mode == "market-data"; a fallback-csv seed writes .csv that the store
> reader (which globs *.parquet) cannot see, so the run would silently read ~721
> live bars instead of years."

### F3 (new) — `market_data_store: null` does not turn the store off

My first attempt at the pinned-without-store test **succeeded when it should have
failed** (exit 0). Cause: I ran it against `vStore`, whose *own* baked
`config.yaml` carries `market_data_store: /tmp/krb-verify/store`, and passed a
run config with `market_data_store: null`. The log shows it reading the store
anyway: `Reading 60-minute ETH/USD bars from the market-data store at
/tmp/krb-verify/store`. Confirmed directly against the shipped resolver
(`backtest.py:282-307`, `_resolve_env_setting`):

| run config | model config | resolved |
|---|---|---|
| `null` | `/m/store` | **`/m/store`** ← null is SKIPPED |
| `/run/store` | `/m/store` | `/run/store` |
| `null` | `null` | `None` |

This is deliberate in code ("YAML `key: null` is how a config says *unset*, not
*set to nothing*", `backtest.py:298-300`). **But one shipped remedy message
advertises the opposite** — `data.py:267`, inside
`MarketDataStoreUnavailableError`, is the only place in the tree that says *"Set
market_data_store: null in the config to go back to the live paginated fetch
(~721 bars)."* (verified by grep across `kraken_trading_bot/` and `tools/`;
`PinnedWindowUnavailableError` does **not** carry it, and it is echoed in spirit
by DECISION §10.3 and `configs/deep-history.example.yaml:11-12`).

For a store-trained model that instruction **does not work**: the reader edits
their run config, the resolver skips the `null`, the model's own
`config.yaml` wins, and they silently keep 76k bars while believing they went
live. It needs either a working override (honour an explicit sentinel such as
`""` or a `--no-store` flag) or wording that names *the model's own* config as
the thing to edit. Severity is bounded — reaching it requires a store-trained
model plus a store that has since become unresolvable — but the message is the
one place a user is told how to recover, and it does not work.

### F6 (new) — the store has 157 missing hourly bars

Not previously reported anywhere. `/tmp/krb-verify/store` holds **76,563 bars**
over a span of **76,721 hours → 157 missing**, across **28 gaps**, max single gap
**39 hours**.

| | |
|---|---|
| largest gap | `2026-08-31 23:00 → 2026-09-02 14:00` — **38 bars missing** |
| next | 10 bars (2018-06-26), 10 bars (2019-05-15), 8 bars (2019-08-15), 7 ×2, … |

Two consequences:

1. **The 38-bar hole is at the seed/live-append seam, in the most recent month**
   — precisely the region a live deployment would trade. `2026-09.parquet` starts
   at 2026-09-02 14:00; nothing covers 2026-09-01 00:00 → 2026-09-02 13:00.
2. **Features are computed on bar counts, not wall-clock.** `return_{4}`,
   `sma_24`, `rsi_24`, `obv_slope_{24}` and `start_index = 24` all assume
   consecutive hourly bars. Across a 39-hour jump, `return_1` reports a 39-hour
   return as though it were a 1-hour return, and the 24-bar warm-up consumes
   24 *rows* that span more than a day. `store-verify` should surface gap count
   and max gap; the store arm's provenance (`market_data_store_venue`) should
   record that the seed and the live leg are **two venues joined without an
   overlap check**.

**Also (F4): the "partial-month boundary bar" description is wrong.** All 4
zero-volume bars are mid-month (`2019-06-07 21:00`, `2020-12-21 14:00`,
`2021-02-11 03:00`, `2023-03-24 12:00`); 0 of 106 month-first bars have zero
volume; and 2 of the 4 sit **immediately before a gap** (2020-12-21 14:00 → gap
of 3; 2021-02-11 03:00 → gap of 1). They are **exchange-outage no-trade bars**,
not archive-boundary artifacts — a materially different diagnosis, and one that
points at data integrity rather than at the Binance monthly format.

---

## 5. Answers to the brief's five questions, condensed

| item | answer |
|---|---|
| `python -m pytest -q` | **429 passed** (exact count), 24 warnings, 34.46 s |
| `nix flake check --no-build` | **green** |
| non-vacuity | 5 named guards proven non-vacuous; **compute seam vacuous + false claim**; 4 redundant-and-unpinned sites |
| (a) inf coverage | **complete**, no unguarded site; hard check on both `fit` and `transform` ✓. **compute-seam docstring inaccurate; `return_1`/`log_return_1` dead-under-shipped-config confirmed** |
| (b) width-neutrality | **PASS — byte-identical**, 60 features, `start_index` 24. No `models/` artifact invalidated |
| (c) bar count | **RESOLVED: benign accumulation.** Store grows ~1 bar/run via the live upsert leg (`data.py:1349-1352`). 0 duplicates; pinned edge unmoved. But docs quote stale live counts inconsistently |
| (d) +1800% seed | **MAJOR — degenerate friction artifact, NOT seed noise.** +2015% → −88% at taker costs; IQR 1961pp → 11.7pp across seeds; the gate's yardstick measures cost |
| (e) wording | **PASS.** Required sentence present at `DECISION.md:537-539`; no "106× more training" anywhere. Two corrections: `DECISION.md` internal inconsistency, `RESEARCH*.md` not self-labelling |
| two-matrix gate | §9A.1 defect **reproduces exactly**; two-matrix method **works**, reproduces §9A.2 ratios. Headline **`NOT SEPARATED`** |
| integration | **PASS**, magnitude satisfied on all three runs; labels appear on all three surfaces; pinned-without-store names the recipe |

## 6. Could not be completed

- **Pinned-without-store against a store-trained model** — cannot be tested as
  intended, because of F3: `market_data_store: null` in a run config cannot
  override the model's own store. I tested the *correct* case instead (a model
  trained without a store, `vLive`, backtested with a pinned window), which fires
  correctly. F3 is reported as a defect instead.
- **No `USD_SOL` arm.** Confirmed out of scope; the store is ETH_USD-only.
- **Phase 7 harness fix** — deliberately not attempted (user decision 4).

## 7. Reproduce

```
nix develop --command bash -c "python -m pytest -q"          # 429
nix flake check --no-build
bash /tmp/krb-verify/vacuity/battery.sh                       # 12 reverts
bash /tmp/krb-verify/vacuity/battery2.sh                      # cases 4/4b/4c, 2d/2e
nix develop --command python /tmp/krb-verify/gate2.py         # two-matrix gate
bash /tmp/krb-verify/integ.sh                                 # integration, 3 runs
bash /tmp/krb-verify/seed44.sh && bash /tmp/krb-verify/friction3.sh   # friction
```
