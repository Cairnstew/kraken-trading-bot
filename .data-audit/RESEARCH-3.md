# RESEARCH-3.md — G-6: cross-exchange spot basis is never materialised

**Researcher-3, read-only, 2026-10-08. HEAD `ccc23ae`.**
Scope: gap **G-6** (`AUDIT.md:227`, `AUDIT-SOURCES.md:165`) — cross-exchange / crypto-native
category, candidate **NEW-DATA-SOURCE**. The only file written is this one.

**Evidence tiers:** **[M]** measured here (command given), **[C]** cited at a `file:line` or
URL, **[I]** inferred. All network probes were live on 2026-10-08 via stdlib `curl`/`urllib`;
scratch in `/tmp/opencode/g6_basis.py`.

---

## 0. Headline findings (up front)

| # | finding | tier |
|---|---|---|
| **H1** | **Binance archive access is proven and cheap, and Coinbase adds a second deep, keyless venue.** `data.binance.vision` monthly klines for `ETHUSDT/BTCUSDT/SOLUSDT/XRPUSDT` all return **HTTP 200** keyless, one month = **38 KB** (ETH 1h 2020-06). Coinbase Exchange `/products/{id}/candles` is keyless and paginates back to at least **2020-06** (measured). Kraken's own REST stays capped at **721 bars**. | [M] |
| **H2** | **The counter-venue's quote currency decides what the basis MEANS.** Measured on 350 common hourly ETH closes: Kraken–Binance(USDT) basis mean **−2.80 bp**, sd 1.69; Kraken–Coinbase(USD) mean **+0.07 bp**, sd **1.02**; Binance–Coinbase mean **+2.87 bp**. The Binance leg carries a **USDT/USD stablecoin premium** (a level shift), not pure venue dislocation. A *USD-vs-USD* pair (Kraken vs Coinbase) is the clean spot-basis signal; the AUDIT's `+5.41 bp level shift` is exactly this quote-currency offset. | [M] |
| **H3** | **This is orthogonal to the perp `basis` the bot already has.** `kraken-funding-rates` `basis = (markPrice − indexPrice)/indexPrice` — a *futures-vs-index* premium on ONE venue. G-6 is a *spot-vs-spot* dislocation *between* venues. Different numerator, different economics (arbitrage flow / fragmentation, not leverage demand). | [C] `models.py:30` |
| **H4** | **The store cannot hold both venues at once** — it is keyed `store/{PAIR_ID}/{interval}/{YYYY-MM}.parquet` with no venue dimension (`store.py:6-10`, `data.py:96-98`). So the basis must be materialised as a **signal JSONL** through `merge_extra_features`, NOT as a second store leg. This is the cheap reduction and reuses the funding-rates pattern wholesale. | [C] |
| **H5** | **Naming is constrained.** `basis` is already the perp basis (`features.py:99`); `spread` is reserved for the funding bid/ask microstructure and `realized_spread_bps` is reserved for a future tape recorder (`features.py:146`). The new column must be a fresh name, e.g. **`venue_basis_bps`**. | [C] |

---

## 1. Candidate sources (scored)

Scoring 0–5 per axis; **"reduce" = how cheaply it becomes the per-(ticker,hour) scalar the RL
seam consumes** (`merge_extra_features`, `data.py:633`).

| # | source | keyless | depth | rate limit | shape | reduce | **total /25** |
|---|---|---|---|---|---|---|---|
| **C1** | **Binance archive** `data.binance.vision` (deep leg) | 5 | 5 (2017→) | 5 (CDN, gentle) | 4 (zip CSV) | 5 | **24** |
| **C2** | **Coinbase Exchange candles** `/products/{id}/candles` (2nd venue) | 5 | 4 (2020→, 300/req) | 4 (10 req/s) | 4 | 5 | **22** |
| **C3** | Binance REST `/api/v3/klines` (recent leg / top-up) | 5 | 3 (~1000/req, keyless) | 3 (weight 2, 6000/min) | 5 | 5 | **21** |
| **C4** | Kraken `/0/public/Ticker` + `/OHLC` (venue A, already in bot) | 5 | 1 (721-bar cap) | 4 | 5 | 5 | **20** |
| **C5** | Coinbase `/v2/prices/{pair}/spot` | 5 | 1 (current only) | 4 | 5 | 3 | **18** |
| **C6** | ccxt (unified wrapper, 100+ venues) | 5 | 4 | 3 | 4 | 3 (new dep) | **19** |
| **C7** | CryptoCompare / Kaiko / Amberdata | 3 (free key/paid) | 4 | 2 | 4 | 3 | **16** |

**Recommendation: C1 (deep history) + C3 (recent top-up) for the counter-venue, Kraken live
(C4) as the base — and prefer C2 Coinbase as the counter-venue if the goal is a clean USD
basis.** C6 ccxt is the "buy breadth with a dependency" option; the repo has **no ccxt** today
(`pyproject.toml` deps: kraken-python, dotenv, numpy, pandas, gymnasium, pyyaml, SB3) and the
sibling `kraken-deep-history` deliberately ships a **stdlib-only** client [C]. Keeping that
property is worth more than 100 venues here.

### C1 — Binance public archive (the deep leg)
- **Maintenance/licence:** official exchange-operated S3 bucket; the *helper* in
  `kraken-deep-history` is MIT-ish in-repo, stdlib-only. **Auth: none.**
- **URL shape:** `https://data.binance.vision/data/spot/monthly/klines/{SYM}/{tf}/{SYM}-{tf}-{YYYY-MM}.zip`; daily variant also exists. **Measured 200** for ETHUSDT/BTCUSDT/SOLUSDT/XRPUSDT 1h 2020-06/2021-01. ETH 1h 2020-06 = 38,510 bytes.
- **Output shape:** CSV inside zip — `open_time,open,high,low,close,volume,close_time,quote_vol,n_trades,...`. `open_time` is **ms before 2025-01-01, µs from 2025-01-01** (timestamp switch already handled by `client.parse_kline_rows`, `client.py:12-18`).
- **History depth:** full since **2017-08** for ETHUSDT (measured 2017-08-17 kline returned by REST; archive mirrors it).
- **Rate limits:** none documented; CDN-backed, one-time seeds effectively unthrottled. Be gentle [C].

### C2 — Coinbase Exchange candles (clean USD counter-venue)
- **Maintenance:** Coinbase Exchange (formerly Pro) public market-data API, long-lived.
- **Auth: none.** `GET https://api.exchange.coinbase.com/products/ETH-USD/candles?granularity=3600[&start=ISO&end=ISO]`.
- **Measured:** returned **350 rows** for a single 1h request (docs say ≤300; API gave 350); explicit `start`/`end` reached **2020-06-01** keyless.
- **Output shape:** `[time_epoch_s, low, high, open, close, volume]` — newest-first, JSON array. Direct close at index 4; trivial to map to a bar grid.
- **History depth:** paginate `start`/`end` back to listing (≥2020 measured); granularities 60/300/900/3600/21600/86400.
- **Rate limit:** public ~**10 req/s per IP** [C docs]; one 24h/1h backfill per ticker is ~a few dozen requests.
- **Why it matters:** USD quote ⇒ **no stablecoin premium** (H2). This is the single most
  defensible counter-venue for the bot's USD book.

### C3 — Binance REST klines (recent top-up / forward fill)
- `GET https://api.binance.com/api/v3/klines?symbol=ETHUSDT&interval=1h&limit=1000[&startTime=ms]`.
- **Measured:** old kline 2017-08-17 returned; `x-mbx-used-weight-1m: 1` after a 100-bar call (weight 1 for limit≤100). Spot budget 6000 weight/min/IP; klines weight 2 at 500, 5 at 1000.
- **Output shape:** JSON arrays, same column order as the archive CSV; `open_time` in ms.
- Recent-leg uses this; deep history uses C1.

### C4 — Kraken REST (venue A, already the bot's live source)
- `/0/public/Ticker` (current bid/ask/last/vol) and `/0/public/OHLC` (720-bar cap **confirmed measured: 721 rows**). No key. This is the *base* leg — no new dependency, and the bot already holds these bars in its store when live.

### C5 — Coinbase `/v2/prices/{pair}/spot`
- Keyless, current spot only, no history. Useful only as a **live** basis tick (e.g. for the
  paper-trade observation), not for training history. Lower score because history is what the
  RL episode needs.

### C6 — ccxt
- Unified `fetch_ohlcv(symbol, timeframe, since)` across ~100 exchanges, keyless for public
  data. **New dependency** (`pip install ccxt`), heavy, and the repo + siblings are deliberately
  stdlib/minimal. Wins if the decision wants ≥3 venues now; loses on the "cheapest reduction"
  criterion the task weights.

### C7 — Aggregators (CryptoCompare / Kaiko / Amberdata)
- CryptoCompare has a free key tier with historical hourly spot per exchange; Kaiko/Amberdata
  are paid. Higher friction, licensing questions, no advantage over C1+C2 for 2–3 venues.

---

## 2. Computing a per-bar cross-exchange basis

**Definition (USD-USD, the clean form):**
```
venue_basis_bps(t) = 1e4 * (close_A(t) - close_B(t)) / close_B(t)
```
with `A = Kraken` (the venue the bot trades), `B = Coinbase` (or Binance). Positive ⇒ Kraken
richer than B. Measured 350-bar series: Kraken–Coinbase mean +0.07 bp, sd 1.02 bp (H2) — a
zero-mean, ~1 bp dislocation that z-scoring makes a usable regressor.

**Grid alignment (the real work):**
1. **Floor both venues to the hour** — the merge seam already floor-truncates to the hour
   (`data.py:698-700`), so producing on floored UTC hour stamps matches it exactly.
2. **Timestamps** — Kraken OHLC `time` is **epoch seconds**; Binance `open_time` is **ms (µs
   post-2025)**; Coinbase is **epoch seconds**, newest-first. Normalise all to UTC epoch seconds,
   then `// 3600`.
3. **Missing bars** — the join is a **left join onto the OHLCV frame**, so a missing counter-venue
   bar yields no row; the seam then forward-fills for a **bounded** window and writes
   `signal_observed=0.0` / `signal_age_hours=-1.0` when there is none (`data.py:675-684`). Do
   **not** invent a 0.0 basis for a missing bar — the freshness pair is exactly the mechanism
   that makes absence visible. An inner-join materialisation is wrong here.
4. **Timezone** — every source is UTC; keep it that way (no localisation anywhere).

**Libraries:** none needed. It is `close_A - close_B` over two aligned integer-hour keys.
`pandas` (already a dep) suffices; ccxt/CryptoCompare would only replace the *fetch*, not the
*alignment*. There is no maintained "cross-exchange basis" library that does the alignment for
you in a form cheaper than ~30 lines — the signal is 3 lines of arithmetic on a merge that
already exists.

---

## 3. Spot basis vs perp/funding basis — why it is orthogonal

| | perp `basis` (already merged) | **`venue_basis_bps` (G-6, new)** |
|---|---|---|
| formula | `(markPrice − indexPrice)/indexPrice` | `(spotA − spotB)/spotB` |
| venues | **one** (Kraken Futures) | **two** (Kraken spot vs Coinbase/Binance spot) |
| economics | leverage/funding demand, expiry pull | venue fragmentation, arbitrage flow, regional order-flow imbalance |
| source | `kraken-funding-rates` `basis` (`models.py:30`) | new producer |
| cadence | hourly, forward-only history (366 d for rate only) | hourly, **deep history via archive** |

They share a *name* family but not a numerator: perp basis is a within-venue futures/spot
premium; venue basis is a between-venue spot dislocation. A bar can have zero perp basis and a
wide venue basis (and vice versa), so the column adds an independent dimension rather than a
collinear one. The funding sibling already deliberately refuses to merge
`relative_funding_rate` for collinearity (`models.py:182-184`) — the same discipline should
apply here: keep `venue_basis_bps` distinct from `basis`.

---

## 4. Reduction to the RL seam (per-ticker, per-timestamp scalar)

**Shape the seam accepts:** a JSONL record `{"timestamp": ISO-8601 UTC, "ticker": "ETH/USD",
"venue_basis_bps": <float>, ...}` — exactly the funding-rates file shape
(`extra_features_file: ~/signals/eth_usd_funding.jsonl`). `merge_extra_features` filters by
ticker, de-dupes to one record per floored hour, left-joins, and carries the freshness pair
(`data.py:633-696`).

**One-line change needed in the consumer** (the only code the bot needs):
- Add `"venue_basis_bps"` to `_SIGNAL_COLUMNS` (`features.py:94-125`). It then rides the
  existing `signals` group into the observation and the normalization stats with **no new
  plumbing** — exactly how `funding_rate`/`stt_tilt` arrive today.
- **Do not** add it to `_SIGNAL_BUILDER_INPUT_COLUMNS` (`features.py:149`); it is a passthrough
  signal, not a builder input.
- **Provenance:** declare it point-in-time like the other exogenous columns; the zero-fill vs
  `signal_observed` discipline (`features.py:151-173`) already covers the sparse-producer case.
- **Naming:** `venue_basis_bps` avoids the `basis` and `spread`/`realized_spread_bps` collisions
  (H5). `spot_basis_bps` is an acceptable synonym; pick one before the builder writes tests.

**Producer options, cheapest first:**
1. **Fold into `kraken-deep-history`** (it already has the Binance archive client + symbol map
   `ETH/USD→ETHUSDT`, `utils.py:24-26`) — add a `basis` command that reads the Kraken store leg,
   fetches the Binance/Coinbase leg, and writes the JSONL. One repo, reuses the archive code.
2. **New sibling `kraken-cross-basis`** mirroring `kraken-funding-rates` (stdlib, keyless,
   hourly timer + a `backfill` command over the archive). Cleanest separation; most code.
3. **Compute at the read seam** — rejected: the store is single-venue (H4), so this needs a
   second store root and a store-schema change, i.e. the expensive path the task warns about.

---

## 5. Recommended outcome

**NEW-DATA-SOURCE, medium effort, high directness.** Build a keyless hourly producer that writes
`venue_basis_bps` JSONL for the four seeded tickers, using:
- **Coinbase Exchange candles** (C2) as the counter-venue for a **clean USD basis** (H2), with
  the **Binance archive** (C1) as the deep-history backfill where Coinbase pagination is thinner;
- **Kraken** (C4) as the base leg from the bot's own store;
- consumer change = **one tuple entry** in `_SIGNAL_COLUMNS`.

Top pick if forced to one counter-venue: **Coinbase (USD)** — zero stablecoin contamination,
keyless, deep, and it makes the measured basis a true venue-dislocation signal (mean ~0, sd ~1 bp)
rather than a USDT peg level shift.

**Cheapest viable path to the decision:** extend `kraken-deep-history` with a `basis` subcommand
(reuses the archive client + symbol map), emit the JSONL, add the one column. No new dependency,
no store-schema change, no second live leg required for training history.

**Open caveat for the builder:** confirm Coinbase's per-request candle cap in code (docs 300,
measured 350) and paginate on `start`; and pin the counter-venue's quote currency in the column
semantics — a `venue_basis_bps` computed against Binance-USDT is a *different quantity* from one
against Coinbase-USD, and must be labelled, not silently mixed.

RESEARCH COMPLETE
