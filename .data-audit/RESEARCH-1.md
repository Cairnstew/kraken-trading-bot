# RESEARCH-1 — Candidate 1: normalization.npz never shapes the observation (IMPROVE-EXISTING, data-quality)

Researcher: researcher-1. Gap: **Candidate 1** from AUDIT.md (2026-09-30).
Sub-problem in scope: `prepare_episode` fits stats on the full frame before
slicing (`data.py:534`) — the look-ahead trap.

Method: this is an IMPROVE-EXISTING item, so I surveyed **approaches/patterns/
reference implementations** for observation normalization in RL pipelines and
look-ahead-safe stat fitting, and grounded everything in the actual repo code
on disk (read: `features.py`, `environment.py`, `data.py`, `export.py`,
`backtest.py`, `paper_trade.py`, `train.py`, `registry.py`, `agent.py`,
`tests/test_rl_export.py`; reproduced the red test in the dev shell). No code
was written.

---

## 0. Ground truth: why the red test fails (reproduced)

`tests/test_rl_export.py::test_build_export_frame_normalized_block_is_z_scored`
fails on the current tree. Reproduction in `nix develop`:

- `transform(episode)` computes z from the **raw `compute()` frame** (leading
  NaN warmup rows present), normalized by stats that `prepare_episode` fitted
  on the **full pre-slice frame**, then `ffill().fillna(0)`. So:
  - warmup row 0 → `z_return_1[0] = 0.0` (raw NaN → ffill has nothing before it → 0),
  - and the mean/std are derived from the raw column's `skipna` mean, while the
  test re-derives mean/std from the **ffilled observed column** — a different
  number.
- The assertion `z == (observed − mean)/std` fails at row 0 (`0.0` vs `0.16146`)
  and is systematically offset on every other row (e.g. `0.4760` vs `0.4766`).

So the mismatch is *exactly* AUDIT's Candidate 1: stats are fitted on the raw
NaN-warmup frame while the observation (and the test's re-derivation) is the
ffilled frame. Three concrete defects in the repo, in severity order:

1. **The transform is never applied to the observation.** The policy trains and
   infers on `_raw_feature_array()` (`environment.py:445-448` = compute + ffill
   + fillna(0)). `transform()` is reachable only from WIP `export.py:195`.
   `normalization.npz` is saved (`train.py:238`), loaded (`backtest.py:161`,
   `paper_trade.py:188`) and then **ignored for the obs vector**. Dollar-scale
   `sma_24/ema_24/bb_upper_*/bb_lower_*/atr_*/obv*` sit beside unit-scale
   `return_*/price_ratio_sma_*/bb_pctb_*` in a single `Box`.
2. **Fit-frame ≠ observation-frame** (the red test). `fit()` stores mean/std of
   the raw compute output; the observation is the ffilled matrix.
3. **Look-ahead in fit ordering.** `prepare_episode` calls `features.fit(df)` on
   the full frame, *then* slices the trailing episode (`data.py:547,557`). Any
   bar in the fetched frame outside the trailing slice leaks into the stats.

Reference-format note for these three: the normalization stack is *already*
in-repo and complete (`NormalizationStats` + `fit/transform/fit_transform` +
npz save/load + per-ticker keying + std-floor guard). The gap is **wiring and
ordering**, not a missing library. Net dependency surface for the recommended
fix: **zero new packages**.

---

## 1. Approach A — Apply the fitted transform on the observation path (recommended)

Reference pattern: **Stable-Baselines3 `VecNormalize`** — the canonical RL
answer to "policy trains on raw features of wildly different scales, and eval
must see the same scale." Its reference elements that matter here:

- running mean/std stored **next to the policy**, loaded at eval, frozen with
  `training=False` so statistics never drift or leak during evaluation;
- a small epsilon on the std and an optional clipping range;
- obs = affine `(x − mean)/std`, so it is losslessly invertible.

The bot already has the durable half (npz persisted per model, loaded by
backtest/paper). It is missing the application half. The mapping to this code:

- `environment.py`: build the obs matrix by **normalizing the already-computed
  `_features` frame** with the ticker's `NormalizationStats` (mean/std from the
  ffilled frame — inflate `_raw_feature_array`, don't re-call `compute`). The
  std-floor guard (`NormalizationStats.normalize`, `features.py:127`) already
  handles constant columns; `z` of a zero-std column is `0`.
- `paper_trade.py:_build_observation` (`paper_trade.py:300-319`): apply the same
  transform to the live window's last row, so live inference scale == training
  scale. This is the exact pattern VecNormalize's eval-with-loaded-stats is for.
- `backtest.py`: already loads npz; once the env applies stats, backtest is
  automatically on the same scale. No backtest change needed.
- obs-space width is unchanged (affine per-feature), so `model.zip` still loads;
  only the *scale* changes. **`models/` is empty on disk** (AUDIT §"models/ is
  empty"), so there are no existing policies to migrate.
- **Cost to reduce to what the RL pipeline consumes:** trivially per-ticker,
  per-timestamp scalar — mean/std per (ticker, feature) is already exactly the
  `NormalizationStats` dict shape; each observation row applies it column-wise.

**Cost:** ~20-30 lines across `features.py` (fit/transform convention),
`environment.py` (obs path), `paper_trade.py` (inference row), plus the export
docstring flip (§4). No new dependency. Downside: any *existing* trained model
must be retrained (obs scale changes); with an empty `models/` that is moot.

---

## 2. Approach B — Fix fit-frame + fit-ordering only (minimal green test)

Pattern source: **`sklearn` `StandardScaler` fit/transform split** and
**walk-forward/scikit `TimeSeriesSplit`** — "fit the scaler on the training
block only; transform test with the frozen fit." The Bot already implements the
train/eval distinction via *persistence*; it applies it nowhere and fits on the
wrong frame. Two ordering fixes, in `features.py` + `data.py`:

1. **Fit on the ffilled observation frame, not the raw NaN frame.** Make `fit()`
   (and `transform()`) operate on `compute → ffill → fillna(0)`, i.e. exactly
   the matrix `_raw_feature_array` produces. Then `z_* = (observed − mean)/std`
   is an exact affine image of the observation by construction, and the red test
   passes *as written* (it re-derives mean/std from the ffilled observed block).
2. **Fit on the training slice only.** In `prepare_episode`, move `features.fit`
   after the slicing decision and fit on the returned `window` (`data.py:547-559`),
   so the fork of the fetched frame that is never traded on never feeds the stats.
   This is the look-ahead fix. With `episode_bars=None` (no slice) nothing
   changes; with a slice, stats now cover exactly what the episode covers.
   Backtest/paper continue to load the persisted npz and never refit — so the
   only stat-fitting sites are train and export, both now slice-consistent.

**Cost:** ~10-15 lines, both files, plus one or two test adjustments. Zero new
dependencies (all patterns already present in-tree). Closes defects 2+3 but
**leaves defect 1** (policy still trains on raw, heteroscaled obs) — the
conditioning win is deferred. This is the "cheapest green" option if the team
wants CI green before touching the observation.

---

## 3. Approach C — Robust / quantile scaling for heavy-tailed price features

Reference implementations: `sklearn.preprocessing.RobustScaler` (median / IQR),
`QuantileTransformer(output_distribution='normal')`, `PowerTransformer`
(Box-Cox/Yeo-Johnson). These genuinely address the heavy tails in dollar and
volume features (`article_count`, `stt_mention_count`, `open_interest` dwarf the
returns).

**The blocker:** the red test's contract is **linear** — `z = (observed − mean)/
std`, "a faithful render of normalization.npz." RobustScaler is linear
(median/IQR) and could satisfy a *renamed* test but not this one; Quantile and
Power transforms are **nonlinear**, so the `z_*` block can never be expressed as
`(x − mean)/std` and the npz format (`means`/`stds` arrays, `features.py:76-83`)
would have to change shape (per-feature quantile maps). That is a larger format +
test rewrite than Candidate 1 needs, for an incremental-stability gain on a
handful of columns.

**Recommendation:** keep mean/std z-scoring as the baseline (matches the npz
layout and the red test). Treat robust/quantile scaling as a *follow-up
hardening* for the heavy-tailed signal columns, gated on the linear format being
settled first. Median/IQR also does nothing for the look-ahead trap — any fit
statistic is leakable the same way.

---

## 4. The fit-frame question (raw NaN vs ffilled), and the export contract

The red test pins the convention: **fit on the ffilled observation frame.** That
is also the semantically right target because the env hands the policy rows of
that ffilled matrix; `z_*` must be an affine image of what the policy sees or the
"normalized block" describes a vector nothing consumes.

One statistical caveat worth the architect knowing: the ffilled frame's warmup
rows (pre-`_first_valid_index`) carry copies of the first valid value into
`mean`/`std`. The env never *shows* those warmup rows to the policy (`reset()`
starts at `_start_index`), so they are a tiny dilution of the stats, not a leak —
~30 of ~4320 rows on a 6-page fetch. The cheaper alternative ("fit on the valid
region only") would require editing the red test to mask warmup rows; not worth
it since the ffilled convention is what the test already encodes and what
guarantees `z == affine(obs)`.

**Export semantics flip if Approach A lands:** `export.py:24-32` currently
documents the raw `features` block as "the agent's observation vector." Once the
env applies stats, that block is the *pre-transform* features and the `z_*`
block is the observation. The fix changes the docstring and the `--normalized`
flag's meaning (it stops being opt-in "for inspection" and becomes the live obs).
`attrs["stages"]` keys can stay.

---

## 5. Deleting the npz machinery — concrete "what breaks" map

If the team instead decides the normalization stack is worthless (not
recommended — it is the audit's #1 observation-impacting code fix), here is the
full blast radius, because the machinery is load-bearing beyond the z-columns:

| Call site | What breaks if `normalization.npz` write+read is removed |
|---|---|
| `backtest.py:155-161` | The `record.normalization_path is not None` guard is what decides *whether to build a config FeaturePipeline at all*. Without it, `pipeline=None` → env falls back to the **builtin 8-feature** obs (`_BUILTIN_FEATURES`) while `model.zip` was trained on **49** → SB3 `PPO.load` obs-space mismatch. Backtest must build the pipeline unconditionally from config. |
| `paper_trade.py:187-188` | Same guard → same obs-width mismatch on live inference. |
| `cli.py:428` | `record.is_trained()` requires `normalization_path is not None` (registry.py:82) → an npz-less model is reported "not trained" and `paper-trade` refuses to run. |
| `registry.py:82` `is_trained()` | Semantics of "trained" collapse to "model.zip exists," or every gated command changes. |
| `train.py:238` | Removed call; fine by itself. |
| `export.py:195` + `test_rl_export.py:133-161` | The `--normalized` block has nothing to emit → the red test must be **deleted**, which removes the only guard the audit uses to detect this class of bug. |
| Also affected | `tests/test_rl_environment.py:217-240` (npz roundtrip), `test_rl_training.py:275` (asserts npz after train), `test_rl_paper_trade.py:99` (fixture fabricates npz), `test_rl_cli.py:53-63,224` (mock npz in a trained fixture), README/configs/.gitignore artifact docs. |

Net: deleting is **mechanically possible but reverts the conditioning outcome the
audit ranks first, forces obs-width logic changes in backtest/paper/CLI, and
deletes the one regression test that catches this bug class.** The cheap,
correct branch is fixing the wiring — not removing it.

---

## 6. Sibling repo / half-implementations

- `kraken-python`: pure REST transport + paper account. Grep for
  `zscore/normalize/scaler/standardize` finds only `watch.py`'s dict-shaped
  ticker "normalized" — **nothing RL- or feature-related to reuse**. No
  half-implementation exists there.
- The bot's own `rl/` *does* contain a complete, unused half-implementation:
  `NormalizationStats.fit/transform/fit_transform` + npz persistence are all
  written and unit-tested (`test_normalization_stats_roundtrip`,
  `test_normalization_stats_isolated_per_ticker`). The unfinished parts are
  exactly the two seams this research names: (a) apply on the observation, (b)
  fit on the right frame/slice. Confirmatory evidence: `features.fit_transform`
  has **no callers** anywhere in the repo (grep).

---

## 7. Reference implementations surveyed (maintenance/license/effort)

| Ref | Role here | Maintenance / license | Effort to consume |
|---|---|---|---|
| **SB3 `VecNormalize`** (already a dependency via `stable_baselines3` PPO) | Canonical save-stats-at-train / freeze-at-eval observer normalizer. MIT. Actively maintained. | Pattern-only, ~20-30 lines in env+paper. Do **not** literally wrap in VecEnv (that would add a `make_vec_env` + `vecnormalize.pkl` layer the export CSV cannot mirror). |
| **gymnasium `NormalizeObservation`/`NormalizeReward`** | Online *running* stats wrapper. MIT, maintained. | Pattern to **avoid copying**: running stats introduce a train/eval freeze distinction the bot lacks; batch-fit-on-train-slice + persisted npz is deterministic and already the bot's shape. |
| **PopArt** (Niculescu-Mizil & Caruana 2005; adopted in Tensorforce) | Value/target normalizer that rescales the value function online. | **Not applicable** — normalizes *return targets*, not features; reward is already scale-engineered (`pnl_scale=1e-4`). Listed for completeness only. |
| **`sklearn` StandardScaler / TimeSeriesSplit** | "Fit on train fold only, transform test" walk-forward pattern. BSD-3, maintained. | The exact leak-free idiom for `prepare_episode` (slice first, then fit). Zero new dependency (bot already uses sklearn? — check; nothing new required regardless). |
| **`sklearn` RobustScaler / QuantileTransformer / PowerTransformer** | Heavy-tail alternatives to mean/std. BSD-3. | Follow-up hardening only; nonlinear variants break the npz format and the red test's linear contract. |
| **mlfinlab / Lopez de Prado purged splits** | Purging/embargo for cross-validation leakage. | Overkill here: single train window + persisted stats, no overlapping-label CV. The pattern to lift is only "never fit on the held-out tail." |

## 8. Scoring (cheapness of the red-test fix × look-ahead closure)

| Option | Red test → green | Look-ahead closed | Conditioning win | New deps | Effort |
|---|---|---|---|---|---|
| **A. Apply transform on obs + fit on ffilled + fit on slice (full)** | ✅ (fits contract; z == affine of obs, now the real obs) | ✅ | ✅ recipes: put the persisted stats on all three obs paths (train env, backtest env, paper inference) and keep export/npz as the faithful render. | none | ~1-1.5 d |
| **B. Fit-frame + fit-ordering only** | ✅ as-written | ✅ | ❌ (policy still consumes raw scale) | none | ~0.5 d |
| C. Robust/quantile scaling first | ❌ breaks contract/npz format | neut | would need format change | needs decision | follow-up |
| D. Delete npz machinery | ❌ must delete the test | ✅ (no stats to leak) | ❌ (reverts the #1 fix) | none | ~1 d but reverts value |
| E. VecNormalize wrapper (literal) | ❌ disjoint from export CSV | ❌ (needs train/eval freeze) | ✅ | none (in stack) | — |

---

## 9. Recommendations (top 3, for the Decision phase)

1. **Do Approach A (full wiring).** Make `features.fit`/`transform` operate on
   the **ffilled observation frame**, have `prepare_episode` **slice first then
   fit on the slice**, and apply the ticker's fitted stats on the observation in
   `environment._observe`/`_raw_feature_array` and
   `paper_trade._build_observation`. Zero new dependencies (SB3 already present);
   the red test passes as-written and becomes a true contract (z_* == the
   policy's observation); `models/` being empty means no retrain burden. This is
   the audit's Candidate-1 fix in full.
2. **If scope is clamped to CI-green only, do Approach B** (fit-frame + slice
   ordering, ~0.5 d). Green test, look-ahead closed; the observation-application
   is left as an explicit follow-up so the conditioning win is not silently lost.
3. **Do not delete the npz stack.** Deleting breaks `is_trained()`, the
   backtest/paper obs-width guard, and the CLI gate, and removes the one
   regression test for this bug class — while forfeiting the audit's
   highest-ranked fix. Keep the linear mean/std npz format; defer robust/quantile
   scaling of the heavy-tailed signal columns to a later pass.

RESEARCH COMPLETE