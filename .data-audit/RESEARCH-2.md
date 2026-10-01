# RESEARCH-2 — Rank 2: data quality / reliability (until-gap, provenance, pages=6, no scheduler, paper-trade refetch)

Researcher: researcher2, team `audit-pipeline`, pass of 2026-10-01 (tree
`ca360a8`, working tree clean). **Read-only pass.** This file **overwrites**
the prior-pass `RESEARCH-2.md`, which was the G2 feature-width guard — a
different gap, now superseded; this pass's AUDIT ranks the G2 content under
Rank 4 and cites it in §5-Rank2 as the width-guard seam. AUDIT.md §5 Rank 2
(lines 336-391) + §2.4 (lines 148-162) were read in full before any code; so
were RESEARCH.md, RESEARCH-1.md and the prior RESEARCH-2.md (the latter two
specced the config half and the width guard respectively — this file extends
both rather than re-deriving them).

Gap in scope: **AUDIT.md §5 Rank 2** — "thin depth + dropped `until` + no
recorded window + no scheduler + per-tick refetch" (IMPROVE-EXISTING,
data-quality). Outcome type: IMPROVE-EXISTING.

All line numbers are against commit `ca360a8`.

---

## 1. until-gap spec

### 1.1 Root cause (verified, both layers)

1. **`fetch_ohlc_dataframe` has no `until` parameter at all.**
   Signature at `data.py:706-717`:
   `(pair, interval, pages=6, manager, since, extra_features_file,
   funding_features_file, social_features_file, signal_max_age_hours,
   signal_require_ticker)`. There is `since` and no `until`.
2. **`read_ohlc_dataframe` accepts `until` (`data.py:797`) but the default
   (`market_data_store: null`) branch does not even forward it.** At
   `data.py:875-887` the live branch calls `fetch_ohlc_dataframe(..., since=since,
   ...)` — `until` is absent from the call. So on the default path `until` is
   silently dropped twice: port of call-point and function signature. Even if
   someone "fixed" the forwarding, it would crash with a TypeError the moment
   `until is not None` because the callee has no such parameter.
3. **The store branch already honours it.** At `data.py:901-931` the fetch→
   upsert→read branch passes `since=since, until=until` into `store.read`
   (`data.py:929`), whose semantics are since-inclusive / until-exclusive on
   bar-bucket starts (`kraken-market-data/market_data/store.py:147-185`, with
   the `>= since` / `< until` filters at `store.py:181-184`). The store branch
   is *correct*; only the live branch is wrong.
4. The admitted gap sits in `read_ohlc_dataframe`'s own docstring
   (`data.py:869-873`): *".. todo:: honour since/until from config"*.
5. **Zero callers pass `since=`/`until=` anywhere.** Verified by grep:
   `grep -rn "since=\|until=" kraken_trading_bot/` outside `rl/data.py` returns
   nothing but a stale `.pyc`. `train.py:185-196`, `backtest.py:138-149`,
   `export.py:166-177`, `paper_trade.py:289-300` all omit both. RESEARCH-1 (the
   pass that specced "the whole config half") confirmed the same at its own
   line numbers; the seam was reworked by `5951f72`/`1e404ba` since, moving the
   sites, and the gap moved with them.

### 1.2 What the wrapper passes (verified in kraken-python)

- `KrakenManager.ohlc(pair, interval=, since=)` → `client.ohlc` →
  `transport.public("OHLC", params={"pair", "interval", "since"})`
  (`kraken_api/manager.py:163-183`, `kraken_api/client.py:99-114`).
- **Kraken's public OHLC has no end/until parameter** — the only selects are
  `pair`, `interval`, `since`. So an upper bound **cannot be expressed
  server-side on the live path; it must be enforced client-side.**
- Cursor paging: the response carries `"last": <ts>`; the convention (already
  implemented in `_page_candles`, `data.py:662-703`) is that the next page
  passes `since=last` (`manager.py:183` documents "last is the timestamp to
  pass back as since for the next page"), and the loop stops after `pages`
  pages, when `last == 0`, or when a page is empty. Candles come back ascending
  by `time`; `candles_to_dataframe` re-sorts and dedups anyway
  (`data.py:656,777-778`).

### 1.3 Minimal fix

**Single change to `_page_candles`** (`data.py:662-703`) — add
`until: int | None = None` and stop following the cursor once the page has
crossed the bound. Because candles are ascending within and across pages, the
stop is: keep the batch (trim later) but do not page further:

```python
    for _ in range(pages):
        batch, last = manager.ohlc(pair, interval=interval, since=cursor)
        if batch:
            collected.extend(batch)
        if last == 0 or not batch:
            break
        if until is not None and int(batch[0].time) >= until:
            _LOGGER.debug("OHLC reached until bound %s at cursor %s", until, cursor)
            break
        cursor = last
```

`batch[0].time >= until` is the correct predicate: the first candle of a page
already at/after `until` means every later page is too. A page straddling the
bound is kept and trimmed by the caller, so the closed-bar set is not lost to
an off-by-one.

**Then `fetch_ohlc_dataframe`** (`data.py:706-717`): add `until: int | None =
None` to the signature, pass it to `_page_candles`, and trim the returned df
when set — the live path's df *is* the window, unlike the store path where the
read leg filters:

```python
    collected = _page_candles(pair, interval, pages, manager, since, until)
    ...
    df = candles_to_dataframe(collected)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    if until is not None:
        df = df[df.index < pd.to_datetime(until, unit="s", utc=True)]
```

`candles_to_dataframe` builds the index with `pd.to_datetime(time, unit="s",
utc=True)` (`data.py:657`), so the trim is an index comparison — same
exclusive `<` semantics as `store.read` (`store.py:184`), by construction.

**Then `read_ohlc_dataframe`**'s live branch (`data.py:875-887`): add
`until=until` to the `fetch_ohlc_dataframe` call. That is the whole live-path
fix — three sites, ~8 LOC, and one test (see §6).

The store branch needs only the `_page_candles` `until` pass-through if we want
the append leg to stop at the bound too (optional; correctness is unaffected
because `store.read` already filters). Recommended: pass it for symmetry —
`_page_candles(pair, interval, pages, source, since, until)` at `data.py:912`.

### 1.4 Config surface

RESEARCH-1 already specced exactly this; confirmed still valid and un-built:

```yaml
# configs/default.yaml, immediately after market_data_store (line 125)
data_window:
  since: null          # epoch seconds, INCLUSIVE on bar-bucket starts
  until: null          # epoch seconds, EXCLUSIVE on bar-bucket starts
```

Accessor (constants-friendly, tolerates flat keys), one helper, four callers:

```python
def window_bounds(cfg: dict) -> tuple[int | None, int | None]:
    w = cfg.get("data_window") or {}
    return (w.get("since"), w.get("until"))
```

Caller matrix (all four already call `read_ohlc_dataframe` and already pass
`market_data_store=`):

| caller | change |
|---|---|
| `train.py:185-196` | add `since, until = window_bounds(cfg)` |
| `backtest.py:138-149` | add since/until from `record.config` — **this is what makes a backtest replay the model's recorded window** |
| `export.py:166-177` | add since/until from `cfg` — export dumps exactly the frame training saw |
| `paper_trade.py:289-300` | **deliberately NOT since** (wants the fresh tail). Optionally honour `until` as a "paper must not see past this" cap, default off. Comment it. |

### 1.5 Semantics: since only vs both set

- **`until` is a no-op without `since` on the live path.** Kraken pages forward
  from a `since` cursor; it has no way to say "scan backwards to `until`". With
  `since=None` the live fetch returns the newest `pages`×720 bars regardless of
  `until`, so a lone `until` trims an arbitrary tail — meaningless. On the
  **store** path `until` alone is meaningful (the store has total history). So
  the documented contract is: *live path honours `until` only in combination
  with `since`; store path honours each independently.* This is worth a
  docstring sentence both in `configs/default.yaml` and at
  `fetch_ohlc_dataframe`, or the next implementer will "fix" the live path to
  do something with a lone `until`.
- **`since` only → go live, no upper bound.** The live fetch pages from `since`
  until `pages` runs out or history is exhausted (`last == 0`); the store read
  returns `[since, ∞)`. This is the "train from 2018 forward" mode RESEARCH-1's
  seed runs used.
- **both set → bounded, reproducible window.** Fetch/paging stops at `until`
  (live) or the read filters on it (store). RESEARCH-1's note applies: `since`
  also moves the fetch *cursor*, so on the store leg a distant `since` makes
  the append leg page from the old bound (wasteful but correct); the clean
  follow-on is passing `since=None` to `_page_candles` on the store leg and
  keeping it on the read.
- **`prepare_episode` slices again (§`train.py:204-209`)** — the effective
  fitted window is `episode_df = prepare_episode(df, ...).tail(episode_bars)`,
  so provenance must record the **post-slice** bounds, never the request (§2).

---

## 2. Provenance fields & staleness surfacing

### 2.1 What registry.py writes today

`register_model` (`registry.py:121-152`) writes `config.yaml` as `yaml.safe_dump(cfg)`
of whatever dict it is handed — so provenance is *"mutate `cfg` (or a copy)
before the `register_model` call"*, with **no registry schema change**;
`scan_model` reloads the same dict at every consumer (`backtest.py:154`,
`paper_trade.py` config load, `registry.scan_model`). `cli.py models` surfaces
`config_summary()` (`registry.py:61-78`), which today returns **exactly six**
keys: `ticker, model_name, action_space, reward_mode, feature_windows,
feature_groups`. Confirmed absent: `n_features`, any window/bar-span, any hash.

The training write point is `train.py:238-242`:

```python
    features.save_normalization(ticker_key, agent.save_dir / "normalization.npz")  # 240
    record = register_model(ticker_id, model_name, cfg, root=models_root)          # 242
```

`cfg` here is the merged config dict returned by `build_train_config`
(`train.py:178`); `episode_df` (the post-slice fitted frame) is in scope at 204.
`env.observation_space.shape[0]` is in scope at 211-221. Everything needed for
full provenance is present at one seam.

### 2.2 Fields to add

Two prior research passes each spec'd half of this and neither overlapped;
the combined block is the recommended shape, all written between
`prepare_episode` (train.py:204-209) and `register_model` (train.py:242):

```python
    # provenance: the window and width this policy was actually fitted on
    win = episode_df.index  # UTC DatetimeIndex == bar-bucket starts
    cfg["data_window"] = {
        "since": cfg.get("data_window", {}).get("since"),
        "until": cfg.get("data_window", {}).get("until"),
        "actual_start": int(win[0].timestamp()),
        "actual_end": int(win[-1].timestamp()) + int(cfg.get("ohlcv_interval_minutes", 60)) * 60,
        "n_bars": int(len(win)),
        "interval_minutes": int(cfg.get("ohlcv_interval_minutes", 60)),
        "store": cfg.get("market_data_store"),
        "source": "store" if cfg.get("market_data_store") else "kraken_rest_live",
        "data_hash": _frame_hash(episode_df),   # see RESEARCH-1 §4: pd.util.hash_pandas_object, sha256[:16]
    }
    cfg["n_features"] = int(env.observation_space.shape[0])   # RESEARCH-2-prior Layer 2
    cfg["feature_fingerprint"] = _feature_fingerprint(cfg)    # RESEARCH-2-prior Layer 3
```

Field rationale (merging the prior specs, with the deltas called out):

- **`data_window.{actual_start, actual_end, n_bars, interval_minutes}`** — from
  RESEARCH-1 §4. `actual_end` is made end-exclusive so the recorded window
  round-trips straight into `read_ohlc_dataframe(until=...)`. Record the
  **post-slice** frame's bounds — the audit's whole point is "the bars this
  policy saw", and `prepare_episode` may have tailed the fetch.
- **`data_window.store` / `.source`** — a live-fetched model is *not*
  reproducible even with bounds (the window moved with the market); `source`
  states it. RESEARCH-1 §4.
- **`data_window.data_hash`** — sha256 over `pd.util.hash_pandas_object` of the
  OHLCV columns (stdlib-only, already-provable dep; RESEARCH-1 §4 has the
  implementation). Change-detector, not identity — documented so.
- **`n_features`** — from prior RESEARCH-2 (Layer 2): use
  `env.observation_space.shape[0]`, the width the *policy* was trained against,
  **not** `pipeline.n_features()`, which is stateful against the last compute.
  `models/` today holds six untrained-scratch models; every shipped config has
  `market_data_store: null` and all signal keys `null`, so all future models
  will be 49-wide until a store is wired.
- **`feature_fingerprint`** — from prior RESEARCH-2 (Layer 3): canonical-JSON
  sha256 over `feature_windows`, `feature_groups`, the indicator params,
  `ohlcv_interval_minutes`, the three `*_features_file` paths, **and the code
  constant `_SIGNAL_COLUMNS`** — that last line is what makes a *code* drift
  (someone widening the allow-list, AUDIT §5-Rank-1) visible in provenance.
  Without it a config-only fingerprint is blind to the most likely drift.

### 2.3 Why `cli.py models` is the right staleness surface

`config_summary()` feeds both the table (`cli.py:577-593`) and `--json`
(`cli.py:551-574`, which dumps `config_summary` verbatim). Minimal:
`"n_features": self.config.get("n_features")` plus a nested
`"data_window": {k: self.config["data_window"].get(k) for k in ("actual_start",
"actual_end", "n_bars", "source")}` — then the marching `n_features` column
appears for free with no CLI change in `--json`, and a one-line table-column
addition (`Feat`) in the plain view. That directly answers "which models are
stale": a model whose recorded `n_features` differs from the *current*
pipeline width is stale — and the width guard (prior RESEARCH-2 §1 Layer 1/2,
still open, Rank 4) is what detects that at load. No new dependency; `npz`
`feature_names` is already round-trip-tested (`features.py:78,101-106`).

---

## 3. pages=6 centralisation

Confirmed literal sites (grep this pass):

| site | current |
|---|---|
| `rl/data.py:709` | `pages: int = 6,` (fetch_ohlc_dataframe) |
| `rl/data.py:794` | `pages: int = 6,` (read_ohlc_dataframe) |
| `rl/train.py:133` | `pages: int = 6` (train_ticker) |
| `rl/backtest.py:91` | `pages: int = 6` (backtest_model) |
| `rl/export.py:128` | `pages: int = 6` (build_export_frame) |
| `cli.py:156, 231, 290` | argparse `default=6` for train/backtest/export-data (derives from the above) |
| `rl/paper_trade.py:56` | `_FETCH_PAGES = 2` — **deliberately different, do not merge** |

Fix: one module constant, imported everywhere the default is spelled:

```python
# rl/data.py, next to _page_candles
DEFAULT_OHLC_PAGES = 6
PAPER_FETCH_PAGES = 2   # the live-tail exception; document the relationship
```

`train.py:133` / `backtest.py:91` / `export.py:128` become
`pages: int = DEFAULT_OHLC_PAGES`; the `cli.py` argparse defaults read
`DEFAULT_OHLC_PAGES` (import from `rl.data`, or `rl/__init__`). `paper_trade`
keeps its own `_FETCH_PAGES = 2` with a one-line comment that it is the
deliberate exception (fresh tail, not history). Same shape as the already-open
A5 drift (the 5th spelling of `_FEATURE_GROUPS`, `export.py:73-79`) — worth
fixing both in one pass so the "spell once" precedent is visible.

---

## 4. Scheduler options — ranked, with concrete shapes

### 4.1 Current state (verified)

- `nix/module.nix` ships exactly one unit:
  `systemd.services."kraken-trading-bot-env"` (`module.nix:220-237`) — an
  EnvironmentFile writer (`Type=oneshot; RemainAfterExit`). It runs at boot and
  does nothing periodic. **Zero timers, zero fetchers.**
- `flake.nix:4-8` declares three inputs — `nixpkgs`, `kraken-python`, and
  `kraken-market-data` (the only *signal* sibling). The three signal projects
  are **not inputs**, so `nix build`/`nix develop` cannot build or run
  `ticker-news-signals`, `kraken-funding-rates`, or `kraken-social-signals`.
- Sibling `nix/` dirs, on disk this pass:
  - `kraken-market-data/nix/module.nix` — the ONLY sibling timer. Per-(pair,
    interval) `systemd.services."kraken-market-data-{P}_{I}"` + matching
    `systemd.timers`, `OnCalendar="*-*-* *:*:30"`, `Persistent=true`,
    `DynamicUser=true`, `StateDirectory`. **Its ExecStart is broken**:
    `.../bin/kraken-market-data market update --pair … --interval … --store …`
    but the CLI registers `update` directly (no `market` group) — verified by
    RESEARCH-1 pass: `invalid choice: 'market'`. `nix/checks.nix` asserts unit
    *shape*, not argv, so `nix flake check` stays green.
  - `ticker-news-signals/nix/` — `default.nix` (package), `gnews.nix`,
    `vader-sentiment.nix`. Overlay-injected PyPI deps. **No module, no timer.**
  - `kraken-funding-rates/nix/` — **empty dir.**
  - `kraken-social-signals` — **no nix dir at all.**
- Documented cadence that exists only in prose:
  `ticker-news-signals/INTEGRATION.md:114-120` — "hourly cron (appends: the file
  is JSONL …)": `0 * * * * cd ~/Projects/ticker-news-signals && python cli.py
  pull --ticker ETH/USD --output signals/eth_usd.jsonl >> signals/eth_usd.jsonl`.

CLI shapes a scheduler must run (verified from each repo):

| sibling | command | cadence it feeds |
|---|---|---|
| kraken-market-data | `update --pair P --interval I --store root` (since-cursor poller, persists `_meta.json`) | the store path (`market_data_store`); RESEARCH-1 verified it has its own backoff + min-interval |
| ticker-news-signals | `pull --ticker P --output f` (one-shot; append via shell `>>`) | `extra_features_file` |
| kraken-funding-rates | `pull --pair P --output f [--append]` (has its own append flag) | `funding_features_file`; funding settles ~8-hourly (`configs/default.yaml` comment at :89) so hourly pulls are idempotent-by-key |
| kraken-social-signals | `pull --ticker P --output f` (one-shot) | `social_features_file` |

### 4.2 Options, ranked (cheapest → most robust)

**Option A — `systemd.user` timer + service pairs, this repo. (cheapest to ship today)**
Declare `systemd.user.services.*` + `systemd.user.timers.*` (NixOS supports
these in a module) — or, zero-Nix, plain units under
`~/.config/systemd/user/` with `loginctl enable-linger $USER`. One pair per
fetcher, ExecStart pointing at the checked-out sibling (their `.venv`s already
exist) or `nix develop --command …` for hermeticity. RESEARCH-1 §6 already
recommended exactly this for the store updater and gave the unit sketch:
`OnCalendar=*-*-* *:10,40:00`, `Persistent=true`, `Type=oneshot`.
- Cheap: no flake change, no sibling edit, store/signals roots stay user-owned
  (matches every config's `~/Projects/...` paths), journald catches structured
  JSONL-side logs.
- Drawback: absolute workspace paths (host-specific — but the config already
  does this), and units live outside the flake unless this repo's
  `nix/module.nix` gains a `systemd.user.*` section.

**Option B — add the 3 siblings as `git+https` flake inputs. (the buildability fix; prerequisite for C)**
The audit's own finding: three signal projects cannot be built at all today.
Add `git+https://github.com/Cairnstew/{ticker-news-signals,kraken-funding-rates,
kraken-social-signals}` — the run log (`.opencode/commands/audit-pipeline.md`
lines 533-536) is explicit that **private inputs need `git+https`, not
`github:`**, because the `github:` prefetcher cannot authenticate private repos
while `git+https` uses `~/.git-credentials`. Then `nix flake lock` verifies.
Closure cost: each sibling pins its own `nixpkgs`, so the lock grows three
nixpkgs revisions — acceptable, and the correct trade for *reproducible*
fetcher builds. This option alone does nothing to *schedule*; it makes the
packages referenceable as `${inputs.X.packages.${system}.default}/bin/...`.

**Option C — system-level `systemd.services` + `systemd.timers` in this repo's
`nix/module.nix`, ExecStart from the flake-pinned packages. (most robust)**
After B, `nix/module.nix` (or a new `nix/scheduler.nix` imported by it) closes
over the sibling inputs and declares one oneshot service + one
`OnCalendar`/`OnUnitActiveSec` timer per fetcher, with `User` set to the
operator and data dirs pointing at the user-owned roots (not `DynamicUser` +
`/var/lib`, which is the ownership story AUDIT/RESEARCH-1 flagged as the
sibling-module blocker):

```nix
systemd.services.kraken-ticker-news = {
  serviceConfig = { Type = "oneshot"; User = "seanc"; };
  # --append for funding; shell >> for news (idempotent by (ticker, hour) key)
  script = ''
    ${ticker-news-signals}/bin/ticker-news-signals pull --ticker ETH/USD \
      --output /home/seanc/Projects/kraken-trading-bot/signals/eth_usd.jsonl >> /home/seanc/Projects/kraken-trading-bot/signals/eth_usd.jsonl
  '';
};
systemd.timers.kraken-ticker-news.timerConfig = { OnCalendar = "*-*-* *:05:00"; Persistent = true; };
```

Cadence recommendation: news/social hourly (`*:05:00`), funding 3×/day at least
or hourly-with-`--append` (idempotent by key), market-data store update hourly
(`*:10,40:00` as RESEARCH-1) — the module's own `*:*:30` (every 30 s) exists to
feed sub-hour bars and is overkill at 60-min.

**Option D — fix + use the sibling modules directly (the eventual home, not today).**
`kraken-market-data`'s module is the ideal system-level home for the store
poller (per-(pair,interval) units, `Persistent`) once two upstream things land:
the `market` alias (or an ExecStart fix) so the unit actually parses, and an
ownership path (either `DynamicUser` + a `/var/lib` store root, or `User=` +
the `~/Projects` root). Writing parallel modules for the other three siblings
lives in their repos (three more upstream changes). Right answer for a later
pass; not this one.

**Ranking:** A (today, zero deps) → B (unblocks every fetched package) → C
(best long-term shape, depends on B) → D (upstream-parallel, later). A+B
together are honest one-afternoon work; A alone is the minimal *operational*
fix and is what RESEARCH-1 already recommended.

Note for implementers: the run log also records that `kraken-market-data` CLI
takes `MARKET_DATA_DIR=...`/`--store` **before** the subcommand in the sibling
invocations that were validated (`MARKET_DATA_DIR=... python cli.py update
--pair … `), while the sibling module's ExecStart passes `--store` after. Re-
verify flag ordering against the sibling CLI before writing a unit — it is
exactly the class of bug that ships green and dies on first fire.

---

## 5. Paper-trade bar-close gate pattern

### 5.1 What it does today (verified)

- `_FETCH_PAGES = 2` (`paper_trade.py:56`) → `_fetch_data()`
  (`paper_trade.py:280-300`) calls `read_ohlc_dataframe(pages=_FETCH_PAGES, ...)`
  **every** `step()`.
- `run_paper_trader` loop (`paper_trade.py:622-641`): `trader.step()` then
  `sleeper(interval)` (default 60 s). `iterations=None` → forever.
- `step()` (`paper_trade.py:345-382`): fetches if `df is None`, sets
  `self._last_close`, rebuilds the **entire** observation via
  `_build_observation` → `self.pipeline.compute(df)` (`paper_trade.py:320`) —
  which re-merges 3 signal JSONLs by re-entering `merge_extra_features`
  (`data.py:779-786` inside the read) — then `agent.predict`.
- So per hour on a 60-min bar: ~60 ticks × (2 OHLC pages + 3 O(file) JSONL
  parses + full-window feature compute + predict) with the observation's last
  row unchanged for the ~59 ticks inside one bar. The audit's 59×/hour is
  exact when fetches land mid-bar.

### 5.2 The gate: "did the last bar's timestamp advance?"

The invariant that makes the observation *change* is a **closed bar**: the
frame's last index value (`df.index[-1]`, bar-bucket start, UTC) only moves
when a new bar exists. The gate is therefore a comparison of that timestamp
between consecutive fetches — the pattern RESEARCH-3 already ranked as the
engine bar-close gate and AUDIT §5-Rank-2 names explicitly.

```python
    # in PaperTrader.__init__
    self._last_bar_ts: pd.Timestamp | None = None

    # in step(): after `df = self._fetch_data()` (or after the cache path)
    bar_ts = df.index[-1]
    if self._last_bar_ts is None or bar_ts != self._last_bar_ts:
        self._last_bar_ts = bar_ts          # a bar closed: recompute + predict
        self._last_close = float(df["close"].iloc[-1])
        obs = self._build_observation(df)
        action = self.agent.predict(obs, deterministic=True)
        self._ticks += 1
        signal = self._interpret_action(action)
        self._last_signal = signal
        self._execute_signal(signal)
        self._last_reply = {...}            # cache the reply for unchanged bars
    return self._last_reply                  # unchanged bar -> prior signal, no re-predict
```

Honest limits, stated plainly so the implementer does not oversell it:

1. **A true "stop polling Kraken" gate is impossible without a websocket** —
   the only way to learn a new bar exists is to fetch. What the gate actually
   buys is *not predicting sixty times on one bar* and *not re-merging the
   3 JSONLs 60×/hour*; it does not remove the per-tick REST call unless we also
   accept a stale last price for fills.
2. **Two sub-options**, both behaviour-preserving vs. behaviour-breaking:
   - *Predict gate (recommended minimum):* keep the per-tick fetch (paper needs
     a fresh fill price anyway) but recompute+predict+execute only on a bar
     close. Kills the redundant work; signal decisions still happen per bar.
   - *Fetch+parse gate (the one that also cuts REST):* cache the frame and skip
     `_fetch_data` entirely while `index[-1]` is unchanged, i.e. fetch only at
     bar boundaries. Cheaper, but the trade price (`df["close"].iloc[-1]`) then
     lags up to a bar. RESEARCH-3's seam #1 already parked the fix for the
     JSONL side whatever the choice: cache the parsed merge inputs keyed on
     `(path, size, mtime_ns)` so only files that changed are re-read.
3. The environment's row only changes on a bar close (`_raw_feature_array` is
   per-row), so predicting mid-bar is mathematically guaranteed to repeat the
   same policy output for the same `_last_bar_ts` — the gate is *provably*
   no-op-safe, which is the strongest justification to freeze a bar's decision
   and re-run only when a bar closes.

---

## 6. Recommended minimal set (for the planner)

Ranked by cost-to-value, this pass's evidence:

1. **`until` plumbing (the actual bug, §1).** `_page_candles(+until)`,
   `fetch_ohlc_dataframe(+until → page + trim)`, `read_ohlc_dataframe` live
   branch forwards it. ~8 LOC + one test asserting a bounded live fetch stops
   at the wall-clock bound; the store-branch `until` is already correct.
2. **Config surface + 4 caller hops (§1.4).** `data_window: {since, until}`
   block + `window_bounds(cfg)`, wired through train/backtest/export; paper
   deliberately ignores `since` (RESEARCH-1's spec, unchanged).
3. **Provenance (§2).** `data_window.{actual_start,actual_end,n_bars,
   interval_minutes,store,source,data_hash}` + `n_features` +
   `feature_fingerprint` written at `train.py:238-242`; one line each in
   `config_summary()`; a `Feat`/window column surfaced in `cli.py models`. This
   is the audit's own "training window is never recorded" item and gates every
   A/B conclusion (16 % fitted-std swing between fetches, per PLAN/DECISION).
4. **`DEFAULT_OHLC_PAGES = 6` constant (§3)** + `_FETCH_PAGES = 2` kept apart,
   folded into the same pass as the `_FEATURE_GROUPS` dedup so "spell once"
   lands as one precedent.
5. **Scheduler: Option A first (§4)** — four `systemd.user` timer+service
   pairs in this repo (store update, news, funding, social), then **Option B**
   (three `git+https` flake inputs) to make every fetcher buildable, then
   Option C to move the units to flake-pinned system services. A is an
   afternoon; B is the audit's buildability gap; C is the long-term shape.
6. **Paper gate (§5)** — `PaperTrader._last_bar_ts` freeze: recompute+predict
   only at bar close, cache the observation/JSONL parses between closes. Kills
   the 59×/hour redundant predict and the 3-parse-per-tick cost.
7. **Explicitly NOT in this slice:** retry/backoff in `kraken-python`
   transport (sibling boundary, Rank-3's F1-F3), the store-root ownership
   migration, the upstream sibling-module fixes (Option D), and the
   microstructure recorder (Rank 3).

Deliverables map: items 1-2 are the "until" gap, 3 is provenance, 4-6 the
reliability half — all IMPROVE-EXISTING, all in-repo except the flake inputs.

RESEARCH COMPLETE