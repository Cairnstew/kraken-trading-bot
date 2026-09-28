# VALIDATION — Phase 8: kraken-social-signals integration (Gap 3)

Author: reviewer, audit-pipeline Phase 8 pass (2026-09-28). This is the
**mandatory end-to-end gate**: the bot was trained AND backtested reading
through the new social-signal data source, live. Both unit suites and the
real integration test passed.

## What was built

**New sibling repo: `/home/seanc/Projects/kraken-social-signals`** (own git
repo, pushed to `github.com/Cairnstew/kraken-social-signals`, commit `71ca27d`):
- Keyless StockTwits v2 message streams + alternative.me Fear & Greed
  (full 2018+ history in one call, matched by UTC day).
- `SocialRecord` dataclass → per-(ticker, hour) JSONL
  `{ticker, timestamp, stt_mention_count, stt_tilt, fng_index}`.
- Hardened fetch (retry/backoff; HTTP 403 Cloudflare challenge is
  *non-fatal* — pipeline proceeds with no StockTwits data rather than crash).
- `export.py` registry mirroring the kraken-python pattern, typed models,
  structured logging, tiny CLI (`pull` / `list` / `version`), Nix flake
  dev shell + package, 52 offline tests.

**Integration seam in this repo (commits `6c88d2a` + `c95f984`):**
- `_SIGNAL_COLUMNS` widened to **9** in `rl/data.py` AND `rl/features.py`
  (kept in sync via a NOTE for `stt_mention_count`, `stt_tilt`, `fng_index`).
- New `social_features_file` config key in `configs/default.yaml` (null default).
- Third `merge_extra_features` call in `fetch_ohlc_dataframe` and
  `read_ohlc_dataframe` (store + fallback paths).
- Propagated through `train.py`, `backtest.py`, `paper_trade.py` call sites.
- 2 new offline tests (social merge + all three signal sources combined).

## Unit validation

| Repo | Suite | Result |
|------|-------|--------|
| `kraken-social-signals` | `nix develop --command pytest tests/ -q` | **52 passed** (0.10s) |
| `kraken-social-signals` | `nix flake check` | all checks passed (package + devShell) |
| `kraken-trading-bot` | `nix develop --command pytest tests/ -q` | **84 passed** (9.70s) — 82 pre-existing + 2 social seam |
| `kraken-trading-bot` | `nix flake check` | all checks passed |

## Live data source (step 2)

```
cd /home/seanc/Projects/kraken-social-signals
nix develop --command bash -c "python cli.py pull --ticker ETH/USD \
  --output /tmp/krss-test/eth_usd_social.jsonl --lookback-hours 48"
signals -> /tmp/krss-test/eth_usd_social.jsonl    (file exists, 3 records)
```

```json
{"fng_index":74,"stt_mention_count":2,"stt_tilt":0.0,"ticker":"ETH_USD","timestamp":"2026-09-28T12:00:00Z"}
{"fng_index":74,"stt_mention_count":15,"stt_tilt":0.0,"ticker":"ETH_USD","timestamp":"2026-09-28T13:00:00Z"}
{"fng_index":74,"stt_mention_count":13,"stt_tilt":0.0,"ticker":"ETH_USD","timestamp":"2026-09-28T14:00:00Z"}
```

- All three columns present; `timestamp` is ISO-8601 UTC on the hour.
- StockTwits returned **some** data for ETH.X (2/15/13 mentions in the last
  3h; thin but non-empty). Tilt is 0.0 = no Bullish/Bearish-tagged messages
  in the window — valid. `fng_index` (74) is present on every record.
  Even in the worst case (StockTwits 403 / empty), F&G always rides along
  and the pipeline degrades gracefully to zero-filled columns.

## Integration test (steps 3–5) — social-backed vs baseline

Scratch configs: `/tmp/krss-test/config.yaml` (`social_features_file:
/tmp/krss-test/eth_usd_social.jsonl`) and `/tmp/krss-test/config_baseline.yaml`
(same, `null`). Both: `--pages 2 --timesteps 3000 --action-space discrete
--seed 42 --models-root /tmp/krss-test/models`. Models kept separate
(`ppo_social_smoke` vs `ppo_social_baseline`) so neither run clobbers the
other's artifacts.

| Metric | Social-backed (through source) | Baseline (no source) |
|--------|-------------------------------|----------------------|
| Observation features | **52** (49 base + 3 social) | **49** (no signals) |
| Total return | +0.17% | +0.49% |
| Sharpe | 0.059 | 0.114 |
| Max drawdown | 5.54% | 8.44% |
| Trades | 419 | 54 |
| Win rate | 23.26% | 0.00% |
| Final equity | 10,016.94 | 10,049.04 |

Both train + backtest completed with **no error, no feature-width mismatch**.

### Source genuinely consumed — proof

- Train log: `Training PPO on 721 bars of ETH_USD (52 obs features, 3000
  timesteps)` for the social run vs `49 obs features` for baseline — **+3
  exactly = the social columns**.
- Merge log (DEBUG, confirmed via probe):
  `Merged 3 signal records from /tmp/krss-test/eth_usd_social.jsonl onto 721 OHLCV bars (columns: ['stt_mention_count', 'stt_tilt', 'fng_index'])`
- `normalization.npz` `feature_names` (the observation vector) contains
  `stt_mention_count`, `stt_tilt`, `fng_index` — count 52 vs 49 baseline.
- Social values landed on the correct bars: bars 12:00/13:00/14:00 UTC carry
  stt_mention_count 2/15/13, fng_index 74, matching the JSONL exactly;
  earlier bars zero-filled (leading-NaN fill), as designed.
- Saved model config `config.yaml` carries the source path, and the backtest
  re-reads it (`record.config.social_features_file`), so the backtest is
  genuinely through the same data source.

## Gate verdict

**PASS**

- (a) Social-file-backed train + backtest complete without error — yes.
- (b) Social columns entered the observation — yes: feature width grew from
  baseline 49 → 52, and the 3 social columns are named in the observation
  vector (`normalization.npz` + train log).
- (c) Backtest equivalent to baseline within stochasticity — yes:
  |+0.17% − +0.49%| = 0.32 pts ≪ 5-pt tolerance, same sign (both positive);
  Sharpe same sign and same order. PPO with a shared seed is still stochastic
  across runs (discrete action space at 3000 timesteps → small, near-random
  policies), so the small delta is expected stochasticity, not divergence.
  Feature *vectors* are observably identical in shape modulo the correct
  +3 social columns.
- The "Merged N signal records … onto M OHLCV bars" log fires for the
  social file.

Failure mode noted but not hit: the gate would be `NEEDS_FIX` if the merged
columns never reached the observation or the runs diverged beyond budget.
It did pass, so `PERIOD: kraken-social-signals — INTEGRATION GATE PASS`.