# RESEARCH — GAP 2: Funding-rates / perp-basis

Audit phase, Researcher 2 (derivatives microstructure). Read-only. Every API claim below was
**verified live this session** against the production endpoint (2026-09-28), not taken from docs.
House-style mirror target: `kraken-python` (github.com/Cairnstew/kraken-python) — small typed
modules fronted by a manager facade, `export.py` extraction registry (`extract(mgr, "resource")`
→ JSON/JSONL), Decimal-safe strings, Nix flake dev shell, thin `cli.py`, offline tests + a live
verify script.

## The consumer seam (what a candidate must reduce to)

The RL feature path can take exogen per-(ticker, hour) scalars **two ways** today:

1. **`merge_extra_features` seam** (`rl/data.py:61`) — JSONL of `{ticker, timestamp, col…}`,
   hour-floor left-join, ffill + zero-fill. Requires widening `_SIGNAL_COLUMNS`
   (`rl/features.py:30`) — currently `("sentiment_score", "article_count", "novelty_flag")` — by
   the new column names. One-line widening + `_add_signals_features` already forwards any numeric
   column present (`features.py:390`), so **no feature-pipeline redesign** is needed.
2. **`microstructure` group** (`rl/features.py:364`) — already coded to read
   `spread/bid/ask/bid_vol/ask_vol` columns off the OHLC frame. Adding `funding_rate`,
   `perp_basis`, `open_interest` columns to the frame feeds this group directly; it currently
   yields nothing because the OHLC fetcher never produces those columns.

So the hard requirement on any candidate: **a per-(ticker, hour) scalar** — an instantaneous
funding rate (carries net-long/​short crowding), a 24 h cumulative funding (carries cash-flow) or a
*basis* = (perp index price − spot index)/spot. Basis needs both legs; the pipeline already has
spot OHLC, so basis = `markPrice`/spot-close − 1 computed locally. Quote-currency note: the bot's
spot is USD-quoted (`ETH/USD`); perp funding is usually USDT-quoted (`ETHUSDT`), but a funding
*rate* is dimensionless and USDT/USD drift is ~0.1 % — harmless for a feature. USD-quoted perps
exist where noted.

## Candidates (verified live)

### A. Kraken Futures REST — native, USD-quoted, same venue

Host `https://futures.kraken.com/derivatives/api/v3`, **keyless public market data**.

- `GET /tickers` → per instrument: `fundingRate`, `fundingRatePrediction`, `markPrice`,
  `indexPrice`, `bid/ask`, `openInterest`, `vwap24h`, `lastTime`. ✅ (live response confirmed:
  `PF_ETHUSD`, `PI_XBTUSD`, all `PF_*` perps + quarterly `FF_*`).
- `GET /instruments` → `fundingRateCoefficient` (8 on perps), contract sizes, openingDate.
- `GET /api/charts/v1/trade/{symbol}/{resolution}` → hourly trade candles per perp (basis leg
  backfill, verified live at `PF_ETHUSD/1H`). ✅
- **Funding history: NOT publicly available.** Docs reference
  `GET /derivatives/api/v3/historicalfundingrates` but it returns **404 on production** (verified);
  `GET /derivatives/api/v3/history/funding` is an **authed** trading-history endpoint (404s without
  a key). Forward-only accumulation via a poller, or use an authed Futures API key.
- Funding cadence: Kraken perp funding is applied on an hourly schedule; `tickers` exposes live
  rate + predicted next rate.
- Rate limits: public market-data is wide-open for a 1/h poller (cost budget 500/10 s applies to
  authed calls). Same exchange the bot already talks to — no new geo/ToS surface.
- License/ToS: Kraken API, no key required for market data. Zero geo restriction.

### B. Binance FAPI (USDⓈ-M futures) — deepest keyless funding history

Host `https://fapi.binance.com`, **keyless public** (requires a `User-Agent` header).

- `GET /fapi/v1/premiumIndex?symbol=ETHUSDT` → `lastFundingRate`, `nextFundingTime`, `markPrice`,
  `indexPrice`, `interestRate`. ✅ verified.
- `GET /fapi/v1/fundingRate?symbol=ETHUSDT&startTime=&endTime=&limit=` → **full funding history,
  back to 2020** (verified `startTime=0` returns Jan-2020 records for BTCUSDT); 1000 rows/call,
  weight 1. Funding settles every 8 h.
- `GET /fapi/v1/markPriceKlines?symbol=ETHUSDT&interval=1h` → hourly mark-price candles →
  **full basis backfill**. ✅ verified.
- Output: structured JSON, **all numerics are strings** → Decimal-safe.
- Rate limits: generous for hourly use (public weight budget ~2400/min). 
- **ToS/geo risk: US IPs are blocked and the fapi terms bar US persons** — the one real caveat.
  Also `ETHUSD_PERP` (inverse, USD-quoted) exists for a USD-denominated leg if the USDT-notation
  bothers anyone.

### C. Bybit V5 (linear) — full keyless history + native `basis` field

Host `https://api.bybit.com`, keyless.

- `GET /v5/market/tickers?category=linear&symbol=ETHUSDT` → `fundingRate`, `nextFundingTime`,
  `fundingIntervalHour` (8), `fundingCap`, `openInterest`, `markPrice`, `indexPrice`, and a native
  `basis` field. ✅ verified (basis currently empty on that pair; present for some).
- `GET /v5/market/funding/history?category=linear&symbol=&limit=200` → paginated (`nextPageCursor`)
  full history. ✅ verified.
- `GET /v5/market/mark-price-kline?interval=60` → hourly mark → basis backfill. ✅ verified.
- Structured JSON strings; rate limits ~50–120 req/min — fine.
- ToS: restricts US.

### D. OKX v5 (SWAP)

Host `https://www.okx.com/api/v5`, keyless.

- `GET /public/funding-rate-history?instId=ETH-USDT-SWAP` → `fundingRate` + **`realizedRate`
  (actual settled)** + `premium`, paginated cursor, 100/query. ✅ verified.
- `GET /public/funding-rate?instId=` → current incl `premium`, `interestRate`, `nextFundingTime`.
- `GET /market/candles?instId=ETH-USDT-SWAP&bar=1H` → hourly perp candles (basis leg). ✅ verified.
- Structured JSON, Decimal-safe strings; public endpoints ~20 req/2 s/IP — ample.

### E. Hyperliquid — finest granularity (raw hourly *samples*)

Host `https://api.hyperliquid.xyz/info` (JSON POST), keyless, no geo blocks in practice.

- `{"type":"fundingHistory","coin":"ETH","startTime":..,"endTime":..}` → **hourly funding samples**
  (verified 1 h spacing) each `{coin, fundingRate, premium, time}`. ✅ — no aggregation needed, and
  `premium` is the perp-vs-index gap the basis feature wants.
- `{"type":"metaAndAssetCtxs"}` → current funding + `openInterest` + `markPx` + `oraclePx`. ✅
- `{"type":"candleSnapshot", "coin":"ETH", "interval":"1h"}` → perp candles (basis backfill).
- History since venue launch (2023 for most majors; BTC/ETH deep). Guidance ~1200 req/min,
  "be polite". ~40–60 coins — enough for majors, smaller than the exchanges above.

### F. ccxt (library) — one API, >100 venues

v4.5.84 (published 2026-09-24), **MIT, ~44k ⭐, actively maintained** (daily releases).

- Unified `fetchFundingRate` / `fetchFundingRates` / `fetchFundingRateHistory` work across A–E and
  dozens more; also `fetchMarkPrice`/market klines for basis.
- **Not packaged in nixpkgs** (verified NOT_FOUND on unstable) → dev-shell friction for the house
  Nix-flake style (needs a `fetchPypi`/pip fallback derivation).
- Heavy single dependency (ships every exchange's metadata; the package is the opposite of the
  small single-purpose modules house style). Numeric fidelity is weaker by default (floats unless
  parse options used).
- Verdict: excellent for a *research/backfill* script, wrong shape for the kraken-python mirror.

### G. Others, checked + rejected / deprioritised

- **Gate.io** `GET /api/v4/futures/usdt/funding_rate` — keyless historical funding. ✅ verified.
  Solid but a fringe venue; would only add signal if cross-venue consensus matters.
- **Bitmex** `GET /api/v1/funding?symbol=XBTUSD` — keyless history from **2016** (verified), but
  declining venue, thin alt coverage.
- **Deribit** `get_funding_rate_history` — keyless 1 h funding, but ETH/BTC/SOL only.
- **CoinGecko** `GET /derivatives` — live per-(venue,symbol) snapshot incl `funding_rate`,
  `basis`, `spread`, `open_interest` (verified) but **no history**, heavy keyless rate caps
  (10–30 req/min), lossy floats. Reject as a source; usable only as a cross-check.
- **Coinglass** — the best public derivatives dashboard, but **API is paid** (no keyless tier).
- **CryptoCompare futures** — funding behind a key + paywall.
- **KuCoin futures / pycoingecko / coincap / openbb** — checked; no keyless funding history or
  overkill.

## Ranked shortlist vs the RL consumer

Scoring weights: directness-to-seam 25 %, backfill depth 25 %, keyless/ToS 20 %, rate-limit/ops 15 %,
output-shape (typed/Decimal-safe) 10 %, house-style fit (nix packaging, small dep) 5 %.

| Rank | Candidate | Direct | Depth | Keyless/ToS | Rate/ops | Shape | House-fit | **Score** | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | **Binance FAPI** (direct REST) | 9 | 10 (since 2020) | 6 (US-blocked) | 9 | 9 | 6 | **8.4** | Funding + basis both deep-backfillable; single clean API; strings. ToS/US-IP is the only real blot. |
| 2 | **Bybit V5** (direct REST) | 9 | 9 | 8 | 8 | 9 | 6 | **8.3** | Same shape as Binance, friendlier ToS (still non-US); native `basis` field. |
| 3 | **Hyperliquid** (direct REST) | 9 | 7 (since 2023) | 9 | 8 | 8 | 6 | **8.1** | Hourly raw samples + `premium`; no aggregation; no geo blocks; shorter history. |
| 4 | **OKX v5** (direct REST) | 9 | 9 | 8 | 7 | 8 | 6 | **8.0** | `realizedRate` distinct from predicted; 100/query paging. |
| 5 | **Kraken Futures** (extend kraken-python) | 9 | 4 (no public history) | 10 | 9 | 9 | 10 | **7.6** | Same venue, USD-quoted, zero new dep/ToS risk; funding history only via authed key or forward poller (ties to Gap 1). Best *hous-style* fit, not best *data* fit. |
| 6 | **ccxt** | 8 | 9 | 8 | 7 | 6 | 3 | **7.1** | One import for everything, but heavy, float-y, not in nixpkgs. Research tool, not the production mirror. |
| 7 | Gate.io | 8 | 8 | 8 | 7 | 8 | 5 | ~7.2 | Fringe-venue redundancy only. |
| 8 | Bitmex | 8 | 10 | 9 | 6 | 7 | 5 | ~7.0 | Deepest history, declining venue. |
| 9 | Deribit / CoinGecko / Coinglass / CryptoCompare | ≤6 | varies | varies | — | — | — | ≤6 | Rejected above. |

## Recommendation

**Primary: Binance FAPI, direct unauthenticated REST** (not via ccxt). Rationale:

- Only (joint-)winner on **both** data needs of the RL consumer: (1) **backfill** for
  train/backtest — `fundingRate` history to 2020 and `markPriceKlines` hourly basis, both keyless
  and paginated; (2) **live hourly refresh** — `premiumIndex` in one call per ticker. Every serious
  rival (Bybit/OKX/Hyperliquid) scores within ~0.3, but none beats it on backfill depth + a
  single-spec surface.
- The **one caveat to record as a decision risk**: US IPs are geo-blocked and the self-cert is
  US-restricted. Audit is conducted from a non-US host; the architect must confirm the deployment
  host's jurisdiction before commit. **If that fails, the fallback is Bybit V5 or Hyperliquid**
  (functionally equivalent, no US block in practice).

**Complementary second leg (recommended regardless): a Kraken-Futures live poller.** Because the
pipeline's own spot is Kraken and its `ETH/USD` OHLC is the basis denominator, polling
`tickers` for `PF_ETHUSD` each hour gives a same-venue, USD-quoted `fundingRate`/mark/index/OI
snapshot with zero ToS surface and zero new dependency. It cannot backfill, so it pairs with the
Binance/full-history backfill for train and serves the live engine. This mirrors the house pattern
of a long-running poller → NixOS module (systemd timer), which also dovetails with **Gap 1**
(local store) since the poller is the component that eventually owns persistence.

**Skip ccxt** for production: it fails the house fit (huge dep, not in nixpkgs, float-fidelity) for
every benefit that a ~250-line thin REST module gets for Binance alone; keep it in mind only for a
one-off cross-venue backfill script.

## House-style mapping if built (for architect / builder)

- Small project mirroring `ticker-news-signals` + `kraken-python`: `pyproject.toml`, Nix flake dev
  shell; NixOS module only for the live poller. Small modules
  `transport/client/manager/models/errors/logging_config`, manager facade, `export.py` registry.
- Fit inside `kraken-python` instead: add a `derivatives.py` module (a `KrakenManager.derivatives_tickers()`
  / `funding_rate()`), a `FundingRate` dataclass (raw `Decimal`/`str`, `to_dict()`), and an
  `export.py` extractor `"funding"` → `extract(mgr, "funding", symbol="PF_ETHUSD")` →
  `write_jsonl`. This matches the latent `recent_trades`/`spread` methods already there
  (`manager.py:187,199`).
- Output records: `{ticker: "ETH/USD", timestamp: <ISO UTC hour>, funding_rate: <str>,
  perp_basis: <str>, cumulative_funding_24h: <str>, open_interest: <str>}` — hour-floor join via
  `merge_extra_features`; widen `_SIGNAL_COLUMNS` (`rl/features.py:30`) to forward the new columns.
- `.env.example` for optional `FUTURES_API_KEY` (Kraken authed backfill) + `BINANCE_*`/client
  config; structured JSON logging with secret redaction as in the foundation package.

RESEARCH COMPLETE

# RESEARCH — GAP 3: Social / search-trend

Audit phase, Researcher 3 (social/search-trend exogenous signals). Read-only. The seam stays exactly
what GAP 2 described — `rl/data.py:merge_extra_features` hour-floor left-join + ffill/zero-fill, with
`rl/features.py:_add_signals_features` forwarding any numeric column already present; feeding a new
source is a one-line `_SIGNAL_COLUMNS` widening. The mandate: **find a genuinely new modalitiy vs the
finished GNews+VADER news pass (headline sentiment)** — i.e. search volume, social mention velocity,
aggregate sentiment indices. As with the news pass, prefer keyless, respect rate limits/ToS.

## What the consumer needs (per candidate)

1. A **per-(ticker, hour) scalar** (int/float) that `merge_extra_features` can consume — so *any*
   candidate that reduces to "count/score per symbol-hour" qualifies without touching the feature
   pipeline.
2. **Backfill** deep enough for `train`/`backtest` (kraken-python `fetch_ohlc_dataframe(pages=6)` ≈
   180 d) and a cheap **live hourly refresh** for paper trade.
3. Keyless preferred; structured JSON > scraped HTML; no gated geo; maintenance stays alive.

## Candidates

### A. StockTwits v2 API — keyless, official, per-ticker retail chatter  ✅ best fit

Host `https://api.stocktwits.com/api/2/...`, **keyless read** (verified live this session, no auth
header, 2026-09-28).

- `GET /streams/symbol/{BTC.X|ETH.X}.json` → paged message stream; each message has `created_at`
  (ISO UTC) + body; crypto symbols exist natively (`BTC.X`, `ETH.X`, cashtag `$BTC`). ✅ live-verified.
- Per-message `entities.sentiment` is set when the author tags **Bullish/Bearish** (null often) →
  derive tilt from tagged messages; aggregate followers via `symbol.watchlist_count`
  (682,030 for BTC in the live sample) and `symbol.sentiment_change` / `volume_change` where present.
- Pagination via `since`/`max` cursors → **hourly grouping is trivial** (count messages, ratio
  bullish/bearish) and backfill reaches the ~60–180 d window (page until the cursor damps out).
- Auth model: **no key required for public streams**; a free OAuth app key raises limits
  (registration currently paused pending API review — existing keys keep working; unauthenticated
  read continues). RapidAPI mirrors sell the same data at ~1,000 req/h free tier.
- Output: structured JSON, timestamps clean. Numeric counts derived locally → int-typed.
- License/ToS: data is user-generated retail posts; attribution link requested; personal/non-sale
  use is conventional. Risk low.
- Rate limits: unauthenticated read is throttled (~tens of req/h is the observed ceiling for a
  single IP) — plenty for hourly pull of the bot's ticker set (a handful of calls/h) plus a
  backfill run. No geo block. This is the closest keyless analogue of "X/Twitter mention velocity"
  that still works in 2026 (see F).

### B. alternative.me Fear & Greed Index — keyless aggregate sentiment, deepest free history

Host `https://api.alternative.me/fng/`, **keyless**, no library needed (plain GET). ✅ verified live.

- `GET /fng/?limit=N` → JSON array of `{value: "74", value_classification: "Greed", timestamp:
  <unix>}`; `limit=0` returns the **entire series back to 2018-02-01** (verified `limit=10` returns
  back-filled daily points). ✅
- Market-wide (not per-ticker) crypto sentiment composite (volatility, volume, social, survey,
  dominance, trend sub-indices weighted into one 0–100 score) — a *different* modality from
  headline sentiment and from per-ticker social velocity.
- Cadence: **daily** value (updated every ~24 h; `time_until_update` in latest record). Hour-floor
  ffill on the merge handles the sub-day gap cleanly, exactly like the existing signal ffill.
- Rate limit: their docs ask ~1 call/5 min — a single hourly request (or daily) is trivially within
  it. Zero auth, zero geo, zero ToS friction (official public API, explicitly offered for apps).
- Historic free **backfill to 2018** — best-in-class for Gap 3 — the whole series in one request.

### C. pytrends / Google Trends — search interest, keyless but ToS-grey and stale

PyPI `pytrends` 4.9.2 (last release **2023-04-13**, README says *"Looking for maintainers!"*),
Apache-2.0. Unofficial pseudo-API; **scrapes Google's trends JSON** — ToS-grey, IP blocks, unknown
ratelimits (one report: ~1,400 seq requests before a block; sleep ≥60 s once limited).

- `interest_over_time()` (daily 0–100, relative) and `get_historical_interest()` (**hourly**, one
  ~weekly-request per keyword — ≈9 req/ticker for a 60 d backfill, fine; live refresh `now 1-H`/
  `now 4-H` is 1 req).
- Best at **`gtrends_search_interest` per (ticker, hour)** — true search-volume modality the gap
  names. Caveats: keyword disambiguation (ETH vs eth-name, DOGE memecoin noise → pin Google *topics*
  via `suggestions()`), values are *relative* (index 0–100, not counts), and the whole thing breaks
  whenever Google changes the backend. Treat as a **research/second-phase** source, not the
  production heartbeat.

### D. LunarCrush — the obvious "crypto social" name, but social is paywalled

API key required (free Lite tier). 2026 checks (third-party pricing pages 2026-04..07):
- Free **Discover** tier = market data only; **social metrics** (Galaxy Score™, social volume,
  per-coin hourly social activity, sentiment) start at the paid Individual plan ≈ **$90/mo**
  (if exactly the right signal, that may be worth it — but it isn't keyless and isn't free).
- If the decision lands on "pay for social", LunarCrush is the strongest per-coin hourly social
  velocity/sentiment product (X + Reddit + YT + TikTok normalized). Flag as the **budget upgrade
  path**, not the default.

### E. Reddit Data API — technically free, practically gated

2026 status (verified via multiple 2026 pricing/terms pages): **free tier = 100 QPM** but
(a) requires app approval, (b) is for non-commercial dev usage — commercial/automated trading use
needs a paid agreement (~$0.24/1 K calls, enterprise floor ~$12 K/mo), (c) **Pushshift is gone** so
deep historical search is restricted to the official premium API surface. Reddit is the highest-value
non-X forum for crypto retail, but the compliance/key-gating makes it a **tertiary** pick under
"prefer keyless".

### F. X / Twitter API — dead for this use in 2026

Since **2026-02** X moved to **pay-per-use** (~$0.005/post read, $0.010/profile, Basic/Pro closed to
new devs, reads capped ~2–3 M/mo). The legacy "free" tier (~100-500 reads/mo) is discontinued for new
applications. No longer a keyless option — documented so the team doesn't re-litigate it.

### G. snscrape — GPL-3.0, backends effectively broken for X/Reddit

Supports Twitter/Reddit/etc., but: X requires auth (scraper is broken since 2023), Reddit routes via
**Pushshift (defunct)**, and it's effectively unmaintained. Copyleft license also clashes with the
house style. Reject for X/Reddit; would only still touch Mastodon/Telegram (not crypto-retail-heavy).

### H. Others checked + rejected / deprioritised

- **Santiment / Socialgrep / Coinalyze / Kaito (Yapper)**: paid aggregators, no keyless tier; Kaito's
  AI-attention is interesting but paid. Free-trial-only → out.
- **CryptoCompare *social*** endpoints: social data removed from the free tier (follower metrics
  were the free offering; social/`stats` now gated).
- **CoinStats public API**: `/public/v1/*` returns 404 in 2026 (API now key + paid). Community-
  sentiment endpoints gone.
- **CoinGecko community_data** (followers/subscribers): free-tier key required, rate-capped
  (10–30/min), follower counts are a weak slow proxy for mention velocity — skip.
- **CryptoPanic**: free key sentiment, but it's **news** aggregation → would duplicate the GNews
  pass. Excluded by scope, as instructed.
- **coincap / openbb / pycoingecko**: no social/search modality of interest.

## Ranked shortlist vs the RL consumer

Scoring weights: directness-to-seam 25 %, keyless/ToS 25 %, reliability/maintenance 20 %,
exogeneity (does it lead retail price action — the gap's thesis) 15 %, backfill depth 15 %.

| Rank | Candidate | Direct | Keyless/ToS | Reliability | Exogeneity | Backfill | **Score** | Notes |
|---|---|---|---|---|---|---|---|---|
| 1 | **StockTwits v2** (keyless REST) | 9 | 9 | 9 | 10 | 8 | **9.0** | Real per-ticker hourly *mention velocity + tilt*; official JSON; no key; retail-native; live-verified. |
| 2 | **alternative.me F&G** | 7 | 10 | 10 | 8 | 10 | **8.9** | Free aggregate sentiment, 2018+ history, zero ToS risk; market-wide + daily (not per-ticker/hour) → ffill. |
| 3 | **LunarCrush** (Lite→paid) | 10 | 3 | 8 | 10 | 4 | **7.1** | Best pure social-metrics product but social tier ≈ $90/mo; free Discover tier excludes the signal. |
| 4 | **pytrends / Google Trends** | 9 | 3 | 2 | 9 | 4 | **5.4** | True search-volume modality, but stale lib, ToS-grey scraping, fragile backends. Second-phase only. |
| 5 | **Reddit Data API** | 8 | 2 | 5 | 8 | 3 | **5.2** | 100 QPM free tier exists but approval + paid-for-production + Pushshift gone. |
| 6 | **X API** | 8 | 0 | 6 | 10 | 2 | **5.1** | Pay-per-use 2026, no free tier for new devs. Documented & rejected. |
| 7 | **snscrape** | 7 | 2 | 1 | 8 | 2 | **4.1** | GPL, unmaintained, X/Reddit backends broken. |
| 8 | Santiment / Socialgrep / Kaito / CryptoCompare-social / CoinStats | ≤7 | ≤2 | — | — | — | ≤5 | Paid or 404s; see H. |

## Recommendation

**Primary: StockTwits v2 keyless REST; complementary second column: alternative.me Fear & Greed.**
Rationale:

- StockTwits is the *only* tier-1 source that delivers the gap's headline modality — **per-ticker
  social mention velocity on a retail-investor venue** — fully keyless, official, structured JSON,
  live-verified this session. It is the closest working 2026 analogue of "X/Twitter volume" (X is
  pay-per-use now). Two derived columns slot straight into `_SIGNAL_COLUMNS`:
  `stt_mention_count` (per-hour message count) and `stt_tilt` (Σ bullish − Σ bearish tagged msgs,
  float in [−1, 1] or raw delta). No feature-pipeline redesign.
- F&G adds a second, orthogonal axis (market-level crypto sentiment index) at ~zero cost, with the
  deepest free history (2018→) for backfill and trivial daily refresh. `fng_index` (0–100, int).
  Together they beat a single paid aggregator on cost, ToS, and redundancy.
- **Phase 2 (optional): pytrends/Google Trends** for `gtrends_search_interest` per (ticker, hour) —
  the real "search volume" leg — built as a deliberately-tolerated scrape (cached, retry-capped,
  short TTL, non-fatal on block), matching how the news pass already treats Google RSS. Do not make
  it the production heartbeat.
- **Do not** build the project on X, Reddit scraping/snscrape, or LunarCrush's paywalled social
  tier. Reddit official API is a reasonable *future* free tier if the 100 QPM allowance gets used
  inside Reddit's terms, but its compliance surface argues against a first build.

## House-style mapping if built (for architect / builder)

- New sibling project (e.g. `kraken-social-signals`) mirroring `ticker-news-signals` /
  `kraken-python`: `pyproject.toml` + Nix flake dev shell; **NixOS module only if a long-running
  poller is wanted** — but both sources are cheap enough for the manual/cron CLI pull like the news
  pass, so a module is optional.
- Small modules `transport/client/manager/models/errors/logging_config`; manager facade exposing
  `manage.social_mentions(ticker)` (StockTwits) and `manage.fear_greed()` (alt.me). StockTwits is a
  plain REST JSON API → fits the existing `transport`/`client` pattern; optional
  `STOCKTWITS_ACCESS_TOKEN` in `.env.example` to raise limits, absent-by-default (keyless).
- `export.py` registry: `extract(mgr, "social_mentions", ticker="ETH/USD")`,
  `extract(mgr, "fear_greed")`; `write_json/write_jsonl`. Typed dataclasses
  (`SocialMentionWindow(ticker, timestamp, mention_count, tilt)`, `FearGreedRecord(value,
  classification, timestamp)`), raw ints/strings, `to_dict()`/`from_json()`; never lossy floats.
- Output records for the merge: `{ticker, timestamp: <ISO UTC hour>, stt_mention_count: <int>,
  stt_tilt: <str>, fng_index: <int>}`. Widen `rl/features.py:30` `_SIGNAL_COLUMNS` by these names
  (and consider reading that tuple from `configs/default.yaml` so future sources stop needing code
  edits — a small hardening the audit already flags as missing for the news seam's schema drift).
- Offline unit tests hit cached fixtures; separate live-verify script (as in the sibling projects);
  structured JSON logging with secret redaction.

RESEARCH COMPLETE

# RESEARCH — GAP 1: Local market-data store / backfill

Audit phase, Researcher 1 (data quality). Read-only; every Kraken API claim below was **verified
live this session (2026-09-28)** against `https://api.kraken.com/0/public/OHLC`, plus nixpkgs
versions verified against unstable. House-style mirror target: `kraken-python`
(github.com/Cairnstew/kraken-python) — small typed modules fronted by a manager facade, `export.py`
extraction registry → JSON/JSONL, Decimal-safe strings, Nix flake dev shell, thin `cli.py`,
offline tests + separate live-verify script.

## The consumer seam (what a candidate must reduce to)

The RL feature path consumes a single pandas `DataFrame` per (ticker, interval):

- `fetch_ohlc_dataframe` (`rl/data.py:200`) → `candles_to_dataframe` → DataFrame indexed by UTC
  `DatetimeIndex`, columns `time, open, high, low, close, vwap, volume, count` (prices float,
  `time`/`count` int). Everything downstream (`FeaturePipeline.compute()`, `TradingEnvironment`,
  `PaperTrader`, `backtest_model`) consumes exactly this object. So the store's job is:
  **`read(pair, interval, start, end) -> that same DataFrame`**, and `append/write` from the
  Candle objects (`kraken_api.models.Candle`, raw-string prices) kraken-python already returns.
- `merge_extra_features` (`rl/data.py:61`) stays untouched — it just joins the store's output onto
  the exogen JSONL seam already proven by the news pass.
- `prepare_episode` (`rl/data.py:265`) already slices a trailing window from whatever DataFrame it
  gets; `TradingEnvironment.reset(options=...)` is unused today — a store + `since`/`until` window
  is exactly what unlocks train/eval split and walk-forward without touching the environment.

Hard requirement: **parquet reads must round-trip to a per-ticker, per-timestamp DataFrame** with a
UTC `DatetimeIndex` — i.e. the store should be built on pandas/pyarrow, not on a foreign
DataFrame type the pipeline would have to convert.

## ⚠️ Verified exchange ceiling (corrects the audit)

Origin of the whole gap is worse than the audit assumed. Live tests this session:

- `GET /0/public/OHLC?pair=XBTUSD&interval=60&since=0` → **721 rows**, first bar `2026-08-29`,
  last `2026-09-28` (the current open candle). This is the **most recent ~720 bars (~30 days at
  1 h)**, regardless of `since`.
- `since=2017…` (epoch `1500000000`) → **identical 721 rows**. `since` is a lower bound for
  *incremental* updates only; per Kraken's own docs "Returns up to 720 of the most recent entries
  (older data cannot be retrieved, regardless of the value of `since`)."
- Following the returned `last` cursor (the bot's `pages=6` loop at `data.py:238-253`) returns
  only the tail rows again (verified: `since=<last>` → 2 rows). **The "~4320 bars / ~180 d" claim
  in AUDIT.md §2 row 4 is not achievable today** — the `/0/public/OHLC` REST endpoint caps depth at
  ~720 candles.

Consequence: **deep history (> 30 d at 1 h, > 5 d at 5 m) is only obtainable by accumulating
forward from now**, or by taking a *second* venue as source-of-truth (different prices). So Gap 1
is not "cache for convenience" — it is **the only way to ever see more than a month of the bot's
own market**. A NixOS module / systemd timer (or an on-every-tick incremental append) is a
prerequisite, not an optional extra, for any deep backfill later (Gap 2/3 signal history included).

(All candles in scope are already-fetched data — the store itself is 100 % keyless; only "seed a
deep backfill now" wants a second source.)

## Storage-engine candidates (ranked vs the RL consumer)

| # | Candidate | nixpkgs (unstable) | License | Engine / shape | Reads straight to pipeline DF? | Verdict |
|---|---|---|---|---|---|---|
| 1 | **pandas + pyarrow** (`df.to_parquet`/`read_parquet`) | pyarrow 24.0.0, pandas 3.0.4 | Apache-2.0 / BSD-3 | columnar parquet, row-group append | **yes — native**, pandas already a dependency | **Recommended core** |
| 2 | **duckdb** (reads the same parquet, SQL windows) | duckdb 1.5.5 | MIT | embedded OLAP over parquet files | yes, via pandas bridge; optional layer on top of #1 | Strong optional add-on |
| 3 | **polars** (scan_parquet, lazy) | polars 1.42.1 | MIT | Rust DataFrame engine | converts to pandas; second DF stack | Skip — pipeline is pandas-native |
| 4 | fastparquet (pure-python parquet engine) | 2026.5.0 | Apache-2.0 | alternative pyarrow backend | yes | Fallback if pyarrow too heavy |
| 5 | `requests-cache` (HTTP-level TTL cache of REST) | 1.3.3 | MIT | sqlite/http-cache of raw responses | no (caches *pages*, not a window store) | Cheap complement in transport; not a store |
| 6 | deltalake (transactional append on parquet) | 1.4.2 | Apache-2.0 | ACID parquet + transaction log | yes | Overkill for a single-writer poller |
| 7 | sqlite3 (stdlib) | stdlib | — | row store | yes, via pandas read_sql | Fine for cursor/metadata; not for candle windows |
| 8 | zarr v3 (chunked arrays) | 3.2.1 | MIT | array store | converts; no row semantics | Wrong shape for OHLCV |
| 9 | h5py / HDF5 | 3.16.0 | BSD-3 | hierarchical array | converts | Legacy; parquet is the better fit |
| 10 | ArcticDB (Man Group) | **not packaged** | **BSL-1.1 (paid for production!)** | LMDB/S3 columnar | yes (pandas in/out) | **Ruled out on licence** — production use requires a paid ArcticDB licence, and it is absent from nixpkgs |
| 11 | ccxt (fetch_ohlcv everywhere) | **not packaged** | MIT | unified exchange REST | no store of its own | Reusable only for *cross-venue* backfill seed; duplicates kraken-python otherwise |
| 12 | freqtrade (its `download-data` layer) | **not packaged** | GPL-3.0 | parquet/feather/sqlite storage + timerange + incremental | yes | Excellent *reference architecture*, but GPL + whole-bot; do not vendor |

## Backfill / historical seed sources (for the "deep history today" question)

Because Kraken's REST cannot serve > ~720 bars, any *initial* deep window must come from elsewhere:

- **Binance public data** (`data.binance.vision`, keyless, deep klines daily+zips, per-symbol/interval
  CSV) — free, huge, and **different venue** (Binance prices ≠ Kraken prices). Legal to download;
  feeds a generic `shape = same DataFrame`. Good for volume/tech-feature training, wrong venue for
  venue-consistent RL.
- **CryptoDataDownload** — Kraken store is **"currently unavailable"** as of today (verified), so
  not a current Kraken seed.
- Kraken's own **WebSocket `ohlc` channel** (`kraken-python/websocket.py` supports it) — a natural
  *forward* recorder: subscribe, append each committed candle, no REST polling at all. This is the
  cleanest long-lived poller shape and can back the multi-interval accumulation for paper-trade and
  train windows.

Recommendation on seed: **do not buy/harvest a second venue initially** — stand up the store +
forward poller first; the bot's realism (venue-consistent Kraken prices) comes from accumulation,
and 30 days at 1 h is already enough for the current ~24-48-bar feature windows to train on.

## What a managed local parquet store should look like (house-style mapping for builder)

- New sibling project `kraken-market-data` (or a `market_data/` module inside this repo —
  *Architect's call*; sibling mirrors the news project and keeps the store independently testable).
  `pyproject.toml` + Nix flake dev shell (pandas, pyarrow, duckdb-optional, pytest).
- `pyarrow.dataset` parquet store laid out as `store/<pair>/<interval_min>/YYYY-MM.parquet`
  (month-sliced) or one file per (pair, interval) with appended row groups; `since`-cursor state per
  (pair, interval) kept in a tiny `store/_meta.json` or sqlite sidecar.
- Manager facade `MarketDataStore`:
  - `read(pair, interval, since=None, until=None) -> pd.DataFrame` (UTC DatetimeIndex) — direct
    input to `fetch_ohlc_dataframe`'s consumers via a thin adapter `read_ohlc_dataframe(...)` that
    keeps the existing signature, so `train`/`backtest`/paper call sites barely change.
  - `upsert(pair, interval, candles) -> int` (append row groups, dedupe on bar timestamp — the
    `fetch_ohlc_dataframe` loop already dedupes with `~index.duplicated`, `data.py:261`).
  - `update(pair, interval)` — fetch since last cursor, `KrakenManager.ohlc(pair, interval, since=…)`.
- `export.py` registry mirroring kraken-python: `extract(mgr, "ohlc", pair=, interval=, since=, until=)`;
  `write_json/write_jsonl` for small snapshots; `verify_store(pair, interval)` (gap scan vs expected
  bar regularity) as a `researcher-grade` data-quality gate.
- `cli.py`: `market update --pair ETH/USD --interval 60`, `market backfill --since 2026-08-01`,
  `market verify`, `market stats`.
- **NixOS module + systemd timer** for the long-lived poller: a `services.kraken-market-data`
  module with the existing credentials oneshot (keyless market data needs no key, so the module is
  thin: `StateDirectory`, `DynamicUser`, a `systemd.timers` unit at `OnCalendar=*-*-* *:*:30`
  calling `market update` for configured pairs/intervals, log to journald with the house structured
  JSON logging). An alternative to the *timer* polling is a WebSocket recorder service (see above).
- Offline unit tests against small cached parquet fixtures + a separate live-verify script;
  `.env.example` gains only `MARKET_DATA_DIR` (no secret — public data).

## Recommendation

**Build the store on pandas + pyarrow parquet (nixpkgs-stable, Apache-2.0, zero new licence debt,
native round-trip to the exact DataFrame the pipeline consumes), with a `since`-cursor poller and
the `since`-window surfaced on the CLI.** Make `fetch_ohlc_dataframe`'s disk-less single call
(`data.py:200`) read-through this store (fetch → upsert → read), which fixes all four gaps in one
move: persistent OHLCV cache, incremental backfill, replay/history windowing for backtest and
waln-forward train/eval (via the already-present `since`/`until`), and the paper trader's 60 s
60-day refetch collapses to an append + tail read.

Add **duckdb (MIT)** only if SQL windowing/verification queries earn their weight; it reads the
same parquet in place, so it is zero extra state. Skip polars/ArcticDB/deltalake/ccxt/freqtrade
per the table. `requests-cache` is an optional transport-level coat on kraken-python, orthogonal to
the store.

RESEARCH COMPLETE
