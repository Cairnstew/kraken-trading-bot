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

## Research #1 — Social/Search-Trend (Phase 8 refresh)

Researcher-social, Phase 8 audit pass. Re-surveys Gap 3 (social/search-trend) **fresh against 2026-09-28**
with live probes, superseding the Phase-1 GAP 3 section above where facts changed, and widening beyond the
assumed list (added BlueSky/ATProto, Mastodon, Google News RSS, Reddit public-JSON status). Same consumer:
`merge_extra_features` hour-floor left-join + ffill/zero-fill into `_SIGNAL_COLUMNS`
(`rl/data.py:49`, `rl/features.py:32`) — any per-(ticker, hour) scalar or small vector qualifies.

**Live-verified this session** (GET from this environment, 2026-09-28):

| Source | Probe result |
|---|---|
| StockTwits v2 `/streams/symbol/{BTC.X,ETH.X}.json` | Keyless, structured JSON, 30 msgs/page, `created_at` (ISO UTC), `entities.sentiment` (Bullish/Bearish, often null), `symbol.watchlist_count`, `cursor` pagination (=[max/since walk]). ⚠ **Now Cloudflare-fronted**: a bare-script `curl` drew `403 cf-mitigated: challenge`; a browser-like fetch returned full JSON → headless/datacenter use must set a browser UA and tolerate the occasional challenge. |
| alternative.me `GET /fng/?limit=N` | Keyless 200, `access-control-allow-origin: *`, JSON `{value:"74", value_classification:"Greed", timestamp:unix}`, `limit=0` = full series since 2018-02-01. Daily cadence. |
| LunarCrush `/api4` docs + pricing | Key required. **Free Hobby tier = market-data endpoints only (4 req/min, 100/day); social/sentiment/Galaxy Score unlock on paid** (Individual 10 req/min, 2,000/day; full historical time-series on Builder+). Social tier ≈ $90/mo (prior-pass figure). |
| CryptoPanic `/api/v1/posts/` | **403 without auth token** → free key required; it is a *news* aggregator → duplicates the finished GNews+VADER pass. Excluded by scope (audit-consistent). |
| Mastodon public API `v2/search` | Keyless, valid JSON (query returned empty for 'ethereum' on mastodon.social — thin crypto retail surface). |
| BlueSky ATProto `app.bsky.feed.searchPosts?q=bitcoin` | **Keyless**, full JSON posts: `record.createdAt`, `repostCount/likeCount/replyCount`, text; `hitsTotal` cap 10,000; cursor paging. Public firehose also keyless. |
| Reddit public JSON (`old.reddit.com .../new.json`) | **302 redirect** → effectively gated keyless as of 2026 (prior-pass finding re-affirmed: free 100 QPM Data API needs app approval + non-commercial; Pushshift gone). |
| Google News RSS `news.google.com/rss/search?q=` | Keyless XML, but feed ToS is explicit: *"solely for ... a personal feed reader for personal, non-commercial use. Any other use..."* → programmatic/commercial use is ToS-grey, AND it is the same GNews surface as the finished news pass. Excluded by scope. |
| pytrends (PyPI JSON) | v4.9.2 latest, released **2023-04-13**, README: *"Looking for maintainers!"*, Apache-2.0, unofficial pseudo-API scraping Google's JSON endpoint → ToS-grey, IP blocks (~1,400 seq reqs / 60 s sleep once limited), untested backend stability. |
| Santiment GraphQL / Socialgrep | 400 without key / 523 (CF-blocked) — paid, no keyless tier (prior-pass consistent). |

### Candidates

**A. StockTwits v2 — keyless per-ticker retail chatter.** Best fit for the gap's headline modality
(mention velocity + tilt), now with ONE new caveat vs Phase 1: Cloudflare bot-challenge on scripted
HTTP. Mitigation is cheap (browser UA, backoff, hourly handful of calls; optional free OAuth key for
headroom — registration historically paused). Reduce to `stt_mention_count` (int, msgs/hour) +
`stt_tilt` (`(Σbull−Σbear)/(Σbull+Σbear)` over tagged msgs, float) per (ticker, hour); `watchlist_count`
is a slow-moving baseline. Backfill by walking `max`/`since` cursors to ~the 60–180 d train window
(prior pass confirmed this reaches). Structured JSON, no geo-block, retail-native. License: user-
generated posts, attribution requested; we neither store-forward nor resell — low ToS risk. **Best
overall on reduction-cost.**

**B. alternative.me Fear & Greed — keyless market-wide risk axis.** Zero auth, `limit=0` gives the whole
2018+ history in one request; daily cadence (hour-floor ffill on the merge, same as funding). One column
`fng_index` (int 0–100, `value_classification` → Greed/Neutral/Fear can be carried as a string column or
dropped). Different modality from headline VADER and from per-ticker velocity. Cheapest line-item in the
whole gap.

**C. BlueSky / ATProto — NEW keyless "X-like" velocity (phase-2 option).** `app.bsky.feed.searchPosts`
per coin keyword → count/hour + likes/reposts (engagement = a velocity proxy), plus the free public
firehose for full-volume counting. No native bull/bear tilt → derive lexical/emoji tilt or reuse the
VADER machinery from ticker-news-signals. Rate limits ≈ 5,000 public req/day (fine for hourly pull);
ToS acceptable-use only, no bulk resale. Growing network, real post-X retail chatter, but smaller than
StockTwits on investor-native signal and no backfill beyond ~a short window (appview search reaches back,
cursor-limited). Phase-2/second-source.

**D. LunarCrush — best paid product, wrong price tier.** Free tier deliberately excludes the social
signal; social unlocks ≈ $90/mo. If the Decision lands on "pay for social", take LunarCrush (native
per-coin hourly social volume + Galaxy Score + sentiment, official API, MCP-ready); it beats any free
scrape on cleanliness. Not the default under "prefer keyless".

**E. pytrends / Google Trends — true search volume, ToS-grey and stale.** v4.9.2 (2023), maintainer-
wanted. `get_historical_interest()` yields hourly relative 0–100 per keyword (pin Google *topics* via
`suggestions()` to disambiguate ETH/DOGE). ~9 requests per keyword for a 60 d backfill; `now 1-H` for
live. Breakable whenever Google changes the backend; ToS-grey scraping. Same verdict as Phase 1:
**research/second-phase only, never the production heartbeat.**

**F. Mastodon — keyless but thin.** Public REST search works with no auth, but crypto retail presence is
marginal; would add noise, not signal. Deprioritise.

**G. Reddit — still gated.** Public `.json` now 302s; official Data API free 100 QPM requires approval +
non-commercial terms; Pushshift dead. Tertiary under "prefer keyless".

**H. X/Twitter — dead keyless** (2026 pay-per-use; free tier closed to new devs). **snscrape** — GPL-3.0,
X/Reddit backends broken, unmaintained. **Santiment / Socialgrep / Kaito (Yapper)** — paid, no keyless
tier. **CryptoPanic** — key, and it's news (duplicates pass). **Google News RSS** — ToS personal-feed-only
and same surface as the news pass. **CoinGecko community_data / CMC trending / CryptoCompare social /
CoinStats** — follower-count proxies (weak velocity) or de-listed; skip.

### Ranked vs the RL consumer

Weights unchanged from Phase 1 (directness 25, keyless/ToS 25, reliability 20, exogeneity 15, backfill 15).

| Rank | Candidate | Direct | Keyless/ToS | Reliability | Exogeneity | Backfill | Score | Notes (Δ vs Phase 1) |
|---|---|---|---|---|---|---|---|---|
| 1 | **StockTwits v2** | 9 | 9 | 8 | 10 | 8 | **8.9** | Reliability 9→8: Cloudflare bot-challenge now observed on scripted HTTP; Hobby-tier-adjacent. Still the only keyless per-ticker velocity+tilt. |
| 2 | **alt.me F&G** | 7 | 10 | 10 | 8 | 10 | **8.9** | Unchanged; zero-risk, deep-free backfill. |
| 3 | **LunarCrush** (paid social tier) | 10 | 3 | 8 | 10 | 4 | **7.1** | Unchanged; budget-upgrade path only. |
| 4 | **BlueSky / ATProto** | 7 | 9 | 7 | 8 | 3 | **6.7** | NEW. Keyless, ToS-clean, real velocity + engagement; weak backfill + needs own tilt derive. Phase-2 second leg behind StockTwits. |
| 5 | **pytrends / Google Trends** | 9 | 3 | 2 | 9 | 4 | **5.4** | Unchanged; ToS-grey, stale lib, fragile. |
| 6 | **Reddit Data API** | 8 | 2 | 5 | 8 | 3 | **5.2** | Re-verified: public JSON now 302s. |
| 7 | **Mastodon** | 6 | 9 | 7 | 5 | 2 | **5.0** | NEW; keyless but thin crypto surface. |
| 8 | X API / snscrape / paid aggregators / CryptoPanic / GNews-RSS | ≤8 | ≤2 | — | — | — | ≤5 | Dead, paid, or scope-duplicate; see H. |

### Recommendation

**Primary still: StockTwits v2 keyless REST + alternative.me Fear &Greed** — the two cheapest, most
ToS-clean legs, exactly as the audit deferred. Columns: `stt_mention_count` (int/hour),
`stt_tilt` (float), `fng_index` (int). **New since Phase 1, fold in as a free hardening**: (1) the
StockTwits client MUST send a browser-grade UA and treat the Cloudflare challenge as a retryable,
backed-off, non-fatal event (mirror the news pass's tolerance of Google RSS flakiness); (2) read
`_SIGNAL_COLUMNS` from config so this seam stops needing the 2-file code edit
(`AUDIT.md:87-88`, `PLAN.md:151`) — pair with Candidate-2 normalization fix so raw mention counts and
the 0–100 F&G index enter the observation on comparable scales. **Phase-2 (optional): BlueSky/ATProto**
as the genuine "X-like" breadth leg (keyless firehose/search; reuse VADER for tilt); keep **Google
Trends/pytrends** as an explicitly-tolerated scrape only for the literal "search-volume" thesis, never
the heartbeat. **Do not build on** X, Reddit-scrape/snscrape, or LunarCrush's paywalled social tier.

**House-style note (for architect/builder):** sibling project `kraken-social-signals` mirroring
`ticker-news-signals`; manager facade `social_mentions(ticker)` + `fear_greed()`; export records
`{ticker, timestamp: <ISO-UTC hour>, stt_mention_count: int, stt_tilt: float, fng_index: int}`; optional
`STOCKTWITS_ACCESS_TOKEN` in `.env.example` (absent default → keyless + UA-header). NixOS module
optional — both sources are cheap enough for the manual/cron hourly CLI pull like the news pass.

RESEARCH COMPLETE

## Research #3 — On-Chain / Network-Native

Researcher-onchain. Surveys **Gap 5 / Candidate 5** (AUDIT/DECISION/PLAN: "on-chain metrics — backfill-limited
free sources") for the bot's `merge_extra_features` hour-floor seam
(`rl/data.py:49` `_SIGNAL_COLUMNS`, `rl/features.py:32`) — the feed is genuinely absent today
(`AUDIT.md:174-176`). Target input: **per-(ticker, hour) scalar or small vector**, e.g. for the bot's
ETH/USD (and BTC) pairs. Modality family: whale/exchange-flow transfers, network/gas metrics, active
addresses, stablecoin supply. **Every claim below is live-verified from this environment (2026-09-28)
where noted; docs-only items are marked.**

**The reduction problem (why this gap is hard):** per-asset per-hour on-chain metrics do not exist as a
curated free feed. Clean aggregate endpoints are *daily*, and per-hour series must be either (a)
carried forward from daily values, (b) derived from raw per-block/per-transaction data, or (c) bought.
Backfill depth splits cleanly: block-explorer raw reads (Blockchair, Etherscan free) go arbitrarily deep
but are call-count-limited for *bulk* history on free tiers; aggregate vendors (Coin Metrics community,
DefiLlama) go deep but daily-only; whale-specialists (Whale Alert) are paid with a 90-day window on the
mid tier but publish a **free historical archive**.

**Live-verified probe matrix (2026-09-28):**

| Source | Probe result |
|---|---|
| Blockchair API v2 (`api.blockchair.com/{bitcoin,ethereum}/...)` | **Keyless works.** `/stats` → `transactions_24h`, `mempool_median_gas_price`, per-chain totals (ETH+BTC verified). `/ethereum/blocks?q=id(9000000..)` returns historical **2019** blocks with `gas_used` + `transaction_count` (→ hourly gas/tx scalars). `/ethereum/transactions` returns **2022** txs with `value_usd` per tx (→ whale filter client-side). 12 rapid keyless calls all 200 — permissive burst for a 1/h poller. ⚠ API v2 is in **maintenance mode**: every response carries `"documentation": "…/api/docs", "notice": "Try out our new API v.3: https://3xpl.com/data"`, `last_major_update: 2022-11-07`. The `/bitcoin/exchanges` flow endpoint now 404s (deprecated). |
| Coin Metrics **Community** API v4 (`community-api.coinmetrics.io`) | **Keyless.** `timeseries/asset-metrics?metrics=AdrActCnt,TxCnt,CapMrktCurUSD&start_time=2020-01-01` returned daily rows **deep into 2020** (full history on Community). ⚠ **Frequency gate confirmed live:** `frequency=1h` on *any* metric returns `403 forbidden … not available with supplied credentials` → **Community tier = daily only**; hourly is paid tier. |
| DefiLlama stablecoins (`stablecoins.llama.fi`) | **Keyless.** `/stablecoins?includePrices=true` → current + `circulatingPrevDay/Week/Month` per stablecoin per chain (USDT/USDC/USDB…). `/stablecoincharts/ethereum?stablecoin=1` → **daily circulating/minted history since Nov-2017** (verified `1511913600`). Structured JSON, CORS-open. |
| Etherscan / BscScan free tier | **Free API key required** (not keyless). Docs-verified limits: **3 calls/s, up to 100,000 calls/day, selected chains only, PRO endpoints unavailable** (`docs.etherscan.io/rate-limits`). Deep per-call history (blocks, token transfers, `dailytx` stats) but **bulk backfill is call-cost-blocked** — ETH ≈ 7k blocks/day, a year of per-block gas/tx reads ≈ 2.5M calls ≫ 100k/day free. Forward/hourly is trivial. |
| Whale Alert | API **paid**: Alerts **$29.95/mo** (100 alerts/h, personal use, 7-day trial, WS), Enterprise **$699/mo** (90-day history, 500 CPM), Enterprise+ custom (full archive, bulk parquet exports). Keyless `/status`. **Free historical alert archive** (`whale-alert.io/whale-alerts-archive.json.gzip`, **40 MB gzip – live-verified HTTP 200, downloadable keyless**) = whale-alert events back to ~2017, intended for backtesting/ML. |
| web3.py + public archive RPC | `web3` PyPI **8.0.0 (2026-08-31), MIT, actively maintained**. Keyless via free public RPCs (LlamaRPCh/1RPC/erpc). Full chain history through `eth_getBlockByNumber` (incl. `baseFeePerGas`, `gasUsed`) → *your own* hour bucketing. SDK maintained; the cost is engineering (see reduction). |

**Python wrapper maintenance (PyPI, live):**

| Wrapper | Verdict |
|---|---|
| `coinmetrics-api-client` | **2026.9.2.14 (2026-09-02), MIT, active — official Coin Metrics v4 client.** This is the maintained SDK of the community tier. |
| `web3` | 8.0.0 (2026-08-31), MIT, active. |
| `etherscan-python` | 2.1.0, MIT, **stale** vs Etherscan's current V2/onchain-data surface; treat as a thin REST shim you own. |
| `blockchair-python` | 1.0.9 (2020-03), MIT, **unmaintained**; Blockchair v2 is plain GET+JSON → no wrapper needed. |
| `coinmetrics` (unofficial) | 0.2.5, **GPL-3.0** — avoid (license + superseded by official client). |

### Candidates

**A. Blockchair v2 — keyless, deep, gas + whale raw material (ETH & BTC).** Full historical block/tx
reads with `gas_used`/`transaction_count` per block and `value_usd` per tx. **Reduction (LOW cost):**
- network gas/tx scalars per hour = `blocks` grouped by `time` hour → mean `gas_used`, sum `transaction_count` (verified fields present; `base_fee` is null pre-merge, present post-merge — both fine as features);
- whale count/volume per hour = `transactions` per hour range, client-side filter `value_usd ≥ 5M`, group by hour → `whale_cnt`, `whale_vol_usd` per (ticker, hour);
- exchange-flow *derived*: `value_usd` over a labeled exchange-address set gives net in/out per hour (build-your-own label list — Blockchair's `/exchanges` listing is dead).
- **Backfill: DEEP free** (verified 2019 blocks, 2022 txs). ⚠ Maintenance-mode API (2022 last major update; successor 3xpl); no SLA; empty-value filters need client-side filtering (a `value_usd(>…)` filter expression errored — do filtering locally). ToS: free public API, no key; fair-use throttling for anonymous bursts.
- Score: the **best keyless + deep** candidate; the sole caveat is upstream marketing shift to 3xpl.

**B. Coin Metrics Community API — keyless, deep, DAILY network stats, maintained SDK.** AdrActCnt (active
addresses), TxCnt, FeeTotUSD/FeeMeanUSD (≈ gas/fees in USD), CapMrktCurUSD, SplyCur, VelCur*, HashRate —
all chains, full history, structured JSON, official MIT client. **Reduction (LOW cost, granularity
penalty):** `AdrActCnt`/`TxCnt`/`FeeMeanUSD` are **chain-level** (one value per ticker on that chain, not
per-symbol) → hour-floor carry-forward of the daily value into all 24 hours. This is exactly the "market-
wide risk axis" pattern (like alt.me F&G in Research #1). Direct per-asset daily scalars too (`CapMrktCurUSD`).
- **Backfill: DEEP** (verified 2020+ keyless). ⚠ **1h frequency is paid-tier** (403 verified) — a real
granularity miss for an hourly seam; mitigated if the store already carries daily→hour ffill (it does —
`merge_extra_features` ffill/zero-fill).

**C. DefiLlama stablecoins — keyless, deep, stablecoin-supply axis.** `/stablecoincharts/{chain}?stablecoin=N`
→ daily `totalCirculatingUSD`/`totalMintedUSD` per stablecoin since **Nov-2017**; `/stablecoins` → current +
ΔDay/ΔWeek/ΔMonth without history paging. **Reduction (LOW-MED cost):** market-wide liquidity scalar
`stable_supply_delta_24h` (USDT+USDC, maybe on both ETH & any chain), hour-floor carry-forward — a pure
exogenous "deFi-credit" axis. Backfill DEEP + keyless. Minor: no official SDK (bare `httpx` is fine);
`stablecoincharts` needs per-stablecoin ids (the multi-`stablecoin=1&2` call returned empty — page ids singly).

**D. Etherscan / BscScan free key — forward-only hourly poller, shallow backfill.** `getblocknoreceipts`
per block (gas + tx count), `tokentx` filtered by `contractaddress` (whale transfers for a chosen token
ERC-20), `stats/dailytx` (historical daily). **Reduction (MED cost):** same hour-bucket math as Blockchair
but per-block RPC-costed; at ~30 blocks/h sleep to stay under 3 cps, one hour of ETH = ~30 calls — fine
for a forward poller, **unaffordable as a deep backfill** (the audit's stated constraint, confirmed:
100k calls/day cap ≈ ≤2 weeks of full-block history). Best left as the *per-address/entity* engine once a
label hypothesis exists. BscScan: same product/brand, same free tier, so a BNB-chain leg is free too.

**E. Whale Alert — paid live API, FREELY downloadable historical archive.** Live alerts API: keys required,
$29.95–$699/mo, 90-day window on mid tier, full archive only on Enterprise+. **But the historical alert
archive (40 MB gzip, keyless) gives the comparable backfill** for training/backtests: per-event
`{symbol, amount, value_usd, timestamp, blockchain, type}` → **reduction trivial** (`count`+`sum(value_usd)`
of events ≥ threshold, grouped by hour) and the event schema is the same the paid WS would stream forward.
Net: **free deep history now, pay $30/mo only if a forward whale-TWAP alert heartbeat is wanted**; or poll
the free `/status` + public socials with caveats (ToS-grey). Structured JSON; official Python examples
(not a pip lib).

**F. web3.py + free public archive RPC — build-your-own, zero-cost, arbitrary depth.** MIT active SDK +
keyless archive RPC (LlamaRPC etc.) = full per-block gas/base fee/tx-hash history; plus EVM `Transfer` log
decoding for per-token whale counts. **Reduction cost HIGH** (you build the block→hour aggregator, handle
rate limits, chain reorgs) but: unlimited depth, owns the definitions, no vendor lock. Pragmatic split:
**Blockchair for history/backfill + your own archive-RPC hook for the live hour** if verbatim per-hour ETH
gas is required beyond daily resolution.

**G. Paid analytics suites (not keyless, excluded as primary).** Dune (API-key + paid credits), Glassnode
(free plan = delayed/subset, most metrics paid), CryptoQuant, IntoTheBlock, Santiment, Bitquery (free
credits ≠ keyless). Only relevant if exchange netflow or entity-granular flows become a must-have that
Parts A–F cannot derive.

### Ranked vs the RL consumer

Weights from the audit (directness 25, keyless/ToS 25, reliability 20, exogeneity 15, backfill 15).

| Rank | Candidate | Direct | Keyless/ToS | Reliability | Exogeneity | Backfill | Score | Notes |
|---|---|---|---|---|---|---|---|---|
| 1 | **Blockchair v2** (gas + whale raw) | 9 | 9 | 6 | 9 | 9 | **8.4** | Keyless, deep free backfill verified; relic of 2022 maintenance-mode API drags reliability. |
| 2 | **Coin Metrics Community** (daily net metrics) | 6 | 10 | 9 | 9 | 10 | **8.6→ but hourly-gated: 7.4** | Daily-only on free tier (1h = paid, verified); maintained official SDK. Chain-level not per-ticker. |
| 3 | **DefiLlama stablecoins** | 6 | 10 | 9 | 9 | 10 | **8.4** | Keyless, deep to 2017, market-wide liquidity; daily carry-forward. |
| 4 | **Whale Alert free archive** (backfill) + paid WS (forward) | 9 | 5 | 8 | 10 | 10 | **8.0** | Free historical seed is the keyless win; live forward costs $30+/mo. |
| 5 | **Etherscan/BscScan free key** | 7 | 4 | 9 | 8 | 2 | **6.6** | Forward-poll only; bulk backfill call-blocked (reaffirms audit constraint). |
| 6 | **web3.py + archive RPC** | 8 | 8 | 7 | 9 | 10 | **8.1** | Highest build cost (score assumes shipped); ownership of definitions. |
| 7 | Dune/Glassnode/CryptoQuant/... (paid) | 8 | 1 | 8 | 9 | 5 | **5.9** | Only for exchange/netflow that A–F can't derive. |

### Recommendation

**Primary: Blockchair v2** for the on-chain heartbeat — keyless, deep free backfill (verified), native
`value_usd`/`gas_used` → trivial reduction to the seam:
`{ticker, timestamp: <ISO-UTC hour>, gas_mean_gwei, tx_count, whale_cnt, whale_vol_usd}`; for the bot's
pairs it is **ETH on Ethereum + BTC on Bitcoin** in one API. **Add as free second legs:** Coin Metrics
Community (`adr_act_cnt` daily, official MIT client) and DefiLlama stablecoins (`stable_supply_delta_24h`)
— both carry-forward cleanly through the existing `merge_extra_features` ffill. **Seed history with the
Whale Alert free archive** only if a mean-whale-reversion thesis needs pre-2023 context; do not pay for
the live whale feed unless a forward alert heartbeat is wanted (then $29.95/mo Alerts WS is the entry).
**Explicitly note:** Exchange-Netflow is **the one axis with no keyless deep source** (Blockchair's flow
endpoint is dead, verified) — either derive it from labeled-address filtering over Blockchair history
(build cost, no vendor) or treat it as the paid-suite upgrade path. Backfill-depth verdict per the audit's
fear is **overturned for Blockchair** (deep reads are free and keyless); it stands for Etherscan-free.

**House-style note (for architect/builder):** sibling project `kraken-onchain-signals`; manager facade
`onchain(ticker, hour)` reading Blockchair blocks+transactions for ETH/BTC; export record
`{ticker, timestamp, gas_mean_gwei, tx_count, whale_cnt, whale_vol_usd, adr_act_cnt, stable_supply_delta_24h}`;
Decimal-safe strings already (Blockchair numerics are JSON strings written verbatim); optional
`COINMETRICS_COMMUNITY`/`ETHERSCAN_API_KEY` in `.env.example` (absent → keyless paths only). NixOS timer
for the hourly pull (mirrors `kraken-market-data`). Offline tests against cached Blockchair fixtures +
live verify script, same discipline as the completed passes.

RESEARCH COMPLETE

---

## Research #2 — Market Microstructure

Audit phase, Researcher 2 (market microstructure). Read-only. REST claims **verified live this session**
(2026-09-28) against production; WebSocket v2 schemas **verified against the live docs site** the same
session; the house lib (github.com/Cairnstew/kraken-python) already ships REST + WS v2 wrappers for every
endpoint below. This section covers Candidate 4 of `.data-audit/AUDIT.md`: sourcing order-book depth,
spread, and trade-flow, and reducing them to per-(ticker, hour) scalars for the dormant `microstructure`
feature group.

## The consumer seam (what any candidate must reduce to)

`_add_microstructure_features` (`kraken_trading_bot/rl/features.py:373-386`) reads **exactly four
columns** off the hourly OHLC frame and derives two features from them:

- `spread` = `(ask − bid) / bid` (or a pre-computed `spread` column if present)
- `order_book_imbalance` = `(bid_vol − ask_vol) / (bid_vol + ask_vol)`
- raw passthrough columns consumed: `bid`, `ask`, `bid_vol`, `ask_vol`

So the hard requirement on any candidate is a **per-(ticker, hour) small vector** — at minimum
`{best_bid, best_ask, bid_vol, ask_vol}` (from which spread + imbalance fall out), ideally also a taker-side
trade flow `{buy_vol, sell_vol, trade_count}`. The group currently yields nothing because the OHLC fetcher
never produces those columns; the engine already polls `order_book(pair, count=10)` on **every 60 s loop and
discards it** (`engine.py:72`, verified) — the cheapest unlock in this whole gap is to stop discarding it.

Feed path options (both already exist in the codebase):
1. **`microstructure` group directly** — write the hour columns onto the hourly bar frame (same seam the
   funding pass used by adding raw columns).
2. `merge_extra_features` JSONL seam (`rl/data.py:61`) — wider reuse, hour-floor join, but requires the
   `_SIGNAL_COLUMNS` widening dance. Group-1 is preferred because the consumer already exists.

## Candidates (labelled by how far each was verified)

### A. Kraken REST — Depth / Trades / Spread / Ticker (house lib) — ✅ verified live today

Host `https://api.kraken.com/0/public/…`, **keyless public market data**.

- `GET /Depth?pair=&count=` → top-N `bids`/`asks` of `[price, volume, ts]`. **`count=500` verified live**
  (5× the engine's need). Engine already fetches `count=10` every loop.
- `GET /Trades?pair=&since=` → up to 1000 most-recent trades `[price, vol, ts, side(s/b), ord_type(l/m), …]`;
  `since` = last trade id cursor. ✅ live.
- `GET /Spread?pair=` → stream of `[ts, bid, ask]` samples (a ready-made spread history). ✅ live.
- `GET /Ticker?pair=` → best bid/ask with depth, 24 h vwap, trade count. ✅ live.
- Already wrapped: `KrakenManager.order_book/recent_trades/spread/ticker` (`kraken-python/manager.py:185-203`),
  models `OrderBook`, `Trade`, `SpreadPoint`.
- Rate limits: spot REST weighted call counter, ~15–20 req/s tiered; a 60 s or 1/h poller is noise.
- **Output shape:** raw per-poll snapshot. Reduction to hour scalar = trivial pandas math.
- **Reduction cost: LOWEST.** Zero new dependencies, zero new geo/ToS surface, same exchange the bot already
  talks to, and the *poll data is already in hand* at `engine.py:72`. Backfill: shallow (REST Trades/Spread
  keep only a recent window; Kraken has **no keyless public depth history**), so the record is
  forward-accumulating — the same posture Gap 2 accepted for funding.

### B. Kraken WebSocket v2 — `book` + `trade` channels (house lib) — ✅ docs-verified today

`wss://ws.kraken.com/v2`, **keyless for public channels** (a WebSocket token is only needed for private
channels).

- `book` (L2): depths `10 / 25 / 100 / 500 / 1000` (default 10), a subscribe-time **snapshot** then
  incremental `update` messages carrying price-level deltas, each with a **CRC32 checksum** of the top 10 —
  integrity check built in. Multi-symbol subscribe in one request. The `depth=10` default matches the
  engine's `count=10` exactly.
- `trade`: push on every match with taker `side`, `price`, `qty`, `ord_type`, `trade_id`; snapshot = last 50
  trades. **This is the only keyless way to get a clean buy/sell trade-flow split** (REST Trades carry the
  maker/taker side per trade but no accumulation).
- Level-3 `level3` channel also exists (`_CHANNELS` in the house lib) — per-order granularity, overkill here.
- **Already wrapped:** `kraken-python/kraken_api/websocket.py` — `SpotWebSocket.connect_public()`,
  `.subscribe(channel, symbols, depth=…)`, `.iter_messages()`, `decode_message()` (book/trade decoders at
  websocket.py:280-300). Runs `websocket-client`, no new dep.
- Rate limits: no documented cap on public market-data delivery; the only rate-limited messages are the
  subscribe/unsubscribe requests.
- **Reduction cost: LOW** for a small always-on consumer (adds continuous top-of-book sampling + trade flow
  that a 60 s poller aliases). Needs a long-lived process rather than a cron/short systemd timer, and a
  resubscribe/checksum-recovery path.

### C. ccxt — unified cross-exchange depth/trades (third-party, MIT) — maintenance verified

- `ccxt/ccxt`, **MIT**, v4.5.84 (2026-09-24, actively pushed daily), Python ≥3.10, ~44k stars. One unified
  API over 100+ venues incl. Kraken, Coinbase, Binance, Bybit, OKX.
- Keyless public market data. `fetch_order_book` / `fetch_l2_order_book` / `fetch_trades` over REST;
  `watch_order_book` / `watch_trades` (the former ccxt.pro) maintain a **live order-book cache** keyed by
  symbol — that cache is itself the "order-book aggregation library" item (see F).
- **Output shape:** normalized `{bids: [[price, amount], …], asks: […], timestamp, nonce, symbol}` and
  `[ {price, amount, side, timestamp, id} ]` trades.
- **Reduction cost: MEDIUM-LOW** cross-venue. For a **Kraken-only** recorder it re-wraps endpoints the house
  lib already calls natively and drags an 8 MB dependency in — not a net win for the single-venue minimum.
  Its value is deferred: if a second legal venue (Coinbase) is ever joined, this is the library that makes
  the pairing a one-liner.

### D. cryptofeed — streaming feed handler with parquet backends (third-party, AGPL) — maintenance verified

- `bmoscon/cryptofeed`, **AGPL-3.0** (confirmed in LICENSE, v3.0.1, active: pushed within a day, 2.9k stars).
- Handles `Book`, `Trades`, `Tickers`, `BBO`, `Funding`, `Open Interest`, `Liquidation` across Kraken,
  Kraken Futures, Coinbase, Binance, Bybit, OKX. Async; **backends write straight to Parquet**, Redis,
  InfluxDB, Kafka.
- **Output shape:** normalized events (`BookUpdate`, `Trade`); `feed.add_kraken(...)` style config.
- **Reduction cost: MEDIUM** engineering-wise (it does the streaming + persistence for you), but:
  **AGPL-3.0 copyleft** + Cython build + a large dep tree is a poor Nix-flake citizen, and it re-implements
  what the house `websocket.py` wrapper already covers for Kraken. Honorable mention, not the answer.

### E. Cross-exchange depth APIs for a cross-venue leg (Binance / Bybit / OKX / Coinbase)

For a *cross-venue* signal (e.g. Kraken-vs-venue-x spread divergence, or liquidity from a louder book) these
are the pools:

- **Binance**: `GET /api/v3/depth?limit=` (0–5000), `bookTicker`, `aggTrades`, WS partial-depth streams
  (`<symbol>@depth5/10/20@100ms`) and diff-depth. Keyless public. **Reachable from this host (HTTP 200
  verified today) but flagged geo-restricted below.**
- **Bybit**: `GET /v5/market/orderbook` (REST **HTTP 200 verified today**) + `v5/market/recent-trade`; WS
  `orderbook.10/50/200/500`. Keyless public.
- **OKX**: `GET /api/v5/market/books?sz=` (REST **HTTP 200 verified today**) + `trades`; WS `books5/books`.
  Keyless public.
- **🌐 GEO-FLAG (prior research, reaffirmed):** Binance.com, Bybit and OKX all **ToS-restrict / geo-block
  US users** (Binance → separate `binance.us`; Bybit and OKX require self-certification that you are not a US
  person and are shut off from US IPs). Reachability from a non-US host proves nothing about the operator's
  standing. **Treat all three as unavailable for this user** unless a VPN-on-VPS operator posture is
  explicitly accepted — out of scope here.
- **Coinbase Exchange** is the one **US-legal** cross-venue depth companion: `GET /products/{id}/book?level=2`
  (**HTTP 200 verified live today**) and WS `level2` channel — same jurisdiction as Kraken, keyless.
  Reduction cost: MEDIUM — a whole second pipeline for one feature column; value is a sanity/depth-divergence
  feature, not a primary.

### F. Order-book aggregation libraries (the "reduce snapshots to features" item)

- No **maintained, focused** Python package computes spread/imbalance/liquidity features from book data;
  the formulas are the 5 lines already coded at `features.py:378-386`. Anything a library adds is a wrapper
  over the book structure itself.
- The candidates that exist: ccxt's live book cache (`watch_order_book`, MIT, maintained — item C), and the
  two stale ones — PyPI `orderbook` 0.1.2 (**last release 2013**, matching-engine GUI demo), and
  `python-binance`'s `DepthCache` (**deprecated/archived** upstream).
- **Verdict: do not add a library for the reduction.** Capture the raw strip, aggregate in pandas/polars —
  ~15 lines. Keeping the aggregation in-house is also the only way to control the *semantics* of
  `bid_vol`/`ask_vol` (see the top-of-book caveat in the recommendation).

### G. Storage / consolidation pattern (ephemeral snapshots → hourly bar)

Mirrors `kraken-market-data`'s proven layout (`market_data/store.py`: month-sliced parquet + `_meta.json`
cursor, shape-preserving reads) but for **event streams instead of bars** — so two tiers:

1. **Tier-1 (the RL consumer only needs this): poll/feed → aggregate in memory → write ONE row per
   (ticker, hour).** Row shape: `{ticker, timestamp: <ISO-UTC hour>, spread_pct_avg, best_bid_avg,
   best_ask_avg, bid_vol_topN, ask_vol_topN, imbalance_last, buy_vol_h, sell_vol_h, trade_cnt_h}`.
   ~24 rows/day/ticker → parquet slice (or reuse `merge_extra_features`' monthly-sliced store pattern).
   This is *exactly* 24× cheaper than persisting raw 60 s depth strips and directly feeds the dormant group.
2. **Tier-2 (optional raw archive):** if per-poll snapshots are wanted later, append JSONL partitioned by
   hour (cheap appends) and compact to parquet on a cron. **Not required** for the feature group.

Backfill note, same as Gap 2's finding: **no keyless depth-history exists** (Kraken publishes none; REST
Trades/Spread are short windows). The recorder is **forward-accumulating** — a few weeks of runtime fill a
trainable window. Accept this rather than paying for depth history.

## Ranked shortlist vs the RL consumer

| Rank | Candidate | Keyless | New deps | Geo risk | Reduction cost | Verdict |
|---|---|---|---|---|---|---|
| 1 | **A. Kraken REST poller** (house lib) | ✅ | **zero** | none | **lowest** | Unlocks the group tomorrow; data is *already fetched and thrown away* at `engine.py:72` |
| 2 | **B. Kraken WS v2 book+trade** (house lib) | ✅ | zero | none | low | Upgrade path: continuous sampling + the only keyless buy/sell trade-flow split |
| 3 | C. ccxt | ✅ | 1 (8 MB) | none (venue choice is ours) | medium-low | Only worth it the day a 2nd venue (Coinbase) joins |
| 4 | E. Coinbase Exchange depth | ✅ | 1 | none (US-legal) | medium | Optional cross-venue sanity leg; **HTTP 200 verified** |
| 5 | D. cryptofeed | ✅ | heavy + AGPL | none | medium | Capable but **AGPL-3.0** + Cython is not house fit |
| 6 | E. Binance / Bybit / OKX | ✅ | 1–3 | **geo-blocked for US** | medium | Excluded for this user (self-cert/ToS per prior research) |
| 7 | F. order-book "feature" libs | — | — | — | — | None maintained; the math is already in `features.py` |

## Recommendation

**Kraken REST poller (A) as the minimum, Kraken WS v2 (B) as the upgrade — both through the existing
`kraken-python` house lib, zero new third-party dependencies.** The cheapest possible first slice of this
gap is not even a poller: **capture the `order_book(pair, count=10)` result the engine already fetches and
discards at `engine.py:72`** into an hourly aggregator (60 s ticks → 60 samples/hour), and write the
`{bid, ask, bid_vol, ask_vol, spread, order_book_imbalance}` row per (ticker, hour). The `microstructure`
group then lights up with no new fetch at all. For taker-side trade flow (`buy_vol/sell_vol`), B is the
lottery ticket: a `trade`-channel consumer accumulates the buy/sell split that REST polling aliases away.

Encoding caveat the architect owns: `features.py` computes imbalance from whatever `bid_vol/ask_vol` you feed
it, but **single-level top-of-book volumes are noisy** (1-trade walls dominate). Report volumes as
top-N sums (N=10 matches the depth already fetched) so `order_book_imbalance` is a stable feature, not a
spike. Backfill is forward-only (no keyless depth history anywhere); accept that, as Gap 2 already did for
funding.

**House-style mapping (for architect/builder):** extend `kraken-market-data` (or the bot's own poller leg)
with a `microstructure` writer; manager facade `microstructure(ticker, hour)` → row `{spread_pct_avg,
best_bid_avg, best_ask_avg, bid_vol_top10, ask_vol_top10, imbalance, buy_vol, sell_vol, trade_cnt}`; store as
month-sliced parquet + `_meta.json`, exactly the `store.py` contract. Optional WS consumer in a thin
sibling using `websocket.py` + book-checksum validation. Feed the bot the raw 4-column frame per hour
(group 1) or the JSONL seam (group 2). NixOS timer for the poller, systemd service for the WS consumer.

RESEARCH COMPLETE
