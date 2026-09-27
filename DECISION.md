# Decision: Ticker News Signals

## Chosen Data Vector

**Headline sentiment + article volume for a ticker's underlying crypto asset, refreshed hourly.**

Per (ticker, timestamp) record:
- `sentiment_score`: float in [-1.0, 1.0] — mean VADER compound score of headlines published in the preceding hour
- `article_count`: int — number of unique articles published in the preceding hour
- `novelty_flag`: bool — True if this is the first hour with articles since a 6-hour silence gap

This is a **3-element feature vector** that aligns directly with the OHLCV bar timestamps. It adds minimal dimensionality (3 floats) to the existing feature matrix, which already produces 40+ features per bar.

## Chosen Libraries

1. **GNews** (v0.8.2, MIT) — keyless Google News RSS search with `DIGITAL_CURRENCIES` topic support. Returns structured article dicts (title, description, published_date, url, publisher). Built-in retry/backoff for rate limits.

2. **vaderSentiment** (v3.3.2, MIT) — rule-based sentiment scoring on headlines. Zero ML dependencies, O(1) per headline, works well on short social/news text.

## Project Name

**`ticker-news-signals`**

Chosen over `kraken-news-signals` because the signal is not Kraken-specific — it works for any crypto ticker. The project lives at `../ticker-news-signals/` relative to `kraken-trading-bot`.

## Target Location

`/home/seanc/Projects/ticker-news-signals/` — sibling directory to `kraken-trading-bot`.

## Integration Sketch

### Output Contract

One JSONL file per ticker, one record per bar:

```json
{
  "ticker": "ETH_USD",
  "timestamp": "2026-09-27T14:00:00Z",
  "sentiment_score": 0.42,
  "article_count": 7,
  "novelty_flag": false
}
```

Written to a configurable output directory (default: `./signals/`). The `kraken-trading-bot` RL pipeline reads this JSONL and merges it onto the OHLCV DataFrame by timestamp before feature engineering.

### How kraken-trading-bot Consumes It (stub)

1. Add `extra_features_file: null` to `configs/default.yaml`
2. In `data.py::fetch_ohlc_dataframe`, after building the OHLCV DataFrame, check if `extra_features_file` is set. If so, read the JSONL and left-join on timestamp.
3. The merged columns become additional input to `FeaturePipeline.compute()` which already handles extra columns gracefully.

### Refresh Cadence

The CLI runs as a cron job or manual pull every hour:

```bash
python cli.py pull --ticker ETH/USD --output signals/eth_usd.jsonl
```

This appends the latest hourly signal to the JSONL file. The RL training pipeline reads the full file and joins on bar timestamps.

## Scope Boundary

This pass builds:
- The extraction + sentiment pipeline (GNews → vaderSentiment → signal vector)
- CLI for pulling signals
- Offline tests with mocked responses
- Integration stub in kraken-trading-bot (config flag + TODO in data loading)

This pass does NOT build:
- Real-time streaming (future: WebSocket integration)
- Multi-source aggregation (future: add RSS feeds, CryptoPanic)
- Full article body extraction (future: trafilatura enrichment)
- Model retraining with the new signal (future: feature pipeline update)
