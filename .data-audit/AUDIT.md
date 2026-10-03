# AUDIT.md — data-pipeline audit, pass dated 2026-10-03

**Phase 1 (auditor). Read-only. HEAD = `c6295be`, tree clean at entry.**
No source file was modified. The previous pass's artifact is preserved in git
history at `e027621`; this file replaces it on disk only and is **uncommitted**
— the lead commits it.

Package is `kraken_trading_bot/` (top-level `rl/` does not exist; the RL code
is `kraken_trading_bot/rl/`).

---

## 0. Method note — what "verified" means here

Three tiers, used consistently below:

- **[M] measured** — I ran a command and it is quoted verbatim, with the command.
- **[C] cited** — read directly off the source at the line quoted.
- **[I] inferred** — reasoned from a fixture, a docstring, or a comment. Never
  presented as measured.

Measurement environment: every figure below was produced inside
`nix develop --command python …` (the repo's dev shell), because `.venv` has an
editable install pointing at MAIN and would silently measure the wrong tree.
Scripts lived in `/tmp/opencode/`; nothing was written into the repo.

**Bar counts, widths and degenerate-column counts are all dated 2026-10-03**,
because this repo has shipped an undated count as an error twice (`PLAN.md` §6
item 6; F-4/F-9/F-11).

Three scripts, all reproducible:

```
nix develop --command python /tmp/opencode/measure_widths2.py   # width table
nix develop --command python /tmp/opencode/m4.py                # degenerate cols
nix develop --command python /tmp/opencode/m5.py                # shipped default vs the REAL file
```

**I reproduced F-15 live before writing anything.** My first width script
called `FeaturePipeline.compute()` on the raw frame without
`data.add_derived_ohlcv_features`, and reported **49** for the all-null
configuration. Adding the seam step — the one `data.py:1214` and `data.py:1407`
both apply — gives **52**. Same code, same frame, 3 columns apart. F-15 is real,
reproducible, and still unguarded.

---

## 1. End-to-end pipeline map

Two **disjoint** programs share the `kraken_api` wrapper. Only the first one
feeds the RL policy.

### 1.1 The RL path — `train` / `backtest` / `export-data` / `paper-trade`

```
config YAML ──> load_train_config / build_train_config         train.py:69, :115
                    │
                    ├─ cfg["ticker"], cfg["ohlcv_interval_minutes"]      train.py:208-209
                    │
        ┌───────────┴────────────────────────────────────────────┐
        │  read_ohlc_dataframe()          data.py:1229           │  ← the ONE read seam
        │                                                            │
        │  store is None (shipped default, configs/default.yaml:212)│
        │    └─> fetch_ohlc_dataframe()             data.py:1136   │
        │          └─> _page_candles()              data.py:1092   │
        │                └─> manager.ohlc(pair, interval=, since=cursor)   [data.py:1126]
        │                      └─> kraken_api  client.depth()  → Kraken /0/public/OHLC
        │          └─> candles_to_dataframe()      data.py:1053   │  time/open/high/low/close/vwap/volume/count
        │          └─> add_derived_ohlcv_features() data.py:989   │  +vwap_dev, trade_count_zscore_20, volume_per_trade
        │                                                            │
        │  store configured (market_data_store set)                  │
        │    └─> _resolve_store()                  data.py:1455    │
        │    └─> _page_candles() → store.upsert()  data.py:1385-1387│  fetch → upsert
        │    └─> store.read(pair, interval, since=, until=)  :1402  │  ← since/until NEVER PASSED (§4 G-A)
        │    └─> add_derived_ohlcv_features()      data.py:1407   │
        │                                                            │
        │  ┌─ three exogenous JSONL seams, in merge order ────────┐ │
        │  │ data.py:1215-1225 (fetch leg) / :1408-1418 (store)   │ │
        │  │  for key,file,_ in _signal_channels(...):  data.py:154│ │
        │  │    merge_extra_features()                data.py:612  │ │
        │  │      1 ticker filter      _filter_ticker   data.py:866│ │
        │  │      2 dedupe floored h   data.py:776-781             │ │
        │  │      3 bounded ffill + ages             data.py:810-825
        │  │      4 absence ≠ zero: signal_observed / signal_age_hours
        │  │                            data.py:833-849             │ │
        │  └────────────────────────────────────────────────────────┘ │
        └───────────┬────────────────────────────────────────────┘
                    │
        prepare_episode()   data.py:1513   (slice trailing episode_bars, THEN fit)
                    │
        FeaturePipeline.fit()   features.py:~640  → compute().ffill().fillna(0.0)
        TradingEnvironment      environment.py:193 → _raw_feature_array()  environment.py:470-495
                    │
             observation (n_bars × n_features) → PPO
                    │
        artifacts: models/{TICKER_ID}/{model_name}/
                     model.zip          (RLAgent)
                     normalization.npz (NormalizationStats.save, features.py:~365)
                     config.yaml       (register_model, train.py:289-306)
```

Call sites of the read seam — **all four**, and they do **not** agree:

| leg | call | passes `since`/`until`? | passes `market_data_store_venue`? |
|---|---|---|---|
| train | `train.py:215-227` | **no** | yes (`:226`) |
| backtest | `backtest.py:471-499` | **no** | yes (`:493`) |
| export-data | `export.py:165-177` | **no** | yes (`:176`) |
| paper-trade | `paper_trade.py:298-309` | **no** | **no** ← §4 G-B |

### 1.2 Every `kraken-python` / sibling call site in the tree

**[M]** measured with `grep -rn --include='*.py' '\b<reference>\b' kraken_trading_bot nix`:

| wrapper surface | call sites in this repo | where |
|---|---|---|
| `manager.ohlc` | 2 | `data.py:1126` (both legs, shared) · `engine.py:65` |
| `manager.order_book(pair, count=10)` | 1 | `engine.py:72` — **result is never read by any strategy** |
| `manager.ticker` | 1 | `engine.py:59` |
| `manager.recent_trades` | **0** | wrapped at `kraken_api/manager.py:192`, never called |
| `manager.spread` (bid/ask time series) | **0** | wrapped at `kraken_api/manager.py:200`, never called |
| `ws_token` / `public_ws_url` / `private_ws_url` | **0** | `kraken_api/manager.py:365,369,374` — the whole WS surface is unused |
| `known_pairs` / `assets` / `asset_pairs` / `trade_history` / `ledger` / `server_time` | **0** | `kraken_api/manager.py:131,212,221,343,352,127` |

Siblings (none of them is a flake input except the two named):

| sibling | @ | how it is invoked | cadence | scheduled? |
|---|---|---|---|---|
| `kraken-python` | `81de597` | flake input, `flake.nix:6` | per read | n/a |
| `kraken-market-data` | `055d7f6` | flake input, `flake.nix:7`; `store.upsert`/`store.read` `data.py:1387,1402` | per read | **no timer** |
| `kraken-funding-rates` | `dc49847` | `nix run ~/Projects/kraken-funding-rates#kraken-funding-rates` — `justfile:186,235`, `systemd/kraken-trading-bot-funding.service:39` | hourly | **yes**, one timer |
| `ticker-news-signals` | `23dc965` | named only in a producer string `data.py:157` and `configs/default.yaml:53` | — | **no unit, no recipe, key is `null`** |
| `kraken-social-signals` | `71ca27d` | named only at `data.py:174`, `configs/default.yaml:96` | — | **no unit, no recipe, key is `null`** |
| `kraken-deep-history` | `d00bb98` | `just store-plan` / `store-seed` / `store-verify` — `justfile:297,305,349` | manual | **no timer** |

### 1.3 systemd timers

**[C]** `systemd/` contains exactly **three** files, all funding:
`kraken-trading-bot-funding.service`, `.service.in`, `.timer`.

- `kraken-trading-bot-funding.timer:19` — `OnCalendar=*-*-* *:17:00`, `:21 Persistent=true`, `:22 RandomizedDelaySec=120`.
- `ExecStart` (`:39`) is `nix run …/kraken-funding-rates#kraken-funding-rates -- pull --pair @PAIR@ --output @OUTPUT@ --append` — the production path.
- `systemd/kraken-trading-bot-funding.service:9` says so itself: *"It is deliberately NOT the full Gap-2 scheduler — no news/social timers, no market-data-store timer, no flake inputs for the other two sibling CLIs."*
- `nix/module.nix` installs **no** unit (it is a NixOS module, so `systemd.user.*` cannot evaluate there — `configs/default.yaml:75-78`). Installation is `just funding-timer` (`justfile:163`).

**There is no timer for the news channel, the social channel, or the store's
append leg.** That is the single biggest structural fact in this audit.

### 1.4 State of the data artifacts on this host — [M], 2026-10-03

| artifact | state | command |
|---|---|---|
| `signals/eth_usd_funding.jsonl` | **1 line**, 364 bytes, mtime 2026-10-02 01:28 | `wc -l signals/eth_usd_funding.jsonl` |
| `models/` | **empty** (only `.gitkeep`) | `find models -type f` |
| `~/Projects/kraken-market-data/store` | **does not exist** | `ls -d ~/Projects/kraken-market-data/store` → *No such file or directory* |
| `KTB_STORE_ROOT` env | unset | `env \| grep -i KTB` → nothing |
| non-venv `*.parquet` month files under `~/Projects` | **0** | `find /home/seanc/Projects -name '*-*.parquet' -not -path '*/.venv/*' …` → none |

**The market-data store is absent on this host.** G4 ("deepen the price frame")
is not a deferred optimization here — the deep-price path is currently
*unreachable* without running `just store-seed` (~158 s per `data.py:91-99` (STORE_SEED_HINT)).
Any figure quoted in a prior artifact as "the store holds ~76.5k bars" was
measured against a store that is not present now.

---

## 2. How the RL pipeline actually consumes data

### 2.1 Config keys — every one, and whether anything reads it

`configs/default.yaml`, with the reader for each. **[C]** except where noted;
the read inventory is `[M]` from
`grep -rn --include='*.py' -oE '(config|cfg|settings|merged)\.get\("…"' kraken_trading_bot tools`.

| key | line | default | read by |
|---|---|---|---|
| `ticker` | `:8` | `ETH/USD` | `train.py:207`, `backtest.py:472`, `export.py:164` |
| `ohlcv_interval_minutes` | `:11` | `60` | `train.py:208`, `backtest.py:466`, `export.py:164` |
| `initial_balance` | `:14` | `10000.0` | `train.py:257` |
| `fee_rate` | `:17` | **`0.0`** | `train.py:258`, `backtest.py` |
| `slippage` | `:18` | **`0.0`** | `train.py:259`, `backtest.py` |
| `allow_short` | `:19` | `false` | `train.py:261` |
| `action_space` | `:24` | `continuous` | `train.py:209`, `backtest.py` |
| `reward.*` (9 keys) | `:29-38` | see below | `RewardSpec.from_dict`, `environment.py:90-113` |
| `feature_windows` | `:43` | `[1,4,24]` | `train.py:243`, `backtest.py:553`, `export.py:179`, `paper_trade.py` |
| `feature_groups` | `:48` | all five | same four |
| `extra_features_file` | `:57` | **`null`** | `train.py:220`, `backtest.py:476`, `export.py:170`, `paper_trade.py:303` |
| `funding_features_file` | `:92` | non-null path | same four |
| `social_features_file` | `:100` | **`null`** | same four |
| `signal_max_age_hours` | `:153` | `12` | same four |
| `signal_require_ticker` | `:164` | `true` | same four |
| `market_data_store` | `:212` | **`null`** | `train.py:225`, `backtest.py:483`, `export.py:175`, `paper_trade.py:308` |
| `market_data_store_venue` | `:237` | `kraken-live-rest` | `train.py:226`, `backtest.py:493`, `export.py:176` — **not `paper_trade.py`** |
| `data_window.{since,until,eval_split}` | `:284-286` | `null,null,0.7` | `train.py:232-233`, `backtest.py:455`; **`eval_split` is inert while unpinned** (`data_window.py:411-417`) and `paper_trade.py` never resolves it at all |
| `model_name` | `:291` | `ppo_eth_01` | written at `train.py:139`; **never read** — the CLI takes `--model` (`cli.py:142`) and nothing in the pipeline reads the key |

**Every key in the shipped YAML is read by at least one production leg, except
`model_name`.** No dead config key. That is genuinely clean and worth saying —
it is not what I expected to find in this repo.

One near-miss: `data.py:1319-1323` carries a `.. todo::` saying *"honour
`since`/`until` from config"*. That is **stale as a description of the code**:
`train.py:232-233` and `backtest.py:518` already apply the window (as a
post-read clip). What is actually missing is narrower and is §4 **G-A** — the
window is never pushed *into* the read.

### 2.2 `train` / `backtest` knobs

`train` (`cli.py:134-217`): `--ticker --model --config --pages(6) --episode-bars
--timesteps(10000) --seed(42) --interval-minutes --action-space
--initial-balance --fee-rate --slippage --models-root --json`.

Only five flags become config overrides (`cli.py:486-495`); the rest fall
through to YAML. **`--pages` defaults to 6** (`cli.py:154`) — i.e. the shipped
default asks for ~6 × 720 = 4,320 candles from an endpoint that serves ~720
(`data.py:283-287`, `data.py:378`), and the page loop just stops when
`last == 0` (`data.py:1129`).

`backtest` (`cli.py:223-276`): `--ticker --model --pages --seed --models-root
--config --json`. Note `cli.py:255`: backtest warns when a run names neither
`market_data_store` nor `data_window`.

### 2.3 Artifact layout and where observation width is decided

`models/{TICKER_ID}/{model_name}/` — `TICKER_ID` is `normalize_ticker_id`
(`features.py:~1258`, `ETH/USD` → `ETH_USD`).

- **`model.zip`** — PPO, written by `RLAgent`.
- **`normalization.npz`** — `NormalizationStats.save` (`features.py:~365-380`):
  four arrays: `ticker_id`, `feature_names` (numpy `U` dtype, **pickle-free**,
  `allow_pickle=False` on load), `means`, `stds`. **`feature_names` is the width
  authority** and is what `check_feature_width` compares against
  (`features.py:~472-530`).
- **`config.yaml`** — `register_model` (`train.py:289-306`). Carries the whole
  resolved config **plus** `n_features` (`train.py:288-289`), `n_bars`
  (`train.py:298`) and `train_timesteps` (`train.py:300`).

**Width is decided in three places, and all three must agree:**

1. `FeaturePipeline.compute()` builds `out` and caches it as
   `_last_feature_names` (`features.py:~830-833`); `n_features()` returns
   `len()` of that (`features.py:~900`).
2. `TradingEnvironment._raw_feature_array()` (`environment.py:470-495`)
   materialises `self._features.ffill().fillna(0.0)` → `stats.normalize(...)` →
   `float32`, and `environment.py:200` builds the `Box` from its shape.
3. `train.py:288` records `int(env.observation_space.shape[0])` into
   `config.yaml`.

The guard is **non-self-referential** by design (`features.py:~487-495`):
expected names come from the artifact, actual columns from `compute()`. Same
width, different names, still raises.

### 2.4 Measured observation widths — [M], 2026-10-03

`nix develop --command python /tmp/opencode/measure_widths2.py`, 200 synthetic
hourly bars, `feature_windows [1,4,24]`, all five groups,
`add_derived_ohlcv_features` applied (i.e. the real read seam):

| configuration | width | `first_tradable_index` |
|---|---|---|
| raw OHLCV, **no** `vwap`/`count`, all signal keys null (the F-15 trap) | **49** | 24 |
| all `*_features_file` null, `vwap`/`count` present | **52** | 24 |
| **`microstructure` group off**, all signal keys null | 52 | 24 |
| **`signals` group off**, funding dense | 49 | 24 |
| funding file **dense/backfilled, 14-key shape** (quote keys present, values null) | **60** | 24 |
| **SHIPPED DEFAULT** — funding key non-null, real checked-in 1-line file | **60** | 24 |
| funding file carrying **only `funding_rate`** | **55** | 24 |
| **all three channels** (news + funding-dense + social) | **61** | 24 |

Sane range on the shipped config family: **49 – 61**. There is no single width.

This **confirms F-8 and F-13**: 52 is the *all-null* width and 60 is the shipped
default — the `features.py` docstring, corrected in `aba7b9b`, is right.

**New, not in any prior artifact — the width is coupled to the producer's key
set, not to its values.** Same frame, same 6-column funding intent, two files:

| funding file | width | nonzero bars (of 200) | distinct | degenerate-std cols |
|---|---|---|---|---|
| 14 keys, quote fields present-but-null (**the real `backfill` shape**) | **60** | `funding_rate` 200, `basis` 0, `open_interest` 0, `funding_rate_prediction` 0, `vol24h` 0, `spread` 0 | `funding_rate` 200; every other funding col **1** | **10** |
| `funding_rate` only (a hand-made file) | **55** | `funding_rate` 200 | all others absent | 5 |
| no file at all | **52** | — | — | 3 |

`data.py:786-790` intersects on **column presence**
(`available_cols = [c for c in _SIGNAL_COLUMNS if c in signal_df.columns …]`),
so a null-valued column still yields an observation column. That is why
`justfile:206` can honestly claim the 14-key backfill "still composes 60
observation columns". The corollary is a trap: **anyone hand-rolling a
backfill JSONL with only `funding_rate` silently gets 55 and a
`FeatureWidthMismatchError` against a 60-wide artifact** — a provenance accident
presenting as a config error.

Also **[M]** on the shipped default against the **real** checked-in file
(`/tmp/opencode/m5.py`, 721 bars, width 60, `first_tradable_index` 24 → **697
tradable bars**):

```
funding_rate              nonzero= 13/721  distinct=  2  std=0.00336272
basis                     nonzero= 13/721  distinct=  2
open_interest             nonzero= 13/721  distinct=  2
funding_rate_prediction   nonzero= 13/721  distinct=  2
vol24h                    nonzero= 13/721  distinct=  2
signal_observed           nonzero= 13/721  distinct=  2
signal_age_hours          nonzero=720/721  distinct= 14   (max 12.0 → confirms F-10)
```

`funding_rate` at **2 distinct values on 13 of 721 bars, std 0.00336** is the
DECISION §3.1 figure reproduced on the shipped default **at HEAD**. I did not
use a fixture that lacked `vwap`/`count`, which is what made the earlier 57/49
constants 3 low (F-8).

(The `vwap_dev` std in that run is ~5.6e-17 — an artifact of *my* synthetic
`vwap = close × 1.0005`, not a repo finding. Excluded deliberately.)

---

## 3. Carried over from prior passes

Each prior gap → still open / fixed / partial, with the `file:line` that decides it.

| prior gap | verdict today | deciding evidence |
|---|---|---|
| **G1** `microstructure` group enabled but `order_book_imbalance` unreachable | **still open, unchanged** | `features.py:1018-1025` is the *only* occurrence of `bid_vol`/`ask_vol` in the whole tree — no producer. They are also **not** in `_SIGNAL_COLUMNS` (`features.py:94-125`), so `data.py:786-790`'s intersection would drop them even if a signal file carried them. `order_book` is fetched at `engine.py:72` and read by no strategy (`sma.py:68-69` reads only `candles` and `ticker`). **Correction to `PLAN.md` §4/G1:** the citation "`strategies/base.py:92`" is the *docstring* listing `order_book` as an available key, not the discard site. The discard is the absence of any reader. |
| **G2** funding log one record deep | **producer fixed, ARTIFACT not shipped** | The producer exists (`justfile:235 funding-backfill`, sibling @`dc49847`). But **[M]** `signals/eth_usd_funding.jsonl` is **1 line** (364 bytes, mtime 2026-10-02 01:28). **[M]** The shipped default therefore still trains on `funding_rate` = **2 distinct values on 13/721 bars**. Every "721 distinct values" figure in `RUN-LOG.md` was measured on a `/tmp` artifact that was never committed. |
| **G3** no retry / backoff / rate-limit handling | **still open, and now cited upstream too** | `data.py:1125-1133` (`_page_candles`) has no `try`. `kraken_api/transport.py:186` `time.sleep(wait)` is the fixed-interval **throttle**, not a retry; `_request` **re-raises** on `requests.RequestException` and on `RateLimitError` (`:250`). `grep -rn 'retry\|backoff\|MAX_RETR' kraken_api/` → **zero hits**. `min_interval` defaults to `0.0` (`transport.py:108`), i.e. no throttle either. Contrast the sibling, which retries: `kraken-funding-rates/client.py:53-95`, `_MAX_RETRIES = 3` at `:25`. |
| **G4** thin historical depth behind `market_data_store: null` | **open, and worse than recorded** | **[M]** The store does not exist on this host (§1.4). `justfile:289` points at `~/Projects/kraken-market-data/store`, unsettable via `KTB_STORE_ROOT`. So today there is no deep-price path at all, only the ~721-bar REST ceiling. |
| **G5** dead strategy machinery + a wrong bar-interval comment | **the wrong comment is fixed; the dead machinery is not** | The 8-hour funding comment was corrected in `4B`/`Phase 5` (now `data.py:118-126`, `systemd/…timer:2-4`). Still dead: `TradingEnvironment.reset(options=…)` has **no caller** — only `environment.py:211,231` `self.reset(seed=…)` and `backtest.py:629`. |
| **G6** zero fees and zero slippage in the shipped config | **still open** | `configs/default.yaml:17-18` — `fee_rate: 0.0`, `slippage: 0.0`. `cli.py` echoes what was applied (`cli.py:621-623`), so the output is honest, but the *default* is frictionless. |
| **G7** whole data categories with zero presence | **still open** | §5 below. |
| **F-8 / F-13** 52 vs 60 | **confirmed by measurement** | §2.4. |
| **F-10** `signal_age_hours` max 12.0 | **confirmed** | `signal_age_hours distinct=14` over 721 bars, max 12.0 on the shipped shape (§2.4). |
| **F-15** `width_check.py` reports 49 for a raw parquet | **confirmed, reproduced, still unguarded** | `tools/width_check.py:53-55` calls `pipe.compute(df)` with no `add_derived_ohlcv_features`. My §0 note is the live reproduction. The docstring (`:1-27`) still carries **no** warning — `PLAN.md` §6 item 2 was not done. |
| **F-14** vacuous characterisation tests | **unchanged** (no executable change this pass to make them non-vacuous) | — |
| **F-18** funding depth cannot buy episode length | **still open, and now doubly true** | §2.4 + §1.4: the funding channel is 366 d *available*, and the price frame it merges into is 721 bars *and the store is absent*. |
| **`PLAN.md` §6 item 2** — annotate `width_check.py`'s docstring | **NOT DONE** | `tools/width_check.py:1-27`. |
| **`PLAN.md` §8.1** — G1 depth recorder, scheduled | **NOT DONE. No recorder exists.** | `grep` for a recorder: nothing in `systemd/`, `tools/`, or the siblings beyond funding. This is the time-critical item from the last plan and it has not started. |
| **`PLAN.md` §8.0** — `since`/`until` on the shipped default | **CLOSED at `c6295be`, correctly** | README + justfile header carry the "a default run is NOT an out-of-sample measurement" block. Do not reopen. |

**New this pass, not previously recorded:**

- **N-1** No production caller passes `since=`/`until=` to `read_ohlc_dataframe` (§4 G-A).
- **N-2** `market_data_store_venue` is dropped on the paper-trade leg (§4 G-B).
- **N-3** Observation width is coupled to the sibling producer's key set (60 vs 55) (§2.4).
- **N-4** The store is absent on this host (§1.4).
- **N-5** `model_name` in `configs/default.yaml:291` is read by nothing.

---

## 4. Ranked candidate gaps

Category-agnostic: these came out of reading the code, not out of a preset
list. Six candidates, spanning **four** categories. The architect chooses; I do
not commit to one.

### 🥇 G-A — `read_ohlc_dataframe`'s `since`/`until` are unreachable, and the `.. todo::` describing them is stale

**Category: IMPROVE-EXISTING / effective-but-unapplied machinery.**

**[C]** `data.py:1235-1236` declares `since: int | None = None, until: int | None
= None`, and `data.py:1402` forwards them into `store.read(pair, interval,
since=since, until=until)` — the mechanism that makes a *deep* store windowable
and is the whole stated point of `configs/default.yaml:182-183` (*"`read_ohlc_dataframe`
already forwards since/until (None -> whole store) to `store.read`"*).

**[C]** No production caller passes either argument. All four call sites omit
them: `train.py:215-227`, `backtest.py:471-499`, `export.py:165-177`,
`paper_trade.py:298-309`. Instead the window is applied *after* the read as a
clip — `train.py:232-233` (`resolve_data_window` → `training_frame`),
`backtest.py:518-519` (`evaluation_frame`). **[M]** `grep` for
`since=` / `until=` at those four sites returns nothing.

Consequence: with a seeded store, `train` reads **the whole store** and then
throws most of it away, and `pages` only bounds the live append
(`configs/default.yaml:184`) — so the *read* cost scales with store depth while
the *episode* does not. And `data.py:1319-1323`'s `.. todo::` says "honour
since/until from config", which now reads as an unfixed gap when `train.py:232`
already does exactly that one layer up. A reader auditing this file will either
double-fix it or trust the todo.

**Directness: high but indirect.** It does not add an observation column. What
it buys is that the exogenous depth already present (funding: 366 d, §2.4) and
any future recorder depth land on an episode that is *long enough to hold them*
— i.e. it is the unblocking half of F-18.

**Sourcing: none.** This is a parameter-plumbing change in four call sites.
`data.py` already has the parameter; nothing new is fetched.

**Blast radius:** `data.py` (stale todo only), `train.py`, `backtest.py`,
`export.py`, `paper_trade.py`, `data_window.py` (the clip may become
redundant or must stay as the authority), `tests/test_evaluation_scope.py` (four
`read_ohlc_dataframe` monkeypatches at `:628,670,710,752` would need the new
kwargs). **Five production files plus one test file.**

### 🥈 G-B — `paper-trade` is the only leg that mislabels its venue, and it re-fetches the whole window every 60 s

**Category: IMPROVE-EXISTING / data-quality & reliability of what is already pulled.**

Two defects on the one leg that actually places orders.

**(i) Venue provenance dropped.** **[C]** `market_data_store_venue` is passed by
`train.py:226`, `export.py:176` and `backtest.py:493`, and **omitted** at
`paper_trade.py:298-309`. `data.py:1362` therefore falls back to
`DEFAULT_STORE_VENUE` (`data.py:84`) and `data.py:1367-1374` logs
`venue: kraken-live-rest` — **for a store that `kraken-deep-history` seeded from
Binance-USDT spot**, which `configs/default.yaml:222-230` explicitly says must
be labelled. The comment at `data.py:82-83` says the label exists *because* "the
config key … states which venue the bars came from"; on the live leg it does not.

**(ii) No tail read, no throttle, full re-derive per tick.** **[C]**
`paper_trade.py:65` `_FETCH_PAGES = 2`, used at `:301`, inside `_fetch_data()`
called from `step()` (`:386`) on every tick; `run_paper_trader(..., interval=60)`
defaults to a 60 s sleep (`paper_trade.py:597`, `:665`). So the loop issues up
to **2 full OHLC page calls per minute, forever**, then rebuilds the entire
feature matrix and re-z-scores it (`paper_trade.py:311+`), to read one row.
`data.py:1126` has no error handling, so a single 429 kills the tick (G3).
`data.py:1319-1323`'s own `.. todo::` already names the fix — *"let … paper
trade collapse to append + tail read"* — and it is still open.

**Directness: none to the observation, high to the trading decision.** The
observation is unchanged; what changes is that the live leg stops mislabelling
its own input and stops paying ~1,440 candles + a full `compute()` per minute to
use one row. Category-adjacent to microstructure in that it is the same
"fetch more than you consume" waste.

**Sourcing: none.** Both are local.

**Blast radius:** `paper_trade.py` (two sites), plus a test for the venue
forwarding — `tests/test_rl_signal_config_wiring.py` is the file that already
AST-asserts this class of forwarding (`:497-551`), so it is where the guard
belongs.

### 🥉 G-C — Two of the three exogenous channels are structurally unreachable: null key, no producer schedule, no recipe

**Category: IMPROVE-EXISTING (activation), with a `NEW-DATA-SOURCE` tail.**

**[C]** `configs/default.yaml:57` `extra_features_file: null` and `:100`
`social_features_file: null`. **[C]** `systemd/` holds one timer, funding-only,
whose own unit says it is "deliberately NOT the full Gap-2 scheduler — no
news/social timers, no market-data-store timer" (`…funding.service:9-10`).
**[C]** `justfile` has `funding-timer`/`funding-pull`/`funding-backfill`
(`:163,186,235`) and **no** news or social recipe. **[C]** `flake.nix:6-7` pins
exactly two siblings; `ticker-news-signals` (`23dc965`) and
`kraken-social-signals` (`71ca27d`) are neither pinned nor in the dev shell.

So **6 observation columns are dead in the shipped default**:
`sentiment_score`, `article_count`, `novelty_flag` (`features.py:95-97`) and
`stt_mention_count`, `stt_tilt`, `fng_index` (`features.py:101-103`).

**[M]** The measurement that makes this concrete: activating all three channels
takes the observation from **60 → 61** (§2.4) — because funding-dense is 55 and
news+social add 3+3. On the *shipped* 60-wide artifact the same activation is
**+6 columns at +6 width**, and `check_feature_width` will correctly reject a
60-wide artifact against it. This is a genuine widening, unlike G2 which was
width-neutral.

**Directness: highest of anything still on the table.** Both channels are
already per-`(ticker, hour)` scalar vectors that the merge seam already accepts
verbatim — the *file format is done*. What is missing is that nobody runs the
producer. Per-ticker, per-timestamp, scalar.

**Sourcing: already solved.** `ticker-news-signals` pulls Google News via
`gnews` (keyless, no auth); `kraken-social-signals` pulls StockTwits + Fear &
Greed (keyless). Neither needs a paid feed. **[I]** Rate limits are
Google/StockTwits-unofficial, so cadence should be hourly to match the bar grid —
an inference from the absence of a documented limit, not a measured figure.

**Blast radius:** `configs/default.yaml` (two keys), two new `.service`/`.timer`
pairs + two `just` recipes modelled on `justfile:163`, and the
`_SIGNAL_COLUMNS`-width consequence in every existing artifact (every
`normalization.npz` in `models/` becomes stale — currently moot, `models/` is
empty, §1.4). **The subtlety: activation must not change the schema later**, so
the two new channels should be recorded with the same "log, not state"
discipline the funding channel uses.

### 4️⃣ G-D — `order_book_imbalance` still has no producer, and the book is already being thrown away every 60 s (prior G1, re-verified)

**Category: market microstructure.** Same finding as the last two passes,
re-verified against HEAD and still open (§3). **[C]** `engine.py:72`
`data["order_book"] = self.manager.order_book(pair, count=10)` runs every
`interval=60` (`engine.py:36`) and **no strategy reads it** — `sma.py:68-69`
reads `candles` and `ticker` only, and `base.py:92` is documentation, not code.
**[C]** `features.py:1018-1025` computes `order_book_imbalance` from
`bid_vol`/`ask_vol`, which exist nowhere else in the tree and are absent from
`_SIGNAL_COLUMNS` (`features.py:94-125`), so the merge seam's intersection
(`data.py:786-790`) would drop them even if a producer wrote them.

**Directness: the only remaining candidate that adds a genuine new dimension.**
`order_book_imbalance` is a per-bar scalar in [-1, 1] computed from two numbers —
exactly the shape the observation wants.

**Sourcing: keyless today, unusable for history.** `manager.order_book` →
`client.depth()` → `/0/public/Depth`, keyless. `PLAN.md` §8.1/§8.1.1 record why
it is forward-only by physics (no historical depth endpoint; a `count=10`
snapshot at *t* does not encode *t−1h*) and that `cryptofeed` is AGPL-3.0-or-later
requiring Python ≥3.13 while `ccxt` is MIT.

**Blast radius:** a recorder (new sibling subcommand or a small producer inside
the funding family), a timer, and `_SIGNAL_COLUMNS` + `_SIGNAL_BUILDER_INPUT_COLUMNS`
(`features.py:94-149`) widened for `bid_vol`/`ask_vol`. Plus every existing
artifact's width. **Not started, and the last plan named it time-critical.**

### 5️⃣ G-E — No retry/backoff/rate-limit handling anywhere on the fetch path (prior G3)

**Category: data-quality / reliability.** **[C]** `data.py:1125-1133` has no
`try`; a partial page-loop failure propagates out of `_page_candles` and
`fetch_ohlc_dataframe` never reaches `candles_to_dataframe`, so **every candle
already collected is discarded**. **[C]/[M]** `kraken_api/transport.py:186` is
the only sleep and it is a *throttle*; `_request` re-raises on both
`requests.RequestException` and `RateLimitError` (`:250`); `min_interval`
defaults to `0.0` (`:108`). `grep -rn 'retry\|backoff\|MAX_RETR' kraken_api/` →
**zero hits**. So the *upstream half* of G3 is unfixable without a lock bump
(`flake.nix:6`), and only the consumer half (a bounded retry around
`data.py:1126`) is reachable from this repo.

**Directness: none to the observation.** Its value is that a run does not
vanish. It is amplified by G-B(ii): paper-trade calls that loop 60×/hour.

**Blast radius:** `data.py` only — one function. Smallest candidate here.

### 6️⃣ G-F — Store holes are still invisible to the feature pipeline (prior §14 / CAND-3b)

**Category: data-quality of what is already pulled.** **[C]** `data.py:1402`
`store.read(...)` concatenates month files; there is no reindex onto a regular
bar grid and no gap assertion on the read path. `configs/default.yaml:198-211`
documents the measured damage — *"158 missing bars across 28 gaps, largest 39h,
including a 38-bar hole at the SEED/LIVE-APPEND SEAM"* — and correctly states
*"No guard here can catch it: every value is finite and correctly computed from
the rows given."* `tools/store_gap_scan.py` + `just store-verify`
(`justfile:349`) detect and label it; nothing consumes the verdict.

**Caveat I must state:** that 158/28/39h figure is **[I] an inference from a
config comment**, and §1.4 shows the store is absent on this host, so I could not
re-measure it. It should be re-derived after a seed, not quoted.

---

## 5. Category coverage — the breadth self-check

Per the brief, including the categories with **nothing** found, because a
reader needs to know they were looked for.

| category | what exists in the tree | verdict |
|---|---|---|
| **IMPROVE-EXISTING** (first to mine) | 4 candidates: G-A, G-B, G-C, G-D | **richest category, as expected** |
| data-quality / reliability | G-E (no retry), G-B(ii) (no tail read), G-F (store holes) | 3 candidates |
| text / news | seam + 3 columns ready (`features.py:95-97`), producer exists @`23dc965`, **key null, no timer, no recipe** | unreachable in shipped default |
| social / sentiment | seam + 3 columns ready (`features.py:101-103`), producer exists @`71ca27d` (StockTwits + Fear & Greed), **key null, no timer, no recipe** | unreachable in shipped default |
| market microstructure | `microstructure` group enabled (`configs/default.yaml:48`) but only `spread` is reachable, and only from a live snapshot's `bid`/`ask`. `order_book_imbalance` unreachable. `recent_trades`, `spread` (time series) and the entire WS surface wrapped, uncalled | 1 candidate (G-D) |
| cross-exchange | the store can hold a **Binance-USDT** seed (`configs/default.yaml:222-230`, `data.py:85 SEEDED_STORE_VENUE`) and a Kraken live leg, but nothing ever holds both at once, and there is no basis/spread column between them | **no candidate** — would need new code and new sources |
| funding / derivatives | the one channel that works; 6 columns, 1 deep, 5 zero-fill (§2.4) | prior G2, partial |
| macro / economic calendars | **zero presence** — no key, no seam column, no sibling, no code path | **no candidate**; scoping pass required |
| on-chain / crypto-native | **zero presence** — same | **no candidate** |
| research / academic | **zero presence** | **no candidate** |
| options surface / derivatives chain | **zero presence** | **no candidate** |

Four categories are empty, which is a real finding but is *not* the same as a
ranked gap: nothing here has a seam, a config key, or a producer to build on, so
each is a scoping exercise, not a slice. Consistent with `PLAN.md` §4/G7
("Needs its own scoping pass before it can be ranked").

---

## 6. What I checked and found clean — so nobody re-audits it

1. **Every `configs/default.yaml` key is read** by at least one production leg.
   The only exception is `model_name` (`:291`), which nothing reads (N-5).
2. **The merge seam is genuinely hardened.** Ticker filter (`data.py:866`),
   floored-hour dedup (`data.py:776-781`), bounded carry (`data.py:821`),
   absence-is-not-zero (`data.py:833-849`), and a *declared* failure for a
   configured-but-unusable file (`data.py:710`, `:740`) — all present and
   tested (`tests/test_rl_signal_config_wiring.py`).
3. **`check_feature_width` is non-self-referential** (`features.py:487-495`):
   expected names from the artifact, actual from `compute()`. It cannot be
   defeated by a same-width name swap.
4. **`normalization.npz` is pickle-free** (`features.py:~372` `'U'` dtype;
   `allow_pickle=False` on load).
5. **The IN-SAMPLE labelling works.** `backtest.py:518` derives the verdict
   before any number (`backtest.py:530-541` warns when it is IN-SAMPLE); `cli.py:599` prints
   `Evaluation:` as the **first** line of
   the report. `evaluate_scope` derives the verdict from the actual halves, not
   a flag.
6. **`frictionless` defaults are disclosed, not hidden** — `cli.py:617-620`
   echoes the fee/slippage actually applied.
7. **The funding producer hint is right.** `data.py:170`
   `"just funding-backfill && just funding-pull"` uses `&&` with a comment
   (`data.py:162-169`) recording that `#` silently ran the backfill alone.
8. **The timer `ExecStart` is the production path** (`…funding.service:39` =
   `nix run …`, the same shape `justfile:193` uses) — not a dev-shell `PATH`
   lookup, which would not work (`flake.nix:6-7` pins only two siblings).

---

## 7. Open questions deliberately left to Phase 2

1. **Should the backfilled funding file be committed?** `signals/eth_usd_funding.jsonl`
   is 1 line at HEAD (§1.4), so the shipped default still trains on a
   near-constant column. The file is a *log* that ages out (~366 d rolling
   window), so committing a snapshot buys a fixed window and then goes stale.
   That trade-off is a decision, not an obvious yes.
2. **Width-vs-freshness on channel activation (G-C):** adding news+social takes
   60 → 61 and invalidates every existing artifact. Is a widening worth it now,
   or should it wait until G-A/G4 make the episode long enough to see them?
3. **Does G-A pay for itself before G4?** If the store is seeded and the window
   is clipped after a full read, the read cost is proportional to store depth —
   so pushing the window *into* the read may matter more on a seeded store than
   it does today.
4. **The two 60-vs-55 shapes (N-3):** should `_SIGNAL_COLUMNS` intersection be
   presence-gated (current, §2.4) or value-aware? Presence-gating is what makes
   a null-valued backfill width-neutral; value-aware would make the width honest
   about how much of it is real. Both defensible; the choice changes what a
   width number *means*.
5. **Re-derive F-18's arithmetic against a seeded store.** §1.4 shows the store
   is gone, so the "76.5k bars" figure in `PLAN.md` §8.1.2 point 2 cannot be
   re-measured now. Per `PLAN.md` §8.3, re-derive rather than inherit.
6. **`tools/width_check.py` docstring (F-15 / `PLAN.md` §6 item 2).** Still
   unannotated, and I have now reproduced the trap twice in one session. A
   docstring-only edit is invisible to the `executable-ast` guard, so it is safe
   whenever someone picks it up.

---

*Written by the auditor, pass 2026-10-03, read-only. Every number carries its
frame or is labelled `[I]`. Bar counts and widths are dated. No source file was
modified; nothing was committed.*
