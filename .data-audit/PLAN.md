# PLAN — Phase 8: kraken-social-signals validation and wiring roadmap

Author: reviewer, audit-pipeline team. Written after the build/integrate/verify
phase. This is the **Phase 8** plan: what was audited, what was built, the
validation evidence (full detail in `VALIDATION.md`), what was deferred, and
the concrete next steps for fully wiring the social signal into the RL
pipeline. Sibling docs: `AUDIT.md` (gap catalog), `RESEARCH.md` (three live
surveys), `DECISION.md` (Gap 3 choice: StockTwits + alternative.me F&G).

---

## 1. What was audited

Phase 8 is the third exogenous-signal pass. After the store seam
(kraken-market-data, Gap 1) and the funding seam (kraken-funding-rates,
Gap 2), the audit re-mapped the pipeline and re-ranked the remaining gaps:

- **Gap 3 — social / search-trend (this pass).** StockTwits v2 is keyless,
  per-ticker, with bullish/bearish tags (mention velocity + tilt);
  alternative.me Fear & Greed gives a free, full 2018+ history market-wide
  0-100 index. Both join onto hourly bars through the same proven
  `merge_extra_features` seam as news and funding. Live surveys confirmed
  both sources are reachable keyless with the browser-grade UA and that
  403s are retryable/non-fatal.
- **Gap 4 — order-book / trades microstructure recorder** (now unlockable):
  a dedicated `kraken-microstructure` recorder can build depth/flow features
  on top of the store. Depth was live-verified this phase; needs a recorder
  + feature columns.
- **Gap 5 — on-chain metrics.** Backfill-limited free sources (Etherscan /
  blockchain.info are not clean per-asset keyless historical feeds).
  Deferred as deep-keyless-research.
- **Gap 6 — macro calendar.** Feature-design-heavy (event-envelope encoding).
  Deferred.

Smaller findings carried over from the audit remain deferred: the news-signal
hourly scheduler (systemd/cron — now four signals need one), model-config
provenance hardening (`feature_windows/groups/reward` in `models/*/config.yaml`
is partially recorded), throttle/retry policy (`KRAKEN_MIN_INTERVAL` unset by
default).

## 2. What was built

**New sibling repo `/home/seanc/Projects/kraken-social-signals`** (commit
`71ca27d`, pushed to `github.com/Cairnstew/kraken-social-signals`):
- `client.py` — keyless StockTwits v2 + alternative.me F&G clients.
- `models.py` — `StockTwitsMessage`, `FearGreedRecord`, `SocialRecord`
  (`ticker, timestamp, stt_mention_count, stt_tilt, fng_index`, JSON-safe).
- `pipeline.py` / `export.py` — per-(ticker, hour) bucketing + tilt
  `(bull−bear)/(bull+bear)`, extraction registry + `write_jsonl`.
- `cli.py` (`pull`/`list`/`version`), `errors.py`, `logging_config.py`,
  `utils.py`, Nix flake (dev shell + package), 52 offline tests, README,
  `.env.example` (optional token only — default path keyless).

**This repo — integration seam** (commits `6c88d2a` + `c95f984`):
- `_SIGNAL_COLUMNS` widened to 9 in `rl/data.py` + `rl/features.py` (offset
  `stt_mention_count`, `stt_tilt`, `fng_index`), kept in sync with a NOTE.
- `social_features_file` config key in `configs/default.yaml`.
- Third `merge_extra_features` call in `fetch_ohlc_dataframe` and
  `read_ohlc_dataframe` (both store and fallback paths).
- Propagated through `train.py` / `backtest.py` / `paper_trade.py`.
- 2 new offline tests (social merge + all three signals combined).

## 3. Validation evidence (summary; full matrix in `VALIDATION.md`)

| Check | Result |
|---|---|
| kraken-social-signals `pytest tests/ -q` | **52 passed** |
| kraken-social-signals `nix flake check` | all checks passed |
| kraken-trading-bot `pytest tests/ -q` | **84 passed** (82 pre-existing + 2 seam) |
| kraken-trading-bot `nix flake check` | all checks passed |
| Live `pull --ticker ETH/USD --lookback-hours 48` | 3 records, ISO-UTC hours, fng present, StockTwits thin but non-empty |
| **Integration train** (through source, `--pages 2 --timesteps 3000` discrete) | completed, **52 obs features** (49 base + 3 social), no width mismatch |
| **Integration backtest** (through source) | return +0.17%, Sharpe 0.059, maxDD 5.54%, 419 trades |
| **Baseline** train (same config, `social_features_file: null`) | completed, **49 obs features** |
| **Baseline** backtest | return +0.49%, Sharpe 0.114, maxDD 8.44%, 54 trades |
| Merge log | `Merged 3 signal records from .../eth_usd_social.jsonl onto 721 OHLCV bars` |
| Observation-column proof | `normalization.npz` feature_names contains the 3 social columns; social values land on correct bars |

**Gate: PASS** — social-backed train + backtest complete without error, the
social columns entered the observation (+3 features, confirmed via the model
artifact and train log), and the backtest is equivalent to baseline within
training stochasticity (|0.32 pts|, same sign, same Sharpe order).

Smoke artifacts (models, JSONL, configs) live under `/tmp/krss-test/` and were
**not committed**; the repo tree stays clean except `.data-audit/`.

## 4. What was deferred (explicitly out of this pass)

- **Normalization wiring.** The seam merges raw social values and the
  `signals` group forwards them as-is into the same normalization stats as
  every other feature — but there is no dedicated normalization policy for
  bounded vs unbounded signal distributions yet; that is a correctness/tuning
  slice, not a blocker (stats are per-ticker mean/std like all features).
- **Scheduler.** The four sibling pollers (news, funding, social, and the
  market-data timer) are not yet wired into one systemd/NixOS schedule.
- **Microstructure (Gap 4) and on-chain (Gap 5) projects** — deferred
  sibling projects; microstructure needs a recorder, on-chain needs a
  keyless-history survey.
- **Paper-trade live smoke of the social columns** — the call site is wired
  (`paper_trade.py:296`) but was not run live in this pass.
- Smaller audit findings listed in §1.

## 5. Concrete next steps to fully wire the signal into the RL pipeline

1. **Collapse the pollers onto one scheduler.** Add a NixOS module (mirroring
   `kraken-market-data`'s) that runs the social pull (and news + funding)
   hourly → per-ticker JSONL into a stable `signals/` dir, and point the bot
   config at those paths so train/backtest/paper read a continuously
   refreshed source.
2. **Honour `since`/`until` from config** on train/backtest so historical
   windows can be selected (and backfill F&G 2018+ history rather than only
   the live lookback).
3. **Dedicated signal-normalization policy.** Decide whether bounded (tilt,
   F&G 0-100) vs unbounded (mention count) columns should share one z-score
   path, or get per-column handling; document the choice in
   `features.py` next to `_SIGNAL_COLUMNS`.
4. **Paper-trade live smoke** of the social columns end-to-end at a small
   budget, confirming the 60-s loop keeps the merged observation 52-wide.
5. **Microstructure (Gap 4).** Build the recorder on the store; add
   `spread`/`order_book_imbalance` via `_add_microstructure_features` (the
   feature group already handles those columns when present).
6. **On-chain (Gap 5).** Re-survey keyless per-asset history; at minimum
   gecko-free, else document clearly mitigated sources.