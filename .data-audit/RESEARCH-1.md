# RESEARCH-1 — G1: training windows un-recorded + shipped store seam un-applied

Researcher: researcher1, team `audit-pipeline`. Pass of 2026-09-30 (this file
**overwrites** the prior-pass RESEARCH-1.md, which was about the normalization
candidate — different gap, different numbering; AUDIT.md §5 flags the
`.data-audit/` files as this pass's handoff artifacts).

Gap in scope: **AUDIT.md §3-G1 / §4 #1** — "Training windows are not recorded;
the shipped depth machinery is un-applied" (IMPROVE-EXISTING, data-quality).

**Read-only pass.** No code changed, nothing committed. Everything below is
grounded in files on disk: read `kraken_trading_bot/rl/{data,train,backtest,
paper_trade,export,registry,features,environment}.py`,
`configs/default.yaml`, `configs/deep-history.example.yaml`,
`tests/test_rl_data_store.py`, `flake.nix`, plus the sibling repos
`~/Projects/kraken-market-data` (README, INTEGRATION.md, `market_data/cli.py`,
`market_data/store.py`, `nix/module.nix`, `flake.nix`) and
`~/Projects/kraken-deep-history` (README, INTEGRATION.md,
`kraken_deep_history/cli.py`, `flake.nix`).

---

## 0. TL;DR

The whole gap is **three small edits plus a one-time seed**:

1. **One config block** — `data_window: {since, until}` in
   `configs/default.yaml` next to `market_data_store` (default.yaml:78-97),
   read by the same four callers that already read `market_data_store`.
2. **Four one-line call-site changes** — each caller passes
   `since=cfg.get("since"), until=cfg.get("until")` (or the nested
   `data_window.*`) into the `read_ohlc_dataframe` call it *already makes*.
   The parameter exists (`data.py:329-341`) and is already forwarded to
   `store.read` (`data.py:454`); only the config→caller hop is missing. The
   explicit todo sits at `data.py:396-401`.
3. **Provenance** — `train_ticker` writes `cfg` verbatim into
   `models/{T}/{N}/config.yaml` via `register_model` (`train.py:241`,
   `registry.py:145-152`). Add a `data_window:` block to `cfg` **after** the
   frame is read, carrying the *actual* window bounds + `n_bars` + an optional
   content hash. No registry schema change; every consumer
   (backtest/paper/export) already reloads `record.config`.
4. **One-time seed** — `kraken-deep-history seed` into a user-owned store
   root, in the bot's dev shell (market-data mode), ~8 commands in §5.

Scheduler: **`systemd.user` timer**, not the sibling's NixOS module *as it
ships today* — the module's `ExecStart` is verifiably broken
(`kraken-market-data market update` → `invalid choice: 'market'`) and its
`DynamicUser` + `StateDirectory` ownership fights a `~/Projects` store root
the bot config already points at. Details + evidence in §6.

---

## 1. What the seam already does (so the wiring is trivial)

`read_ohlc_dataframe(pair, interval, *, pages, manager, since, until, ...,
market_data_store, market_data_source)` (`data.py:329-341`):

- `market_data_store is None` → pass-through to `fetch_ohlc_dataframe`
  (`data.py:402-412`), byte-identical to today. `since` is forwarded as the
  *first page cursor* (`data.py:314` → `_page_candles(..., since)`,
  `data.py:218-259`).
- store set → `_resolve_store` (`data.py:463-501`) → **fetch → upsert → read**
  (`data.py:437-445`): pages Kraken with `pages` (bounded append leg), upserts,
  then `store.read(pair, interval, since=since, until=until)` (`data.py:454`) — so `pages`
  bounds only the *append*, while the *depth served* is the whole store.
  `since` inclusive / `until` exclusive, on bar-bucket starts
  (`store.py:147-185`).
- Provenance of the seam is already asserted by tests:
  `tests/test_rl_data_store.py:170-181` proves the `since`/`until` window, and
  `:148-168` proves fetch→upsert→read ordering.

So the missing hop is exactly what `data.py:396-401` says: *"honour
`since`/`until` from config"*.

---

## 2. Config keys

Add immediately after `market_data_store: null`
(`configs/default.yaml:97`), same block in
`configs/deep-history.example.yaml`:

```yaml
# --- Training window (optional; requires market_data_store) -------------
# Bounds for the OHLCV window this model is trained/evaluated on.
# `since`/`until` are epoch seconds (or an ISO date — the store CLI accepts
# both; keep epoch seconds in YAML so the recorded provenance is unambiguous).
# `since` is INCLUSIVE, `until` is EXCLUSIVE, both on bar-bucket starts.
# Leave both null for "the whole store" (deep-history default) or "whatever
# the live paged fetch returns" (no store). Set `until` to freeze a window for
# a reproducible retrain.
data_window:
  since: null          # e.g. 1514764800  (2018-01-01T00:00:00Z)
  until: null          # e.g. 1751328000  (2025-07-01T00:00:00Z)
```

Recommended accessor shape (one helper, four call sites, avoids two
spellings floating around):

```python
def window_bounds(cfg: dict) -> tuple[int | None, int | None]:
    """Read data_window.since/until, tolerating flat top-level keys."""
    w = cfg.get("data_window") or {}
    return (w.get("since"), w.get("until"))
```

Tolerating flat `since:`/`until:` too is a two-line courtesy and matches how
`extra_features_file` etc. are flat; pick **one** and document it — nested
`data_window` is preferred because it is the block that gets *written back*
into `config.yaml` as provenance (§4) and cannot collide with SB3/reward keys.

`market_data_store` itself needs no new key — it exists at
`configs/default.yaml:97` and in every caller already.

---

## 3. Caller changes (exact sites)

All four sites already call `read_ohlc_dataframe` and already pass
`market_data_store=`; the change is adding two kwargs.

| file:line (current) | call | change |
|---|---|---|
| `kraken_trading_bot/rl/train.py:185-194` | `read_ohlc_dataframe(pair, interval=…, pages=pages, manager=manager, …, market_data_store=cfg.get("market_data_store"))` | add `since=_since, until=_until` from `window_bounds(cfg)` |
| `kraken_trading_bot/rl/backtest.py:138-147` | same shape, `record.config` | add `since=…/until=…` from `record.config` — **this is what makes a backtest replay the model's recorded window instead of "whatever is newest"** |
| `kraken_trading_bot/rl/paper_trade.py:289-298` (`PaperTrader._fetch_data`) | `pages=_FETCH_PAGES` (2) | **do not** forward `since` — paper wants the fresh tail. It reads the model config, so it must *ignore* `data_window.since` here, or a 2018 window starves live trading. Add a comment saying so. (Optionally honour `until` as a "paper must not see past this" cap; default off.) |
| `kraken_trading_bot/rl/export.py:166-176` | `read_ohlc_dataframe(…)` | add `since/until` from `cfg` — export should dump exactly the frame training saw, so an exported CSV is comparable to the policy |

Two behavioural notes the implementer must not miss:

- **`since` also moves the *fetch* cursor.** In the store branch,
  `_page_candles(pair, interval, pages, source, since)` (`data.py:437`) starts
  paging from `since`, and `fetch_ohlc_dataframe` passes `since` the same way
  (`data.py:314`). With Kraken, `since` in the distant past returns the *first*
  720 bars after it, not recent ones (`store.py`/sibling README: "caps every
  response at ~720 recent bars *regardless* of `since`" — Kraken's behaviour
  here is that `since` selects the start of a ≤720-bar window). That is
  harmless (it upserts old Kraken-native bars, and the read leg still returns
  the full window) but it makes each fetch leg wasteful. The clean fix is to
  let the read-leg window and the fetch-leg cursor be separate: keep `since`
  on the read and pass `since=None` to `_page_candles` when a store is
  configured. If that is judged too invasive for the slice, note it as a
  follow-up — correctness is unaffected.
- **The effective window ≠ the requested window.** `prepare_episode`
  (`data.py:505-561`) slices the trailing `episode_bars` *after* the read, so
  the frame the environment actually trades is `df.tail(episode_bars)`.
  Provenance must record the **post-slice** bounds, not the request (§4).

---

## 4. Provenance: make a trained model reconstructible

`train_ticker` persists `cfg` verbatim (`train.py:241` →
`register_model` → `registry.py:145-152` `yaml.safe_dump`). So provenance is
"put a block in `cfg` after the frame exists, before `register_model`" — no
registry change, and backtest/paper/export already reload `record.config`
(`backtest.py:134`, `paper_trade.py:295`, `registry.scan_model`).

Insert between `prepare_episode` and the `register_model` call
(`train.py:241`):

```python
    # provenance: the window this policy was actually fitted on
    win = episode_df.index  # UTC DatetimeIndex == bar-bucket starts
    cfg["data_window"] = {
        "since": cfg.get("data_window", {}).get("since"),
        "until": cfg.get("data_window", {}).get("until"),
        "actual_start": int(win[0].timestamp()),
        "actual_end": int(win[-1].timestamp()) + interval * 60,  # end-exclusive
        "n_bars": int(len(win)),
        "interval_minutes": interval,
        "store": cfg.get("market_data_store"),
        "source": "store" if cfg.get("market_data_store") else "kraken_rest_live",
        "data_hash": _frame_hash(episode_df),   # optional, see below
    }
```

Field rationale:

- `actual_start` / `actual_end` / `n_bars` — **the** answer to "what bars did
  this model see". `actual_end` is made end-exclusive so it round-trips
  straight back into `read_ohlc_dataframe(until=…)`.
- `interval_minutes` — without it the bounds are meaningless.
- `source` — `store` vs live. A live-fetch model is *not* reproducible even
  with bounds, and the field should say so.
- `store` — the store root, so a replay resolves to the same bytes.
- `data_hash` — cheap, deterministic, stdlib-only:

```python
import hashlib
def _frame_hash(df) -> str:
    h = hashlib.sha256()
    h.update(pd.util.hash_pandas_object(df[["time","open","high","low","close","volume"]],
                                        index=False).values.tobytes())
    return h.hexdigest()[:16]
```

`pd.util.hash_pandas_object` is already available (pandas is a hard dep) and
is stable for a fixed dtypes/column set — which is why the column allow-list
is explicit. **Caveat to state in the docstring**: it is a *change-detector*,
not a provenance identity — re-reading the same store into a different month
slice with a different float parse would change it. That is precisely the
property wanted: "the fitted stats came from these exact values".

Follow-on the audit already flagged (out of this slice's scope, listed so the
implementer does not conflate): `n_features` pin (PLAN.md §4.1.1) and the
`normalization.npz` width guard — that is **G2**, a separate candidate.

---

## 5. Seed-and-refresh procedure (exact commands)

Prerequisites: the store root the bot config points at must be
**market-data mode** (parquet + `_meta.json`), i.e. `market_data` *and*
pyarrow importable — `kraken-deep-history` README §"Market-data mode is the
bot-facing mode" is explicit that fallback-CSV mode yields **0 bars** to the
bot. The bot's own dev shell satisfies this (`flake.nix:58-64` puts pandas,
pyarrow, requests on PATH and `market_data` on `PYTHONPATH`).

### 5a. One-time seed (years of history, Binance archive, keyless)

```bash
# Dry run first — prints the monthly ZIP URLs, no network.
nix develop ~/Projects/kraken-trading-bot --command bash -c \
  'PYTHONPATH=$HOME/Projects/kraken-deep-history:$PYTHONPATH \
   python ~/Projects/kraken-deep-history/cli.py plan \
     --ticker ETH/USD --interval 60 --from 2018-01-01'

# Seed. --store is the root the bot's config will name.
nix develop ~/Projects/kraken-trading-bot --command bash -c \
  'PYTHONPATH=$HOME/Projects/kraken-deep-history:$PYTHONPATH \
   python ~/Projects/kraken-deep-history/cli.py seed \
     --ticker ETH/USD --interval 60 --from 2018-01-01 \
     --store $HOME/Projects/kraken-market-data/store'

# Prove it landed in *market-data* mode: report.store_mode must be
# "market-data" (not "fallback-csv"), and files must be *.parquet.
ls $HOME/Projects/kraken-market-data/store/ETH_USD/60/ | head
ls $HOME/Projects/kraken-market-data/store/_meta.json

# Gap-scan + per-source summary (exit 1 = holes).
nix develop ~/Projects/kraken-trading-bot --command bash -c \
  'PYTHONPATH=$HOME/Projects/kraken-deep-history:$PYTHONPATH \
   python ~/Projects/kraken-deep-history/cli.py verify \
     --ticker ETH/USD --interval 60 --store $HOME/Projects/kraken-market-data/store'
nix develop ~/Projects/kraken-trading-bot --command bash -c \
  'python ~/Projects/kraken-market-data/cli.py stats --store $HOME/Projects/kraken-market-data/store'
```

Mapped tickers (seeder refuses unknowns — `kraken_deep_history/utils.py`
`TICKER_SYMBOL_MAP`): `ETH/USD`, `BTC/USD`, `SOL/USD`, `XRP/USD` (*USDT spot
family). `DOGE/USD` is **not** mapped. Intervals: 1/5/15/30/60/240/1440/10080.

### 5b. Forward refresh (only if you want bars *newer* than the last seed)

```bash
cd ~/Projects/kraken-market-data
nix develop --command python cli.py update --pair ETH/USD --interval 60 \
  --store ~/Projects/kraken-market-data/store
```

`update` is the since-cursor poller: appends incremental bars and persists the
cursor to `_meta.json` (`store.py:257-296`, `market_data/cli.py:63-68`). It
also honours `--min-interval` (default env/`0.5`s) and exponential backoff —
this is the *good* transport; the bot's `kraken-python` transport is the
un-backed-off one (AUDIT G3). A few hourly runs close the gap; a timer makes it
automatic (§6).

Note: with a store configured, `paper_trade` already appends on every tick
(`data.py:437-445`), so the forward-refresh timer is only needed when
paper-trading is *not* running.

### 5c. Turn it on for a model

```bash
cp configs/deep-history.example.yaml models/ETH_USD/ppo_eth_01/config.yaml
# then edit: market_data_store: ~/Projects/kraken-market-data/store
#            data_window: {since: 1514764800, until: null}
```

Verify the depth actually changed before trusting it:

```bash
python -c "
from kraken_trading_bot.rl.data import read_ohlc_dataframe
df = read_ohlc_dataframe('ETH/USD', 60, pages=6,
                         market_data_store='$HOME/Projects/kraken-market-data/store')
print(len(df), df.index[0], df.index[-1])"
```

Expect tens of thousands of bars, not 4320. If it prints ~720 or 0, the store
is in fallback-CSV mode or the root is wrong.

---

## 6. Scheduler recommendation — `systemd.user` timer (ONE)

**Recommend: a `systemd.user` timer** (in the user's nixos-config, `linger`
enabled) running §5b's `update` command against the `~/Projects` store root.

Why not the sibling's NixOS module (`services.kraken-market-data`,
`kraken-market-data/nix/module.nix`), which is otherwise the obvious answer —
two verified blockers:

1. **Its `ExecStart` is broken.** `nix/module.nix` builds
   `${cfg.package}/bin/kraken-market-data market update --pair … --interval … --store …`,
   but `market_data/cli.py:61-101` registers the subcommands *directly*
   (`update`, `backfill`, `verify`, `stats`, `list`, `extract`, `version`) — there
   is no `market` group. Verified by running it:
   ```
   $ python cli.py market update --pair ETH/USD --interval 60 --store /tmp/x
   kraken-market-data: error: argument command: invalid choice: 'market'
     (choose from 'update', 'backfill', 'verify', 'stats', 'list', 'extract', 'version')
   ```
   The unit fails at every fire; `nix/checks.nix` asserts unit *shape*, not that
   the packaged CLI parses that argv, so `nix flake check` stays green.
2. **Ownership mismatch.** The module hard-codes
   `DynamicUser = true` + `StateDirectory = "kraken-market-data"`
   (`nix/module.nix` `serviceConfig`), i.e. a dynamically-uid-owned
   `/var/lib/kraken-market-data`. The bot runs as a normal user and reads a
   `~/Projects/kraken-market-data/store` root today (that is what every config
   comment and `configs/deep-history.example.yaml:12-14` point at). System-level
   state means either `User =`/`DynamicUser = false` + group permissions, or the
   bot config moves to `/var/lib` — extra moving parts for zero benefit at this
   scale.

Why `systemd.user` wins:

- Same properties that make the module attractive, without either blocker:
  `Persistent = true` (catches up after suspend), declarative
  `OnCalendar`, per-pair services if wanted, journald receives the store's
  structured JSON logs (`market_data/logging_config.py`).
- The store root stays user-owned, so the bot reads it with no permission
  story and no `/var/lib` migration.
- Zero edits to the sibling repo — which matters because this slice is scoped
  to the bot.
- `loginctl enable-linger $USER` is the only extra step versus a system unit.

Sketch (user-level, `~/.config/systemd/user/kraken-market-data.service` +
`.timer`; `OnCalendar=*-*-* *:10,40:00` — hourly is plenty at 1 h bars; the
module's every-30-min default is tuned for sub-hour bars):

```ini
# .timer
[Timer]
OnCalendar=*-*-* *:10,40:00
Persistent=true
[Install]
WantedBy=timers.target

# .service
[Service]
Type=oneshot
ExecStart=/home/seanc/Projects/kraken-market-data/.venv/bin/python \
  /home/seanc/Projects/kraken-market-data/cli.py update \
  --pair ETH/USD --interval 60 --store /home/seanc/Projects/kraken-market-data/store
```

Using the checked-out `.venv` (the sibling has one) avoids a `nix develop`
eval/build check on every fire; the store's flake also works
(`nix develop --command python cli.py update …`) if a hermetic path is
preferred.

**Rejected alternatives for the scheduler:**

- **Plain cron** — same command, but no `Persistent` catch-up, coarser logging,
  and it will not be picked up by the host config's rebuilds. Strictly worse
  than `systemd.user` here.
- **`nix develop --command …` on a timer** — correct but re-enters the flake
  eval/store-path machinery every fire for a command whose deps are already
  realised in a local venv.
- **The NixOS module (fixed)** — the right *eventual* home (system-level state,
  `DynamicUser` for a keyless poller, per-(pair, interval) units). Worth a
  two-line upstream fix (`market_data/cli.py` — add a `market` alias group, or
  fix `ExecStart`) plus an ownership story, but it belongs to the sibling repo
  and to a later pass, not to G1.
- **No timer at all** — legitimately correct for the *first* model: a seeded
  store plus a fixed `data_window.since`/`until` makes training reproducible
  without any scheduler. The timer only buys *fresh* bars. If the slice is
  time-boxed, ship the config+provenance wiring and the one-time seed, and file
  the timer as the operational follow-up.

---

## 7. Library / pattern survey (the "research-grade data" part of G1)

Scored on **reduction cost**: how much code stands between the candidate and
the thing the RL pipeline consumes — a pandas frame of per-(ticker,
timestamp) bars with columns `time, open, high, low, close, vwap, volume,
count`, UTC `DatetimeIndex`, sliced by `since`/`until`.

| candidate | version / last release | license | auth | rate limits | output shape | reduction to per-ticker bars | verdict |
|---|---|---|---|---|---|---|---|
| **duckdb** | 1.5.6 (PyPI 2026-09-28); nixpkgs-unstable 1.5.5 | MIT | none | n/a (local files) | SQL over `read_parquet('store/*/*/*.parquet')` | **High for inspection, zero for training reads** — one query replaces the month-glob loop in `store.py:147-185`, ideal for `verify`/gap-scan and window previews at store scale | **Optional, defer.** The sibling README already names it as the deliberately-not-v1 verification layer. Do not put it on the training read path |
| **stable-baselines3** | 2.9.0 (2026-06-15) | MIT | none | n/a | — | **None.** Already a dependency (`flake.nix:59`); `sb3.common.buffers.ReplayBuffer` is an in-memory transition buffer, not a pinned dataset. SB3 ships no dataset-freezing/snapshot helper — there is nothing here to borrow | **No-op.** Explicitly recorded so nobody re-surveys it: SB3 cannot supply G1's pinning |
| **pyarrow** (+ `pandas.util.hash_pandas_object`) | pyarrow 25.0.1 (2026-08-10) | Apache-2.0 | none | n/a | `pyarrow.dataset`, `ParquetFile` metadata | **Already available** (transitive via the store; `flake.nix:61-64`) | **Adopt the idea, not a new dep.** `ParquetFile` metadata is already what `store.stats` uses (`store.py:380-410`); `pd.util.hash_pandas_object` gives the provenance hash in §4 at zero cost |
| **ccxt** | 4.5.84 (2026-09-24, very active) | MIT | keyless for public OHLCV (optional key for higher tiers) | exchange-specific (Binance ~1200 req/min by weight; Kraken tier-based, both documented) | uniform `OHLCV` list → maps to `Candle` → `store.update(..., source=…)` duck-type (`kraken-market-data/README.md` §Extending) | **High syntactically, but it does not lift the ceiling.** Kraken's REST OHLC caps at ~720 bars regardless of `since`; a uniform client does not change the venue's REST semantics. It buys *multi-venue* (a Binance-native live leg alongside the seeded archive) | **Reject for G1.** Real value is the G4/G7 multi-venue question; note it there |
| **d3rlpy** | active (Apache-2.0) | Apache-2.0 | none (pip) | n/a | D4RL/MDP-style offline datasets, its own learners | **Low.** It replaces the SB3 PPO agent, the registry, and the whole `RLAgent` surface — a stack replacement to solve a bookkeeping gap | **Reject** |
| **HF `datasets` / `ml-datasets`** | active | Apache-2.0 | none for public data (token only for gated/hub pushes) | n/a | Arrow-backed `Dataset` keyed by `(ticker, time)` | **Medium technically, wrong shape operationally.** It wants a hub/dataset-builder identity and a cache dir for what is already a local month-sliced parquet tree | **Reject** |
| **dvc / lakeFS / git-annex** | active / active | Apache-2.0 / Apache-2.0 / GPL-2.0 | none (git remote optional) | n/a | content-addressed dataset versions | **Low.** Git-based multi-GB versioning for a store whose entire footprint is a few hundred MB of parquet | **Reject — but borrow the one idea:** the reason G1 hurts is *unrecorded identity*, which a 20-line `hashlib` block fixes |
| **zarr / h5py** | active | MIT / BSD-3 | none | n/a | chunked arrays | **Low.** Replaces a working parquet format with another one; the store's month-sliced layout is already the right shape | **Reject** |
| **Nix-native precedent (no library)** | — | — | — | — | `nix hash path` / `nix-store --query --hash` | — | Worth a nod: the store root *can* be content-hashed with Nix tooling already present, which is the strongest form of "the bytes a model was fitted on" |

**Net survey conclusion:** the research-grade reliability piece of G1 does not
need a library. The store *is* the pinned dataset (immutable month-sliced
parquet + a cursor sidecar); what is missing is a *recorded pointer* to a
window of it (bounds + count + content hash) and a way to *choose* the window
(config `since`/`until`). Both are stdlib+pandas. Every library that would
"help" either re-implements the store, replaces the agent, or solves a
distributed-data problem this single-host store does not have. The one
first-class *pattern* worth adopting from outside is the offline-RL
dataset-manifest convention — a small JSON/YAML sidecar next to the dataset
naming the window, its size, and a checksum — which `models/{T}/{N}/config.yaml`
already has a home for.

---

## 8. Short "rejected" list

- **ccxt / Binance-REST as the depth source** — does not lift Kraken's
  ~720-bar REST ceiling; only adds venue breadth (G4/G7 material).
- **d3rlpy / any offline-RL stack** — replaces `RLAgent` + SB3 + the registry to
  close a bookkeeping gap.
- **HF `datasets`** — hub-shaped identity for a local-file problem.
- **dvc / lakeFS / git-annex** — multi-GB dataset versioning; the win is a hash,
  not a system.
- **zarr / h5py / polars** — re-plumbing a working parquet store.
- **The sibling NixOS module as the scheduler** — verifiably broken `ExecStart`
  (`invalid choice: 'market'`) + `DynamicUser`/`StateDirectory` vs a
  `~/Projects` store root. Fix upstream, migrate later; mechanical once the
  ExecStart line is fixed.
- **Plain cron** — dominated by `systemd.user` on `Persistent` + logs.
- **Walking the store window with duckdb in the training path** — the month-glob
  read is already O(months-in-window); duckdb is for inspection.

---

## 9. Verification hooks for whoever implements this

- `tests/test_rl_data_store.py:170-181` already pins `since`/`until`
  behaviour against a `FakeStore`; extend it to drive the **config** path —
  assert `data_window.since` in a config dict reaches `store.read`, and that
  `train_ticker` writes `data_window.actual_start`/`actual_end`/`n_bars` into
  the produced `models/.../config.yaml`.
- A regression that `paper_trade` **ignores** `data_window.since` (§3) — the
  failure mode is silent (no fresh bars → no trades).
- An `export-data` equivalence check: `export.py`'s frame must equal the frame
  `train.py` read for the same config + window (both call `read_ohlc_dataframe`
  with the same args once the seam is wired).
- End-to-end acceptance: `read_ohlc_dataframe` with the store returns
  materially more than `6 × 720 = 4320` bars for a mapped ticker.

## RESEARCH-1 COMPLETE