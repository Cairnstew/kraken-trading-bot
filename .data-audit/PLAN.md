# PLAN — data-pipeline audit pass 2026-10-02 (`IMPROVE-EXISTING`, gap **G1**)

**Architect:** `architect`, team `data-audit-1002`. **Validation:** `reviewer`
(`VALIDATION.md` carries the **GATE verdict: NEEDS_FIX**).
**Repo state:** `kraken-trading-bot` @ `3f9708e`; sibling `kraken-funding-rates` @ `935dfc0`.

This supersedes the 2026-10-01 revision, which planned and gated a *different* pass (the
sparse-exogenous `first_tradable_index` fix). That work is done and stays done; the numbers
below corroborate it — that pass measured width **60** and **697 bars**, and this pass
measured width **60** and **697 bars** again, independently, on a different day.

---

## 1. What was audited

The claim under audit was the one the whole repo's usefulness rested on:

> *"Exogenous signals ride JSONL seams: news, funding/basis, social"* — **TRUE as code,
> FALSE in practice.** `AUDIT.md:154`

Three seams exist, all three are well built, and **none was populated**: the two others
were `null`, and the third pointed at a path under a `signals/` directory that did not
exist — and was **structurally unreadable even once it did**.

Gap **G1** was that structural unreadability, and it was worse than a dropped column. The
skipped merge does not only lose the funding columns; it loses `signal_observed` and
`signal_age_hours` too, because the same seam writes the freshness pair. **The diagnostic
designed to make the absence auditable inside the observation was itself absent.** Width
as-shipped (52) was identical to width-with-the-broken-path-configured (52), so
`check_feature_width` — which checks width, not NaN — could not see it. Nothing downstream
flagged it, which is why it survived.

## 2. What was built (and it worked)

| Commit | Change |
|---|---|
| `444fe1f` | audit + research artifacts |
| `3e0186f` | DECISION committed for worktree teammates |
| `8569c08` | **G1** — `_resolve_config_path()` on all five path sites; `_SIGNAL_CHANNELS` pairs every key with its producer; `SignalFileNotFoundError` |
| `3f9708e` | matrix: name a missing signal file instead of reporting `process_failed` |
| `935dfc0` *(sibling)* | funding records now emit `ticker` = the **spot** pair |

Five design decisions in `8569c08` are worth keeping:

1. **One helper for all five path sites** — `data.py` ×2, `train.py` ×1, `export.py` ×2.
   The second instance of the same tilde bug (`data.py:1054`, `market_data_store`) was
   silently blocking that path too; fixing it once fixes both.
2. **Off is now distinct from broken.** A null key is silent, so a fresh clone with no
   sibling repos still runs on price alone. A *set* key that cannot be used is a
   configuration error. Verified both directions (`VALIDATION.md` §8.1, §8.2).
3. **A log with records but no overlap stays a WARNING.** The expected state of a young
   forward-only log is not a fault; raising there would train the feature off.
4. **Refusals name the fix.** Key, raw `~`-spelled value, expanded path, producer command —
   verified present 2× each through the real CLI.
5. **One channel table for both read legs.** `_SIGNAL_CHANNELS` (`data.py:118`) makes the
   live-fetch and store-backed legs structurally unable to drift about which key feeds which
   merge or how a refusal reads.

And it fixed a **test** that had been lying: `test named 'config points at a REAL funding
file'` only asserted the string was non-empty and ended `.jsonl` — it passed for
`/definitely/not/here/nope.jsonl`, which is exactly why this shipped.

## 3. What is BROKEN — needs a scoped fix before this pass closes

### 3.1 The blocking item

**`tools/model_matrix.py:1726` swallows the refusal it was written to explain.**

```python
stderr_tail = (err_tr + err_bt).strip().splitlines()[-4:]
```

Train stderr and backtest stderr are concatenated and then truncated to the last 4 lines of
the whole. When `train` fails, `backtest` also runs and also fails, and *its* output — the
downstream consequence, "No trained model" — is what lands in the tail. The train leg's
refusal, the one message that names the fix, is discarded before
`classify_process_failure` ever sees it. Measured: the refusal is at line 8 of a 10-line
`err_tr`, 19 lines survive concatenation, the slice keeps 4, and it does not survive.

Live result: `INVALID: process_failed`, not `signal_file_not_found`.

**Why it matters in scale, not in principle.** `configs/default.yaml` ships
`funding_features_file` non-null. So on any checkout without that file — a fresh clone, a
CI runner, the second machine in DECISION §7 — *every cell of a matrix* fails the same way,
and the one opaque code explains none of them. The control run proves the shape: the
identical cell with the record present is `0 invalid, 0 errored`. One line decides whether
the single fix that would rescue the whole grid is delivered or swallowed.

**Why the test missed it.** `3f9708e`'s
`test_a_refused_signal_file_is_named_and_not_mistaken_for_another_cause` feeds
`classify_process_failure(_real_errored(REAL_SIGNAL_FILE_REFUSED))` — a synthetic
`stderr_tail` built directly. It never runs `cmd_run`, so it never builds the tail that
broke. The commit's reasoning ("matching is on the message the real CLI prints") is right
about matching and wrong about **capture**. This is the same class of error as the one
`8569c08` fixed in the source: *a test that pins the intended value without exercising the
path that produces it.*

**Fix (one line, verified sufficient):** take the tail **per leg**.

```python
stderr_tail = (err_tr.strip().splitlines()[-4:] + err_bt.strip().splitlines()[-2:])
```

With the refusal present in the tail, `classify_process_failure` returns
`['process_failed', 'signal_file_not_found']` — confirmed live.

**And the regression test must pin the capture, not just the classifier.** Assert that
`err_tr`'s content survives into `record["stderr_tail"]`, ideally by building a record the
way `cmd_run` builds it from a real train-fails/backtest-fails pair. A second synthetic
`stderr_tail` would leave this exactly as findable as it was.

Not done here — out of scope for a reviewer.

### 3.2 Non-blocking, worth folding in

- **`backtest --config` must agree with the model's recorded `action_space`.** `train
  --action-space discrete` records the override in the model's `config.yaml`, but the YAML
  you hand `backtest` still says `continuous`, and `action_space` is one of the six keys a
  backtest config *does* honour — so the first replay fails with `ActionSpaceMismatchError`
  on a model that is perfectly fine. Documented in the `model-matrix` skill.
- **Refusal wording asymmetry.** `SignalTickerMismatchError` renders `found` in canonical
  form (`ETHUSD`) against a request in pair form (`SOL/USD`). Correct, mildly confusing.
- **`_SIGNAL_COLUMNS` is 19 entries.** RESEARCH-1 reports "3 / 18" and "10 / 18". Anyone
  re-deriving a denominator should count the tuple.

## 4. Known caveats

1. **A fresh clone now needs `just funding-pull` before it can train.** Previously it
   trained silently on 52 features; now it stops and tells you to run the producer. This is
   the intended trade — but it is a **behaviour change on the shipped default**, and any CI
   runner, container or second machine that trains from `configs/default.yaml` will now
   fail until it either runs the producer or sets the key to `null`. It failed my own
   matrix run for exactly this reason, which is the best possible evidence that it works.
   DECISION §7 already lists seeding the file as a documented prerequisite.
2. **`configs/default.yaml` is only valid for one ticker.** It ships a single, ETH-named
   `funding_features_file`. Combined with `signal_require_ticker: true` now genuinely
   filtering (because the record carries `ticker` = `ETH/USD`), a second ticker **raises**
   `SignalTickerMismatchError` rather than silently merging — the intended direction, and
   strictly better than the old BTC-onto-ETH merge, but user-visible. A per-ticker
   `funding_features_file: null` default (opted into per model) would make the shipped
   config valid everywhere.
3. **A loud error does not make the signal any better.** It makes its **absence visible**.
   With a 1-record log, `signal_observed` is true on 13 of 240 bars. This pass establishes
   that the observation *contains* the signal — not that the signal *predicts*. Nothing
   here should be quoted as evidence of edge. And `excess_return` was deliberately not
   asserted, because per G4 it is not resolvable at this design (detection floor 2.82 pp
   against a 0.25–0.8 pp effect); a gate keyed on it is a coin flip.
4. **`--pages` buys no depth.** 2, 4 and 8 pages all return 721 bars at 60 min — Kraken's
   REST ceiling is ~720 candles per pair/interval regardless of page count. Pre-existing
   and documented in `configs/matrix.example.yaml`; re-measured here. **No gate may assert
   depth from a page count.**
5. **`market_data_store` is fixed but unexercised.** `data.py:1054` was the second instance
   of the tilde bug; `_resolve_config_path` fixed it, and the shipped `null` keeps its
   pass-through behaviour. But it was validated by call-site inspection, not against a
   populated store. **Needs a dedicated check the moment a real store exists** — that is
   also the first thing G3 depends on.
6. **Single seed.** Everything here is seed 42. `configs/matrix.example.yaml` records PPO
   swinging 576 → 374 trades on an identical config from the seed alone, so `num_trades:
   350` is a count, not a stable quantity. Gates should assert `> 0`, never a value.
7. **Feature width is 60 *today*.** It is a function of `feature_windows` and
   `feature_groups` as configured. Re-derive from the artifact; never hardcode — which is
   precisely why 49/52/59/60/67 were all "true" in different documents.

## 5. Deferred — the other gaps, and what each would take next

All eight were audited in `AUDIT.md`; RESEARCH-1/2/3 corrected it six times. G1 was chosen
as the smallest honest change with the highest leverage. What each remaining gap costs:

### Runner-up #1 — **G4: make measurement power a number, not a literal**

`tools/model_matrix.py:230` hard-codes `MIN_REPLICATES_FOR_A_CLAIM = 3` and never compares
it against any **observed** spread. The harness documents in its own words why a fixed 3
isn't enough (576 → 374 trades on an identical config).

**Next:** ~30–60 lines, **no new runs**. Compute an observed spread per config group from
the records already in the JSONL, compare it to the effect being claimed, and turn "3 seeds"
into "the seeds you have resolve this effect". RESEARCH-3 §8.3 sizes it. **No gate can be
keyed on `excess_return` until this lands** — which is why the G1 gate was written to
assert names, widths and bar counts instead. *This is the highest leverage per line left,
and it is the prerequisite for ever answering "is the signal worth trading".*

### Runner-up #2 — **G3: lift the ~721-bar cap**

Swap `kraken-deep-history`'s seeder to Kraken's own OHLCVT bulk archive (free, public, no
key). **Unblocked by this pass at zero extra cost** — `data.py:1054` was the last blocker
and `8569c08` fixed it as a side effect of the shared helper.

**Next:** a research pass to confirm archive coverage and rate limits, then a seeder swap.
Caveat from RESEARCH-3: **G5 must land with it, not after it** (below). And note DECISION's
correction — σ(L) ∝ √L, so depth buys σ *measurement*, not σ reduction; a longer window is
a **noisier** read. Depth is worth having for resolution, not for certainty.

### Runner-up #3 — **G2-R1: `vwap_close_gap_zscore_20` + `count_per_range`**

Two derived columns from data the OHLCV frame **already carries** (`vwap`, `count`) — no
new source, no new network dependency. **Next:** ~2 columns in `features.py` plus a width
test. Cheap; do it in the same pass as anything else that touches the feature pipeline,
because every width change invalidates every artifact in `models/`.

### Runner-up #4 — **G2-R2: a `kraken-microstructure` sibling** (`NEW-DATA-SOURCE`)

`/public/Depth` never was, and `/public/Spread` is **not backfillable** (RESEARCH-2 §1.2 —
the audit's Depth-vs-Spread contrast is half wrong; the real asymmetry is Depth vs Trades).
Building it before G3's venue hazard is fixed would repeat the mistake, and this pass's own
§4.4 is the cautionary example: every cell of a matrix fails the same way for one missing
file. **Do not build until G3 + G5 land.**

### The rest

- **G5 — retry/backoff/partial-failure on every fetch path.** `data._page_candles` is a bare
  loop with no `try`, no backoff, no resumption; one transient 5xx aborts `train`,
  `backtest` and `paper-trade` mid-read. The *sibling* producers already document
  retry/backoff-on-429 — this repo is the weaker half of the pipeline. RESEARCH-3's stated
  **precondition** for any G3 push-down: it must land *with* G3, not after.
- **G6 — the exogenous channel is forward-only.** The funding service file says so itself:
  "it is a log, not state". The exogenous channels therefore have **no history before the
  log's start**, which is the structural reason no Phase 6 gate for G3/G4 can be written
  yet. Closing it means a backfill producer, i.e. work in the sibling repos.
- **G7 — `--ticker` is a blind `_`→`/` substitution.** `USD_SOL` produces
  `Unknown Kraken pair: 'USD_SOL'` from Kraken instead of a config error naming base/quote.
  Confirmed again this pass (`ETH_USD` works, `USD_SOL` does not). **Next:** validate
  against `AssetPairs` before the first fetch and name the reversal in the error.
- **G8 — walk-forward is unreachable.** `reset(options=...)` is ceremonial ("Unused;
  reserved for future start-bar overrides"), and three episode-slicing mechanisms coexist.
  Without walk-forward, σ_win is not measurable at all, which is the other half of why G4
  is the runner-up.

## 6. Next steps, in order

1. **Dispatch the §3.1 fix** — one line in `tools/model_matrix.py:1726`, plus a test that
   pins the *capture*. Re-run the two matrix specs in `VALIDATION.md` §8.3 and require
   `signal_file_not_found` on the failing cell and `0 invalid, 0 errored` on the control.
   This is the only thing standing between this pass and **PASS**.
2. **Add the §3.2 skill note** on `backtest --config` vs. recorded `action_space`, and
   correct the `model-matrix` skill's invalid-cell table with the capture lesson.
3. **Decide the fresh-clone posture** (§4.1): either ship `funding_features_file: null` as
   the default and document `just funding-pull` as opt-in, or keep the loud default and
   accept that every non-ETH environment must act. The current loud default is defensible
   for a personal repo and wrong for CI. *Your call — I would keep it loud and add the file
   to any CI setup explicitly.*
4. **Exercise `market_data_store` end to end** (§4.5) the moment a real store exists. That
   is the last unverified path this pass touched, and it is G3's entry point.
5. **Then G4** — it is the only gap that changes what any future gate can assert.
6. **Then G5 + G3 together**, never G3 alone.

## 7. What a future gate may and may not assert

Derived from this pass, so it does not have to be rediscovered:

| assert | verdict |
|---|---|
| `n_features` / `n_bars` / `num_trades` **as counts** | **yes** — `> 1`, `> 0` |
| the 7 names **by name** in `normalization.npz` | **yes** — and null the key to prove it |
| `signal_observed` **present** and `.any()` | **yes** — never `== 0.0`, which asserts the bug |
| `signal_age_hours` present | **yes** |
| null-key train returns rc=0 **and** logs no signal word | **yes** — the false-failure guard |
| unresolvable key names key + raw + expanded + producer | **yes** |
| that cell classifies as `signal_file_not_found` | **yes** — *currently false* (§3.1) |
| `n_bars` consistent with the span | **yes** — 697 = 721 − 24 warm-up; `--pages` is not depth |
| width equals a hardcoded number | **no** — re-derive; 49/52/59/60/67 were all "true" |
| depth implied by `--pages` | **no** — 2/4/8 pages all give 721 bars |
| `excess_return` sign or magnitude | **no** — G4 detection floor 2.82 pp vs a 0.25–0.8 pp effect |
| a specific `num_trades` value | **no** — seed swing is 576 → 374 |