# G1 branch audit — the three uncontained `opencode/*` commits

Scope: `e57056d`, `ab44f11`, `4c65432`. Method is the one accepted at **RUN-LOG §14**
(patch-id against master, per-hunk, behaviour-in-master or not). Nothing was deleted by
this pass; no tag was created; `signals/` and `notebooks/` were not touched.

Baseline for containment: `master` = `b99bc56`. The suite baseline measured in this
worktree (at its own HEAD `8b67d48`) is **518 passed**.

---

## Summary

| sha | subject | verdict |
|---|---|---|
| `e57056d` | wire normalization.npz into the observation path | **SUPERSEDED** — 0 of 16 hunks carry behaviour master lacks |
| `ab44f11` | `extra_features_file` seam for exogenous news signals | **SUPERSEDED** — and the seam is the **best-protected** thing in the repo (4 mutations, all RED) |
| `4c65432` | export-data CLI + config default path fallback (WIP) | **SUPERSEDED** — 9/9 files, 5/5 symbols, tests ≥ shipped on all three files |

No branch among the three needs human judgment. **Nothing needs preserving before
deletion on content grounds** — but see "Ref preservation" below, which is about
trail, not content.

---

## 1. `e57056d` — hunk table

The task brief named five files. The commit actually touches **seven** —
`features.py` and `paper_trade.py` are also in it and both are load-bearing. All seven
were assessed.

Every one of the commit's **87 substantive added lines is present in master verbatim**,
with one exception, and that exception is master being *ahead*:

| # | file | hunk | change | master | verdict |
|---|---|---|---|---|---|
| 1 | `justfile` | 1 | doc flip: `z_` block "is the agent's z-scored observation" | lines 121-122, **byte-identical prose** | carried |
| 2 | `cli.py` | 1 | `--normalized` help text flipped | lines 339-341, **byte-identical** | carried |
| 3 | `data.py` | 1 | `from .features import (FeaturePipeline, _SIGNAL_COLUMNS, normalize_ticker_id)` | `data.py:48` imports `_SIGNAL_COLUMNS` | carried |
| 4 | `data.py` | 2 | comment: canonical definition now in `features` | `data.py:65-67`, byte-identical | carried |
| 5 | `data.py` | 3 | `prepare_episode` docstring: slice-then-fit | `data.py:1576-1581`, byte-identical | carried |
| 6 | `data.py` | 4 | **delete** `features.fit(df, ...)` before windowing | `features.fit(df, ticker_id=...)` absent | carried |
| 7 | `data.py` | 5 | **add** `features.fit(window, ...)` after windowing + comment | `data.py:1613` `window = df.tail(...)`, `1619` `features.fit(window, ...)` | carried |
| 8 | `environment.py` | 1 | `_raw_feature_array`: ffill→**z-score via `stats.normalize`** | `environment.py:488-496`, identical expressions | carried |
| 9 | `export.py` | 1 | module docstring: features = pre-transform, `z_` = the observation | `export.py:25-43`, byte-identical | carried |
| 10 | `export.py` | 2 | `include_normalized` docstring flipped | `export.py:146-148` | carried |
| 11 | `features.py` | 1 | `_SIGNAL_COLUMNS` comment: canonical here | `features.py:75-78` | carried |
| 12 | `features.py` | 2 | `fit` docstring: stats fitted on the ffilled frame | `features.py:612-617`, byte-identical | carried |
| 13 | `features.py` | 3 | `fit`: `compute(df)` → `compute(df).ffill().fillna(0.0)` | `features.py:642` | carried |
| 14 | `features.py` | 4 | `transform` docstring: warmup rows map to `(0-mean)/std` | `features.py:678-682`, byte-identical | carried |
| 15 | `features.py` | 5 | `transform`: same fill; **and drop** trailing `normalized.ffill().fillna(0.0)` | `features.py:740` has the fill; `normalized.ffill().fillna(0.0)` **absent** (goes straight to `out = normalized.to_numpy(...)`) | carried |
| 16 | `paper_trade.py` | 1 | `_build_observation` docstring: filled **then z-scored** | `paper_trade.py:322-330` | carried |
| 17 | `paper_trade.py` | 2 | live window: `stats.normalize` applied before taking the last row | `paper_trade.py:344-350`, plus `self._validate_observation(computed.columns, stats)` | carried, **master ahead** |

**The single line not present verbatim** is `features = self.pipeline.compute(df).ffill().fillna(0.0)`
(`paper_trade.py`). Master reads:

```
computed = self.pipeline.compute(add_derived_ohlcv_features(df))
features = computed.ffill().fillna(0.0)
```

— it interposes the vwap/count derivation and a comment. A strict superset.

### Where the reused `6ebe95e` reasoning applies

Hunks 13, 14, 15, 8, 17 are the **same expressions** §14 already accepted for `6ebe95e`
(`features.py:642` / `:740`, `environment.py:488`, `export.py:192`, `paper_trade.py:345`),
and §16 already proved that family protected by mutation (61-test blast radius at the
fit site, plus the `c8aed16` site-presence guard). Not re-argued. Master's `fit`/
`transform` and `environment`/`paper_trade` docstrings are in fact **byte-identical to
`e57056d`'s**, so master landed this branch's wording, not `6ebe95e`'s.

One naming note: `e57056d` names the tuple `_SIGNAL_COLUMNS`; `6ebe95e` named it
`SIGNAL_COLUMNS`. **Master kept `e57056d`'s spelling** (`features.py:94`). Master is a
strict **superset** on content — 9 entries in the branch, 21+ today (adds
`signal_age_hours`, `signal_observed`, `vwap_dev`, `spread`, `bid`, `ask`,
`funding_rate_prediction`, `vol24h`, …).

### Master is ahead in three places `e57056d` has nothing for

1. `_require_finite` guards on both `fit` and `transform`, with `NonFiniteFeatureError`
   (`features.py:619-627`, `688-695`) — the branch has no equivalent.
2. `add_derived_ohlcv_features` inside `paper_trade._build_observation`.
3. `_validate_observation(computed.columns, stats)` — checks the live frame's columns
   against the model's own npz, replacing a check that compared against a vector
   `normalize` had already narrowed to the model's width.

**VERDICT: SUPERSEDED.** No hunk is missing. No preservation recommended.

---

## 2. `ab44f11` — mutation, not containment

This one is an **activated** feature, so "master contains it" is not the question. The
question is whether removing it turns the suite red. It does — at every level.

Baseline in this worktree: **518 passed**. Four mutations, each reverted before the next.

| # | mutation | result |
|---|---|---|
| **M1** | `merge_extra_features` body replaced with `return df` — i.e. exactly the stub `ab44f11` shipped | **41 failed, 477 passed** |
| **M2** | seam function left intact, but both `read_ohlc_dataframe` / store-leg wiring blocks removed | **18 failed, 500 passed** |
| **M3** | `train.py`: the three `*_features_file=cfg.get(...)` kwargs dropped — `ab44f11`'s exact `train.py` contribution | **4 failed, 514 passed** |
| **M4** | `configs/default.yaml`: `extra_features_file` back to `null` — the value `ab44f11` shipped, i.e. de-activation | **6 failed, 512 passed** |

Named guards, so the protection is attributable rather than incidental:

- M1/M2 — `test_three_channel_seam.py`: `test_export_lands_all_three_channels_in_one_frame`,
  `test_the_freshness_pair_combines_across_all_three_channels`,
  `test_both_read_legs_pass_the_three_values_in_channel_order`,
  `test_backtest_reads_all_three_channels_and_runs`,
  `test_paper_trade_reads_all_three_channels`,
  `test_one_live_channel_among_nulls_raises_naming_that_channel`.
- M3 — `test_gc_channel_activation.py::test_every_consumer_threads_all_three_signal_file_keys`,
  `test_rl_signal_config_wiring.py::test_every_consumer_threads_both_signal_keys`,
  `test_every_read_ohlc_dataframe_call_site_forwards_venue`.
- M4 — `test_rl_data_store.py::test_the_shipped_default_declares_all_three_channels_non_null`,
  `test_the_shipped_default_channel_files_agree_with_the_timer_units`,
  `test_three_channel_seam.py::test_the_store_key_and_the_signal_keys_ship_differently_on_purpose`,
  `test_gc_channel_activation.py::test_the_config_comments_document_the_fresh_clone_consequence`.

The **finding worth more than the verdict**: this seam is not merely superseded, it is
the most heavily defended seam in the repo. The specific thing a reviewer would
reasonably worry about — *that a config key can be set but silently ignored* — is closed
by M3+M4 going red with named tests. Note M4 also fails on a **comment** assertion, so
the documented consequence of the fresh-clone state is pinned too.

### Tree restored, proven

```
$ git status --porcelain            # 0 bytes — empty
$ git diff HEAD --stat              # empty
$ git ls-files --others --exclude-standard   # empty
$ nix develop --command 'pytest tests/ -q'
518 passed, 24 warnings in 71.18s
```

Identical to the pre-mutation baseline. No mutation was left in the tree.

**VERDICT: SUPERSEDED.** No preservation recommended.

---

## 3. `4c65432` — the light pass (`52271cb`'s shape)

- **Files: 9/9 present.** `.gitignore`, `justfile`, `cli.py`, `rl/__init__.py`,
  `rl/export.py`, `rl/train.py`, and all three test files.
- **Exported symbols: 5/5 present.** `build_export_frame`, `write_export_csv`,
  `default_export_path` (`export.py`, all re-exported from `rl/__init__.py:100-102`),
  `cmd_export_data` (`cli.py:699`, dispatched at `cli.py:766`), `resolve_default_config_path`
  (`train.py`, exported `__init__.py:96`).
- **Justfile: 3/3 recipes present with identical parameter lists** — `run` (:84),
  `bench` (:109), `export-data` (:125).
- **`.gitignore`:** `exports/` present at :57.
- **Tests — master ≥ shipped on every file:**

  | file | before | shipped | master | master − shipped |
  |---|---|---|---|---|
  | `test_rl_export.py` | 0 (new) | 10 | **11** | +1 |
  | `test_rl_cli.py` | 14 | 19 | **21** | +2 |
  | `test_rl_training.py` | 12 | 14 | **18** | +4 |

  The commit message names the red test that made the branch worth landing —
  `test_build_export_frame_normalized_block_is_z_scored`. It is in master **under that
  exact name** at `test_rl_export.py:205`, and master adds
  `test_normalized_block_equals_environment_observation` (:276).

**VERDICT: SUPERSEDED.** Rebuilt, not merged — hence the differing patch-id. No
preservation recommended.

---

## CHECK A — is anything holding these branches?

**Method used: `git worktree list --porcelain`** — the authoritative one, because a
worktree can be *registered without a directory* (the `prunable` shape §13 documents),
which a directory listing cannot see. Cross-checked with
`find "$GIT_COMMON_DIR/worktrees" -maxdepth 2 -name locked` and a per-entry read of each
`worktrees/*/HEAD`.

Registered worktrees at audit time — **three**, and none of them is on an `opencode/*`
branch except this session's own two:

```
worktree /home/seanc/Projects/kraken-trading-bot                     branch master
worktree …-x622s1-branch-audit2                                     branch …-x622s1-branch-audit2
worktree …-x622s1-recorder                                           branch …-x622s1-recorder
```

- `locked` occurrences in that output: **0**. `locked` files under `worktrees/`: **0**.
- `prunable` occurrences: **0**. Every registered entry's directory exists (verified by
  reading `worktrees/*/gitdir` and stat-ing the parent, not inferred from the flag).

**So all three subjects are demonstrably IDLE.** No worktree is checked out on
`audit-pipeline-geu0v0-builder` or `ticker-pipeline-t2huls-integrator`; no worktree
anywhere is locked; nothing is registered at all for the other twelve.

Caveat that weakens "idle" slightly, stated rather than hidden: two branches holding
these commits have **remote** copies — `origin/…/geu0v0-builder` = `e57056d` and
`origin/…/geu0v0-builder-2` = `6ebe95e`. `ab44f11` has no remote. A remote ref is not a
preservation guarantee (it is gc-eligible on the remote too), so it is recorded, not
relied on.

Reflog freshness also says idle: `geu0v0-builder`'s last entry is the commit itself at
**2026-09-30 22:42**, `t2huls-integrator`'s at **2026-09-27 21:21** — three and six days
before this audit, and neither has a checkout entry since.

---

## CHECK B — the contained branches

**Measured at `master` = `8b67d48`, before the deletions below.** Containment was
re-verified against the then-current `b99bc56` for the two survivors; `b99bc56` touches
only `tests/test_rl_environment.py`, so no source-hunk verdict moved.

| branch | tip | patch-id verdict | worktree on it? | worktree clean? |
|---|---|---|---|---|
| `…-audit-pipeline-1002-5tbhsv-builder` | `840c3e3` | 0 ahead | no | n/a |
| `…-audit-pipeline-1002c-h7biuo-builder` | `4fba0ab` | CONTAINED (1/1) | no | n/a |
| `…-audit-pipeline-1003-1m66ht-builder-gc` | `b112184` | 0 ahead | no | n/a |
| `…-audit-pipeline-1003-1m66ht-integrator` | `6844cb0` | CONTAINED (1/1) | no | n/a |
| `…-audit-pipeline-2-kd5vk5-builder` | `2df5715` | CONTAINED (2/2) | no | n/a |
| `…-audit-pipeline-93bxf0-builder` | `62d1e98` | CONTAINED (1/1) | no | n/a |
| `…-audit-pipeline-93bxf0-integrator` | `0227d04` | CONTAINED (2/2) | no | n/a |
| `…-audit-pipeline-geu0v0-integrator` | `9a2cb4c` | CONTAINED (1/1) | no | n/a |
| `…-audit-pipeline-guu5vi-builder-integrate` | `c265523` | CONTAINED (1/1) | no | n/a |
| `…-audit-pipeline-guu5vi-builder-scaffold` | `c59d9a0` | 0 ahead | no | n/a |
| `…-data-audit-1002-g08vu1-builder` | `e824f0c` | CONTAINED (1/1) | no | n/a |
| `…-matrix-harness-pseemi-matrix-verify` | `6bce756` | 0 ahead | no | n/a |

**Correction to the brief's arithmetic: eight branches are CONTAINED with commits, not
seven**, plus four at zero-ahead — twelve in total, plus the two carrying the
uncontained commits = the fourteen the brief counted. `n/a` for worktree cleanliness is
literal: no worktree was ever checked out on any of these twelve, so there was no
directory whose porcelain could be read. That is stronger than "clean".

### These twelve were deleted by another actor mid-audit

While this pass was running, `opencode/*` went from 16 refs to 4. The table above is the
evidence that the deletion was safe — it was captured before. **The two branches holding
the uncontained commits survived**, which is the correct outcome.

Six of the twelve deleted tips are now **pointed at by no ref and are not ancestors of
master**; they survive on reflog alone and are gc-eligible:

`4fba0ab`, `2df5715`, `62d1e98`, `0227d04`, `c265523`, `e824f0c`

Nothing is lost — all six were patch-id CONTAINED, which is exactly the condition that
made deleting them correct. But the *trail* is now only in this file. If the lead wants
the shas to survive `git gc`, one annotated tag each is sufficient and free:

```
git tag -a archive/4fba0ab-h7biuo -m "..." 4fba0ab
```

Two further deleted tips survive only via `origin/` refs (`6844cb0`, `9a2cb4c`), and two
have exact-sha twins under `ensemble/preserved/` (`c59d9a0`, `6bce756`) so those are
belt-and-braces already.

### Ref preservation for the three subjects

All three are superseded, so no tag is *needed*. If the lead wants the pre-deletion
state reproducible the same way §13 did it, the three are:

| tag name | sha |
|---|---|
| `archive/e57056d-normalization-wiring` | `e57056d` |
| `archive/ab44f11-extra-features-seam` | `ab44f11` |
| `archive/4c65432-export-data-wip` | `4c65432` |

`4c65432` and `e57056d` are on one branch (`…-audit-pipeline-geu0v0-builder`, in that
order), so deleting that branch drops both unless they are pinned first. `ab44f11` is
the sole commit on `…-ticker-pipeline-t2huls-integrator`.

---

## What this pass did not do

No branch or tag was deleted. No `git worktree remove` was run. `signals/` and
`notebooks/` were not read or written. No file under `kraken_trading_bot/`, `tests/`,
`configs/` or the root `justfile` was modified in the final state of the tree — the only
file this pass adds is this one.

One run was discarded and re-run rather than reported, per §8.2: a containment table
came back with 2 of 14 rows after a zsh glob failure had truncated a loop. The cause was
not the glob but a concurrent ref deletion by another actor; the re-run against the
post-deletion ref set is what is reported, and the pre-deletion table is the one above.