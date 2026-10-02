# PLAN — the data-pipeline audit, what happened, and what is next

Supersedes the Phase-4 version of this file. Written by `reviewer` at the close
of Phase 6. Read `VALIDATION.md` for the evidence; this file is the map.

---

## 1. What was audited, and what came of it

The pass was scoped in `.data-audit/AUDIT.md` (auditor) against the pipeline's
**data** surface, then `DECISION.md` (architect) picked exactly one
(gap, improvement) pair and fixed the outcome type.

| phase | agent | artifact | outcome |
|---|---|---|---|
| audit | `auditor` | `AUDIT.md` | enumerated + ranked candidate gaps spanning >1 category |
| research | `researcher1/2/3` | `RESEARCH-1/2/3.md` | library/API surveys for the top 3 gaps |
| decide | `architect` | `DECISION.md` | **chose CAND-3a** (seed a local market-data store, flip one config key) as **IMPROVE-EXISTING**, and folded in **CAND-5** (a dispersion gate) as the acceptance instrument |
| build | `builder*` | source + tests | store seam, `just store-*` recipes, dispersion gate, venue label, non-finite guard, IN-SAMPLE labelling, pinned-window error |
| gate | `reviewer` (this file's companion) | `VALIDATION.md` | **`NEEDS_FIX`** |

**What CAND-3a delivered, stated without inflation:** a store makes an
**out-of-sample measurement possible at all**. On the shipped default,
`since`/`until: null` means `training_frame` and `evaluation_frame` return the
**same object**, so no OOS number existed to be good or bad. It did **not**
deliver better returns, and it did **not** deliver a claim that could be
adjudicated without the two-matrix method (§4.1 below).

**Verified working:**

| claim | status |
|---|---|
| store seam reads years of history from one config key | ✅ 76,563 bars, 0.19 s |
| `n_features` 60 and `start_index` 24 on **both** arms | ✅ hard-asserted |
| `IN-SAMPLE` / `OUT-OF-SAMPLE` label on every result, 3 surfaces | ✅ verified in real output |
| pinned window with disjoint splits | ✅ 36,804 train / 15,773 eval, 0 overlap |
| pinned-without-store names the seed recipe + its cost | ✅ `just store-plan` / `just store-seed`, ~158 s, ~13 MB |
| non-finite guard: hard finite check on `fit` **and** `transform` | ✅ and **non-vacuous** |
| live-arm width-neutrality (no `models/` artifact invalidated) | ✅ **byte-identical**, sha256 match |
| honest OOS result | ✅ **−34.30 % / Sharpe −0.568**, reproduces §9A.3 to the digit |

**Not delivered, or delivered wrong — see `VALIDATION.md` §0.** Headline:
the store arm's dispersion (the +1800 % seed) is a **friction artifact**, not
seed variance, so the gate's yardstick is currently measuring a cost
assumption.

---

## 2. What was deferred, and why

| # | deferred item | why | trigger to revisit |
|---|---|---|---|
| D1 | **CAND-2** — exogenous coverage (funding backfill + Fear&Greed + the timer) | runner-up 1, "the closest call" (DECISION §9.1). Deferred only because the pass could hold one outcome. **Its look-ahead handling must be settled before it lands**: the merge seam currently uses `signal_max_age_hours` + `ffill`, and a funding series that backfills *after* the fact would put a record into a bar that did not know it yet. | **next pass.** Needs an explicit decision on whether the funding backfill is (a) point-in-time reconstructible from the settlement timestamps or (b) only usable going forward. Answer (b) ⇒ the timer must land first and the backfill must not touch the evaluation window. |
| D2 | **CAND-1** — a `kraken-microstructure` trade-tape source (`NEW-DATA-SOURCE`) | runner-up 2, the best NEW-DATA-SOURCE candidate and it still lost (DECISION §9.2). A sibling repo + flake input is a larger surface than this pass allowed. | only after CAND-2 resolves the exogenous look-ahead question; the two share the merge seam. |
| D3 | **CAND-4** — retry/backoff/salvage on `_page_candles` | runner-up 4, deferred *with a trigger* (DECISION §9.4). A seeded store's seed leg is 158 s over the network; a dropped page there is expensive. | **fires when a `store-seed` run reports a short page / partial month.** Store-side gap accounting (D5/F6) is what will detect it, so land that first. |
| D4 | **§9A.1 harness defect** — a live-vs-store pair inside ONE matrix is structurally uncomparable | **deferred to Phase 7 by user decision** (2026-10-02). Forcing the guard to treat a 697-bar arm as a peer of a 76,538-bar arm marks all live cells INVALID and excludes all valid cells as IN-SAMPLE, so **6 cells yield 0 usable measurements and the gate never fires**. Confirmed still reproducible in Phase 6. | **Phase 7.** Interim method this pass: two separate matrices, each with its own `min_bar_ratio` reference, then `pooled_within_spread` / `dispersion_verdict` across the two replicate sets. That method works and reproduces §9A.2's ratios exactly. |
| D5 | **`since`/`until` push-down** (CAND-3b, ~21 ln) | DECISION §10.4. `train.py:215-226` and `backtest.py:400-419` never pass them, so it is dead code on the default path; and a whole-store read is 0.142 s vs 0.010 s windowed, so the saving is not load-bearing. | once Phase 6's pinning is the *default* path (it is not — see §12.1 of DECISION). |
| D6 | **sibling `_meta.json` venue label** | DECISION §10.2 — this repo consumes `kraken-market-data` as a pinned flake input (`rev 055d7f6`), so a sibling edit needs a `flake.lock` bump + push. The label went on the artifact the reviewer already opens (`config.yaml`) instead. | next time the sibling is touched anyway. |

---

## 3. What was built (Phase 5, by the builders)

| builder | files | change |
|---|---|---|
| `builder` / `builder2` | `data.py`, `justfile`, `configs/deep-history.example.yaml` | store read/upsert seam, `just store-plan/seed/stats/verify`, venue label |
| `builder-inf` | `kraken_trading_bot/rl/features.py` | `NonFiniteFeatureError`, `_require_finite`, `fit`/`transform`/`normalize` gates, `_NON_FINITE_INPUTS` / `_NON_FINITE_INPUTS_NO_ZERO`, sweep of every ratio/log/pct_change site |
| `builder-inlabel` | `data_window.py`, `backtest.py`, `data.py` | `EvaluationScope`, `evaluate_scope`, `evaluation_scope_label` on every result surface, `PinnedWindowUnavailableError` |
| finishing passes | tests + `tools/model_matrix.py` | 379 → **429 tests**; the three gaps reported back were closed (missing CLI label, missing `__init__` export, pinned failure classified as opaque `process_failed`) |

**Headline commit:** `aeace32` — *"429 passed (from 379 at the start of Phase 5),
flake check green."*

Worth recording as a process note: the lead's recovery commit `fef25b3` had
**broken 3 existing tests** — it applied the zero-guard to a passthrough column,
where zero is *data* (`signal_observed` 0.0/1.0, `signal_age_hours` −1.0), so it
erased the distinction between "no reading" and a genuine 0. Fixed by
`builder-inf`, and now pinned by
`tests/test_feature_nonfinite_guards.py::test_exogenous_passthrough_preserves_zero_and_the_minus_one_sentinel`
rather than by a comment. This is the 2026-10-01 absence-vs-observed lesson in
new costume, and the fix is verified non-vacuous (revert #3 in `VALIDATION.md`
§1a).

---

## 4. What this pass found wrong — the concrete work list

Ordered by severity. All are **fix-in-place**; none needs a redesign.

### 4.1 Re-run the gate with costs — **MAJOR, blocks any use of the dispersion numbers**

`VALIDATION.md` §2(d). Store arm, same models / same 76,539 bars / same trade
counts, only `fee_rate`/`slippage` changed:

| seed | trades/bar | frictionless | Kraken taker (0.26 % + 0.05 %) |
|---|---|---|---|
| 42 | 0.606 | +219.73 % | −99.79 % |
| 43 | 0.700 | +53.68 % | −99.86 % |
| 44 | 0.878 | **+2015.13 %** | **−88.18 %** |

Store IQR **1961 pp → 11.7 pp**. The gate's yardstick is currently 873 pp of
unpriced churn. Steps:
1. set non-zero `fee_rate`/`slippage` in the matrix base config (Kraken taker
   ≈ 0.26 %, plus slippage);
2. re-run all 6 cells;
3. re-score with the two-matrix method;
4. rewrite §9A.2's **explanation**: `NOT SEPARATED` survives, but the reason is
   *the metric has no cost basis*, not *the gap sits inside seed noise*;
5. the two **RESOLVED** rows (`sharpe` 1.400, `max_drawdown` 10.74) must not be
   reported as evidence of a better model — they are churn artifacts, and the
   horizon confound §9A.2 names is the *smaller* of the two effects.

### 4.2 Fix the false pandas claims — **MAJOR**

`VALIDATION.md` §2(a)(i). Measured on pandas 3.0.4:

- `rolling(w)` **masks** inf → NaN (it does not propagate), so the compute seam
  is **not** what stops an infinity escaping the rolling builders;
- `ewm()` **skips** the bad bar — finite, silently wrong, no NaN.

Correct five places: `features.py:789-791` (the seam's justification),
`1054-1056` (`_rsi`), `1098-1101` (`_bollinger`), `1121-1123` (`_atr`), and
`tests/test_feature_nonfinite_guards.py:703-708`. The test
`test_compute_seam_is_the_only_guard_for_the_rolling_technical_group` is
**vacuous** — reverting the seam leaves all 31 tests green — so either delete it
or rewrite it around what the seam actually buys (0 → NaN in `sma`/`obv`, and
the price group's semantics). Also document the real residual hazard: `ewm`
yields a **silently wrong but finite** feature that the guard cannot catch.

### 4.3 Fix the bar-count wording — material

`VALIDATION.md` §2(c). The discrepancy is **resolved**: benign accumulation from
the live upsert leg (`data.py:1349-1352`), ~1 bar per run, 0 duplicates, pinned
edge unmoved. There are **four** live figures in circulation (76,561 / 76,562 /
76,538 / 76,563). Fixes:
- `DECISION.md` §3 (`:47`, `:85`, `:86`), §5.1 (`:127`), §8 (`:318`, `:325`) and
  §11 (`:605`) still say **76,561** — strike or annotate as superseded by §9A;
- add a header to `RESEARCH.md` and `RESEARCH-3.md` pointing at §9A, since both
  quote 76,561 with no `NOT SEPARATED` and no −34.3 % OOS figure;
- quote bar counts **with their timestamp or their run**, never as a constant.

### 4.4 Correct the zero-volume-bar description — material

`VALIDATION.md` §4 F4. §9A.3 says "a zero-volume bar at each **partial-month
boundary** (4 across the store)". **Wrong.** 0 of 106 month-first bars have zero
volume; all 4 are mid-month, and 2 sit immediately before an exchange-downtime
gap. They are **no-trade outage bars**. Point this at data integrity, and fix
the wording wherever it is repeated.

### 4.5 Add store gap accounting — material (new finding)

`VALIDATION.md` §4 F6. The store has **157 missing hourly bars across 28 gaps**,
max single gap **39 hours**, including a **38-bar hole at the seed/live-append
seam in the most recent month**. Features are computed on *bar counts*, so a
39-hour jump is z-scored as a 1-bar step and the 24-bar warm-up spans more than a
day. Fixes:
- `just store-verify` should report gap count and max gap, and warn past a
  threshold;
- `market_data_store_venue` provenance should record that the seed (Binance
  archive) and the live leg (Kraken REST) are **two venues joined without an
  overlap check** — the existing label only names one;
- decide whether the seed should backfill the seam months rather than leaving a
  hole where live trading would run.

### 4.6 Make the store-off remedy true — material

`VALIDATION.md` §4 F3. `_resolve_env_setting` (`backtest.py:282-307`) **skips**
a `None` source, so `market_data_store: null` in a run config cannot override a
store-trained model's own config — yet `data.py:267` tells users that is exactly
how to go back to live. Either honour an explicit sentinel (`""` / a
`--no-store` flag) or reword to name the model's own `config.yaml`.

### 4.7 Pin or drop the redundant guards — minor

`VALIDATION.md` §1a. Reverting any one of these leaves the suite green:
`features.py:745-750` (the final float32 check — **429 pass without it**),
`features.py:737-742`, `features.py:452-457`, and
`data_window.py:589-601` (`has_split`, redundant with the overlap clause).
They are defence-in-depth and harmless, but four unpinned call sites read as
load-bearing. Either add a test per site or say in the docstring that the check
is deliberately redundant.

### 4.8 Keep the dead-under-config lines, but say why — minor

`return_1` / `log_return_1` (`features.py:883-884`) are overwritten by the
`windows` loop when `1 ∈ windows` (the shipped default). They are **not**
deletable — a config without `1` would need them. Add a one-line note; do not
remove.

---

## 5. Next steps, in order

1. **Land 4.1** (re-run the gate with costs) — it is the only item that changes
   a published number. Until it lands, treat **every** dispersion figure in
   §9A.2 as unquoted.
2. **Land 4.2** (false pandas claims + the vacuous seam test) — the next agent
   will otherwise trust a mechanism that does not exist.
3. **Land 4.3 + 4.4** (wording) — cheap, and they are what a future reader
   hits first.
4. **Land 4.5 + 4.6** (gap accounting, store-off remedy).
5. **Land 4.7 + 4.8** (test/docstring tidy).
6. **Phase 7**: the §9A.1 harness defect (D4). Decide whether the fix is a
   per-arm `min_bar_ratio` reference, an explicit `comparable_cohort` flag, or a
   refusal to score arms whose depth differs by construction. **Whichever it is,
   any report comparing arms of deliberately different depth must state the
   limitation rather than quoting a `report` run that silently produced no
   verdict** (DECISION §9A.1).
7. **Then D1 (CAND-2)**, with the look-ahead question answered first.

## 6. Standing rules this pass established

- **`NOT SEPARATED` is a result.** It is not a failure and must never be
  presented as one. The gate exists to be able to say it.
- **Verify the fix, not the intent.** Four separate claims in this pass were
  true in spirit and false in mechanism (the compute seam, `has_split`, the
  transform's final check, and the "partial-month boundary" bars). A guard that
  is *collectively* load-bearing can still have individual call sites that no
  test reaches.
- **Measure the pandas/stdlib behaviour; do not document it from memory.** Three
  docstrings asserted `ewm` masks inf to NaN. It does not — it skips the bar and
  returns a finite, wrong number. The comment was the bug.
- **Record magnitude on every run.** Bars replayed **and** trades taken. A run
  that exits 0 while replaying ~1 bar is a fail, not a pass.
- **Never `.venv/bin/python`.** Its editable install points at the MAIN
  checkout and silently tests a different tree.
