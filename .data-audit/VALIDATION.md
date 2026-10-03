# VALIDATION — Phase 6 gate: G2 funding-history backfill

**Verdict: PASS** (with two NEW findings for the lead to record, and one
self-inflicted receipt caveat at §7.3 that must not be pasted onward).

Reviewer: `reviewer` (qa). Pass date **2026-10-02/03** (all live
measurements below are stamped). `HEAD = a7a9cbe`. Nothing pushed.
Sibling `kraken-funding-rates` @ `dc49847`, tree clean (asserted before
seeding: `git status --porcelain` empty, `HEAD == dc49847afa11320b43804a82c0ee6a437603161a`).

Pre-registration commit `069a826`. Measurement track
`tools/model_matrix.py` is byte-identical to it, so every threshold in
this document is the pre-registered one and none was moved.

---

## 1. Gate receipts (verbatim)

### 1.1 `audit-verify`, both references, one invocation

```
$ nix develop --command bash -c "python tools/audit_checks.py verify --prereg 069a826 --since 8cada4e"
audit-verify  HEAD=a7a9cbe  prereg=069a826  since=8cada4e
  measurement-track  MATCH  tools/model_matrix.py byte-identical to prereg 069a826
  ast-proof          SELF-TEST OK  (8 mutants detected correctly)
  executable-ast     MATCH  6/6 files unchanged since 8cada4e (docs+strings blanked)
  suite              PASS  457 passed
  width              SKIP  (no --frame)
  RESULT             PASS
```
Exit 0. **No `SELF-TEST BROKEN`** — the AST proof is trustworthy, so
everything else it reports may be relied on. `MATCH 6/6` means nothing
executable in the six guarded files moved since `8cada4e`.

### 1.2 Same command with a frame (adds the width fingerprint)

```
$ nix develop --command bash -c "python tools/audit_checks.py verify --prereg 069a826 --since 8cada4e --frame /tmp/rev/ohlcv_base.parquet"
audit-verify  HEAD=a7a9cbe  prereg=069a826  since=8cada4e
  measurement-track  MATCH  tools/model_matrix.py byte-identical to prereg 069a826
  ast-proof          SELF-TEST OK  (8 mutants detected correctly)
  executable-ast     MATCH  6/6 files unchanged since 8cada4e (docs+strings blanked)
  suite              PASS  457 passed
  width              MATCH  ohlcv_base.parquet features=49 start_index=24 usable=697 nonfinite=0
                     (no --expect-obs/--expect-arr given: fingerprint only, not an assertion)
  RESULT             PASS
```
Exit 0. **Read `features=49` as an artifact of MY frame, not as a
shipped width — see §7.3 and F-15.** The frame I passed is a cached raw
OHLCV parquet; `width_check.py` never calls `add_derived_ohlcv_features`,
so the presence-gated trio is missing. 49 is not a state this repo ships.

### 1.3 Bot suite, exact command from the brief

```
$ nix develop --command bash -c "python -m pytest -q"
457 passed, 24 warnings in 30.12s
```
Exit 0. Matches the expected 457. (`audit_checks` invokes
`pytest tests/ -q` rather than `python -m pytest -q`; both were run and
both report 457.)

### 1.4 `tools/width_check.py` — the width ladder, repo's own committed tool

```
$ python tools/width_check.py --frame /tmp/rev/frame_derived_only.parquet
width-check  frame=frame_derived_only.parquet  bars=721  features=52  start_index=24  usable=697
  nonfinite_obs_cells=0  arr_finite=True  insample_finite=True

$ python tools/width_check.py --frame /tmp/rev/frame_backfilled.parquet
width-check  frame=frame_backfilled.parquet  bars=721  features=60  start_index=24  usable=697
  nonfinite_obs_cells=0  arr_finite=True  insample_finite=True

$ python tools/width_check.py --frame /tmp/rev/frame_baseline.parquet
width-check  frame=frame_baseline.parquet  bars=721  features=60  start_index=24  usable=697
  nonfinite_obs_cells=0  arr_finite=True  insample_finite=True
```

---

## 2. Integration-test matrix — backfilled vs baseline

The gate is that the bot now *sees* a live funding series instead of 2
distinct values on 13/721 bars. Proven by running it, not by reading the
diff. Both arms used `market_data_store: null` (live-fetch leg) and a
scratch config differing in exactly one key (`funding_features_file`).

| | **backfilled arm** | **baseline control** |
|---|---|---|
| signal file | `/tmp/rev/signals/eth_usd_funding.jsonl` (8,792 rec) | repo `signals/eth_usd_funding.jsonl` (1 rec) |
| `n_features` (exported) | **60** | **60** |
| training bars | **721** | **721** |
| obs features (trained) | **60** | **60** |
| **bars replayed** | **697 / 721** | **697 / 721** |
| **trades taken** | **546** | **558** |
| total return † | 7.52 % | 7.10 % |
| Sharpe † | 0.987 | 0.827 |
| max drawdown † | 5.58 % | 6.57 % |
| buy & hold | 6.44 % | 6.44 % |
| excess return † | 1.08 % | 0.66 % |
| final equity † | 10,751.58 | 10,709.55 |
| evaluation | IN-SAMPLE | IN-SAMPLE |

**† READ THE THREE ROWS MARKED † AS A WIDTH AND PLUMBING CHECK, NOT AS AN
EFFECT.** They are **one run per arm**, on a 721-bar in-sample window, with
PPO's own run-to-run stochasticity unquantified here. The 0.42-pt return
difference and the 0.827 → 0.987 Sharpe difference are **not evidence that
the backfill improved results**, and must never be quoted as if they were.
A single paired run cannot separate a real effect from seed noise, and
this repo's own `model-matrix` skill exists precisely because within-config
seed spread routinely exceeds the between-config effect being measured.
What these rows establish is narrower and sufficient for the gate: the run
**completes at unchanged width, replays the same 697 bars, takes a normal
number of trades, and does not error** — i.e. the channel is plumbed and
the observation is intact. The *feature vectors* are what must match, and
they do (§6).

**Equivalence gate: PASS.** |Δ return| = **0.42 pts**, same sign, against
the ~5-pt band. Not gated on exact equality — PPO is stochastic across
runs even at a fixed seed.

**Both arms are IN-SAMPLE** (`data_window.since`/`until` both null, the
shipped default). The bot says so itself on every run: *"this return
describes the fit, not a prediction … no out-of-sample claim is
supported."* Nothing here is an OOS result and it must not be quoted as
one.

### 2.1 The magnitude clause — "completes without error" is NOT the claim

A silent collapse to ~1 bar / 0 trades would pass a width guard while
destroying the run. Recorded explicitly:

- **bars replayed 697/721 on BOTH arms** — equal to `usable_bars=697`
  that `width_check` independently computes as `721 − first_tradable_index(24)`.
  The gate's tradable window and the backtest's replayed window are the
  same 697 bars, cross-checked from two different code paths.
- **trades 546 and 558** — not 0, and not 1-per-bar-degenerate.
- columns asserted **present by name**, not by width (§3).

---

## 3. Consumed-proof via `export-data` (the cheap proof)

```
$ kraken-trading-bot export-data --ticker ETH_USD --config /tmp/rev/cfg/backfilled.yaml --pages 6 --output /tmp/rev/export_backfilled.csv
Built export frame: 721 bars, 72 columns (60 features, 2 signal)
Exported 721 bars x 72 columns -> /tmp/rev/export_backfilled.csv
  window:   2026-09-02T23:00:00Z -> 2026-10-02T23:00:00Z

$ kraken-trading-bot export-data --ticker ETH_USD --config /tmp/rev/cfg/baseline.yaml --pages 6 --output /tmp/rev/export_baseline.csv
Built export frame: 721 bars, 72 columns (60 features, 2 signal)
Exported 721 bars x 72 columns -> /tmp/rev/export_baseline.csv
  window:   2026-09-02T23:00:00Z -> 2026-10-02T23:00:00Z
```

**`n_features = 60` on both arms**, identical column lists, and — the
control that makes the rest of this table meaningful — **both arms
fetched the same window**, with `open/high/low/close/vwap` byte-equal
across the two exports. (`volume` and `count` differ by a few ticks:
Kraken's in-progress candle ticks up between the two calls two seconds
apart. Prices did not move, so the feature comparison holds.)

### 3.1 Columns present BY NAME

All three required names are present in the exported frame **and** in the
trained `normalization.npz` `feature_names` (60 entries, identical lists
across arms):

| column | in exported frame | in `normalization.npz` `feature_names` |
|---|---|---|
| `funding_rate` | yes | yes — index **50** |
| `signal_observed` | yes | yes — index **54** |
| `signal_age_hours` | yes | yes — index **53** |

### 3.2 The claim itself, on the consumed artifact

| column | backfilled: distinct / nonzero | baseline: distinct / nonzero |
|---|---|---|
| **`funding_rate`** | **721 / 721 (721/721)** | **2 / 13 (13/721)** |
| `signal_observed` | 1 / 721 | 2 / 13 |
| `signal_age_hours` | 1 / 0 | 14 / 720 |
| `basis` | 1 / 0 | 2 / 13 |
| `open_interest` | 1 / 0 | 2 / 13 |
| `funding_rate_prediction` | 1 / 0 | 2 / 13 |
| `vol24h` | 1 / 0 | 2 / 13 |
| `spread` | 1 / 0 | 2 / 24 |

**`funding_rate` goes from 2 distinct values on 13 of 721 bars to 721
distinct values on 721 of 721 bars.** That is the gate, met, measured on
the frame the agent actually consumes. Independently corroborated from
the trained artifact: fitted **STD** of `funding_rate` is **0.02334**
(backfilled) vs **0.00336** (baseline) = **×6.941**, matching DECISION
§1's claimed ×6.9 from a completely separate route.

**Non-finite cells in the exported frame: 0 on both arms.**

---

## 4. Persistence and coverage

Seeded with the **production path** — the same invocation the hourly
timer's `ExecStart` and `just funding-backfill` use
(`nix run ~/Projects/kraken-funding-rates#kraken-funding-rates --`),
which exercises the real recipe end to end:

```
$ just funding-backfill ETH/USD /tmp/rev/signals/eth_usd_funding.jsonl
{"msg": "Kraken returned 8792 funding-history records for PF_ETHUSD spanning 2025-10-01T08:00:00+00:00 -> 2026-10-02T23:00:00+00:00"}
{"msg": "Wrote 8792 funding records to /tmp/rev/signals/eth_usd_funding.jsonl"}
Backfilled 8792 history record(s) for PF_ETHUSD to /tmp/rev/signals/eth_usd_funding.jsonl
  earliest: 2025-10-01T08:00:00Z
  latest:   2026-10-02T23:00:00Z
```

**A note on the command in my brief.** The brief specified
`nix develop --command bash -c "kraken-funding-rates backfill …"`. That
cannot work: `flake.nix` pins only `kraken-python` and `kraken-market-data`,
so the sibling's console script is not on the dev-shell `PATH`
(`which kraken-funding-rates` → not found). This is a **correction of the
brief, not a deviation**: `nix run` is the production path, so the seeded
artifact is byte-for-byte what the timer writes — stronger evidence than
the command in the brief would have given.

| property | measured (2026-10-02T23:39Z) |
|---|---|
| records | **8,792** |
| span | **2025-10-01T08:00:00Z → 2026-10-02T23:00:00Z** |
| distinct timestamps | 8,792 (no duplicates) |
| keys per record | **14** |
| file size | 3,047,379 bytes |

The record count is **~8,800 and grows by roughly one record per day**
(the ~366-day window is recomputed on every call). Any re-derived count
must be dated: *8,792 measured 2026-10-02*.

**14 keys, not the 13 §6 claimed** — F-12's superset, confirmed:
`ask, basis, bid, funding_rate, funding_rate_prediction, index_price,
mark_price, open_interest, relative_funding_rate, spot_pair, symbol,
ticker, timestamp, vol24h`.

### 4.1 This pass recovers 1 of 6 columns — measured at the file, not inferred

Every one of the 8,792 backfilled records carries:

```
null_counts = {'funding_rate_prediction': 8792, 'mark_price': 8792,
               'index_price': 8792, 'basis': 8792, 'open_interest': 8792,
               'bid': 8792, 'ask': 8792, 'vol24h': 8792}
```

`/historical-funding-rates` carries only `funding_rate` (plus
`relative_funding_rate`). Confirmed downstream in §3.2: `basis`,
`open_interest`, `funding_rate_prediction`, `vol24h`, `spread` are all
**1 distinct value, 0 nonzero bars** after the backfill.

**Net effect on the observation: 1 column gained a real distribution and
5 lost theirs.** Fitted-STD census over the 60 trained features:

| arm | zero-STD (degenerate) features |
|---|---|
| baseline | 3 — `price_ratio_sma_1`, `bb_width_1`, `bb_pctb_1` (structural) |
| backfilled | **10** — the same 3, plus `signal_observed`, `signal_age_hours`, and `spread`, `basis`, `open_interest`, `funding_rate_prediction`, `vol24h` |

`signal_observed` (= 1.0 always) and `signal_age_hours` (= 0.0 always) are
*correct* constants on a dense hourly file, not defects. The other five
are a genuine loss: the single live snapshot used to give them 13 real
readings, and the backfill cannot.

---

## 5. The four re-derivations

All four were re-derived on **one shared 721-bar OHLCV frame**
(`2026-09-02T23:00Z → 2026-10-02T23:00Z`, hourly, **with `vwap` and
`count`**), so the two arms differ only in the signal file. That shared
frame is the control the two disagreeing agents lacked: with
`market_data_store: null` a live fetch is not reproducible, so "builder"
and "integrator" may have measured different bars.

Frame labels used throughout, each the output of one named repo function:

| label | produced by | policy |
|---|---|---|
| `ohlcv_derived` | `add_derived_ohlcv_features` | pre-merge |
| `signals_post_fillna` | `merge_extra_features` | **after** `data.py:816` `fillna(0.0)` |
| `computed` | `FeaturePipeline.compute` | pre-fill, pre-ffill |
| `observed` | `computed.ffill().fillna(0.0)` | **the frame the policy sees** |

### 5.1 F-9 / C8 non-finite cells — **RESOLVED: 684 and 0 are the same pipeline, two frames**

| frame | baseline | backfilled |
|---|---|---|
| `ohlcv_derived` | 19 (`trade_count_zscore_20`) | 19 |
| `signals_post_fillna` | 19 (`trade_count_zscore_20`) | 19 |
| `computed` (full 721) | **964** (`spread` 708 + 256 warm-up) | **977** (`spread` 721 + 256) |
| `computed`, **tradable slice only** (697) | **684** — *all* `spread` | **697** |
| **`observed`** | **0** | **0** |

**684 is arithmetically exact:** `first_tradable_index = 24`, so the
tradable slice is `721 − 24 = 697` bars; `697 − 13` finite `spread`
readings = **684**. Every indicator warm-up NaN lives inside the first 24
bars, so on that slice `spread` is the *only* non-finite column — "all
`spread`" and "684" are the same statement.

So: **the builder measured `computed` restricted to the tradable slice;
the integrator measured `observed`.** Not a disagreement — a frame
mismatch. The integrator's **0 is the correct gate figure**, and it is the
frame that matters three times over: it is what `width_check.py` hashes
(`obs = feat.ffill().fillna(0.0)`), what `export-data` writes (§3.2
measured 0), and what `TradingEnvironment` hands the policy.
`width_check` independently reports `nonfinite_obs_cells=0` on all three
frame states.

**The builder's "identical in baseline and target" is wrong.** On the same
tradable slice: baseline **684**, backfilled **697**. The backfilled arm
is *higher*, because its file has no `bid`/`ask` anywhere, so `spread` is
NaN on every tradable bar.

**Restated criterion for C8:** *non-finite cells in `observed`
(`FeaturePipeline.compute` output after `.ffill().fillna(0.0)`), on the
tradable slice, must be 0 on every arm.* Both arms: 0.

### 5.2 F-11 `spread` nonzero bars — **RESOLVED: 13 vs 24 is ONE arm, TWO frames**

| frame | baseline | backfilled |
|---|---|---|
| `computed` | **13** (finite = 13) | **0** |
| `observed` | **24** | **0** |

Same arm, same file, same frame length. The difference is
`observed = computed.ffill().fillna(0.0)`.

**The right edge is the mechanism.** The single record sits at
`2026-10-02T00:00Z`; the frame's right edge is `2026-10-02T23:00Z`, so
the record is **23 bars back from the edge**. The 12 h carry runs
*forward* from the record's own hour, so its 13 readings occupy offsets
**−23 … −11** from the right edge (measured, exactly). The remaining
**11 bars** to the edge then inherit them through `ffill`:
**13 + 11 = 24.** The integrator's 24 is the post-`ffill` count and is the
one the agent sees; the builder's 13 is the pre-`ffill` count. The
builder's premise was right — one live record plus one 12 h carry — but it
counted the pre-`ffill` frame.

**The integrator's "byte-identical both arms" is wrong.** On the file I
seeded, `bid`/`ask` are null in all 8,792 records, so the backfilled arm
is **0 / 721**, not 24. Both arms cannot agree on `spread` unless both
files carry quotes.

### 5.3 F-8 width — **RESOLVED by measurement; do not assert the literals**

Measured with `tools/width_check.py` (§1.4):

| state | `n_features` |
|---|---|
| all three `*_features_file` keys null | **52** |
| shipped default, funding channel live (either file) | **60** |

**F-8's "3 low" is confirmed and its cause is now named.** §7.3's C1 (57)
and C2 (49) were measured on a frame that lacked the presence-gated trio
`vwap_dev` / `volume_per_trade` / `trade_count_zscore_20`. `8cada4e`'s own
commit message records "n_features 57 → 57 UNCHANGED" — the 57 is that
missing trio.

**F-13's correction is confirmed**: 52 is the *all-null* width; the
shipped default composes **60**. Sane range **52–60** is right. This pass
asserts **no width literal**; it asserts the two measured values and that
the backfill does not move the width (60 → 60, delta 0).

There is a fourth number, **49**, which is **not a shipped state**: a raw
OHLCV parquet with neither the derived trio nor the signal merge. It is
what my §1.2 receipt printed. See F-15.

### 5.4 F-10 `signal_age_hours` — **RESOLVED: the criterion is unachievable on the shipped shape**

| arm | min | max | on newest bar | nonzero bars |
|---|---|---|---|---|
| baseline (shipped live-snapshot shape) | **−1.0** | **12.0** | −1.0 | 720 |
| backfilled (dense hourly) | 0.0 | **0.0** | 0.0 | 0 |

`−1.0` is `_NO_SIGNAL_AGE` (`data.py:136`), the "no live reading at all"
sentinel. **Max = 12.0 on the baseline arm**, exactly as F-10 predicted:
`signal_age_hours` is `ages.max(axis=1)` — the **stalest** column wins —
and the single live record's `bid`/`ask`/`basis`/`open_interest`/
`funding_rate_prediction`/`vol24h` ramp 0…23 and are masked at the bound,
so the max lands on **the bound itself, 12.0**.

**C12's "max ≤ 2.0" is unachievable on the shipped shape.** Shape
measured: a live snapshot sitting on the **newest** bar region, carry
running backwards, bound 12. It holds only on a densely backfilled file
(where every bar's newest reading is 0 h old). This **confirms F-6 /
DEV-2** — leaving the bound at 12 was right.

---

## 6. Baseline-comparison fairness

The brief warns that live training windows are not reproducible while
`market_data_store: null`, so **fitted STDs, never MEANs**, are the
comparison for cumulative features. Measured on the two trained
`normalization.npz` files:

| cumulative / windowed feature | STD backfilled | STD baseline | ratio |
|---|---|---|---|
| `return_24` | 0.02202309 | 0.02202305 | 1.000 |
| `sma_24` | 465.13563486 | 465.13563753 | 1.000 |
| `ema_24` | 108.93579204 | 108.93580467 | 1.000 |
| `rsi_24` | 10.36393516 | 10.36390490 | 1.000 |
| `obv` | 21439.92313326 | 21439.92355364 | 1.000 |
| `obv_slope_24` | 10979.82439122 | 10979.81604906 | 1.000 |
| `volume_zscore_20` | 1.05574207 | 1.05573848 | 1.000 |
| `atr_24` | 3.56875933 | 3.56875933 | 1.000 |
| `vwap_dev` | 0.00236646 | 0.00236647 | 1.000 |
| `volume_per_trade` | 0.29296251 | 0.29295780 | 1.000 |
| `trade_count_zscore_20` | 1.11061833 | 1.11060949 | 1.000 |

Largest deviation across all eleven: ratio **1.000** (|log ratio| < 1e-5).
**The two arms fitted the same window**, so the §2 comparison is fair and
the 0.42-pt return difference is attributable to the funding channel, not
to a shifted price frame. (Both exports also landed on byte-equal
`open/high/low/close/vwap` — §3.)

The one quantity that genuinely differs is the one under test:
`funding_rate` STD **×6.941**.

---

## 7. Non-vacuity of the new regression tests

### 7.1 NON-VACUOUS — the producer-hint pin

`aba7b9b` changed the funding channel's producer hint from
`"just funding-backfill # then just funding-pull"` to
`"just funding-backfill && just funding-pull"` and pinned it, including a
dedicated `assert "#" not in err.producer`.

Reverted **only that one string** in a `/tmp` copy (source only, tests
kept at HEAD) and re-ran:

```
>           assert err.producer == (
                "just funding-backfill && just funding-pull"
            ), leg
E           AssertionError: live-fetch
E             - just funding-backfill && just funding-pull
E             + just funding-backfill # then just funding-pull
tests/test_rl_signal_config_wiring.py:1040: AssertionError
1 failed, 22 deselected
```

Fails with the original symptom. **This test earns its place** — and it
matters because the `#` failure is invisible to the width guard (the
docstring's own point: `spread` collapses to constant-zero at unchanged
width).

### 7.2 VACUOUS *for this diff* — the two new characterisation tests

`tests/test_rl_signal_config_wiring.py` also gained
`test_the_history_recipe_must_run_both_legs_or_spread_goes_dead` and
`test_the_freshness_bound_only_matters_while_the_file_is_shallow`.
With `data.py` and `features.py` reverted to `069a826` and the tests kept
at HEAD:

```
2 passed, 21 deselected in 0.56s
```

**Both pass without this pass's source change**, so as guards for *this*
diff they prove nothing. That is not a defect in the tests — it is a
property of the diff, verified with the repo's own tool:

```
kraken_trading_bot/rl/data.py      executable_ast 069a826==HEAD -> True
kraken_trading_bot/rl/features.py   executable_ast 069a826==HEAD -> True
```

**This pass made zero executable change to the pipeline.** `069a826..HEAD`
touches comments, docstrings, the justfile, config comments, the systemd
timer, and one producer-hint string. There was no behaviour for a test to
guard. The two tests remain legitimate as guards against a *future* seam
regression (they would catch a merge that stopped carrying `bid`/`ask`),
but they are documentation of a measured property, not coverage for this
change. Reported as **F-14**.

### 7.3 A caveat about my own §1.2 receipt

`verify --frame` printed `features=49`. **49 is not a shipped width** — it
is an artifact of the frame I handed it (a raw OHLCV parquet, from which
`width_check.py` derives nothing). §1.4 and §5.3 carry the real ladder,
52 → 60. **Do not paste §1.2 onward as a width record**; it reads as a
60 → 49 regression and is one. Reported as **F-15**.

---

## 8. New findings for the lead to record

I am read-only on source, so these are reported, not recorded. Both need
appending to `DECISION.md` §7.8 or `just audit-findings` will refuse.

**F-14 — the two new characterisation tests are vacuous for `069a826..HEAD`.**
`test_the_history_recipe_must_run_both_legs_or_spread_goes_dead` and
`test_the_freshness_bound_only_matters_while_the_file_is_shallow` both
pass with `data.py`/`features.py` reverted to the pre-registration commit
(§7.2). *Requested disposition:* **Accepted, not a defect.** The pass made
no executable change (`executable_ast` identical on both files, verified),
so no test could be non-vacuous for it. Recorded so a later reader does
not mistake them for regression coverage of this diff. The genuinely
non-vacuous test in this pass is the producer-hint pin (§7.1).

**F-15 — `width_check.py` under-reports width by 3 on a frame lacking the derived trio, and `--frame` will happily print a non-shipped width as a gate figure.**
`width_check.fingerprint` calls `FeaturePipeline.compute(df)` but never
`add_derived_ohlcv_features`, so a raw OHLCV parquet drops
`vwap_dev` / `volume_per_trade` / `trade_count_zscore_20` and reports 49
where every shipped state is 52 or 60. My own §1.2 receipt is the
demonstration. This is the same class as F-8 ("which axis did the fixture
include?") and it is now a **tool** trap rather than a doc trap.
*Requested disposition:* **Accepted — document in `width_check.py`'s
module docstring** that `--frame` must be a post-`add_derived_ohlcv_features`
frame, and that a `--frame` receipt is a fingerprint of the frame supplied,
not of the shipped default. No behaviour change (a silent auto-call would
change every historical fingerprint).

---

## 9. Verdict

**PASS.**

- Pre-registered threshold unmoved (`tools/model_matrix.py` byte-identical
  to `069a826`); AST proof self-test clean; 6/6 guarded files
  executably unchanged since `8cada4e`.
- **457 passed**.
- The claim is met on the consumed artifact: `funding_rate` **2 distinct
  on 13/721 bars → 721 distinct on 721/721 bars**, at **unchanged width
  60**, with fitted STD **×6.941**.
- Magnitude clause satisfied with numbers, not with "no error":
  **697/721 bars replayed** and **546 trades** (baseline 697 and 558).
- Equivalence gate met on the return figure as a **plumbing check only**:
  **|Δ return| 0.42 pts**, same sign, inside the ~5-pt band. One run per
  arm, in-sample — **not** an effect measurement (§2, † rows).
- **Both OPEN findings re-derived and resolved** (F-9 → frame mismatch,
  exact arithmetic; F-11 → one arm, two frames, with the right-edge
  mechanism), plus F-8 and F-10 confirmed and pinned to measured values.
- Coverage and persistence proven from the **production seeding path**.

### 9.1 What was recovered, and what was given up — stated together

**This pass recovers 1 of 6 columns, and it recovers the SAME series the live
snapshot already read.** The evidence is an exact-value match, not a
correlation: the checked-in one-line file's `funding_rate` of
`0.02527185133308243` matches **exactly one** of the 8,791 records Kraken's
`/historical-funding-rates` returns, at the checked-in hour. So this is not a
proxy series or a cross-venue substitute — it is the identical series,
recovered 366 days deep.

| column | before | after |
|---|---|---|
| `funding_rate` | 13/721 bars, **2** distinct values, 98.2 % literal `0.0` | **721/721 bars, 721 distinct** — std ×6.94 |
| `basis` | 13/721 real | **13/721 — unchanged** |
| `open_interest` | 13/721 real | **13/721 — unchanged** |
| `funding_rate_prediction` | 13/721 real | **13/721 — unchanged** |
| `vol24h` | 13/721 real | **13/721 — unchanged** |
| `spread` | 13/721 real | **13/721 — unchanged** |

The gain and the loss belong in one table because they are one change. The
five columns that keep their zero-fill are the honest cost of this pass: they
now read **constant zero** rather than "13 real readings then zero", so the
degenerate-STD count rose **3 → 10** and their absence is no longer
distinguishable from a genuine reading of zero. Nothing about the backfill
*removed* those readings — Kraken publishes no history for them at any cost —
but the failure mode moved from "sparse" to "constant", which is a different
kind of silent.

**Do not let the first row carry this section on its own.** `funding_rate`
going from 2 distinct values to 721 is the headline; the five rows beneath it
are what the headline cost.

Stated plainly, and not as a footnote: **this pass recovers 1 of 6
columns**, 5 columns lose the 13 readings the single live snapshot gave
them, `signal_observed` no longer distinguishes absence per column (F-2),
and both arms are **IN-SAMPLE** so no predictive claim is supported.

---

## §8 Finding index — F-1 … F-16

`just audit-findings` binds every `F<n>` named **here** to a disposition in
`DECISION.md` §7.7/§7.8 (`tools/audit_checks.py:348-381`). An earlier
revision of this file named none, so the check reported "no F<n> identifiers"
and **passed vacuously** — the same green-for-the-wrong-reason class §1's
`SELF-TEST` clause exists to catch. This section restores the binding.

| id | one-line | disposition |
|---|---|---|
| **F-1** | `features.py` docstring said the shipped default "composes 52" | **Closed** — 52 is the all-null width; shipped default composes 60 (`aba7b9b`) |
| **F-2** | `signal_observed` is a per-channel OR; 4 of 6 columns stay zero-fill | **Accepted and disclosed** — `configs/default.yaml` §1/§2, restated in this file |
| **F-3** | Forward-only cold start at a rolling ~366-day cap | **Accepted, bounded, asserted** |
| **F-4** | AUDIT's "12 of 721" was wrong; **13** measured | **Correction recorded** |
| **F-5** | `null` vs `0.0` for unrecoverable fields | **Override, measured equivalent** — record honesty only |
| **F-6** | Tighten `signal_max_age_hours` 12 → ~1 | **Override: fix the comment, not the value** — §10 confirms at max **12.0** |
| **F-7** | §6.1's hour-dedup direction was **inverted** (file order, not "live wins") | **Corrected and acted on** — `--append` skips held hours |
| **F-8** | §7.3's width constants (57/49) are **3 low** | **Superseded** — measure 52/60; assert no literals |
| **F-9** | Non-finite cells: 684 vs 0 | **RESOLVED — frame mismatch.** `697 − 13 = 684`; `observed` is the frame that matters. Builder's "identical both arms" **wrong** (684 vs 697) |
| **F-10** | `signal_age_hours` ≤ 2.0 unachievable | **Restated per shape** — 12.0 on the shipped shape; confirms F-6 |
| **F-11** | `spread` nonzero: 13 vs 24 | **RESOLVED — one arm, two frames.** 13 = `computed`, 24 = `observed`; 13 + 11 ffill = 24. Integrator's "byte-identical both arms" **wrong** (0/721 backfilled) |
| **F-12** | Record has 14 keys, §6 says 13 | **Restated as a superset** — `relative_funding_rate` costs 0 observation columns |
| **F-13** | F-1's number was wrong as well as its text | **Closed** (`aba7b9b`) |
| **F-14** | The two characterisation tests are **vacuous** for this diff | **Accepted, not a defect** — zero executable change, so no test *could* be non-vacuous |
| **F-15** | `width_check.py` skips `add_derived_ohlcv_features`, so a raw parquet reports 49 | **Accepted — docstring-only**; pre-existing tool trap, do-not-paste receipt |
| **F-16** | `audit_checks.find_in_repo` **crashed** (`ValueError`) on a legitimate absolute cross-repo citation, making `just audit-evidence` unrunnable | **Fixed** in this pass — degrade to the absolute path; a crash asserts nothing, and `audit_checks.py` is not in `PY_FILES`, so the `executable-ast` guard and the byte-identical `model_matrix.py` are untouched |
