# VALIDATION — data-pipeline audit pass 2026-10-02 (`IMPROVE-EXISTING`, gap **G1**)

**Reviewer:** `reviewer`, team `data-audit-1002`. Task `task_muq5u4z1_0008_pwg3of`.
**Base:** `master` @ `3f9708e` (clean tree — this pass wrote nothing but this file and `PLAN.md`).
**Supersedes** the 2026-10-01 revision of this file, which gated a *different* pass (the
sparse-exogenous `first_tradable_index` fix, base `b45a7a4`). That file's numbers are kept
below where they corroborate this one; nothing from it is asserted without re-measuring.

Everything here was **executed live** against the real CLI, the real keyless funding
producer, and real network OHLCV. Nothing is quoted from the source without running it.

---

## 0. GATE VERDICT: **NEEDS_FIX**

Three of the four conjunctive conditions of DECISION §8 pass outright, one of them
fails, and it fails **on exactly the case the gate was written to catch**.

| | Condition | Verdict |
|---|---|---|
| **(a)** | The seam is *consumed*, by name — `signal_observed` present and `.any()` true | **PASS** (§3) |
| **(b)** | The 7 new columns present **by name** in the fitted artifact | **PASS** (§5) — and proven non-vacuous (§6) |
| **(c)** | Magnitude: `n_bars > 1`, `num_trades > 0`, span consistent | **PASS** (§7) |
| **(d)** | Negative direction: null key trains cleanly *and*; unresolvable key raises by name **and** `model_matrix` classifies it `signal_file_not_found` | **FAIL** (§8) |

**(d) fails.** The seam's own refusal is correct and complete — it names the config key, the
raw `~`-spelled value, the expanded path and the producer command, verified through the real
CLI. But `tools/model_matrix.py` does **not** classify that cell as `signal_file_not_found`;
it degrades to a bare `process_failed`, live, on the real harness, on a fresh results file.
That is the specific regression `3f9708e` was written to prevent. Root cause, mechanism and a
scoped fix are in §8.3. **It is a one-line capture defect, not a design problem** — the
classifier itself is correct and its tests pass.

---

## 1. Suite validation

| Check | Command | Result |
|---|---|---|
| pytest | `nix develop --command bash -c "python -m pytest -q"` (from MAIN) | **327 passed**, 0 failed, 20 warnings, **23.57 s** |
| flake | `nix flake check` | **all checks passed!** |

`327` is the exact count claimed for `3f9708e` (314 before the pass, +13). Reproduced.

> Note for the record: `nix flake check` was unclaimed and is now exercised. It checks
> `packages`, `nixosModules.default` and `devShells` — all evaluate. It does **not** run the
> test suite, so the two are independent confirmations, not one twice.

---

## 2. A real funding record, and the seam consuming it

```
$ just funding-pull
Wrote 1 funding records to /home/seanc/Projects/kraken-trading-bot/signals/eth_usd_funding.jsonl
```

`signals/` is gitignored (`.gitignore:65`), so nothing polluted the tree. The record, in full:

```json
{"ask": 2706.2, "basis": 8.826536004681354e-05, "bid": 2706.1,
 "funding_rate": 0.02527185133308243, "funding_rate_prediction": 0.011412836464249098,
 "index_price": 2706.12, "mark_price": 2706.35885665613, "open_interest": 25985.898,
 "spot_pair": "ETH/USD", "symbol": "PF_ETHUSD", "ticker": "ETH/USD",
 "timestamp": "2026-10-02T00:00:00+00:00", "vol24h": 29621.035}
```

**Sibling `935dfc0` confirmed live, not merely committed:** `ticker` is `"ETH/USD"` — the
**spot** pair, which is what makes `signal_require_ticker: true` able to filter at all (§9).

The `--ticker` note in the brief holds: the seam needs the **base** asset. `ETH_USD` works;
`USD_SOL` does not (`Unknown Kraken pair: 'USD/SOL'`).

---

## 3. (a) THE SEAM IS CONSUMED, BY NAME

Driven directly against `merge_extra_features` with the `~`-spelled value exactly as
`configs/default.yaml` spells it, on a 240-bar hourly frame:

```
signal_observed present  : True
signal_observed .any()   : True        <-- (a)
signal_observed .sum()   : 13.0
signal_age_hours present : True
signal_age_hours min/max : -1.0 / 12.0
frame has basis / funding_rate / funding_rate_prediction / vol24h / bid / ask : all True
```

`.sum() == 13.0` is the arithmetic of one hourly record carried forward under
`signal_max_age_hours: 12` on 60-minute bars: 1 bar observed + 12 bars of bounded carry.
`-1.0` is the documented no-reading sentinel on the remaining bars. **This is the exact
shape a genuine sparse forward-only log should produce** — and it is unreachable on the
pre-fix tree, where the merge never ran.

**The negative control, which matters more than the positive.** With the key null:

```
returned identical frame : True        (no raise, no log line — "off" is silent)
signal_observed present : False       <-- this IS the pre-fix bug, not the fix
```

So a gate written as `assert frame["signal_observed"] == 0.0` would have asserted the
**defect**. DECISION §8(a) is right about this and it is worth restating for whoever writes
the permanent gate.

---

## 4. The end-to-end run

Scratch config: `configs/default.yaml` copied to `/tmp/opencode/data-audit-val/gate.yaml`
with only `model_name` changed to `gate_probe_01`. `feature_windows: [1, 4, 24]` and
`feature_groups` untouched from the shipped default. Models under `/tmp` only.

```
$ nix develop --command bash -c 'python -m kraken_trading_bot.cli train \
    --ticker ETH_USD --model gate_probe_01 \
    --config /tmp/opencode/data-audit-val/gate.yaml \
    --pages 4 --timesteps 512 --action-space discrete \
    --models-root /tmp/opencode/data-audit-val/models --json'

Fetching 4 pages of ETH/USD 60-minute OHLC
Training PPO on 721 bars of ETH_USD (60 obs features, 512 timesteps)
Trained and registered ETH_USD/gate_probe_01
{"ticker_id": "ETH_USD", "model_name": "gate_probe_01", "n_features": 60,
 "n_bars": 721, "timesteps": 512, "seed": 42, ...}     rc=0
```

Backtest, replaying **that same model** against **its own recorded config**:

```
$ nix develop --command bash -c 'python -m kraken_trading_bot.cli backtest \
    --ticker ETH_USD --model gate_probe_01 --pages 4 \
    --models-root /tmp/opencode/data-audit-val/models --json'      rc=0
```

### One operational wrinkle worth recording

The first backtest attempt **failed**, and correctly:

```
Action-space mismatch: the model was trained as 'discrete' but this backtest would
replay it as 'continuous'.
```

`train --action-space discrete` overrides the run config for training, and the model's
`config.yaml` records it — but passing the *scratch* `--config` to `backtest` supplies
`action_space: continuous`, which is the one of the six keys a backtest config *does*
honour. The fix is to replay against the model's own config (what I did) or to keep the
scratch config in step. Worth a line in the skill: **a `--config` handed to `backtest`
must agree with the model's recorded `action_space`**, and `train`'s CLI override is not
reflected back into the YAML you pass to `backtest`.

---

## 5. (b) THE 7 NAMES, BY NAME, RE-DERIVED FROM THE ARTIFACT

Read straight out of `models/{TICKER_ID}/{model_name}/normalization.npz`:

```
keys: ['ticker_id', 'feature_names', 'means', 'stds']

WIDTH (len(feature_names)) = 60

  basis                        present=True
  funding_rate                 present=True
  funding_rate_prediction      present=True
  signal_age_hours             present=True
  signal_observed              present=True
  vol24h                       present=True
  spread                       present=True
MISSING: []
```

### Observed width: **60** — and its provenance

Four numbers were in circulation and none is authoritative. Resolved:

| Source | Claimed | Status |
|---|---|---|
| RUN LOG (prior pass) | 49 / 52 / **60** | **60 corroborated** — and the 2026-10-01 `VALIDATION.md` in this repo already recorded "width 60 both sides" for a different fix on the same pipeline. Two independent runs, same number. |
| RESEARCH-1 | measured 52 / 52 / **59** | **Wrong by 1.** Its denominators also do not match the code: `_SIGNAL_COLUMNS` (`features.py:46-77`) has **19** entries, which I counted directly; RESEARCH-1 reports "3 / 18" and "10 / 18". |
| RESEARCH-2 | computed 51 / 54 / **67** | **Wrong by 7.** It could not run a probe at all (its venv cannot import numpy), so this was arithmetic on assumptions. |
| Builder (integrator) | re-derived **60** "with a fed record that includes `open_interest`" | **Confirmed, and the `open_interest` caveat is exactly right** — see §6. |

**60 is the number, re-derived from the artifact rather than hardcoded.** The delta from the
no-channel baseline is **exactly 8**, and the eighth name is `open_interest` — which is why
the builder's "60 with a fed record that includes `open_interest`" and RESEARCH-1's "59"
both felt right: RESEARCH-1 omitted it.

Full ordered `feature_names` (60), signals tail intact:

```
  0 return_1                      20 atr_1                    40 bb_pctb_24
  ... (price / technical / volume groups, indices 0-48) ...
 49 spread                        53 signal_observed          57 volume_per_trade
 50 funding_rate                  54 signal_age_hours         58 funding_rate_prediction
 51 basis                         55 vwap_dev                 59 vol24h
 52 open_interest                 56 trade_count_zscore_20
```

`bid`/`ask` are correctly **absent** from the observation — they are
`_SIGNAL_BUILDER_INPUT_COLUMNS`, carried to the frame and consumed by
`_add_microstructure_features` to *produce* `spread` (index 49). Observed spread from this
record: `(2706.2 − 2706.1) / 2706.1 = 3.695e-05`. **`spread` is the sharpest of the seven:**
it produces nothing whatsoever before the fix, because its only inputs arrive with the
funding record.

---

## 6. (b) IS **NON-VACUOUS** — proven, not asserted

Trained a second model from the identical scratch config with the funding key set to
`null` (all three channels off — the shipped-default shape):

```
width WITH funding record consumed : 60
width with all channels null       : 52

DELTA (present only with the record): 8
   + spread                        + signal_age_hours
   + funding_rate                  + signal_observed
   + basis                          + funding_rate_prediction
   + open_interest                  + vol24h

7 gate names all in the delta     : True
7 gate names all ABSENT when null : True
```

**A gate that passes with and without the fix measures nothing. This one does not:** null
the funding key and all seven names disappear, `spread` first among them. The width guard
`check_feature_width` cannot see this — it compares widths, and 52 ≠ 60 so it would in fact
fire here, but only because the delta is 8 names wide; had the record carried a single
overlapping column the widths would have matched and the guard would have stayed silent
while `signal_observed` vanished. The *name-based* assertion is what makes it robust.

---

## 7. (c) MAGNITUDE — bars replayed and trades taken

`backtest --json`, same model as §4, same run:

| field | value |
|---|---|
| `n_bars` | **697** |
| `num_trades` | **350** |
| `n_steps` | 697 |
| `action_space` | `discrete` |
| `total_return` | +0.012563 |
| `buy_hold_return` | +0.137193 |
| `fee_rate` / `slippage` | 0.0 / 0.0 |
| all 18 `REQUIRED_BACKTEST_FIELDS` present | **True** |

`excess_return` (−0.12463) is recorded for completeness and **deliberately not asserted** —
per G4 it is not resolvable at this design (detection floor 2.82 pp against a 0.25–0.8 pp
effect), so a gate keyed on it is a coin flip. Note it also *underperforms* buy-and-hold
here, which is exactly what an underpowered single-cell comparison looks like and exactly
why it must not gate.

### Span consistency — the 24-bar gap is fully accounted for

```
  --pages  2 at 60m ->  721 bars  (2026-09-02 00:00 .. 2026-10-02 00:00)
  --pages  4 at 60m ->  721 bars
  --pages  8 at 60m ->  721 bars

  train n_bars       = 721
  backtest n_bars    = 697
  721 - max(feature_windows)=24  =  697     <-- exact
  697 / 721                     =  0.9667
```

`697 = 721 − 24` **exactly**, where 24 is `max(feature_windows)`. The backtest replays the
entire fetched frame minus the warm-up trim. **96.7 % of the frame — not silently short**,
and nothing like the prior pass's 1-bar collapse.

Separately confirmed (pre-existing, documented in `configs/matrix.example.yaml`, **not** a
regression of this pass): **`--pages` does not buy depth.** 2, 4 and 8 pages all return 721
bars at 60 min — Kraken's REST ceiling is ~720 candles per pair/interval regardless of page
count. Any gate that asserts depth from a page count is asserting nothing.

---

## 8. (d) THE NEGATIVE DIRECTION — one half passes, one half **fails**

### 8.1 Null key → trains cleanly and silently. **PASS**

`extra_features_file: null`, `funding_features_file: null`, `social_features_file: null`:

```
$ ... train --config gate_nochan.yaml ... --json
Fetching 4 pages of ETH/USD 60-minute OHLC
Training PPO on 721 bars of ETH_USD (52 obs features, 512 timesteps)
{"...","n_features": 52, "n_bars": 721, ...}          rc=0

grep -icE "signal|funding_features_file is set|no file exists" train_nochan.log  ->  0
```

`rc=0`, **52** features, and **zero** occurrences of any signal/funding word in the entire
log. A fresh clone with no sibling repos cloned still runs on price alone. This is the
requirement that the loud failure must *not* break, and it is intact.

### 8.2 Unresolvable key → raises by name. **PASS**

Seam level, and then through the real CLI:

```
$ ... train --config gate_missing.yaml ...
rc=1

Error training ETH_USD/missing_probe: funding_features_file is set to
'~/Projects/kraken-trading-bot/signals/not_there_ever.jsonl' but no file exists at
/home/seanc/Projects/kraken-trading-bot/signals/not_there_ever.jsonl (expanded from
'~/Projects/kraken-trading-bot/signals/not_there_ever.jsonl'). Produce it with: just
funding-pull. Or set funding_features_file to null to run without this channel: a null key
means off and is silent, a set key is a declared intent to use it.
```

All four required elements present:

| element | occurrences |
|---|---|
| config key (`funding_features_file is set to`) | 2× |
| raw `~`-spelled value | 2× |
| expanded absolute path | 2× |
| producer command (`just funding-pull`) | 2× |

A fresh-clone user is handed a copy-pasteable fix rather than a silent narrower model. This
is the single best thing in the pass.

### 8.3 `model_matrix` classifies it `signal_file_not_found`. **FAIL — live**

Run the real harness on a one-cell spec whose base config is `gate_missing.yaml`:

```
$ nix develop --command bash -c \
    'python tools/model_matrix.py run /tmp/.../missing_signal.yaml --force'

[1/1] c47d4fdbd89b  action_space=discrete, friction={fee_rate=0,slippage=0},
      pages=4, seed=42, ticker=ETH_USD, timesteps=512
    INVALID: process_failed                    <-- NOT signal_file_not_found
    ERROR: train rc=1, backtest rc=1; ... [ERROR] Backtesting ETH_USD/mtx_c47d4fdbd89b
      failed: No trained model at .../model.zip; train it first ...

ran 1 cell(s): 0 invalid, 1 errored.
```

Dumping the record it wrote:

```
status            : error
returncodes       : {'backtest': 1, 'train': 1}
=== stderr_tail actually stored ===
  | ... GET /0/public/OHLC
  | ... GET /0/public/OHLC
  | [ERROR] Backtesting ETH_USD/mtx_c47d4fdbd89b failed: No trained model at ...
  | Error backtesting ETH_USD/mtx_c47d4fdbd89b: No trained model at ...

  "SignalFileNotFoundError"          in classified text -> False
  "funding_features_file is set to"  in classified text -> False
  "just funding-pull"                in classified text -> False
  "No trained model"                 in classified text -> True
```

**The control proves the harness itself is healthy.** The identical cell with the real
funding record present:

```
[1/1] c47d4fdbd89b ...
    return=+1.48% excess=-12.15% sharpe=0.324 trades=353 bars=697 (8s)
ran 1 cell(s): 0 invalid, 0 errored.
```

So the whole-grid "why is nothing working" shape is *real and reproducible* — drop the one
file and every cell becomes this same opaque `process_failed` — and the reason code that was
supposed to explain it does not fire.

#### Root cause — `tools/model_matrix.py:1726`, one line

```python
stderr_tail = (err_tr + err_bt).strip().splitlines()[-4:]
```

It concatenates **train** stderr then **backtest** stderr and keeps only the **last 4
lines of the whole thing**. But when `train` fails, `backtest` also runs and also fails, and
*its* output — the downstream consequence, "No trained model" — is what lands in the tail.
The train leg's refusal, which is the message that names the fix, is truncated away before
`classify_process_failure` ever sees it.

Measured deterministically by reproducing the capture per leg:

```
rc_tr, rc_bt                  : 1 1
err_tr lines                  : 10
refusal IS in err_tr?         : True      <-- the TRAIN leg does say it
its line index in err_tr      : 8
err_bt lines                  : 9

line 1726 keeps LAST 4 of 19 concatenated lines
refusal survives that slice?  : False     <-- THE BUG
```

And the classifier itself is fine — it fires the moment the text reaches it:

```
real chain (train fails -> backtest also fails, its error is last):
   reasons = ['process_failed']                                        <-- live
hypothetical (backtest produced NO stderr, so the refusal is last):
   reasons = ['process_failed', 'signal_file_not_found']               <-- correct
```

**Why the test did not catch it.** `3f9708e` pins the classifier with
`test_a_refused_signal_file_is_named_and_not_mistaken_for_another_cause`, which feeds
`classify_process_failure(_real_errored(REAL_SIGNAL_FILE_REFUSED))` — a synthetic list of
stderr lines placed **directly** into `stderr_tail`. The test never runs `cmd_run`, so it
never builds the tail that broke. This is precisely the trap the module's own docstring
warns about ("Matching is on the message the real CLI actually prints … found against the
real binary"): the classifier was matched against a real message, but not against a real
**capture**.

#### Scoped fix for the lead — one line, with the test to match

Take the tail **per leg** instead of over the concatenation, so a refusal from either leg
survives:

```python
# tools/model_matrix.py:1726
stderr_tail = (err_tr.strip().splitlines()[-2:] + err_bt.strip().splitlines()[-2:])
```

Better still, keep the refusing leg whole — the refusal is one long line and is what a
reader needs:

```python
stderr_tail = (err_tr.strip().splitlines()[-4:] + err_bt.strip().splitlines()[-2:])
```

Either restores `signal_file_not_found` on the live cell (verified above: with the refusal
in the tail the classifier returns `['process_failed', 'signal_file_not_found']`).
The regression test should **not** be another synthetic `stderr_tail` — it should build a
record the way `cmd_run` builds it from a real train-fails/backtest-fails pair, or at
minimum assert that `err_tr`'s content survives into `record["stderr_tail"]`, so the
capture is pinned and not only the classifier.

I did **not** make this fix — out of scope for a reviewer.

---

## 9. Both read legs, and the user-visible behaviour change

### 9.1 The refusal carries key + producer from **both** legs

`3f9708e` closed a real gap here (`config_key` was pinned only on direct
`merge_extra_features` calls, never on the two config-driven read legs). Verified live:

```
  fetch_ohlc_dataframe     forwards config_key=config_key : True
  read_ohlc_dataframe      forwards config_key=config_key : True
  seam-level refusal names key      : True
  seam-level refusal names producer : True
```

The wiring is genuinely single-sourced — `_SIGNAL_CHANNELS` (`data.py:118`) pairs every key
with its producer, and both legs iterate it:

```
  extra_features_file   -> python ~/Projects/ticker-news-signals/cli.py pull --ticker <PAIR> --output <path>
  funding_features_file -> just funding-pull
  social_features_file  -> python ~/Projects/kraken-social-signals/cli.py pull --ticker <PAIR> --output <path>
```

So the two legs cannot drift about which key feeds which merge, or about how a refusal reads.

### 9.2 The behaviour change — it **RAISES** (intended, but user-visible)

The builder and integrator both flagged this. With `signal_require_ticker: true` now
actually filtering — because the funding record carries `ticker` = `ETH/USD` — pointing a
second pair at the ETH file now raises rather than silently merging:

```
$ merge_extra_features(<ETH file>, ticker="SOL/USD", require_ticker=True)
SignalTickerMismatchError: Signal file .../eth_usd_funding.jsonl holds no records for
SOL/USD (contains: ETHUSD); point the signal file config key at a SOL/USD file or set
signal_require_ticker: false to merge it unfiltered.
```

**It raises.** This is the intended direction and it is strictly better than the old silent
BTC-onto-ETH merge — but it is a real user-visible break for anyone training a second pair
against `configs/default.yaml` as shipped, and the config comment already tells them to
point each pair at its own file. Two notes worth carrying forward:

- The `found` list renders in **canonical** form (`ETHUSD`) while the request renders as
  `SOL/USD`. Correct, mildly confusing to read.
- Because `configs/default.yaml` ships a **single, ETH-named** `funding_features_file`, the
  shipped config is only valid for ETH. Any second ticker must override the key. A config
  that could not be valid for more than one ticker by default would be a better default.

---

## 10. Summary of every number observed

| quantity | value | source |
|---|---|---|
| pytest | **327 passed** / 23.57 s | `nix develop`, MAIN |
| `nix flake check` | **all checks passed** | — |
| funding records pulled | 1 (real keyless pull) | `just funding-pull` |
| `signal_observed` present / `.any()` / `.sum()` | **True / True / 13.0** | `merge_extra_features` |
| `signal_age_hours` present / range | **True / −1.0 … 12.0** | `merge_extra_features` |
| **observed feature width (with record)** | **60** | `normalization.npz` `feature_names` |
| feature width (channels null) | 52 | `normalization.npz` `feature_names` |
| width delta | **8** = the 7 gate names + `open_interest` | diff of the two artifacts |
| 7 gate names present | **7 / 7** | `normalization.npz` |
| 7 gate names present when key nulled | **0 / 7** | second artifact |
| `n_bars` (train / backtest) | 721 / **697** | `--json` |
| `num_trades` | **350** | `backtest --json` |
| span replayed | 697 / 721 = **0.9667**; = 721 − 24 warm-up exactly | §7 |
| `--pages` depth at 60 min | 721 bars at pages 2, 4 **and** 8 | live fetch |
| null-key train | rc=0, 52 features, **0** signal mentions in log | §8.1 |
| unresolvable-key refusal | names key / raw / expanded / producer, **2× each** | §8.2 |
| `model_matrix` on that cell | **`process_failed`** — expected `signal_file_not_found` | §8.3 |
| `model_matrix` control cell (record present) | **0 invalid, 0 errored**, bars=697 trades=353 | §8.3 |
| ETH file + `SOL/USD` ticker | **raises** `SignalTickerMismatchError` | §9.2 |

---

## 11. What is *not* established by this validation

- **Whether the signal is worth trading.** Nothing here speaks to that. `excess_return` was
  not asserted, by design. This gate establishes that the observation *contains* the signal,
  not that it *predicts*.
- **Multi-seed reproducibility.** One seed (42). `configs/matrix.example.yaml` correctly
  records PPO swinging 576 → 374 trades on an identical config from the seed alone, so
  `num_trades: 350` is a count, not a stable quantity. The gate needs `> 0`; it does not
  need a specific number.
- **Depth beyond ~30 days.** `--pages` cannot buy it (§7). Deep history needs the local
  market-data store seeded, which is G3 territory and out of this pass's scope.
- **The store-backed read leg under a real store.** `market_data_store: null` in the shipped
  config, and §9.1 verifies the leg's *wiring* by inspection of its call site plus the
  refusal text both legs surface — not by exercising a populated store. `data.py:1054`
  (`_resolve_store`, the second instance of the same tilde bug) is fixed and unexercised
  end-to-end here. **Worth a dedicated check when a real store exists.**

---

## 12. Bottom line

**(a), (b) and (c) pass, and (b) is proven non-vacuous. (d) fails on its second half.**

The seam fix itself — `8569c08` — is correct, complete and well defended. It converts a
silent narrowing of the observation into a named, actionable refusal, it leaves the
off-state silent, and it does so from a single-sourced channel table that both read legs
share. The 52 → 60 width jump with all seven names present, `spread` among them, is exactly
what DECISION §8 predicted.

`3f9708e` is the weak point, and it is weak in a specific, small, fixable way: the reason
code is right, but the harness never hands it the text. One line
(`tools/model_matrix.py:1726`) decides whether the one diagnostic that explains a
whole-grid failure is delivered or swallowed. The commit's own reasoning — "matching is on
the message the real CLI prints" — is right about matching and wrong about capture, and the
synthetic-record test could not see it.