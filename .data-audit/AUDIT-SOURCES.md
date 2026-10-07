# AUDIT-SOURCES.md — data-sourcing / ops layer audit

Auditor: `auditor-sources` (team `audit-pipeline-1008`). Read-only pass over
the data-sourcing/ops layer of `kraken-trading-bot`. Companion to the
price/feature audit in `AUDIT.md`; this file does **not** re-litigate
feature-width questions.

Method: skimmed `tools/`, every `systemd/` unit+timer, `justfile`, `flake.nix`,
every `kraken_api.*` call site (grep), the ranked section of `.data-audit/AUDIT.md`
(§4–§5 only), and the READMEs of the six sibling projects. Evidence is
`file:line`. No code was changed.

---

## 1. Sourcing / ops map

### 1.1 What is scheduled (systemd **user** timers, all local, all hourly)

| minute | timer | producer (ExecStart) | source | cadence | sink |
|---|---|---|---|---|---|
| :17 | `kraken-trading-bot-funding.timer` | `nix run …/kraken-funding-rates#kraken-funding-rates -- pull --pair ETH/USD --append` | Kraken Futures public REST (keyless) | hourly | `signals/eth_usd_funding.jsonl` |
| :23 | `…-news.timer` | `nix run …/ticker-news-signals#ticker-news-signals -- pull --ticker ETH/USD --append` | Google News RSS via GNews + VADER (keyless) | hourly | `signals/eth_usd_news.jsonl` |
| :29 | `…-social.timer` | `nix run …/kraken-social-signals#kraken-social-signals -- pull --ticker ETH/USD --append` | StockTwits v2 + alternative.me F&G (keyless) | hourly | `signals/eth_usd_social.jsonl` |
| :41 | `…-order-book.timer` | `nix run <this repo>#kraken-trading-bot -- record-depth --count 100` | Kraken `/0/public/Depth` (keyless) | hourly | `signals/eth_usd_orderbook.jsonl` |
| :47 | `…-signal-gaps.timer` | `nix develop … python tools/signal_gap_scan.py --channel funding --channel news --channel social --gap-factor 2` | local files only | hourly | exit 1 on gap |
| Mon 04:23 | `…-depth-checkpoint.timer` | `just depth-backup-git <remote>` | git push of depth log | weekly | off-machine copy |

All six carry `Persistent=true`; the four producers stagger minutes so their
HTTP requests do not contend (`news.timer`/`social.timer`/`order-book.timer`
comments; a test parses the OnCalendar lines). Every producer is **forward-only**
— one reading per fire, appended; nothing backfills except funding
(`just funding-backfill`, Kraken `/historical-funding-rates`, ~366 d).

### 1.2 Price / OHLC path (no timer)

- Live REST through `kraken_api`: `KrakenManager.ohlc(pair, interval, since)`
  → `(candles, last)`, paged client-side in
  `kraken_trading_bot/rl/data.py:1120 _page_candles` (default `pages=6`;
  Kraken serves ≤721 bars newest-first regardless of `since`,
  `data.py:1259 REST_OHLC_CEILING_BARS`).
- Store read-through: `read_ohlc_dataframe` (`data.py:1633`) does
  **fetch → `store.upsert` → `store.read`** when `market_data_store` is set,
  else a byte-identical live fetch (`data.py:1765`). `refresh=False` skips the
  fetch and proves coverage (`data.py:1354 _guard_refresh_coverage`).
- Store = sibling `kraken-market-data` (a **flake input**, `flake.nix:7`;
  month-sliced parquet). Seeded from the Binance archive by sibling
  `kraken-deep-history` via manual `just store-seed`
  (`justfile:524`, guarded by `tools/store_guard.py`).
- **No market-data timer exists in this repo's `systemd/`** — the store only
  advances on a read's upsert leg or a manual seed. (`INTEGRATION.md` describes
  a sibling NixOS-module timer; it is not wired here.)
- Live engine loop `kraken_trading_bot/engine.py:66 fetch_market_data` pulls
  `ticker` + `ohlc(interval=60)` + `order_book(count=10)` every
  `interval=60 s` (`engine.py:36,150`) but is the strategy path, not the RL path,
  and no strategy reads the book (`sma.py` reads candles+ticker only).

### 1.3 Signal seam (already-built channels)

`merge_extra_features` (`data.py:633`) left-joins each JSONL onto the floored
bar hour; the allow-list is `_SIGNAL_COLUMNS` (`features.py:94-125`):
news `sentiment_score/article_count/novelty_flag`, funding
`funding_rate/basis/open_interest/funding_rate_prediction/vol24h/spread/bid/ask`,
social `stt_mention_count/stt_tilt/fng_index`, plus freshness
`signal_age_hours/signal_observed` and OHLCV-derived scalars.
All three keys are non-null in `configs/default.yaml:89,124,144`.

### 1.4 Sibling projects on disk — wired vs not

| sibling | provides | wired into bot? |
|---|---|---|
| `kraken-python` | REST+WS wrapper, paper trading, pair catalog | **flake input** (`flake.nix:6`); the only live API client |
| `kraken-market-data` | month-sliced OHLC parquet store + `since`-cursor poller | **flake input** (`flake.nix:7`); read/write via `data.py` store seam; no timer here |
| `kraken-deep-history` | Binance-archive deep OHLCV seeder | **not a flake input**; invoked by `just store-seed` (`justfile:524`) |
| `kraken-funding-rates` | perp funding, basis, OI, vol24h, bid/ask | **not a flake input**; `nix run` hourly timer (`funding.timer`) + `just funding-backfill` |
| `ticker-news-signals` | GNews→VADER per-hour sentiment | **not a flake input**; `nix run` hourly timer (`news.timer`) |
| `kraken-social-signals` | StockTwits mentions/tilt + Fear&Greed | **not a flake input**; `nix run` hourly timer (`social.timer`) |

Note: the four un-pinned siblings are invoked through `nix run <checked-out
path>#<pkg>` deliberately (their units' comments: private repos, avoid freezing
fixes into `flake.lock`), so the working tree, not a lock, is the contract.

### 1.5 Rate-limit / retry / backoff at each call site

- `kraken-python` transport: a **throttle only** —
  `min_interval` default `0.0` (`kraken_api/transport.py:108`), one
  `time.sleep` at `:186`; `_request` re-raises on `RequestException` and
  `RateLimitError` (`:250`). `grep retry|backoff` over `kraken_api/` → **zero hits**.
- Bot consumer fetch: `_page_candles` (`data.py:1154`) has **no try/except**; a
  mid-page failure propagates and every candle already collected is discarded.
- `kraken-market-data` client: has its own min-interval limiter + exponential
  retry/backoff (sibling README) — used on the store fetch leg when a store
  source is passed, not on the default `KrakenManager` path.
- Producers: each sibling has its own retry/backoff; `kraken-social-signals`
  treats HTTP 403 (Cloudflare) as *non-fatal* and exits 0 with partial data.
- Timers: `RandomizedDelaySec=120` on the four producers; signal-gaps is
  deliberately un-randomised.

### 1.6 Artifact state on this host (measured 2026-10-07)

- `signals/eth_usd_funding.jsonl` — 8,914 lines (backfilled + live), last
  2026-10-07T23:00Z.
- `signals/eth_usd_news.jsonl` — **166 lines (~7 d)**; sparse (only hours with
  articles), no historical endpoint.
- `signals/eth_usd_social.jsonl` — 337 lines; same forward-only property.
- `signals/eth_usd_orderbook.jsonl` — 103 records, `depth_fraction 0.011` of a
  8760-h target, `ok:false` with 1 GAP (2.0×) in `.status.json`.

---

## 2. Ranked gap list

Ranked most-direct-to-RL/tick first. Categories are mixed on purpose; evidence
is `file:line`. No single gap is endorsed.

### S-1 — Trade tape + realized spread are wrapped but never called (market microstructure)
**Evidence.** `kraken_api/manager.py:192 recent_trades`, `:200 spread`, and the
whole WS surface (`:365 ws_token`, `:369 public_ws_url`; `SpotWebSocket` in the
sibling README) have **zero call sites** anywhere in the bot (repo-wide grep for
`recent_trades|\.spread\(|SpotWebSocket|ws_token|public_ws_url` → none). The
only recorded microstructure is the depth snapshot (`record-depth`,
`depth_recorder.py`), and `features.py:94-125` `_SIGNAL_COLUMNS` has no
trade-flow / realized-spread column. `features.py` even reserves the name:
*"A future tick-level tape recorder must use `realized_spread_bps`"*
(`features.py:_SIGNAL_BUILDER_INPUT_COLUMNS` comment).
**Feeds RL/tick:** direct — a per-bar signed-volume / order-flow-imbalance and a
realized-spread scalar are exactly the shape the observation already takes.
**Sourcing:** keyless (`/0/public/Trades`, `/0/public/Spread`; WS public
channels keyless). Forward-only for history (same physics as depth).

### S-2 — No retry/backoff on the consumer fetch path; one 429 discards the page (reliability)
**Evidence.** `data.py:1154` `_page_candles` loop has no `try`; the transport
`min_interval` defaults to `0.0` and re-raises (`transport.py:108,250`);
`grep retry|backoff` in `kraken_api/` → zero. The store leg has retry only when
the store's own client is the source, not the default `KrakenManager`.
**Feeds RL/tick:** none to the observation — it stops a run/tick vanishing;
amplified by the 60 s paper loop (`paper_trade.py:65 _FETCH_PAGES=2`,
`:597 interval=60`).
**Sourcing:** none (local; upstream half needs a `kraken-python` lock bump).

### S-3 — Coverage gate misses the depth channel and the news/social holes are permanent (data quality)
**Evidence.** The gate checks only three channels:
`signal-gaps.service:ExecStart … --channel funding --channel news --channel social`.
The order-book log is *not* gated by it (its `.status.json` is written on
write, not checked by a timer), yet it is already `ok:false` with a 2.0× GAP.
`signals/eth_usd_news.jsonl` is 166 lines (~7 d) and news/social have **no
historical endpoint** (`signal_gap_scan.py` docstring), so a hole is
irrecoverable; the 2026-10-04 measurement (social 403 every call, exit 0, 4
holes) is the precedent. `plan` only checks a signal file *exists* and is
ticker-tagged, never that it *covers* the window (`signal_gap_scan.py` docstring).
**Feeds RL/tick:** indirect but load-bearing — a silently-short channel biases
every matrix pinned over the window.
**Sourcing:** funding is backfillable; news/social are not (need extra
redundant producers, not a backfill).

### S-4 — Funding history recovers 1 of 6 columns (data quality of the one live channel)
**Evidence.** `configs/default.yaml:176-189`: `funding_rate` is the only column
in Kraken's `/historical-funding-rates`; `basis`, `open_interest`,
`funding_rate_prediction`, `vol24h`, `spread` "stay 0.0 fill". `signal_observed`
is a per-channel OR, so it reads 1.0 while four of six fields are zero-fill.
**Feeds RL/tick:** the funding group is enabled in the shipped default, so
zero-fill columns are live inputs today.
**Sourcing:** Kraken Futures may expose OI history; basis/vol24h would need
another endpoint or accumulation. Keyless.

### S-5 — Cross-exchange spot basis is never materialised (cross-exchange / crypto-native)
**Evidence.** The store can hold a Binance-USDT seed *or* a Kraken live leg but
never both at once (`data.py:85 SEEDED_STORE_VENUE` vs `LIVE_STORE_VENUE`), and
no basis/spread column exists between them; `AUDIT.md §5` cross-exchange row:
"no candidate — would need new code and new sources". `kraken-deep-history`
already proves Binance archive access is keyless and cheap.
**Feeds RL/tick:** direct — a per-bar venue basis is a scalar the seam accepts.
**Sourcing:** keyless (Binance archive already used; or a second live REST leg).

### S-6 — No scheduled refresh of the market-data store (ops)
**Evidence.** `ls systemd/` has funding/news/social/depth/gaps/checkpoint and
**no** market-data unit; the store advances only when a read's fetch→upsert
runs or a manual `just store-seed` (`justfile:524`). `kraken-market-data/
INTEGRATION.md §2` describes a 30 s poller timer in the *sibling's* NixOS module,
not wired here.
**Feeds RL/tick:** indirect — keeps the store fresh so `refresh=False` reads
are honest.
**Sourcing:** none (add a unit; keyless endpoint).

### S-7 — Depth is thin and coarse: 100 levels, 1 snapshot/hour, 11 days deep (improve existing microstructure)
**Evidence.** `record-depth --count 100` (`order-book.service`), and
`depth_recorder.py:79-93` documents that `count=1000` silently returns 100 and
that `count=500` (~325 MB/yr) buys real depth; `.status.json` shows
`count_requested 100`, `n_records 103`, `depth_fraction 0.011` of 8760 h.
**Feeds RL/tick:** the depth channel has no consumer yet (`order-book.service`
comment: "Nothing reads this file yet"), so this is about building depth worth
consuming.
**Sourcing:** keyless; cadence/`count` trade disk for resolution.

### S-8 — Macro / economic calendar: zero presence (new category)
**Evidence.** `AUDIT.md §5`: "zero presence — no key, no seam column, no
sibling, no code path". No macro/calendar reference in any `.py` or config
(grep).
**Feeds RL/tick:** per-event level/binary scalar per bar, but needs a new seam
column + producer.
**Sourcing:** FRED (free key), most economic calendars paid or scraped.

### S-9 — On-chain / crypto-native: zero presence (new category)
**Evidence.** `AUDIT.md §5`: "zero presence". No on-chain reference in the tree
(grep).
**Feeds RL/tick:** per-bar scalar (exchange netflow, active addresses, gas),
new producer + seam required.
**Sourcing:** mostly free-tier API keys (Etherscan free, Dune/Glassnode paid).

### S-10 — No real-time/WS path; paper leg re-fetches 2 pages/min (improve existing ops)
**Evidence.** WS wrapped, uncalled (see S-1); `paper_trade.py:65 _FETCH_PAGES=2`
inside `_fetch_data()` per 60 s tick (`:597`), rebuilding the whole matrix to
use one row — `data.py` `.. todo::` names "collapse to append + tail read".
**Feeds RL/tick:** decision quality, not a new column.
**Sourcing:** none (WS keyless; local tail-read).

### S-11 — Research / academic signal: zero presence, hardest to operationalise (new category)
**Evidence.** `AUDIT.md §5`: "zero presence". No clean per-bar scalar exists.
**Feeds RL/tick:** weak/unclear without a specific paper→feature mapping.
**Sourcing:** scraping/paid; needs its own scoping pass.

---

## 3. Already built — do NOT re-propose

- `ticker-news-signals` — news → VADER sentiment (Google News RSS), hourly
  timer, seam live (`features.py` news columns).
- `kraken-market-data` — OHLC parquet store + `since`-cursor poller; flake
  input; read/write via `data.py` store seam.
- `kraken-funding-rates` — perp funding / basis / OI / vol24h / bid-ask;
  hourly timer + `just funding-backfill`.
- `kraken-social-signals` — StockTwits mentions/tilt + alternative.me Fear &
  Greed; hourly timer, seam live.
- `kraken-deep-history` — deep OHLCV seeder (Binance archive → store), via
  `just store-seed`.
- Order-book depth recorder (`record-depth`, `depth_recorder.py`) + weekly
  off-machine checkpoint — built and firing.
- Signal-coverage gate `tools/signal_gap_scan.py` + `signal-gaps.timer`.

## 4. Category coverage (what was looked for)

Improve-existing ops: S-2, S-3, S-4, S-6, S-7, S-10. Market microstructure:
S-1, S-7. Cross-exchange: S-5. Macro: S-8. On-chain: S-9. Research: S-11.
Text/news & social: **already built** (see §3); remaining issue is reliability
(S-3), not a missing source.
