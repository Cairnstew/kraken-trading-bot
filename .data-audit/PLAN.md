# Plan — audit-pipeline IMPROVE-EXISTING pass, 2026-10-01 (post-fix)

Companion to `VALIDATION.md`, which carries the **GATE verdict: PASS**. The previous
revision of both files described the pre-fix state; §1 records what changed and §4 replaces
the "blocking" item the prior plan called out.

---

## 1. What changed since the NEEDS_FIX gate

Five commits on `master`; the fifth is the fix this re-verification gates:

| Commit | Change |
|---|---|
| `a39e184` | `rl`: widen the observation 49 → 55 and guard the width |
| `28fcfb0` | `tests`: cover the Gap-1 widening end to end, both width probes fixed |
| `eb3771e` | `configs`: name the funding CLI that actually exists |
| `8f6ab7b` | `README`: state the real observation widths instead of the fixture's 55 |
| `b45a7a4` | `rl`: **do not let a sparse exogenous column consume the trading window** |

### 1.1 The defect that is now fixed

A one-record funding file left `spread` NaN on 720/721 rows (the merge seam zero-fills `bid`
for absence; the microstructure builder computes `(ask - bid) / bid.replace(0, nan)` and
correctly refuses to invent a spread). `TradingEnvironment._first_valid_index()` selected
the first row valid on **all** columns, so it picked 720 and every funding-backed episode
replayed **1 bar with 0 trades** — silently, with the width guard passing because it checks
width, not NaN. The 143 tests missed it because every fixture populated funding on every
row, which is the one shape the shipped configuration does not produce.

The fix adds `first_tradable_index()` + `POINT_IN_TIME_EXOGENOUS_COLUMNS` in `rl/features.py`
and makes **both** `environment.py::_first_valid_index` and `export.py::_warmup_mask`
delegate to it. The start index now gates on indicator warm-up only; a NaN in a
point-in-time exogenous column means "no reading on this bar" and is resolved by the
environment's pre-existing, documented `ffill().fillna(0.0)` policy — which the freshness
pair exists to keep unambiguous.

Measured live before/after on the real one-record file (`VALIDATION.md` §2): **1 bar / 0
trades → 697 bars / 374 trades**, width 60 both sides. Null-funding and no-new-inputs legs
are unchanged at `_start_index == 24`, so the bug was not traded for a changed warm-up
semantic.

Provenance rather than a NaN-pattern heuristic is the load-bearing part: with one record on
the last bar, `spread` is NaN on rows 0..719 — byte-identical to a 720-bar rolling window,
so a "leading prefix" test cannot distinguish warm-up from absence.

### 1.2 The test that pins it

`tests/test_rl_environment.py::test_sparse_funding_coverage_does_not_consume_the_window`
builds the sparse shape (721 bars, one funding record) and asserts
`n_bars - _start_index > 0.9 * n_bars` plus a real step loop. Verified **non-vacuous**: with
only `rl/{features,environment,export}.py` reverted it fails with
`a one-record funding file left only 1 of 721 bars tradable (start index 720)`. Three
sibling tests pin the warm-up boundary (24), its invariance across the two legs, and the
49-width presence-gating path. Suite is **147 passed**, `nix flake check` green.

---

## 2. What was validated on this re-verification

- **Suite**: 147 passed / 0 failed; `nix flake check --no-build` green (run once).
- **Live gate scenario**: real keyless one-record funding file, train + backtest at pages 2 /
  timesteps 3000 / seed 42 / discrete. 697/697 bars, 374 trades, width 60. The pre-fix
  failure was reproduced on a reverted scratch tree first (1 bar / 0 trades), so the
  before/after is measured rather than asserted.
- **Widths**: 49 / 52 / 60 — all three reproduced.
- **Consumed proof**: the six names reach the model via export CSV, trained
  `normalization.npz` `feature_names`, and the train log's `Obs features: 60`.
- **Guard**: proven end-to-end through the real CLI (a 52-wide artifact against a 60-wide
  frame raises `FeatureWidthMismatchError` and names the eight columns). Untouched by the
  fix.
- **Identity**: the 52 pre-existing columns are bit-identical with and without the funding
  file (`max|delta| = 0.0`, same-frame method).
- **Baseline**: null-funding control trained and backtested, same seed and budget, 697 bars /
  298 trades. PPO's cross-run stochasticity is recorded, not papered over.

### Correction the next reader needs (unchanged from the prior plan)

`DECISION.md` §2.1 pins the gate at **55**. That is the width of a *synthetic fixture*
carrying exactly the six new columns. The width is dynamic because every builder is
presence-gated; the real numbers are **49 / 52 / 60**. `8f6ab7b` fixed the README; the
DECISION's "55" should still carry an explicit correction note in a later docs pass so the
next reader does not gate on it.

---

## 3. What remains: Gap-2, signal QUALITY

**The fix restored the window; it did not enrich the signal.** These are separate and the
distinction is the whole point of recording it:

| | Before the fix | After the fix |
|---|---|---|
| Trading window | 1 of 721 bars | 697 of 721 bars |
| Bars carrying a funding reading | 1 of 721 | **1 of 721** |

A freshly-configured deployment points at a funding file that covers recent hours only.
Kraken funding settles ~8-hourly, so at `signal_max_age_hours: 12` a file with one record
informs exactly one bar of a 721-bar window and the remaining bars observe zeros. That is
now *honest* rather than *destructive*: `signal_observed` (1.0 on one row) and
`signal_age_hours` (max 0.0) are in the observation, so the agent can tell "no reading" from
a genuine zero instead of guessing. But a mostly-zero funding column is a weak signal, and
three of the six new columns (`funding_rate_prediction`, `vol24h`, `spread`) are currently
non-zero on 1 bar in 721.

Remaining Gap-2 work, in the order I would take it:

1. **Make the funding timer actually run long enough to matter.** The shipped
   `systemd.user` timer (`kraken-trading-bot-funding.timer`, `just funding-timer`) plus the
   `signal_max_age_hours: 12` setting are the whole mechanism; a file that has been pulling
   for weeks turns 1/721 coverage into a usable series. This is operational, not code, and
   it is the cheapest remaining win.
2. **News and social signals remain unscheduled.** This pass shipped exactly **one** timer.
   The merge seam and the freshness bound already accept all three files, so scheduling them
   is configuration, not a new seam.
3. **Coverage telemetry**, so a thin signal is visible rather than inferred. A count of
   `signal_observed` per file, logged at merge and recorded in the model artifact, would
   have surfaced both this pass's defect and the present thinness without anyone having to
   notice a 0-trade backtest. This is the cheap half of the
   `data_window` / `feature_fingerprint` provenance item below, and it is now better
   motivated: the width guard answers "can this model consume this pipeline?", not "did this
   pipeline receive any signal?".
4. **`signal_max_age_hours` default.** 12 is a compromise chosen when coverage was ~300/721.
   It should be re-derived from the timer cadence and the settling interval once the file
   has run long enough to measure, not carried forward on the strength of one run.

---

## 4. What was deferred (unchanged, with re-ranking)

| Deferred item | Why | Rank for next |
|---|---|---|
| **Microstructure recorder** as a NEW-DATA-SOURCE | 3/5 difficulty, needs a new sibling repo + a recorder process. Substantively overlaps this pass (`volume_per_trade` is the bar-scale analogue of `mean_trade_size`; `vwap_dev` overlaps `vwap_pressure`). `RESEARCH-3` frames it as the fallback. | **Runner-up. Next NEW-DATA-SOURCE.** Its genuinely-new scalars (taker side split, `realized_spread_bps`) are the justification. Must use `realized_spread_bps`, never compete for `spread` — `_SIGNAL_BUILDER_INPUT_COLUMNS` reserves that name and a future recorder must not become a second writer. |
| **`order_book_imbalance`** (+1) | Needs the `bid_vol`/`ask_vol` producer — Gap-3 / recorder territory. `_add_microstructure_features` already has the branch. | With the recorder. |
| **`mark_price` / `index_price`** | `DECISION.md` §2.1's borderline internal runner-up. `basis = (mark − index)/index` is already merged, so the incremental information is marginal. The sibling does emit both. | If the interface budget ever permits, they ride the same `_SIGNAL_COLUMNS` edit and the same retrain. Not required by any gate. |
| **`until` clip fix** | Independent defect, orthogonal to this pass. | Unchanged priority; small. |
| **`data_window` / `feature_fingerprint` provenance** | `n_features` shipped (a width). A *fingerprint* — which inputs were live, and how many bars carried a reading — is what would have caught this pass's own defect automatically. The failure was a provenance gap: nothing recorded that 720 of 721 funding bars were zero-filled. | **Strongly justified, now for a second reason** — see §3.3, which is the cheap version of it. |
| **Provenance of `vwap`/`count` themselves** | **Next NEW-DATA-SOURCE**, and the one that most directly improves signal quality: `vwap_dev` / `trade_count_zscore_20` / `volume_per_trade` currently depend on the exchange feed happening to carry `vwap`/`count`. The live Kraken OHLC endpoint does return them — which is why widths 52/60 are reachable at all, and a venue that omits them drops to 49 — so this is durability of the pass's own gains, not a new signal. | After the recorder, or before it if a second venue is ever planned. |

---

## 5. Follow-ups that did not block the gate

- **Sibling ticker field**: `kraken-funding-rates` writes `spot_pair`, not `ticker`, so the
  seam logs a WARNING and merges every record as an explicitly one-ticker file. Documented,
  correct behaviour — but it means `signal_require_ticker: true` cannot be satisfied by that
  producer. A producer-side field rename would enable the per-ticker filter;
  consumer-side is unaffected either way.
- **DECISION.md §2.1's "55"**: add the correction note (§2 above).
- **EFFICIENCY-PROPOSALS**: none this run — the pass was one test run, one flake check, a
  live train/backtest pair and a targeted revert experiment, with no repeated multi-call
  pattern worth collapsing.

---

## 6. What the defect still teaches about the audit framing

The width guard did its job exactly as designed: it caught a **stale artifact against a
widened pipeline** and refused it loudly, and after the fix it still does. What it cannot
catch — before or after `b45a7a4` — is a model and a pipeline that agree perfectly on width
(60 vs 60) while the *data* behind those 60 columns is 99.86% zero-fill. The fix addresses
the consequence (the window was consumed); it does not address the condition (the signal is
thin). Provenance — a count of live readings per column, recorded with the model — is the
mechanism that turns the second from something a reviewer has to notice into something the
pipeline asserts. That is Gap-2 work, and it is the natural next pass.