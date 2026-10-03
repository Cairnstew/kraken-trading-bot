# PLAN — data-pipeline audit, Phase 6 close-out

Pass date **2026-10-02/03**. Gate verdict: **PASS** (see `VALIDATION.md`).
`HEAD = a7a9cbe`. Nothing pushed; the close-out push is the lead's.

---

## 1. What this pass was

One outcome type, one target: `IMPROVE-EXISTING`, gap **G2**, the
one-record-deep funding channel. Phases 1–5 produced `AUDIT.md`,
`RESEARCH*.md` and `DECISION.md`; Phase 6 ran the gate.

**Audited**: whether the bot now *sees* a live funding series rather than
2 distinct values on 13 of 721 bars — by running it end to end (seed →
export → train → backtest, both arms plus a baseline control), not by
reading the diff. Plus the pre-registered structural gates
(`just audit-verify` with **two different commits**, the bot suite) and a
re-derivation of every number two agents had measured differently.

---

## 2. What was built (all committed before this pass)

| phase | commit | what |
|---|---|---|
| 4B | `8cada4e` | `just funding-backfill`; corrected two factually-wrong "~8-hourly" comments (the series is **hourly**); `data.py` producer hint `&&`; `/tmp` output fix |
| 5 | `aba7b9b` | seam closed through train/backtest/paper; 8 stale "8-hourly" mentions closed; 252 lines of tests |
| — | `a7a9cbe` | F-7..F-13 dispositions recorded at `DECISION.md` §7.8 |

Sibling `kraken-funding-rates` @ `dc49847`: `historical_funding_rates()`,
`FundingHistoryRow`, `@_register("funding_history")`, `backfill`
subcommand. Keyless, 1 request, ~366 d hourly.

**Measured result**: `funding_rate` goes from **2 distinct values on
13/721 bars → 721 distinct on 721/721 bars**, at **unchanged observation
width 60**, fitted STD **×6.941**. Coverage **8,792 records**,
`2025-10-01T08:00Z → 2026-10-02T23:00Z`, measured 2026-10-02; the count
grows ~1/day because the window is recomputed each call.

---

## 3. Stated plainly: what this pass is worth, and what it is not

**It recovers 1 of 6 columns.** Kraken's `/historical-funding-rates`
carries only `funding_rate`; all 8,792 backfilled records have
`basis`, `open_interest`, `funding_rate_prediction`, `vol24h`, `bid`,
`ask` = **null**. Downstream, five columns that the single live snapshot
gave 13 real readings on now read as constant zero (`spread` included),
taking the degenerate-STD count from 3 features to 10. One real
distribution gained; five lost. That is the honest trade.

**`signal_observed` no longer distinguishes absence per-column (F-2).**
It is a per-channel OR (`data.py:812`), so it reads 1.0 on 721/721 bars
while five of six funding columns are zero-fill. `signal_observed` and
`signal_age_hours` become *correct constants* on a dense file (1.0 and
0.0) rather than diagnostics. Per-column absence is now only visible by
comparing a column's distinct-value count against the bar count — which
is exactly how §3.2 of `VALIDATION.md` reports it, and why that table is
the artefact to keep.

**Both arms are IN-SAMPLE** (`data_window.since`/`until` both null, the
shipped default; the bot warns on every run). No predictive claim is
supported by anything in this pass.

---

## 4. Deferred, and why

Unchanged by this pass. Recorded so the deferral is a decision with a
reason rather than an omission.

### G1 — `order_book_imbalance` has no producer · **NEXT SLICE**

The `microstructure` group is enabled in the shipped config but
`order_book_imbalance` is unreachable: `_add_microstructure_features`
computes it only when `bid_vol`/`ask_vol` are present, no producer emits
them, and `_SIGNAL_COLUMNS` does not list them. Meanwhile
`data["order_book"] = manager.order_book(pair, count=10)` already runs
every 60 s and is thrown away by `strategies/base.py:92`.

**Why it is the right next slice.** It is the only remaining gap that
*widens the observation* rather than deepening one column. G2 recovered a
series the agent could mostly already see; G1 adds a dimension
(order-book depth) that is structurally absent from every training run so
far. Highest remaining information gain per unit of work.

**Why it is UN-backfillable — the reason it was not a G2-shaped fix.**
G2 worked because Kraken publishes *historical* funding rates, so an
existing endpoint could be replayed ~366 days into the past. **Kraken's
order book is a live snapshot only.** There is no historical depth
endpoint, and a depth log cannot be reconstructed after the fact: the
`count=10` snapshot at time *t* does not encode what the book looked like
at *t−1h*. So G1 is **forward-only by physics, not by choice** — it needs
a recorder plus a timer plus a widening of `_SIGNAL_COLUMNS`, and its
first bars only exist from the day it is switched on. Running it in
parallel with the other work is the only way to have any depth at all.
It also carries the §14 hazard in a sharper form: a forward-only file
means the *early* training bars of every future run are depth-absent, so
the recorder must be running before the next G1 slice is worth measuring.

### G3 — no retry / backoff / rate-limit handling
A partial page-loop failure discards every candle already collected. A
robustness fix on the fetch path; orthogonal to signal depth, and it
touches the same function G1's recorder would sit beside, so sequence it
*after* G1 to avoid two edits to one path in one pass.

### G4 — thin historical depth behind `market_data_store: null`
Still fully open, and worth being precise about what this pass did
**not** fix: the funding channel is now ~366 days deep, but the **price**
frame is still the ~721-bar live REST ceiling, because the pass kept
`market_data_store: null` deliberately (live-fetch leg). Deep funding on a
721-bar price frame is still a 721-bar episode. G4 is the gap between
"the signal is deep" and "the episode is long", and it remains the
binding constraint on episode length.

### G5 — dead strategy machinery + a wrong bar-interval comment
Cosmetic/low-risk; a clean standalone slice.

### G6 — shipped config trains and backtests at zero fees and zero slippage
Both backtest arms in this pass ran at `fee=0.0000% slip=0.0000%`, so
their returns are gross. Every return figure in `VALIDATION.md` inherits
that. Do not quote one as achievable.

### G7 — whole data categories with zero presence anywhere
Needs its own scoping pass before it can be ranked.

---

## 5. Findings this pass settled

**F-9 / C8 — resolved (frame mismatch, exact arithmetic).** 684 and 0 are
the same pipeline measured on two frames. `first_tradable_index = 24`, so
the tradable slice is 697 bars; `697 − 13` finite `spread` = **684**, and
on that slice `spread` is the only non-finite column. The integrator's
**0** is on `observed` (`compute().ffill().fillna(0.0)`) — the frame the
policy sees, the frame `width_check` hashes, the frame `export-data`
writes. Both arms: 0. The builder's "identical in baseline and target" is
**wrong** (684 vs 697 on the same slice).

**F-11 — resolved (one arm, two frames; right edge named).** 13 =
`computed`, 24 = `observed`, same file. The record sits 23 bars back from
the frame's right edge, so its 12 readings span offsets −23…−11 and
`ffill` carries them over the last 11 bars: 13 + 11 = **24**. The
integrator's "byte-identical both arms" is **wrong** — the backfilled arm
is 0/721, because its file has no `bid`/`ask` anywhere.

**F-8 — confirmed by measurement; assert nothing.** 52 (all-null) / 60
(shipped). C1's 57 and C2's 49 are 3 low because the fixture lacked
`vwap`/`count`. F-13's correction (52 is the all-null width) is confirmed.

**F-10 — confirmed.** `signal_age_hours` max **12.0** on the shipped
live-snapshot shape (it is `ages.max(axis=1)`, the stalest column, and
the bound itself is the max); 0.0 on a dense file. C12's "≤ 2.0" is
unachievable as written, which **confirms F-6 / DEV-2**.

**New, for the lead to record in `DECISION.md` §7.8** (I am read-only on
source; `just audit-findings` will refuse without them):

- **F-14** — the two new characterisation tests are **vacuous for
  `069a826..HEAD`**: both pass with `data.py`/`features.py` reverted to
  the pre-registration commit, because `executable_ast` is byte-equal on
  both files across the whole pass. Not a defect — the pass made no
  executable change — but they must not be read as regression coverage of
  this diff. The genuinely non-vacuous test is the producer-hint pin
  (`assert "#" not in err.producer`), proven by reverting only that
  string and watching it fail with the `#` symptom.
- **F-15** — `width_check.py` calls `compute()` but never
  `add_derived_ohlcv_features`, so a raw OHLCV parquet reports **49**
  where every shipped state is 52 or 60. My own `verify --frame` receipt
  printed it. Same class as F-8, but a **tool** trap rather than a doc
  trap.

---

## 6. Concrete next steps

1. **Record F-14 and F-15** in `DECISION.md` §7.8 (lead), then
   `just audit-findings` and the ordered close-out push.
2. **Annotate `width_check.py`'s docstring** (F-15) so a `--frame` receipt
   is never mistaken for a shipped-width record. Docstring-only, invisible
   to the `executable-ast` guard.
3. **Start the G1 depth recorder now**, even before writing the feature
   work — it is forward-only, so every hour it is not running is an hour
   of depth that cannot be recovered. Budget the widening of
   `_SIGNAL_COLUMNS` for `bid_vol`/`ask_vol` as its own commit.
4. **Decide G6 before any return figure is quoted.** Fees and slippage at
   zero make every number in `VALIDATION.md` gross.
5. **Pin `data_window.since`/`until`** for the first genuinely
   out-of-sample measurement. Both arms here are IN-SAMPLE by shipped
   default and the bot says so on every run.
6. **If a report needs a bar count, date it.** 8,792 measured 2026-10-02,
   growing ~1/day. This repo has shipped an undated count as an error
   twice.
7. **G4 remains the binding constraint on episode length** — deep funding
   does not lengthen the price frame. Consider it alongside G1's
   recorder, since both want the store populated on a schedule.

---

## 7. Reproducing this gate

```
nix develop --command bash -c "python tools/audit_checks.py verify --prereg 069a826 --since 8cada4e"
nix develop --command bash -c "python -m pytest -q"          # 457 passed
just funding-backfill ETH/USD /tmp/rev/signals/eth_usd_funding.jsonl
```

The seeding command is the **production path** — the same
`nix run ~/Projects/kraken-funding-rates#kraken-funding-rates --` the
hourly timer's `ExecStart` and the justfile recipe use. My brief
specified a dev-shell `PATH` lookup, which cannot work (`flake.nix` pins
only `kraken-python` and `kraken-market-data`); that is a correction of
the brief, not a deviation, and it produces the artifact the timer
writes. Assert the sibling is clean at `dc49847` before seeding — the
choice of a non-flake-input sibling rests on the tree you seed from being
the committed one.

All scratch (models, stores, exports, frames) under `/tmp/rev/`. Never
committed. No real API key used — the funding endpoint is keyless.

---

## 8. NEXT PASS — ordered, and the first item is time-sensitive

### 8.0 ⚑ A DECISION THE LEAD OWES — not for the team to pick up

**`configs/default.yaml`'s `data_window.since` / `until: null`.** This is
deliberately left at the top of the next checkpoint as an open decision, not
a task. Every arm in this pass's integration test was **IN-SAMPLE** because
those two are null on the shipped default, which is why the return and
Sharpe rows could not be read as an effect. Related machinery that is
already present but unused: `data.py:1298-1302` (`.. todo::` — honour
`since`/`until` from config) and `data_window.eval_split`, which the
matrix-harness pass showed is **inert unless the window is pinned**.

Deciding it changes what "an effect" can even mean for this bot, so it
should be settled deliberately, with the store's available depth in hand —
**after** G4 — rather than picked up opportunistically.

**Status: the lead's decision, to be confirmed at the next checkpoint.
NOTHING HAS BEEN CHANGED.** The lead's stated lean is to **keep the
shipped default `null`**, on the grounds that pinning the window without a
store yields **0 train and 0 eval bars** — i.e. pinning is not a free
win, it is a way to make the bot refuse to run. Under that lean:

- unpinned runs stay **labelled IN-SAMPLE**, exactly as this pass's arms
  were (`VALIDATION.md` §2, and the bot's own warning on every run);
- the **documented OOS path** becomes `configs/deep-history.example.yaml`
  plus the deep-history `just` recipes — the place where a pinned window is
  paired with the store that can actually fill it.

Recorded here so the next checkpoint starts from the reasoning rather than
re-deriving it. The open question that lean leaves, to settle with it: if
the shipped default stays unpinned, **every out-of-sample number anyone
quotes requires deliberately choosing the example config**, so the OOS path
needs to be prominent enough that a reader cannot mistake a default run for
an OOS one. That is a docs/discoverability obligation attached to the
decision, not a separate task.

*(Typo to fix if this file is next edited: "VALIDATION.md" above should
read "VALIDATION.md".)*

### 8.1 FIRST ACTION — start the G1 recorder, and do it before any feature work

**The depth is unrecoverable by construction.** Kraken's order book is a
live snapshot with **no historical endpoint** (Kraken's public archive is
OHLCVT-only; Binance Vision `bookDepth` was falsified directly against the
S3 bucket with `KeyCount=0` against a klines control that returned 2), and a
`count=10` snapshot at *t* does not encode *t−1h*. **Every day without the
recorder is data that cannot be recovered later.** This is the only
time-critical item in the whole plan, which is why it goes first and why
nothing else may precede it.

**Keep the brief small.** The recorder is the deliverable; the feature is
NOT. Do not touch `rl/features.py`, `_SIGNAL_COLUMNS`,
`_SIGNAL_BUILDER_INPUT_COLUMNS`, or any observation width in this slice.

1. **Record snapshots.** `kraken-python` already wraps the producer —
   `manager.order_book(pair, count)` → `client.depth()` → keyless
   `/0/public/Depth`, plus the keyless `book` channel on `ws.kraken.com/v2`.
   **No new dependency**: `cryptofeed` is AGPL-3.0-or-later and needs
   Python ≥3.13; `ccxt` is MIT but unnecessary. Do not build a
   `kraken-order-book` sibling that re-wraps an existing sibling — the
   house-style answer is a small recorder inside the funding/producer
   family, following the "log, not state" convention the JSONL seam
   already uses.
2. **Append-only, with your own timestamps.** A book snapshot carries **no
   timestamp of its own** — each level's timestamp is that order's placement
   time. The recorder must stamp its own clock, and must **record the depth
   used**, because depth is not comparable across a `count` change.
3. **Include a gap counter**, given what F-6 showed about silent holes: a
   run that stops for a week and appends happily afterwards is
   indistinguishable from a run that never stopped. Report expected-vs-actual
   intervals alongside the data, and make a hole visible in the artifact
   rather than only in stdout.
4. **Prove the gap counter can go RED before calling this slice done.** Feed
   it a **deliberately skipped interval** and paste the verbatim report of the
   hole into the evidence file. This is the guard-must-fire-once rule in the
   command file's *CHECKPOINT RULE* section: a counter that has only ever
   printed `0 gaps` is indistinguishable from a counter that cannot count.
   The red run is part of the deliverable, not a follow-up.
5. **Do not consume the recorded data in this pass.** No `_SIGNAL_COLUMNS`
   change, no observation-width change, no merge into the observation. The
   recorder runs, accumulates, and reports its gap count. Wiring the feature
   is a later slice, once there is history worth wiring — which is also why
   it must not be attempted before the recorder has been running for a while.

#### 8.1.1 ⚠️ IT MUST BE SCHEDULED — a recorder that is not running loses the same data as no recorder

Writing the recorder is not the deliverable. **Depth only accumulates if
something runs it unattended**, and the unrecoverability above makes a
recorder that merely *exists* worth exactly nothing — the snapshots it was
built to capture are gone either way. So the scheduling is in scope for this
slice, not deferred as "ops follow-up".

- **How it runs: a systemd timer.** The repo already has the pattern to copy —
  `systemd/kraken-trading-bot-funding.service{,.in}` +
  `kraken-trading-bot-funding.timer` pulls funding hourly, and
  `nix/module.nix` installs the unit. The recorder's unit should follow the
  same shape. **Mind the option-namespace trap**: `nix/module.nix` is a
  **NixOS** module, so `systemd.user.*` (a home-manager option) will not
  evaluate there — the funding pass had to ship a plain `systemd/user` unit
  for exactly this reason. Check which namespace the recorder's timer needs
  before writing it.
- **The timer's `ExecStart` is the production path**, so the recorder must be
  runnable exactly the way the unit invokes it. The funding precedent is
  `nix run <sibling>#<pkg> -- <args>` — a sibling that is **not** a flake
  input, so it builds from its working tree (`src = ./.`) and an edit is live
  with no lock bump. Say explicitly in the brief which of the two shapes the
  recorder uses, because "it works when I run it by hand" and "the timer runs
  the committed tree" are different claims and only one of them accumulates
  data.
- **First-checkpoint evidence: at least TWO real snapshots with DISTINCT
  timestamps**, taken from a live call, showing the recorder stamps its own
  clock (a book snapshot has no timestamp of its own — each level's timestamp
  is that order's placement time, so two snapshots must differ by the
  interval, not share one). Also show the unit is *installed and enabled*
  (`systemctl --user list-timers`), because an enabled-but-never-fired timer
  is the same failure as no timer.
- **Cadence floor.** Poll no finer than hourly unless the hour-floor in
  `data.py:757` changes — otherwise you pay 60× the calls to keep one row per
  hour. Record the depth used in the data, because depth is not comparable
  across a `count` change.

#### 8.1.2 What this slice must NOT do

The no-consume constraint is load-bearing, not politeness: the recorder's
value is that it runs **unattended for weeks before** anything depends on its
output. Consuming it in the same pass creates a reason to change its schema
the first time an awkward column shows up, and the history already written
becomes incompatible with itself. Write data; read nothing.

### 8.2 Then, in order

- **G4 — deepen the price frame.** Now formally recorded as **F-18**: funding
  is 366 d deep while the price frame is 721 bars (~30 days), so funding has
  ~11× more depth than the episode it is merged into. Funding depth cannot
  buy episode length until the price side is deepened. G1 and G4 are coupled
  for the same reason — recorder depth is bounded by whatever episode length
  the store eventually allows.
- **G3 — retry / backoff / rate-limit handling.** Still zero. Re-read F-15's
  note: `tools/width_check.py` fingerprints a raw parquet at 49 where shipped
  states compose 52/60 (logged proposal, deliberately unchanged, because
  auto-calling the derived step would invalidate the committed CAND-3a
  fingerprints).
- **G5 / G6 / G7** — untouched; G6 in particular means the default run is
  still frictionless, so every in-sample number in this file is flattered.

### 8.3 Carry into the next architect

**Re-derive every constant and every direction from the artifact before a
gate is built on it.** This pass produced three sourced, plausible, wrong
claims — an inverted dedup direction, width constants 3 low, and two
findings both agents measured wrong. Mark each figure in a decision as
*measured* or *inferred from a fixture*.
