# VALIDATION — Phase 6 gate: G-1 order-book depth into the RL observation

**Verdict: PASS.**

Reviewer: `reviewer` (qa), team `audit-pipeline-1008`, 2026-10-08.
`HEAD = 745ff4d` (`feat(rl): wire recorded order-book depth into the RL
observation (G-1)`), working tree clean. Read-only on source; the only writes
are this file and `.data-audit/PLAN.md`. Nothing committed.

Pass target: `DECISION.md` §2.2/§6/§7 — outcome `IMPROVE-EXISTING`, G-1,
width Δ **+1**, retrain-required, no new library.

---

## 1. Gate receipt — the bot suite, exact brief command

```
$ nix develop --command bash -c "python -m pytest -q"
........................................................................ [  9%]
........................................................................ [ 19%]
........................................................................ [ 28%]
........................................................................ [ 38%]
........................................................................ [ 47%]
........................................................................ [ 57%]
........................................................................ [ 66%]
........................................................................ [ 76%]
........................................................................ [ 85%]
........................................................................ [ 95%]
....................................                                     [100%]
=============================== warnings summary ===============================
tests/test_engine.py: 3 warnings
tests/test_models.py: 8 warnings
tests/test_strategies.py: 9 warnings
  <string>:8: DeprecationWarning: datetime.datetime.utcnow() is deprecated and scheduled for removal in a future version. Use timezone-aware objects to represent datetimes in UTC: datetime.datetime.now(datetime.UTC).

tests/test_feature_nonfinite_guards.py::test_fit_refuses_to_persist_a_non_finite_stat_and_names_the_column
tests/test_feature_nonfinite_guards.py::test_transform_in_sample_fallback_also_refuses_to_return_non_finite
tests/test_feature_nonfinite_guards.py::test_non_finite_feature_error_is_a_value_error_subclass
tests/test_feature_nonfinite_guards.py::test_non_finite_feature_error_reports_stage_columns_and_count
  /nix/store/mppp9zinainmwsc69z6hwdlry7w112bq-python3-3.14.7-env/lib/python3.14/site-packages/numpy/_core/fromnumeric.py:54: RuntimeWarning: overflow encountered in accumulate
    return bound(*args, **kwds)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
756 passed, 24 warnings in 37.12s
```

Exit 0. **756 passed, 0 failed** — matches the brief's expectation exactly.
The 12 offline tests in `tests/test_orderbook_depth_seam.py` are included.

---

## 2. The consumed proof — `order_book_imbalance` reaches the OBSERVATION

The point of this pass. A **raw frame column** is not the observation; the
gate is that the derived scalar reaches `FeaturePipeline`'s
`normalization.npz` `feature_names`. Route: `merge_extra_features` (the same
seam `read_ohlc_dataframe` calls internally, `data.py:1768`/`data.py:1925`)
→ `FeaturePipeline(windows=[1,4,24]).fit(...)` → `save_normalization` →
read `feature_names` back from the `.npz`. Scratch only:
`/tmp/opencode/consumed_proof.py`, `/tmp/opencode/norm_*.npz`.

Live signal file: `signals/eth_usd_orderbook.jsonl` — **114 records, 111
distinct hours**, span `2026-10-03T19:10Z … 2026-10-08T10:42Z`. (DECISION
§7 C-5 measured 103 records; the file is append-only and live, so it has
grown. Same shape, same keys.)

### 2.1 Matrix

| | **A: depth-only vs nothing** | **B: news+depth vs news** (shipped shape) |
|---|---|---|
| control arm | no signal channel | synthetic news channel (`sentiment_score`) |
| test arm | depth file only | news **+** depth file |
| bars (both arms) | 136 | 136 |
| `n_features` control | **49** | **53** |
| `n_features` test | **52** | **54** |
| **width delta** | **+3** | **+1** |
| added columns | `order_book_imbalance`, `signal_observed`, `signal_age_hours` | **`order_book_imbalance` only** |
| `order_book_imbalance` present | **yes** (control: **no**) | **yes** (control: **no**) |
| `bid_vol` present | **no** | **no** |
| `ask_vol` present | **no** | **no** |

**A's +3 is not a defect — it is the freshness pair.** `merge_extra_features`
derives `signal_observed`/`signal_age_hours` per merge and combines them
across channels (`_combine_freshness`, `data.py:1107-1137`: OR on the flag,
`max` on the age). A control with **no** channel at all has neither column;
turning one channel on adds the pair **plus** that channel's own columns.
The shipped default always has news/funding/social on, so the depth channel's
**marginal** contribution is the pair already being present and only
`order_book_imbalance` arriving — that is **B's +1**, and it is the number
DECISION §6 pins ("delta vs the **same config with the channel off** …
exactly **+1**, measured per configuration").

### 2.2 The three names, as required

| name | in `feature_names`, channel ON | in `feature_names`, channel OFF |
|---|---|---|
| **`order_book_imbalance`** | **present** | **absent** |
| `bid_vol` | **absent** | **absent** |
| `ask_vol` | **absent** | **absent** |

`bid_vol`/`ask_vol` are `_SIGNAL_COLUMNS` members (so the seam carries them to
the frame) **and** `_SIGNAL_BUILDER_INPUT_COLUMNS` members (so
`_add_signals_features` skips them, `features.py:1075-1077`). Only the derived
`order_book_imbalance` (`features.py:1030-1038`) wins the width. This is D-3,
confirmed on the consumed artifact rather than by reading the diff.

### 2.3 Non-constancy (the feature carries real signal, not a constant)

On the merged 136-bar frame: `order_book_imbalance` has **112 distinct values**
(min **−0.914730**, max **+0.871389**, std **0.433971**) over the depth file's
hours. DECISION §7 C-8 predicted min −0.915 / max +0.871; the sign and scale
agree (the exact extremes differ because C-8 was measured on the 103-record
file and the file has since grown). A zero-fill-only column would be constant.

### 2.4 A review footgun found while building the proof (not a G-1 defect)

`merge_extra_features` **mutates its input `df` in place** (`data.py:988`,
`df[col] = filled[col].fillna(0.0).to_numpy()`). A first draft of the proof
ran the ON arm first on a shared frame, then the OFF arm on `frame.copy()` —
and the OFF arm inherited the already-merged `bid_vol`/`ask_vol`, reading
**52 with `order_book_imbalance` present on BOTH arms** (a false NEEDS_FIX).
The corrected script builds a **fresh frame per arm** and runs the control
first. This behaviour is pre-existing (every channel writes this way; the
depth channel adds no new mutation) and the production callers each own their
frame, so it is **not** a G-1 defect — recorded as a review observation so the
next person writing a channel comparison does not repeat it.

---

## 3. The seam is wired through all four callers

The four external callers thread the new config key explicitly:

| caller | site |
|---|---|
| `train` | `train.py:254` |
| `backtest` | `backtest.py:567` |
| `export` | `export.py:181` |
| `paper_trade` | `paper_trade.py:315` |

Covered by the AST guards, run together:

```
$ nix develop --command bash -c "python -m pytest -q \
    tests/test_gc_channel_activation.py::test_every_consumer_threads_all_signal_file_keys \
    tests/test_rl_signal_config_wiring.py"
...........................                                              [100%]
27 passed in 1.51s
```

`test_every_consumer_threads_all_signal_file_keys` walks each consumer's
source and asserts every `_SIGNAL_CHANNELS` key (including
`orderbook_features_file`, the 4th entry, `data.py:198-208`) is passed by all
four. The two read legs are `data.py:1243` (fetch) and `data.py:1908`
(store), both threaded (`data.py:1376`, `data.py:2051`). Pass.

---

## 4. Scope, and what this verdict does and does not claim

- **In scope, proven:** the flattener reduces the real depth file's nested
  records; `bid_vol`/`ask_vol` reach the frame; `order_book_imbalance` reaches
  the observation with real variance; the raw volumes do not; width Δ is +1
  on the shipped-config shape; all four callers thread the key; 756 tests pass.
- **Not claimed:** any *effect* on returns. `models/` is empty
  (`DECISION.md:36`), so no model exists to retrain and none was trained. This
  pass proves **delivery**, not predictive value — the same discipline the
  prior VALIDATION applied to its `†` rows.
- **Forward-only, disclosed (DECISION §7 C-7):** the live file covers ~1.1 % of
  a multi-year training window, so on the shipped default the column is 0.0 for
  the overwhelming majority of bars. The gate here is a **per-configuration
  +1 width and a non-constant feature on the covered hours**, not a claim of
  dense coverage. PLAN.md carries the "enable by default once coverage matures"
  next step.
- **Inherited failures not re-opened:** the `audit-findings` parity (via
  DECISION.md §8) and the depth-log green-control (updated for the real
  `2026-10-05T14:00` gap) were fixed earlier this pass. Neither is re-tested
  here.

---

## §8 Finding index — F-1 … F-16

`just audit-findings` binds every `F<n>` named **here** to a disposition in
`DECISION.md` §7.7/§7.8 (`tools/audit_checks.py:348-381`). An earlier
revision of this file named none, so the check reported "no F<n> identifiers"
and **passed vacuously** — the same green-for-the-wrong-reason class §1's
`SELF-TEST` clause exists to catch. This section restores the binding.

| id | one-line | disposition |
|---|---|---|
| **F-1** | `features.py` docstring said the shipped default "composes 52" | **Closed** — 52 is the all-null width; shipped default composes 60 (`aba7b9b`) |
| **F-2** | `signal_observed` is a per-channel OR; 4 of 6 columns stay zero-fill | **Accepted and disclosed** — `configs/default.yaml` §1/§2, restated in this file |
| **F-3** | Forward-only cold start at a rolling ~366-day cap | **Accepted, bounded, asserted** |
| **F-4** | AUDIT's "12 of 721" was wrong; **13** measured | **Correction recorded** |
| **F-5** | `null` vs `0.0` for unrecoverable fields | **Override, measured equivalent** — record honesty only |
| **F-6** | Tighten `signal_max_age_hours` 12 → ~1 | **Override: fix the comment, not the value** — §10 confirms at max **12.0** |
| **F-7** | §6.1's hour-dedup direction was **inverted** (file order, not "live wins") | **Corrected and acted on — ON THE CONSUMER.** The producer deliberately does the opposite of what the old row recorded: `ticker-news-signals:ticker_news_signals/export.py:188` is `mode = "a" if append else "w"`, i.e. it appends UNCONDITIONALLY with **no hour-skip**, and its own docstring plus `54c9d24` say a duplicate hour is harmless because the consumer dedupes. The dedup *direction* was fixed on the consumer — `data.py:798-809` exact-dup `keep="last"` → `sort_index(kind="stable")` → `floor("h")` → `groupby(level=0).last()`, from `5951f72`. **The old row's "`--append` skips held hours" was false against the code.** [CORRECTED 2026-10-03] |
| **F-8** | §7.3's width constants (57/49) are **3 low** | **Superseded** — measure 52/60; assert no literals |
| **F-9** | Non-finite cells: 684 vs 0 | **RESOLVED — frame mismatch.** `697 − 13 = 684`; `observed` is the frame that matters. Builder's "identical both arms" **wrong** (684 vs 697) |
| **F-10** | `signal_age_hours` ≤ 2.0 unachievable | **Restated per shape** — 12.0 on the shipped shape; confirms F-6 |
| **F-11** | `spread` nonzero: 13 vs 24 | **RESOLVED — one arm, two frames.** 13 = `computed`, 24 = `observed`; 13 + 11 ffill = 24. Integrator's "byte-identical both arms" **wrong** (0/721 backfilled) |
| **F-12** | Record has 14 keys, §6 says 13 | **Restated as a superset** — `relative_funding_rate` costs 0 observation columns |
| **F-13** | F-1's number was wrong as well as its text | **Closed** (`aba7b9b`) |
| **F-14** | The two characterisation tests are **vacuous** for this diff | **Accepted, not a defect** — zero executable change, so no test *could* be non-vacuous |
| **F-15** | `width_check.py` skips `add_derived_ohlcv_features`, so a raw parquet reports 49 | **Accepted — docstring-only**; pre-existing tool trap, do-not-paste receipt |
| **F-16** | `audit_checks.find_in_repo` **crashed** (`ValueError`) on a legitimate absolute cross-repo citation, making `just audit-evidence` unrunnable | **Fixed** in this pass — degrade to the absolute path; a crash asserts nothing, and `audit_checks.py` is not in `PY_FILES`, so the `executable-ast` guard and the byte-identical `model_matrix.py` are untouched |

VALIDATION COMPLETE
