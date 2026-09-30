# PLAN — data-pipeline pass: Candidate 1 (normalization → observation)

Author: reviewer, audit-pipeline team, Phase 6 (2026-09-30). Overwrites the
prior-pass `PLAN.md` (the `kraken-deep-history` roadmap, still carried in
git history at `d55cac0`). The gate verdict and full evidence live in
`VALIDATION.md`; this document covers what was audited, what was deferred,
what was built, and concrete next steps.

---

## 1. What was audited

Outcome type **IMPROVE-EXISTING**; target **Candidate 1** — the
`normalization.npz` stack that `train.py` saved but that nothing ever
applied to the observation the policy reads.

- **The pipeline, end to end, as shipped on master:** `rl/data.py`
  (`read_ohlc_dataframe`, `prepare_episode`) → `rl/features.py`
  (`FeaturePipeline.compute/fit/transform`, `NormalizationStats`) →
  `rl/environment.py` (`_raw_feature_array` → `_observe`) →
  `rl/train.py` / `rl/backtest.py` / `rl/paper_trade.py` /
  `rl/export.py`.
- **The defect:** the fitted per-ticker stats were persisted next to every
  model and read back by `backtest.py:155-161` / `paper_trade.py:187-188`,
  but never shaped the observation. The policy trained and traded on raw
  heteroscaled float — `obv` at ±55,000 sitting in the same `Box` as
  `return_1` at ±0.01. It was already surfaced as a red test on the
  working tree (`test_rl_export.py:157-161`).
- **The fix as landed (`5df99ef`):** `prepare_episode` slices the episode
  window *before* fitting (no look-ahead into the held-out tail);
  `FeaturePipeline.fit/transform` operate on the **ffilled** observation
  frame (`compute → ffill → fillna(0)`) rather than the raw compute frame;
  `_raw_feature_array` applies the ticker's stats to the ffilled matrix;
  `paper_trade._build_observation` applies the identical transform to the
  live window. `1568771` then pinned all four consumers with 5
  mutation-checked tests (101 → 106 passed).
- **Verified in Phase 6** against the live keyless Kraken API: the
  observation is now z-scored (raw `|max|` 55,082 → obs `|max|` 12.81,
  per-column std 1.000000, width unchanged at 49), the npz is *applied*
  not just saved, and train/backtest/paper/export all reach the identical
  canonical row — all four comparisons exact (`max|Δ| = 0.0`). See
  `VALIDATION.md`.

## 2. What was deferred (the runner-ups)

None of these were dropped; they lost on directness per `DECISION.md` §5
and are listed here with what Phase 6 added to each.

1. **Candidate 2 — market-data depth + clean splits** (`since`/`until`
   plumbing, `kraken-deep-history` seeder, store-backed reads). Biggest
   deferred item, and Phase 6 turned one part of its case from assumption
   into a measurement: with `market_data_store: null` and no store root on
   this host, **a training window is whatever the live endpoint returns at
   that moment**, so a trained model's `normalization.npz` is not
   reconstructible afterwards. One CLI train and a later fetch of the
   *same* 721-bar span disagreed by 16 % on `rsi_24`'s fitted std; two
   later fetches agreed to 1.2e-4, and a repeat train reproduced a fresh
   fit to 6.5e-7. The live path is deterministic; the *snapshot* is not.
   A seeded store makes both the split and the stats reproducible.
2. **Candidate 4 — `kraken-python` retry/backoff** (`transport.py:188-261`
   raises `RequestException`/`RateLimitError` immediately, no retry;
   proven in-family in `kraken-funding-rates` and `kraken-market-data`).
   Sibling-repo change, no observation impact, correctly excluded from a
   one-repo outcome. Still the cheapest safety win on the board.
3. **Candidate 3 — scheduling + staleness + forward
   `funding_rate_prediction`**. Operational, protects the three signal
   seams, and `funding_rate_prediction` is a genuine zero-cost
   forward-looking win — but all three signal projects still ship only a
   manual `cli.py pull`, and none is wired into the bot flake.
4. **NEW-DATA-SOURCE candidates — microstructure (5) and on-chain (7)**.
   Lowest directness-vs-cost; both need recording/consolidation machinery,
   and both only become worth building *because* this pass made the
   observation pipeline normalize any incoming column. That precondition
   now holds.

## 3. What was built

**On master before Phase 6** (`2b3e065`, `5df99ef`, `1568771`) — the
fix itself, its regression contract, and the `export-data` CLI. Phase 6
contributed **validation only**: two documents, no source changes, no
new tests, nothing committed from scratch. Artifacts from the integration
test live under `/tmp` and were never committed.

## 4. Concrete next steps

### 4.1 Finish wiring normalization into the RL pipeline

The fix makes the observation z-scored, but three things are still open
and are *not* required for correctness — they are what turns "correct
observation" into "trained model that means something".

1. **Persist the fitted-stats shape in `config.yaml` provenance.**
   `config.yaml` records `feature_windows` and `feature_groups` but not
   the feature *count* the npz was fitted on. A model whose
   `feature_groups` were edited after training silently loads a 49-column
   npz against a 50-column pipeline. The regression tests pin the 4
   consumers; nothing pins *staleness*. Cheapest fix: record
   `n_features` (and the `feature_groups` actually used) at train time and
   have `scan_model`/`RLAgent.load` refuse a mismatch with a clear
   message instead of a raw width error deep in the env.
2. **Make `feature_groups` provenance exact, not just present.**
   `_SIGNAL_COLUMNS` is duplicated (`features.py:36` and `data.py:50`)
   and currently agrees by hand. Centralize it in one module so the
   allow-list cannot drift from the normalize path — flagged in
   `DECISION.md` §2.3 and still open.
3. **Re-fit existing models.** `models/` is empty on this host, so there is
   no retrain burden today — but the first model trained *before* `5df99ef`
   learned on raw features and must be discarded, not backtested. The
   README caveat added in `1568771` covers the documentation side; the
   operational side is "delete and retrain", which has no automation yet.

### 4.2 For the next pass (in rough priority order)

1. **Candidate 2, split into its two independent halves.** The
   `since`/`until` plumbing is config keys plus one function at the
   `data.py` call sites and needs no sibling repo — do it alone as a
   clean-splits improvement. Seeding a store (`kraken-deep-history seed`)
   is ops, not code — do it separately, after the plumbing, because it
   makes both splits and stats reproducible (`VALIDATION.md` §4).
2. **Candidate 4** (`kraken-python` retry/backoff) as its own one-repo
   pass. Pure reliability, proven in-family, no observation impact.
3. **Candidate 3** (schedulers + staleness + `funding_rate_prediction`).
   Unblocks the three signal JSONLs that are configured but never
   populated.
4. **A real convergence A/B.** `VALIDATION.md` §5 records why the optional
   micro-A/B was deliberately skipped (721 bars / one seed = noise). Do it
   properly once a store exists: multi-seed, multi-thousand-bar windows,
   z-scored vs. raw, reporting a reward trajectory rather than a single
   number. That is the experiment that would substantiate the fix's *value*,
   as opposed to proving its *correctness* (which is now exact).
5. **Microstructure / on-chain sources** — only after the above; the
   normalization precondition is the thing that made them worth building,
   and that part is done.

### 4.3 Standing constraints for any future pass

- A training window must be **recorded, not just fetched** — the
  reproducibility gap in `VALIDATION.md` §4 is the concrete cost of not
  doing so.
- Scratch models, stores and exports go to `/tmp` and are never committed.
- Phase 6's live runs needed `--ticker ETH_USD`; `--ticker USD_SOL` fails
  with `Unknown Kraken pair: 'USD/SOL'`. Worth resolving before any pass
  that assumes USD-quoted pairs resolve from the CLI's `USD_`-prefix form.
