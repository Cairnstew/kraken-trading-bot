# PLAN — Phase 6: validation evidence and wiring roadmap

Author: Reviewer, audit-pipeline team. Written after the build/integrate phase.
This is the **Phase 6** plan: what was audited, what was deferred, what was
built (both repos), the validation evidence, and the concrete next steps for
fully wiring the store — and later the Gap 2/3 vectors — into the RL pipeline.
Sibling docs: `AUDIT.md` (gap catalog), `RESEARCH.md` (live API research),
`DECISION.md` (Gap 1 + pandas/pyarrow choice).

---

## 1. What was audited

The Phase 1 audit mapped **four independent data paths** in kraken-trading-bot:
the live engine loop (ticker / OHLC / order book per 60 s tick), RL train
(paged OHLC), backtest (refetch on `data=None`), and paper trade (60-day
refetch every 60 s). All paths talk only to kraken-python REST; no path
persisted anything, and the exogenous news-signal seam was the only disk
source. The audit catalogued six candidate gaps; the live research phase
**corrected an audit assumption**: `GET /0/public/OHLC` returns at most ~720
bars (~30 days at 1 h) regardless of `since`, so `train(pages=6)` never
actually produced ~180 d — every RL run saw ≤ ~30 d.

## 2. What was decided and built

**DECISION.md chose Gap 1** (local market-data store) on pandas + pyarrow
parquet, as a sibling **`kraken-market-data`** repo (house-style mirror of
kraken-python + ticker-news-signals), with a NixOS systemd-timer poller.

Built (both repos):

- **`/home/seanc/Projects/kraken-market-data`** (own git repo, 2 commits):
  - `MarketDataStore` — month-sliced parquet `store/{PAIR_ID}/{interval}/{YYYY-MM}.parquet`,
    `_meta.json` since-cursor sidecar, `read/upsert/update/verify/stats`,
    `_frame_shape` guaranteeing the pipeline's exact DataFrame contract
    (UTC `DatetimeIndex` named `time`, columns
    `time, open, high, low, close, vwap, volume, count`).
  - `export.py` registry (ohlc / ohlc_candles / stats / verify), `client.py`,
    `models.py` (Decimal-safe Candle), `errors.py`, `logging_config.py`,
    `utils.py`, thin `cli.py` (`update/backfill/verify/stats/list/extract/version`).
  - Nix flake: dev shell, `buildPackage`, NixOS module + systemd timer
    (`OnCalendar=*-*-* *:*:30`, `StateDirectory` + `DynamicUser`, keyless),
    `nix/checks.nix` module-shape assertions.
  - `tests/` (46 offline tests), `scripts/verify_live.py`, `INTEGRATION.md`
    (consumer contract), `.env.example` (`MARKET_DATA_DIR` only, no secret).
- **This repo — integration seam** (commit `b6fedce`, 7 files):
  - `kraken_trading_bot/rl/data.py`: `_page_candles` (shared pagination),
    `read_ohlc_dataframe` adapter (fetch → `upsert` → `read` when a store is
    configured; byte-identical pass-through to `fetch_ohlc_dataframe` when
    `null`), `_resolve_store` (lazy import, path or store-like duck-typed
    object).
  - `configs/default.yaml`: documented `market_data_store: null` key.
  - `train.py`, `backtest.py`, `paper_trade.py`, `rl/__init__.py`: call sites
    and exports read through the adapter.
  - `tests/test_rl_data_store.py`: 7 new offline tests (in-memory store shim,
    no network / pyarrow / sibling import).

## 3. Validation evidence (all run this phase)

### kraken-market-data (`/home/seanc/Projects/kraken-market-data`, commit `055d7f6`)

| Check | Command | Result |
|---|---|---|
| Offline tests | `nix develop -c python -m pytest tests/ -q` | **46 passed** (0.90 s) |
| Flake eval incl. module | `nix flake check --no-build` | all checks passed |
| Module-shape check build+run | `nix build .#checks.x86_64-linux.market-data-module` | build OK, run OK (grep assertions on 1-pair/2-pair/disabled shapes, ExecStart `market update --pair … --interval … --store …`, timer calendar) |
| Package build | `nix build .#kraken-market-data` | build OK, `result/bin/kraken-market-data` present |

### kraken-trading-bot (this repo, commit `b6fedce`)

| Check | Command | Result |
|---|---|---|
| Full suite | `nix develop -c python -m pytest tests/ -q` | **80 passed** (16.64 s) — 73 pre-existing + 7 new |
| New seam tests | `nix develop -c python -m pytest tests/test_rl_data_store.py -q` | **7 passed** |

Env note: `nix develop` (flake dev shell) was required — the checked-out repos
have no local venv with working numpy (kraken-market-data's `.venv` is a
broken pip/numpy install). No network happened during tests.

## 4. Seam sanity check (code review)

- **Shape parity when store is null** — `read_ohlc_dataframe` with
  `market_data_store=None` returns `fetch_ohlc_dataframe(...)` directly
  (same args), so train/backtest/paper call sites are byte-identical to
  before; proven by `test_read_ohlc_dataframe_null_store_falls_back_to_live_fetch`
  (`assert_frame_equal`).
- **Lazy store resolution** — `_resolve_store` imports
  `from market_data.store import MarketDataStore` only inside the path branch;
  a store *object* is used duck-typed via `getattr(upsert/read)`; non-store
  values get a guiding `TypeError`. Tests cover the missing-sibling
  `ValueError` (monkeypatched `sys.modules`).
- **Config docs consistent** — `configs/default.yaml` `market_data_store: null`
  comment matches the DECISION contract and `kraken-market-data/INTEGRATION.md`.
- **Read contract matches real store** — sibling `store.read` applies
  `_frame_shape` (same `_OHLCV_COLUMNS`, UTC index, ints/floats) as its final
  step; adapter and test shim mirror exactly that shape.

## 5. What was deferred (explicitly out of this pass)

From AUDIT.md/DECISION.md, the runner-up gaps stay deferred; each is
depth-capped without the store, and the store is now the foundation to land
them on:

- **Gap 2 — funding-rates / perp-basis.** Best data fit = Binance FAPI
  (deep since-2020 funding + hourly mark = basis), but US geo/ToS risk must be
  host-jurisdiction-confirmed; house-fit poller = Kraken-Futures `PF_*`
  (same venue, no public history → forward-only). Joins as per-(ticker,hour)
  scalar through `merge_extra_features` / widened `_SIGNAL_COLUMNS`.
- **Gap 3 — social / search-trend.** StockTwits v2 (keyless, per-ticker
  mention velocity/tilt) + alternative.me Fear & Greed (free 2018+ history);
  both join onto the store's deeper frame via the same seam.
- **Gap 4 — order-book / trades microstructure recorder.** Needs the store
  first; engine already fetches top-10 depth and throws it away.
- **Gap 5 — on-chain metrics.** Backfill-limited free sources.
- **Gap 6 — macro calendar.** Feature-design-heavy (event-envelope encoding).

Also deferred items from the audit's "smaller findings": the news-signal
hourly scheduler (systemd/cron), model-config provenance hardening
(feature_windows/groups/reward recorded in `models/*/config.yaml`), and
throttle/retry policy (`KRAKEN_MIN_INTERVAL` still unset by default).

## 6. Concrete next steps to fully wire the store into the RL pipeline

1. **Set the config key.** Point `market_data_store:` at the store root
   (e.g. `~/Projects/kraken-market-data/store`) in `configs/default.yaml`
   for the environments that should persist. Verify `train_ticker` runs with
   the store path (needs the sibling package importable — add it to this
   repo's dev shell / flake input, or rely on the duck-typed object).
2. **Honour `since`/`until` from config.** `read_ohlc_dataframe` already
   accepts `since`/`until`; surface them from `train.py`/`backtest.py`
   config so historical windows are selected by CLI/config, not only by the
   live pages loop. Follow-up note in the adapter's `.. todo::` is the
   current inventory: drive train/eval split + walk-forward through the
   currently-unused `TradingEnvironment.reset(options=...)`.
3. **Collapse the paper trader to append + tail read.** Paper trade currently
   fetches `pages=2` every 60 s; with the store configured the same adapter
   call already appends — next step is to make it work when `pages=0`/no
   fetch needed (config knob) so the per-tick cost is a store tail read.
4. **NixOS module wiring.** Enable `services.kraken-market-data` on the host
   (pairs/intervals from the bot config, `dataDir` matching
   `market_data_store`), so the since-cursor poller accumulates depth on the
   systemd timer — this is what finally breaks the ~30-day ceiling over time.
5. **Deep-history seed (optional, 2nd venue).** Only if venue-consistent
   depth is needed *now*: seed via `backfill --csv/--jsonl` from a backup or
   an allowed source; Kraken REST cannot backfill deep history (verified).
6. **Then Gap 2 / Gap 3 vectors.** Once the OHLC frame is store-backed,
   widen `_SIGNAL_COLUMNS` (`rl/features.py:30`) for the new columns and pipe
   the funding/basis (Kraken-Futures poller or Binance, jurisdiction-checked)
   and StockTwits/F&G pullers into the same `extra_features_file` /
   `merge_extra_features` seam; optionally read `_SIGNAL_COLUMNS` from config
   so future sources stop needing code edits.

## 7. Risks / notes for the lead

- Neither repo was committed by the reviewer (per guardrails; the lead owns
  commits). Plan written to `.data-audit/PLAN.md` in the reviewer worktree
  and the main-repo `.data-audit/` collection point.
- The existing repo-root `PLAN.md` (prior ticker-news-signals pass plan) was
  **not** touched.
- Live-verify scripts (`scripts/verify_live.py`, bot `RUN_LIVE=1` tests) were
  not run — out of scope (read-only phase, no network load). The 720-bar
  ceiling is already live-verified in RESEARCH.md.

PLAN COMPLETE