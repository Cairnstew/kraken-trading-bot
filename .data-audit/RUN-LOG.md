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
  real `spread` readings → `NaN`, +13 non-finite cells. Fixed on the CONSUMER, by
    `sort_index(kind="stable")` before `groupby(level=0).last()` — *not* by `--append`
    skipping held hours, which this line previously claimed and which is false against
    the code (`export.py:188` is `mode = "a" if append else "w"`; appending is
    unconditional and a duplicate hour is deduped on read). See F-7.
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

### Efficiency, measured (never from memory)

From `~/.local/share/opencode/opencode.db`, sessions with
`directory LIKE '%kraken-trading-bot%'` and `time_created` after 2026-10-02 20:00 —
**9 sessions (lead + 8 teammates), all attributable this time**: input **11,202,405** /
output **293,080** / reasoning **113,007** / cache-read **94,696,967**, cost 0.0 (free
model, not meaningful), **675 tool calls**.

Tool calls per session — lead **122**, then 106 / 104 / 79 / 66 / 65 / 53 / 41 / 40.
The **lead had the most tool calls of any session**, which is the pattern worth naming:
most of them were verification, not coordination.

Cache-read is **~8.5x** input, so the run is still cache-bound, but far less extremely than
the 2026-10-02 pass's 41x — because that pass's `.data-audit/` artifacts (~93KB) were
uncommitted, whereas committing the artifact dir **after each phase** let every worktree
teammate `git show` the artifact instead of being handed an absolute path.

Measured avoidable cost: **~10 `just audit-*` invocations, each entering a fresh
`nix develop` at 17-28 s.** Two of them were re-runs of the same recipe while iterating on
the `audit-findings` repair, and the per-file post-merge verification
(`git show <sha>:<path> | diff -q - <path>`) ran as a shell loop after each of 2 merges.
proposal: the close-out's four checks are already independent subcommands of one script —
add a `just audit-all` that runs verify → findings → evidence → closeout in ONE
`nix develop`, and a `just merge-verify <commit>` recipe wrapping the per-file loop, so
the two repeated shapes stop costing a dev-shell entry each.

---

# RUN LOG — matrix-diagnose pass (2026-10-03)

Appended after the audit-pipeline-1002 record above, which is closed. Nothing here is
retrospective: the red run was captured before the fix, from a real failing cell.

## 1. What was asked, and what was found

A request for a tickers x algo x settings matrix with no Null/NaN. Three findings, in the
order they surfaced:

1. **The matrix harness already exists** — `tools/model_matrix.py`, three subcommands, a
   worked spec. It was not missing; it was mis-used.
2. **`RL_alg` does not exist and cannot.** `rl/agent.py` is a thin SB3 `PPO` wrapper.
   `plan` already emits `no_algo_axis` saying so. Adding the axis would multiply cells and
   return the same answer.
3. **The shipped example spec could not run.** `configs/matrix.example.yaml` carries
   `ticker: [ETH_USD, SOL_USD]`; all three signal channels in `configs/default.yaml` point
   at ETH-only files with `signal_require_ticker: true`. Every SOL cell raises
   `SignalTickerMismatchError` on the train leg. `plan` said nothing.

## 2. RED RUN — the guard shown wrong on purpose (§8, RG6 precedent)

The named defect: **a `SOL_USD` cell against the ETH-only news file.** RG6 requires the red
run to show the *wrong* answer first, to prove the guard is real and not vacuous.

Verbatim, BEFORE any change — `classify_process_failure` on a record `cmd_run` actually
wrote (cell `524a56a0298b`, real stderr, not hand-built):

```
invalid_reasons : ['process_failed']
[ERROR] kraken_trading_bot.cli: Training SOL_USD/mtx_524a56a0298b failed: Signal file
/home/seanc/Projects/kraken-trading-bot/signals/eth_usd_news.jsonl holds no records for
SOL/USD (contains: ETHUSD); point the signal file config key at a SOL/USD file or set
signal_require_ticker: false to merge it unfiltered.
Error training SOL_USD/mtx_524a56a0298b: Signal file ... holds no records for SOL/USD ...
```

The cause is legible in the text and the code was `process_failed` — what ANY failure
returns. That is the defect: a code carrying no information.

Verbatim, AFTER the clause, same spec re-run live:

```
INVALID: process_failed, signal_ticker_mismatch
```

## 3. Why a SEPARATE code, not a fold into `signal_file_not_found`

The two send the reader to **opposite fixes**. A *missing* file means "run the producer"
(`just news-pull`). A file that exists, parses, and is tagged for another pair means the
*pointing* is wrong — you need that ticker's file, or `signal_require_ticker: false`.
Folding them would send a reader to run a producer that already ran, producing nothing.

Its real-CLI text carries **no class name** (`cli.py` prints `str(e)` only), so the clause
matches `"holds no records for"` — the message opener, which appears in no other refusal —
rather than the spelling. Same trap the existing docstring flags for
`PinnedWindowUnavailableError`.

## 4. Plan-time preflight — silence was the actual defect

The classifier only improves the message *after* a cell has burned its train+backtest
budget. `plan` now BLOCKERs the condition up front:

```
[BLOCKER] signal_ticker_mismatch
    Cell ticker SOL_USD has no record in the configured signal file(s)
    ['extra_features_file', 'funding_features_file', 'social_features_file'], which
    carry ['ETHUSD']. ... the seam raises SignalTickerMismatchError on the train leg ...
```

`plan --strict` on the example spec: **exit 1**. On the corrected spec: **exit 0**.

Written conservatively, so it cannot BLOCK a cell the run would accept — every escape
hatch the seam honours is honoured here: a null/empty key is OFF; an **untagged** or
**empty-tagged** file is a one-ticker file the seam merges with a WARNING; and
`signal_require_ticker: false` turns it into a log line. An unreadable file is a
*different* defect (`signal_file_not_found`) and downgrades to silence here. The ticker
fold is re-implemented rather than imported, because the harness never imports the RL
package; if the two copies ever drift, the failure mode is a missed warning, not a false
BLOCKER.

## 5. Verification was made non-vacuous by mutation, not by assertion

The `assess_cell` table shows the guards firing on inputs I drove — which is the same
self-certified green §5 of this record warns about. So each guard was deleted in a scratch
copy and the suite re-run:

| guard deleted | suite went red on |
|---|---|
| `signal_ticker_mismatch` clause | `assert 'signal_ticker_mismatch' in ['process_failed']` |
| `nan_metric` check | `test_nan_metric_is_invalid[total_return]`, `[max_drawdown]`, `test_infinite_metric_is_invalid` |
| the plan-time preflight BLOCKER | `test_a_ticker_absent_from_its_signal_file_is_blocked_at_plan_time`, and `plan --strict` on the example spec returned **exit 0** instead of 1 |

The preflight check found a **real gap in my own work**: the first mutation run failed only
`test_cli_plan_json_and_strict`, and on a `JSONDecodeError` — an incidental failure, not an
assertion that the BLOCKER exists. Nothing tested the preflight. Eight tests were added for
it, including the six escape hatches that keep it from producing a **false** BLOCKER (null
key, untagged file, empty-tag file, `signal_require_ticker: false`, missing file, and the
`XBT`→`BTC` alias). After that, the mutation produced the targeted failure naming the
defect, and the six escape-hatch tests stayed green — proving they do not merely track the
block being present.

Full suite after all three changes: **516 passed**.

## 5b. The audit gate went RED on this change — and that was correct

`tools/model_matrix.py` **is** `MEASUREMENT_TRACK` in `tools/audit_checks.py:44`, pinned
byte-identical to `--prereg`, and also AST-checked. So this work was always going to trip
it, and did:

```
$ just audit-verify --prereg 97a2a52 --since 316a55b
  measurement-track  CHANGED  tools/model_matrix.py byte-identical to prereg 97a2a52
  ast-proof          SELF-TEST OK  (8 mutants detected correctly)
  executable-ast     CHANGED  5/6 files unchanged since 316a55b (docs+strings blanked)
                     differs: tools/model_matrix.py
  suite              PASS  508 passed
  RESULT             FAIL
```

**The guard working, not a regression.** `ast-proof SELF-TEST OK` is the load-bearing line:
the detector caught 8/8 deliberate mutants, so this is not a false positive. `features.py`,
`data_window.py`, `data.py` and `backtest.py` are all unchanged — the edit is confined to
the measurement track and additive (+162 lines, no deletions). The dispersion estimator,
`DISPERSION_RATIO_THRESHOLD` and `dispersion_verdict` are **untouched**, which is the
property the pre-registration exists to protect.

Width hash re-confirmed on the cached 721-bar frame, unchanged by any of this:

```
width-check  bars=721  features=60  start_index=24  usable=697
  expect obs=93edc733  ->  MATCH
  expect arr=6a88a379  ->  MATCH
  nonfinite_obs_cells=0  arr_finite=True  insample_finite=True
```

### The pin is stale in ONE flag only, and that red is being left standing

Two flags, two different questions, and this change invalidates exactly one of them:

| flag | guards | moved here? | current state |
|---|---|---|---|
| `--prereg` | the **estimator and its threshold** — "did the measurement rule change?" | **NO, deliberately** | `measurement-track CHANGED` — **red, by design, documented below** |
| `--since` | "the last commit you believe is code-identical" | **YES**, `316a55b` → `8b220f6` | `executable-ast MATCH 6/6` — green |

`--prereg 97a2a52` asserts `tools/model_matrix.py` is **byte-identical** to the
pre-registration commit. It no longer is, so that line reads CHANGED and
`just audit-verify` exits non-zero. **That red is not being cleared, moved, or
quieted — it is expected, and here is the evidence that it is expected.**

**Cause: an additive change, in the measurement track, that computes nothing a verdict
reads.** This pass added a failure classifier (`signal_ticker_mismatch`) and a plan-time
preflight. Neither produces or consumes a number on the path to a dispersion verdict.

**Independent evidence the estimator did not move:**

| evidence | result |
|---|---|
| `DISPERSION_RATIO_THRESHOLD = 1.0` present | 1× at prereg, 1× now |
| `def dispersion_verdict` | 1× at prereg, 1× now |
| `def pooled_within_spread` | 1× at prereg, 1× now |
| `def replicated_groups` | 1× at prereg, 1× now |
| `MIN_REPLICATES_FOR_A_CLAIM =` | 1× at prereg, 1× now |
| deletions vs `97a2a52` (`git diff`, `-` lines) | **zero** — the change is +162, no removals |
| `git diff --stat 97a2a52 HEAD -- tools/` | `model_matrix.py` untouched in history; this pass adds the 162 lines |
| width hash on the cached 721-bar frame | obs `93edc733…` MATCH, arr `6a88a379…` MATCH, `start_index=24`, `nonfinite_obs_cells=0` |
| `features/data_window/data/backtest` AST vs prereg | all unchanged |
| `ast-proof` self-test | SELF-TEST OK, 8/8 mutants detected — the detector is not merely firing |

**Why the red stays anyway.** A guard that is red by design trains its readers to ignore
it, and this repo has already argued that point once in this very record (§5: two
independent failures that came from reasoning about a summary instead of the artifact).
A silently-green gate is worth less than a loudly-red one — but a red that blocks
closeout forever is not sustainable either, so it is recorded here, given a Phase 7 item
to scope the check properly (§9 item 3), and **reported in the closeout summary as
"not READY, one documented red"** rather than "READY".

### Where the pin lives, and what "moving" it means

`--prereg` is a CLI argument, not stored state: `audit-verify --prereg <c> --since <c>`.
There is no baseline constant in `tools/`, no config key, nothing to edit. So "moving a
pin" is naming a new reference in the audit record and in the invocation — no gate code was
edited to accommodate this change.

**The distinction that matters:** `--prereg` guards the *estimator and its threshold*; it
answers "did the measurement rule change?". `DISPERSION_RATIO_THRESHOLD = 1.0`,
`dispersion_verdict` and `pooled_within_spread` are **byte-identical to `97a2a52`** — only
the failure-classifier and the plan preflight were added, and neither computes a number
that feeds a dispersion verdict. So the estimator pin is **still valid at `97a2a52`** and
is deliberately NOT moved. What moved is `--since`, the "last commit you believe is
code-identical" reference, which is what this change invalidates.

New `--since` reference for future invocations: **`8b220f6`** (this pass's code commit).
`--prereg 97a2a52` should continue to be used unchanged — that is the whole point of
pre-registering it separately.

## 6. The run — 6 cells, ETH_USD, live arm

`configs/matrix.eth-single.yaml`. SOL stayed out: giving it its own signal files is data
work, not matrix work. Recorded as a limitation in the spec header, not smoothed over.

**Window arithmetic, checked before the run and confirmed by it.** The live frame is 721
bars, `2026-09-03T15:00Z .. 2026-10-03T15:00Z`, and `pages=1` and `pages=3` both return
721 — Kraken's REST ceiling does not scale with pages. A 2020→2026 pin would therefore
give **0 train and 0 eval bars**; the pin used (`2026-09-10T15:00Z .. 2026-10-01T15:00Z`,
504 bars) sits inside the frame on both ends. Predicted ~127 replayed bars; **every cell
replayed exactly 127**, against a 151-bar eval-slice denominator (width guard 75.5).

§9A.1 honoured: this is the **live arm only**. The deep-history/store arm is a separate
matrix — a live-vs-store pair in one matrix replays different bar counts, so the
comparison is an artefact of the harness.

### Valid cells (6 of 6; 0 INVALID)

| seed | fee | slip | return | buy&hold | excess | sharpe | trades | n_bars |
|---|---|---|---|---|---|---|---|---|
| 42 | 0.0026 | 0.0005 | −0.96% | −0.06% | −0.90% | −3.372 | 13 | 127 |
| 43 | 0.0026 | 0.0005 | −5.06% | −0.06% | −5.00% | −2.091 | 55 | 127 |
| 44 | 0.0026 | 0.0005 | −2.41% | −0.06% | −2.35% | −0.887 | 23 | 127 |
| 42 | 0.0 | 0.0 | −0.13% | −0.06% | −0.06% | −0.728 | 13 | 127 |
| 43 | 0.0 | 0.0 | −1.76% | −0.06% | −1.69% | −0.701 | 58 | 127 |
| 44 | 0.0 | 0.0 | −1.92% | −0.06% | −1.85% | −0.721 | 26 | 127 |

**Invalid cells: none.** All six `status: ok`, `invalid_reasons: []`, `out_of_sample: True`,
`window_pinned: True`, `eval_split_active: True`.

### Null/NaN audit — read straight from the JSONL, not the report

- NaN or inf across all 8 `NUMERIC_METRICS`, all 6 cells: **NONE**
- null across all 18 `REQUIRED_BACKTEST_FIELDS`, all 6 cells: **NONE**
- missing contract field: **NONE**

This is a stronger claim than "the report showed no INVALID": it was computed directly from
the records.

## 7. RESULT — 6 of 6 valid, both arms losing money, NOT SEPARATED at 0.44×

**6 of 6 cells valid, all losing money in both friction arms, NOT SEPARATED at 0.44× under
the pre-registered rule.**

Pooled within-group IQR **+1.47%** over the 2 groups with n≥3; the friction gap is **+0.65%**
(−1.69% → −2.35% median excess). The gap sits inside the seed noise.

**A finding, not a failure.** `DISPERSION_RATIO_THRESHOLD = 1.0` was fixed in `97a2a52`,
before this run, and was not touched. The ratio is the fact; the threshold is the judgement
call, and both are quoted so a reader can disagree with the latter.

Seeds are a floor, not a verdict. The `seed` marginal reads **−0.48% / −3.35% / −2.10%**
for seeds 42 / 43 / 44 — a spread **wider than the friction gap it is being asked to
detect**.

That is recorded as an **observation, with no conclusion drawn from it here**. It is not
evidence that friction is irrelevant, nor that the signal is absent, nor that 3 seeds are
enough: it is the measured width of one arm's seed spread on one thin eval, and what it
licenses is only the verdict already stated above — the gap is not resolved by this sample.
Whether a larger eval would resolve it is exactly what §9 item 2 leaves open.

### What the out-of-sample claim covers — and nothing wider

Every cell reports `out_of_sample: True`, and that is exactly as strong as its scope: **one
30-day live frame, one pinned window, 151 eval bars with 127 replayed, ETH only, 3 seeds per
level.** The run is out-of-sample *within that window* and supports nothing beyond it.

- **It is NOT a claim that friction or the window "doesn't matter".** The gap sits inside
  seed noise on a thin eval slice, and the run says nothing beyond that. NOT SEPARATED is
  not evidence of absence; it is an unresolved comparison on a sample too small to resolve
  it.
- **Both arms lose money in all six cells.** Median excess is negative at both friction
  levels, and friction does not change the sign. Nothing here is a candidate strategy.
- **Three seeds estimate training variance, not regime variance.** Nothing about another
  market regime.
- **One ticker.** No cross-asset claim of any kind.
- **Sharpe here is per-bar and is not annualised.** It is a cross-cell comparator only, not
  comparable to a published annualised figure.
- **The frictionless rows are a diagnostic ceiling**, never a quote. Every number above
  from the `0.0026/0.0005` level is the one to read; the frictionless median appears only
  because the dispersion gate needs both arms.

## 8. Open, with triggers

- **`--live-vs-store` guard (§9A.1).** Still an underlying defect in
  `tools/model_matrix.py`; worked around here by running two matrices. Unchanged.
- **More seeds before any friction claim.** At n=3 the gate is necessary, not sufficient.
  Trigger: a decision that depends on the friction gap being real.

## 9. Phase 7 additions — LISTED, NOT STARTED

1. **SOL-scoped signal files (data work).** `configs/default.yaml` points all three
   channels at ETH-only files, which is why SOL is absent from the matrix above rather
   than measured and found wanting. Needs news + funding + social pulled against SOL
   paths (`just news-pull` / `funding-pull` / `social-pull` with SOL arguments). Until
   they exist, **no matrix in this repo can answer a cross-asset question**, and the
   preflight now says so as a BLOCKER rather than letting the cells fail one by one.
   Trigger: the three SOL files exist.
2. **The thin-eval problem.** 127 replayed bars cannot separate anything smaller than the
   seed spread — which is precisely what the 0.44× verdict is measuring. A deeper
   evaluation needs the **store arm in its own matrix** (a separate `data_window` against
   a seeded `market_data_store`), never a wider pin on the live leg: the live REST ceiling
   is ~721 bars regardless of `--pages`, so a longer live window is not reachable by
   asking for more pages. Trigger: a store is seeded and the store-arm matrix is specced.
3. **Scope the measurement-track guard to the ESTIMATOR, by AST.** `MEASUREMENT_TRACK`
   (`tools/audit_checks.py:44`) currently asserts `tools/model_matrix.py` is
   **byte-identical** to `--prereg`, so it goes CHANGED for *any* edit to the file — which
   is why §5b carries a documented red that is expected but still blocks closeout. The
   check should compare only the estimator's own surface — `DISPERSION_RATIO_THRESHOLD`,
   `dispersion_verdict`, `pooled_within_spread`, `replicated_groups`,
   `MIN_REPLICATES_FOR_A_CLAIM` — by AST, and stay green when unrelated code is added to
   the same file. That makes the guard go red **only when the estimator moves**, which is
   the thing the pre-registration is actually protecting. The byte-identity check stays
   as a separate, explicitly-labelled check, because "the file did not change at all" is a
   real and useful fact even when it is not the gate. Note this needs the same care the
   `ast_selftest` already gets: a guard that cannot fail is worse than no guard, so the
   scoped check ships with its own mutants. Trigger: the next pass that needs to justify
   editing `model_matrix.py` again without a documented red.


## 10. What the commits actually contain

Three commits, recorded here because the split was specified as four and **three is what
the history shows**. Nothing is missing; one intended commit does not exist, and the
reason is worth stating rather than leaving a reader to guess.

| sha | subject | files |
|---|---|---|
| `8b220f6` | `fix(matrix): name a mis-pointed signal file, and refuse the cell before it runs` | `tools/model_matrix.py` (+162), `tests/test_model_matrix.py` (+279) |
| `d0b56f0` | `docs(audit): move the --since reference to 8b220f6; --prereg stays at 97a2a52` | `.data-audit/RUN-LOG.md` (+261) |
| `210cfea` | `feat(matrix): a single-ticker ETH cost-aware matrix that can actually run` | `configs/matrix.eth-single.yaml` (+174) |

**THE BUNDLE, STATED PLAINLY: `d0b56f0` is the `--since` baseline move AND the audit record,
in one commit — it is not a dedicated baseline commit.** The `--since` move was intended to
be isolated in its own commit so a pin change could never hide inside the change that
caused it. That intent was **not met**, for a mechanical reason: the record is a single
file, so staging `RUN-LOG.md` for the baseline message also staged the red run, the §5b
gate evidence and the §7 results rewrite, which live in that same file. A separate record
commit then had nothing left to carry.

The mitigation is that the separation is still legible to a reviewer rather than merely
asserted here:

- the pin move is the **subject line** of `d0b56f0`, not a line buried in it;
- `--prereg` and `--since` were treated differently **inside** that one commit, and §5b
  says so explicitly — one moved, one deliberately did not, with the reason;
- the estimator evidence in §5b is a **table of symbol-presence checks** anyone can re-run
  against `97a2a52`, so a bundled commit does not have to be taken on trust.

**A fourth commit was considered and rejected:** a dedicated "move the baseline" commit
containing nothing but a one-line record edit. It would have had no diff to speak of and
would have added a sha without adding a safeguard.

`configs/matrix.eth-single.yaml` (`210cfea`) and the code change (`8b220f6`) are cleanly
separate, and neither contains any part of the pin decision.

## 11. The other two reds, bisected to a commit — "pre-existing" gets a boundary

Earlier this pass called `audit-findings` and `audit-evidence` "pre-existing", meaning
only "not mine". That is too weak a word to leave in a record: it hides *when* they broke
and *who* to ask. Both were re-run at every commit from the last recorded green
(`06a9d2a`) forward through the six already-ahead commits:

| commit | audit-findings | audit-evidence bad citations |
|---|---|---|
| `06a9d2a` | **PASS** | 9 |
| `5b5ff99` | PASS | 9 |
| `c6295be` | PASS | 9 |
| `f6d9118` | PASS | 8 |
| `7534ace` | PASS | 8 |
| **`bbdbe56`** | **MISSING DISPOSITION: F-2..F-14, F-16** | **1** |
| `b112184` | red | 1 |
| `2f96a92` | red | 1 |
| `316a55b` | red | 1 |

**BOTH reds were introduced by `bbdbe56`** ("docs: data-pipeline pass 2026-10-03 — Phase 3
decision"), which rewrote `DECISION.md`. So "pre-existing" now means something checkable:
*introduced by `bbdbe56`, in the previous pass, not by the four commits here.*

**What `bbdbe56` actually did to each check — the two are not the same kind of red.**

- **`audit-findings`: a genuine regression.** The Phase 3 rewrite of `DECISION.md` carried
  a fresh findings table (F-1, F-15) and dropped the decision-level disposition for F-2
  through F-14 and F-16. The findings still exist; their dispositions no longer do. That is
  the R5 defect class the check exists to catch, and it caught it. **This is a real defect
  and needs 14 real dispositions** — fixed, deferred with a trigger, or rejected with a
  reason. It is NOT to be made green by bulk-writing "deferred".

- **`audit-evidence`: a wrong citation, and the test is NOT lost.** The check reports
  ``tests/test_gc_producer_append.py`` cited at `DECISION.md:401` but absent from the repo.
  Established rather than assumed: it is in **no commit** (`git log --all -- <path>` empty),
  in **no stash**, and in **none of the eight leftover ensemble worktrees**. So it was never
  written here — which matches what the spec itself says. `DECISION.md:401` reads "in each
  sibling's own test suite, since the write code lives there", and both siblings do have
  it: `~/Projects/ticker-news-signals/tests/test_export_append.py` and
  `~/Projects/kraken-social-signals/tests/test_export_append.py`, six tests each, including
  the RG1/RG2 sentinel guard (`test_two_appends_keep_the_first_file_intact`). **The guard
  ran and passed; the citation points at a path in the wrong repo.** The fix is to correct
  the citation and to record that a decision doc cited a test that was never there.

  Worth stating plainly, because it changes what the number means: **`audit-evidence` was
  never green in this chain.** At `06a9d2a` it reported 9 bad citations; those were
  *cross-repo* paths (`client.py`, `kraken-python/kraken_api/transport.py`) that the
  in-repo checker cannot resolve, not wrong claims. `bbdbe56` fixed 8 of those and traded
  them for 1 that is genuinely wrong. The error count fell 9 → 1 while the underlying
  honesty got better, which is exactly why "9 → 1" must not be read as "1 remains to fix".

## 12. Closeout line — NOT READY, three documented reds

**This is not a clean close-out and is not described as one anywhere.** As of `5492895`:

| red | introduced by | kind |
|---|---|---|
| `measurement-track CHANGED` | `8b220f6` (this pass) | **EXPECTED** — additive change to `model_matrix.py`; estimator symbols proven unmoved, zero deletions vs `97a2a52` (§5b). Phase 7 item 3 scopes it to the estimator so it goes red only when the estimator moves. |
| `audit-findings` — 14 missing dispositions | **`bbdbe56`** (previous pass) | **DEFECT** — Phase 3's `DECISION.md` rewrite dropped them. Needs 14 real dispositions. |
| `audit-evidence` — 1 bad citation | **`bbdbe56`** (previous pass) | **DEFECT** — a citation to a test that was never in this repo; the real tests live in both siblings. |

The two defects are a small fix pass, **not a Phase 7 deferral**, and they run in parallel
with the G1 recorder rather than ahead of it — the recorder is the one with a data-loss
clock. `audit-closeout` is re-run after that fix pass, and **only then** is READY written.
Until then the status is: **not READY, three documented reds.**

## 13. Ensemble worktree cleanup — refs, not shas

Six non-main worktrees were registered, left by the previous pass. **A first pass at this
produced a confidently wrong answer and nearly caused data loss**: a loop broke `PATH`
mid-run, `git`/`wc`/`grep` returned "command not found" for the later entries, and the
malformed output was still reported — five of six branches described as "fully merged into
master" when **five of six held commits not in master**. Caught by an independent patch-id
check before anything was deleted. That failure is now a standing rule at **DECISION.md
§8.1** (output from a broken run is discarded and re-run, never reported).

Containment established by **patch-id** (content-based, not subject-based), because
subject matching gave a false "merged" on a cherry-picked commit whose trees differ:

| branch | commits | patch-id verdict |
|---|---|---|
| `…audit-pipeline-1002c-h7biuo-builder` | `4fba0ab` | CONTAINED |
| `…data-audit-1002-g08vu1-builder` | `e824f0c` | CONTAINED (as `8569c08`) |
| `…matrix-harness-pseemi-matrix-verify` | — | 0 ahead |
| `…audit-pipeline-2-kd5vk5-builder` | `2df5715`, `f6a5761` | BOTH CONTAINED (`f6a5761` trees identical to `a39e184`) |
| `…audit-pipeline-geu0v0-builder-2` | `6ebe95e`, `52271cb` | **NEITHER contained** |
| `…matrix-harness-pseemi-harness` | `94d03e2` | **NOT contained** |

### Refs, not shas

A sha in a document is a pointer to an object `git gc` may delete once nothing references
it, so the three uncontained commits are pinned by **annotated tags**, verified to resolve
both before and after the worktree removals:

- `archive/52271cb-export-data-wip` → `52271cb`
- `archive/6ebe95e-fitted-normalization` → `6ebe95e`
- `archive/94d03e2-matrix-harness` → `94d03e2`

Their branches are also still present. **Nothing was purged**: `team_cleanup`'s archive
purge was deliberately NOT called, per instruction, and no `team_cleanup` of any kind was
run in this pass.

### Removed (four), each checked clean first

`git status --porcelain` was empty in both worktrees that still had a directory
(`matrix-verify`, `ktb-builder-fix`). The other two (`h7biuo`, `g08vu1`) had **no
directory at all** — `prunable`, git's own marker for it — so there were no working files
to be dirty; that was confirmed by finding the paths absent rather than inferred from the
flag. Patch-id containment covers commits; the porcelain check covers the uncommitted files
patch-id cannot see. Removed with `git worktree remove`, not with a purge.

## 14. Supersession review of the three uncontained commits

### `6ebe95e` — "apply the fitted normalization stats to the observation" · SUPERSEDED

The commit's thesis: the moments must be fitted over the **observation frame**
(`compute` → `ffill` → `fillna(0)`), not over raw `compute` output, because fitting on raw
output mixes the NaN warm-up rows into the moments and makes the `z_` columns a *different*
normalization from the one the policy runs on.

**Every claim verified present in master, by expression rather than by appearance:**

| 6ebe95e route | master's line | expression |
|---|---|---|
| `fit` → `observation_frame(compute(df))` | `features.py:642` | `self.compute(df).ffill().fillna(0.0)` |
| `transform` | `features.py:740` | `self.compute(df).ffill().fillna(0.0)` |
| `TradingEnvironment._raw_feature_array` | `environment.py:488` | `self._features.ffill().fillna(0.0)` |
| `PaperTrader._build_observation` | `paper_trade.py:345` | `computed.ffill().fillna(0.0)` |
| export's feature block | `export.py:192` | `computed.ffill().fillna(0.0)` |

And `observation_frame` is, in full, `return features.ffill().fillna(0.0)`. So the
abstraction is a one-line wrapper over an expression master writes out at all five sites.
Master's `fit` docstring (`features.py:612`) states the invariant in prose — "Stats are
fitted on the **ffilled observation frame**". **Behaviour is equivalent.**

The refactor's real value — one function every observation path must route through, so the
convention cannot drift — is **not** reproduced in master, which has no `observation_frame`
symbol. That is a genuine difference, and it is a *maintainability* difference, not a
behavioural one.

**Where master is arguably ahead:** `features.py:55-70` carries a comment explaining why
the fill policy is subtle — `signal_observed` (0.0/1.0) and `signal_age_hours` (−1.0 for
"no reading") are *load-bearing sentinels*, and mapping their values to NaN "is what broke
three freshness/signal regression tests when this list was first used here". `6ebe95e`
predates that finding and shares no such guard.

**The five tests `6ebe95e` added are all absent from master by name** —
`test_environment_observation_is_z_scored_affine_image`,
`test_environment_builtin_features_unscaled`,
`test_built_observation_is_z_scored_with_the_saved_stats`,
`test_prepare_episode_slices_window_and_fits_stats`,
`test_prepare_episode_fits_stats_on_observation_frame`. Checked individually rather than
assumed lost: **each invariant is covered in master under a different name**, and master's
coverage is broader (114 tests in `test_model_matrix.py`, plus
`test_observation_uses_saved_stats_not_a_refit`, `test_built_observation_is_z_scored_by_saved_stats`,
`test_normalized_block_equals_environment_observation`,
`test_all_six_activated_columns_reach_the_observation`,
`test_ten_zero_variance_columns_normalize_to_zero_not_inf`). **No invariant was lost.**

### `52271cb` — original `export-data` CSV export (WIP) · SUPERSEDED

All 9 touched files exist in master. All three exported symbols present
(`build_export_frame`, `write_export_csv`, `default_export_path`), `cmd_export_data` wired
into the CLI, and master's `test_rl_export.py` carries **11 tests against the 10 it
shipped**. The feature was rebuilt, not merged — hence the differing patch-id.

### `94d03e2` — original model-matrix harness · SUPERSEDED, and the progenitor

All 6 files present. All six key symbols present (`cmd_plan`, `cmd_run`, `cmd_report`,
`assess_cell`, `dispersion_verdict`, `classify_process_failure`). Master is **larger on
every axis**: `tools/model_matrix.py` 2917 lines vs 2279 shipped;
`test_model_matrix.py` **114 tests vs 87** shipped. This is the ancestor of the harness
this pass has been extending — my own commits modified `model_matrix.py` today.

**Conclusion: all three superseded. No hunk is missing from master.** The tags stay until
this is approved for dropping.

## 15. The three archive tags are dropped — shas recorded as the trail

Per instruction, the three tags and their two branches are deleted. The tip shas are
recorded here so the trail survives the refs going with them:

| sha | subject | why superseded |
|---|---|---|
| `52271cb` | `feat(rl): data-pipeline CSV export (export-data) WIP` | Rebuilt, not merged: all 9 files present in master, all 3 exported symbols (`build_export_frame`, `write_export_csv`, `default_export_path`) present, `cmd_export_data` wired, and master's `test_rl_export.py` carries 11 tests against the 10 it shipped. |
| `6ebe95e` | `fix(rl): apply the fitted normalization stats to the observation` | Behaviour carried by identical expressions at all five hand-written sites; invariant proven protected by mutation (§16); convention now drift-guarded by two tests. |
| `94d03e2` | `tools: a benchmark-matrix harness that refuses unvalid comparisons` | The progenitor of master's harness: all 6 files and all 6 key symbols present, master larger on every axis (`model_matrix.py` 2917 vs 2279 lines; tests 114 vs 87). |

`git worktree list` now lists master alone. Suite: **518 passed**.

## 16. Was `6ebe95e`'s invariant actually protected? Two mutations, not a test count

The coverage claim for its five tests rested on "different name, broader coverage" and on
counts — and **a count is not coverage**. Settled by mutation.

**Mutation 1 — is the invariant guarded at all?** Drop `.ffill().fillna(0.0)` from the
`fit` site (`features.py:642`) so the moments are fitted on raw `compute(df)`:

```
61 failed, 438 passed, 17 errors   across 6 files
```

and that site's own symptom is precisely the NaN-warm-up one the convention exists to
prevent:

```
NonFiniteFeatureError: Non-finite feature values (nan) at the fit stage for
ticker 'ETH_USD': 237 cell(s) across 21 column(s) ['return_1', 'log_return_4',
'price_ratio_sma_24', 'log_return_24', ...] (+13 more)
```

The named columns are exactly the rolling-window features that need look-back. **PROTECTED.**

**Mutation 2 — can the convention drift silently?** The expression is hand-written at five
sites with no shared helper, and no test drove all five off one frame. Two tests added
(`c8aed16`):

- `test_the_observation_frame_expression_is_written_at_every_declared_site` — mutated
  (environment site → `fillna(0.0)`, `ffill` dropped) it goes RED:
  `AssertionError: the observation-frame convention moved or lost a site:
  kraken_trading_bot/rl/environment.py: 'self._features.ffill().fillna(0.0)'`. The
  failure names file and expression; a sixth site means a new line here.
- `test_all_reachable_observation_routes_agree_row_for_row` — drives fit / transform /
  environment / export off ONE episode and asserts row-for-row equality.

**Two findings recorded in the tests rather than smoothed over:**

1. The export's feature block is the **pre-transform** frame (`export.py:204` writes
   `observed`, the ffilled matrix; its `z_` block is route 2 by construction), so route 4
   compares against the frame, not the normalized matrix. Measured max abs diff **2428** on
   `obv` before that was corrected — comparing across the boundary is a false red.

2. **MEASURED: on this frame `ffill()` and `fillna(0.0)` are numerically IDENTICAL** (max
   abs diff **0.0** across all 49 columns), because the warm-up NaNs form one LEADING block
   with nothing earlier to carry forward from. So dropping `ffill` from the *environment*
   site leaves the behavioural test green — route 1 is rebuilt from `fitted_frame`, not read
   back out of the environment. **The presence check is what catches that mutation**, plus
   the 61-test blast radius at the fit site. A test asserting the two fills differ would be
   asserting something false about this frame, and is deliberately absent.

One stale-worktree run reported `30 deselected`, which was a failed collection rather than a
passing filter. Discarded and re-run per **§8.1**, added earlier the same day.

## 17. Push state — NOT pushed

`79b2590` and its successors are **local only**. Nothing in this record describes any commit
from this pass as pushed. The manual push is the user's and had not happened at the time of
writing; `git rev-list --count origin/master..HEAD` counts local commits ahead, which is
consistent with an unpushed branch.

## 18. Open: ten stale `opencode/*` branches, NOT touched

Removing the two named branches left **ten** stale ensemble branches registered. By
patch-id, **seven** hold nothing new (CONTAINED), and **three** are genuinely uncontained:

| sha | subject | assessment |
|---|---|---|
| `e57056d` | `fix: wire normalization.npz into the observation path (Candidate 1)` | Same normalisation wiring as `6ebe95d`'s family — master normalizes the observation (`stats.normalize` in `environment.py`) and carries the npz plumbing. **Almost certainly superseded**, but it was not in the approved scope and was not assessed hunk-by-hunk. |
| `ab44f11` | `feat(rl): add extra_features_file seam for exogenous news signals` | The seam master shipped and then activated under G-C (`default.yaml` has `extra_features_file`; `data.py` has `merge_extra_features`). Almost certainly superseded. |
| `4c65432` | `feat: export-data CLI + config default path fallback (prior WIP)` | Same family as `52271cb`. Almost certainly superseded. |

**Not deleted.** The instruction named three commits and two branches; these are neither, and
"almost certainly" is not the standard the rest of this cleanup was held to. They are cheap to
assess (the §14 method) whenever that is wanted. `team_cleanup` remains uncalled.

## 19. Both bbdbe56 reds are GREEN; closeout is NOT READY and says why

`audit-closeout` run at HEAD `cf31f9e`, verbatim:

```
  closeout  branch=master  upstream=origin/master
    HEAD == upstream   NO  (cf31f9ed)
    working tree       2 dirty entry(ies)
                       M .gitignore
                       ?? notebooks/

    ordered steps:
      1. just audit-verify --prereg <pre-reg> --since <last-code-identical>
      2. git push                                         # push, THEN re-check step 1
      3. just audit-verify --prereg <same> --since <same>  # in the PUSHED state
      4. just audit-findings                             # every F<n> has a disposition
      5. just audit-evidence                             # nothing cited is /tmp-only
      6. Phase 8 self-improvement -> append the RUN LOG entry
      7. shutdown every teammate, THEN team_cleanup      # order matters; see command file

    RESULT  NOT READY — resolve the above first
```

Both reds are closed:

| check | result |
|---|---|
| `audit-evidence` | **PASS** — the last error was this file's own `RUN-LOG.md:615`, a single-backtick citation to a file the same sentence establishes is absent. A quotation, not a citation; wrapped as ``…``. |
| `audit-findings` | **PASS** — `PIN OK 16/16 rows, all ids pinned, every disposition cell non-vacuous` |
| `audit-verify` | `suite PASS 602 passed`; `measurement-track CHANGED` — the **documented expected red** from `8b220f6` |

### 19.1 The measurement-track line was stating the opposite of its own verdict

The check was always right. The sentence was not:

```
before:  measurement-track  CHANGED  tools/model_matrix.py byte-identical to prereg 97a2a52
after:   measurement-track  CHANGED  differs from prereg 97a2a52 (+162/-0 lines) — NOT byte-identical
         measurement-track  MATCH   byte-identical to prereg HEAD
```

The trailing clause was an **unconditional f-string**, so a CHANGED verdict still
printed "byte-identical". On the one line a reader trusts when deciding whether the
estimator moved, the tool asserted the opposite of what it had just measured. Both
branches now verified by running them. `_numstat()` returns `(0, 0)` rather than raising
when a ref cannot be diffed, deliberately: the verdict is already decided by the byte
comparison, so a *reporting* failure must not become a false red.

The `+162` is `8b220f6`'s `signal_ticker_mismatch` classifier and preflight. Unchanged by
this commit, which touches only the message.

### 19.2 What is still blocking NOT READY — and neither item is mine to close

1. **`HEAD == upstream NO`.** 21 commits local, unpushed. Step 2 is `git push` and the
   push is **the user's**, by standing instruction. Step 3 then re-verifies *in the
   pushed state*, which is a distinct claim: a verification run on unpushed HEAD is not
   the same artifact as one on pushed HEAD.
2. **Working tree dirty**: `M .gitignore`, `?? notebooks/`. Both are the standing
   notebook exclusion — notebook files stay out of every commit. `.gitignore` carries an
   unstaged `.ipynb_checkpoints/` rule that predates this work. Not committed by me
   because it is notebook work and the rule is explicit; it is the user's call whether
   that rule lands as its own commit.

### 19.3 A seventh wrong-measurement-tool, same family as the others

`audit-verify` shells out to bare `pytest`. Run from outside the dev shell it produces
**19 collection errors** (`ModuleNotFoundError: No module named 'kraken_api'`) because
`PYTHONPATH`, which carries the `kraken-python` sibling, is absent — so the suite gate
reads `NO SUMMARY` and `RESULT FAIL` for a suite that passes 602. My first two attempts to
reproduce closeout hit exactly this and I initially took the FAIL at face value.

Both were **discarded and re-run inside `nix develop`**, per §8.1. Not yet recorded in the
tooling as a fix; flagged for the next pass, because an audit gate that reports FAIL
because of the caller's shell is a gate that can cry wolf.

That is now five wrong tools in one day, all the same family — a plausible-looking check
answering a different question than the one being asked:

| tool | claimed | reality |
|---|---|---|
| line-order `diff` | mass divergence | artifact: same lines, shifted |
| set-difference `comm` | 84 lines missing | artifact: master refactored the logic |
| `comm` on `grep`-prefixed output | everything missing | artifact: `grep` prefixes filenames |
| patch-id after a squash-merge | 3 commits uncontained | artifact: a squash patch is a *union* |
| bare `pytest` outside the dev shell | 19 collection errors | artifact: `PYTHONPATH` absent |

Each was caught only by checking a *different* way and demanding the two agree.

## 20. Follow-ups from the §19 closeout, at the lead's direction

### 20.1 The three `opencode/*` commits: tagged before cleanup, and still resolvable

Nothing was purged. `team_cleanup` was run **without** the `purge` option, so no archived
team record and no preserved branch was deleted. State at `e7d53ba`:

| sha | archive tag (annotated, 2026-10-03) | resolves to | branch |
|---|---|---|---|
| `e57056d` | `archive/e57056d-normalization-wiring` | `e57056dd…` ✅ | `…-geu0v0-builder` deleted |
| `ab44f11` | `archive/ab44f11-extra-features-seam` | `ab44f112…` ✅ | `…-t2huls-integrator` deleted |
| `4c65432` | `archive/4c65432-export-data-wip` | `4c65432c…` ✅ | (shared the `geu0v0-builder` branch) |

All three tags were created **before** the branches were deleted and before cleanup, and all
three still peel to the intended commits. Each carries its supersession reason in the tag
message. **Every sha is recoverable by name**, so nothing depends on reflog survival.

§14 results, all three **SUPERSEDED**:

- **`e57056d`** — 7 files (my brief said 5; the auditor caught that). Of 84 distinct
  substantive added lines, **83 are present in master verbatim**; the single exception is the
  fit site, where master is *ahead* (`self.compute(df).ffill().fillna(0.0)` **plus**
  `_require_finite`, which the branch lacks). Re-verified at the lead by content diff after
  two wrong tools (line-order diff, set-difference) both reported mass divergence.
- **`ab44f11`** — 4 mutations, **all RED**: stub → 41 failed, read-path wiring removed → 18,
  `train.py` kwargs dropped → 4, config → `null` → 6. The anticipated *unprotected seam* does
  not exist; the finding was the opposite of the hypothesis.
- **`4c65432`** — the `52271cb` shape: 9/9 files, 5/5 symbols, master's tests exceed the
  shipped counts on all three files (10→11, 19→21, 14→18).

Still standing, deliberately: `…-g1-recorder-branch-audit-x622s1-recorder` → `e64a2ef`. All
seven of its files are **byte-identical** in master (patch-id reports 3 uncontained only
because a squash patch is a *union* — the sixth wrong tool of the day). One `opencode/*`
branch retained is not a leak; it is the only ref pinning those three commit objects, whose
content is already merged.

### 20.2 The store-shaped behavioural test landed

`b99bc56`, an ancestor of HEAD. `test_every_observation_route_agrees_where_ffill_and_fillna_differ`
plus the `_store_shaped_frame` fixture, green. Its measured mutation matrix: dropping
`.ffill()` goes RED at `features.py:642` (fit), `:740` (transform),
`environment.py:488` (env) and `export.py:192` (export), and is caught by the tripwire alone
at `environment.py:525` (builtin, leading-only NaN makes the two fills numerically identical
there — a documented limit, not an oversight).

### 20.3 F-7: the recorder does NOT assume skip semantics — checked, and it is correct

`kraken_trading_bot/depth_recorder.py` states the contract explicitly and I read the code to
confirm it:

> **"log, not state"** — The file is only ever appended to; nothing is rewritten, and no
> prior record is ever corrected in place. A re-fire inside the same hour
> (`Persistent=true` catch-up after a lid-close) therefore appends a second, harmless line
> rather than clobbering a good reading.

The read path **tolerates duplicates rather than resolving them**: `stamps` "may be unsorted
and may contain duplicates: both are real", a duplicate surfaces as a **`SHORT`** interval
counted separately from `GAPS`, and it is never collapsed into one. That is tested
(`test_depth_recorder.py:418`, `STATUS_SHORT`).

**FORWARD-LOOKING, since nothing consumes the data yet (§8.1 item 5):** when a consumer is
written, it must dedupe **on read** — last-wins per floored hour — exactly as
`merge_extra_features` does for the funding/news/social channels. The producer is append-only
and must never be given skip semantics; doing so would silently *lose* a reading, which for
an unrecoverable feed is the one failure that matters.

**Two documents still repeated the false F-7 claim; both corrected in this commit:**

- `.data-audit/VALIDATION.md:600` — the disposition cell of the **pinned** §8 table said
  "`--append` skips held hours". This is worse than a stale duplicate: it is the table the
  findings pin polices, so it **contradicted `DECISION.md`'s corrected row** while passing
  the non-vacuous check. Now states the producer appends unconditionally with no hour-skip,
  and that the dedup direction was fixed on the consumer (`data.py:798-809`, from `5951f72`).
- `.data-audit/RUN-LOG.md:166` — "Fixed by `--append` skipping held hours" → now attributes
  the fix to the consumer's stable sort and records that the old claim was false.

`DECISION.md` and `FIXPASS.md` mention the old wording only to **refute** it, which is correct
and left alone.

### 20.4 `audit-verify`'s suite gate: no longer cries wolf

The gate shelled out to bare `pytest`. Outside the dev shell that is a `FileNotFoundError`
crash, or — where a `pytest` *is* on PATH — 19 collection errors (`No module named
'kraken_api'`) reported as `FAIL NO SUMMARY` on a suite that passes 602. I twice took that
false FAIL at face value before checking the shell.

Fixed by moving the logic into `run_suite_gate()`, which runs
`[sys.executable, "-m", "pytest", …]` — the checker's own interpreter — and distinguishes an
environment problem from a repo verdict:

| case | before | after |
|---|---|---|
| inside the dev shell | `PASS 602 passed` | `PASS 602 passed` |
| outside it | `FAIL NO SUMMARY` (or crash) | `WRONG ENVIRONMENT NO SUMMARY — pytest or a sibling dep is not importable in <exe>, so this is NOT a repo verdict. Re-run inside the dev shell…` |
| a genuine test failure | `FAIL 602 passed` ← reads like a contradiction | `FAIL 1 failed` |

All three verified by running them, the third with a deliberately failing test that was then
removed. It still returns `ok=False` in the broken case — it refuses to claim green, it just
stops blaming the repo for the caller's shell.

### 20.5 Recorder evidence — a firing timer is not the full check

Five snapshots, **five distinct timestamps**, two distinct hours, all `count=100` with 200
levels returned:

```
#1 2026-10-03T19:10:45.752567+00:00  bid=2683.52000 ask=2683.53000 levels=200
#2 2026-10-03T19:12:55.755671+00:00  bid=2683.51000 ask=2683.52000 levels=200
#3 2026-10-03T19:37:04.693310+00:00  bid=2681.57000 ask=2681.58000 levels=200
#4 2026-10-03T19:42:32.145878+00:00  bid=2683.28000 ask=2683.29000 levels=200
#5 2026-10-03T20:41:34.444635+00:00  bid=2686.79000 ask=2686.80000 levels=200   <- 2nd unattended fire
```

Status line, verbatim: `records: 5  intervals: 4  coverage: 100.0%  SHORT: 3  GAPS: 0
longest hole: 0s  VERDICT: GREEN`, and from the sidecar **last-snapshot age 45.2 min,
`n_gaps: 0`, `holes: []`, `ok: True`**. Interval #4→#5 was **3542s against 3600 expected
(0.98×)** — the hourly cadence holding, not a re-fire.

**The unit is DECLARED, not merely hand-installed.** `nix/module.nix` carries
`systemd.services."kraken-trading-bot-order-book"` (line 323) and
`systemd.timers."kraken-trading-bot-order-book"` (line 363), off by default behind
`ob.enable`, in the **`systemd.services`/`systemd.timers`** namespace — the NixOS one. The
namespace trap is called out in the file at line 312. Its `ExecStart` contains **0**
occurrences of `worktree` and targets `/home/seanc/Projects/kraken-trading-bot`.

⚠️ **Two timers can exist, and that is a real double-fire risk worth recording.** The NixOS
module declares a **system** timer; `just depth-timer` installs a **user** timer. They are
independent units and neither suppresses the other, so enabling `ob.enable` on a host that
also runs `just depth-timer` would snapshot **twice an hour**. Today only the user timer
exists here (`ob.enable` is off). Nothing consumes the data yet, so a duplicate is currently
benign — a duplicate *hour*, which the recorder tolerates by design — but it must be resolved
before any consumer lands, or every depth series silently doubles its own cadence.

### 20.6 Minimum depth N, re-derived from the gate (not inherited)

PLAN.md §8.1.2's own instruction is to re-derive `bars_needed` rather than inherit it, since
its figures are measured values with a date on them. Re-derived:

```
live REST ceiling                 721 bars @ 60m           data.py:399 ("~721 recent bars")
warm-up / first_tradable_index     24 bars                 measured this pass: start_index=24
                                  ───
tradable                          697 bars                 VALIDATION §1 (usable=697)
seed-noise eval width             178 bars                 within-config seed spread EXCEEDED
                                                             the between-config effect at this
                                                             width, 3 seeds (EVIDENCE §…, PLAN §8.1.2)
eval must exceed it                712 bars   (178 x 4 margin)
train                             1424 bars  (2 x eval)
                                  ───
bars_needed                       2136 bars
days = bars_needed / 24           2136 / 24 = 89 days      earliest a first evaluation is POSSIBLE
recommended N                     365 days = 8760 snapshots  what makes it MEAN something
```

89 days, not the 90 PLAN.md quotes — the difference is arithmetic, not a disagreement
(§8.1.2 rounded 2,160 = 1,440 + 720). **N = 365 days = 8,760 snapshots**, chosen because it
matches the funding channel's existing ~366-day window, so book and funding can be evaluated
over the same span; at 90 days the book series would be the *shorter* one and the comparison
silently confounded by coverage. Nothing consumes the data until N is met, so a longer N is
free — the only cost of under-waiting is a gate that cannot separate signal from noise, which
this repo has already paid for once.

### 20.7 Closeout stays NOT READY

Unchanged and correct at `e7d53ba`: `HEAD == upstream NO` (21 commits local, the push is the
user's), the working tree dirty with the notebook exclusion, and `measurement-track CHANGED`
as the documented expected red. `.gitignore` and `notebooks/` are the user's to land or
leave — untouched. The checks are re-run **in the pushed state from the dev shell** after the
push; READY is not written before that.

## 21. The lead's six challenges — `SHORT`, the N floor, the double timer, provenance, and a retracted claim

### 21.1 `SHORT 3` explained; my §20.5 summary was the thing that was wrong

The lead was right to stop on this and right that the report as I gave it did not add up.
**My summary said "five distinct timestamps about an hour apart".** That was the error: only
the last two intervals are about an hour apart. The first four records are all inside hour
19. `SHORT` is an **interval status**, and the three it flagged are:

```
  expected = 3600s   gap_factor = 1.5
  SHORT if delta < 2400s      GAP if delta > 5400s

  #  from                       to                             delta status missing  same_hour
  1  2026-10-03T19:10:45.752567+00:00  2026-10-03T19:12:55.755671+00:00   130s SHORT  0  YES
  2  2026-10-03T19:12:55.755671+00:00  2026-10-03T19:37:04.693310+00:00  1449s SHORT  0  YES
  3  2026-10-03T19:37:04.693310+00:00  2026-10-03T19:42:32.145878+00:00   327s SHORT  0  YES
  4  2026-10-03T19:42:32.145878+00:00  2026-10-03T20:41:34.444635+00:00  3542s OK     0  no
  5  2026-10-03T20:41:34.444635+00:00  2026-10-03T21:43:30.815136+00:00  3716s OK     0  no
```

So the three SHORT intervals are the three sub-40-minute gaps inside the **bring-up hour**:
a manual pull, the planted-hole measurement, the worktree-defect re-seed, and the first
unattended fire all landed within 19:00–19:42. Intervals 4 and 5 are the unattended hourly
cadence and are **OK**.

**Answering the lead's two hypotheses directly, because both were reasonable and one was
right to worry about:**

- *"SHORT means the book returned fewer levels"* — **no.** `SHORT` is computed purely from
  the timestamp delta (`classify_interval`, lines 315-360); it never reads the book. But the
  underlying worry is already covered: every snapshot records
  `depth = {requested_count, bid_levels, ask_levels, levels_total, truncated}`, and all six
  snapshots show `bid_levels=100, ask_levels=100, truncated=false` — 0 short of request. A
  short book would surface there, not in `SHORT`.
- *"state it in the status output's own wording"* — **done.** The line used to read
  `SHORT : 3 (re-fire inside an hour — benign, counted separately from gaps)`, which is
  what I over-read as "3 duplicates". It now reads:

  ```
  SHORT         : 3 interval(s) closer than 2400s (= 3600s expected / 1.5 factor)
                   a sub-hour re-fire, timer catch-up, or two manual pulls in quick succession;
                   counted separately from gaps, and NEITHER a duplicate-record count NOR a depth shortfall
  ```

  The threshold is printed rather than only the count, so a nonzero value can never again sit
  unexplained beside a `VERDICT` line. The docstring's "benign" is also gone: a timer
  misfiring twice in 40 minutes is `SHORT` too and is **not** benign.

### 21.2 The `holes` annotation was wrong (found by the type checker, pre-existing)

`GapReport.holes` is annotated `tuple[Hole, ...]`, but it is assigned GAP-classified
`Interval` objects, and `class Hole` is constructed nowhere in the repo. Annotations are not
enforced at runtime, so every number reported so far is unaffected — but it misdescribed the
field to any reader, in the same file where "don't leave state unexplained" is the rule.
Now `tuple[Interval, ...]`, with a comment saying so. **Pre-existing, not introduced here**
(confirmed by AST-reading the stashed tree).

### 21.3 N: which number is the floor and which is the choice

§20.6 blurred these. Precisely:

| | value | kind |
|---|---|---|
| **`bars_needed` 2,136** = train 1,424 + eval 712 | **89 days** | **THE FLOOR.** Derived from measured numbers; a consumer running below it cannot separate signal from seed noise. Hard gate. |
| **N = 365 days = 8,760 snapshots** | 365 days | **THE CHOICE.** Not derived — selected. |

**Why 365 and not 89.** The floor answers "when is an evaluation *possible*". 89 days buys
exactly one train/eval split, which is the matrix-harness finding already paid for once:
within-config seed spread **exceeded** the between-config effect at short eval widths, so a
single split at the floor measures the seed, not the config. 365 days buys four independent
quarterly splits — **Q4 2026, Q1, Q2 and Q3 2027 are four different volatility regimes** —
so the question becomes "does the effect hold across regimes or was it one regime?", which is
the only form of the claim worth making about a signal that will gate real capital. It also
matches the funding channel's existing ~366-day window, so book and funding are evaluated over
the same span instead of the comparison being silently confounded by unequal coverage.

**89 days is recorded as a hard floor: no consumer may run below it.** N = 365 is the target.

**Consumption date implied by the first snapshot.** First record
`2026-10-03T19:10:45Z` + 365 days → **2027-10-03**, so a consumer may first run on
**2027-10-04** (the first full day past the window). That is ~14 months out, and the lead's
point stands: a timer left unobserved for a year needs its status line *seen*, not assumed.
The evidence trail therefore records the unattended cadence as it accumulates — six records
and three unattended fires at the time of writing — and §21.4 makes the survivability
question explicit rather than leaving it to chance.

### 21.4 The double timer is settled now: the recipe refuses

Chosen over deleting either path, because both serve real hosts: `nix/module.nix` is the
declarative route for a NixOS host, `just depth-timer` is the route for a host this repo is
merely checked out on (this one). Deleting a path would break one of those two cases.

`just depth-timer` now **refuses before writing anything** if the system unit is enabled:

```
if systemctl is-enabled --quiet kraken-trading-bot-order-book.timer 2>/dev/null; then
    echo "REFUSING: the SYSTEM timer kraken-trading-bot-order-book.timer is enabled." >&2
    ... names both remedies (ob.enable = true, or disable the unit) ...
    exit 1
fi
```

Refuse, not warn: a warning is what produced this defect in the first place. Three tests,
each extracting the recipe body and running it against a **fake `systemctl`** on `PATH` with
`HOME` redirected, so the assertion neither depends on nor touches this host's units:

- `test_depth_timer_refuses_when_the_system_timer_is_enabled` — non-zero exit, `REFUSING` on
  stderr, and it must name `ob.enable` so the user chooses rather than guesses.
- `test_depth_timer_proceeds_when_the_system_timer_is_absent` — no false block; reaches its
  normal success path. Without this, a guard that always fires would pass the first test.
- `test_the_two_timer_paths_are_both_declared` — the NixOS declaration still exists, so if
  someone deletes a path deliberately they must delete the guard and this test together.

Verified on the real host afterwards: guard silent (`systemctl is-enabled` → `not-found`),
`just depth-timer` completes, timer still `active`, 6 records.

**Normalisation found on the way.** `depth-timer` was the **only one of 42 recipes** indented 4
spaces, and was internally *mixed* — a 4-space header block over a 2-space body. The body is
now 2-space like every other recipe, and the test's recipe extractor no longer assumes an
indent width (it takes lines while blank-or-indented, which is how `just` itself delimits).

### 21.5 Survives logout: yes, already — verified rather than assumed

`loginctl show-user seanc -p Linger` → **`Linger=yes`**, so the user manager persists past
logout and the timer keeps firing with no session. This was already the case; it had never
been checked, and the lead is right that it had to be. Suspend is handled separately and
already: `Persistent=true` means a missed wall-clock slot is caught up on resume, and the
gap counter then shows the *record* of what was missed — it cannot recover the depth itself,
which is precisely why the hourly cadence matters and why §21.3's floor exists.

### 21.6 Recorder commits pinned by a proper ref; harness branch deleted

The lead's concern was right: `opencode/ensemble-…-x622s1-recorder` sat in a harness
namespace that a later cleanup or rename could drop. Resolving the question asked — *are the
commits needed, or only their content?* — path by path against master:

```
IDENTICAL  README.md, justfile, kraken_trading_bot/cli.py,
           kraken_trading_bot/depth_recorder.py, nix/module.nix,
           systemd/kraken-trading-bot-order-book.{service,service.in,timer},
           tests/test_depth_recorder.py
SUPERSET   .data-audit/EVIDENCE-G1-DEPTH-RECORDER.md — master's 734 lines contain the
           branch's 556 verbatim, plus the lead takeover addendum
EXCLUDED   --.status.json — a CLI arg-parse accident, never wanted in master
```

(This corrects §20.1's "all seven files byte-identical": it was **nine** identical paths plus
one superset plus one deliberate exclusion. The count was wrong; the conclusion held.)

**Only the content is needed — it is already in master.** So `archive/g1-recorder-x622s1` now
pins the three commits (`ac44ffd`, `6f8576d`, `e64a2ef`, all verified reachable by name) and
the `opencode/*` branch is deleted. `refs/heads/opencode/*` is now **empty**.

All four archive tags resolve by name: `archive/g1-recorder-x622s1`→`e64a2ef`,
`archive/e57056d-normalization-wiring`→`e57056d`, `archive/ab44f11-extra-features-seam`→`ab44f11`,
`archive/4c65432-export-data-wip`→`4c65432`. They stay **local** (`--tags` is never pushed, per
the lead). Loss of this machine costs the history, not the code — the content is in master.

### 21.7 RETRACTED: there is no formatter, and I should not have said there was

I told the lead "a formatter kept reverting my writes". **That was false, and I retract it.**
Checked for the actual mechanism before accepting it:

| candidate | result |
|---|---|
| `.pre-commit-config.yaml` | absent |
| `.git/hooks` non-sample hooks | none |
| `core.hooksPath` | unset |
| direnv / `.envrc` | absent |
| `flake.nix` devShell `shellHook` | present, but only `echo`s, exports `PYTHONPATH`, and runs two import checks — **rewrites nothing** |
| `black`/`ruff`/`isort`/`treefmt`/`nixfmt` in the dev shell | not installed |
| opencode `plugin`/`plugins` | `[]` (only a stale `.backup` names ensemble) |
| any script referencing `audit_checks` | none |

There is no such tool in this repo, so there was nothing to name, no hook to document, and
nothing to add to a `tools/README` — which does not exist either. The honest description of
what happened is duller and worse: **I broke that file's indentation with my own edits, twice,
and restored it from git myself.** The "reverting" was my own failed writes being reported
to me as failures I then misattributed to an outside agent — the same shape as §20.4's wrong
tools: a plausible external cause preferred over the plain one. The fix (a module-level
`run_suite_gate()` rather than repeated inline edits) stands on its own merits and is
untouched by this retraction.

**No formatter touches `tools/audit_checks.py`**, before or after. Nothing to guard against,
and no environment table to extend.

## 22. The lead's second round — provenance of the six records, a softened N, and an off-host copy

### 22.1 Which records are real readings: 2 seeded, 5 unattended fires — and two errors corrected

The file holds **7** records now (the lead's "six" was accurate when written; a sixth
unattended fire has since landed). Attribution by matching every `recorded_at` against
**all** the journal's `Starting` lines, not one of them:

| # | `recorded_at` (UTC) | nearest service exec | verdict | counts toward N? |
|---|---|---|---|---|
| 1 | 19:10:45.752567 | 1572 s | **MANUAL, and SEEDED** — byte-identical to `/tmp/g1-rescued/` | **NO** |
| 2 | 19:12:55.755671 | 1442 s | **MANUAL, and SEEDED** — same | **NO** |
| 3 | 19:37:04.693310 | **7 s** | **TIMER** — `Persistent=true` catch-up on enable | yes |
| 4 | 19:42:32.145878 | **8 s** | **TIMER** | yes |
| 5 | 20:41:34.444635 | **7 s** | **TIMER** | yes |
| 6 | 21:43:30.815136 | **4 s** | **TIMER** | yes |
| 7 | 22:43:30.255090 | **3 s** | **TIMER** | yes |

**Two of my own earlier claims were wrong**, both from quoting a single journal line instead
of reading all of them: §20.5 and EVIDENCE §5 named **20:42:24 BST as the first unattended
fire** — **19:36:58 UTC was earlier** and produced record #3, the `Persistent=true` catch-up
that fired when `just depth-timer` enabled the unit. And EVIDENCE §3 called records #1–#3
"a manual start"; **#3 is a timer fire.** Corrected in place in the evidence file.

**The planted-hole RED run used a copy.** It wrote `/tmp/g1-red-hole.jsonl` — 8 records with
a deliberate 5-hour hole — a separate file. Its timestamps were intersected against the real
log: **zero appear.** The real log's only writer is `record_once` → `append_record`; there is
no hand-edit path. So: **no synthetic or hand-edited record is present.**

**Records #1–#2 are real readings but were COPIED IN, not appended here** — byte-identical to
the rescued worktree file. That is a third category the earlier text did not have, between
"manual pull" and "timer fire". They are excluded from the depth count: **N = 8,760 starts at
record #3**, the first unattended fire. The records stay in the file (append-only, real data),
so anyone counting rows must subtract the seeded pair — recorded in EVIDENCE §6 rather than by
tagging the records, because tagging would mean rewriting an append-only artifact.

### 22.2 N: the regime claim softened, and the floor's provenance made explicit

§21.3 said 365 days buys "four quarterly splits across four volatility regimes". **That
asserts something nobody has measured.** Four calendar quarters give four splits; whether they
are four *different regimes* is a question about realised volatility that can only be
computed from data that does not exist yet. Corrected to:

> **N = 365 days buys four quarterly splits — regime diversity to be MEASURED, not assumed.**
> Once the depth series exists, compute realised volatility per quarter and report it. If the
> four quarters turn out to be one regime, the four splits buy nothing over one and N has to
> grow; that is a measurement to make, not a property to claim now.

**The 89-day floor's own provenance, which §21.3 also blurred.** The 178-bar seed-noise figure
was measured on the **price-only policy** — 3 seeds, and within-config spread exceeded the
between-config effect at that eval width. **Carrying that number over to book features is an
assumption**, not a measurement: a book-derived feature set has different seed variance, and
nothing in the corpus measures it. Book features may well need a *wider* eval slice, which
would push the floor above 89 days. **The first consumer must check this before treating 89
days as sufficient** — and if book features are noisier, the honest response is a larger
floor, not a smaller N.

So both numbers now carry their provenance: **89 days = floor, derived from a
price-only seed-noise measurement, transfer to book features assumed and unverified. 365 days
= choice, four quarterly splits, regime diversity to be measured.**

### 22.3 The double timer in the OTHER order — cannot be blocked, so it is detected

§21.4's guard fires when `just depth-timer` runs and the **system** unit is already enabled.
The lead asked the reverse: the user timer is installed and running, and someone later
enables `ob.enable` and rebuilds the host.

**That cannot be blocked, and I am not going to pretend otherwise.** The recipe is a
one-shot installer; by the time the system unit appears there is no recipe left to refuse.
Neither systemd unit is aware of the other, and there is no ordering constraint that could be
declared between a `systemd.user` unit and a `systemd.services` one — they are different
managers.

What a double fire looks like: **two records per hour, roughly 1800 s apart**, which the gap
counter classifies as **`SHORT`** — and §21.1 already established that `SHORT` outside a
bring-up burst is not benign. So the status output now says so, and names the two commands
that settle it:

```
  SHORT         : 3 interval(s) closer than 2400s (= 3600s expected / 1.5 factor)
                   a sub-hour re-fire, timer catch-up, or two manual pulls in quick succession;
                   counted separately from gaps, and NEITHER a duplicate-record count NOR a depth shortfall
                   >> 3 SHORT interval(s) AFTER the first record is not benign: if these are not your own manual
                      pulls, CHECK FOR A SECOND TIMER — `systemctl is-enabled kraken-trading-bot-order-book.timer`
                      (system) vs `systemctl --user is-active kraken-trading-bot-order-book.timer` (user). Both fire hourly.
```

The guard prevents the reachable order; this line detects the unreachable one. A consumer must
also treat `dedup-on-read` (§22.4) as mandatory rather than optional, since a double timer
produces two records in one floored hour.

### 22.4 `depth-verify`: the read-only check, and what it caught

Written, run against the real log, and **non-vacuous by mutation** — full output and the
ten-of-ten matrix in EVIDENCE §7. `VERDICT: PASS`, 7 records, 0 short of depth, dedup
`7 rows -> 4`, and **the file's hash is identical before and after** (`a7b540d1…`), which is
the part that matters: a verification pass must not be able to damage an unrecoverable
artifact. No sidecar either.

The dedup line answers the same-hour problem without touching the producer: **7 rows → 4**,
the 3 collapsed rows being exactly the 3 `SHORT` rows. Last-wins per floored hour, in the
consumer, never in the recorder.

**It caught a real schema fact on first run, and the fact was that I was wrong.** I asserted
`best_bid`/`best_ask`/`mid`/`spread` were JSON numbers; all 7 records store **decimal
strings**. Reading `depth_recorder.py:537-544` showed that is deliberate — Kraken's raw price
string kept, `spread`/`mid` formatted to 10 dp — consistent with the `Decimal`-for-money
convention used throughout this codebase. **My assertion described a schema the recorder does
not use.** Rewritten to parse decimals and check much more than a type: `best_bid`/`best_ask`
must equal the touch of `bids[0]`/`asks[0]`; `spread == ask - bid`; `mid == (bid + ask)/2`;
each side monotone **as a Decimal**, because as strings `"9.9" > "10.0"` would invert the book
and pass an ascending book as descending; `hour == floor(recorded_at)`.

The assertion that matters for the lead's depth question, isolated on a copy:

```
  short book (40<100), truncated=false   RED     40/100 short of requested 100 but truncated is false
  short book (40<100), truncated=true    GREEN   the honest version of the same book passes
```

That pair is the whole point: it separates a **silent depth reduction** from an honestly
flagged short book, which is what §8.1.4 needs.

### 22.5 Off-host copy: mechanism built, destination still owed

`just depth-backup dest` (`dest` **required, no default**) and `just depth-verify-restore
src`. Three refusals, each a way a backup could look finished while not being one: no
default destination (a default would resolve locally and solve nothing); refuses a
destination on the **same filesystem** as the source (`df --output=source` — a second copy on
one disk is not off-host); refuses an uncreatable or unwritable destination, naming the
unmounted-network-mount case. Every copy is timestamped and then **verified byte-identical
with `cmp`** before success is reported, and `depth-verify-restore` reads a copy back and
parses it.

**No destination is reachable from this host**: 0 Tailscale peers online, no named SSH host,
no NFS/CIFS/sshfs mount, no rclone. So the mechanism is in place and refusing by default, but
**the first verified restore is still owed** — it cannot be done until the lead names a
destination. Recorded as open rather than glossed: an off-host copy that has never been read
back is a hope, not a backup, and this one has not yet left the machine.

### 22.6 `Hole` removed, `§8.1` extended

`GapReport.holes` is annotated `tuple[Interval, ...]` (21.2). `class Hole` was referenced by
**nothing** — the only other hit is the phrase "Hole threshold multiplier" in a `cli.py`
help string, which is prose about gaps, not the class. Removed, in the same pass that fixed
the annotation, as the lead asked.

**`DECISION.md` §8.1 now opens with the attribution rule**, because the failure was in
explaining a failure rather than in producing one: *a claim that attributes a failure to
tooling — a formatter, a hook, the environment — is checked for the mechanism before it is
reported.* Name the tool, the hook or config, and whether it still applies; or state that no
such mechanism exists. The 2026-10-03 case is recorded there as the measured instance.

### 22.7 Off-host copy: guard hole found and fixed, first restore done — but machine loss is still open

The lead chose NAS/NFS. **No NAS is mounted and no client is installed** (`mount.nfs`,
`mount.cifs`, `sshfs`, `rclone` all absent; `/etc/fstab` has no NFS or CIFS entry), so the
real off-machine destination cannot be wired. Exercised against the best destination this
host has: `/mnt/media` (`nvme0n1`, vs the log's `sda`).

**The guard I wrote in 22.5 had a hole, found by checking instead of re-reading.** It compared
`df --output=source`, which is a **per-partition** device string. The log is on `/dev/sda5`,
and `sda` also carries `sda1` (`/boot`), `sda3` and `sda4` — so a dest on `sda3` would have
produced a *different* `df` string, passed the guard, and been the **same physical disk**:
a second copy offering zero extra durability, which is the precise failure the recipe exists
to prevent. Now compares the physical disk via `lsblk -no PKNAME`.

Worth recording *how* this was found: I first asserted the opposite — that `/mnt/data` was a
same-disk hole — from a `df` comparison alone. Then I checked `lsblk` and found `/mnt/data` is
`/dev/sdb1`, a genuinely different disk, and my own printed "proof" line was **self-contradictory**
(`/ = sda5`, `/mnt/media = nvme0n1p1` — I had captioned them the same disk). Discarded, and
the real hole found by looking at the partition layout rather than at two mount points.

Three refusals verified live:

```
  no default dest        -> "dest is REQUIRED and has no default", exit 2
  /mnt/data unwritable   -> "destination not writable", exit 1   (root-owned docker-data)
  /tmp                   -> refused: same PHYSICAL disk as the log (/dev/sda)
```

**First verified restore:**

```
$ just depth-backup /mnt/media
  off-host check: log on /dev/sda, dest on /dev/nvme0n1 — different disks
  copied -> /mnt/media/eth_usd_orderbook.jsonl.20261003T231511Z
  records: 7   bytes: 51702   verified byte-identical

$ just depth-verify-restore /mnt/media/eth_usd_orderbook.jsonl.20261003T231511Z
  schema OK | short of depth 0 | dedup 7 rows -> 4 | wrote anything: no | VERDICT: PASS
```

A real 51,702-byte file, not a link, read back **off the original path** and fully parsed. So
the copy is proven, not assumed.

**And the honest limit.** `/mnt/media` is a different *disk* but the same *machine*. A
single-disk failure no longer takes the log; **machine loss, theft, or the failure of this
host's storage still does.** The lead's stated concern is **still open** — the first copy has
left the disk, not the machine. Recorded that way rather than letting "verified restore" imply
more than it covers, and noted in EVIDENCE §9 that the recipe's "off-host copy" wording should
be read as *off-disk-or-remote, whichever you point it at*.
