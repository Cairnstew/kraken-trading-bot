# Research: Exogenous Signal Libraries for Crypto Trading

## Candidate Libraries Evaluated

### News Aggregation & Article Scraping

| Library | Version | License | Auth | Rate Limits | Output | Maintenance |
|---------|---------|---------|------|-------------|--------|-------------|
| **GNews** | 0.8.2 | MIT | Keyless (Google News RSS) | Google throttles; built-in retry + backoff | Structured JSON (title, description, published_date, url, publisher) | Active (237 commits, 1k stars) |
| **trafilatura** | 2.2.0 | Apache-2.0 | None | None (fetches URLs) | JSON/CSV/XML with clean text + metadata | Production/Stable (1,662 commits, 6.9k stars) |
| **feedparser** | 6.0.14 | BSD-2-Clause | None | None | Parsed feed entries (title, summary, link, published) | Production/Stable |
| **newspaper4k** | 0.9.6 | MIT | None | None | Article text + metadata | Beta (0.9.x) |
| **newsapi-python** | — | — | API key required | 100 req/day free; paid for production | JSON articles | Tied to NewsAPI.org service |

### Sentiment Analysis

| Library | Version | License | Auth | Dependencies | Notes |
|---------|---------|---------|------|--------------|-------|
| **vaderSentiment** | 3.3.2 | MIT | None | None | Rule-based, fast, works on short text/headlines. No ML deps. |
| **TextBlob** | 0.20.1 | MIT | None | None | Simple sentiment polarity (-1 to 1). Lightweight. |
| **transformers** | 5.17.0 | Apache-2.0 | None | torch (heavy) | FinBERT available but requires GPU or slow CPU inference. Overkill for hourly headline scoring. |

### Crypto-Specific

| Library | Version | License | Auth | Notes |
|---------|---------|---------|------|-------|
| **pycoingecko** | 3.2.0 | MIT | Keyless | Market data (prices, volumes, market cap). Not news/sentiment. |
| **CryptoPanic** | 0.1.0 | MIT | API key (free tier) | Crypto news aggregator. Beta. Needs API key. |

## Evaluation Against RL Pipeline Needs

The RL feature pipeline (`kraken_trading_bot/rl/features.py`) consumes a per-ticker, per-timestamp DataFrame with OHLCV columns plus optional microstructure columns. The ideal exogenous feature is:

1. **A scalar per (ticker, timestamp)** — e.g. sentiment score, article volume, novelty flag
2. **Aligned to OHLCV bar timestamps** — so it can be merged into the feature matrix
3. **Refreshable hourly** — matching the default 60-minute bar interval
4. **Keyless for production use** — avoids paid API dependencies
5. **Lightweight** — no GPU, no heavy ML dependencies

### Key Insight

Raw article text is not directly useful for RL. The pipeline needs **derived signals** — aggregated sentiment scores, article counts, and novelty flags per time window. The extraction library provides the raw articles; a lightweight sentiment scorer and aggregation logic produce the actual features.

## Ranked Shortlist

### 1. GNews + vaderSentiment (Recommended)

**Rationale:** GNews provides structured, keyless article search with a `DIGITAL_CURRENCIES` topic and keyword search (e.g. "Bitcoin", "Ethereum", "ETH price"). It returns title, description, publisher, and published date as clean dicts — no HTML parsing needed. vaderSentiment scores headlines in O(1) time with no ML dependencies. Together they produce: `(sentiment_score, article_count, novelty_flag)` per hourly window.

**Risks:** Google may rate-limit aggressive scraping; GNews has built-in retry/backoff. The RSS feed returns redirect URLs (resolvable with `gnews[playwright]` or the SearchApi backend). Article volume may be thin for niche tickers.

### 2. feedparser + RSS from crypto outlets

**Rationale:** RSS feeds from CoinDesk, CoinTelegraph, The Block provide crypto-specific news. feedparser is battle-tested and has zero auth requirements. Pair with vaderSentiment for scoring.

**Risks:** Requires curating and maintaining RSS feed URLs per outlet. Feed quality varies. No topic filtering — must parse keywords manually. More operational burden than GNews.

### 3. trafilatura (for article body extraction)

**Rationale:** Best-in-class text extraction (Apache-2.0, 6.9k stars, production stable). Could be used to extract full article text from URLs found via GNews or RSS feeds, enabling deeper sentiment analysis beyond headlines.

**Risks:** Requires fetching each article URL (adds latency, rate limits). Full article text is overkill for hourly scalar features — headline sentiment is sufficient. Best used as an optional enrichment step, not the primary extraction layer.

## Additional Findings

- **CryptoPanic** offers a free tier API for crypto news but requires API key registration. Could be a future alternative if GNews rate limits become problematic.
- **transformers/FinBERT** is the gold standard for financial sentiment but requires torch (~2GB) and is too heavy for an hourly polling pipeline. Reserved for future passes if headline-level sentiment proves insufficient.
- **pycoingecko** provides market data but not news — useful for price/volume enrichment, not sentiment.
