# DATA PIPELINE DECISION — kraken-trading-bot (2026-09-30 pass)

Written by the architect after reading AUDIT.md (fresh) and RESEARCH.md
(consolidated R1/R2/R3). Overwrites the prior-pass DECISION.md. Grounded in
the code on disk (verified: `environment.py:445-448`, `data.py:547`,
`data.py:168-171`, `features.py:395-408`, `train.py:238`,
`kraken-python/kraken_api/transport.py:188-261`).

---

## 1. OUTCOME TYPE + TARGET

**Outcome type: `IMPROVE-EXISTING`** — decided FIRST, on AUDIT.md §4/§6
evidence, not a default. The audit's single highest-directness finding —
Candidate 1, the saved-but-never-applied `normalization.npz` stack — is a
pure-code improvement to this repo's own pipeline. AUDIT.md §6 is explicit
that it must be weighed as highly as any new source, and RESEARCH.md's
synthesis adds the decisive pressure: the working tree already carries a
**red test** (`test_build_export_frame_normalized_block_is_z_scored`,
`tests/test_rl_export.py:157-161`) on exactly this seam. There is no
sourcing gap to chase before the code we already ship is made honest; every
new-source column (Candidates 3/5) is unusable at a comparable scale until
the observation stops being raw heteroscaled float.

**Target (exactly one): the normalization stack — Candidate 1.** Apply
`transform()` (fitted per ticker on the *ffilled observation frame*, not the
raw NaN-warmup compute frame) inside the observation constructor, slice-first-
then-fit to kill look-ahead, and let the red export test become a true
contract. Zero new dependencies, ~1-1.5 d for the full wiring (R1, Approach A),
no retrain burden (`models/` is empty on disk this pass), and it is the
precondition that lowers the cost of every other candidate downstream.

The clean-splits half of Candidate 2 (`since`/`until` plumbing — config keys +
one function at `data.py` call sites, store seam already unit-tested) is
**excluded from this outcome**; it shares the slice-first-then-fit fix but adds
a sibling repo + seed-run dependency and is a separate, additive decision.

## 2. JUSTIFICATION AGAINST THE RL PIPELINE'S REAL SHAPE

- **Config inputs / knobs:** train/backtest/paper load `default.yaml` today
  (`models/` is empty → every run falls back to defaults; AUDIT §1 F). The
  relevant knobs already exist but invert meaning under the fix: normalization
  is fitted/loaded via `normalization.npz` per ticker
  (`train.py:238` saves → `backtest.py:161` / `paper_trade.py:188` load) yet
  never shapes the observation. No new config key is required; the existing
  npz is repurposed from "dead persistence" to "the actual conditioning".
  `episode_bars` (per-episode window) is the one knob that interacts with the
  look-ahead fix — see landing point 2.
- **Artifact layout:** policy + `normalization.npz` + `config.yaml` under
  `models/{TICKER_ID}/{model_name}/` (AUDIT §1 F). The fix must keep the
  `is_trained()` gate (`registry.py:82`) and the SB3 obs-space guard coherent:
  `backtest.py:155-161` and `paper_trade.py:187-188` currently read the npz as
  the *whether-to-build-the-49-feature-pipeline* flag; deleting the stack is
  explicitly off the table (R1 blast radius). The npz stays, changes meaning.
- **Exact per-ticker landing points (this repo):**
  1. `kraken_trading_bot/rl/environment.py` — `__init__:180-197` fits stats on
     the full frame then `_raw_feature_array()` (`:445-448` = compute + ffill +
     fillna(0)) builds the raw box as-is. **Apply the ticker's fitted stats to
     the ffilled matrix here** so `_observe()` (`:367-375`) yields the z-scored
     row the model was trained on.
  2. `kraken_trading_bot/rl/data.py` — `prepare_episode:547` calls
     `features.fit(df, ...)` on the full frame *before* `df.tail(episode_bars)`
     at `:557`. **Slice first, fit on the window** — removes held-out-tail
     leakage into the stats (R1 defect 3).
  3. `kraken_trading_bot/rl/features.py` — `NormalizationStats.fit/transform/
     fit_transform` + npz save/load: make fit and transform operate on the
     **ffilled observation frame** (the mismatch behind the red test, R1 defect
     2) rather than the raw compute frame. Optionally centralize `_SIGNAL_COLUMNS`
     (here `:36` vs `data.py:50`) to one module so the allow-list can't drift.
  4. `kraken_trading_bot/rl/paper_trade.py` — `_build_observation:300-319`
     rebuilds the same raw row per 60 s tick; must apply the identical
     transform so paper observations match backtest/train (no train/infer skew).
  5. `kraken_trading_bot/rl/export.py` (WIP) + `tests/test_rl_export.py:157-161`
     — the failing assertion becomes the regression contract for the whole fix;
     export's `z_*` block must equal the observation-derived z-scores.
- **Transport (for context, not in this outcome):** `kraken-python/kraken_api/
  transport.py:188-261` confirmed — `_request` raises `RequestException`/
  `RateLimitError` immediately, no retry/backoff; that fix (Candidate 4) is
  proven in-family (`kraken-funding-rates/client.py:70-95`,
  `kraken-market-data/client.py:115-142`) but is a sibling-repo change and is
  excluded from this outcome (see runner-ups).

**Seam note (Integrator):** this outcome lives entirely in this repo, so the
Integrator's seam step **collapses** into *confirm the normalization path is
wired through train/backtest/paper and close call-site gaps* — i.e. verify all
four consumers (`train`, `backtest`, `paper`, `export`) reach the same
ffilled-and-transformed row, and that `models/` provenance (`config.yaml`)
records the fitted-stats shape at train time.

## 3. NAMING

**No new project, no new repo, no naming step.** Touched repo:
`kraken-trading-bot` (this repo). In-scope units as listed in §2. The only
other repo that could be touched is `kraken-python` (Candidate 4 retry), but
that is explicitly NOT this outcome.

## 4. INTEGRATION SKETCH

Because the chosen outcome is an in-repo code fix, no new data transport
exists to integrate: the "output contract" is already satisfied — every feature
is produced per (ticker, hour) by `FeaturePipeline.compute` and already lands
in the observation at `environment.py:445-448` via the per-ticker keyed stats
in `models/{TICKER_ID}/{model_name}/normalization.npz`. The entire integration
is internal: (a) `prepare_episode` slices the frame, (b) `features.fit(window)`
registers ticker stats on that slice, (c) `_raw_feature_array` applies them,
(d) `paper_trade._build_observation` and `export.py --normalized` re-derive the
identical row, (e) the red test pins all four to one canonical z-scoring. If a
future outcome adds an external source, its JSON/JSONL per (ticker, timestamp)
lands through the existing `merge_extra_features` → `_SIGNAL_COLUMNS` → signals
group (`data.py:168-171`, `features.py:395-408`) and is then scaled correctly
because this fix made the observation pipeline normalize *any* incoming column.

## 5. RUNNER-UP OPTIONS AND WHY THEY LOST

- **Candidate 2 (market-data depth + clean splits)** — the stakeholder-level
  depth gap and a legitimate `NEW-DATA-SOURCE`-flavored improvement, but it is
  more ops than plumbing: the store root does not exist on this host, the
  `kraken-deep-history` seeder is not a flake input/PYTHONPATH (R2), and it
  needs a seed run + config keys. It also *depends on* the C1 look-ahead fix for
  its clean-split value to be composable. Lost on directness: it changes what
  feeds the pipeline; C1 changes the vector itself. Deferred; not dropped.
- **Candidate 4 (kraken-python retry/backoff + min_interval default)** — pure
  code and proven in-family, but it is a *sibling-repo* reliability fix with no
  observation impact, and one outcome must not span two repos. Weighing a
  safety fix against a signal fix, the red test forces C1 first.
- **Candidate 3 (scheduling + staleness + forward `funding_rate_prediction`)**
  — cheap and protects the three signal seams, and `funding_rate_prediction`
  is a real zero-cost forward-looking win, but it is operational (timers,
  flake inputs) and depends on C1 to be usable at scale. Runner-up for any
  phase-2 follow-on.
- **NEW-DATA-SOURCE overall (Candidates 5 microstructure / 7 on-chain)** —
  lowest directness-vs-cost per AUDIT §4; both need recording/consolidation
  machinery and would only make sense after the observation pipeline is
  normalized. Explicitly not the right first move.

---

DECISION COMPLETE