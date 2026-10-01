# Validation — audit-pipeline IMPROVE-EXISTING pass, 2026-10-01 (RE-VERIFICATION)

Base: `a39e184` + `28fcfb0` + `eb3771e` + `8f6ab7b` + `b45a7a4` (master `b45a7a4`).

This supersedes the prior gate verdict in this file. That gate returned **NEEDS_FIX** on a
silent defect (§2.4 of the previous revision); the fix is `b45a7a4`. Everything below was
re-measured on the post-fix tree — nothing is carried over from the pre-fix run.

## 0. GATE VERDICT: **PASS**

All six criteria pass. The defect is fixed, the fix is **non-vacuous** (§3 — the new test
fails with exactly the window-consumed symptom when the source change alone is reverted),
and nothing the prior gate passed has regressed.

---

## 1. Suite validation

| Check | Command | Result |
|---|---|---|
| pytest | `nix develop --command bash -c "python -m pytest tests/ -q"` | **147 passed, 0 failed** (20 warnings, 17.56s) |
| flake | `nix develop --command bash -c "nix flake check --no-build"` | **all checks passed!** (ran once) |

147 is the exact count on `b45a7a4` (143 before the fix, +4 regression tests). No test was
modified, skipped or weakened to produce this number; §3 is the proof of the opposite.

---

## 2. The decisive check — the gate scenario, reproduced live

Real keyless funding file, fresh pull, genuine sparse shape:

```
$ nix run ~/Projects/kraken-funding-rates#kraken-funding-rates -- \
    pull --pair ETH/USD --output /tmp/rv2/eth_usd_funding.jsonl --append
Wrote 1 funding records to /tmp/rv2/eth_usd_funding.jsonl
```

The file did not exist beforehand, so this is a **1-record** file produced by a real pull —
not a trimmed one. Both legs: `--pages 2` (721 bars), `--timesteps 3000`, `--seed 42`,
discrete actions, ETH/USD, scratch config + `--models-root` under `/tmp/rv2/`.

### 2.1 Before / after, both measured here

**Before** — the same scenario on a scratch copy of the tree with ONLY
`rl/features.py` + `rl/environment.py` + `rl/export.py` reverted to `b45a7a4^`:

```
Training PPO on 721 bars of ETH_USD (60 obs features, 3000 timesteps)
Backtest ETH_USD/rv2_prefix: 1 steps, return=0.00% max_dd=0.00% trades=0 win=0.00%
```

**After** — the committed tree, same budget:

```
Training PPO on 721 bars of ETH_USD (60 obs features, 3000 timesteps)
Backtest ETH_USD/rv2_funding: 697 steps, return=4.35% max_dd=4.93% trades=374 win=46.07%
```

The defect reproduces exactly as recorded and is gone: **1 bar / 0 trades → 697 bars / 374
trades**, width 60 on both sides, `0.9667 = 697/721 > 0.9`. Both bar count and trade count
pass the criterion. (The fix commit's message claims 576 trades; I measured 374. PPO is
stochastic across runs — see §5.1 — and the gate is on bars and trades > 0, both of which
hold.)

### 2.2 Direct environment probe (the same numbers, without PPO in the way)

`TradingEnvironment` built from the live read path, funding-backed vs not:

```
FUNDING-BACKED   n_bars=721 width=60 _start_index=24 replayable=697 ratio=0.9667 passes=True
   spread non-NaN 1/721 | signal_observed sum 1.0 | signal_age_hours max 0.0
NULL-FUNDING     n_bars=721 width=52 _start_index=24 replayable=697 ratio=0.9667 passes=True
NO-NEW-INPUTS    width=49 vwap_dev=False spread=False _start_index=24 replayable=697
```

Same probe on the reverted tree, so the mechanism is attributable and not incidental:

```
FUNDING-BACKED   n_bars=721 width=60 _start_index=720 replayable=1 ratio=0.0014 passes=False
NULL-FUNDING     n_bars=721 width=52 _start_index=24  replayable=697 ratio=0.9667 passes=True
NO-NEW-INPUTS    width=49 _start_index=24 replayable=697
```

Note the null-funding and no-new-input legs are **unchanged by the fix** (24 / 697 in both
trees). Only the sparsely-covered exogenous leg moved, which is what §4.5 requires.

The exported `warmup` flag now agrees with the environment instead of contradicting it —
previously the export claimed 720 warm-up rows while the environment traded 1 bar:

```
$ kraken-trading-bot export-data --ticker ETH_USD --pages 2 --output /tmp/rv2/funding_export.csv
Built export frame: 721 bars, 72 columns (60 features, 2 signal)
warmup rows flagged True: 24
```

---

## 3. NON-VACUITY of the new tests — the part that matters

`test_sparse_funding_coverage_does_not_consume_the_window` was run against a scratch copy
(`/tmp/rv2/scratch`) whose source is at `b45a7a4^` for the three files the fix touched
(`rl/features.py`, `rl/environment.py`, `rl/export.py`) while the tests are at `b45a7a4`.
Only the source was reverted. Confirmed the scratch tree really is pre-fix
(`hasattr(features, "first_tradable_index")` → `False`).

```
$ cd /tmp/rv2/scratch && nix develop --command bash -c \
    "python -m pytest tests/test_rl_environment.py::test_sparse_funding_coverage_does_not_consume_the_window -q"
```

It **fails**, with the window-consumed symptom and the exact message:

```
>       assert replayable > 0.9 * env.n_bars, (
            f"a one-record funding file left only {replayable} of {env.n_bars} "
            f"bars tradable (start index {env._start_index})"
        )
E       AssertionError: a one-record funding file left only 1 of 721 bars tradable (start index 720)
E       assert 1 > (0.9 * 721)
E        +  where 721 = <TradingEnvironment object>.n_bars
1 failed in 1.07s
```

With the three source files restored, the same four tests pass:

```
$ python -m pytest tests/test_rl_environment.py -q -k 'sparse or null_funding_start or presence_gating_survives'
4 passed, 26 deselected in 0.83s
```

So the test pins the fix, it does not merely restate the fixture. The 143-test suite missed
this defect because every fixture populated funding on every row; the new fixture builds the
sparse shape (721 bars, one funding record) that the shipped configuration actually
produces, and it asserts preconditions (`spread.notna().sum() == 1`,
`signal_observed.sum() == 1.0`) so it cannot quietly stop being that shape.

---

## 4. No regression of what the prior gate passed

### 4.1 Widths — 49 / 52 / 60, all three reproduced

| Inputs present | Width | How measured now |
|---|---|---|
| No OHLCV `vwap`/`count`, no funding file | **49** | live read with `vwap`, `count` and the three derived columns dropped; `vwap_dev` absent, `spread` absent |
| `vwap`/`count` present, no funding file | **52** | train log on the null-funding config |
| Funding file present (what `configs/default.yaml` ships) | **60** | train log on the funding config |

52 → 60 is **+8**, confirmed by set difference:

```
observation columns ONLY with funding (+8):
  ['basis', 'funding_rate', 'funding_rate_prediction', 'open_interest',
   'signal_age_hours', 'signal_observed', 'spread', 'vol24h']
```

### 4.2 All six names reach the observation — three sinks

| The six new columns | export CSV (721 bars) | `normalization.npz` `feature_names` | train log |
|---|---|---|---|
| `vwap_dev` | present | present | in the 60 |
| `trade_count_zscore_20` | present | present | in the 60 |
| `volume_per_trade` | present | present | in the 60 |
| `funding_rate_prediction` | present | present | in the 60 |
| `vol24h` | present | present | in the 60 |
| `spread` | present | present | in the 60 |

```
funding artifact feature_names: 60 entries
{'vwap_dev': True, 'trade_count_zscore_20': True, 'volume_per_trade': True,
 'funding_rate_prediction': True, 'vol24h': True, 'spread': True}
baseline artifact feature_names: 52 entries
"bid" in observation: False   "ask" in observation: False
```

Non-degeneracy in the post-fix funding-backed export:

```
vwap_dev                nunique=721  min=-0.0113278 max=0.0251535   nonzero 721/721
trade_count_zscore_20   nunique=703  min=-2.22985    max=3.95118     nonzero 702/721
volume_per_trade        nunique=721  min=0.130569    max=2.10747     nonzero 721/721
funding_rate_prediction nunique=2    min=0           max=0.0360916   nonzero   1/721
vol24h                  nunique=2    min=0           max=37923.9     nonzero   1/721
spread                  nunique=2    min=0           max=3.72079e-05 nonzero   1/721
```

The three OHLCV derivations are live on every bar. The three funding-sourced columns are
still non-zero on **1 of 721** bars — that is now, correctly, a *coverage* fact and not a
*defect*: the fix restored the trading window, it did not enrich the signal. See §6.

Clean-vs-naive accounting holds: `bid`/`ask` are absent from both observations even though
the export CSV carries them as staged columns.

### 4.3 Legacy columns are bit-identical (same-frame method)

One shared OHLCV frame through the merge seam twice (funding file vs nothing):

```
merged   OBSERVATION width: 60
unmerged OBSERVATION width: 52
the 52 pre-existing columns: max|delta| = 0.0
```

Method note carried forward: comparing two separately-fetched export CSVs is invalid — a
90-second gap moves the partial tail candle, so every rolling window anchored on it shifts.
Same-frame comparison is the only valid form.

### 4.4 The width guard is UNTOUCHED and still fires

Pointed the **52-wide** trained artifact at a **mismatched** config (its
`funding_features_file` repointed at the real one-record file, `n_features` left at 52),
then ran the real CLI:

```
$ kraken-trading-bot backtest --ticker ETH_USD --model rv2_stale --pages 2 --seed 42 \
    --models-root /tmp/rv2/staleroot
Error backtesting ETH_USD/rv2_stale: Feature-width mismatch (ETH_USD/rv2_stale backtest):
the model was fitted on 52 features but the live pipeline produced 60. not in the model:
['spread', 'funding_rate', 'basis', 'open_interest', 'signal_age_hours',
 'signal_observed', 'funding_rate_prediction', 'vol24h']. This means the feature pipeline
was widened or narrowed after training (feature_windows / feature_groups, or the
funding/vwap/count inputs the derived columns need). Retrain the model, or restore the
feature config it was trained with.
```

It refuses and names the eight missing columns. The fix touches only the start-index
selection, never `check_feature_width`; unit coverage including the anti-tautology pin is
unchanged and inside the 147.

### 4.5 Null-funding start index is still exactly 24

Pinned on the committed tree and re-measured on the reverted tree:

| | `_start_index` (post-fix) | `_start_index` (pre-fix) |
|---|---|---|
| Null-funding (width 52) | **24** | 24 |
| Funding-backed (width 60) | 24 | 720 |
| No new inputs (width 49) | **24** | 24 |

The fix did not trade the bug for a changed warm-up semantic: on the leg with no exogenous
column the rule reduces to the original one and the number is bit-for-bit the same. New
tests `test_null_funding_start_index_is_unchanged` and
`test_sparse_and_null_funding_share_the_warmup_boundary` pin exactly 24.

---

## 5. Baseline comparison, recorded honestly

| | Funding-backed (`rv2_funding`) | Null-funding baseline (`rv2_baseline`) |
|---|---|---|
| Obs width (train log) | **60** | **52** |
| `n_features` in config.yaml | 60 | 52 |
| Train | completed, `Trained: True` | completed, `Trained: True` |
| Backtest bars replayed | **697 of 721** | **697 of 721** |
| Total return | 4.35% | 2.24% |
| Sharpe | 0.876 | 0.298 |
| Max drawdown | 4.93% | 7.39% |
| Trades | 374 | 298 |
| Win rate | 46.07% | 40.00% |
| Final equity | 10,434.69 | 10,224.00 |

No `FeatureWidthMismatchError` and no `NotEnoughDataError` on either leg. The two failure
modes the gate names are absent, and unlike the previous revision both legs replay a
comparable number of bars.

### 5.1 What the gate does and does not assert on metrics

PPO with a fixed seed is **still stochastic across runs** — measurably so here: the fix
commit reports 576 trades on this exact scenario and I measured 374. The comparison
therefore gates on:

- **feature vectors being identical** for shared columns — asserted, **PASSING** (§4.3: all
  52 bit-identical, `max|delta| = 0.0`);
- **bars replayed and trades > 0** on the funding-backed leg — asserted, **PASSING** (§2.1,
  §2.2);
- **metrics in the same neighbourhood** — **NOT ASSERTED, and not assertable at this
  budget**. A 3,000-timestep PPO run on 721 bars with one seed cannot establish behavioural
  equivalence, and the two legs here differ in width (60 vs 52) so they are not the same
  model class anyway. Any claim that funding-backed training "matches" null-funding must
  rest on many-seed, many-window runs. The numbers above are a record, not a result.

---

## 6. Persistence, freshness, and what the fix did *not* do

- **Funding file**: `/tmp/rv2/eth_usd_funding.jsonl`, produced by the keyless sibling CLI.
  **1 record**. No API key used anywhere in this pass.
- **Freshness**: `signal_observed` is 1.0 on exactly **1** row; `signal_age_hours` maxes at
  0.0. Both are in the observation, so "a zero here means no reading" is unambiguous to the
  agent rather than guessed at.
- **Coverage is still thin, and that is now honest rather than hidden.** One record covers 1
  of 721 bars. The fix made absence *observable and non-destructive*; it did not make the
  signal richer. At `signal_max_age_hours: 12` a freshly-configured deployment reads funding
  on a handful of bars until the file accumulates. That is Gap-2 work — see `PLAN.md` §3.
- **Ticker warning**: the sibling writes `spot_pair`, not `ticker`, so the seam logs
  `has no 'ticker' field — treating it as a one-ticker file and merging every record` and
  merges at WARNING. Correct documented behaviour; it does mean the sibling's output cannot
  satisfy `signal_require_ticker: true` as shipped. Producer-side follow-up, not a defect in
  this slice.
- **Scratch discipline**: configs `/tmp/rv2/{funding,baseline}.yaml`, models
  `/tmp/rv2/models*`, CSV `/tmp/rv2/funding_export.csv`. Nothing under `models/`; nothing
  under `/tmp` committed. The reverted-source experiment lived in `/tmp/rv2/scratch` and was
  restored from the MAIN checkout afterwards.

---

## 7. Summary

| # | Criterion | Verdict |
|---|---|---|
| a | pytest green + flake check green | **PASS** — 147 passed; flake "all checks passed!" |
| b | real train AND backtest, no width mismatch, no `NotEnoughDataError`, window intact | **PASS** — 697/697 bars replayed, 374 trades; the pre-fix 1-bar/0-trade failure reproduced and is gone (§2.1, §2.2) |
| c | consumed proof, six names by name | **PASS** — export CSV + `normalization.npz` + `Obs features: 60` |
| d | guard non-tautological and fires, untouched by the fix | **PASS** — real CLI raises on a 52-wide artifact against a 60-wide frame, naming the eight columns |
| e | presence-gating: no new inputs → 49 | **PASS** — measured 49 |
| f | baseline comparison recorded, with the stochasticity caveat | **PASS as a record** — vector identity and bar/trade counts asserted; metric equality explicitly not asserted, and not assertable (§5.1) |
| g | the fix's own tests are non-vacuous | **PASS** — the key test fails with `1 of 721 bars tradable (start index 720)` when only the source is reverted (§3) |

The gap that mattered is closed, and it is closed with evidence rather than assertion: the
defect reproduces on a reverted tree, disappears on the committed tree, the no-new-inputs
and null-funding widths and start indices are unchanged, and the guard that did its job
before still does.