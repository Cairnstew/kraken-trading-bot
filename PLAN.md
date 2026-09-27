# Plan: Ticker Data Pipeline Expansion — Pass 1

## What Was Built

### New sibling repo: `ticker-news-signals`
Path: `/home/seanc/Projects/ticker-news-signals/` (own git repo, 4 commits, not nested in
`kraken-trading-bot`, not a submodule).

A Python package that pulls crypto news headlines keylessly (GNews → Google News RSS),
scores them with VADER sentiment, and emits per-(ticker, hour) signal vectors as JSONL.
Package mirrors `kraken-python`'s conventions:

- `ticker_news_signals/client.py` — thin `NewsClient` wrapper over GNews (search /
  search_by_topic, retry/backoff)
- `ticker_news_signals/models.py` — typed dataclasses `NewsArticle`, `SignalVector` with
  `to_dict()` / `from_dict()`
- `ticker_news_signals/sentiment.py` — `score_headline` / `score_batch` (VADER compound)
- `ticker_news_signals/pipeline.py` — `fetch_signals(ticker, lookback_hours)` → hour-bucketed
  aggregation: `sentiment_score` (mean), `article_count`, `novelty_flag`
- `ticker_news_signals/export.py` — extraction registry (`EXTRACTORS`, `extract`,
  `write_json` / `write_jsonl`, `SCHEMA_VERSION = "ticker-news-signals/1"`)
- `ticker_news_signals/{errors,logging_config,utils}.py` — exception hierarchy, structured
  JSON logging with redaction, RFC2822→UTC/hour-floor helpers
- `cli.py` — `pull --ticker --output --lookback-hours --json` + `version`
- `tests/` — 27 offline unit tests (GNews mocked via `unittest.mock.patch`), no network
- `pyproject.toml` + Nix flake (dev shell + package; gnews 0.8.2 and vaderSentiment 3.3.2
  vendored from PyPI in `nix/` because neither is packaged in nixpkgs)
- README (Features → Setup → Quickstart → CLI → Project layout → Extending → Tests),
  `.env.example`, `.gitignore`

**Output contract** (one record per ticker/hour):

```json
{
  "ticker": "ETH_USD",
  "timestamp": "2026-09-27T20:00:00Z",
  "sentiment_score": -0.1132,
  "article_count": 2,
  "novelty_flag": true
}
```

### Integration seam in `kraken-trading-bot` (stub only)
- `configs/default.yaml` — new optional `extra_features_file: null` flag (documented).
- `kraken_trading_bot/rl/data.py` — `merge_extra_features(df, extra_features_file)` stub
  (returns `df` unchanged; TODO documents the JSONL → hour-floor → left-join seam);
  `fetch_ohlc_dataframe()` gained the optional param and routes through the stub.
- `kraken_trading_bot/rl/train.py` — `train_ticker()` passes
  `cfg.get("extra_features_file")` into `fetch_ohlc_dataframe`.
- `INTEGRATION.md` in `ticker-news-signals` (cross-linked from its README) documents the
  output schema, refresh cadence, the current consumption stub, and the full wiring plan.

## Validation

| Check | Result |
|-------|--------|
| `ticker-news-signals` pytest | 27 passed (offline, GNews mocked) |
| `ticker-news-signals` `nix flake check` | all checks passed |
| `ticker-news-signals` live smoke pull | real GNews → VADER → JSONL in documented shape |
| `kraken-trading-bot` pytest (with integration) | 73 passed (20 pre-existing deprecation warnings) |
| `merge_extra_features` integration test | signal columns correctly merged + survive FeaturePipeline.compute → 52 features (49 + 3 signals) |

## What Was Deliberately Deferred

- NixOS module / systemd timer for the puller — on-demand CLI only this pass.
- Real-time streaming, multi-source aggregation (RSS outlets, CryptoPanic), full-article
  body extraction (trafilatura enrichment), transformer-grade sentiment (FinBERT).
- Remote repo creation / push / PR — not requested.

## Next Steps (for a follow-up pass)

1. ~~Implement `merge_extra_features`~~ — **DONE.** JSONL read, hour-floor left-join,
   forward-fill, 3 signal columns flow through `FeaturePipeline.compute()` into the
   agent's observation vector. `normalization.npz` will include signal stats on next
   training run.
2. Backtest a trained model with `extra_features_file` set to confirm the join and no
   feature-width regressions (`kraken-trading-bot backtest --ticker ETH_USD --model ...`).
3. Optional: a systemd timer / NixOS module to run `cli.py pull` hourly per ticker.
4. Optional pass 2 source: RSS feeds (feedparser) from crypto outlets, or CryptoPanic free
   tier, to thicken article volume for less-covered tickers.
5. Optional pass 2 source: RSS feeds (feedparser) from crypto outlets, or CryptoPanic free
   tier, to thicken article volume for less-covered tickers.