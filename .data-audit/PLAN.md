# PLAN — data-pipeline pass 2 (2026-09-30): make the three signal seams sound

Author: reviewer, audit-pipeline team, Phase 6 (2026-10-01). Overwrites the
prior pass's `PLAN.md`, which is preserved in full in §6 below. Outcome type
**IMPROVE-EXISTING**; target **Candidate 1** per `DECISION.md` §2 — AUDIT.md
A1–A4, the four defects in the one seam every exogenous column travels through.

> **Numbering note.** Candidate numbers follow **this** pass's `DECISION.md`
> §6 (Candidate 2 = spread widening, 3 = window recording, 4 = schedulers +
> flake, 5 = fetch reliability). The *prior* pass (§6) used a different
> numbering; where they collide, this pass's `DECISION.md` wins.

The gate verdict and all evidence live in `VALIDATION.md`. **GATE: PASS**
(119 tests green, `nix flake check` green, Leg A unchanged at 49-wide,
Leg B widened 49→60 through the CLI with the freshness pair delivered).

---

## 1. What was audited this pass

- **The seam, end to end, as shipped:** `rl/data.py::merge_extra_features` →
  `read_ohlc_dataframe` → `FeaturePipeline.compute` → `_raw_feature_array` →
  `_observe` → `PPO(MlpPolicy)`, plus all four consumers that call the merge
  (`train.py:190`, `backtest.py:143`, `paper_trade.py:294`, `export.py:171`).
- **The four defects (AUDIT.md A1–A4), each independently reproduced:**
  - **A1 ticker-blind join** — a `BTC_USD` signal file merged onto an ETH frame
    with no error and no log. Every one of the 8 pre-existing merge tests wrote
    records with *no* `ticker` field, so the join key was untested.
  - **A2 duplicate-hour crash** — `signal_df.reindex(ohlc_index)` raised
    `ValueError: cannot reindex on an axis with duplicate labels`, and the
    sibling's own documented hourly-append cron
    (`ticker-news-signals/INTEGRATION.md:114-120`) is exactly what produces it.
    A second pull inside the same hour killed every train, backtest, paper tick
    and export.
  - **A3 unbounded ffill, no freshness** — `.ffill().fillna(0.0)`; one record at
    h0 propagated unchanged to h1, h2, … Funding settles ~8-hourly and
    news/social are hand-pulled, so multi-hour gaps are the *normal* case and
    the agent could not tell a 3-day-old `funding_rate` from a current one.
  - **A4 absence == a genuine extreme** — `_FNG_MISSING = 0` collides with the
    low end of the real 0–100 scale ("extreme fear"), `_tilt` returns `0.0` for
    "no tagged message" which is also "perfectly balanced", and the merge's
    `fillna(0.0)` then made *no record at all* identical to *neutral*.
- **Why these four and not another source:** they are **upstream of every other
  candidate on the board**. A2 mechanically blocks Candidate 4 (you cannot ship
  the hourly cron onto a seam that crashes on the second pull of the hour);
  Candidate 2 (activating `spread`) rides the same allow-list and would
  forward-fill an 8-hourly snapshot across a session — precisely the A3 defect;
  and Candidate 3's A/B measures feature values whose correctness A1/A3
  determine.
- **Verified in Phase 6 against the live keyless Kraken API**, all four defects
  shown fixed through the packaged CLI: BTC record absent from the ETH frame and
  a BTC-only file now exiting 1; duplicate hour merged last-write-wins with no
  raise; `signal_age_hours` capped at the derived bound; unobserved bars
  distinguishable from zero. See `VALIDATION.md` §3.

## 2. What was built

**On master before Phase 6:**

| Commit | Contents |
|---|---|
| `5951f72` | **Builder A1–A4.** `merge_extra_features` rewritten around `_filter_ticker` / `_bar_hours` / `_resolve_max_age_hours` / `_signal_ages` / `_combine_freshness`; `_SIGNAL_COLUMNS` gains the freshness pair with `_SIGNAL_FRESHNESS_COLUMNS` as the single source of the names; the two new config keys in `configs/default.yaml` **and** `configs/deep-history.example.yaml`; 6 new tests in `tests/test_rl_data_store.py`. (+760/−31) |
| `1e404ba` | **Integrator wiring + carry-bound fix.** All four consumers thread `signal_max_age_hours` / `signal_require_ticker` through to the merge; the carry bound is applied by masking on the *measured age* rather than `ffill(limit=N)` (a row-count limit only equals an hour-count on bars of an hour or longer — on 15m/5m bars `limit=N` carries a reading N bars *past* its whole hour); `_signal_ages` measures age against the unfloored bar index so sub-hourly bars inside one record-hour stop all reporting age 0; `tests/test_rl_signal_config_wiring.py` (+543) pins the whole config→observation path. (+586/−18) |

Net: `106 → 119` tests, **no new dependency** (stdlib + the pandas already
required by the package), no new repo, no submodule.

**In Phase 6:** validation only — two documents, no source changes, no new
tests. Scratch configs/fixtures/models/exports live under `/tmp/audit-sig` and
were never committed.

## 3. Integration results (summary — full evidence in `VALIDATION.md`)

| Leg | Config | Signal cols on frame | Observation | Train | Backtest |
|---|---|---|---|---|---|
| **A** (control) | all three keys `null` | **0** | **49** | OK, 721 bars / 49 obs | 697 steps, +3.10 %, Sharpe 0.469, DD 4.55 %, 531 trades |
| **B** | three scratch JSONL fixtures | **11** | **60** | OK, 721 bars / 60 obs | 697 steps, +12.38 %, Sharpe 1.291, DD 5.17 %, 517 trades |
| B (news only) | one fixture | **5** | **54** | — | — |

Plus: Leg B paper-trade (1 tick, dry-run) OK; export 721 bars × 70 cols with
11 signal columns; `normalization.npz` carries `signal_age_hours`
(mean −0.5298 / std 0.5954) and `signal_observed` (mean 0.4175 / std 0.4931),
all finite — the `-1.0` sentinel keeps "no reading" out of the z-scoring.

*No convergence claim is made from the Leg A vs Leg B returns: 721 bars / 1
seed / 3,000 timesteps makes that difference seed noise.*

## 4. What was deferred

None of these were dropped; each is filed with what must happen first.

1. **Candidate 2 — activate `spread` (49→51, zero new API calls).**
   `bid`/`ask` are already emitted at `kraken_funding_rates/models.py:47-48,62-63`
   and dropped at the merge allow-list. **Deferred by ordering, not value**: it
   rides the allow-list this pass repaired, and an 8-hourly snapshot
   forward-filled into `spread` *is* the A3 defect. It is now safe — the next
   widening is one line in `_SIGNAL_COLUMNS`. The `bid_vol`/`ask_vol` half also
   needs a recorder (Candidate 6). Guard it with the RESEARCH-2 width check
   (`feature_names` already in the npz).
2. **Candidate 3 — record the training window, then split it** (RESEARCH-1's
   fully-specced design: one `data_window: {since, until}` block after
   `market_data_store`, four one-line caller edits). Independent of this pass;
   sequence it *after* this one so the A/B it enables measures a correct
   observation. Includes the bonus bug: **`until` is silently dropped on the
   live null-store path** (`data.py`) — one-line fix + test, file it here where
   `until` starts mattering.
3. **Candidate 4 — schedule the three signal projects + put them in the flake.**
   `flake.nix` cannot run the other three CLIs; all three config keys are null.
   **The hourly-append cron is the natural acceptance test for A2** and is now
   safe to schedule.
4. **Candidate 5 — fetch reliability** (retry/backoff + bar cache; RESEARCH-3's
   measured answer: Kraken rate-limits in the *body* with HTTP 200 so
   `urllib3.Retry(429)` never fires; `from_env(min_interval=)` is a silent no-op;
   the throttle is ~300× under limit, so the real cost is the re-fetch and the
   3 O(file) JSONL parses per tick). **Sibling-repo boundary** — the
   load-bearing half is a `transport.py` change. Not gate evidence, not
   observation content.
5. **A5 — defaults drift** (`_FEATURE_GROUPS` re-spelled 4×, `pages=6` ×5,
   `export.py` fifth spelling, `[1, 4, 24]` ×4). Real, cheap, orthogonal
   bookkeeping. Filename-free follow-up.
6. **A4 source-side in `kraken-social-signals`** (`_FNG_MISSING = 0` at
   `pipeline.py:38,136`; `_tilt` `0.0` at `:52-62`). The bot-side
   `signal_observed` flag makes the ambiguity *harmless* from here, so the
   sibling edit is an upstream improvement deliberately not bundled.
7. **Run the `kraken-deep-history seed`** — still ops, not code, still un-run on
   this host. Store root absent and `market_data_store: null` everywhere.
8. **Widen the `signal_max_age_hours` doc** — the measured finding in
   `VALIDATION.md` §4: at the `null` default an 8-hourly funding source
   contributes a live reading to only 76/721 bars. One comment line in
   `configs/default.yaml`. **This is the only Phase 7 item arising from this
   pass's own findings.**

## 5. Concrete next steps — fully wiring signals into train/backtest

The pass made the seam *sound*; it did not turn the signals *on*. Here is the
route from "sound" to "the signals are in the model", in order.

1. **Get real signal files onto the keys** (ops, no code). Today all three
   `*_features_file` keys are `null` in `configs/default.yaml`, so the seam is
   exercised only when a user points it somewhere. Fetch one ticker per sibling
   CLI into a scratch dir, point the keys at it, and confirm the observation
   width moves 49→54/60 — exactly what `VALIDATION.md` §2 did with hand-written
   fixtures.
2. **Set `signal_max_age_hours` per source, deliberately.** The bound is global
   across all three files, so a single value has to serve an hourly news pull
   and an 8-hourly funding settle. With the default (`null` → 1 h) funding is
   zero-filled ~89 % of the time; `12` restores it and is measured in
   `VALIDATION.md` §4. **If per-source bounds turn out to be necessary, that is
   a small schema change** (`extra_features_max_age_hours`, …) and should be
   filed against Candidate 4, not smuggled in here.
3. **Decide the funded-vs-fresh tension in the reward, or accept it.** A
   funding-heavy run now sees mostly zeros with `signal_observed = 0`. Either
   the observation learns to gate on `signal_observed` (it has both columns now,
   which is the point of A3/A4) or the funding channel is mostly inert. Worth
   one explicit decision, recorded, not left implicit.
4. **Re-fit any model trained before this pass.** Widening the observation
   invalidates the policy input space; `models/` is empty on this host so there
   is no retrain burden today, but the first pre-`5951f72` model must be
   *deleted*, not backtested. No automation guards this yet — the cheap guard is
   the RESEARCH-2 width check (`feature_names` in `normalization.npz` vs the
   live pipeline's columns) surfacing a clear message at
   `scan_model` / `RLAgent.load`.
5. **Then Candidate 2 (`spread`).** One line in `_SIGNAL_COLUMNS`, riding a
   merge seam that is now ticker-filtered, de-duplicated, bounded and
   freshness-annotated. Cheapest width win on the board and the natural payoff
   for this pass.
6. **Then Candidate 3 (window recording + `until` fix)**, so the next
   A/B — "did `spread` and the freshness columns earn their 2 columns?" — is
   measured on a recorded, reproducible window instead of whatever the live
   endpoint returned that minute (prior pass measured a 16 % swing in one
   feature's fitted std between two snapshots of the "same" span).
7. **Standing constraint.** Scratch models, stores, fixtures and exports go to
   `/tmp` and are never committed. A training window must be **recorded, not
   just fetched** — Candidate 3 is what makes that possible.

## 6. Prior pass (2026-09-30, pass 1) — preserved

*Outcome **IMPROVE-EXISTING**, target **Candidate 1** = the
`normalization.npz` stack that `train.py` saved but that nothing applied to the
observation. Landed on master as `2b3e065` (WIP `export-data`), `5df99ef` (the
fix), `1568771` (5 regression tests + README). 101 → 106 tests. Gate: **PASS**.*

What it audited: `rl/data.py` (`read_ohlc_dataframe`, `prepare_episode`) →
`rl/features.py` (`FeaturePipeline.compute/fit/transform`,
`NormalizationStats`) → `rl/environment.py` (`_raw_feature_array` → `_observe`)
→ `rl/train.py` / `backtest.py` / `paper_trade.py` / `export.py`. The defect:
fitted per-ticker stats were persisted next to every model and read back by
`backtest.py`/`paper_trade.py`, but never shaped the observation — the policy
trained and traded on raw heteroscaled float (`obv` at ±55,000 in the same
`Box` as `return_1` at ±0.01).

What was deferred then, and its status now:

1. **Market-data depth + clean splits** (`since`/`until` plumbing, the
   `kraken-deep-history` seeder, store-backed reads) — still deferred; now
   tracked as this pass's **Candidate 3** (§4.2) plus the deep-history seed
   (§4.7). Pass 1 turned the case from assumption into measurement: with
   `market_data_store: null`, **a training window is whatever the live endpoint
   returns at that moment**, so a model's `normalization.npz` is not
   reconstructible afterwards. One CLI train and a later fetch of the same
   721-bar span disagreed by 16 % on `rsi_24`'s fitted std; two later fetches
   agreed to 1.2e-4 and a repeat train reproduced a fresh fit to 6.5e-7. The
   live path is deterministic; the *snapshot* is not.
2. **`kraken-python` retry/backoff** (`transport.py` raises
   `RequestException`/`RateLimitError` immediately) — sibling-repo change, no
   observation impact, still the cheapest safety win. Now tracked as this pass's
   **Candidate 5** (§4.4), with RESEARCH-3's measured answer attached.
3. **Scheduling + staleness + forward `funding_rate_prediction`** — operational;
   protects the three signal seams. Now split: staleness **shipped** by this
   pass (`signal_age_hours`/`signal_observed`); scheduling is **Candidate 4**
   (§4.3).
4. **NEW-DATA-SOURCE candidates (microstructure, on-chain)** — lowest
   directness-vs-cost; both only became worth building *because* pass 1 made
   the observation normalize any incoming column. That precondition holds, and
   `spread` (Candidate 2) is now the near-term instance of it.

Open items pass 1 filed that are still open:

- **Persist the fitted-stats shape in `config.yaml` provenance.** `config.yaml`
  records `feature_windows` and `feature_groups` but not the feature *count* the
  npz was fitted on, so a model whose groups were edited after training
  silently loads a 49-column npz against a 50-column pipeline. **This pass
  raises the stakes**: `VALIDATION.md` §2 shows the width legitimately moving
  49 → 54 → 60 with config alone, so a stale-width model is now an easy mistake
  to make, not a theoretical one. Cheapest fix: record `n_features` at train
  time and have `scan_model`/`RLAgent.load` refuse a mismatch with a clear
  message.
- **Make `feature_groups` provenance exact, not just present.** `_SIGNAL_COLUMNS`
  is imported by `data.py` from `features.py` (this pass kept that
  centralization — the allow-list cannot drift), but the *group-to-column*
  mapping is still spelled by hand.
- **Re-fit existing models** — no burden today (`models/` is empty), but the
  first model trained before `5df99ef` must be discarded, not backtested.
- **A real convergence A/B** — deliberately skipped at 721 bars / 1 seed, for
  the same reason this pass declined to read its Leg A vs Leg B returns. Do it
  properly once a store exists: multi-seed, multi-thousand-bar windows,
  reporting a reward trajectory rather than one number.
- **Ticker resolution** — `--ticker ETH_USD` works; `--ticker USD_SOL` fails
  with `Unknown Kraken pair: 'USD/SOL'`. Still unresolved, and worth fixing
  before any pass that assumes USD-quoted pairs resolve from the CLI's
  `USD_`-prefix form.

## 7. Standing constraints for any future pass

- A training window must be **recorded, not just fetched** — the
  reproducibility gap measured in the prior pass is the concrete cost of not
  doing so.
- Scratch models, stores, fixtures and exports go to `/tmp` and are **never
  committed**.
- `nix flake check` is cheap here (~seconds) and was green on both this pass
  and the last — keep it in the gate rather than treating it as heavy.