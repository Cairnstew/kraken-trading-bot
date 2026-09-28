# VALIDATION — Phase 7: kraken-funding-rates integration (Gap 2)

Author: lead agent, audit-pipeline Phase 7 pass (2026-09-28). Follows the
PASS gate from the kraken-market-data store (Gap 1). All commands below
were run live against real Kraken Futures data.

## What was built

**New sibling repo: `/home/seanc/Projects/kraken-funding-rates`** (own git repo):
- `KrakenFuturesClient` — keyless REST client for `futures.kraken.com/derivatives/api/v3`
- `FundingSnapshot` / `InstrumentInfo` dataclasses with `to_dict()` / `from_dict()`
- Export registry: `extract` / `extract_many` / `extract_snapshot` + `write_jsonl`
- CLI: `pull`, `extract`, `snapshot`, `list` commands
- Nix flake dev shell + package
- 28 offline tests (mocked HTTP), README, .env.example

**Integration seam in kraken-trading-bot** (commit `dc94012`):
- `_SIGNAL_COLUMNS` widened in `rl/data.py` and `rl/features.py` to include
  `funding_rate`, `basis`, `open_interest`
- New `funding_features_file` config key in `configs/default.yaml`
- `read_ohlc_dataframe` / `fetch_ohlc_dataframe` accept and merge the funding JSONL
- Propagated through `train.py`, `backtest.py`, `paper_trade.py` call sites
- 2 new offline tests for funding merge + combined signal merge

## Live verification

```
$ nix develop --command python -m kraken_funding_rates.cli snapshot --pair ETH/USD
Symbol:          PF_ETHUSD
Spot pair:       ETH/USD
Timestamp:       2026-09-28T13:22:38.086709Z
Funding rate:    0.01717456
Rate prediction: 0.01750777
Mark price:      2682.671972
Index price:     2682.450000
Basis:           0.008275%
Open interest:   26385.8000
24h volume:      47687.6700

$ nix develop --command python -m kraken_funding_rates.cli pull --pair ETH/USD --output /tmp/funding_test.jsonl
Wrote 1 record(s) to /tmp/funding_test.jsonl
```

JSONL output (timestamp floored to hour):
```json
{"symbol": "PF_ETHUSD", "spot_pair": "ETH/USD", "timestamp": "2026-09-28T13:00:00+00:00",
 "funding_rate": 0.017174561738, "basis": 9.921239066464465e-05, "open_interest": 26385.213, ...}
```

## Test evidence

| Repo | Suite | Result |
|------|-------|--------|
| `kraken-funding-rates` | `pytest tests/ -v` | **28 passed** (2.14s) |
| `kraken-trading-bot` | `pytest tests/ -q` | **82 passed** (15.28s) — 73 pre-existing + 7 store seam + 2 funding seam |

## Gate verdict

**PASS** — funding-rate JSONL merges cleanly onto the OHLCV frame alongside
news signals; the `_SIGNAL_COLUMNS` widening adds 3 new feature columns
(`funding_rate`, `basis`, `open_interest`) to the RL observation vector
when `funding_features_file` is configured; no regressions to existing
tests. The feature pipeline's `signals` group now passes through 6 columns
(3 news + 3 funding) instead of 3.

## Deferred / next dev slices

- Wire `kraken-funding-rates` into the bot's dev shell (flake input) for
  live smoke-test of `funding_features_file` in train/backtest.
- Gap 3: social/search-trend (StockTwits v2 + alt.me Fear & Greed).
- Surface `since`/`until` on train/backtest CLI.
- NixOS module for the market-data timer + optional funding poller.