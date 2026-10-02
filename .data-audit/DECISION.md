# DECISION — data-pipeline audit pass 2026-10-02

**Architect:** `architect`, team `audit-pipeline-1002`.
**Inputs:** `.data-audit/AUDIT.md` @ `ca107fa` (CAND-1..7) · `.data-audit/RESEARCH.md` @ `a4c14d4`
(the assembly) · `RESEARCH-1.md` / `RESEARCH-2.md` / `RESEARCH-3.md` for the detail behind each
claim relied on here.
**Repo state:** `kraken-trading-bot` @ `a4c14d4`. Read-only pass: nothing outside this file was
written, committed, deleted or tidied.

**Supersedes** the `.data-audit/DECISION.md` from the `data-audit-1002` pass, which chose **G1**
(the `~`-expansion defect in the signal seam). G1 has since landed — `_resolve_config_path`
(`data.py:133`) now calls `expanduser()` — so G1 is not an open gap and is not re-chosen here.

---

## 1. THE DECISION

| | |
|---|---|
| **Outcome type** | **`IMPROVE-EXISTING`** |
| **Gap** | **CAND-3a** — the local market-data store is a config key away and has never been built, so `train` is capped at Kraken's ~721-bar REST ceiling and **no pinned `data_window` older than 30 days is reachable at all** |
| **Library** | **`kraken-market-data`** — already an in-repo flake input, pinned at rev `055d7f6`; **no new dependency.** `kraken-deep-history` is used as an already-written seeder, also **not built** |
| **Project name + path** | **N/A — no new repo.** Not a submodule, not nested, no naming step. |
| **Improvement scope** | `kraken-trading-bot` (primary; the builder's own worktree) + the acceptance instrument in `tools/model_matrix.py` |
| **Required code** | **~50 lines** (the CAND-5 dispersion gate) + **~10 lines** (venue provenance label). The store flip itself is **0 lines** — it is a YAML value. |
| **Required data** | **158 s, 13 MB**, seeded once into a shared local path |

**One line:** stop measuring this bot on one month of data — build the store it already has a
config key for, so `train`/`backtest` can reach bars Kraken's REST API will never serve, **and** land
the dispersion gate that decides whether the resulting two-arm comparison means anything.

---

## 2. WHY CAND-3a — the blocker I measured myself

I did not take RESEARCH-3's 76,561-bar figure on trust. I drove the real
`read_ohlc_dataframe` (`data.py:1052`) with a duck-typed store — the exact contract
`_resolve_store` (`data.py:1211`) accepts — and measured what a pinned window does on each leg:

```
SHIPPED default.yaml -> is_pinned=False has_split=False [unbounded, unbounded) eval_split=0.7

LIVE 721-bar frame, unpinned      -> train 721 / eval 721   (IDENTICAL OBJECT: True)
PINNED 2020-01-01..2026-01-01 on the LIVE 721-bar frame
                                  -> train 0 / eval 0
                                     prepare_episode RAISES NotEnoughDataError
PINNED 2020-01-01..2026-01-01 on a 76,561-bar STORE frame
                                  -> train 36826 / eval 15782
```

Three consequences, each one load-bearing:

1. **The shipped default config cannot produce a single out-of-sample measurement.**
   `since: null, until: null` (`default.yaml:200-203`) ⇒ `is_pinned` False ⇒ `has_split` False
   (`data_window.py:82-101`) ⇒ `training_frame` and `evaluation_frame` return **the same object**
   (verified `is` identity True). So every `train`/`backtest` pair today is a backtest on training
   bars — which `configs/default.yaml:188-192` itself calls out as *in-sample*. And
   `tools/model_matrix.py:2229-2232` already emits the consequence: *"A matrix with zero
   out-of-sample cells supports NO generalization claim at all."*
2. **Pinning is the documented fix and it is unreachable past 30 days.** `data_window.py:5-8`
   records that two fetches of the same pair "disagree by 16% on a fitted std" — pinning exists
   precisely so a comparison compares configs, not data. But with the REST ceiling at 721 bars,
   pinning to anything older clips to **empty** and raises `NotEnoughDataError`. The store is the
   **only** route past that, and 721 is a hard limit: Kraken's OpenAPI says *"Returns up to 720 of
   the most recent entries (older data cannot be retrieved, regardless of the value of `since`)"*,
   and RESEARCH-3 confirmed it with four live probes.
3. **Therefore CAND-3a is the precondition for this pass's own Phase 6.** Not "an optimisation" —
   the instrument.

### Why this is the highest-directness improvement, and why it is not a footnote

`AUDIT.md:271-275` ranks CAND-3 "Directness to the RL observation: **Highest of any candidate** —
this *is* the training window." Every rolling feature, the 24-bar warm-up boundary and the
normalization stats are fitted on whatever the read returns. Nothing else on the board changes
that. And it is the **only** candidate that changes it by **106×**.

**It is also width-neutral, which no other candidate is.** Measured, same funding file, same
`feature_groups`/`feature_windows` as `configs/default.yaml`, via `prepare_episode` +
`TradingEnvironment`:

| configuration | `n_bars` | `n_features` | `start_index` |
|---|---|---|---|
| live 721 bars, bare (no signal files) | 721 | **52** | 24 |
| live 721 bars + the 1-record funding file **[SHIPPED]** | 721 | **60** | 24 |
| **store, 76,561 bars, bare** | **76,561** | **52** | 24 |
| **store, 76,561 bars + the 1-record funding file [THIS DECISION]** | **76,561** | **60** | 24 |

So: **no `models/{TICKER_ID}/{model_name}/` artifact is invalidated by this change.** The width
guard `check_feature_width` (`features.py:262`) compares *by name* against the artifact's
`feature_names`, and that set is unchanged. What changes is `n_bars` (`train.py:298`), which is
recorded as provenance precisely so a report can tell the two apart. That is the safest possible
shape for a first real measurement in this repo.

---

## 3. THE LANDING POINT — exact, per ticker

This is where the change is **read**, once per `(ticker, interval)` read:

**The store adapter — `kraken_trading_bot/rl/data.py:1052` `read_ohlc_dataframe`, with
`_resolve_store` at `data.py:1211`.**

```
config key  market_data_store            (configs/default.yaml:161)
   -> train.py:225     market_data_store=cfg.get("market_data_store")
   -> backtest.py:412  market_data_store=_resolve_env_setting(...)   # run config may override
   -> export.py:175    market_data_store=cfg.get("market_data_store")
   -> data.py:1216     store = _resolve_store(...)      # lazy `from market_data.store import MarketDataStore`
   -> data.py:1174     candles = _page_candles(pair, interval, pages, source, since)
   -> data.py:1176     store.upsert(pair, interval, candles)          # fetch -> upsert
   -> data.py:1191     df = store.read(pair, interval, since=since, until=until)
   -> data.py:1196     add_derived_ohlcv_features(df)                  # SAME derivation as the live leg
   -> data.py:1197     merge_extra_features x3                        # SAME seam, same order
```

All three RL consumers already read the key. `pages` stops meaning depth on the store leg — it only
bounds the live *append*. `since`/`until` default to `None` so `store.read` returns the whole store.

**Config inputs the outcome consumes:** `market_data_store` (the store root path; `null` = live
fetch) and, for the acceptance comparison, the existing `data_window.{since,until,eval_split}`.

**`train` / `backtest` knobs that matter and their real defaults** (measured, not quoted):

| knob | default | effect here |
|---|---|---|
| `--pages` | `6` (`cli.py:154-158`) | bounds only the live append on the store leg. **Does not buy depth** — `--pages 2/4/8` all return 721 bars live (RESEARCH-3 §3.2: the `last` cursor never advances, so calls 2..N are byte-identical replays) |
| `--episode-bars` | `None` = **all** (`cli.py:160-164`, `train.py:157`) | **critical and favourable**: the episode is *not* re-capped, so a seeded store's 76,561 bars flow straight into training |
| `--timesteps` | `10_000` (`cli.py:167-171`) | **honest caveat**: PPO's step budget is unchanged, so 76,561 bars buys *diverse* experience and a longer evaluation span, **not** 106× more gradient steps |
| `data_window.eval_split` | `0.7` (`default.yaml:203`) | inert while unpinned; becomes real the moment a bound is set |
| `signal_max_age_hours` | `12` (`default.yaml:126`) | unchanged — the store changes bars, not signals |

**Artifact layout the outcome must leave trainable and measurable:**

```
models/{TICKER_ID}/{model_name}/
    model.zip           stable-baselines3 policy
    normalization.npz   per-ticker feature_names + mean/std  (registry.py:32)
    config.yaml         the exact training config — train.py:288 writes n_features,
                        train.py:298 writes n_bars, and market_data_store rides
                        along because build_train_config keeps every YAML key
```

`models/` is empty today (`.gitkeep` only), so **no width or coverage figure in any of the four
artifacts is read off a shipped model.** This decision is chosen partly because it is the one whose
acceptance can be settled by a *trained artifact's own recorded provenance* (`n_bars`) rather than
by a width comparison — the thing Phase 6 has never been able to do.

---

## 4. LIBRARY

| Library | Verdict |
|---|---|
| **`kraken-market-data`** — the store reader (`market_data.store.MarketDataStore`) | **CHOSEN.** Already a flake input (`flake.nix:7`), pinned `rev 055d7f6` in `flake.lock`, and its dev-shell dependency closure (pandas/pyarrow/requests) is already carried by *this* repo's `flake.nix:62,74` + `PYTHONPATH` at `flake.nix:71`. Zero install work. |
| **`kraken-deep-history`** — the Binance-archive seeder (`seed`/`plan`/`verify`/`stats`) | **USED, NOT BUILT.** Already written; `plan` is a free zero-network dry-run and `stats`/`verify` are exactly what a gate needs. **Not a flake input of this repo** — see the hermeticity trap in §7. |
| `krakenex` / `krakenapi` / `ccxt` | Rejected (RESEARCH §5): two are ~2 y stale; `ccxt` is current but coarser and would erase the exact endpoint detail. None is needed — `/0/public/OHLC` is already wrapped by `kraken-python`. |
| `tenacity` / `urllib3.HTTPAdapter` | Rejected — the retrying client already ships in `market_data.client.KrakenClient`, and both hide policy from the repo's `log_event` convention. (CAND-4, deferred — see §9.) |
| **Any new library / new repo** | **None.** |

---

## 5. IMPROVEMENT SCOPE — repos and in-scope files

### 5.1 `kraken-trading-bot` — PRIMARY (the builder's own worktree)

| # | Unit | What | ~size |
|---|---|---|---|
| **R1** | `tools/model_matrix.py` | **The CAND-5 dispersion gate.** Required. Full spec in §6. | ~50 ln |
| **R2** | `justfile` | `just store-plan` / `just store-seed` / `just store-stats` / `just store-verify` — the **hermetic** seeding path (§7). Uses the existing `dev := "nix develop --command bash -c"` convention. | ~40 ln |
| **R3** | `configs/deep-history.example.yaml` | Already sets `market_data_store: ~/Projects/kraken-market-data/store` (`:81`). Add a **pinned-window** example (`data_window.since/until/eval_split`) — today the file offers deep OHLCV bundled with an inert signal block (`AUDIT.md:267`) — plus the venue label from R4. | ~15 ln |
| **R4** | `configs/default.yaml` + `kraken_trading_bot/rl/data.py` | **Venue provenance label.** R3 measured the seeder is Binance-\*USDT spot and the basis is a **+5.41 bp level shift against a 58.30 bp hourly sigma** — harmless for z-scored features, but it must be *labelled*, not silently assumed. Add a declared `market_data_store_venue` key (default `"kraken-live-rest"`) and log it once on the store leg so the run log states which venue the bars came from. **COMMENT-ONLY in `default.yaml`; the key must stay `market_data_store: null`.** | ~10 ln |
| **R5** | `tests/` | Tests for R1's gate (the 4 RESEARCH-3 §5.2 cases, incl. the n=1 degenerate case) and for R2's `store_mode` assertion. | ~60 ln |

**R4's `default.yaml` rule is load-bearing and easy to get wrong:** `market_data_store` **stays
`null`** in the shipped default. A non-null value that does not resolve raises `ValueError` from
`_resolve_store` (`data.py:1244-1251`), so pointing the shipped default at a store path that does
not exist on a fresh clone would turn every first run into an error. The flip belongs in
`configs/deep-history.example.yaml` and in a per-model `models/{TICKER_ID}/{NAME}/config.yaml`.

### 5.2 `kraken-market-data` / `kraken-deep-history` — OUT OF SCOPE THIS PASS (explicitly)

R3's prescription was to label the venue in `_meta.json`, which is a sibling-side write. **I am not
putting that in scope**, for two concrete reasons: (a) they are **separate git repos**, and the
builder owns a worktree of *this* repo — a sibling commit would land outside the branch under
review; (b) this repo consumes `kraken-market-data` as a **pinned flake input**
(`rev 055d7f6`), so a local sibling edit would not even reach `nix develop` without a `flake.lock`
bump and a push. R4 achieves the same *label* on the artifact the reviewer already inspects
(`config.yaml`), which is the better home for it anyway. Sibling `_meta.json` is a follow-up.

---

## 6. THE ACCEPTANCE INSTRUMENT — CAND-5, and why it is in scope

**This is not a second gap; it is the instrument that makes CAND-3a's own effect claim admissible.**
The brief is explicit: *"CAND-5 gates the CLAIM of any change, not its BUILD."* CAND-3a's payoff is
precisely a two-arm comparison ("does 8 years beat 721?"), so shipping the store without the gate
produces exactly the number the brief says is undefendable. 50 lines, no data, and it lands in the
same change.

**Spec, verbatim from RESEARCH-3 §5.3 with the n=1 constraint honoured:**

1. **Estimator:** `pooled_within_spread(summaries)` = **median** of the per-group `q3 - q1`
   (`summarize` already returns `q1`/`q3`, `model_matrix.py:328-339`), taken over groups with
   `n >= MIN_REPLICATES_FOR_A_CLAIM` (`model_matrix.py:243`). `None` if fewer than two such groups
   or if the result is `0`. **Median, not `max`** — `max` lets one wild group condemn every
   comparison (measured: case 1 collapsed 53× → 0.21×); `mean` is not robust to the same group.
2. **Placement — the gate must sit BEHIND the count gate, never beside it.** At n=1 per arm,
   `q3 - q1 == 0`, so a pooled-IQR gate reports `gap = 5 pp / 0 = inf -> RESOLVED` — blessing the
   exact anecdote the surrounding prose exists to kill (RESEARCH-3 §5.2 case 4). It must require
   `n >= 3` **and** `pooled_iqr > 0` before it emits anything.
3. **Output a RATIO, not a boolean.** `gap / pooled`, where `gap = |median_A - median_B|` over
   adjacent arms in the headline cohort. The threshold is a judgement call; the ratio is a fact.
   Verdict wording: `RESOLVED (ratio >= 1)` / `NOT SEPARATED (< 1)` / `UNDEFINED (n < 3 or zero
   spread)`.
4. **Sites:** a new `pooled_within_spread()` beside `summarize()` (:328); one line per adjacent
   arm-pair in `_build_claims` (:2208), replacing the `thin`/else branch at :2259-2280 rather than
   adding to it; one pooled-IQR line in `_print_group_table()` (:1929) so a reader sees the
   yardstick the verdicts used.
5. **Documented limitation, in the docstring:** at n=3 a group IQR is a 50th-percentile-of-two
   estimate. This is **necessary, not sufficient**. It stops a noise-ranked difference being
   *presented* as a finding; it does not manufacture power. More power is still 5+ seeds.

The four worked cases are the regression fixtures: case 1 (gap 4 pp, pooled 0.075 pp, **53×**) →
RESOLVED; case 2 (gap 3 pp, pooled 19 pp, **0.16×**) → NOT SEPARATED — *today's `len(seeds) >= 3`
PASSES this*; case 3 (0.91×) → NOT SEPARATED; case 4 (n=1, 0.00 pp) → UNDEFINED, **not** RESOLVED.

---

## 7. INTEGRATION SKETCH

**Output contract.** Not a record stream — **a parquet tree**, and that is deliberate.

```
<store-root>/
    _meta.json                       # since-cursor sidecar per (PAIR_ID, interval)
    {PAIR_ID}/{interval_min}/{YYYY-MM}.parquet
```

read by `MarketDataStore.read(pair, interval, since=..., until=...) -> DataFrame`, written by
`.upsert(pair, interval, candles)`. Measured contract (RESEARCH-3 §1.5): inclusive `since`, exclusive
`until`, on bar-bucket start; globs only the months the window touches; an empty window returns an
empty frame with the same columns and the *caller* decides; `upsert` dedupes on bar `time` keeping
last, so re-polling overwrites the still-forming bar with Kraken's own revision.

**Why a shared local path and not a pipe.** Three reasons, in order of weight:

1. **The consumer already declares it.** `_resolve_store` (`data.py:1211-1210`) takes exactly a path
   to a store root, and `configs/default.yaml:139-161` + `configs/deep-history.example.yaml:81`
   already spell the path `~/Projects/kraken-market-data/store`. This decision changes a value, not
   a contract. A pipe would mean inventing a second contract for a consumer that has none.
2. **The data is a growing, append-only, multi-reader asset, not a per-run stream.** `pages` on the
   store leg bounds only the live append (`data.py:1174`), and `train`, `backtest` and `export` all
   read the same root independently. A stream would have to be re-fetched per reader.
3. **Cost is a non-issue and windowing already works.** 60 bytes/bar, 13 MB for 280 months; a
   whole-store read of 75,840 rows is **0.142 s**, a 2-month windowed read **0.010 s** (14× faster,
   because it touches 2 parquet files instead of 104). The "reads the whole store every time" cost
   is not real, so R3's `since`/`until` push-down is **deferred** as the research itself recommends.

**Where the bot picks it up.** One place, already built: `data.py:1216` inside
`read_ohlc_dataframe`. Nothing in `features.py`, `environment.py` or `train.py` needs to know a
store exists — they already receive a `DataFrame` with the identical 11-column shape
(`time, open, high, low, close, vwap, volume, count` + `vwap_dev, trade_count_zscore_20,
volume_per_trade`), which I verified by measuring the store leg's output column-for-column against
the live leg.

**Default path:** `~/Projects/kraken-market-data/store` — machine-independent (`~`-spelled, expanded
by `_resolve_config_path`), shared by every model, matching what the example config already says.

### Build traps the builder must not walk into

1. **Seed where `market_data` + pyarrow are importable, or the seed silently writes unreadable CSV.**
   `kraken-deep-history`'s own `nix develop` carries **only pytest** — no pandas, no pyarrow — so
   `_open_store` catches `ImportError` and returns `FallbackStoreWriter`, which writes `.csv`
   files that `MarketDataStore.read` **cannot see** (it globs `*.parquet`). The JSON report's
   `store_mode` is the *only* signal. **This is the second time a pass has hit it.** The recipe must
   run the seeder under **this** repo's dev shell (`flake.nix:62,74` already carries
   `market_data`/pyarrow/requests) with the local `kraken-deep-history` checkout on `PYTHONPATH`,
   and must **assert `store_mode == "market-data"` and refuse `fallback-csv`** rather than printing
   it. A seed that "succeeded" into csv yields a store the bot reads as ~721 live bars — the
   exact failure this decision exists to remove.
2. **`kraken-deep-history` is not a flake input here** (inputs are `nixpkgs`, `kraken-python`,
   `kraken-market-data` — `flake.nix:5-8`). So `nix run ~/Projects/kraken-deep-history#…` from
   inside the dev shell is not the reliable route; use the `PYTHONPATH` form above. Do **not** add
   a new flake input this pass — that needs a `flake.lock` bump plus a push before it resolves.
3. **`--pages` is a replay loop, not a depth knob.** `last` never advances past page 1 on a live
   pair; `pages=6` → 6 calls, 721 unique bars, 10 dup rows discarded at `data.py:1030-1031`. Set
   `--pages 1` on store-backed runs; the store supplies the depth.
4. **A seeded store is Binance-\*USDT spot** (`kraken-deep-history/export.py:45-48`). Label it (R4).
   Do **not** build a Kraken OHLCVT reader to "fix" this — RESEARCH-3's judgement, which I accept:
   the newest Kraken archive is **94 days stale** (MANIFEST `coverage.end = 2026-06-30`,
   `generated = 2026-08-17`, today 2026-10-02, Q3 unpublished) so it does **not** replace the
   forward poller, and Kraken's bulk OHLCVT has **no `vwap`** (MANIFEST is 7 columns, the store
   contract is 8) so a Kraken-seeded store would **silently lose `vwap_dev`** through the presence
   gate. 10.5 GB and 300 lines to buy a 5.4 bp level shift that z-scoring already removes.

---

## 8. WHAT THE GATE CAN AND CANNOT PROVE FOR THIS OUTCOME

**Can prove (and Phase 6 will be held to exactly these):**

- `n_bars` in `models/{TICKER_ID}/{NAME}/config.yaml` and in the `BacktestResult` is **≥ 1000**
  on the store leg and **≈ 721** on the live leg. This is the primary magnitude assertion, it is
  read off the artifact's own recorded provenance (`train.py:298`, `backtest.py:147`), and it is
  the assertion that would have caught the 2026-10-01 pass's failure mode.
- `n_features` and `feature_names` are **unchanged** by the flip (measured: 60 vs 60, 52 vs 52), so
  `check_feature_width` passes on both arms and the comparison is not confounded by width.
- `first_tradable_index` / `start_index` is **24** on both arms, so no arm gained or lost warm-up.
- `num_trades > 0` and `n_bars == frame - 24` on both arms (the arithmetic the last pass validated:
  697 = 721 − 24 exactly).
- The two arms' frames are **disjoint**: `training_frame` vs `evaluation_frame` over a pinned
  window yield 36,826 / 15,782 bars with no overlap. This is the assertion that makes the run
  out-of-sample rather than in-sample.
- The CAND-5 gate itself: the four §6 cases, and on the real matrix a **ratio** per arm-pair.

**Cannot prove — stated so Phase 6 is not asked for it:**

- **It cannot prove 76,561 bars is *better* than 721.** It can prove the store was read, and it can
  report the between-arm gap as a ratio against measured within-arm spread. Whether the gap clears
  the ratio is an *empirical* question this pass cannot pre-answer — and the honest gate, given the
  16%-on-fitted-std instability `data_window.py:5-8` already records, is that **the verdict may
  legitimately be `NOT SEPARATED`**. A `NOT SEPARATED` verdict on a store-vs-live comparison is a
  *finding*, not a failure: it would say the extra 7 years did not resolve the effect at n=3 seeds,
  which is exactly what the gate exists to be able to say.
- **It cannot measure `total_timesteps`.** 10,000 PPO steps against 76,561 bars means the policy
  still takes ~13% of the store (with resets). Depth buys *normalization-sample size*,
  *regime diversity* and a *longer evaluation span* — **not** more gradient steps. No report may
  describe this as "106× more training".
- **It cannot separate Kraken venue from Binance venue.** The store is cross-venue by
  construction. R4 labels it; nothing in this pass models the 5.41 bp basis away, because
  z-scoring removes a level shift and the hourly-return correlation is 0.999081. A store-vs-live
  comparison therefore measures *history + venue together* and must be described that way.
- **If the honest gate degrades to name/width/bar-count, say so.** The bar-count + width +
  disjointness set above is a *configuration* assertion, not an *effect* assertion. Phase 6 should
  record explicitly which of the two it achieved. The 2026-10-01 pass is the precedent for the
  failure: a run completed cleanly while replaying **1 bar of 721** (a sparse-exogenous file moved
  `first_tradable_index` from 24 to 720 and the width guard still passed — which is why
  `POINT_IN_TIME_EXOGENOUS_COLUMNS`, `features.py:123-125`, now excludes exogenous columns from
  the warm-up gate). Any run whose `n_bars` is a small fraction of its frame is **NOT** evidence,
  whatever its exit code.

---

## 9. RUNNER-UPS — and plainly why each lost

All four are cheap and defensible. I beat them on one axis each, stated explicitly.

### Runner-up 1 — **CAND-2, exogenous coverage** (funding backfill + F&G + the timer). *The closest call.*

**What I am giving up, honestly:** 1.80% → **100%** coverage (`13/721` → `721/721`), distinct
`funding_rate` values **1 → 721** (lag-1 autocorr 0.75), and `signal_observed` z-scoring correctly
to **0.000** instead of a **+7.4σ step**. The lever is real: one `just funding-timer` that **has
never been run** (the only timer in the repo, funding-only, absent from `nix/module.nix` which
declares zero timers, and not installed on this host) is most of the fix. On raw
information-recovered-per-hour this beats CAND-3a.

**Why CAND-3a still wins — four reasons:**

1. **The information content is ~1 bit, replicated 8 times.** R2 measured **seven of the eight
   exogenous columns at Spearman ρ = 1.000 exactly** — a correctness item, not a depth nicety.
   Recovering coverage to 100% on a channel that is 8-wide and 1-dimensional inflates the
   observation's *width* by 8 collinear columns. CAND-3a is **width-neutral** (§2). For a policy
   learner, 7 years of independent bars beats 8 collinear columns of one bit.
2. **CAND-2's fix must land a 24-hour look-ahead landmine in the hot path, and no plausible number
   will catch it.** The F&G daily value is *final-at-fetch*; emitting it hourly at 00:00 of day D
   asserts day D's close. 19 polls over 9 min held the value at 72 while `time_until_update`
   counted 40017 → 39472s. **Fix: stamp at D+1 00:00Z.** This *inflates* results, so a reviewer's
   "numbers look reasonable" check passes on corrupted data.
3. **Its fix depends on two live defects in a sibling that has one commit and no LICENSE.** R2
   measured: the shipped `Mozilla/5.0 (compatible; …)` UA **loses the Cloudflare gate** → HTTP 403,
   and `_get` treats 403 as non-fatal → **silently produces nothing** (a full Chrome UA → 200); and
   pagination reads a cursor key the API never returns (`cursor.after` vs actual `{more, since,
   max}`), so pages 1/2/3 are byte-identical and the hard ceiling is 30 messages ≈ 3.9h, making the
   `--lookback-hours 24` default **unreachable**. The F&G backfill must therefore **not** depend on
   StockTwits succeeding.
4. **Its landing point is inert until CAND-3a lands.** The signal seam joins onto bars. With 721
   bars there is ~30 days for 721 hourly funding records to cover — feasible but on one month. The
   substrate has to exist first.

*(CAND-2 is the correct **next** pass, ideally immediately after this one, once a store exists to
join onto. It is deferred, not rejected.)*

### Runner-up 2 — **CAND-1, a `kraken-microstructure` trade-tape source** (`NEW-DATA-SOURCE`). The best NEW-DATA-SOURCE candidate, and it still loses.

**What I am giving up:** a keyless, paginable `/0/public/Trades` reaching each pair's **full
listing history** (XBT/USD to 2013-10-06, ETH/USD to 2015-08-07, SOL/USD to 2021-06-17) — *not*
subject to the 721 ceiling; **6 point-in-time scalars + 1 rolling** per (ticker, bar) with **0 lines
of parsing** (typed models already exist in `kraken-python`); **zero marginal warm-up**; and a
strict-superset property — a fully-paged 1h bar deduplicated on `trade_id` reproduces the candle's
`volume`, `count` and `vwap` with **delta exactly 0.0**. That is a genuinely excellent source, and
if the brief's premise were "we must ship a new repo" this would be it.

**Why it loses:**

1. **It backfills FEATURES, not BARS — and it does not touch the 721 ceiling at all.** The tape's
   features land on a frame that is still 721 bars long. Its payoff is measured on the same ~30-day
   substrate CAND-3a exists to end.
2. **Its consumer cannot be lit by one line.** `_add_microstructure_features`
   (`features.py:577-590`) is keyed on `bid_vol`/`ask_vol`, which a *tape* cannot produce — the tape
   carries no book. So the `microstructure` group stays at 0 → 1 columns and new feature-builder
   code is required. The claim "zero consumer-code change, the machinery is waiting" holds for a
   *Depth* producer, which R1 proved is snapshot-only and silently ignores `since`; it does **not**
   hold for the tape.
3. **Three undocumented traps, all load-bearing:** `Trades.last` is a **nanosecond** cursor returned
   as a bare `str` — passed back verbatim it lands in **1970** (`Spread.last` is in *seconds*: same
   param name, incompatible units, so a generic paginator silently corrupts one of them); rows are
   **7 fields, not 6**, and `Trade.from_public_row` silently drops `trade_id` — measured **7.278×
   volume inflation** without it; throttle arrives as **HTTP 200 with `{"error":["EGeneral:Too many
   requests"]}`**, not 429, at ~30 burst, and `KrakenTransport.min_interval` defaults to **0.0**.
4. **Highest cost of anything on the board** — a new repo, a timer, a packaging/flake/lock cycle —
   for features measured on 721 bars.
5. **Honest negative I'd respect anyway:** realized spread is **structurally unbuildable** from the
   tape (no book, and the only mid source ignores `since`); roll was **+260,551 bps** where defined
   and undefined on 13/16 one-minute bars; Corwin-Schultz went negative on 11/15. Dropped.

### Runner-up 3 — **CAND-5 alone** as the outcome. It beats everything on cost and loses on being an outcome.

50 lines, no data, no new repo, and R3 is right that it should not wait behind anything. But it
feeds **no feature**, changes **no observation**, and — decisively for this brief — its landing point
is the *report*, not the RL pipeline. Phase 6's mandatory evidence template ("bars replayed, trades
taken, new columns present **BY NAME**") has nothing to bind to for a change that moves no bar and
adds no column. So it is **in this outcome as the instrument** (§6) rather than *as* the outcome.
That is the honest resolution, not a hedge: the brief says the highest-directness improvement must
be a candidate outcome, and CAND-3a's directness is higher.

### Runner-up 4 — **CAND-4**, retry/backoff/salvage on `_page_candles`. *Deferred with a trigger.*

~40 lines, **no new dependency** (the retrying client already ships at
`market_data.client.KrakenClient`), and it kills a **measured 5-of-6 wasted API calls** via a
1-line `if last == cursor: break`. It is **not a precondition** for CAND-3a — R3 proved the
pushed-down read and the post-hoc clip agree to **1.5e-13**. But R3 is right that it should land
*with* the store change, because the store leg calls `_page_candles` too (`data.py:1174`) and after
the flip a read failure means "the tail of my training window is missing". **Scope call:** I am
keeping it out of this outcome to keep one clean target, and **I am recording the trigger**: if
Phase 6 sees any store-backed run log a `_page_candles` failure or a salvage warning, CAND-4 is
promoted immediately. Scope CAND-4 as **transient-failure resilience, not rate-limit compliance** —
Kraken's counter limits did not reproduce on the keyless path (16 rapid + 8 at 2.5/s both 100% OK).

### Deliberately NOT chosen (with reasons)

- **A Kraken OHLCVT reader** — RESEARCH-3 §2.4, accepted verbatim: 94 days stale, no `vwap` (silently
  loses `vwap_dev`), basis is a 5.4 bp level shift vs a 58 bp hourly sigma. 10.5 GB. Label the
  venue instead.
- **Realized spread** (R1) — structurally unbuildable from the tape.
- **Cross-venue funding sources** (Deribit/Binance, R2) — on-venue funding beats 7× shallower
  because it introduces no unmodelled basis. Consistent with refusing cross-venue bars. Both
  researchers refused cross-venue independently, from opposite directions; that position holds.
- **CAND-6** (`pair_from_ticker_id` blind `_`→`/`) — real, cheap, but zero feature effect. It is
  *falsified in practice by this pass*: `--ticker USD_SOL` fails while `ETH_USD` works, and
  `train.py:138` is exercised on every Phase 6 run, so the correct spelling is simply used.
- **CAND-7** (walk-forward) — structurally unreachable, and it depends on CAND-3a landing first. A
  store makes it *possible*; making it *implemented* is a separate change.

---

## 9A. POST-DECISION AMENDMENTS — recorded after the Phase 4B measurement

The builder's measurement (commit `bfe32aa`, real keyless data, store seeded 2018-01→2026-10)
confirmed the gate assertions and surfaced three things §8 did not anticipate. They are recorded
here so the reviewer reads them as **known limitations of the harness**, not as new findings.

### 9A.1 A live-vs-store pair inside ONE matrix is structurally uncomparable (DEFECT — Phase 7)

`tools/model_matrix.py report` emits **no dispersion section at all** for a matrix mixing both
arms. With `min_bar_ratio: 0.5` the degenerate-cell guard scores each cell against the ticker's
**largest observed `n_bars`**:

| | live arm | store arm |
|---|---|---|
| `n_bars` | 697 (721 − 24 warm-up) | 76,538 |
| guard `0.50 × 76,538` | **31,269** — 697 fails by 45× | passes |
| outcome | **all 3 cells marked INVALID** | valid cells flagged IN-SAMPLE and excluded |

Net effect: **6 recorded cells → 0 usable out-of-sample measurements, no arm-pair, gate never
fires.** The guard is doing exactly what it was written to do — a cell replaying 0.9% of its
peers' bars is degenerate *within a comparable cohort* — but a live-vs-store pair is not such a
cohort: the two arms differ **by construction** in bar count, so the guard measures the treatment
and discards the arm it is comparing.

**This is a defect in the harness, not in the store or the gate.** The CAND-5 estimator is sound;
it is being invoked through a report path that cannot reach it.

- **Deferred to Phase 7** by user decision (2026-10-02), NOT fixed in this pass.
- **Interim method the reviewer MUST use:** score the two arms as **two separate matrices**, each
  with its own `min_bar_ratio` reference, then apply `pooled_within_spread` /
  `dispersion_verdict` across the two sets of replicates. The builder did exactly this and got the
  numbers in §9A.2.
- Any future report comparing arms of deliberately different depth **must** state this limitation
  rather than quoting a `report` run that silently produced no verdict.

### 9A.2 The gate verdict is `NOT SEPARATED`, and that is a finding

Six cells (2 arms × 3 seeds), 0 invalid at run time, 0 errored. Measured with the shipped
`pooled_within_spread` + `dispersion_verdict` (threshold 1.0, `MIN_REPLICATES_FOR_A_CLAIM` 3):

| metric (live↔store) | live median | store median | pooled IQR | gap | **ratio** | **verdict** |
|---|---|---|---|---|---|---|
| `excess_return` (headline) | −3.23% | −19.48% | 439.01% | 16.24% | **0.037** | **NOT SEPARATED** |
| `total_return` | +4.07% | +225.13% | 439.06% | 221.06% | **0.503** | **NOT SEPARATED** |
| `sharpe` | +0.574 | +1.626 | 0.751 | 1.052 | 1.400 | RESOLVED |
| `max_drawdown` | +4.79% | +66.97% | 0.058 | 0.622 | 10.74 | RESOLVED |

**Why the headline is `NOT SEPARATED`:** the store arm's own within-group IQR is **8.73** — its three
seeds returned **+53%, +225%, +1800%**. The apparent between-arm gap sits entirely inside seed
noise, so those medians are not ordered by the data. This is precisely the presentation CAND-5
exists to prevent, and it is working.

**The two RESOLVED rows are NOT evidence of a better model** and must never be reported as such.
They are **confounded by horizon**: the store arm replays 76,538 bars against the live arm's 697,
and *both* replay bars they were fitted on (neither arm is pinned here), so a longer in-sample fit
mechanically produces both a higher Sharpe and a deeper drawdown. `timesteps` was 10,000 on both
arms — **depth buys normalization-sample size and regime diversity, not gradient steps.**

### 9A.3 Honest out-of-sample result is NEGATIVE

The +225% figure is **in-sample and is not a result**. Both arms above are unpinned, so backtest
replays the bars the model was fitted on. The pinned store model (`storeP1`) backtested on the
**disjoint** eval tail:

| | value |
|---|---|
| `total_return` | **−34.30%** |
| `sharpe` | **−0.568** |
| `max_drawdown` | 54.22% |
| `num_trades` | 11,866 |
| bars replayed | 15,749 / 15,773 |
| buy-and-hold | −19.46% |
| **excess** | **−14.84%** |

**What CAND-3a actually delivered, stated without inflation:** a store makes an
**out-of-sample measurement possible at all**. On the shipped default, `since`/`until: null` means
`training_frame` and `evaluation_frame` return the **same object**, so no OOS number existed to be
good or bad. It did **not** deliver better returns, and it did not deliver a claim that could be
adjudicated without the two-matrix method in §9A.1.

**Wording to use in all downstream reports** (user-specified, binding):
> The store arm reached **76,562 bars** with **disjoint** train/eval splits over a pinned
> 2020→2026 window. Honest out-of-sample result: **−34.3% return, Sharpe −0.568**. The CAND-5 gate
> reports **NOT SEPARATED** on both return metrics. **No claim of improvement.**

Two further facts that fall out of the measurement:
- **Pinning alone is not enough and, without a store, is actively destructive** — pinned on the
  live arm it yields **0 train / 0 eval**. The store is a *precondition* for pinning, not an
  optional extra.
- **The store flip is not deliverable without the non-finite guard** (`bfe32aa`). The seeded
  archive carries 4 zero-volume bars; `pct_change`
  turns it into `inf`, which the documented `compute → ffill → fillna(0)` policy cannot repair, and
  one such bar inside the training slice poisoned all 36,804 rows and killed PPO. **The live arm was
  unaffected** (721 Kraken bars contain no zero-volume bar), so this was invisible until the store
  was switched on.
  > **CORRECTION (2026-10-02, Phase 6 finding F4) — the diagnosis above was wrong, the
  > finding was not.** The original wording here read "a zero-volume bar at each partial-month
  > boundary", implying a Binance monthly-file artifact. That is **factually false**: all 4 bars are
  > **mid-month**, **0 of the 106 month-first bars** in the archive are zero-volume, and 2 of the 4
  > (`2020-12-21 14:00`, `2021-02-11 03:00`) sit *immediately before* a missing-bar gap. They are
  > **exchange-outage no-trade bars** (`2019-06-07 21:00`, `2020-12-21 14:00`, `2021-02-11 03:00`,
  > `2023-03-24 12:00`) — a data-integrity problem in the upstream exchange feed, not a
  > month-partitioning artifact. The **symptom, the non-finite guard and the requirement are
  > unchanged**; only the attributed cause was wrong. Git history is left intact deliberately —
  > `bfe32aa`'s commit message still carries the old wording.

---

## 10. DEVIATIONS — labelled

1. **DEVIATION — CAND-5 is inside the outcome, not a separate one.** RESEARCH-3 §6.3 recommends
   landing it as item 1 of a five-item order. I collapse items 1 and 3 into one outcome. Justified
   from the consumer: CAND-3a's acceptance criterion is a two-arm matrix number, and `report`/
   `_build_claims` is the only thing that produces one — shipping the store without the gate ships
   an unadjudicable claim. *Visible so a reviewer does not read it as scope creep.*
2. **DEVIATION — R4 labels the venue in the model's `config.yaml`, not in the sibling's
   `_meta.json`.** Justified from the consumer: this repo consumes `kraken-market-data` as a pinned
   flake input (`rev 055d7f6`), so a sibling edit would not reach `nix develop` without a
   `flake.lock` bump and a push, and the builder owns a worktree of *this* repo. `train.py` already
   writes `n_features`/`n_bars` into the artifact as provenance; the venue belongs beside them, on
   the artifact the reviewer already opens. Sibling `_meta.json` is a follow-up, not dropped.
3. **DEVIATION — `market_data_store: null` STAYS `null` in `configs/default.yaml`.** The literal
   reading of "flip the config key" would edit the shipped default; that would make
   `read_ohlc_dataframe` raise `ValueError` from `_resolve_store` (`data.py:1244-1251`) on any host
   without the store. The flip belongs in `configs/deep-history.example.yaml:81` (already correct)
   and in a per-model config. *This is the one place a downstream builder could reasonably
   disagree — the consumer's code is unambiguous that a non-null unresolvable path raises, so the
   decision stands.*
4. **DEVIATION — `since`/`until` push-down (CAND-3b, ~21 ln) is DEFERRED**, against RESEARCH-3's
   own ordering which places it at step 4. Justified from the consumer: `train.py:215-226` and
   `backtest.py:400-419` never pass `since`/`until` at all, so on the default path the push-down is
   dead code; and a whole-store read is **0.142 s** against **0.010 s** windowed (RESEARCH-3 §1.5), so
   the saving is not load-bearing for a 158 s-seeded store. It becomes worth landing the moment
   Phase 6 pins a window and the split is computed in Python rather than in `store.read`.
5. **Width figures are my own measurements, and they differ from RESEARCH/AUDIT by one column at the
   full allow-list.** Measured on this host at `a4c14d4`: bare **52**, shipped funding file **60**,
   full allow-list **66** (AUDIT.md:116 and RESEARCH §1 quote 65 at the full allow-list; the +1 is
   the microstructure `spread` column, which I count and the earlier passes evidently did not). I
   am reporting the measured numbers and flagging the discrepancy rather than quietly adopting
   either. The two figures this decision actually rests on — **52** and **60** — match both sources.

---

## 11. WHAT THE BUILDER OWNS, IN ONE PLACE

1. **CAND-5 gate** in `tools/model_matrix.py` — behind the count gate, `n>=3 AND pooled_iqr>0`,
   output a **ratio**. §6. Four regression cases. (~50 ln)
2. **`just store-plan` / `store-seed` / `store-stats` / `store-verify`** — hermetic, run under this
   repo's dev shell, **assert `store_mode == "market-data"`, refuse `fallback-csv`**. §7. (~40 ln)
3. **Pinned-window example + venue label** in `configs/deep-history.example.yaml`, the label key in
   `configs/default.yaml` (comment-only, `market_data_store` stays `null`), one INFO log line on the
   store leg naming the venue. (~25 ln)
4. **Tests** for 1 and 2. (~60 ln)
5. **Not in scope:** CAND-4, the `since`/`until` push-down, any new repo, any new flake input, any
   sibling-repo commit.

**Verification the builder must run:** `nix develop --command bash -c "python -m pytest -q"` from
the repo root (never `.venv/bin/python` — its editable install points at the MAIN checkout and will
silently test a different tree), then `nix flake check --no-build`, then a store-backed
`train` + `backtest` against the live-leg baseline showing `n_bars` **76,561 vs 721**, `n_features`
**60 vs 60**, `start_index` **24 vs 24**, and disjoint train/eval slices over a pinned window.

---

*End of decision. Written by the architect for team `audit-pipeline-1002`; nothing outside this file
was modified, committed or deleted.*

---

## 12. USER DECISIONS — 2026-10-02, binding on the reviewer

Recorded at the checkpoint between Phase 4B and Phase 5. These are decisions, not proposals.

1. **`configs/default.yaml` stays as-is.** `market_data_store: null` and `since/until: null` are
   **coupled**: pinning without a store yields 0 train / 0 eval bars, so making out-of-sample the
   default would break fresh clones. Pinned windows live in `configs/deep-history.example.yaml` and
   in per-model `models/{TICKER_ID}/{NAME}/config.yaml`.
2. **When `since`/`until` are null, the output must label the result `IN-SAMPLE`** so it cannot be
   read as out-of-sample. Implemented by `builder-inlabel`.
3. **Pinning with no store must fail with a message naming the seed recipe**, not a bare
   `NotEnoughDataError`. Implemented by `builder-inlabel`.
4. **The mixed-matrix harness defect (§9A.1) is Phase 7, not this pass.** For this pass's gate the
   two arms are scored as **two separate matrices**, each with its own `min_bar_ratio` reference.
   The defect is nevertheless documented in §9A.1 so the limitation is on record before it is fixed.
5. **Required before the reviewer's gate verdict:**
   - the non-finite guard swept across every ratio/log/pct_change feature, plus a hard finite-check
     after normalization on **both** `fit` and `transform`, failing loudly and naming the offending
     column (`builder-inf`);
   - **live-arm width-neutrality proven bit-identical** before and after the fix — 60 features,
     `start_index` 24 — so no `models/` artifact is invalidated;
   - the **76,562 vs 76,561** bar-count discrepancy resolved and attributed to a specific bar
     (leading hypothesis: the store arm's live upsert leg contributed one additional bar);
   - the **+1800% seed** confirmed *not* a degenerate artifact of the zero-volume boundary bars
     before it is described as seed noise.

### Two-reviewer-group split for this pass (busy-time limit)

Three agents were aborted by the busy-time limit during Phase 4B, all on briefs that bundled
implementation with verification. This pass therefore separates them: `builder-inf` and
`builder-inlabel` implement (disjoint file ownership: `features.py` vs
`data.py`/`backtest.py`/`export.py`), and the reviewer verifies. **No teammate both implements and
verifies its own claim.**

### 14.0 COST-AWARE GATE RESULT (measurement track, merged after this section)

Parameters applied **exactly** as §13.1 pre-registered: threshold read from the shipped module
and `assert`ed `== 1.0` (not passed as a literal), cost basis `fee_rate: 0.0026` /
`slippage: 0.0005`, shipped estimator, ticker `ETH_USD`, seeds 42/43/44, **no retraining**.

| metric (live↔store) | live median | store median | pooled IQR | gap | **ratio** | **verdict** |
|---|---|---|---|---|---|---|
| `excess_return` (headline) | −12.44% | −341.22% | 3.97% | 328.79pp | **82.91** | **RESOLVED** |
| `total_return` | −5.26% | −99.76% | 3.89% | 94.51pp | **24.29** | **RESOLVED** |
| `sharpe` | −0.719 | −4.976 | 1.404 | 4.257 | **3.03** | **RESOLVED** |
| `max_drawdown` | 8.51% | 99.85% | 1.69% | 91.34pp | **53.91** | **RESOLVED** |

**Stated plainly, as §13.3 required: the strategy loses money after costs in BOTH arms, in all six
cells.** Live median −5.26% (equity $9,474 / $9,062 / $9,813). Store median **−99.76%** (equity
**$23.68 / $12.03 / $818.02**), 99.85% max drawdown — a wipeout, not a drawdown statistic. The
policy re-enters every **~1.22 bars in the live arm** (1.216 / 1.191 / 1.234, median 1.216) and every
**~1.43 bars in the store arm** (1.649 / 1.429 / 1.139, median 1.429), while replaying 110× more bars.

**The store arm's cost ratio is NOT directly recomputable, and no single figure is claimed.** The
backtest records carry `fee_rate`, `slippage`, `n_bars`, `num_trades` and `final_equity` but **no
notional or position size**, so absolute cost cannot be rebuilt. Two reproducible proxies, and
**neither is picked as the true ratio**:

| proxy | per-seed | median | what it assumes |
|---|---|---|---|
| seed-matched **trades** ratio (`store num_trades / live num_trades`) | 81.0× / 91.6× / 118.9× | **91.6×** | fixed notional — cost scales with trade count |
| **final-equity drag** ratio (`ctrl_equity − cost_equity`) | 34.1× / 18.6× / 179.7× | **34.1×** | fractional equity on a compounding curve |

They disagree by ~2.7× because the store arm's balance collapses (to $23.68 / $12.03 / $818.02), so
drag is measured over a shrinking notional. The drag proxy's 179.7× outlier is seed 44, whose
frictionless `ctrl_equity` is **$190,009** — the +1800% seed — so its drag spans a ~19× notional and
is not comparable to the others; that is why the median, not the mean, is quoted. **The earlier
`~145×` figure this line carried is withdrawn: it is not derivable from these records under either
sizing.** Both proxies feed no verdict — §13.3 forbids directional framing either way.

**No "store arm improved" claim is made in either direction.**
What CAND-3a delivered is unchanged and orthogonal: *an out-of-sample measurement became possible
at all.*

**Both previously-RESOLVED rows re-derived: the verdicts survive, the evidence does not.** `sharpe`'s
gap **sign flips** (+1.005 higher frictionless → 4.257 lower under costs; every cell negative) —
exactly the churn artifact §13.2 predicted, now measured rather than argued. `max_drawdown`'s
magnitude inflates 5× on a larger numerator, not more signal.

**CORRECTION — §13.1's pre-registered magnitude prediction was wrong, and the error was mine.**
§13.1 predicted ratio ≈1.4 (pooled IQR ~11.7pp against a 16.24pp gap). Measured: pooled IQR
**3.97pp**, gap **328.79pp** → ratio **82.91**, ~59× off. The pre-registration held the **gap**
fixed while swapping a cost-aware **pooled IQR**, but the gap is the variable that moved most
(16.24pp → 328.79pp, the store arm's return travelling −322pp). Holding one side of a ratio
constant across a change that moves it 20× is not a valid projection. **The verdict is
unaffected** — both figures sit on the same side of a threshold fixed and committed in advance —
and **the threshold was not touched to reconcile it.** What the pre-registration bought — the
direction call, and the rule that `RESOLVED` is not a claim the store helps — both held.

**Control fidelity.** The store arm reproduces frictionless to ~0.1% (+224.86 / +52.98 / +1800.10
vs recorded +225.13 / +53.17 / +1800.10; ratio 0.031 vs 0.037). **The live arm drifts** (+4.27 /
−1.14 / +8.65 vs +4.07 / −0.44 / +8.87) because it re-fetches a **rolling** REST window. **A
live-arm backtest cannot be replayed bit-identically across days; a store-backed one can** — an
unplanned argument for what this pass built.

---

## 13. PRE-REGISTERED DISPERSION RULE — committed BEFORE the cost-aware re-run

**Written and committed before any cost-aware number exists.** That ordering is the entire point
of this section: the threshold below is fixed now, so it cannot be tuned to produce a preferred
verdict once the result is known.

**Background (Phase 6 finding F1).** The frictionless gate yielded `NOT SEPARATED` on both return
metrics, and §9A.2 attributed that to seed noise. That attribution is **wrong**. At Kraken taker
costs the store arm's within-group IQR collapses from 1961.45pp to 11.68pp (168×) while the
return goes from +2015% to −88%. The frictionless dispersion was measuring **cost sensitivity**,
not seed variance. §9A.2 is therefore superseded, not patched — see §13.2.

### 13.1 The rule, fixed now

| | |
|---|---|
| **Cost basis for every quoted number** | **Kraken taker: `fee_rate: 0.0026`, `slippage: 0.0005`** (31 bp round-trip) |
| **Estimator** | unchanged — the shipped `pooled_within_spread` = **median** of per-group IQRs over groups with `n >= 3`, and `dispersion_verdict` |
| **Ratio** | `gap / pooled`, `gap = abs(median_A − median_B)` over adjacent arms. **The ratio is a fact.** |
| **Threshold** | **`DISPERSION_RATIO_THRESHOLD = 1.0`, UNCHANGED** |
| `ratio >= 1.0` | `RESOLVED` |
| `0 < ratio < 1.0` | `NOT SEPARATED` |
| `n < 3`, or `pooled == 0`, or missing arm median | `UNDEFINED` (no number invented) |
| **Ordering** | must sit **BEHIND** the count gate and require `n >= 3 AND pooled > 0` |
| **Never tuned post hoc** | the threshold stays 1.0 whatever the re-run returns |

**Pre-registered expectation, stated before the run:** with IQR ≈ 11.7pp under costs, the
`excess_return` gap (16.24pp) gives a ratio of roughly **1.4**, i.e. it may well come back
`RESOLVED` — and if it does, **`RESOLVED` is not a claim that the store helps.** A resolved
return difference at 31 bp costs on a churn-dominated strategy means the arms differ measurably
in *cost sensitivity*, nothing more. **The verdict will be reported as it falls, in either
direction, with no "store improved" or "store hurt" framing.**

### 13.2 §9A.2 is SUPERSEDED, not patched

The frictionless rows in §9A.2 (`excess_return` 0.037, `total_return` 0.503, `sharpe` 1.400,
`max_drawdown` 10.736) are retained **only as a labelled diagnostic** of the churn effect. They
are not seed-variance measurements and must never be quoted as such. Both `RESOLVED` rows are
churn artifacts (seed 44 Sharpe 2.717 → −0.550 under costs), so they are re-derived from scratch
under §13.1 rather than carried forward.

### 13.3 What the report must state regardless of the verdict

If the cost-aware numbers show the strategy losing money in **both** arms — which Phase 6's
frictionless→taker swing suggests — the report **says so plainly**. No "store arm improved"
language in either direction. What CAND-3a delivered remains: *an out-of-sample measurement
became possible at all.*

---

## 14. KNOWN LIMITATION — features are computed on bar counts, not wall-clock

**Status: DETECTED AND LABELLED, NOT FIXED.** Recorded 2026-10-02 from Phase 6 finding F6.
**This section is a standing caveat on every number computed from the store arm.**

### 14.1 What was measured

`just store-verify` now runs `tools/store_gap_scan.py` alongside the sibling seeder's own
contiguity check. On the shipped ETH/USD 60-minute store (106 month files):

| | |
|---|---|
| bars present | **76,564** (was 76,563 at Phase 6 review; the live append leg adds ~1 bar per run) |
| span | `2018-01-01T00:00Z` → `2026-10-02T17:00Z`, **76,721 hours** |
| **missing bars** | **158** across **28 gaps** (157 at review time; the +1 is the live leg) |
| largest gap | `2026-08-31 23:00` → `2026-09-02 14:00` — **38 missing bars**, spanning **39 elapsed hours** |
| other gaps | 32 (2018-02, off-grid boundary), 10 (2018-06-26), 10 (2019-05-15), 8 (2019-08-15), 7 ×2, 6, … |

**Units, because this gap has already been reported both ways and read as a contradiction:**
*38 missing bars* and *39 hours* are both correct. `N` missing bars means the two surviving bars
are `N+1` steps apart.

### 14.2 Why it matters

Every feature in `features.py` is computed over a **window of rows**, and the environment's
`start_index = 24` skips 24 **rows**:

- `return_1` reports the return between whichever two rows happen to be adjacent. Across the
  39-hour seam that is a **39-hour return presented as a 1-hour return**.
- `sma_24`, `rsi_24`, `obv_slope_24` and `bollinger_24` span 24 rows that may cover **more than
  a day**.
- After z-scoring, a 39-bar jump is indistinguishable from a 1-bar jump. **No guard in this repo
  can detect it**, because every value involved is finite and correctly computed from the rows it
  was given. Only the timestamps know.

**The 38-bar hole is at the seed/live-append seam**, in the most recent month: the month-file
boundary where the Binance-archive seed ends and this repo's live append leg takes over. That is
precisely the region a live deployment trades, so the hole is not confined to the training slice.
The tool labels that gap **by name** (`seed/live-append seam`) and flags it on its own line.

### 14.3 What was deliberately NOT done

**No reindexing, no interpolation, no gate.** `store_gap_scan.py` detects and labels; it does not
repair and does not fail the run. Adding a hard failure would have broken the store arm on data
that is otherwise usable, which is a bigger decision than a bug-fix pass should make.

### 14.4 The real fix, and why it is not called CAND-3b

The fix is to compute features over a **reindexed, gap-filled bar grid**, so a window means N
*hours* rather than N *rows* — i.e. make the windows time-aware and decide explicitly what a
window spanning a gap means (hold last value, or mask the row).

**It is not called CAND-3b here on purpose.** `CAND-3b` is **already taken** in this document:
§10 item 4 defines it as the `since`/`until` push-down — a ~21-line, store-only **efficiency** item,
explicitly DEFERRED as not load-bearing (whole-store read 0.142 s vs windowed 0.010 s). *(The venue
label is **not** part of item 4: §10 item 2 moved it **out** of the sibling's `_meta.json` and into
the model's own `config.yaml`, and treats the `_meta.json` label as a follow-up, not dropped. §10.2
therefore says the opposite of what attributing it to item 4 would imply.)* Reusing that id for a
correctness fix would be the same false-claim failure this pass exists to remove, so this item needs
its own registration before it is referred to by id. *Raised with the lead rather than invented here.*

### 14.5 The §14.4 correction above was itself imprecise in one half — a definitional mix, not only staleness

The CORRECTION block in §14.4 concedes that its pooled-IQR half was "stale". That undersells it, and
the independent F1/F2 re-review caught the sharper defect. The two figures were not merely drawn at
different times — **they were not the same statistic**:

- **11.68pp** was the Phase 6 **store-arm-only** IQR, computed on a **76,539-bar** snapshot, before
  the cost-aware estimator existed.
- **3.97pp** is the **shipped estimator's** pooled-within-spread, which takes the **median of BOTH
  arms'** IQRs, on a **76,540-bar** snapshot.

So the 1.4 → 82.91 ratio compares a one-arm spread against a two-arm median. Two independent errors
were folded into that half: the **snapshot** was one bar short, and — the material one — the
**definition** changed underneath it. The gap half (16.24pp → 328.79pp) is unaffected; that
comparison is like-for-like on the same metric.

**This does not move the verdict, and the reason is worth stating precisely.** Both figures sit on
the same side of a threshold that was committed (`97a2a52`) before the run and never touched since.
A ratio's *magnitude* was mispredicted; its *sign against a fixed threshold* was not in question.
The correction is recorded here rather than folded into §14.4's text so the original wording stays
visible as the reviewer saw it.

### 14.6 Phase 6 findings F2 and F3, at decision level

F4 and F6 got records at the point they arose (§9A.3's CORRECTION block, §14). **F2 and F3 did not** —
their dispositions lived only in a docstring and an error string, so a reader auditing this gate's
findings would have had to read diffs to learn them. Recorded here now.

**F2 — the vacuous seam test and five false docstrings.** *Finding:* deleting `compute`'s non-finite
seam left **all 31** non-finite tests green, because pandas' `rolling` masks an infinity on its own;
and five docstrings asserted the *opposite* of pandas' behaviour while claiming to have measured it.
Severity MAJOR — a test that cannot fail, asserting a mechanism that does not exist.

*Option (a) — delete the test and the docstrings.* **Rejected.** The underlying hazard is real; deleting
the evidence would hide it and re-invite the same discovery as a "bug" later. *Option (b) — replace
with a test that pins the seam by an effect pandas does **not** have on its own.* **Chosen**, on the
reason that the seam's only defensible justification is the **zero** price, not the infinity: zero is
in `_NON_FINITE_INPUTS`, a zero price is reachable in real OHLCV, and without the seam a zero enters
a rolling window as a real number and yields `sma_4` **91.41** against a ~122 price and `bb_lower_4`
**−14.14** — finite, plausible, and silent. So the replacement pins that symptom, and the old inf
assertion was **kept and labelled** rather than deleted (it passes with the seam removed; its label
now says so). *Option (c) — invert the claim to "the seam prevents infinities".* **Rejected as still
false**: neither aggregation emits an infinity, so no `isinf` assertion can observe the seam.

*Consequence accepted:* with the seam mapped to NaN, `rolling` reports a poisoned window as NaN
("unknown") while `ewm` **skips** it and reports a **stale carried value** — finite and wrong, and
invisible to `_require_finite`. That is **out of scope for this pass** and is pinned by
`test_rolling_masks_but_ewm_skips_a_non_finite_price_this_guard_cannot_see`, which asserts `ewm`
still skips a non-finite input so a future pandas change surfaces here. See the `KNOWN GAP` note in
`compute` and the `_rsi` docstring.

**F3 — `market_data_store: null` does not turn the store off.** *Finding:* `data.py`'s
`MarketDataStoreUnavailableError` told the reader to "Set `market_data_store: null` in the config",
but `_resolve_env_setting` treats a YAML `null` as *unset* and **skips** that source, so a run config
saying `null` falls through to the **model's own** `config.yaml` — where the store path comes from —
and the store silently stays on. A store-trained model would keep 76k bars while believing it went
live. *Option (a) — honour an explicit sentinel (`""` / `--no-store`).* **Rejected**: it changes
resolver semantics, which is outside this pass and would need its own test matrix. *Option (b) —
correct the message to name the file that actually has to change.* **Chosen**: the advice now names
**the model's own `config.yaml`**, because that is the file the reader must edit. The deliberate
`null`-means-unset semantics are **kept** — YAML `key: null` legitimately means *unset*, and
overriding it would break every other config layer that relies on it.

*Residual, accepted and recorded:* with option (b) the trap is still reachable by editing the wrong
file, but the message no longer *instructs* it. A working override remains the correct future fix.

---

## 15. GATE CLOSING RECORD

**Final verdict: `NEEDS_FIX on R1, resolved by docs-only repair, confirmed by the reviewer`.**

This is deliberately **not** recorded as a clean pass. The Phase 6 gate returned `NEEDS_FIX` with
seven findings; F1 was the only MAJOR *result* finding and it was resolved by re-running the gate
under costs (§14.0, §13), but F2 carried a MAJOR *truthfulness* finding whose repair was incomplete.

**What the narrow re-review established.** A reviewer independent of the implementing agent
re-examined F1 and F2 only, per the user's exit condition. It returned:

- **F1 — CLEAN on all five checks.** The threshold was not tuned (`tools/` provably zero-diff
  against `97a2a52`); all 20 figures in §14.0 reproduce from the per-seed records through two
  independent implementations agreeing to 1e-12; all six cells lose money and the report says so
  without directional framing; §14.4's self-correction is honest (see §14.5); pre-registration
  precedes the result commit by direct ancestry.
- **Width-neutrality held, byte-identical** — obs `93edc733…`, arr `6a88a379…`, 60 features,
  `start_index` 24, 0 non-finite obs cells. **No `models/` artifact is invalidated.**
- **F2's structural repair is real.** The vacuous test is dead: deleting the seam now takes down
  exactly one test, with `sma_4` at 91.41 against a ~122 price — the original symptom. The old
  infinity assertion was **kept and labelled**, not deleted, and that label is true under mutation.
  The `ewm` hazard is recorded as a known gap and its pin provably bites in both directions.
- **R1 — one material defect, in prose only.** `_rsi`'s docstring asserted a pandas mechanism that
  is the *opposite* of what pandas does, contradicting the `KNOWN GAP` note added in the same
  commit. Repaired; no executable line changed.

**Why the verdict is not "clean".** R1's blast radius was deliberately bounded — no functional
regression, no coverage gap — and it was repaired docs-only. But the repair went through **three
review rounds**, and two of the rounds found further defects: one pre-existing (a swapped
cosmetic/load-bearing claim that contradicted itself within three lines, in the very paragraph the
repair cites as governing) and **one introduced by the repair itself** — a clause asserting the chain
"breaks at its first real link" while the next clause asserted the opposite. That last one is the
reason this record exists: the lead described the wording in a message as "second" while the file
said "first real", and only a reviewer reading the source rather than the description caught it.
**The lesson recorded for this pass: a repair that cites its own prose is not finished when the
prose is corrected — it is finished when every falsifiable clause in the cited text has been
measured. But measured *by someone who re-derives it*, not by a script the repair wrote about
itself.**

**Mechanical evidence, not self-certification.** Each round was verified by: an AST comparison with
docstrings stripped (itself self-tested against six mutants after two real bugs in the proof were
found); a width-hash re-run; the full suite; and a `tools/` zero-diff check against `97a2a52`. Those
four are structural and hold: no executable line changed, the width hash is byte-identical, 455
tests pass, and the measurement track is untouched.

**The confirmation rests on the reviewer's own 12-clause probe — explicitly NOT on a script.** A
clause-checking script was written during this pass and an earlier draft of this record cited it as
covering the final state. **That citation was withdrawn: the script was proved inert.** The reviewer
cloned the final commit, restored *both* round-4 defects in the docstring while leaving the
executable body untouched — `(A)` back to "``avg_gain`` is ``-0.0``" with its **correct** outcome
clause intact, `(B)` back to "the whole frame" — and ran the script against it. Result: **11 of 11
reason checks PASS, exit 0, "ALL CLAUSES AND ALL STATED REASONS MEASURED TRUE"**, against a
docstring asserting both defects. Two reasons, both structural:

- **No binding.** Every assertion measures pandas' behaviour. None is bound to any word of the
  docstring, so "avg_gain is never -0.0" passes *because pandas behaves that way* — orthogonal to
  what the docstring *says*. Measuring the right things is not the same as checking the text.
- **The extraction never found the clause.** The clause-splitter breaks on the `.` inside `-0.0`,
  `0.0` and `1.001`, so it prints mid-sentence fragments; and it requires the literal "so that", so
  the very clause whose stated reason was wrong last round ("clipped at zero below, so avg_gain
  cannot be negative") is one it never displayed.

This is the **same failure mode the whole pass exists to remove**, one level up: a verification
artifact that reports green while the defect it was built to catch is present. It is recorded here
rather than quietly deleted because the fact that it was *proved* inert — by mutation, at the
reviewer's initiative — is the actual evidence for the verdict. A gate whose evidence is only ever
green is worth nothing; this one survived an attempt to falsify it and the falsification attempt is
published alongside.

**To actually close the class**, each assertion would have to name the docstring substring it defends and fail when that substring is absent or altered.

### 15.1 OPEN FOLLOW-UP — a docstring gate that is actually bound to the text

**Status: OPEN. Not claimed as done. Not deferred.** This is a real gap with a named trigger, not
work quietly dropped at the end of a pass.

**The class, stated so it is recognisable next time.** *A verification script whose assertions
measure library behaviour but are not bound to any docstring text can report fully green while the
docstring is wrong.* It is the R1 failure mode one level up. The tell is a script that asserts facts
about a dependency and prints "PASS" — it is checking pandas, not the prose, and the two can drift
apart without anything going red. The fix is not more assertions; it is a binding from each
assertion to the exact substring it defends.

**Trigger.** *A mechanism-asserting comment or docstring is added or edited in
`kraken_trading_bot/rl/features.py` again.* At that point, and not before, each assertion must
(a) name the docstring substring it defends and (b) fail when that substring is absent or altered.
A script that satisfies (a) and (b) is worth having; a script that only adds assertions is what was
withdrawn above, and adding more of them would be worse than useless — it would look like coverage.

**Why it is not being built now, on the merits rather than for the sake of the record.** The
docstring it would guard is correct and independently verified. A harness maintained for one
docstring costs more to keep honest than the defect it would catch, and it would still compare
*text against text* — it would not check text against pandas behaviour, which is the thing that was
wrong in every round of this pass. Building it now would mostly be a way to feel finished.

**The `/tmp` script is withdrawn and is not part of the evidence for this gate.** It lives in
scratch state outside the repository, it was proved inert, and nothing in §15 rests on it. The
verdict above rests on the reviewer's independent 12-clause probe plus the four structural checks.
