# R1 CONFIRMATION — one-item re-review

Reviewer: `reviewer-final` (team `audit-pipeline-1002`).
Confirmed commit: **`0005e5a`** ("F2 finding R1: `_rsi` docstring claimed the
opposite of pandas; docs-only repair"), on top of `91c76a9`.

**Scope: R1 only.** Not a third gate. Read-only on source; committed nothing.
Tree clean at `91c76a9` + this file only.

---

# CONFIRMED

**R1 is resolved.** Every *mechanism* claim in the repaired `_rsi` docstring
is true on the installed pandas — measured by me, on my own probe, not the
lead's. Two clauses are **literally over-stated** under a hyper-literal
reading; §4 gives both with reproductions and the exact fix. I judged those
quantifier looseness rather than a surviving false mechanism, and I flag that
judgement explicitly so it can be overruled.

---

## 1. Did the repair land? — **YES**

Read at source, not from the commit message.
`kraken_trading_bot/rl/features.py:1080-1119`, function `_rsi`. The two false
claims are gone:

- the *"a NaN input makes `rs` NaN, which `fillna(50.0)` then maps to the
  neutral reading"* route — **deleted**
- *"The seam is what keeps that unreachable from here"* — **deleted**

`grep -rn "unreachable"` over `features.py` and the test file returns exactly
one hit, `features.py:1110`, and it is the **negation**:

> "So the residual hazard is real and this docstring does not make it
> unreachable."

---

## 2. Is every claim in the NEW docstring true? — **the mechanisms, yes**

My own probe: a 400-bar frame built from `default_rng(7).normal(0,1)` steps
(224 down-bars, so it *has* losses), a **zero price** written into one bar's
whole price row, then the seam (`_NON_FINITE_INPUTS` → NaN) and `_rsi`'s exact
body. 14 clauses asserted, 13 true:

| clause | measured |
|---|---|
| `avg_gain` finite & non-zero at the poisoned bar | **0.764345** ✓ |
| `avg_loss` finite & non-zero at the poisoned bar | **0.128963** ✓ |
| `rs` stays finite at the poisoned bar | **5.926856** ✓ |
| the poisoned bar carries the **previous** bar's value | `rsi_4[200] == rsi_4[199]` ✓ |
| **`85.5634` reproduced** | **`85.5634353370622`** ✓ |
| same number through the **shipped pipeline**, end to end | `85.5634353370622` ✓ |
| a zero price and an `inf` price give the **same** post-seam result | both `85.5634353370622` ✓ |
| `_frame()` has **no down-bars at all** | **0 down-bars**, `delta` min `0.100000` ✓ |
| in the pinned test the **both-sides-zero guard** is what fires | `avg_loss[200..203] == [-0.0]*4`, `rs` all NaN ✓ |
| the pinned test really does see `50.0` | `rsi_4[200..203] == [50.0]*4` ✓ |
| **on a frame with losses the 50.0 route cannot fire** | `rsi_4[200..203] == [85.5634, 85.5634, 89.1263, 92.183]` ✓ |
| `rolling` families → NaN across the poisoned window | `sma_4`/`bb_upper_4`/`bb_lower_4`/`bb_width_4` all NaN ✓ |
| `ewm` families → stale carried value | `ema_4` `94.4558==94.4558`, `rsi_4` `85.5634==85.5634`, `atr_4` `1.3082==1.3082` ✓ |

**On the `85.5634` figure specifically — it is correct, and it is correctly
attributed.** It reproduces exactly, and it is a **zero**-price result, as the
docstring says. (It also happens to be the `inf`-price result, because the seam
maps `0.0` and `±inf` to the same NaN, so the two triggers are bit-identical
downstream — I verified both.) Nothing is wrong with the number.

Full-frame data table from the same probe, seam applied, zero price at bar 200:

| column | @199 | @200 | @201 | @202 | family |
|---|---|---|---|---|---|
| `sma_4` | 94.4688 | **NaN** | **NaN** | **NaN** | rolling |
| `bb_upper_4` | 95.9363 | **NaN** | **NaN** | **NaN** | rolling |
| `bb_lower_4` | 93.0013 | **NaN** | **NaN** | **NaN** | rolling |
| `bb_width_4` | 0.0311 | **NaN** | **NaN** | **NaN** | rolling |
| `bb_pctb_4` | 0.8880 | 0.5000 | 0.5000 | 0.5000 | rolling (neutral) |
| `ema_4` | 94.4558 | **94.4558** | 94.8595 | 95.2023 | ewm |
| `rsi_4` | 85.5634 | **85.5634** | 85.5634 | 89.1263 | ewm |
| `atr_4` | 1.3082 | **1.3082** | 1.1518 | 1.0874 | ewm |

That is exactly the distinction the docstring now draws: *"`rolling` families
report a poisoned window as NaN, the `ewm` families report it as a stale carried
value."*

---

## 3. Is "unreachable" gone, and is the hazard stated as real? — **YES**

`features.py:1109-1118`:

> "So the residual hazard is real and this docstring does not make it
> unreachable. A bar skipped by the `ewm` recursion leaves a *finite, wrong* RSI
> behind, `fillna` cannot distinguish that from a genuine reading, and
> `_require_finite` cannot see it. What `compute`'s seam does buy here is only
> that the bad price never enters `diff()` as an infinity -- it makes the bar
> *skippable*, not *unknown*."

All three required elements are present: hazard **real**, **not** made
unreachable, **`_require_finite` cannot see it**, and the seam's benefit stated
accurately ("skippable, not unknown"). Note the seam's real contribution is
stated correctly and is not trivial — it stops a −100% print entering `diff()`
at all.

---

## 4. Two clauses that are literally over-stated

Reported because the lead asked for an adversarial clause-by-clause read, and
because both are falsifiable with a few lines. **Neither asserts a pandas
mechanism that does not exist** — that is the defect R1 was.

**(a) `features.py:1096` — "**None of that happens.**"**

The list being rejected is three links. Two are correctly rejected (`rs` came
out NaN — false; `fillna` mapped that NaN to 50.0 — false). The **first** link
does happen:

```
close[19..21] after the seam = [1.0, nan, nan]
delta[19..21]               = [1.0, nan, nan]
gain[20] = nan   loss[20] = nan      <-- gain/loss DO see a NaN
```

The docstring's own parenthetical already re-attributes that correctly
(*"`compute`'s seam (which maps a non-finite close to NaN **before** `diff()`
runs) made `gain`/`loss` see a NaN"*) — the old text's error was blaming `ewm`.
So "None of that" over-rejects one link that the sentence has just correctly
credited to the seam.
*Fix:* "**None of that chain holds**" — or "None of that *produces the neutral
reading*."

**(b) `features.py:1101` — "``fillna(50.0)`` never fires"**

True **at the poisoned bar**, which is what the sentence is about: across 25
seeds, **0** NaN inside the poisoned window, and the poisoned bar is never NaN.
False **over the whole frame**. `fillna` fires at **bar 0 on every frame** —
`delta[0]` is NaN → `loss[0]` NaN → `avg_loss[0]` NaN → `rs` NaN → 50.0 — and at
bars 1–2 when consecutive gains drive `avg_loss` to `-0.0`. Across 25 seeds,
whole-frame NaN counts ran **1–5**, and `fillna` fired somewhere in **25 of 25**
frames. The falsifier is a *clean* series:

```python
F._rsi(pd.Series([1.,2.,3.,4.,5.,4.5,4.4,4.3,4.2,4.1]), 4)
# -> [50.0, 50.0, 50.0, 50.0]      50.0 at bar 0 on a perfectly healthy series
```

Bounding it for the reader: the docstring's **next** paragraph says *"The 50.0
that `fillna` **does** supply in the pinned test comes from the both-sides-zero
guard above"* — which bounds this clause for any careful reader.
*Fix:* "`fillna(50.0)` does not fire **at the poisoned bar**" — or "**because of
the seam**".

**My judgement, stated so it can be overruled:** both sentences have a correct
reading in which every term is true, and every *mechanistic* term I verified
true. I read these as quantifier looseness on an otherwise-correct account, not
as R1 surviving. If the pass's bar is "no clause in this docstring may be
falsifiable", then it is not quite there yet, and it is two words away.

---

## 5. Does it now AGREE with `compute`'s KNOWN GAP? — **YES, in both directions**

`compute` (`features.py:820-826`), unchanged:

> "**KNOWN GAP**, not fixable here: because `ewm` returns a finite wrong number
> rather than a NaN, the `ema`/`macd`/`rsi`/`atr` families survive a skipped bar
> and `_require_finite` cannot see it."

`_rsi` (`features.py:1112-1118`), repaired:

> "…`_require_finite` cannot see it. … The `KNOWN GAP` note in `compute` is the
> **governing record** for this, and it states the limitation correctly."

Same hazard, same affected family (`rsi`), same blindness to `_require_finite`,
and the precedence is now explicit rather than contradictory. The contradiction
that existed inside one commit is resolved.

---

## 6. The SECOND site — **VERIFIED TRUE, false attribution gone**

`tests/test_feature_nonfinite_guards.py:721-730` and the inline comment at
`:791-794`. The lead's claim was: *rsi_4's 50.0 there comes from the no-losses
guard, NOT the seam, and on a frame with losses the assertion would not hold.*
**Verified true, clause by clause:**

| claim in the new comment | measured |
|---|---|
| the `rsi_4` entry is **not** the seam reporting an unknown window as its neutral | ✓ the seam gives NaN; the 50.0 is `fillna`'s |
| `_rsi` reads the poisoned bar as a **stale carried value** | ✓ `rsi_4[200] == rsi_4[199]` |
| `ewm` skips a non-finite input instead of masking it | ✓ measured |
| `_frame()` is a rising ramp with **no down-bars** | ✓ **0 down-bars**, `delta` min `0.100000` |
| `avg_loss` is `-0.0` everywhere | ✓ `[-0.0, -0.0, -0.0, -0.0]` at 200..203 |
| the both-sides-zero guard — **not the seam** — feeds `fillna(50.0)` | ✓ `rs` NaN at 200..203 from `replace(0, nan)` |
| **on a frame with losses the assertion would not hold** | ✓ `rsi_4[200..203] == [85.5634, 85.5634, 89.1263, 92.183]` — `np.allclose(..., 50.0)` is `False` |
| "this loop pins the value, not the mechanism that produced it" | ✓ and that is now literally true |

The false attribution is gone and the frame-dependency is recorded deliberately
rather than papered over — which is the honest form of this note.

---

## 7. Sweep for a THIRD instance — **NONE**

Seven pattern families over every `*.py` and `*.md` in the repo (excluding
`.git` and `.venv`):

| pattern | hits |
|---|---|
| `ewm` near `mask` / `mask` near `ewm` | **no live claim** |
| `rolling` near `propagat` | **none** |
| `propagat` near `rolling`/`ewm` | **none** |
| `seam` near `infinit`/`isinf` | 5, all correctly *negative* statements |
| `isinf` near `seam` | 3, all correctly negative |
| `only guard` / `is the only guard` | **none** |
| `NaN rather than propagat/an infinity` | **none** |

Every surviving `ewm … mask` string is one of: a **test name** that correctly
pairs both behaviours (`test_rolling_masks_but_ewm_skips_…`), an explicit
**quotation of the old claim being corrected** (`features.py:1092` "It claimed
pandas' `ewm` 'masks a non-finite input as NaN' … backwards"; `:1198` "previously
said it did, on the grounds that …"), or the audit documents recording the
finding (`VALIDATION.md`, `PLAN.md`, `DECISION.md`, `REVIEW-F1-F2.md`).

**No third instance.** The lead's sweep claim holds.

Worth noting: `DECISION.md:874` records that inverting the claim to "the seam
prevents infinities" was considered and **"Rejected as still false"** — the
right call, and the right thing to have written down.

---

## 8. Docs-only claim — **independently CONFIRMED**

I did not rely on the lead's AST proof. Mine strips docstrings **and blanks
every string constant**, so only structure, numerics and control flow are
comparable — a strictly stronger test than "docstrings stripped" (it also catches
a message-only change like F3's).

| file (91c76a9 → 0005e5a) | AST identical ignoring all strings |
|---|---|
| `kraken_trading_bot/rl/features.py` | **True** |
| `kraken_trading_bot/rl/data.py` | **True** |
| `kraken_trading_bot/rl/backtest.py` | **True** |
| `tests/test_feature_nonfinite_guards.py` | **True** |
| `tests/test_rl_data_store.py` | **True** |
| `tests/test_market_data_store_seeding.py` | **True** |

**No executable line changed anywhere.** Stronger than the lead's version of the
check, and it agrees.

Also re-confirmed at `0005e5a`:

| check | result |
|---|---|
| `git diff 97a2a52 HEAD -- tools/model_matrix.py` | **zero diff** ✓ |
| `git diff --stat 97a2a52 HEAD -- tools/` | only `store_gap_scan.py` (F6's new tool, +433) ✓ |
| suite | **455 passed**, 24 warnings, 33.5 s ✓ |
| **width hash obs** | `93edc7333cb8f5051556232f2acaadeecdae598e1abdb761850f1772e3068fa8` ✓ |
| **width hash arr** | `6a88a379111a7c63fb9e85ee86e98005a391da4519ca40edc6bf4a8d4a284eab` ✓ |
| `n_features` / `start_index` / non-finite obs cells | **60 / 24 / 0** ✓ |
| `cmp` vs the Phase-6 `.npy` files | **BYTE-IDENTICAL**, both ✓ |

The measurement track is still untouched and no `models/` artifact is
invalidated.

---

## Verdict

**NEEDS_FIX on R1, resolved by docs-only repair, confirmed by the reviewer**

R1's substance is repaired and I verified it independently rather than accepting
it. The `85.5634` figure is real and correctly attributed. The seam test's
second-site comment is true in every clause. The `compute` KNOWN GAP and `_rsi`
now agree in both directions with precedence stated. There is no third instance.

Two clauses remain literally over-stated (§4) — "None of that happens"
over-rejects one link that the same sentence correctly credits to the seam, and
"`fillna(50.0)` never fires" is false over the whole frame though true at the
poisoned bar. Both are quantifier looseness on a correct account rather than a
mechanism that does not exist, which is why they do not change the verdict; I
have given the reproduction and the two-word fix for each so the prose can be
made airtight if you want that bar held literally.

---

*Method: read-only on source. Nothing repaired, nothing committed. `HEAD` `0005e5a`
at finish. Scratch: `/tmp/rev-r1/` (clause probe, NaN-location probe, seam-deleted
clone carried over, width recompute).*