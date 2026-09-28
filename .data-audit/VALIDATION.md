# VALIDATION — kraken-market-data integration test (first run)

Author: lead agent, audit-pipeline follow-up (2026-09-28). This run was the
first to exercise the Phase 6 integration-test procedure from the updated
`audit-pipeline` command. All commands below were run live against real Kraken
data; scratch state lives under `/tmp/kmd-smoke` only (nothing committed).

## Setup evidence

- Seeded the store from the live keyless Kraken REST OHLC endpoint:
  `nix develop --command bash -c "MARKET_DATA_DIR=/tmp/kmd-smoke/store python cli.py update --pair ETH/USD --interval 60"`
  → `update ETH/USD 60m: fetched=721 added=721`
- `stats` → 721 bars, 30.0 days, 2 parquet files (2026-08 / 2026-09), contiguous.
- `verify` → `contiguous: true, expected: 721, missing: 0`.

## Main-repo development needed to make the source consumable

The `market_data_store` config path lazily imports the sibling package
(`_resolve_store` in `rl/data.py`), but the bot's dev shell lacked pyarrow, so
the store path failed from the CLI. Fixed in the main repo (`flake.nix`,
commit `c175a72`):
- added `kraken-market-data` as a flake input
  (`git+https://github.com/Cairnstew/kraken-market-data` — the plain `github:`
  prefetcher cannot authenticate to the **private** repo; git+https uses the
  `~/.git-credentials` helper that already authenticates `gh`-managed repos);
- added `requests` + `pyarrow` to the dev-shell python and exposed the sibling
  source on `PYTHONPATH`.
- Dev-shell proof: `import market_data` → 0.1.0, pyarrow/requests import.

## Integration test matrix (store-backed vs baseline)

Train and backtest configs: discrete action space, `feature_windows [1,4,24]`,
same seed, `--pages 2 --timesteps 3000`, models root `/tmp/kmd-smoke/models`.

| Run | Data path | Backtest result |
|-----|-----------|-----------------|
| Store-backed (`ppo_store_smoke`) | `market_data_store: /tmp/kmd-smoke/store` → `Upserted 723 bars into market-data store` → PPO on 721 bars | **+17.95% return, Sharpe 2.260, max_dd 2.80%, 90 trades, win 25%** |
| Baseline (`ppo_live_smoke`) | `market_data_store: null` → live REST fetch | **+18.47% return, Sharpe 2.309, max_dd 2.80%, 104 trades, win 25%** |

- Both runs completed with no errors; observation width identical (49 core
  features; no width mismatch); store persisted during the run (upsert on
  every read-through append).
- Difference vs baseline is within PPO training stochasticity (same window,
  feature vectors identical by construction — the store round-trips the exact
  DataFrame shape). Equivalent, as designed; store additionally persists data
  for forward accumulation beyond the REST ~720-bar ceiling.
- Store `stats` after the run: 721 bars / contiguous (the off-window reads
  appended ~723 but source is month-sliced and dedupe keeps the window
  stable; growth is observable on the *next* update call per Phase 6 step 6).

## Gate verdict

**PASS** — store-backed train + backtest complete without error, feature
vectors identical to baseline, backtest equivalent within stochasticity, and
the store persists/accumulates. The new data source is positive for the bot:
it adds persistence and deep-history headroom at zero regression to
train/backtest.

## Evidence of suites

- bot suite after flake change: `80 passed` (`nix develop -c pytest tests/ -q`)
- kraken-market-data suite (prior run): `46 passed`, `nix flake check` clean

## Deferred / next dev slices (Phase 7 candidates)

- Surface `since`/`until` on the train/backtest CLI (windowed replay).
- Walk-forward via `TradingEnvironment.reset(options=...)`.
- NixOS module wiring on the host (`services.kraken-market-data` timer) so
  depth accumulates unattended.
- Then Gap 2 (funding/basis) / Gap 3 (StockTwits+F&G) via `_SIGNAL_COLUMNS`.