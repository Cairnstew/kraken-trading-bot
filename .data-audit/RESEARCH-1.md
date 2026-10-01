# RESEARCH-1 — gap G1: the signal seam that ships with an unreadable path

**Pass:** 2026-10-02 · **Role:** Researcher 1 (research only; no source/config/test files modified)
**Target:** `kraken-trading-bot` @ `5aa9bd5` (branch `master`), plus four sibling repos read-only.

> **Two corrections to AUDIT.md's G1 that the architect must read first.**
> 1. The shipped path in `configs/default.yaml:79` is
>    `~/Projects/kraken-trading-bot/signals/eth_usd_funding.jsonl` — **not**
>    `~/Projects/kraken-market-data/signals/...` as the task brief states. The defect is
>    identical either way; only the spelling differs.
> 2. G1's central consequence claim — "12 allow-listed columns … are permanently zero" and
>    "`signal_observed == 0.0` on every bar" — is **measurably wrong in an important way**.
>    Measured: the observation is **52 columns wide**, the signal columns are **absent**, not
>    zero-filled, and `signal_observed` **is not in the observation at all**. See F4. The
>    corrected picture is *worse* for auditability and *different* for the fix.

---

## 0. Method

Read in full: `kraken_trading_bot/rl/{data,backtest,features,export,train,agent,registry,paper_trade}.py`
(the cited regions and every `Path(`/`expanduser` site), `configs/*.yaml`, `systemd/*`,
`nix/{module,default}.nix`, `justfile`, and the contract-assertion tests.

Ran, live, from a **non-repo CWD** (`/tmp/g1-probe`, so nothing depends on where you stand):
a real `merge_extra_features` call against a materialised real funding JSONL; a four-way
observation-width comparison; a verbatim replay of the existing test body against deliberately
broken config values; and `systemctl --user` / `ls` probes of the real filesystem.

Every file I touched lives under `/tmp/g1-probe/`. Nothing in the repo was written except this
file.

---

## 1. FINDINGS

### F1 — The defect is real, reproduced, and CWD-independent

`kraken_trading_bot/rl/data.py:363`

```python
path = Path(extra_features_file)          # :363  no expanduser()
if not path.is_file():                    # :364
    _LOGGER.warning(...)                  # :365-367
    return df                             # :368  <- silent skip
```

The same file, read both ways, through the real seam (`merge_extra_features` on a real
200-bar ETH/USD frame, real JSONL at the expanded path):

| spelling passed to the seam | columns the merge added |
|---|---|
| `~/Projects/.../eth_usd_funding.jsonl` (as shipped) | **`[]`** — nothing |
| `/home/seanc/Projects/.../eth_usd_funding.jsonl` (same file, expanded) | `['funding_rate_prediction','vol24h','bid','ask','signal_observed']` |

The only difference is the tilde. `yaml.safe_load` preserves `~`; `Path("~/x")` looks for a
directory literally named `~` under the CWD, which does not exist and is not `.resolve()`d.
This is not a CWD accident — it is wrong from **every** directory, including the repo root.

### F2 — It is a CLASS of three, not a one-off: three config-path sites omit `expanduser()`

I enumerated every place a config-supplied path becomes a `Path`.

| site | config key | `expanduser()`? | blast radius |
|---|---|---|---|
| `data.py:363` | `extra_features_file` / `funding_features_file` / `social_features_file` | **NO** | **highest** — 12+ columns, every consumer |
| `data.py:1054` | `market_data_store` | **NO** | **high** — silently downgrades deep history (G3) |
| `train.py:82` | `--config` for `train` | **NO** | **medium** — see F3 |
| `backtest.py:219` | `--config` for `backtest` | yes | correct reference |
| `agent.py:50` | `models_root` | yes | correct |
| `paper_trade.py:140` | `models_root` | yes | correct |
| `registry.py:131` | models root (`models_root()`) | yes | correct |
| `features.py:184`, `features.py:210` | normalization `.npz` | **NO** | low — built from `models_root` + join, so parent is already expanded |
| `export.py:250`, `export.py:263` | export CSV root | **NO** | low — relative default, user-supplied |
| `tools/model_matrix.py:434,450,1593` | spec / results / work_dir | yes | correct |

**`data.py:1054` is a second, currently-latent instance of the identical bug**, and the audit
missed it: `configs/deep-history.example.yaml:81` ships
`market_data_store: ~/Projects/kraken-market-data/store`, and `_resolve_store` does
`MarketDataStore(Path(market_data_store))` with no `expanduser()`. The moment anyone enables
deep history, **G3's fix silently fails the same way** — and worse, `_resolve_store` raises a
clear `ValueError` only when the *package* is missing, never when the path is wrong, so it
would go straight to `store.upsert` against a directory it creates relative to the CWD.

Note also that `tools/model_matrix.py` — the harness, not the library — is the one component
that gets this consistently right (three `expanduser()` sites), which is why the matrix runs
never tripped over it.

### F3 — The same class of bug produces an *opposite* asymmetry in `train` vs `backtest`

`train.load_train_config` (`train.py:80-88`) returns `{}` when the path is not a file.
`backtest._load_run_config` (`backtest.py:218-224`) raises `FileNotFoundError`. Both take a
CLI `--config` that is a **plain string** (`cli.py:149-152`, `:250-256` — no `type=Path`, so
argparse does not expand it either).

So `kraken-trading-bot train --config ~/my.yaml` silently trains on **CLI defaults with no
warning**, while `kraken-trading-bot backtest --config ~/my.yaml` refuses to run. Two paths,
one flag, opposite failure modes. The backtest behaviour is the correct one — its docstring
(`backtest.py:208-215`) says so explicitly: *"Silently running frictionless because of a typo
in the config path is precisely the failure the `fee_rate`/`slippage` echo on the result exists
to make visible — better to refuse the run."* That reasoning applies verbatim to G1 and was
not applied to the signal seam.

### F4 — CORRECTION: the failure mode is ABSENCE, not zero-fill — and `signal_observed` is itself absent

AUDIT.md G1 says the 12 columns are "permanently zero" and that `signal_observed == 0.0` on
every bar. Measured against the real pipeline with the real `configs/default.yaml`
(`feature_windows: [1,4,24]`, all five `feature_groups`):

| scenario | observation width | `_SIGNAL_COLUMNS` in the observation | `microstructure` |
|---|---|---|---|
| **A** all three keys `null` | **52** | 3 / 18 — only `vwap_dev`, `trade_count_zscore_20`, `volume_per_trade` | produces nothing |
| **B** configured, file missing (the shipped tilde path, as written) | **52** | 3 / 18 — identical to A | produces nothing |
| **C** configured, file present, 1 record | **59** | 10 / 18 | produces `spread` |
| **D** configured with a tilde, absent under that spelling | **52** | 3 / 18 | produces nothing |

Three consequences the architect needs:

1. **`B == A`, bit for bit.** A configured-but-unreadable seam is *indistinguishable from a
   `null` seam*. `data.py:368` returns `df` unchanged; the columns never reach `df.columns`;
   `_add_signals_features` (`features.py:604`, `if col in df.columns`) skips them. They are
   **absent from the observation**, not zero.
2. **`signal_observed` is not in the observation at all.** The freshness pair is written by the
   same seam (`data.py:487`, `_combine_freshness`), which the missing file skipped. So the
   mechanism designed to make absence *auditable inside the observation* is itself absent.
   The agent is not told "no reading behind this"; it is told nothing at all. That is a
   strictly worse diagnostic position than the audit describes, and it is why nothing
   downstream could have flagged it.
3. **The widths are 52 → 59, not 49 → 55.** The shipped default already carries the three
   OHLCV-derived scalars (`vwap_dev`, `trade_count_zscore_20`, `volume_per_trade` —
   `features.py:64-66`), which is the widening `features.py:270` calls "49 → 55". Activating
   the funding channel adds **7** columns: `basis`, `funding_rate`, `funding_rate_prediction`,
   `signal_age_hours`, `signal_observed`, `spread`, `vol24h` (measured set difference; note
   `bid`/`ask` reach the frame but are correctly withheld from the observation per
   `features.py:74-80`). Any doc, test or matrix cell that hardcodes 49 or 55 is wrong.

### F5 — The width guard CANNOT catch this, and the audit's reason for why is not the real one

`features.check_feature_width` (`features.py:262-310`) compares the model's `.npz`
`feature_names` against the live `compute()` columns **by name**, and is explicitly built to
be non-self-referential (`features.py:244-259` explains it replaced a tautological count check).

The audit's claim — "it checks width, not NaN, so it PASSES while the data is absent" — is
**half right and misses the point**. By-name comparison means the guard *would* fire on a
52-trained model replayed against a 59-wide pipeline. So the guard is a real protection against
the *asymmetry* (trained degraded, replayed live). It is **silent** on the thing we care about:
**nothing records what width the shipped config is *supposed* to produce.** There is no
"expected width for this config" anywhere, so a consistently-degraded 52 is indistinguishable
from a correct 52. And with `models/` empty but for `.gitkeep` (measured), no model exists, so
the guard is not even in the loop today.

### F6 — The test that *should* have caught it is provably vacuous

`tests/test_rl_signal_config_wiring.py:711-734`,
`test_config_points_at_a_real_funding_file_with_a_12h_bound`. Docstring: *"The activation that
keeps the three funding columns non-silent."* Full assertion body, replayed verbatim:

```python
assert funding, "funding_features_file must name a real path"   # :724  non-empty string
assert str(funding).endswith(".jsonl")                          # :725  suffix check
assert default_cfg["signal_max_age_hours"] == 12                # :726
assert deep_cfg["funding_features_file"]                        # :733
assert deep_cfg["signal_max_age_hours"] == 12                   # :734
```

Measured against four different values:

| `funding_features_file` | test verdict |
|---|---|
| `~/Projects/kraken-trading-bot/signals/eth_usd_funding.jsonl` (shipped, broken) | **PASSES** |
| `/definitely/not/here/nope.jsonl` | **PASSES** |
| `/root/nope.jsonl` | **PASSES** |
| `/tmp` (a directory, not a file) | fails — only because it lacks the `.jsonl` suffix |

It never calls `.expanduser()`, never calls `.is_file()`, never opens the file, and never calls
`merge_extra_features` with the configured value. It constrains the *string shape*, not the
*resolution*. Under the prior pass's hard requirement — a test that would pass without the fix
proves nothing — this test is **vacuous** and is the proximate reason the defect shipped.

Two further blind spots in the same family:
- `tests/test_model_matrix.py:1416-1418` asserts on the **verbatim text of the bug's own
  warning**: `"Extra features file not found: ... -- skipping signal merge"`, captured from a
  real run. It pins the buggy behaviour as expected. It will need updating in lockstep.
- `tools/model_matrix.py:494-500` records `base_config_present` for six keys, and
  `:1322-1330` only ever checks presence of `data_window`/`fee_rate`/`slippage`. The three
  signal keys are not in the presence list at all, and presence is not resolution.

### F7 — The producer already creates the directory; the consumer never needs to

- `kraken-funding-rates/kraken_funding_rates/export.py:140` and `:173` both
  `path.parent.mkdir(parents=True, exist_ok=True)` before writing. `write_jsonl` is the exact
  function behind `pull --output … --append` (`export.py:120-158`).
- `kraken-social-signals/kraken_social_signals/export.py:140,148` — same.
- **`ticker-news-signals/ticker_news_signals/export.py:120-127` does NOT** — `write_jsonl`
  opens for write with no `mkdir`.

Live filesystem state: `signals/` does not exist, but `.gitignore:65` ignores `signals/`, and
git cannot track an empty directory — so **its absence is the correct state of a clean
checkout**, not a defect. It is created by the first `funding-pull`. AUDIT.md G1 item 2
overstates this. The real gap in that item is the second half: `systemctl --user is-enabled
kraken-trading-bot-funding.timer` → **`not-found`** (confirmed live), `is-active` → `inactive`.

### F8 — NEW, LATENT, AND SEVERE: the funding channel cannot trip the ticker guard at all

`data._TICKER_FIELD = "ticker"` (`data.py:111`). `_filter_ticker` (`data.py:504-545`) falls
through to *"treat it as a one-ticker file and merge every record"* with a WARNING when the
field is absent (`data.py:537-545`).

The **real** funding record shape, taken from the producer's own `to_dict`
(`kraken-funding-rates/kraken_funding_rates/models.py:50-65`) and materialised live:

```
keys: ask, basis, bid, funding_rate, funding_rate_prediction, index_price,
      mark_price, open_interest, spot_pair, symbol, timestamp, vol24h
has 'ticker'? False
```

News and social **do** emit it (`ticker-news-signals/.../models.py:96`,
`kraken-social-signals/.../models.py:162`). **Funding alone does not.**

So the one channel that is actually configured (`default.yaml:79`, non-null) is the one channel
where `signal_require_ticker: true` (`default.yaml:118`) is inert. `configs/default.yaml:78`
explicitly invites a second pair to be configured — *"Point a second pair's model at its own
file, e.g. `signals/sol_usd_funding.jsonl`"* — and nothing in the code stops a SOL model being
pointed at the ETH file. ETH funding would merge onto SOL bars, silently, with only a WARNING
that most users will read once and never again. This is the exact class of defect
`signal_require_ticker` was written to prevent (`data.py:132-140`, `default.yaml:109-117`).

This is the one part of G1 whose root cause is genuinely on the **producer** side.

---

## 2. THE FULL SET OF PATH-RESOLUTION SITES (ranked)

| # | site | key | fix? | priority |
|---|---|---|---|---|
| 1 | `data.py:363` `merge_extra_features` | 3 signal keys | **no** | **P0 — G1** |
| 2 | `data.py:1054` `_resolve_store` | `market_data_store` | **no** | **P0 — same bug, G3 blocker** |
| 3 | `train.py:82` `load_train_config` | `--config` | **no** | **P1 — silent `{}`** |
| 4 | `export.py:250,263` | export CSV root | no | P2 — relative default |
| 5 | `features.py:184,210` | `.npz` | no | P3 — parent already expanded |
| 6 | `backtest.py:219` | `--config` | yes | reference implementation |
| 7 | `agent.py:50`, `paper_trade.py:140`, `registry.py:131` | `models_root` | yes | correct |
| 8 | `tools/model_matrix.py:434,450,1593` | spec/results/work_dir | yes | correct |

One shared helper covers 1, 2, 3, 4 and 5 and prevents the sixth instance tomorrow.

---

## 3. BEHAVIOUR WHEN A CONFIGURED FILE IS MISSING vs EMPTY vs STALE

Current behaviour, all three, is a silent `return df`:

| condition | site | current | problem |
|---|---|---|---|
| **missing** | `data.py:364-368` | WARNING + `return df` | looks like `null` (F4.1); nothing downstream distinguishes |
| **empty (0 records)** | `data.py:382-384` | **`_LOGGER.debug`** + `return df` | a DEBUG-level line, invisible at default log level, and again identical to `null` |
| **present but no ticker overlap** | `data.py:477-486` | WARNING with a good message | correct — the only one of the three that is loud |
| **stale (records older than the bound)** | `data.py:457-459` | values zero-filled, `signal_observed=0` | correct by design; freshness pair is in the observation |

Note the **inconsistency inside the seam itself**: "no records at all" is DEBUG, "records but
none overlapping" is WARNING, "file absent" is WARNING. The two *uninformative* states are the
loud-ish one and the silent one, in the wrong order.

### Options, concretely

**Option 1 — fail loudly (raise) on a configured-but-unresolved path.**
`FileNotFoundError`/`SignalFileNotFoundError` when the key is non-null and the path does not
resolve. Matches `backtest.py:218-224` exactly, and matches the repo's own stated philosophy in
that docstring.

- *For*: the only option that makes this impossible to ship again silently; the failure surfaces
  at the first `train`/`backtest`, not three passes later; kills the `missing == null`
  indistinguishability permanently.
- *Against*: hard-fails a first-time user who copies `default.yaml`, has no sibling repos cloned
  yet, and just wants to train on price. Their first command exits non-zero. **This is the
  real cost and it is not small** — the task's own framing asks for "the right default for a
  *new* user".
- *Mitigation that makes it acceptable*: make it **opt-in-by-absence-of-intent**. `null` =
  off, silent. Non-null = **on, and the user has declared intent to use it**, so a
  non-resolving path is a configuration error, not a first-run state. The three null channels
  remain perfectly first-run-friendly; the one non-null channel fails loudly, which is exactly
  where the author *declared* they wanted data.

**Option 2 — warn-and-continue, loudly, and record it in the observation.** Keep `return df`,
promote `data.py:382-384` DEBUG→WARNING, and additionally write a *degradation* marker onto
the frame so the shortfall is machine-visible rather than log-visible.

- *For*: zero disruption to first-run; the information still lands somewhere a tool can read it.
- *Against*: this is the option that already exists in spirit and it demonstrably did not work
  — the current WARNING at `data.py:365` fires on **every** run of every model and has been
  ignored through at least two audit passes. **Evidence that "warn" is not a control here.**
  Adding a marker column is better, but it also widens the observation (see F4.3: every width
  is a hardcoded expectation somewhere), and a constant-zero marker column would itself be
  zero-filled by `_raw_feature_array` (`environment.py:487`) and z-scored to 0 — the marker
  would need to be a *feature* (a fitted stat) or it carries no information.

**Option 3 — lazily create the directory on read.**
- **Rejected on evidence, and this is a firm finding.** Both funding and social producers already
  `mkdir(parents=True)` (`export.py:140`; `export.py:140,148`), and the consumer's own `justfile:140,156`
  does `mkdir -p` before invoking them. A reader that creates a directory it does not write to
  would be creating an empty `signals/` that makes the *next* run's `is_file()` check pass a
  differently-shaped failure (present-but-empty → DEBUG) — i.e. it makes the diagnosis
  **worse**, not better. It also papers over a genuinely misspelled path.

**Option 4 (recommended) — Option 1's semantics + Option 2's diagnosis, layered.**

1. `null`/empty ⇒ off, silent. Unchanged. First-run UX preserved.
2. non-null but unresolvable ⇒ **raise**, naming the key, the raw value, and the expanded value
   (so the `~` bug is legible if it ever recurs), plus the fix (`just funding-pull` /
   `just funding-timer`).
3. non-null, resolves, but 0 records ⇒ raise too — a file that exists and is empty is a
   *producer* failure and must not read as `null`.
4. present with no overlap for this ticker ⇒ **keep today's WARNING** (`data.py:477-486`).
   This is the legitimately-expected case for a young forward-only log (G6) and must not be
   fatal.

---

## 4. THE `systemd/` TIMER GAP

**What exists** (`systemd/`, 3 files, one channel):

- `kraken-trading-bot-funding.service.in` — the template (`@PAIR@`, `@OUTPUT@`).
- `kraken-trading-bot-funding.service` — **`diff`-identical to a sed of the `.in` with
  `ETH/USD` and `/home/seanc/Projects/kraken-trading-bot/signals/…` substituted.** Verified by
  regenerating the substitution and diffing: empty diff. It was committed in `a39e184` as a
  *rendered, machine-specific artifact*, with `ExecStart=` (`service:39`) and `Documentation=`
  (`service:24`) hardcoding `/home/seanc/…`. A second user's checkout yields a unit that writes
  into a path they do not own. **This file should not be in git as a rendered artifact**; the
  template is the portable artifact.
- `kraken-trading-bot-funding.timer` — `OnCalendar=*-*-* *:17:00`, `Persistent=true`,
  `RandomizedDelaySec=120`. Sensible cadence, and the comment's justification (bid/ask and
  vol24h move between the ~8h settlements, so hourly keeps `spread` fresh) is correct.

**Installer:** `justfile:135-149` `funding-timer` — `mkdir -p` both the unit dir *and*
`$root/$(dirname output)`, `sed` the template, `ln -sf` the timer, `systemctl --user
daemon-reload`, `enable --now`. Correct. `justfile:152-158` `funding-pull` does the one-off.

**The doc cross-reference is false.** `configs/default.yaml:73-74` tells the reader the timer
is *"the `systemd.user` timer (kraken-trading-bot-funding.{service,timer}, **see nix/module.nix**)"*.
`nix/module.nix` is a **NixOS** module: its whole implementation block (`module.nix:212-238`)
sets exactly two things — `environment.systemPackages` and
`systemd.services."kraken-trading-bot-env"` (the credentials oneshot). It contains **no timer,
no `systemd.user.*`, and no reference to signals or funding**. The instruction sends a reader to
a file that does not contain what it promises.

**`systemd/user` vs `systemd/system` — and the trap.** The timer belongs in **`systemd/user`**,
because:
- the whole install path is user-scoped (`justfile:138` writes `$HOME/.config/systemd/user`);
- the pull is keyless and needs no secrets, so a system unit buys nothing;
- the output path is under `$HOME`;
- `loginctl enable-linger` (`service:21`) is the documented way to fire it logged out.

**Do NOT put it in `nix/module.nix`.** That module is a NixOS module
(`{ config, lib, pkgs, ... }:`, keyed off `config.services.kraken-trading-bot`). `systemd.user.*`
does not exist in a NixOS module's option namespace and will not evaluate. If it is ever to be
Nix-managed it must be a **`systemd.user.services`/`systemd.user.timers` Home-Manager module**,
which is a different module system entirely, and `nix/module.nix` cannot become one by
accumulation. Recommendation: leave the timer as `systemd/` + `justfile` (the install path that
already works) and **delete the false "see nix/module.nix" claim** from `default.yaml:73-74`.

**Covering all three channels** needs, per channel: one `.service.in`, one `.timer`, one
`just <ch>-timer` + `just <ch>-pull`, and one non-null config key. Two caveats that make this
more than copy-paste:
- `ticker-news-signals`' `write_jsonl` (`export.py:120-127`) does **not** `mkdir`, so the news
  recipe's `mkdir -p` is load-bearing, not decorative (F7).
- `ticker-news-signals` and `kraken-social-signals` expose `cli.py` at the repo root; funding
  does **not** (it is a console script, as `configs/default.yaml:60-65` correctly documents).
  The three `ExecStart` lines will not be uniform.
- News/social are `pull --ticker ETH/USD --output …` per `configs/default.yaml:53-54,83-84`,
  but neither `write_jsonl` takes an `append` flag the way funding's does — whether news/social
  are append-only logs or overwrite-per-pull changes their G6 story and **I could not determine
  this from a static read** (see §7).

---

## 5. VERIFICATION STRATEGY — and whether each candidate is non-vacuous

The prior pass's hard requirement: *a test that would pass without the fix proves nothing.*
Each candidate below is scored against that.

**(A) A test that a configured seam path resolves.** **NON-VACUOUS. Recommended.**
Force `monkeypatch.setenv("HOME", tmp)` (or `pathlib.Path.home` monkeypatch), set
`funding_features_file` to `"~/signals/f.jsonl"`, materialise the file under the fake home, and
assert the merge consumed it (e.g. `funding_rate_prediction` in the result, or
`signal_observed` present). Fails on `data.py:363` today; passes only with `expanduser()`.
*Non-vacuity proof: I replayed the existing test's body against four values and it passed for
`/definitely/not/here/nope.jsonl` — this candidate is the one that does not.*

**(B) A startup/lint check over `configs/*.yaml` that every referenced path resolves.** **NON-VACUOUS for the tilde class, VACUOUS for "file not populated yet."**
Catching `~` is mechanical: `assert "~" in v or Path(v).exists()`. But a *legitimately fresh
install* has `default.yaml:79` pointing at a not-yet-created file, so "every path exists" cannot
be a hard failure for `default.yaml` specifically. Recommended shape: **run it in
`tools/model_matrix.py plan` and in a pytest, not as a blocking CLI preflight** — report as a
`WARN` alongside the existing `base_config_missing_keys` warn (`model_matrix.py:1322-1330`),
which is the same class of check and already has the right severity. Extend
`base_config_present` (`model_matrix.py:494-500`) to the three signal keys *and* to resolution.

**(C) Making "the feature group is all zeros/empty" a visible failure.** **PARTIALLY
NON-VACUOUS — and the trap is precise.**
- The width guard as a mechanism: **non-vacuous for asymmetry, vacuous for degradation** (F5).
  It catches 52-vs-59; it cannot catch a consistently-52. Keep it, do not rely on it.
- A *constant-column* guard: **partially vacuous.** Under the shipped config the columns are
  **absent**, not zero (F4), so a "all-zero columns" check fires on nothing. It only becomes
  meaningful once (A)/(D) is fixed and a file *is* being merged but not overlapping. Worse,
  `NormalizationStats.normalize` deliberately floors zero std so constant features normalise to
  zero (`features.py:218-232`) — a "no constant columns" guard would **fail legitimately** on
  `signal_observed` for any young log. Scoring this as a trap: **do not add a constant-column
  guard without also requiring `signal_observed.any()`.**
- The single best non-vacuous variant: **assert `signal_observed.any()` when a seam file is
  configured.** Under the shipped config this fails (the column is absent); after the fix it
  passes iff real data landed. It is falsifiable in both directions.

**(D) A config-shape test (what exists today).** **VACUOUS.** Must be *replaced*, not amended —
see F6.

**(E) A round-trip test that a real producer record shape survives the seam.** **NON-VACUOUS,
and it would have caught F8.** Build a record from the *actual* `FundingSnapshot.to_dict()` key
set (asserted against `kraken-funding-rates`' model, not a hand-written fixture) and assert the
ticker guard fires on a cross-ticker file. Currently the funding channel silently merges
everything.

**(F) `test_model_matrix.py:1416-1418` must be updated in lockstep.** It pins the bug's own
warning text; leaving it would turn the fix into a red suite and invite a revert.

---

## 6. PRODUCER SIDE OR CONSUMER SIDE?

**Consumer side — this repo — for F1, F2, F3, F4 and the bulk of G1.** The producer is correct.
`kraken-funding-rates` writes a well-formed, hour-floored, `append`-only JSONL with the right
columns, and its `write_jsonl` already `mkdir`s the parent (`export.py:140`). It cannot fix the
fact that `kraken-trading-bot` chooses to spell a config path as `Path(x)` and then treat a
`False` `is_file()` as a normal outcome. A producer has no way to know its consumer's CWD, and
no way to make a consumer call `.expanduser()`. The blast radius is also entirely internal: the
audit's own evidence (`backtest.py:219` already does it) shows the correct pattern is present in
this repo and was simply not applied. **This is an internal inconsistency, not an
inter-project contract failure.**

**Producer side — `kraken-funding-rates` — for F8 only, and it should be fixed there.**
Its record omits `ticker` (`models.py:50-65`) while its two sibling producers emit it
(`ticker-news-signals/.../models.py:96`, `kraken-social-signals/.../models.py:162`). The
consumer documents a contract it cannot enforce on this channel (`default.yaml:109-117`) and
papers over the absence at `data.py:537-545`. The right fix is for funding to emit `ticker`
(`symbol` already carries the value, `models.py:40`), matching its siblings — the consumer's
documented contract is already correct and already tested
(`tests/test_rl_signal_config_wiring.py:226`). A consumer-side mitigation (accept `symbol` as an
alias) is defensible as a stopgap but is the wrong layer and should not be the permanent fix.

**Neither side:** the missing news/social timers and the un-installed funding timer are an
**operations** gap in this repo — the producer CLIs exist and are keyless.

---

## 7. RANKED RECOMMENDATION FOR G1

### R1 (P0, ~4 lines) — one shared path resolver, applied at all five sites

Add a single private helper in `kraken_trading_bot/rl/data.py` beside the other private
helpers (`_resolve_store` is at `:1020`; `_filter_ticker` at `:504`):

```
_resolve_config_path(value) -> Path | None
    None/"" -> None
    Path(str(value)).expanduser()
```

Apply at: **`data.py:363`** (G1), **`data.py:1054`** (`market_data_store` — the same bug, and a
silent blocker for G3), `train.py:82` (with `backtest.py:219`'s raise semantics), and
`export.py:250,263`. One helper makes the seventh instance a code-review question rather than an
audit finding.

### R2 (P0, at `merge_extra_features`, `data.py:360-368`) — distinguish "off" from "broken"

The gate becomes, in order:
- `null`/empty ⇒ `return df`, silently. **Unchanged** — first-run UX preserved.
- non-null but not a file ⇒ **raise** `SignalFileNotFoundError`, naming the config key, the raw
  value *and* the expanded value, and the producer command. This is the option that makes the
  defect impossible to ship silently again, and `backtest.py:218-224` is the in-repo precedent.
- resolves but 0 records (`data.py:382-384`) ⇒ **raise** as well, and promote that line from
  `DEBUG` to `WARNING` regardless. A present-but-empty file is a producer failure, not a
  first-run state.
- present with no overlap ⇒ keep today's WARNING (`data.py:477-486`) verbatim. Correct as-is;
  the forward-only log makes it expected (G6).

**Landing point, exactly:** the `if not extra_features_file:` / `path = Path(...)` /
`if not path.is_file():` block at `data.py:360-368`, plus the `if not records:` block at
`data.py:382-384`. **Config key: none added** — the change reads the existing
`extra_features_file` / `funding_features_file` / `social_features_file` keys, which
`data.py:853-860` and `data.py:1009-1016` already thread through both the live-fetch and
store-backed branches. Symmetry is preserved automatically because both branches call the same
function.

### R3 (P0, replaces the test) — make the vacuous test non-vacuous

Replace the body of `test_config_points_at_a_real_funding_file_with_a_12h_bound`
(`tests/test_rl_signal_config_wiring.py:711-734`) with the §5(A) test: fake `HOME`, a `~` path,
assert the seam *consumed* the file. Keep the `signal_max_age_hours == 12` assertions — they are
fine. Then, in the same file, add §5(E) (real `FundingSnapshot` key set + a cross-ticker
assertion).

### R4 (P1) — correct the record

`features.py:270`'s "49 → 55", `default.yaml:104-107`'s "301 of 721", and AUDIT.md G1's
"49-wide … twelve columns constant zero" are all wrong about the current default. Measured:
**52 as shipped, 59 with the funding channel live** (§F4). Any matrix cell, doc or test that
hardcodes 49/55 will need revisiting. Also delete the false `see nix/module.nix` cross-reference
at `default.yaml:73-74`.

### R5 (P1) — operations, not code

Enable the timer (`just funding-timer`, which already `mkdir -p`s the signals dir and the unit
dir — `justfile:140`). Delete the committed rendered `systemd/kraken-trading-bot-funding.service`
in favour of the `.in` template plus `just`. Add news + social `.service.in`/`.timer`/`just`
recipes, remembering `ticker-news-signals`' `write_jsonl` does not `mkdir`
(`export.py:120-127`) and that funding's CLI is a console script while the other two are
`cli.py`.

### R6 (P2, sibling repo) — add `ticker` to the funding record

`kraken-funding-rates/kraken_funding_rates/models.py:50-65` — add `"ticker": self.symbol` to
`to_dict()`. Matches both sibling producers and makes `signal_require_ticker` real for the only
channel that is currently configured.

**Sequencing note:** R1+R2 alone convert a silent 52-wide pipeline into a loud failure. That is
the whole of G1's risk. R3 is what stops it recurring. R5 is what makes it produce data. R6 is
orthogonal and belongs to another repo.

---

## 8. WHAT I COULD NOT DETERMINE

1. **Whether the news/social producers append or overwrite.** Funding's `write_jsonl` takes
   `append: bool` (`export.py:120-158`); news's `write_jsonl` (`export.py:120-127`) and social's
   (`export.py:120-148`) take no such flag. If news/social overwrite, their channels are
   *state*, not a *log*, which materially changes their G6 story and whether a stale-but-present
   file is possible. **Needs a live read of both CLIs' `pull` subcommands** — I did not execute
   them (they are network calls).
2. **Whether `kraken-funding-rates`' console script is reachable from a systemd `ExecStart`
   without the `nix run` wrapper** used at `service:39`. The unit hardcodes
   `/run/current-system/sw/bin/nix run /home/seanc/Projects/kraken-funding-rates#…`, which
   embeds this machine's store paths. Whether a second machine's `nix run` resolves is a live
   check.
3. **Whether `systemctl --user daemon-reload && enable --now` actually succeeds here.** I probed
   `is-enabled`/`is-active` (both `not-found`/`inactive`) and listed `~/.config/systemd/user/`
   (no kraken units present) but did **not** install the timer — that mutates the host's user
   systemd state and is outside a research role.
4. **Whether any model was ever trained with a working funding file.** `models/` contains only
   `.gitkeep`, so the width guard has never been exercised in this checkout and no trained
   artifact needs invalidating. I cannot speak to other machines' `models/` directories.
5. **The true `_SIGNAL_COLUMNS` width after *all three* channels activate.** My §F4 measurement
   used the real funding producer key set. News and social would add
   `sentiment_score`/`article_count`/`novelty_flag`/`stt_mention_count`/`stt_tilt`/`fng_index`,
   but I did not run their CLIs to confirm the exact emitted set — so 59 is a *measured*
   funding-only figure, not a ceiling.

I am confident in F1–F8 (all reproduced or read directly). I am **less** confident in the exact
49/55/52/59 arithmetic as a *documented* number, because it depends on `feature_windows` and
`feature_groups`; my 52/59 figures used the shipped `default.yaml` values
(`[1,4,24]`, all five groups) and should be re-derived if either changes.

---

RESEARCH COMPLETE
