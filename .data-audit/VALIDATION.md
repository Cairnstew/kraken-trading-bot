# VALIDATION — kraken-trading-bot data-pipeline pass (2026-09-30, pass 2)

Phase 6. Outcome type **IMPROVE-EXISTING**; target **Candidate 1 — make the
three existing exogenous signal seams sound** (AUDIT.md A1–A4, DECISION.md §2).
Bots' work landed on master: `5951f72` (builder A1–A4) and `1e404ba`
(integrator config wiring + the sub-hourly carry-bound fix). Baseline suite
**106 passed** → **119 passed** after the pass.

This is **not** a new data source and **not** a store-backed source, so the
store-adapted Phase 6 template (persistence, verify-contiguous, bars-vs-721-
ceiling) does not apply. The gate shape is the adapted in-repo one the prior
pass recorded: **(a)** feature-width proof, **(b)** improvement-is-consumed
proof, **(c)** equivalent-metrics gate = all tests green + `nix flake check`
green + no regression in the observation shape at the new keys' defaults.

The question this document answers is not *"are the new functions tested?"*
(they are, in `tests/test_rl_data_store.py` and
`tests/test_rl_signal_config_wiring.py`). It is: **does the seam actually change
what the policy sees, through the CLI/config path, and does it leave the shipped
defaults untouched?**

---

## 1. UNIT VALIDATION

| Check | Command | Result |
|---|---|---|
| Full suite | `nix develop --command bash -c "pytest tests/ -q"` | **119 passed**, 20 warnings, **20.17 s** ✅ |
| Flake | `nix flake check` (from this repo) | **all checks passed** — `packages` (2 derivations), `nixosModules.default`, `devShells.x86_64-linux.default` ✅ |

Flake note: `nix flake check` reports *"The check omitted these incompatible
systems: aarch64-darwin, aarch64-linux, x86_64-darwin"*. That is the default
host filter, not a failure, and `--all-systems` was not needed for this gate.

`119 − 106 = 13` new tests, all from this pass: 6 in
`tests/test_rl_data_store.py` (A1 ticker mismatch does not merge; filters to
requested ticker; untagged file still merges; duplicate hour does not raise;
ffill bounded + age grows; absence distinguishable from zero) and 7 in
`tests/test_rl_signal_config_wiring.py` (YAML keys reach the merge through
`export`; mispointed config raises through a consumer; freshness columns reach
the z-scored observation; freshness survives the no-`since` read; null max-age
is one hour of carry on any bar interval; every consumer threads both keys;
`paper_trade` deliberately passes no `since`).

---

## 2. INTEGRATION-TEST MATRIX — both legs actually ran

Every row is a real run from this repo inside `nix develop` against the live
keyless Kraken API, driven through the packaged `kraken-trading-bot` CLI. No
mocks. Scratch configs, fixtures, models and exports live under `/tmp` and were
never committed (`git status` shows only `.data-audit/*.md`).

### FEATURE-WIDTH TABLE (the gate's core measurement)

| Config | Signal files | `signal_*` cols on frame | Observation width | Δ vs Leg A |
|---|---|---|---|---|
| **Leg A** (`/tmp/audit-sig/legA.yaml`) | all three `null` (shipped defaults) | **0** | **49** | — (control) |
| Leg B, news only | `eth_news.jsonl` | **5** (3 news + 2 freshness) | **54** | **+5** |
| **Leg B** (`/tmp/audit-sig/legB.yaml`) | all three fixtures | **11** (9 values + 2 freshness) | **60** | **+11** |
| Leg B, max-age 12 | all three fixtures | **11** | **60** | +11 (bound only, not width) |

The `signal` column is the CLI's own `export` accounting (`stages: 1 timestamp,
8 OHLCV, N signal, M observation (raw), 0 normalized, warmup flag`). **This is
the paired control the gate asks for: the freshness columns arrive *with* a
signal file, and with all three keys at their shipped defaults the observation
is byte-for-byte the same width it was before this pass (49).**

### Leg A — defaults unchanged, no regression (the equivalent-metrics-baseline leg)

| Step | Command (abbrev) | Result |
|---|---|---|
| Train | `kraken-trading-bot train --ticker ETH_USD --model ppo_seam_legA --config /tmp/audit-sig/legA.yaml --pages 2 --timesteps 3000 --models-root /tmp/audit-sig/models-legA` | **OK** — `Training PPO on 721 bars of ETH_USD (49 obs features, 3000 timesteps)` |
| Backtest | `kraken-trading-bot backtest --ticker ETH_USD --model ppo_seam_legA --pages 2 --models-root …` | **OK** — 697 steps, return **3.10 %**, Sharpe **0.469**, max DD **4.55 %**, trades **531**, win **0.00 %**, final equity **10,310.32** |
| Export | `kraken-trading-bot export-data --ticker ETH_USD --pages 2 --config …/legA.yaml --output …/legA.csv` | **OK** — 721 bars × 59 cols, **0 signal**, 49 observation |

**No observation-width mismatch, no `NotEnoughDataError`, no traceback, no
signal/freshness column at all.** The 49-wide `normalization.npz` carries
`signal_* : []` — i.e. with the shipped defaults the pass is a no-op on the
observation, which is exactly what an IMPROVE-EXISTING in-repo seam fix must be.

### Leg B — the seam actually feeds the observation (outcome consumed)

Scratch fixtures in `/tmp/audit-sig/signals/`, hand-written per the sibling
schema (`ticker`, `timestamp`, and that source's columns):

- `eth_news.jsonl` — 303 records, hourly, `sentiment_score` / `article_count` /
  `novelty_flag`, all `ticker: "ETH/USD"`. Deliberately seeded with **(i) one
  duplicate-hour record** (a second write into the newest hour, the exact shape
  the sibling's documented hourly-append cron produces) and **(ii) one
  `ticker: "BTC/USD"` record** carrying an extreme `sentiment_score: -0.9`,
  `article_count: 777`.
- `eth_funding.jsonl` — 38 records at **8-hourly** cadence (`kraken-funding-
  rates`' real settling cadence), `funding_rate` / `basis` / `open_interest`.
- `eth_social.jsonl` — 301 records, hourly, `stt_mention_count` / `stt_tilt` /
  `fng_index`.

| Step | Command (abbrev) | Result |
|---|---|---|
| Train | `kraken-trading-bot train --ticker ETH_USD --model ppo_seam_legB --config /tmp/audit-sig/legB.yaml --pages 2 --timesteps 3000 --models-root /tmp/audit-sig/models-legB` | **OK** — `Training PPO on 721 bars of ETH_USD (60 obs features, 3000 timesteps)` |
| Backtest | `kraken-trading-bot backtest --ticker ETH_USD --model ppo_seam_legB --pages 2 --models-root …` | **OK** — 697 steps, return **12.38 %**, Sharpe **1.291**, max DD **5.17 %**, trades **517**, win **0.00 %**, final equity **11,238.22** |
| Paper-trade | `kraken-trading-bot paper-trade --ticker ETH_USD --model ppo_seam_legB --iterations 1 --dry-run --models-root …` | **OK** — 1 tick, `side=buy volume=0.05930682 price=2685.02`, equity 10,000 |
| Export | `kraken-trading-bot export-data --ticker ETH_USD --pages 2 --config …/legB.yaml --output …/legB.csv` | **OK** — 721 bars × 70 cols, **11 signal**, 60 observation |
| Export (news only) | same with `…/legB_news_only.yaml` | **OK** — 721 bars × 64 cols, **5 signal**, 54 observation |

**The observation widened 49 → 60 through the CLI/config path, and a PPO policy
trained, backtested and paper-traded on the widened vector.** That is the
improvement-is-consumed proof at the integration level: all four consumers
(`train`, `backtest`, `paper_trade`, `export`) thread
`signal_max_age_hours` / `signal_require_ticker` through to
`merge_extra_features` (`train.py:190-194`, `backtest.py:143-147`,
`paper_trade.py:294-298`, `export.py:171-175`).

---

## 3. THE FRESHNESS PAIR, AS DELIVERED TO THE AGENT

Read out of the Leg B **export CSV** — a real observation-shaped artifact
produced by the CLI, not a unit-test fixture:

| Measurement | Value |
|---|---|
| `signal_age_hours` present on frame | ✅ (range `[-1.0, 1.0]`, distinct `{-1.0, 0.0, 1.0}`) |
| `signal_observed` present on frame | ✅ (values `{0.0, 1.0}`; **301/721** bars observed = 41.7 %) |
| observed/unobserved separation | the 420 bars before the fixtures' 301-hour window are `signal_age_hours = -1.0`, `signal_observed = 0.0` — and their `sentiment_score` is `0.0`, so **"no record" is now distinguishable from a genuine 0 / balanced reading** (A4) |

### The z-scored observation carries them

`normalization.npz` written by the Leg B train (`allow_pickle=False`):

| Property | Leg A | Leg B |
|---|---|---|
| `means.shape` / `stds.shape` / `len(feature_names)` | (49,) (49,) **49** | (60,) (60,) **60** |
| `signal_*` in `feature_names` | **`[]`** | **`['signal_age_hours', 'signal_observed']`** |
| signal value cols in `feature_names` | `[]` | all 9 (news 3 + funding 3 + social 3) |
| `signal_age_hours` mean / std | — | **−0.529820 / 0.595416** |
| `signal_observed` mean / std | — | **0.417476 / 0.493143** |
| all means/stds finite | ✅ | ✅ |

The finiteness matters and is load-bearing: `_NO_SIGNAL_AGE = -1.0` is a finite
sentinel precisely so that "no reading" cannot reach
`NormalizationStats.normalize` as a NaN and poison the whole z-scored vector.
A non-trivial `std` on both columns (`0.595` / `0.493`) also proves the
freshness pair is *doing work* in the normalization, not a degenerate constant.

`config.yaml` provenance (read back by `backtest`/`paper_trade`) carries both
new keys on both legs — Leg A: `signal_max_age_hours: None`,
`signal_require_ticker: True`; Leg B: the same plus the three fixture paths.

### All four A1–A4 defects, reproduced/fixed through the live CLI path

| Defect (DECISION §2) | Live-path evidence |
|---|---|
| **A1 ticker-blind join** | The BTC fixture record (`sentiment_score: -0.9`, `article_count: 777`) **never reaches the ETH frame**: exported `sentiment_score ∈ [-0.499999, 0.5]`, `article_count ∈ [0.0, 99.0]`. A **BTC-only** file against ETH now **hard-fails through the CLI**: `Signal file …/btc_only.jsonl holds no records for ETH/USD (contains: BTCUSD)` — CLI **exit code 1**, no CSV written. Before this pass that was a silent merge. |
| **A2 duplicate-hour crash** | `eth_news.jsonl` deliberately contains two records in the newest floored hour. **No `ValueError: cannot reindex on an axis with duplicate labels`** on any of the six Leg-B runs, and the newest bar's `sentiment_score` is **−0.42** — the *last written* record for that hour, i.e. last-write-wins as specified. |
| **A3 unbounded ffill, no freshness** | `signal_age_hours` **maxes at 1.0** and never grows without bound; an 8-hourly funding source contributes a live reading on only **76/721** bars (each record covers 2 bars at the 1 h bound: age 0 and age 1) instead of being forward-filled across the whole 300-hour fixture window. |
| **A4 absence == a genuine extreme** | The 420 bars outside the fixture window carry `signal_observed = 0.0` / `signal_age_hours = -1.0` alongside a zero-filled `fng_index`/`sentiment_score`, so the zero fill is no longer ambiguous. 225 bars additionally have `funding_rate == 0.0` **while `signal_observed == 1.0`** (the hourly sources kept the shared flag true) — a visible, non-ambiguous "this source had nothing live here". |

---

## 4. ONE FINDING WORTH RECORDING: `signal_max_age_hours: null` silently zeroes a funding-heavy user

This is the interpretation the integrator flagged, and it is **measured**, not
argued. Same fixtures, same 721-bar window, one config key changed:

| `signal_max_age_hours` | funding bars with a live reading | `signal_age_hours` range | observation width |
|---|---|---|---|
| `null` (default → derived **1 h**) | **76 / 721** (10.5 %) | `[-1.0, 1.0]` | 60 |
| `12` | **301 / 721** (41.8 %) | `[-1.0, 7.0]` | 60 |

`kraken-funding-rates` settles ~8-hourly, so under the derived 1-hour bound
**≈89 % of the funding channel is zero-filled** for a funding-heavy user who
previously got unbounded carry. The observation width is unaffected (60 either
way) and `signal_observed` / `signal_age_hours` make the gap *visible* rather
than silent — which is the A3 fix working as designed — but the funding
*value* itself is gone from those bars.

**My read: acceptable-as-documented, with one caveat that is a documentation
task, not a code task.**

- It is the **safe** direction. The alternative (`null` = unbounded) is the
  behaviour this pass exists to remove: an 8-hourly funding snapshot carried for
  three days reads as current. Silently keeping unbounded carry would
  reintroduce A3 behind the new columns.
- The remedy is **one config key and is already documented in
  `configs/default.yaml:97-99`** (*"null (default) derives the bound from the
  bar interval — one hour of carry … Set an int to widen it
  (kraken-funding-rates settles ~8-hourly, so a funding-heavy run wants e.g.
  12)"*), and `configs/deep-history.example.yaml` carries the same keys.
- The caveat: the doc says *"wants e.g. 12"* but does **not** say what a user
  who leaves it at `null` actually loses. The honest one-line addition is
  *"with the default, an 8-hourly funding source contributes a live reading to
  ~2 bars out of every 8 and is zero-filled for the other 6 — if you train on
  funding, set this key."* That belongs to Phase 7 (a comment in
  `configs/default.yaml`), and I am read-only on code, so it is filed rather
  than applied.

---

## 5. GATE VERDICT

| Clause | Requirement | Evidence | Verdict |
|---|---|---|---|
| (a) feature-width proof | observation unchanged at 49 with no signal file; freshness columns arrive with signals (49→54 news-only, 49→60 all three); z-scored observation separates observed/unobserved | §2 width table; §3 npz (Leg A 49 with `signal_* : []`, Leg B 60 with the pair, finite, non-trivial std) | ✅ |
| (b) improvement-is-consumed | a train/backtest-style path reading through the merged frame sees the freshness pair | §2 Leg B: **real CLI train at 60 obs features**, backtest 697 steps, paper-trade 1 tick, export 11 signal columns; all four consumers thread both keys | ✅ |
| (c) equivalent-metrics gate | all tests green + `nix flake check` green + no regression in the observation shape at the new keys' defaults | §1: **119 passed**, **flake all checks passed**; Leg A trains at 49-wide and backtests cleanly against a null-config | ✅ |

No trusted-config behaviour change: with all three `*_features_file` keys at
their shipped `null` default the merge never runs, the two new keys are inert,
and the observation is 49-wide exactly as before the pass.

**GATE: PASS**

*(The Leg A vs Leg B backtest numbers — 3.10 % vs 12.38 % return, 531 vs 517
trades — are recorded for completeness and are explicitly **not** evidence. At
721 bars / 1 seed / 3,000 timesteps a return difference is seed noise; this
document makes no convergence claim, for the same reason the prior pass declined
its A/B.)*

---

## 6. REPRODUCING THIS

```bash
nix develop --command bash -c "pytest tests/ -q"     # 119 passed
nix flake check                                     # all checks passed

S=/tmp/audit-sig            # scratch root; fixtures + configs generated there
# Leg A (defaults):  all three *_features_file null, both new keys at default
nix develop --command kraken-trading-bot train \
  --ticker ETH_USD --model ppo_seam_legA --config $S/legA.yaml \
  --pages 2 --timesteps 3000 --models-root $S/models-legA
nix develop --command kraken-trading-bot backtest \
  --ticker ETH_USD --model ppo_seam_legA --pages 2 --models-root $S/models-legA

# Leg B (seam on):    the three scratch JSONL fixtures
nix develop --command kraken-trading-bot train \
  --ticker ETH_USD --model ppo_seam_legB --config $S/legB.yaml \
  --pages 2 --timesteps 3000 --models-root $S/models-legB
nix develop --command kraken-trading-bot backtest \
  --ticker ETH_USD --model ppo_seam_legB --pages 2 --models-root $S/models-legB
nix develop --command kraken-trading-bot paper-trade \
  --ticker ETH_USD --model ppo_seam_legB --iterations 1 --dry-run \
  --models-root $S/models-legB
nix develop --command kraken-trading-bot export-data \
  --ticker ETH_USD --pages 2 --config $S/legB.yaml --output $S/exports/legB.csv
```

Ticker note carried forward from the prior pass: `--ticker ETH_USD` works,
`--ticker USD_SOL` fails with `Unknown Kraken pair: 'USD/SOL'`. `ETH_USD` is
what `configs/default.yaml` itself sets.

Scratch artifacts (`/tmp/audit-sig/**`, `/tmp/audit-flake-check.log`) are
outside the repo and were **not** committed. The only files this pass writes are
`.data-audit/VALIDATION.md` and `.data-audit/PLAN.md`; no source file was
modified.

### Test-suite delta attributable to this pass

`106 → 119 passed` is `5951f72` + `1e404ba`: 6 new tests in
`tests/test_rl_data_store.py` and 7 in
`tests/test_rl_signal_config_wiring.py`. Phase 6 added **no** tests and changed
**no** code — it is validation only.