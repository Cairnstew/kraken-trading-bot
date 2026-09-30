# VALIDATION — kraken-trading-bot data-pipeline pass (2026-09-30, IMPROVE-EXISTING / Candidate 1)

Phase 6. Outcome type **IMPROVE-EXISTING**; target **Candidate 1** — the
`normalization.npz` stack that was saved but never applied to the RL
observation. Bots' work on master: `2b3e065` (WIP export-data), `5df99ef`
(fix), `1568771` (5 regression tests + README). Baseline suite **106
passed**.

This pass answers one question: *does the fix actually change what the
policy sees?* Not "does the npz exist" — it existed before the fix. The
proof is a real train + backtest + paper-trade against the live keyless
Kraken API, plus an exact, pinned-frame replay of all four consumers.

---

## 1. INTEGRATION-TEST MATRIX (all three steps actually ran)

Every row is a real run from this repo inside `nix develop`, against the
live keyless Kraken API. No mocks, no synthetic data in this table.

| # | Step | Command (abbrev) | Result |
|---|------|------------------|--------|
| 1 | Unit/regression suite | `pytest tests/ -q` | **106 passed**, 20 warnings, 16.26 s |
| 2 | Train (packaged CLI) | `kraken-trading-bot train --ticker ETH_USD --model ppo_norm_smoke --config configs/default.yaml --pages 2 --timesteps 3000 --models-root /tmp/data-audit-models` | **OK** — 721 bars, **49 obs features**, 3000 timesteps, registered |
| 2b | Train (repeat, live) | same, `ppo_norm_smoke2`, 1000 timesteps | **OK** — used as the reproducibility control (§4) |
| 3 | Backtest (packaged CLI) | `kraken-trading-bot backtest --ticker ETH_USD --model ppo_norm_smoke --pages 2 --models-root /tmp/data-audit-models` | **OK** — 697 steps, return **8.37 %**, Sharpe **0.898**, max DD **8.04 %**, trades **439**, final equity **10,836.82** |
| 4 | Paper-trade (packaged CLI) | `kraken-trading-bot paper-trade --ticker ETH_USD --model ppo_norm_smoke --iterations 1 --dry-run --models-root /tmp/data-audit-models` | **OK** — 1 tick, `side=buy volume=0.14273042 price=2682.45`, equity 10,000 |
| 5 | Export (packaged CLI) | `kraken-trading-bot export-data --ticker ETH_USD --pages 2 --normalized --output /tmp/audit-exports/ETH_USD.csv` | **OK** — 721 bars × 108 cols; stages: 1 timestamp, 8 OHLCV, 0 signal, **49 raw**, **49 normalized**, warmup flag |

**No observation-width mismatch, no `NotEnoughDataError`, no traceback in
any of the five live runs.** The packaged binary imported and ran from the
committed tree (the only dirty paths are `.data-audit/*.md`; `git status`
confirms no source file is modified).

Ticker note: `--ticker USD_SOL` fails first try with
`Unknown Kraken pair: 'USD/SOL'` (`data.py` pair resolution expects
`BASE/QUOTE` with the real Kraken base asset). `ETH_USD` is what
`configs/default.yaml` itself sets and works. Worth a note for any future
pass that assumes USD-quoted pairs resolve from the CLI's `USD_`-prefix
form.

---

## 2. THE Z-SCORED-OBSERVATION PROOF (gate clause c)

`/tmp/audit-proof.py`. **16/16 checks PASS**, most of them *exact*
(`max|Δ| == 0.0`), on a pinned frame: one live ETH/USD 721-bar window is
captured to parquet and every consumer is replayed against that
byte-identical frame. That matters because two independent live fetches
differ (Kraken's newest candle is in-progress and the clock moves), so a
cross-fetch comparison is only valid to ~1e-4 — pinning the frame is what
makes the wiring provable rather than merely plausible.

### (a) the npz is *applied*, not merely saved

| Check | Result |
|---|---|
| (a) npz written by `train_ticker` == an independent `FeaturePipeline.fit` on the ffilled training window | `max|Δmean| = 0.0`, `max|Δstd| = 0.0` over **49** columns — **exact** |
| (a2) the fit target is pinned, not accidental: ffilled-frame stats differ from raw-compute-frame stats | `max|mean_ffilled − mean_raw| = 82.89` (the NaN-warmup rows the observation zero-fills but `compute` does not) |

### (b)(c) the observation the policy receives IS the z-scored row

| Check | Result |
|---|---|
| (b) `env._raw_feature_array() == FeaturePipeline.transform(episode, ticker_id)` | `max|Δ| = 0.0`, shape (721, 49) — **exact** |
| (b2) the observation is *not* the raw matrix (pre-fix behaviour) | `max|obs − raw| = 55,079.5` |
| (c) observation is z-scale, raw features are dollar-scale, width unchanged | raw `|max| = 55,082.1` → obs `|max| = 12.8061` (**4,301× contraction**); mean per-column std of the observation = **1.000000**; obs width **49 == 49** (affine, so `model.zip` still loads) |
| (c2) `reset()` observation == `transform()[start_index]` (z-scored, not raw) | `max|Δ| = 0.0`; `start_index = 24`; `|obs|max = 2.3184` |

Widest raw features against their observation columns — this is the
heteroscaling the fix removes:

| feature | raw `|max|` | observation `|max|` |
|---|---|---|
| `obv` | 55,082.1 | 2.5497 |
| `obv_slope_24` | 43,769.5 | 3.8665 |
| `obv_slope_4` | 20,700.8 | 4.8152 |
| `obv_slope_1` | 13,833.3 | 6.5946 |
| `bb_upper_24` | 2,809.2 | 5.3562 |

### (d)(e)(f) all four consumers reach the same canonical row

| Check | Result |
|---|---|
| (d1) export `--normalized` emits one `z_` col per feature | 49 `z_*` / 49 features |
| (d2) **export `z_*` block == the env observation matrix, row-for-row** | `max|Δ| = 0.0` — **exact** |
| (d3) export `z_*` is z-scored, its raw block is not | `z_*` per-col std (ddof=0) mean **1.0000000**; raw per-col std max **21,032.1** |
| (d4) warmup rows present and flagged | 24 warmup bars flagged, 697 tradable |
| (e0) `PaperTrader` loaded the stats **from disk**, not a refit | 49 features loaded from `normalization.npz`, means equal to disk |
| (e) `PaperTrader._build_observation == env observation last row` | `max|Δ| = 0.0` — **exact**; row `|z|max = 1.1990` (the raw row would be `|max| = 21,151.6`) |
| (e2) paper observation width matches the trained policy's obs space | (49,) vs (49,) |
| (f) stats applied **verbatim**, never refit over the replay window | an unregistered-stats pipeline yields a different row: `max|paper − raw| = 21,150.5` |

Live CLI export agrees independently — the `--normalized` CSV on the real
live path shows exactly the intended contrast:

```
rows 721  cols 108  z_* 49
obv    raw range  -55082.1 ..  38709.6   ->  z_obv    -2.550 .. 1.910
sma_1  raw range    2371.3 ..   2784.5   ->  z_sma_1  -1.663 .. 2.017
```

---

## 3. ARTIFACT-SET PROOF (gate clause d)

`models-root = /tmp/data-audit-models` (scratch; **never committed**):

```
/tmp/data-audit-models/ETH_USD/ppo_norm_smoke/
  model.zip           211,008 B
  normalization.npz     5,352 B
  config.yaml             570 B
```

All three present — the full `{policy, npz, config}` set. `config.yaml`
provenance records `action_space=continuous, reward=pnl, windows=[1, 4, 24]`,
and `PaperTrader`/`backtest` both read the npz back through it.

`normalization.npz` contents (`allow_pickle=False`):

| Property | Value |
|---|---|
| `ticker_id` | `ETH_USD` |
| `means.shape` / `stds.shape` / `feature_names.shape` | (49,) / (49,) / (49,) |
| non-trivial means (`|mean| > 1e-6`) | **47 / 49** |
| non-trivial stds (`std > 1e-6`) | **46 / 49** |
| `abs(mean)` range | 0.000000 … 2561.556 |
| `std` range | 0.0 … 21015.298 |
| dollar-scale columns (`|mean| > 1` or `std > 1`) | **33 / 49** |

The two degenerate columns are the ones the `std > 1e-12` floor in
`NormalizationStats.normalize` exists for; the observation matrix's
per-column std table confirms it (constant columns → 0, everything else
→ 1.000000).

---

## 4. ONE FINDING WORTH RECORDING: live training windows are not reproducible

The first CLI train (`ppo_norm_smoke`) and a later live fetch disagreed on
`rsi_24`'s fitted std by **16 %** — far beyond fetch noise. That looked
like a wiring bug and was chased down rather than waved off:

- Two live fetches of the *same* 721-bar span, minutes apart, agree to
  `max relative std difference = 1.2e-4` (`rsi_24`: 1.9e-6). Only the
  single in-progress candle differs (`max |Δclose| = 0.04`).
- Perturbing only that in-progress bar by `$0.04` moves `rsi_24`'s fitted
  std by `1.4e-9`. So last-candle drift cannot explain 16 %.
- A **repeat** live CLI train (`ppo_norm_smoke2`) reproduces a fresh live
  fit to `max relative std = 6.5e-7` (median 2.1e-8) — i.e. the live train
  path is fully reproducible.

Conclusion: the 16 % gap was a property of that one run's data snapshot,
not of the code path. It is **not** gate evidence and is not counted as
such. It is, however, a real operational signal: with `market_data_store:
null` and no store on this host, a training window is whatever the live
endpoint returns *at that moment*, so a trained model's `normalization.npz`
is not reconstructible after the fact. That is the reproducibility half of
deferred Candidate 2 and it is now measured, not assumed — see PLAN.md.

`obv` deserves one line of its own: its **mean** is the window-start level
(a `cumsum()`, `features.py:384`), so it drifts by up to 16 % between
snapshots while its **std** — the quantity that actually conditions the
observation — moves 1e-4. Comparing means across snapshots is meaningless
for cumulative features; comparing stds is the correct control.

---

## 5. GATE VERDICT

| Clause | Requirement | Evidence | Verdict |
|---|---|---|---|
| (a) | suite green, ≥106 | `pytest tests/ -q` → **106 passed** | ✅ |
| (b) | train + backtest + paper complete, no obs-width mismatch, no `NotEnoughDataError` | 5 live CLI runs, all clean; 49-wide observation loads in all three | ✅ |
| (c) | npz stats non-trivial **and applied** — z-scored observation proven, not just persisted | §2: 16/16 exact checks; `max|Δ| = 0.0` on (a), (b), (d2), (e); 4,301× scale contraction | ✅ |
| (d) | models-root has `{policy, npz, config}` | §3: `model.zip` + `normalization.npz` + `config.yaml`, 49-feature npz | ✅ |

**A/B micro-experiment: not run, deliberately.** The optional A/B
("train with normalization vs. reverted-to-raw, compare convergence")
cannot produce a meaningful verdict at `--pages 2 --timesteps 3000`: that
is 721 bars / 49 heteroscaled features / one seed, so any reward difference
is seed noise, and reporting a convergence claim from it would be
manufacturing evidence. The scale claim is instead proved directly and
exactly (`raw |max| 55,082 → obs |max| 12.81`, per-column std 1.000000),
which is the property the A/B would have been proxying for. A real
convergence comparison needs a market-data store and a multi-seed budget —
both scheduled as next steps in PLAN.md.

**GATE: PASS**

---

## 6. REPRODUCING THIS

```bash
nix develop --command bash -c "pytest tests/ -q"

# live train -> /tmp/data-audit-models (scratch; never commit)
nix develop --command bash -c "kraken-trading-bot train \
  --ticker ETH_USD --model ppo_norm_smoke --config configs/default.yaml \
  --pages 2 --timesteps 3000 --models-root /tmp/data-audit-models"

nix develop --command bash -c "kraken-trading-bot backtest \
  --ticker ETH_USD --model ppo_norm_smoke --pages 2 \
  --models-root /tmp/data-audit-models"

nix develop --command bash -c "kraken-trading-bot paper-trade \
  --ticker ETH_USD --model ppo_norm_smoke --iterations 1 --dry-run \
  --models-root /tmp/data-audit-models"

nix develop --command bash -c "kraken-trading-bot export-data \
  --ticker ETH_USD --pages 2 --normalized --output /tmp/audit-exports/ETH_USD.csv"

# the exact, pinned-frame proof (16 checks)
nix develop --command python /tmp/audit-proof.py
```

Scratch artifacts (`/tmp/data-audit-models*`, `/tmp/audit-frame.parquet`,
`/tmp/audit-exports/`) are outside the repo and are not committed. The only
files this pass writes are `.data-audit/VALIDATION.md` and
`.data-audit/PLAN.md`.

### Test-suite delta attributable to this pass

`101 → 106 passed` is `1568771`'s five regression tests
(`test_rl_environment.py`, `test_rl_export.py`, `test_rl_paper_trade.py`,
`test_rl_training.py`), which pin the same four-consumer contract this
document proves against live data. Phase 6 added **no** tests and changed
**no** source — it is validation only.
