# RESEARCH-2 — G2: pin and guard the fitted feature width at load

Read-only research pass (2026-09-30, tree `17899e1`, working tree clean).
Gap owner: **G2** (`AUDIT.md:149-171`, ranked candidate 2 at `AUDIT.md:300-308`).
No code changed; no commit. This file is the handoff artifact.

All claims below marked **[verified]** were executed against the installed
`.venv` (python 3.14.7, numpy 2.5.3, pandas 3.0.6, gymnasium 1.3.0,
stable-baselines3 2.9.0, torch 2.14.0+cu130) rather than inferred.

---

## 0. Headline: two corrections to the gap's premise

### 0.1 The `feature_names` persistence the audit asks for **already exists**

`AUDIT.md:157-159` and `§4 #2` ask to "persist `n_features` / the fitted
feature-name vector (e.g. an ordered `feature_names` array alongside
mean/std in the npz)". That array is already written and read:

- `NormalizationStats.feature_names` field — `features.py:59`
- written into the npz — `features.py:78` (`feature_names=np.asarray(self.feature_names)`,
  `'U'` dtype, `allow_pickle=False` on read at `features.py:100`)
- read back and rehydrated — `features.py:101,105,106`

**[verified]** `np.load(..., allow_pickle=False)` works, so the array is
pickle-free and safe. `tests/test_rl_environment.py:218-235` already
round-trips it and asserts `stats2.feature_names == stats.feature_names`.

**So the remaining work is an *assertion*, not a format change.** The npz
carries the ground-truth ordered column vector; nothing ever compares it
to what the live pipeline computes. That makes G2 far cheaper than the
audit frames it — no migration, no format bump, no retrain (models/ is
empty; `AUDIT.md:381`).

### 0.2 The `paper_trade` guard the audit credits is **tautological** for this threat

`AUDIT.md:154-155` and `§4 #2` rest the recommendation on "the paper trader
guards observation width (`paper_trade.py:325-338`) but backtest does not".
The guard exists, but **it cannot fire on the drift it was written for**,
because both sides of its comparison are produced by the *same* dropping
function:

- `expected` = `self.env.observation_space.shape[0]` — the env builds its
  space from `self._feature_matrix.shape[1]` (`environment.py:194-196`),
  and `_feature_matrix` comes from `_raw_feature_array()`
  (`environment.py:445-464`), which applies **`stats.normalize(filled)`**.
- `actual` = `_build_observation` (`paper_trade.py:320-323`), which applies
  **`stats.normalize(features)`**.

`NormalizationStats.normalize` iterates `self.feature_names` and silently
`continue`s on a missing column (`features.py:122-124`). A stale 49-name
npz therefore produces 49 columns on *both* sides of the comparison.

**[verified]** Reproduced end-to-end with the real classes (stale 49-name
stats, drifted 58-column compute with the 9 `_SIGNAL_COLUMNS` merged):

```
stale npz feature_names : 49
env.observation_space   = 49   (environment.py:194-196)
_build_observation row  = 49   (paper_trade.py:320-323)
--> _validate_observation raises? False  (paper_trade.py:325-338)
--> ground-truth compute width = 58 (drifted, unseen by the guard)
```

**Consequence for the plan:** the work item is *not* "port paper_trade's
guard to backtest" — that would port a tautology into a second place. The
guard has to be re-anchored to an independent source of truth (the npz's
`feature_names` vs the freshly computed columns), and then the corrected
version replaces the tautological one at **both** call sites.

The existing test gives false confidence: `test_observation_width_mismatch_raises_clear_error`
(`tests/test_rl_paper_trade.py:241-254`) monkeypatches `compute` to return
*one column fewer*, which makes `actual < expected` — the one direction the
tautology does not mask. The real drift direction (`actual == expected`,
both shrunken) is untested.

### 0.3 stable-baselines3 validates the space at load, and it is also blind here

**[verified]** `BaseAlgorithm.load` unconditionally calls
`check_for_correct_spaces(env, data["observation_space"], data["action_space"])`
(`stable_baselines3/common/base_class.py`), whose body is a full `Space` `!=`
comparison. There is no opt-out parameter in 2.9.0 (signature:
`path, env, device, custom_objects, print_system_info, force_reset, kwargs`).

```
LOAD env=7 {}                        -> RAISED ValueError: Observation spaces do not match: Box(-inf, inf, (5,), float32) != Box(-inf, inf, (7,), float32)
LOAD env=7 {'check_obs_space': True}  -> RAISED (same)
LOAD env=3 {'check_obs_space': False} -> RAISED (same)
```

So the audit's "would fail either cryptically deep in SB3 or, worse,
silently" resolves as: **it fails clearly when widths differ, and passes
silently when the width is right but the columns are wrong** — which is
precisely the G2 case, because the env's space is itself derived from the
shrunken matrix. SB3 guards the *policy↔env* contract; G2 is the
*stats↔compute* contract. They are orthogonal and must not be conflated.

---

## 1. RECOMMENDED DESIGN

Three layers, ordered by cost-to-value. **Layer 1 is the whole fix**; 2 and 3
are cheap add-ons that cover the cases 1 provably cannot see.

```
Layer 1  npz feature_names  vs  freshly computed columns   -> hard fail   (~15 LOC, 1 helper)
Layer 2  config.yaml n_features vs npz feature_names      -> pre-flight  (~8 LOC, 2 seams)
Layer 3  feature_fingerprint (config + code constants)    -> provenance  (~10 LOC, same seam)
```

### Layer 1 — the invariant, enforced inside the pipeline (covers all 4 consumers)

The check belongs in `FeaturePipeline`, not in each consumer, because
`compute()` is the single funnel every consumer passes through
(`features.py:264-299`): `fit` (`:207`), `transform` (`:241`), the env
(`environment.py:179-181`), the paper trader's row rebuild
(`paper_trade.py:320`), and export (`export.py:200`).

**Add the pure comparison as a method on `NormalizationStats`**
(next to `normalize`, `features.py:108-128`):

```python
def assert_matches_frame(self, frame: pd.DataFrame, *, context: str = "") -> None:
    """Hard-fail when the computed frame's feature set != the fitted set."""
```

* compare `set(frame.columns)` vs `set(self.feature_names)` → **hard `ValueError`**
* compare `list(frame.columns)` vs `self.feature_names` → **log a warning only**

The asymmetry is deliberate and worth stating in the docstring: `normalize`
builds its output by iterating `self.feature_names` (`features.py:122`), so
if the *sets* match the row is re-ordered into the *fitted* order — i.e. the
policy still receives its trained vector. **Set mismatch is fatal; order
mismatch is benign-but-suspicious** (in practice order only moves when
*code* changes, not config). Do not hard-fail on order; it buys nothing and
would block a legitimate re-order that `normalize` already absorbs.

**Call it from `FeaturePipeline.compute`**, right where
`self._last_feature_names` is set (`features.py:298`), gated so it:

* fires only when stats were **loaded** (not being fitted) — otherwise
  `fit()` (`features.py:207` → `compute`) would compare a frame against
  stats it is in the middle of creating;
* runs **once per pipeline instance** (a `_stats_validated: set[str]`
  keyed by ticker, cleared by `set_stats`/`load_normalization`
  (`features.py:308-310, 327-336`)).

Cost: negligible. It runs per window compute, and the paper trader
recomputes the whole window per 60 s tick anyway (`AUDIT.md:183-185`).

Why this seam and not `normalize()`: `normalize` is the hot path (per tick,
per transform). Raising there would be correct but fires *late* (after the
network fetch) and cannot name the offending config keys. Keep `normalize`
unchanged — it is a pure z-score, and its "drop" behaviour is legitimate for
a frame that legitimately lacks an optional column. Put the assert one level
up, in `compute`.

**Bonus coverage, free:** `export.py:202-207` builds
`pd.DataFrame(z, columns=[f"z_{name}" for name in computed.columns])` where
`z` came from `transform` (which follows the npz names). Under a stale npz
the two widths disagree and pandas raises a raw
`Shape of passed values is (n, 49), indices imply (n, 58)`. Layer 1 converts
that accidental, cryptic crash into the same clear message. It is currently
a de-facto width assertion that nobody wrote on purpose.

### Layer 2 — `n_features` provenance in `config.yaml` (pre-flight, before any fetch)

**Write seam — `train.py:238-240`.** `register_model` dumps the dict it is
handed verbatim (`registry.py:147-150`), so adding keys to `cfg` before the
call is free and needs no `configs/default.yaml` change:

```python
features.save_normalization(ticker_key, agent.save_dir / "normalization.npz")   # train.py:238
cfg["n_features"]   = int(env.observation_space.shape[0])                       # new
cfg["feature_names"] = list(features.stats_for(ticker_key).feature_names)       # new (optional)
record = register_model(ticker_id, model_name, cfg, root=models_root)           # train.py:240
```

Use `env.observation_space.shape[0]` (`environment.py:194-196`) as the
value, not `pipeline.n_features()` — the env's space is what the policy was
actually trained against, and `n_features()` is stateful/pinned to the last
compute (`AUDIT.md:162-163`; `features.py:420-433`).

**Surface seam — `registry.py:69-78`.** One line in `config_summary()`:
`"n_features": self.config.get("n_features")`. Makes `kraken-trading-bot
models` show the width next to `feature_windows`/`feature_groups`, which is
where an operator will look.

**Check seam — both loaders, immediately after `load_normalization`:**

* `backtest.py:161` (`pipeline.load_normalization(ticker_key, record.normalization_path)`)
* `paper_trade.py:187-188` (`pipeline.load_normalization(self.ticker_key, record.normalization_path)`)

```python
expected = config.get("n_features")     # may be absent for a legacy model
if expected is not None and int(expected) != len(stats.feature_names):
    raise ValueError(...)
```

Placed here it fires **before** the OHLC fetch in backtest
(`backtest.py:138-147`) and before the live fetch in paper trade
(`paper_trade.py:190-192`), so the user gets a one-line provenance error
instead of a network round-trip's worth of latency before the same failure.
Layer 1 is the authority (it sees the real columns); Layer 2 is the fast,
explicit "your config.yaml was edited after training" message that names
the keys to fix.

**Absence must not crash.** A model trained before this change has no
`n_features`. Treat absent as *unknown provenance* → `_LOGGER.warning`, and
let Layer 1 do the real work. Otherwise this change breaks its own
back-compat story on a repo whose `models/` is currently empty
(`AUDIT.md:381`) but which will hold models by the time it lands.

### Layer 3 — `feature_fingerprint` (config provenance hash)

Layer 1 compares **names**; Layer 2 compares **count**. Neither can see a
change where the names and count are identical but the *values* mean
something else — the realistic case is a signal source repointed, e.g.
`extra_features_file` repointed at a different JSONL whose
`sentiment_score` has different units, or `ohlcv_interval_minutes` changed
from 60 to 240 (same column names, completely different bars). That is
ask (b) in the brief, and it is the only layer that covers it.

**Hash the effective feature-computation inputs**, canonical JSON, sorted
keys, sha256 truncated to 16 hex chars, stored as `feature_fingerprint` in
`config.yaml` and compared at the same Layer-2 seam:

```python
def feature_fingerprint(cfg: dict) -> str:
    payload = {
        "feature_windows": cfg.get("feature_windows"),
        "feature_groups":  cfg.get("feature_groups"),
        "rsi_period": ..., "macd_fast": ..., "macd_slow": ..., "macd_signal": ...,
        "ohlcv_interval_minutes": cfg.get("ohlcv_interval_minutes"),
        "extra_features_file":  cfg.get("extra_features_file"),
        "funding_features_file": cfg.get("funding_features_file"),
        "social_features_file":  cfg.get("social_features_file"),
        "_SIGNAL_COLUMNS": list(_SIGNAL_COLUMNS),   # code constant, features.py:35-45
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
```

The `_SIGNAL_COLUMNS` line is the important one and is *not* obvious from
the brief: `_SIGNAL_COLUMNS` is a **module constant** (`features.py:35-45`),
not a config key, so a config-only fingerprint is structurally blind to
someone adding `funding_rate_prediction` to the allow-list
(`AUDIT.md:210-214`) — a change that silently widens the vector for every
model. Hashing the code constant into the provenance is what makes a *code*
drift visible. That also pre-empts the "make `feature_groups` provenance
exact" item queued at `PLAN.md §4.1.2` from a different angle.

Use the same discipline as Layers 1-2: mismatch → clear error naming the
differing keys; **absent** fingerprint (legacy model) → warning, not crash.

### Error-message contract (all three layers)

Match the existing, already-good wording at `paper_trade.py:326-337` — it
already names the width, the model, and the remediation. Reuse its shape
and add the *concrete* diff, which is what makes the error actionable:

```
Feature set mismatch for ETH_USD/ppo_01: normalization.npz was fitted on
49 features but the current feature config computes 58.
  missing from npz (never seen by the policy): sentiment_score, article_count, ...
  not in the current compute: (none)
  model config.yaml says: feature_windows=[1, 4, 24], feature_groups=[price, technical, volume, microstructure, signals]
  retrain the model, or restore the feature config it was trained with.
```

This is the one place where borrowing a library's *behaviour* rather than
its *code* pays: pandera's error report naming missing/extra columns
(§2, candidate 4) is the right shape and costs nothing to imitate.

### Summary — where each check goes

| Layer | Check | File:line | Failure |
|---|---|---|---|
| 1 | fitted names vs computed columns | `features.py:298` (in `compute`, via new `NormalizationStats` method beside `:108-128`) | `ValueError` |
| 2 | `cfg["n_features"]` vs `len(stats.feature_names)` | `backtest.py:161`, `paper_trade.py:187-188` (right after `load_normalization`) | `ValueError` before any fetch |
| 2 | record `n_features` / `feature_names` | `train.py:238-240` (before `register_model`) | — |
| 2 | surface in `config_summary` | `registry.py:69-78` | — |
| 3 | `feature_fingerprint` compare | same seam as Layer 2 (`backtest.py:161`, `paper_trade.py:187-188`) | `ValueError` |
| 3 | record `feature_fingerprint` | same seam as Layer 2 write (`train.py:238-240`) | — |
| — | **replace** the tautological guard | `paper_trade.py:325-338` | (retarget to Layer 1's numbers) |

### Tests to add (match house style)

Existing style to match: module-wide **real** PPO fixture under `tmp_path`
(`tests/test_rl_paper_trade.py:99`, `tests/test_rl_training.py:322-375`),
`pytest.raises(ValueError, match="<stable phrase>")`
(`tests/test_rl_paper_trade.py:253`), and the "regression contract" docstring
form (`tests/test_rl_environment.py:298-303`).

1. `test_stale_npz_width_mismatch_raises` — train, then delete 9 signal
   names from the npz (or train signal-less, backtest signal-merged);
   assert **both** `backtest_model` and `PaperTrader._build_observation`
   raise. *This is the test whose absence let 0.2 through.*
2. `test_same_count_different_names_raises` — proves set-comparison beats
   width-comparison (**[verified]** a count-preserving swap is constructible:
   the `signals` group contributes 0 columns on the OHLC-only path,
   `AUDIT.md:53-55`, so group membership moves the count and back again).
3. `test_config_n_features_mismatch_raises_before_fetch` — edit `config.yaml`
   post-train; assert a `ValueError` and that the data fetcher was never called.
4. `test_legacy_model_without_n_features_warns_not_crashes` — back-compat.
5. `test_fingerprint_change_detected` — repoint `extra_features_file`.
6. `test_order_drift_is_not_fatal` — pins the benign-order nuance so a future
   contributor does not "fix" it into a hard failure.
7. Existing suite stays green (106 passing at `AUDIT.md:14-15`).

---

## 2. LIBRARY CANDIDATES — evaluated

**Selection criterion (from the brief):** closest to a single-host SB3 PPO bot
whose model dir is 3 files (`model.zip`, `normalization.npz`, `config.yaml`;
`registry.py:4-10`), and — the decisive score — *how cheaply it reduces to a
safe load-time assertion*. All version/license facts fetched from the PyPI
JSON API this pass.

| # | Candidate | Version | License | Wheels for this env (py3.14.7) | Integration cost | Reduces to a load-time assertion? | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | **MLflow** model schema | 3.16.1 | Apache-2.0 | `py3-none-any` ✔ | **59 direct deps** (incl. `gunicorn`, `Flask`, `alembic`, `docker`, `matplotlib`, `pyarrow`, `scikit-learn`, `scipy`, `skops`) + a tracking server/store | ✗ — adopting it *is* the cost; there is no way to take `ModelSignature` without the rest | **REJECT** |
| 2 | **ONNX** input-shape check | 1.23.1 | Apache-2.0 | 5 `cp314` wheels ✔ (+ `onnxruntime` 1.30.0, 7 `cp314` ✔) | torch→ONNX export step + a **second inference path** to keep in lockstep with SB3 | ✗ — it pins only input shape/dtype, i.e. **exactly the check already proven blind in §0.3** | **REJECT** |
| 3 | **TF SavedModel** signature | 2.21.0 | Apache-2.0 | **0 `cp314` wheels of 16** ✗ | ~600 MB dep | n/a | **REJECT — hard blocker** |
| 4 | **pandera** DataFrame schema | 0.33.1 | MIT | `py3-none-any` ✔ | +1 dep (`pandas`/`typing-extensions`/`multimethod`/`packaging`) into a 7-dep project; a schema object to declare *and keep in sync* — the sync burden is the invariant itself | ~ — right *shape*, wrong *weight* | **REJECT dep, ADOPT its error shape** |
| 5 | **pydantic** typed config | 2.13.5 | MIT | ✔ | +1 dep to type one `int` in a dict `yaml.safe_load` parses in 3 places | ~ | **REJECT** |
| 6 | **safetensors** | 0.8.0 | n/a | ✔ | n/a | n/a — serialization format, not a schema checker; npz already stores the names | **REJECT** |
| 7 | **House pattern** (std `hashlib` + typed schema tag) | stdlib | — | — | ~15 LOC, 0 deps | ✔✔ | **RECOMMEND** |

**Why 1 (MLflow) is the closest *conceptually* and still wrong here.** Its
`ModelSignature` enforces "these input columns, these dtypes" on score/load —
which is the genuinely correct invariant, and the same one Layer 1
implements. But MLflow's unit of work is a *registered model in a tracking
store* with runs, versions, stages and a lineage graph. This repo's whole
registry is `models/{TICKER}/{NAME}/` plus a `config.yaml`
(`registry.py:4-13`), single-host, no server, no concurrency. Pulling in 59
direct dependencies and a web server to obtain "compare a list of strings and
raise" is a ~2-orders-of-magnitude overshoot. It is the right answer for a
team serving many models to many consumers; it is the wrong answer for one
bot and one host.

**Why 2 (ONNX) is the most *tempting* wrong answer and must be rejected on
evidence, not taste.** ONNX's `onnx.checker` validates the graph's input
shapes — which sounds like exactly G2. But the only information it pins for
an `MlpPolicy` export is input *shape* `(49,)` + dtype, and §0.3 established
empirically that a shape-only check passes silently during the exact drift
G2 is about. ONNX would therefore add a torch→ONNX export, a second
inference path that can itself drift out of lockstep with the SB3 path, and
`onnxruntime` at runtime — in exchange for a check that is provably no
stronger than the one SB3 already performs for free.

**Why 4 (pandera) is the closest *library* fit worth naming.** It is the only
candidate whose native unit of validation is the *ordered column set of a
DataFrame* — precisely the object that drifts. `DataFrameSchema` + ordered
columns + a lazy error report listing missing/extra columns is the correct
design. It loses only on weight: the guard is a comparison between two lists
that already exist in memory (`stats.feature_names` and
`compute(df).columns`), evaluated **once at load**, so runtime cost is
irrelevant and the library earns nothing. Its real contribution is the
*error-report shape*, which §1 adopts verbatim. If the project ever grows to
validating the raw OHLC/signal frames at ingest (G3/G4 territory — per-tick
`merge_extra_features` merges, `data.py:166`), pandera becomes worth
revisiting; for a load-time assert it is not.

**House precedent (candidate 7), so the design is not invented here:**
`kraken-python`'s exporter already runs exactly this pattern —
`SCHEMA_VERSION = "kraken-extract/1"` (`kraken_api/export.py:50`) stamped
into every record by `_envelope` (`kraken_api/export.py:251-256`,
`{"schema": SCHEMA_VERSION, "exported_at": ..., "resources": ...}`). A
`n_features` + `feature_names` + `feature_fingerprint` triple in
`config.yaml` is the same discipline (typed schema, stamped at write,
checked at read) with zero new dependencies. Reuse the idiom; do not import
a framework for it.

---

## 3. REJECTED OPTIONS (design space, not libraries)

- **R1 — Make `normalize()` itself raise on any dropped column.** Tempting
  (one line, catches everything). Rejected as the *primary* fix: it is the
  per-tick hot path, it fires after the data fetch, and it cannot name the
  offending config keys. `normalize` legitimately drops an absent optional
  column. Keep Layer 1 in `compute` as the authority.
- **R2 — Assert unconditionally inside `compute()`.** Would fire during
  `fit()` (`features.py:207` → `compute`) while the stats are still being
  created. Must be gated on "stats were loaded, not fitted" plus a
  once-per-ticker flag.
- **R3 — Width (`n_features`) comparison alone.** Insufficient.
  **[verified]** same-count-different-columns is constructible: on the
  OHLC-only path the `signals` group emits nothing (`AUDIT.md:53-55`),
  so group membership changes the count and back. Names are the invariant;
  width is only a fast pre-check. (Layer 2 is deliberately the *pre-flight*,
  Layer 1 the *authority* — the ordering matters.)
- **R4 — Treat SB3's `check_for_correct_spaces` as the fix.** Rejected, and
  this is the one most likely to be got wrong: **[verified]** it raises
  clearly on `(5,)` vs `(7,)` but *passes* on the real drift case
  `(49,)` vs `(49,)`. It is a genuine, valuable, orthogonal guard — keep
  relying on it, but it is not G2 and must not be cited as if it were.
- **R5 — Re-fit stats on load when widths disagree (auto-heal).** Rejected:
  it silently in-sample-normalizes at inference, which is exactly the
  anti-pattern `features.py:229-232` documents ("fine for exploratory use but
  not for evaluation"), and it destroys the train/infer identity contract
  that candidate 1 established and that
  `tests/test_rl_environment.py:298-364` pins (env obs == `transform`).
- **R6 — Persist the fitted frame / the raw window, replay it at load.**
  Rejected: G1 is the gap that owns recording the training window
  (`AUDIT.md:118-147`); don't smuggle it in here, and don't make a load path
  depend on a store that is `null` in every config today (`AUDIT.md:104`).
- **R7 — Compare the env's `feature_names()` accessor
  (`environment.py:355-357`) to the npz.** Rejected: `self._feature_names` is
  set from the **pre-normalize** compute (`environment.py:180`), so it *is*
  the drifted ground truth — which makes it a *better* signal than the
  observation space, but it is a symptom of Layer 1 and unnecessary once
  Layer 1 exists in `compute` (same information, later).

---

## 4. Bottom line

G2 is **cheaper than the audit estimates** (the npz already carries
`feature_names`; no format change, no retrain, `models/` is empty) and
**slightly larger in one respect** (the existing `paper_trade` guard is
tautological against this threat, so it must be re-anchored, not copied).
The fix is one pure helper plus a one-time assertion in `FeaturePipeline.compute`,
which buys Layer 1 coverage of *all four* consumers — train, backtest, paper,
export — from a single seam, and converts export's accidental cryptic crash
into a clear message as a side effect. Layers 2-3 (`n_features` +
`feature_fingerprint` in `config.yaml`) are ~18 more lines at the two
`load_normalization` call sites and cover the two cases names and counts
provably cannot see.

Every heavyweight candidate is rejected on measured grounds: TF is
uninstallable on this interpreter (0 `cp314` wheels), MLflow costs 59 direct
deps + a server, and ONNX pins precisely the shape-only information that was
empirically shown to be blind to this drift. The house pattern
(`kraken-extract/1` in the sibling repo) plus stdlib `hashlib` is the
right-weight answer, and the cheapest possible reduction to a safe load-time
assertion.

RESEARCH COMPLETE
