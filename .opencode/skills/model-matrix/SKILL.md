---
name: model-matrix
description: Use when designing, running, or reading a model-evaluation matrix for the RL pipeline — deciding whether a trained model is actually tradeable, comparing configurations, seeds, action spaces, friction levels, or tickers, or judging whether a backtest result means anything. Also use before quoting any single backtest number as a result, and when a matrix run or report needs interpreting. Covers validity preconditions, multi-seed discipline, out-of-sample discipline, and degenerate-cell detection.
---

# Model matrix: making comparisons valid

A single backtest run is not a measurement. This skill is about the gap
between "a number came out" and "the number supports a decision".

The harness is `tools/model_matrix.py`; `configs/matrix.example.yaml` is a
worked spec. Three subcommands:

```bash
just matrix-plan   configs/matrix.example.yaml   # expand + warn
just matrix-run    configs/matrix.example.yaml   # execute, resumable JSONL
just matrix-report configs/matrix.example.yaml   # median + IQR, invalid listed
```

**Read this before believing any comparison.** Five preconditions, all
checkable, all currently at risk in this repo.

---

## CHECKLIST — the five validity preconditions

Run this list before you quote a number. `just matrix-plan` automates all
five and prints a BLOCKER/WARN/NOTE for each; do not skip it.

### 1. ☐ Is the bar window pinned? (`data_window`)

`train` and `backtest` each fetch data fresh. Two fetches have been
measured to disagree **16% on a fitted feature std** — so two cells that
look identical may have trained on different bars. That is not noise, it
is a different experiment.

Pin `data_window: {since, until}` on every cell. **Both bounds must be
set** — a key present but `null` counts as UNSET. `plan` emits a
**BLOCKER** without it, and `report` refuses to describe unpinned cells
as comparable.

### 1a. ☐ Is `eval_split` actually in play? (the trap that costs you everything)

**`eval_split` is INERT unless the window is pinned.** With `since` and
`until` both null, the run trains on 100% of the frame and the backtest
replays 100% — so a matrix row that *sets `eval_split`* is still **fully
in-sample**, and its return describes the fit, not a prediction.

This is the single easiest way to ship an in-sample result while believing
it was a holdout. The harness treats it as a first-class error:

- `plan` emits a **BLOCKER** (`eval_split_inert`) for any cell that sets
  `eval_split` without pinning both bounds.
- `report` splits cells into **out-of-sample** and **IN-SAMPLE**, prints
  the in-sample ones in their own section, and **excludes them from every
  aggregate**. They are never averaged with holdout cells — an in-sample
  return and an out-of-sample return are different quantities.
- A matrix with **zero** out-of-sample cells gets an explicit
  `NO OUT-OF-SAMPLE CELLS` claim: it supports no generalization claim.

Also note the split **rounds** (`round(n*split)`, clamped to `[1, n-1]`),
so do not predict the split boundary with `floor()`.

### 2. ☐ Was anything run more than once? (`seed` ≥ 3)

PPO on an **identical config** has been measured replaying **576 trades in
one run and 374 in another** — a 1.5× swing from the seed alone
(`.data-audit/VALIDATION.md` §2.1). One run per configuration is an
anecdote.

Three seeds is the floor; five or more before calling any difference
real. The report reduces by **median** and prints the IQR for exactly this
reason — see [multi-seed discipline](#multi-seed-discipline).

### 3. ☐ Were costs applied? (`fee_rate` / `slippage`)

**A frictionless Sharpe is not a Sharpe.** With `fee_rate: 0.0` and
`slippage: 0.0` the backtest books every fill at the mid, so any policy
that trades at all books profit. The shipped `configs/default.yaml` is
frictionless, so this is the default failure mode, not an edge case.

`plan` emits a **BLOCKER** when every cell is frictionless. Include a
realistic level (Kraken retail taker ≈ 0.26% + half a spread ≈ 0.05%) and
read *those* rows.

### 4. ☐ Is there a reference point? (`buy_hold_return`)

A raw return is uninterpretable without knowing what simply holding the
asset did. The report leads with **`excess_return`** = `total_return` −
`buy_hold_return`, and prints `buy_hold_return` beside every group. Never
quote `total_return` alone.

### 5. ☐ Is the result out-of-sample? (`eval_split` **and** a pinned window)

The backtest immediately after training replays **the bars the model was
fitted on**. That number is a description of the fit, not a prediction.

`data_window.eval_split: 0.7` → first 70% trains, last 30% is holdout —
**but only when `since`/`until` are both pinned** (see 1a above).

**Never gate a decision on an in-sample backtest.** `just bench` trains and
backtests back to back and is explicitly in-sample — its own comment says
so.

There is **no walk-forward re-fitting** yet (see
[known limits](#known-current-limits)), so today you get one holdout, not
a rolling series of them. Varying `data_window` across several
`{since, until}` values is the closest available substitute.

### Also check (not blocking, but they change what the answer means)

- **More than one ticker.** One ticker is one asset's story. "Does this
  generalize?" cannot be answered on it.
- **Every cell valid.** A cell that replayed 1 bar with 0 trades is an
  *absent measurement*, not a zero. See
  [degenerate cells](#degenerate-cells-the-silent-failure).
- **Sample size per group.** The report says so in
  `WHAT THIS SAMPLE SUPPORTS`. Read it before reading any ranking.

---

## Choosing axes for a stated question

Start from the question, then pick the axes that answer it. An axis with
no question behind it is a cell you pay to train for nothing.

| Your question | Axes | Why |
|---|---|---|
| "**Is the signal worth trading at all?**" | `ticker` × `seed`(3+) × `friction` × pinned `data_window` | The minimum that makes a claim at all: does it survive repetition, costs, and a fixed window? |
| "**Which algo is best?**" | — | **Not answerable today.** `rl/agent.py` is a thin SB3 `PPO` wrapper; no other backend exists. Documented future axis. `plan` emits a NOTE saying so. |
| "**Does it generalize across assets?**" | `ticker`(≥2) × `seed`(3+) | Two tickers minimum. One ticker cannot answer this. |
| "**Does the discrete action space beat continuous?**" | `action_space` × `seed`(3+) | Both exist today. Note: changing it changes the observation width, so each cell trains its own model (the harness derives a per-cell model name automatically). |
| "**Does more training help?**" | `timesteps`(2+) × `seed`(3+) | Two levels minimum; one level contributes nothing. |
| "**Is the signal robust to timing?**" | `data_window` with several `{since, until}` | Each window is a different holdout. The strongest generalization test available. |

Reserve `ticker`, `seed`, `pages`, `timesteps` for CLI flags; every other
axis name is deep-merged into the per-cell config. Prefix an axis with `.`
to nest its dict under that key (`.data_window` → `data_window: {...}`);
a dict axis without the dot flattens (`friction: [{fee_rate, slippage}]`
→ top-level `fee_rate`/`slippage`).

### Axes a backtest cannot express — retrain, don't fake it

`backtest --config` honours **exactly six keys**: `fee_rate`, `slippage`,
`action_space`, `initial_balance`, `market_data_store`, `data_window`.

Everything else — `reward`, `feature_windows`, `feature_groups`,
`allow_short`, `ohlcv_interval_minutes`, `*_features_file`, `signal_*` —
is read from the **model's own training config**, because a mismatch
there is silent-corruption class: the policy was fitted against one
feature layout and replaying against another produces numbers that mean
nothing.

**So an axis over any of those cannot be expressed to a backtest at all.**
The harness makes that explicit rather than silently applying a backtest
config that cannot express the axis:

- the full merge goes to a **per-cell TRAIN config**, and every level is
  **retrained** (the model name is derived per cell, so no artifact is
  shared between levels);
- the **backtest config is narrowed** to the six expressible keys, with
  train-only keys stripped, so the backtest genuinely defers to the
  model's config rather than to the matrix's;
- `plan` emits a `train_only_axis` **NOTE** naming the keys.

Each such level costs a full training run — that is the honest cost. A
"cheap" backtest-only variation of the reward would measure nothing,
because the backtest re-reads the reward the model was actually trained
with. `ohlcv_interval_minutes` is train-side for the same reason: a policy
trained on one bar interval cannot be replayed on another.

---

## Multi-seed discipline

**Why median and not mean.** The 576-vs-374 measurement is not a bug to
be averaged away — it is the size of the effect you would be reporting on.
A mean of two runs is a number with no interpretation; a median with an
IQR says "the central case was X, and the spread was Y".

**What the report does.** Every multi-seed group is reduced to
`median [q1, q3]`, sorted by median, with `n` shown as `valid/requested`
when some replicates were dropped. It never averages, and never collapses
a group to a single run.

**How to read the IQR.**

- **Narrow IQR, clear gap to the next group** → a real difference.
- **Overlapping IQRs** → the difference is not resolved. Report it as
  "no difference detected", not as a ranking.
- **n < 3** → the report labels it an anecdote; treat as an ordering hint.

`WHAT THIS SAMPLE SUPPORTS` states this per-run. Read it before reading
any table above it.

---

## Out-of-sample discipline

**Never gate on an in-sample backtest.** The number a model produces on
the bars it was trained on measures the fit, and PPO will always look
better there than it is.

- `data_window.eval_split: 0.7` → first 70% trains, last 30% is holdout.
- Read metrics on the **holdout** segment only.
- `just bench` (train + backtest back to back) is in-sample. Its comment
  says so; do not quote it as a result.
- There is **no walk-forward re-fitting** yet (see [known limits](#known-current-limits)),
  so today you get one holdout, not a rolling series of them. Varying
  `data_window` across several `{since, until}` values is the closest
  available substitute.

---

## Friction: why a frictionless Sharpe is not a Sharpe

The backtest's edge is *entirely* an artifact of fill assumptions when
costs are zero. Every round trip books a spread it did not pay and a fee
it did not charge. A policy that trades constantly looks brilliant and
would lose money live.

- `fee_rate` — fractional fee per executed order (Kraken retail taker ≈ `0.0026`)
- `slippage` — fractional adverse move on fills (≈ `0.0005` for half a typical spread)

Both are **not optional**. A frictionless matrix answers "how good would
the fills have to be?" — not "does this model trade?". `plan` blocks the
run as a comparison when every cell is frictionless; `report` refuses to
describe such a matrix as supporting any tradeable claim.

Worked example from a **synthetic** (fake-CLI) run, kept for the shape and
not the numbers: SOL_USD showed **+16.21% median excess frictionless** and
**−1.55% with realistic costs** — a sign flip. A frictionless matrix
would have shipped that as a winner.

> Do not quote those figures as a result. They came from a synthesised
> CLI, not from Kraken. The first real-CLI run (2026-10-01) put the
> frictionless-vs-frictioned gap at well under one percentage point,
> because those models traded 39–176 times over ~178 bars — so costs bite
> in proportion to turnover, and a high-turnover policy pays most. The
> sign-flip above is a property of that synthetic policy's turnover, not
> a general law.

---

## Reading the report

```
MODEL MATRIX REPORT
  cells:    12 recorded, 11 valid (11 out-of-sample, 0 IN-SAMPLE), 1 INVALID

INVALID CELLS (1) — excluded from every aggregate
  35adc8fc2c4e  ticker=SOL_USD, seed=44  [n_bars=1 trades=0]
      - n_bars:1<0.50x456
      - zero_trades:0

IN-SAMPLE CELLS (n) — replayed the bars the model was FITTED on
  (listed, and EXCLUDED from every aggregate below)

PER-TICKER SUMMARY        → per ticker, median [q1, q3] per metric
PER-CONFIG SUMMARY        → per (ticker, config); n = seeds behind the median
PER-AXIS MARGINALS        → per axis level, the marginal effect
WHAT THIS SAMPLE SUPPORTS → the explicit statement of what n supports
```

All aggregates below are computed over **out-of-sample cells only**.

**The headline is `excess_return`, not `total_return`.** It sits beside
`buy_hold_return` so you can see whether the model beat doing nothing.
A great `total_return` on an asset that rose 400% is not a strategy.

Order of reading:

1. **`WHAT THIS SAMPLE SUPPORTS`** — can this answer anything at all?
2. **The IN-SAMPLE and INVALID blocks** — how much of the matrix is
   actually usable, and what failed?
3. **`PER-CONFIG`** at `n >= 3` — the decision-relevant medians.
4. **`PER-AXIS MARGINALS`** — what each axis actually moved.

**A single run proves nothing.** If a group has `n=1`, the report says so
and the number is an anecdote. Do not promote it to a result because it
looks good — that is exactly the failure mode this harness exists to stop.

---

## Degenerate cells: the silent failure

**The worked example.** In this repo's 2026-10-01 pass, a training run
replayed **1 bar out of 721 with 0 trades** — silently. The process
exited 0. The feature-width guard reported **PASS**. Nothing errored.

Tabulated naively, that cell reads as **"0% return"** — which looks
exactly like a real, boring, "the model just didn't do much" result. It is
not a result at all; it is an absent measurement wearing a result's
clothes. A harness that averages it in is worse than no harness.

**A cell is INVALID when any of:**

| Condition | Why |
|---|---|
| `n_bars < min_bar_ratio × requested window` (default 0.5) | it did not replay the experiment |
| `num_trades == 0` | no behaviour to summarise |
| any metric is NaN/inf | the number is not a number |
| a contract field is **missing**, or `n_bars` is **null** | absent/null means UNKNOWN, never 0 |
| JSON unparseable, or the process exited non-zero | it failed |
| an **action-space mismatch** — the model's recorded `action_space` disagrees with the run's | a policy bound to the wrong space emits actions it never learned to emit |
| a pinned window left **no tradable bar** | the eval slice is shorter than the warm-up; it *raises* rather than returning `n_bars: 0` |
| the `--config` path does not exist | deliberately raises, so nobody silently falls back to a zero-cost model config |
| a **configured** signal file (`funding_features_file` / `extra_features_file` / `social_features_file`) is unreadable or empty | `signal_file_not_found`; a `null` key is silent, a **set** key is a declared intent to use it, so it raises by name. The shipped `configs/default.yaml` sets `funding_features_file`, so this is the *whole-grid* failure on a checkout without that file — which is exactly why it is named rather than a bare `rc=1` |

Those RL-side refusals are **classified by name** (`classify_process_failure`)
so `run` reports *why* rather than an opaque `rc=1`. They are surfaced and
never retried around.

Note on an **unrecorded** `action_space` (a pre-provenance artifact): the
RL side treats that as **unproven, not a mismatch**, and the harness does
not upgrade it to `action_space_mismatch`.

**Invalid cells are listed, never silently dropped.** `report` prints each
one with its reason codes, states the count in the header, and warns that
the survivors are *not* a random sample — a cell that fails for one reason
often shares causes with the others, so the survivors can flatter.

Three subtleties the harness handles deliberately:

- **The width denominator is the pinned window**, never `pages * 720`.
  `--pages 2` has been measured returning **721 bars, not 1440** (Kraken's
  REST ceiling does not scale with pages), so that arithmetic would flag
  healthy cells as truncated. With no window pinned, the report falls back
  to each ticker's largest observed `n_bars` — the only honest
  denominator available — and says the check was peer-relative.
- **With an active `eval_split`, the denominator is the window's EVAL
  SLICE, not the whole span** — because `n_bars` counts bars a backtest
  *replayed*, and training has already taken the leading `eval_split`.
  Measured against the real CLI on 2026-10-01: a 672-bar window logged
  `-> 202 replayable bars`, 178 were replayed after feature warm-up, and
  denominating by 672 demanded 336 — so **every** out-of-sample cell was
  INVALID and a correct matrix reported zero usable cells. Expect a
  healthy cell to sit near `(1 - eval_split) × span`, minus warm-up.
  `eval_split: 1.0` is the RL side's *no-split* switch, so it keeps the
  full span.
- **`--pages` must be sized to REACH `until`.** The window bounds are a
  **clip on the read, not a push-down** (the push-down is still a
  follow-up), so too few pages yields a *silently short* window that only
  `n_bars` reveals — a 900-bar frame kept just 804 against
  `until=2025-08-01`. `plan` warns (`pages_may_undershoot`) and a cell
  under `min_bar_ratio` of the requested span is INVALID. **Over-fetch
  rather than under-fetch.**
- **A missing or null field is INVALID, not zero.** This is why the
  harness asserts the full **18-key** `to_dict()` contract before assessing
  anything (`equity_curve` is dropped to a summary on parse — ~4 KB of
  floats per cell would bloat the JSONL, and no aggregate reads it).

---

## Known current limits

State these whenever a matrix result is presented; they bound what the
numbers can mean.

1. **PPO only.** `rl/agent.py` is a thin `stable-baselines3` `PPO`
   wrapper. There is no algorithm axis to vary. "Which algorithm?" is a
   documented **future** axis, not a live one.
2. **No walk-forward re-fitting.** The model is fitted once; there is no
   rolling re-fit, so the holdout is a single segment rather than a
   series. Varying `data_window` is the closest available substitute.
3. **Live fetches are not reproducible without a pinned window.** Two
   fetches have disagreed 16% on a fitted feature std. Without
   `data_window`, a re-run is not the same experiment. For genuine
   reproducibility, seed a local `market_data_store` (see
   `configs/deep-history.example.yaml`) — that removes the fetch variance
   at the source.
4. **One market window is still one window.** Three seeds estimate
   *training* variance. They do not estimate *regime* variance: three seeds
   on the same window of one crypto bear market is not evidence the model
   works in a bull market.
5. **A `--config` path that does not exist is a hard error**, deliberately:
   it prevents silently falling back to a zero-cost model config. The
   harness surfaces it as a cell error (`config_not_found`) and does not
   retry around it.
6. **Per-bar Sharpe is not annualised Sharpe.** `sharpe` is computed over
   per-bar equity returns and scaled by `sqrt(n_bars)`. It is a
   cross-cell comparator, not a number to compare against published
   annualised Sharpe figures.
7. **The test suite is not a composition test.** The harness's own tests
   drive a *synthesised* fake CLI, deliberately, so they stay green while
   the RL side moves. That fake cannot express a whole class of
   disagreement — it returns one fixed `n_bars` for every spec, which is a
   physically impossible bar count for a split window. Two real bugs were
   found on the first real-CLI run (2026-10-01) and were invisible to all
   89 of those tests: the width denominator, and the failure classifier
   matching Python exception names the real CLI never prints.
   `tests/test_matrix_rl_contract.py` now imports the real modules to
   close that gap, but it is new and much smaller than the fake — treat
   "the suite is green" as *not* evidence that the harness and the binary
   agree.

---

## Operational notes

- **`run` is resumable.** One JSONL line per cell, appended and `fsync`'d
  as it completes; cell ids are a hash of the parameter tuple, so re-running
  skips finished cells. A crash loses at most the cell in flight.
  `--force` re-runs; `--limit N` stops early; `--dry-run` records the
  commands without executing.
- **Scratch under `/tmp` only.** Never commit anything under `models/`.
- **`plan --strict`** exits non-zero on a BLOCKER, for CI.
- **`report --json`** emits the same aggregates machine-readably.
- **No new dependencies** — stdlib + pyyaml, and the harness never imports
  the RL package. It drives the documented `train`/`backtest --json` CLI
  contract, so it keeps working when the internals move.