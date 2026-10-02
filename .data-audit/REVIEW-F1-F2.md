# NARROW RE-REVIEW — F1 and F2 only

Reviewer: `reviewer-final` (team `audit-pipeline-1002`), Phase 7 exit.
Reviewed HEAD: **`91c76a9`**. Tree clean, `455 passed`, flake green — all three
re-confirmed by me, not taken on trust.

**Read-only on source.** Nothing was repaired. One file written (this one).
Scratch in `/tmp/rev-f1/`.

# VERDICT: `NEEDS_FIX` — narrow

**F1 is clean. The width-neutrality hard assertion passes byte-for-byte. F2's
structural repair is genuine and the vacuous test is genuinely dead.** One
defect survives, and it is the same *class* of defect F2 was raised to remove:
a false mechanism claim, left in a docstring, in the same commit.

| # | finding | severity |
|---|---|---|
| **R1** | **`_rsi`'s docstring makes two false claims about how the seam protects RSI**, and one of them **contradicts the `compute` comment added in the same commit**. Its stated mechanism is not what happens; I measured the opposite. | **material** |
| R2 | §14.4 misdescribes what §10.4 defines, by one clause — in the very paragraph arguing that reusing an id for the wrong thing is a false claim. | minor |
| R3 | §14.0's "**~145× more absolute cost**" is **not reproducible** from any field in the backtest JSONs. Two measurable proxies give **34×** and **92×**. | minor |
| R4 | §14.0's "re-enters every ~1.2 bars **in both arms**" holds for the live arm (1.216) but not the store arm (median **1.43**). | minor |
| R5 | `DECISION.md` contains **zero** occurrences of "F2" or "F3". The F2 and F3 resolutions are recorded only in code. F4 and F6 got records. | minor |

Nothing here invalidates a number, a `models/` artifact, or a verdict. R1 is a
truthfulness defect in prose and needs a comment edit, not code.

---

## 0. Baseline re-confirmed

| check | observed | verdict |
|---|---|---|
| unit suite | **455 passed**, 24 warnings, 56.5 s | PASS |
| flake | `all checks passed!` | PASS |
| tree | `git status --short` empty at `91c76a9` | PASS |
| pandas in the dev shell | **3.0.4** (matches what the docstrings claim to have measured on) | — |

---

## 1. F1 — the cost-aware re-run

### 1.1 The threshold was not tuned — **PASS**

`DISPERSION_RATIO_THRESHOLD = 1.0` at `tools/model_matrix.py:376`, with the
"this is a JUDGEMENT CALL, the ratio is the fact" comment intact above it.

Stronger than the check asked for: **the measurement track has zero diff.**

```
$ git diff --stat 97a2a52 HEAD -- tools/
 tools/store_gap_scan.py | 433 +++++++++++++++++++   <- F6's NEW tool, not model_matrix
```

`tools/model_matrix.py` — every threshold, `summarize`, `pooled_within_spread`,
`dispersion_verdict`, `replicated_groups` — is **byte-identical to the
pre-registration commit**. No gate parameter was moved, and no gate *code* was
moved to produce the verdicts.

I also checked the code track cannot have contaminated the recorded backtests.
The cost-aware run executed 18:39–18:42; the code-track commits are all
18:44+. That ordering alone would have been suspicious, so I compared the three
files the run depends on at AST level, docstrings stripped **and every string
constant blanked**:

| file | AST identical ignoring all strings |
|---|---|
| `features.py` | **True** |
| `data.py` | **True** |
| `backtest.py` | **True** |

The only non-docstring delta anywhere is the F3 *error-message* string. So
`§14.0`'s recorded numbers remain valid at HEAD.

### 1.2 The §14.0 numbers are real, with no hand arithmetic — **PASS**

Re-derived from the per-seed records in `/tmp/krb-cost-aware/`, by a script of
my own that does **not** reuse the builder's `score.py`. Each metric was scored
twice: once through the shipped `MM.summarize` / `MM.pooled_within_spread` /
`MM.dispersion_verdict`, and once through a from-scratch estimator
(`np.median`, `np.percentile`, `statistics.median` of the two group IQRs).
**The two paths agreed to <1e-12 on every metric, both bases.**

| metric | live median | store median | pooled IQR | gap | ratio | verdict |
|---|---|---|---|---|---|---|
| `excess_return` | −12.4367 (**−12.44** ✓) | −341.2240 (**−341.22** ✓) | 3.9658 (**3.97** ✓) | 328.787 (**328.79** ✓) | **82.9052** (**82.91** ✓) | **RESOLVED** ✓ |
| `total_return` | −5.2568 (**−5.26** ✓) | −99.7632 (**−99.76** ✓) | 3.8912 (**3.89** ✓) | 94.506 (**94.51** ✓) | **24.2872** (**24.29** ✓) | **RESOLVED** ✓ |
| `sharpe` | −0.7193 (**−0.719** ✓) | −4.9758 (**−4.976** ✓) | 1.4036 (**1.404** ✓) | 4.2566 (**4.257** ✓) | **3.0327** (**3.03** ✓) | **RESOLVED** ✓ |
| `max_drawdown` | 8.5109 (**8.51** ✓) | 99.8471 (**99.85** ✓) | 1.6944 (**1.69** ✓) | 91.336 (**91.34** ✓) | **53.9052** (**53.91** ✓) | **RESOLVED** ✓ |

All **20** figures reproduce at the doc's printed precision. The verdict column
is not hand arithmetic — it is whatever `dispersion_verdict` returned.

The frictionless control also reproduces: `excess_return` 0.0313 / `total_return`
0.5022 / `sharpe` 1.2981 / `max_drawdown` 10.7885, and the per-seed store
control **+224.86 / +52.98 / +1800.10** — matching §14.0's recorded
**+225.13 / +53.17 / +1800.10** claim to the digit it quotes.

**And the frictionless *original* is on disk**, at
`/tmp/krb-verify/gate-two-matrix.json`, which independently confirms §13.2's
superseded rows: `excess_return` **0.0370**, `total_return` **0.5035**,
`sharpe` **1.4002**, `max_drawdown` **10.7361** — the four figures §13.2 says
are "retained only as a labelled diagnostic". All four match.

### 1.3 Both arms lose money in all six cells — **PASS**

Straight from the six cost-basis JSONs:

| arm | seed | total_return | excess_return | final_equity |
|---|---|---|---|---|
| live | 42 | −5.26% | −12.44% | $9,474.32 |
| live | 43 | −9.38% | −16.56% | $9,062.35 |
| live | 44 | −1.87% | −9.06% | $9,812.83 |
| store | 42 | −99.76% | −341.22% | **$23.68** |
| store | 43 | −99.88% | −341.64% | **$12.03** |
| store | 44 | −91.82% | −333.28% | **$818.02** |

`total_return < 0 and excess_return < 0 and final_equity < 10000` in **6 of 6**.
Equities match §14.0's `$23.68 / $12.03 / $818.02` exactly.

§14.0 states it plainly and without hedging: *"the strategy loses money after
costs in BOTH arms, in all six cells"*, calls the store result *"a wipeout, not
a drawdown statistic"*, and says **"No 'store arm improved' claim is made in
either direction."** I grepped §13/§14.0/§14.4 for
`improv|better|superior|win|outperform|helps|gain|edge`: every hit is either an
explicit **disclaimer** or unrelated prose. **No directional framing in either
direction.**

### 1.4 §14.4's self-correction is honest — **PASS**, with a nuance

Asked to say plainly if the correction is wrong or self-serving. **It is
neither.** All four of its numbers check out:

| claim in §14.4 | derivation | result |
|---|---|---|
| "ratio ≈ 1.4" from 16.24pp / 11.7pp | 16.24 / 11.68 | **1.3904** ✓ |
| measured ratio 82.91 | 328.7874 / 3.9658 | **82.9052** ✓ |
| "~59× off" | 82.9052 / 1.3904 | **59.6×** ✓ |
| gap "moved it 20×" | 328.79 / 16.24 | **20.2×** ✓ |

The 16.24pp it projects from is real and traceable: `gate-two-matrix.json`
`/excess_return/gap = 0.1624381054713473` = **16.2438pp**. The −322pp it quotes
is also real: that file's `store_median = −0.194762714920389` → −341.224%
gives **−321.75pp**. Neither number was fitted to the outcome.

**It is not self-serving, on three grounds:**

1. **It reports the LARGER, more decisive number.** 1.4 → 82.91. A correction
   that quietly adopted a *smaller* number would be the suspicious move; this
   went the other way.
2. **It cannot be verdict-rescuing.** Both figures sit above the pre-registered
   1.0, so the verdict is `RESOLVED` either way. §14.0 says so explicitly:
   *"The verdict is unaffected — both figures sit on the same side of a threshold
   fixed and committed in advance."*
3. **It discloses BOTH input misses.** It states measured pooled **3.97pp**
   (predicted 11.7pp, a 2.9× miss) *and* the gap move. A self-serving account
   would have hidden the pooled miss and blamed only the gap. It named both,
   then correctly diagnosed which dominates.

**The nuance.** §14.4 presents the pooled half as if it were a legitimate
prediction that merely went stale. It was not: **11.68pp is Phase 6's
store-arm-ONLY IQR, measured on a 76,539-bar store snapshot** (VALIDATION.md §(d)),
whereas the shipped estimator `pooled_within_spread` takes the **median of both
arms'** IQRs, on a 76,540-bar snapshot. So the pre-registered half-ratio mixed
a different *definition* with a different *snapshot*. §14.4's diagnosis — "held
the gap fixed while swapping a cost-aware pooled IQR" — is right about the gap
and incomplete about the pooled. **Disclosed, not concealed** (it prints the
3.97pp), so this is a precision note on the diagnosis, not a correctness
failure. Worth one extra clause, no more.

**The most defensible sentence in §14.0** is the one that concedes the thing the
correction could most easily have buried: *"The live arm drifts… because it
re-fetches a rolling REST window. A live-arm backtest cannot be replayed
bit-identically across days; a store-backed one can."* Volunteering the
disconfirming half of the control, and turning it into an argument for what the
pass built, is the behaviour the pre-registration was for.

### 1.5 Pre-registration genuinely preceded the result — **PASS**

```
$ git merge-base --is-ancestor 97a2a52 ae8bdd5  ->  exit 0
97a2a52  2026-10-02 18:34:24 +0100  docs: PRE-REGISTER the dispersion rule before the cost-aware re-run
ae8bdd5  2026-10-02 18:45:48 +0100  docs: cost-aware gate result - RESOLVED on all four metrics
```

The pre-registration is a **direct ancestor** of the result commit, 11 minutes
earlier, on a linear edge (`97a2a52 → ae8bdd5`, with the code track branching
from the same parent). The exercise is not invalidated. The scoring script also
`assert THRESHOLD == 1.0` and takes it from the module rather than passing a
literal — so even a silent edit would have aborted the run.

---

## 2. The hard assertion — width-neutrality

**PASS. Byte-identical, not merely hash-equal.**

Recomputed at HEAD from the Phase-6 cached live frame
(`/tmp/krb-verify/live721.parquet`, 721 bars, 2026-09-02 16:00 → 2026-10-02
16:00), through the production `FeaturePipeline`, so the live API is not a
confounder:

| | expected (VALIDATION.md §(b)) | measured at `91c76a9` |
|---|---|---|
| **obs sha256** | `93edc733…068fa8` | **`93edc7333cb8f5051556232f2acaadeecdae598e1abdb761850f1772e3068fa8`** ✓ |
| **arr sha256** | `6a88a379…84eab` | **`6a88a379111a7c63fb9e85ee86e98005a391da4519ca40edc6bf4a8d4a284eab`** ✓ |
| **n_features** | 60 | **60** ✓ |
| **start_index** | 24 | **24** ✓ |
| raw / usable bars | 721 / 697 | **721 / 697** ✓ |
| non-finite obs cells | 0 | **0** ✓ |

And beyond the hashes:

```
$ cmp head.obs.npy       width_post.obs.npy       -> BYTE-IDENTICAL
$ cmp head.arr_stats.npy width_post.arr_stats.npy -> BYTE-IDENTICAL
```

Both against the Phase-6 *pre-fix* files too (`width_pre.*` hash to the same two
digests). **No `models/` artifact is invalidated.** This is the strongest
available form of the check.

---

## 3. F2 — the vacuous test and the docstrings

### 3.1 Mutation-proven — **PASS**

Cloned `91c76a9` to `/tmp/rev-f1/mut`, deleted the seam's three lines
(`close`/`high`/`low` `.replace(_NON_FINITE_INPUTS, np.nan)`), confirmed by
reflection that the mutated module was the one imported, then ran the file
unmodified.

| | result |
|---|---|
| control (HEAD, unmutated) | **33 passed** |
| **mutated (seam deleted)** | **1 failed, 32 passed** |

The failing assertion, verbatim:

```
AssertionError: sma_4 reported a finite value across the poisoned window
([91.41316565819656, 91.53511084148846, 91.65717796996366, 91.77936716556732])
instead of NaN: the compute() seam is what stops a zero price entering the
rolling window
```

`91.41` against a ~122 price — **exactly the original symptom**. The Phase-6
finding was *"deleting the compute seam left all 31 tests green"*. Now deleting
it takes down exactly one test, and it is the right one.

I also reproduced the other quoted symptom values myself on the mutated module,
none of them fabricated:

| column | claimed | measured | quoted "healthy" |
|---|---|---|---|
| `sma_4` | 91.41 | **91.4132** | ~122 price (122.13) ✓ |
| `bb_upper_4` | 196.97 | **196.97** | |
| `bb_lower_4` | −14.14 | **−14.14** | |
| `bb_width_4` | 2.3094 | **2.3094** | |
| `bb_pctb_4` | 0.0670 | **0.06699** | |
| `rsi_4` | 0.2979 | **0.2979** | |
| `atr_4` | 30.87 | **30.8664** | 0.4866 — **63.4×** ✓ |

The test now pins the seam's *real* effect — the **zero** path — which is the
honest characterisation: `_NON_FINITE_INPUTS = [0.0, inf, -inf]`, and a zero
price is far likelier in real OHLCV than an infinity.

### 3.2 Docstrings vs measured pandas — **R1, ONE STILL FALSE**

I measured pandas 3.0.4 myself (one `+inf` at index 3 of 10):

| aggregation | inf out | NaN out | value at idx 3 |
|---|---|---|---|
| `rolling(5).mean()` | **0** | 8 | `nan` |
| `rolling(5).std(ddof=0)` | **0** | 8 | `nan` |
| `rolling(3).mean()` | **0** | 5 | `nan` |
| `ewm(span=5, adjust=False).mean()` | **0** | **0** | `1.888889` |
| `ewm(alpha=1/14, adjust=False).mean()` | **0** | **0** | `1.209184` |

**`rolling` masks. `ewm` skips. Confirmed.** `ewm[3] == ewm[2]` exactly — the
bad bar never entered the average.

Four of the five corrected docstrings are true. I checked each specific claim,
not the summary:

| docstring | claim | verdict |
|---|---|---|
| `compute` | "`rolling(w)` masks the bad value to NaN … the bad bar and its whole window read NaN" | **TRUE** (`sma_4[200..203]` all `nan`) |
| `compute` | "no `isinf` assertion can ever observe this seam" | **TRUE** (0 inf out of `sma_4`/`bb_lower_4`) |
| `compute` | "`ewm()` skips the bar … 0 infinities, 0 NaNs" | **TRUE** (`ema_4`: 0/0) |
| `compute` | "`_NON_FINITE_INPUTS` contains `0.0`" | **TRUE** (`[0.0, inf, -inf]`) |
| `compute` | the 400-bar zero-price measurements | **TRUE** (see §3.1) |
| `compute` | KNOWN GAP: `_require_finite` cannot see the ewm result | **TRUE** |
| `_bollinger` | "`rolling` masks a non-finite input … rather than propagating it as an infinity" | **TRUE** (`bb_upper_4[200]`, `bb_lower_4[200]` both `nan`) |
| `_bollinger` | "a caller reaching `_bollinger` directly gets NaN bands without any error" | **TRUE** (`upper[200] = nan`, no exception) |
| `_atr` | "`DataFrame.max(axis=1)` skips the NaN rows, so `tr` is finite across the bad bar" | **TRUE** (0 non-finite of 400) |
| `_atr` | "a zero price inflates `tr` to 30.87 against a healthy 0.487" | **TRUE** (63.4×) |
| **`_rsi`** | **"a NaN input makes `rs` NaN, which `fillna(50.0)` then maps to the neutral reading"** | **FALSE** |
| **`_rsi`** | **"The seam is what keeps that [finite-wrong RSI] unreachable from here."** | **FALSE** |

#### R1 — the `_rsi` docstring, in detail

`features.py:1091-1102` claims:

> "…`compute`'s seam maps a non-finite close to NaN *before* `diff()` runs, so
> `gain`/`loss` never see an infinity, and **a NaN input makes `rs` NaN**, which
> `fillna(50.0)` then maps to the neutral reading."
>
> "The residual hazard is that `ewm` never yields a NaN of its own: a bar
> skipped by the recursion leaves a *finite, wrong* RSI behind … **The seam is
> what keeps that unreachable from here.**"

Both load-bearing clauses are false, and they contradict each other and the
`compute` comment added in the **same commit**.

The premise is true (the seam does map the bad price to NaN). The **inference
is false**: `ewm` skips NaN just as it skips `inf`, so `avg_gain`/`avg_loss` come
back **finite**, `rs` stays finite, and `fillna(50.0)` **never fires**.

The 50.0 that the new test does see comes from a **different route entirely**:
the test's `_frame()` is monotonically rising, so `avg_loss == -0.0` everywhere
and `avg_loss.replace(0, np.nan)` supplies the NaN — the both-sides-zero guard,
documented *four lines above* in the same docstring. On a frame that actually has
losses, that route cannot fire and the shipped pipeline does not produce 50.0:

```
frame      avg_loss>0?      rsi_4[199..202]   50.0 route?
rising         True     [65.86, 65.86, 65.86, 76.30]     False
choppy         True     [42.13, 42.13, 42.13, 50.82]     False
mixed          True     [63.23, 63.23, 63.23, 68.75]     False
```

Mechanism, isolated (seam applied, frame has losses, zero price at bar 200):

```
avg_gain[199..202] = [0.4245, 0.4245, 0.4245, 0.3783]
avg_loss[199..202] = [0.5831, 0.5831, 0.5831, 0.3661]   <- nonzero, so replace(0, nan) does NOT fire
rs[199..202]       = [0.7280, 0.7280, 0.7280, 1.0332]   <- FINITE: ewm SKIPPED the NaN bar
_rsi(close,4)      = [42.1297, 42.1297, 42.1297, 50.8158]
```

And end-to-end through the shipped pipeline, zero price at bar 200:

| column | [199] | [200] | [201] | [202] |
|---|---|---|---|---|
| `sma_4` | 125.2279 | **NaN** | **NaN** | **NaN** |
| `bb_lower_4` | 123.8610 | **NaN** | **NaN** | **NaN** |
| `ema_4` | 125.3971 | **125.3971** (carried) | 124.8543 | 124.7789 |
| **`rsi_4`** | 42.1297 | **42.1297** (carried) | 42.1297 | 50.8158 |
| `atr_4` | 1.4574 | **1.4574** (carried) | 1.2551 | 1.1413 |

`rolling` families read the poisoned window as **unknown**. The `ewm` families
read it as a **stale carried value**. The seam did what it can: it stopped a
−100% print from entering the window. What it cannot do is make an `ewm` window
unknown — and `_rsi` says it does.

**The contradiction, same commit, both files:**

- `compute` (`features.py:820-826`): *"KNOWN GAP, not fixable here: because `ewm`
  returns a finite wrong number rather than a NaN, the `ema`/`macd`/**`rsi`**/`atr`
  families survive a skipped bar and `_require_finite` cannot see it."* ← **correct**
- `_rsi` (`features.py:1099-1102`): *"…a finite, wrong RSI behind … **The seam is
  what keeps that unreachable from here.**"* ← **false**

One of these has to go. Measurement says `_rsi`.

**Blast radius — deliberately bounded.** No executable line changes; the guards
are unchanged; `_require_finite` cannot see this either way; the `compute`
comment already records it correctly as a known gap. So this is a **truthfulness
defect in prose**, not a functional regression and not a new gap in coverage.
It is material rather than minor because F2's entire existence is "this comment
asserts a pandas mechanism, and the assertion is backwards" — and the repair
leaves a backwards assertion standing next to its own refutation.

*Suggested repair (lead's to make — I changed nothing):* delete the "a NaN input
makes `rs` NaN → `fillna(50.0)`" route and the "unreachable from here" clause;
state that the seam makes the bar *skippable* rather than *unknown*, that on a
frame with losses RSI then reports a stale carried value, and that the `compute`
KNOWN-GAP note is the governing record. Optionally note that the 50.0 seen in the
test comes from the both-sides-zero guard, not from the seam.

### 3.3 The ewm hazard is a recorded known gap, and its pin bites — **PASS**

**Recorded.** `compute`'s `KNOWN GAP, not fixable here` block names the hazard,
names the affected families, says `_require_finite` cannot see it, names the
pinning test, and says fixing it is out of scope for this pass. The test's own
docstring says the same and adds *"If this test ever starts failing because
pandas changed the behaviour, that is good news."*

**The pin bites.** I proved it rather than reading it. Note the trap I hit: my
first simulation fed `ewm` a **NaN** instead of an `inf`, on the theory that
"pandas now masks". That is **not a behaviour change at all** — pandas 3.0.4's
`ewm` skips NaN exactly as it skips inf (measured: feeding NaN still yields 0
NaNs), so the test correctly stayed green and my simulation proved nothing. The
honest simulation is at the output boundary. Swapping in an `ewm` that returns a
masked result and running the repo's **unmodified** test function:

```
control — real pandas 3.0.4:  the ewm pin test: PASSED
MUTATION — ewm now MASKS:      the ewm pin test: FAILED
   AssertionError: ewm no longer skips a non-finite input
```

The pin is not vacuous in either direction.

### 3.4 The old inf assertion was KEPT and LABELLED — **PASS**

This is the repair that most easily goes wrong the other way, so I diffed it
specifically. `ee08b26` **renamed** the old test; it did not delete it.

- old: `test_compute_seam_is_the_only_guard_for_the_rolling_technical_group` (at `97a2a52:701`)
- new: `test_a_zero_price_bar_never_yields_an_infinity_in_the_rolling_group` (`HEAD:793`)

The assertion body is carried over **verbatim** — the same eleven columns in the
same order, the same `assert not np.isinf(values).any()`, the same
`assert np.isfinite(_observation(df)).all()`. The only change is the trigger,
narrowed from `np.inf` to `0.0`. And it is **labelled**, not quietly retained:

> "Note what this does **not** establish: it is a property of pandas' own
> masking, not of this module. It passes identically with the seam removed —
> which is precisely why it could never prove the seam was load-bearing. The
> test above is the one that does."

**And the label is true, which I confirmed by mutation:** in the seam-deleted
run, this test **passed** (32 of 33 green; only the new seam test failed). A test
kept with a false "this is load-bearing" label would be the F2 defect all over
again. This one says the opposite, and means it. Nothing was deleted to make a
mutation go green.

---

## 4. `builder-dev`'s CAND-3b judgement

**The call was RIGHT. Keep it.**

`CAND-3b` is genuinely taken — `DECISION.md:584`, §10 item 4, *"DEVIATION —
`since`/`until` push-down (CAND-3b, ~21 ln) is DEFERRED"* — and it is genuinely
a **~21-line efficiency** item, explicitly DEFERRED as not load-bearing on the
strength of a real measurement (whole-store read **0.142 s** vs windowed
**0.010 s**). Pointing a *correctness* caveat, at a section that is a standing
caveat on every store-arm number, at an id that resolves to a *performance*
deferral is precisely the "the id points at the wrong thing" failure F2 and F4
exist to remove. Escalating to the lead rather than minting an id unilaterally
was also right.

The doc reads coherently: §14.4 states the real fix, then says why it cannot
carry that id, then says the item **needs its own registration before being
referred to by id** — which is the correct posture and leaves the next reader a
to-do rather than a trap.

### R2 — but §14.4 misdescribes the id it is defending, by one clause

> "§10.4 defines it as `since`/`until` push-down **plus a venue label in
> `_meta.json`**"

§10 item 4 defines CAND-3b as the `since`/`until` push-down, full stop. The
venue label is **item 2**'s subject — and item 2 says the opposite of what
§14.4 attributes to it:

> "**DEVIATION — R4 labels the venue in the model's `config.yaml`, not in the
> sibling's `_meta.json`.** … Sibling `_meta.json` is a follow-up, not dropped."

So §14.4 credits §10.4 with a `_meta.json` venue label that §10.2 explicitly
moved *out* of `_meta.json`. Minor — the clause does not change the judgement,
and the judgement is sound. But the paragraph whose whole argument is *"that id
describes something else"* misdescribes the id by one clause, which is the
cheapest possible irony. Drop *"plus a venue label in `_meta.json`"* and cite
item 4 alone. (`§10.4` is also a new reference style: §10 is a numbered list
with no subsections, unlike §13.1/§13.2/§13.3. Harmless, but `§10 item 4` is
more accurate.)

---

## 5. R3, R4, R5 — minor

**R3 — "~145× more absolute cost" is not reproducible.** The backtest JSONs
carry `fee_rate`, `slippage`, `n_bars`, `num_trades`, `final_equity` — no
notional, so absolute cost cannot be recomputed directly. The two measurable
proxies disagree with it:

| proxy | value |
|---|---|
| trades ratio store/live, seed-matched | 81.0× / 91.6× / 118.9× — **median 91.6×** |
| final-equity drag ratio (`ctrl_eq − cost_eq`) | 34.1× / 18.6× / 179.7× — **median 34.1×** |

Under fixed notional, cost scales with the trades ratio (~92×). Under fractional
equity the store arm's collapsing balance makes true cost *smaller* still. So
145× looks too high under either sizing. It is a side claim — it does not feed
any verdict — but this pass's standard is that a quoted magnitude should be
derivable. Either give the derivation or drop the number.

**R4 — "re-enters every ~1.2 bars in both arms"** is right for the live arm
(bars-per-re-entry **1.216 / 1.191 / 1.234**) and loose for the store arm
(**1.649 / 1.429 / 1.139**, median **1.43**). "*Both arms churn at roughly one
round trip per bar*" would be true of both. Same non-verdict side claim.

**R5 — the F2 and F3 resolutions have no decision-level record.**
`grep -c "F2" DECISION.md` → **0**; F3 likewise. F4 got a `CORRECTION (2026-10-02,
Phase 6 finding F4)` block at `DECISION.md:551`, F6 got §14. F2's repair lives
only in the docstrings and tests, and F3's only in the error message. Given
§11's "what the builder owns, in one place", a reader auditing the gate's
findings would find F2's and F3's dispositions only by reading diffs.

---

## 6. Bottom line

**F1: clean.** The threshold was not tuned — the entire measurement track has
zero diff. All 20 figures in §14.0 reproduce from the per-seed records through
two independent implementations that agree to 1e-12; the verdict column is
machine-produced. All six cells lose money and the report says so without
directional framing. The self-correction is arithmetically right, discloses both
of its own input misses, and reports the *larger* number. The pre-registration
is a direct ancestor of the result commit. **This is what a pre-registered
re-run is supposed to look like.**

**The width hash matched — byte-identical, on both the observation matrix and
the float32 transform, 60 features, `start_index` 24.** No `models/` artifact is
invalidated.

**F2: the structural repair is real.** The vacuous test is dead — deleting the
compute seam now takes down exactly one test, with `sma_4` at 91.41 against a
~122 price, the original symptom. The old inf assertion was **kept and
labelled**, not deleted, and its label is true under mutation. The ewm hazard is
recorded as a known gap and its pin provably bites.

**One thing is wrong: `_rsi`'s docstring.** It claims the seam makes a NaN input
produce a NaN `rs` that `fillna` maps to the neutral 50.0, and that the seam
makes the finite-wrong-RSI hazard unreachable. Measured, both are false: `ewm`
skips NaN, `rs` stays finite, and on any frame with losses the shipped pipeline
reports `rsi_4` = 42.13 — the value `ewm` carried from the previous bar. The
`compute` comment in the same commit says the opposite, and is right.

That is a comment edit, not code. **It does not block the phase; it should be
fixed before the gate is called clean**, because a gate whose stated subject is
false claims should not close with one standing next to its own refutation.

---

*Method: read-only on source. `git status` empty and HEAD `91c76a9` at finish and
at start. Scratch: `/tmp/rev-f1/` (width recompute, seam-deleted clone, four
measurement scripts). Mutation was applied only to the `/tmp` clone.*
---

> **ADDENDUM (lead, 2026-10-02) — not the reviewer's words.**
> Line 78 cites `score.py`, the scratch scorer that lived in `/tmp/krb-cost-aware/`. It was never
> committed, so a reader could not re-run it — which is precisely the gap
> `just audit-evidence` was built to find, and which it did find here.
>
> **Its role is now `tools/cost_aware_gate.py`**, which imports the *same* shipped estimator from
> `tools/model_matrix.py` (`DISPERSION_RATIO_THRESHOLD`, `summarize`, `pooled_within_spread`,
> `dispersion_verdict`) and chooses nothing itself. Run it and every figure in §14.0 reproduces:
>
> ```
> just cost-aware-replay \
>   --records /tmp/krb-cost-aware/cost --control-records /tmp/krb-cost-aware/ctrl \
>   --cell-live  0396722e7a1a:42,d76622e63d7c:43,dfdd93543409:44 \
>   --cell-store 0fed61e632ba:42,0498bcf75916:43,4630123dff45:44
> ```
>
> → `excess_return 82.9052`, `total_return 24.2872`, `sharpe 3.0327`, `max_drawdown 53.9052`, all
> **RESOLVED**; bars-per-re-entry live **1.216** / store **1.429**; trades proxy **91.6×**,
> equity-drag proxy **34.1×**. Those are the reviewer's independently derived numbers, reached again
> from committed code.
>
> Note the `:seed` annotations are **required** to verify arm pairing: these backtest records all
> carry the same `seed` field (the trainer default), so they cannot be paired by seed on their own.
