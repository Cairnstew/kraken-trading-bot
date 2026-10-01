# RESEARCH-3 — gaps G3 + G4: can anything actually be CONCLUDED from this pipeline?

**Pass:** 2026-10-02 · **Role:** researcher (read-only) · **Gaps:** G3 (thin history) + G4 (measurement power is a literal)
**Headline:** G3's stated caveat ("deep history is Binance-under-a-Kraken-label") is **true but entirely avoidable at zero cost**, because **Kraken publishes its own keyless OHLCVT CSV archive**. And G4's arithmetic inverts the intuition in the audit: **a longer eval window REDUCES power**, so the pinned 456-bar/3-seed design is close to the worst available choice.

---

## 0. What I verified in code myself (not taken from AUDIT.md)

| Claim | Where | Verdict |
|---|---|---|
| `market_data_store: null` makes the deep-history branch dead | `configs/default.yaml:142`; `rl/data.py:949-961` (null → `return fetch_ohlc_dataframe(...)`, byte-identical pass-through); deep branch at `data.py:975-1017` | **CONFIRMED** |
| `~/Projects/kraken-market-data/store` does not exist | `ls` → *No such file or directory*. The sibling repo `~/Projects/kraken-market-data/` exists (`market_data/`, `INTEGRATION.md`, `tests/`), store never seeded. | **CONFIRMED** |
| Window bounds are a CLIP, not a push-down | `rl/data_window.py:212-237` (`_window_mask` masks the *already-read* frame), `:240-272` (`clip_to_window`), `:293-330` (`training_frame`/`evaluation_frame` all call `clip_to_window` first) | **CONFIRMED** |
| `KRAKEN_REST_BAR_CEILING = 720` and `--pages` does not scale it | `tools/model_matrix.py:220-226`; and now **upstream-confirmed**: Kraken's own REST spec says *"Returns up to 720 of the most recent entries (older data cannot be retrieved, regardless of the value of `since`)"* — <https://docs.kraken.com/api/docs/rest-api/get-ohlc-data> | **CONFIRMED (ceiling is absolute, not a paging artefact)** |
| `MIN_REPLICATES_FOR_A_CLAIM = 3` is a static literal | `tools/model_matrix.py:230`; used only as a threshold at `:1239-1246` and `:1302-1310` and in prose at `:2210-2227`. Never compared to any observed spread. | **CONFIRMED** |
| No code compares two groups for overlap | `summarize` (`:315-325`) emits `median/q1/q3/min/max`; `_print_group_table` (`:1874-1898`) prints `median [q1,q3]`. `grep` for any interval-overlap/CI logic → none. | **CONFIRMED** |
| The recorded 672→202→178 run | `tools/model_matrix.py:560-567` quotes verbatim: *"a 672-bar window logged '202 replayable bars', of which 178 were replayed after feature warm-up"* | **CONFIRMED** |
| The `pages_may_undershoot` warning | `tools/model_matrix.py:1374-1397`, using `pages * 300 < span_bars_for(...)` — note the `300`, not `720`; a third divisor the file never explains. | **CONFIRMED (and see §3.4 — this constant is wrong)** |
| Deep history is Binance-under-Kraken-label; DOGE unmapped | `configs/deep-history.example.yaml:9-19`; `kraken-deep-history/kraken_deep_history/utils.py:24-27` maps only `ETH/USD→ETHUSDT`, `BTC/USD→BTCUSDT`, `SOL/USD→SOLUSDT`, `XRP/USD→XRPUSDT`; unknown tickers refused | **CONFIRMED** |
| **NEW — the backtest replay is already deterministic** | `rl/backtest.py:328` `deterministic: bool = True`; `:534` `action = agent.predict(obs, deterministic=deterministic)`. Given a trained model and a pinned window, `backtest_model` has **zero** replay variance. | **CONFIRMED — this changes the whole power analysis** |
| **NEW — the seed is already in every record** | `tools/model_matrix.py:1119-1121` passes `--seed` to `train` (and deliberately not to `backtest`, `:1132-1136`); `BacktestResult.seed` (`:165,190`) is in `REQUIRED_BACKTEST_FIELDS` (`:136`). | **CONFIRMED — paired-by-seed needs ZERO new collection** |

---

## 1. Part A — Seeding deep history: candidate records

### A1. ⭐ Kraken's own OHLCVT CSV archive — **keyless, same-venue, already first-party**

| Field | Value | Evidence |
|---|---|---|
| What | Per-currency-pair OHLCVT CSV, `timestamp,open,high,low,close,volume,trades`, **no header row**, plus a `MANIFEST.json` | support article, <https://support.kraken.com/hc/en-us/articles/360047124832-Downloadable-historical-OHLCVT-Open-High-Low-Close-Volume-Trades-data> |
| Maintenance | Page **last updated 2026-09-16**; archive artifacts `Last-Modified: Mon, 14 Sep 2026 19:12:12 GMT`; quarterly incremental releases | live `curl -sI` + page |
| Coverage | **Each pair from its first trade on Kraken through 30 June 2026** (full dump); incremental quarterly ZIPs (`Kraken_OHLCVT_2026Q1.zip`, `Kraken_OHLCVT_2026Q2.zip`) | page + live `MANIFEST.json` read from the Q2 ZIP |
| Size | Full = 5 × 2,097,152,000 B parts ≈ **10.5 GB**, reassembled, `sha256 fc81b54cba6e12af3e9422dde9416179e6ef76af4831d48d839fbdb43018eaa4`; checksums at `https://assets.kraken.com/marketing/institutions/OHLCVT_Full_PARTS_SHA256SUMS.txt`. **One quarter = 537,866,903 B (538 MB)** | live `curl -sI`: `content-length: 537866903` |
| Intervals | **1, 5, 15, 30, 60, 240, 720, 1440 minutes** — 60 is present | live: `ETHUSD_60.csv` in the ZIP central directory (9,795 entries) |
| Auth | **none** — plain `curl -O` on `assets.kraken.com` | page's own macOS/Windows copy-paste `curl` loop |
| Rate limits | none published; ~2 GB parts on a CDN. I made **2** requests (a HEAD each, then one 538 MB GET) — no throttle encountered. | live |
| Output shape | `timestamp` is **epoch seconds**; `trades` maps 1:1 to the store's `count`; **no `vwap` column** | live read of `ETHUSD_60.csv` |
| Gap behaviour | *"the OHLCVT data only includes entries for intervals when trades happened… gaps are not zero-filled"* | page |
| **Live verification I performed** | Downloaded `Kraken_OHLCVT_2026Q2.zip` (538 MB, keyless) into `/tmp` and parsed it: `MANIFEST.json` = `{"product":"ohlcvt","schema_version":1,"release":"r1","columns":["timestamp","open","high","low","close","volume","trades"],"has_header":false,"coverage":{"start":"2026-04-01","end":"2026-06-30"},"pairs":1399,"files":9793,"rows":35247327,"generated":"2026-08-17"}`. `ETHUSD_60.csv` = **2,184 rows, 2026-04-01→2026-06-30, ZERO gaps**. **`DOGUSD_60.csv` and `XBTUSD_60.csv` both exist.** Row 0: `1775001600,2103.25,2115.26,2095.55,2112.38,1259.76951889,761` | live |

**This single candidate removes the audit's G3 caveat, and removes the DOGE hole.**
- Venue continuity: **perfect** — it is the same venue, and (per the page) "the API equivalent" of Kraken's own OHLC.
- DOGE/USD: `DOGUSD_60.csv` is in the archive. The audit's "DOGE/USD has no mapping and is refused" is a property of the **Binance seeder's ticker map**, not of deep history itself.
- 1,399 pairs / 35.2 M rows per quarter, 30-minute granularity available.

**Two real gaps, both stated plainly:**
1. **`vwap` is not in the archive.** `vwap_dev = close/vwap - 1` (`rl/data.py:668-674`) therefore cannot be computed for archive history. This is the one place where the Kraken-archive option is *narrower* than the Binance seeder (which derives `vwap = quote_vol/volume`). Options, all of which need a decision: (a) declare `vwap_dev` archive-unavailable and drop it from `feature_groups` — honest but shrinks the observation; (b) synthesise `vwap ≈ (h+l+c)/3` — **fabrication, I do not recommend it**; (c) accept a column that is real on the live tail and NaN→0 across history, which re-creates exactly the normalisation-contamination failure quantified in §2. **My recommendation is (a).**
2. **The archive lags.** Coverage ends 30 June 2026; the newest published increment is Q2. The gap between 2026-07-01 and today must come from live REST — which is precisely what `read_ohlc_dataframe`'s existing `fetch → upsert → store.read` leg (`data.py:986-1003`) already does, and it is the *same venue*, so the tail seam is benign. Note the corollary: **the seam is at a fixed calendar date, not at a venue change**, so a rolling window that straddles it sees no venue discontinuity.

### A2. Binance public data archive — **what the seeder uses today; reclassified**

| Field | Value |
|---|---|
| Maintenance | Active. README: *"new `daily` data becoming available the next day and new `monthly` data at the first monday of the month"*; monthly + daily klines for all symbols/intervals |
| Coverage | Spot from 2017; `1s,1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,3d,1w,1mo` |
| Auth | **none** — `https://data.binance.vision/data/spot/monthly/klines/<SYM>/1h/<SYM>-1h-YYYY-MM.zip` |
| Rate limits | none published; per-file `.CHECKSUM` published for `sha256sum -c` |
| Output shape | 12 columns incl. `open_time` (**milliseconds before 2025-01-01, microseconds from 2025-01-01** — the switch the seeder already handles), base `Volume`, `Quote asset volume`, `Number of trades`, `Taker buy …`. Derives `vwap = quote_vol/volume` |
| Licence | Repo **MIT**; separate `TERMS_AND_CONDITIONS.md` for the dataset |
| **Verdict** | **Technically excellent, but the WRONG VENUE for this pipeline.** It is strictly worse than A1 for a Kraken-labelled store: worse venue continuity (§2), no DOGE mapping, and a fee/listing-history asymmetry (Binance spot has no listing fee; Kraken's does) baked into every bar. Retain only as a documented fallback or for a deliberate cross-venue experiment. |

### A3. CryptoDataDownload — keyless, **and has a first-party Kraken section**

- Free CSV/XLSX, **no login, no rate limits, no file-size paywall**; refreshed **daily at 00:00 UTC**; since 2017; daily/hourly/1-min.
- Has **`/data/kraken/`** alongside Binance, Bitstamp, Gemini, Bitfinex.
- Licence: commercial **and** academic use permitted; *"We only ask that you don't redistribute raw datasets to third parties."*
- Paid tiers: Plus+ $39.99/mo, API $79.99/mo, PRO $99.99/mo, QuantDesk $149.99/mo. "Zero-Gap OHLCV" (1-min, Jan 2019→present, with a row-level provenance flag `raw`/`reconciled`/`filled`) is **Plus+**.
- **Verdict:** a genuine second keyless Kraken-venue source, and **daily-fresh** where Kraken's own archive is quarterly — useful for the 2026-07-01→now lag. But it is an *aggregator*, so its "reconciled across alternate venues" rows are a **contamination risk**: provenance flags exist only on the paid tier. Use the free Kraken-only files and verify a sample against `ETHUSD_60.csv` from A1.

### A4. Tardis.dev — tick-level, **not OHLCV**; the right answer for G2, the wrong shape for G3

- Kraken historical data for **all currency pairs since 2019-06-04**; channels `trade`, `book` (L2, `depth=1000`), `ticker`, `instrument`, and (pre-2026-07-10) `spread`.
- **Only the first day of each month is downloadable without an API key** (free samples, e.g. `https://datasets.tardis.dev/v1/kraken/trades/2019/07/01/XBT-USD.csv.gz`). Full history requires a key and (beyond a small free allowance) payment — **exact tiers need a live account check; I did not sign up.**
- 7+ years, GCP europe-west2 (London), >99.9% completeness.
- **Symbol change:** WS v2 uses `BTC`/`DOGE`; v1 used `XBT`/`XDG`; boundary **2026-07-10** (`XBT/USD` → `BTC/USD`, `XDG/USD` → `DOGE/USD`).
- **Verdict:** data must be **re-aggregated from trades into OHLCV** to feed this pipeline. That is strictly more work than A1 for G3. **Its real value is G2**: it is the only source found that carries historical Kraken L2 depth, which is exactly the resource the audit says cannot be reconstructed. Rank it as the G3 answer *below* A1, and flag it to the lead as a **G2** lead.

### A5. CoinAPI / Kaiko / Amberdata — paid institutional; **not evaluable without a sales conversation**

None publishes list pricing for OHLCV history at the tier this needs, and none can be assessed from a public page. CoinAPI covers Kraken natively; Kaiko and Amberdata are institutional order-book/trade vendors. I did **not** open accounts or contact sales. **Recommendation: do not pursue.** A1 is keyless, first-party, and already downloaded and parsed successfully in 30 minutes. Paying for what A1 gives away is unjustifiable here.

### A6. Kaggle / Hugging Face — rejected

Community-uploaded snapshots of Binance/Bitstamp OHLCV. Stale by construction (upload-date-bounded), venue-mismatched for a Kraken store, and licence-varied per dataset. **Not a source for anything load-bearing.**

---

## 2. Part B — The venue-continuity problem, QUANTIFIED (two independent measurements)

I measured this rather than assuming it, using two keyless calls each (Kraken `/0/public/OHLC`, Binance `/api/v3/klines`) plus the A1 archive already on disk. **No rate limits approached; 8 requests total.**

### B1. Measurement 1 — recent 30 days, 720 aligned hourly bars (ETH/USD vs ETHUSDT)

| Quantity | Result | Verdict |
|---|---|---|
| close price diff | mean **+0.032%**, mean\|d\| 0.033%, p95 0.086%, max 0.113% | **negligible** |
| hourly log-return σ | Kraken 0.4890% vs Binance 0.4880%, **ratio 1.0020** | **identical** |
| corr(K[t], B[t]) | **0.999575** (lag ±1: 0.026 / 0.025 — neither venue leads) | **the price path is one path** |
| cross-venue log-price gap | −0.054% → −0.041%, mean −0.032%, per-hour wobble **0.011%** | a stable ~3bp Kraken discount to Binance |
| **base volume K/B** | **median 0.1036 → Kraken is 9.6× smaller**; IQR [0.073, 0.152]; per-bar log-ratio σ **0.554** | **9.6× and 55% multiplicative noise** |
| **trades K/B** | **median 0.0231 → Kraken has 43× fewer prints/bar** (2,264 vs 105,586) | **the tape is a different venue's tape** |
| avg trade size K/B | 0.4951 vs 0.1146 ETH = **4.32× larger on Kraken** | |

### B2. Measurement 2 — 2026-04-01→06-30, 2,183 aligned hourly bars, Kraken **archive** vs Binance (independent regime, 3× the bars)

| Quantity | Result |
|---|---|
| price diff | mean **+0.054%** (o/h/l/c all ≈ +0.05%), mean\|d\| 0.066% |
| hourly log-return σ | Kraken **0.5835%** vs Binance **0.5836%**, ratio **0.9998**, corr **0.999082** |
| **base volume K/B** | **median 0.0537 → Kraken is 18.6× smaller** (9.6× in the recent window — **the gap is regime-dependent, not a constant**) |
| per-bar log-ratio σ | **0.772** |
| **trades K/B** | **median 0.0079 → Kraken is 126× fewer prints/bar** |
| avg trade size K/B | 0.6179 vs 0.0887 ETH = **6.96×** |

### B3. What that does to the actual features — the decisive table

I reproduced the pipeline's own feature builders (`features.py:563-570`, `data.py:668-686`) on a 360-Kraken-bar + 360-Binance-bar splice vs a venue-consistent Kraken-only control:

| Feature | Kraken-only (control) | Binance-under-Kraken-label | Damage |
|---|---|---|---|
| `obv` terminal value (91d) | −3,857 | **−1,025,359** | **266× — and the sign/level is noise** |
| `obv` terminal value (30d) | 24,898 | **460,690** | 18.5× |
| `obv_slope_*` | — | inherits the same scale | **broken** |
| **`volume_zscore_20` at the seam** | max\|z\| 4.35 | **z = +4.29** | **UNCHANGED — the rolling z-score is scale-invariant** |
| **`trade_count_zscore_20` at the seam** | max\|z\| 4.32 | **z = +4.36** | **UNCHANGED — same reason** |
| `volume_change_1` at the seam bar | p99 = 855% | **+4,465%** (30d run: **+1,225%**) | **one bar, 5–31× the 99th percentile** |
| `volume_per_trade` across seam | — | 0.3916 → 0.2083 ETH | 1.9× step; overall median 0.618 → 0.165 |
| **fitted `NormalizationStats` on `obv`** | train max\|z\| 2.6 | **eval max\|z\| 64.5** (30d run: 50.7) | **eval observations land 50–65σ outside the training distribution** |

### B4. VENUE-CONTINUITY VERDICT

1. **Price-derived features are SAFE.** ~5bp constant premium, return-σ ratio 0.9998–1.0020, path correlation 0.9991–0.9996. A constant offset is annihilated by any differencing (`rsi`, `macd`, `bb`, `atr`) or scale-free rolling window (`sma`, `ema`). The audit does not flag these and is right not to.
2. **`volume_zscore_20` and `trade_count_zscore_20` are SAFE — the audit over-warns here.** A 20-bar rolling z-score divides by the window's own σ, so a constant level shift cancels *exactly*. Measured z at the seam (+4.29 / +4.36) sits inside the feature's normal range (max 4.35 / 4.32). I would **not** spend effort here.
3. **`obv`, `obv_slope_*`, `volume_per_trade` and `volume_change_1` are BROKEN at a venue seam.** `obv` is the serious one, because it is a **cumulative** quantity: its terminal level scales with volume, so a 9.6–18.6× volume gap produces a 20–270× OBV discrepancy, and the **fitted z-scores then put evaluation observations 50–65σ outside anything training contained**.
4. **The fix is free and total.** A1 (Kraken's own archive) is the *same venue*, so the volume and `trades` columns are directly comparable to the REST tail. With A1, items 1–3 all vanish and the audit's "caveat a researcher must carry" is discharged. Only `vwap_dev` is lost (§1-A1 gap 1).

---

## 3. Part C — What "the store is a CLIP, not a push-down" costs, and the fix surface

### 3.1 The cost, stated exactly
`clip_to_window` (`data_window.py:240-272`) filters a frame that has already been read. So with `market_data_store: null`:
- `pages` bounds the fetch; `since/until` bound the *result*; the fetch is **unaware of the window**.
- To pin `[since, until)` you must set `--pages` ≥ ceil(span_bars / 720). For the shipped `matrix.example.yaml` 456-bar window that is 1 page — which is why it *appears* to work, and why it silently caps at ~721 bars. It only breaks the moment the window exceeds the ceiling, which is exactly what `pages_may_undershoot` (`:1387-1397`) is warning about.
- **The failure is silent in the wrong direction:** too few pages → a *shorter* frame → the window clip keeps all of it → `n_bars` is small → `min_bar_ratio` may or may not catch it. One recorded run already sat *below* `min_bar_ratio: 0.5` against a naive denominator (`:560-567`).

### 3.2 The fix surface — three small changes, all on existing seams
1. **Push-down when a store is configured** (the cheap, complete fix). `read_ohlc_dataframe` already forwards `since`/`until` to `store.read` (`data.py:1003`). With a seeded store, `pages` should stop governing depth at all — `configs/default.yaml:136-138` already says so: *"`pages` only bounds the live *append*, not the depth served."* **So with A1 seeded, this gap is already closed and the only work is turning the key on.**
2. **Push-down when the store is null** — compute `pages_needed = ceil((until − since)/3600/720) + 1` from the resolved `DataWindow` and pass it to `_page_candles` instead of the caller's `pages`. This cannot actually help beyond 720 bars (the ceiling is absolute, upstream-confirmed), so it is a **guard**, not a fix: it converts a silent short window into a loud one.
3. **Make the harness's constant honest.** `:1385` uses `pages * 300 < span_bars_for(...)`. With a measured ceiling of 721 bars/page, `300` is roughly 2.4× too strict and `720` roughly correct. Change to `KRAKEN_REST_BAR_CEILING` (`:226`) — the constant already exists in the file and is not used here.

### 3.3 Failure modes of a real push-down
| Mode | Mechanism | Mitigation |
|---|---|---|
| **Memory** | a 6-year 1h ETH history is 52,560 rows × 8 cols — trivial (~4 MB). Even the *full* archive (35.2 M rows/quarter, 1,399 pairs) is only a problem if loaded wholesale. | Read month-slices lazily (`store.read` already takes `since`/`until`). Real risk is low at 1h. |
| **API page limits** | `_page_candles` (`data.py:763-771`) has **no try, no retry, no backoff, no sleep** (G5). A long push-down multiplies the chance of hitting a transient failure and losing the whole read. | Any long fetch needs G5's retry/backoff *first*. Do not add push-down without it. |
| **Partial windows** | a fetch that dies at bar 900 of 1,200 yields a frame that clips to a *shorter* window with no error. | Compare `n_bars` against `span_bars_for` and **raise**, not warn. This is the one silent-nonsense failure the audit names. |
| **Rate limiting** | push-down multiplies requests by `pages_needed`. At 720 bars/page, 6 years of 1h ETH = 73 requests. Nothing. | Non-issue at 1h; would matter at 1m (26,280 requests/year). |
| **Feature warm-up** | the recorded 202→178 gap means 24 bars of warm-up. Longer windows amortise this; shorter ones pay it more heavily. | Account for it when choosing L (§4). |

### 3.4 A second, orthogonal push-down is missing and matters for §4
`TradingEnvironment.reset(options=...)` (`environment.py:216-242`) explicitly discards `options`, so **walk-forward is unreachable** (`tools/model_matrix.py` / `data.py:943-947`, `:966-974`). Without it, *one* trained model can only ever be evaluated on *one* eval slice — and §4 shows that one slice is worth ~2–3 independent observations. **Walk-forward is the prerequisite for the entire power story**, and it is ~10 lines of already-signposted code.

---

## 4. Part D — Making power a measurement instead of a literal

### 4.1 The single fact that reframes everything
**`backtest_model` is deterministic** (`backtest.py:328,534`). Given a trained policy and a pinned window, the replay has *zero* variance. Therefore **every bit of the observed 0.3 / 1.0 / 2.7 pp within-config spread is TRAINING variance** (PPO init + minibatch order), and the market path — because the window is pinned — is perfectly *paired* across all cells.

Two consequences:
- **Policy Monte-Carlo averaging is useless here.** Running K stochastic rollouts of one policy would *add* noise unless averaged, and averaging buys nothing over a noiseless single rollout. (I checked this specifically; the default `deterministic=True` already gives a noiseless measurement.)
- **Common random numbers across configs is free.** The `seed` axis already flows to `--seed` on `train` (`tools/model_matrix.py:1119-1121`) and is recorded in every result (`REQUIRED_BACKTEST_FIELDS`, `:136`). Running config A and config B at the *same* seed pairs them. **No new data collection is required to do this.**

### 4.2 The audit's own numbers, turned into a power calculation
Input (from the brief, quoted in the harness at `:96-101` and `:2205-2208`): BTC frictionless, identical config, 3 seeds → 0.3 / 1.0 / 2.7 pp.
→ sample mean 1.3333 pp, **sample SD σ = 1.2343 pp**, IQR 0.7 pp.

MDD at α=0.05 two-sided, power 0.80 = (1.960 + 0.842) × SE = **2.8016 × SE**.

**Table 1 — unpaired two-group comparison** (SE = σ√(2/N)):

| N seeds/group | SE (pp) | **MDD (pp)** | verdict for a 0.25–0.8 pp effect |
|---|---|---|---|
| **3 (today)** | 1.008 | **2.82** | **UNRESOLVABLE — 3.5–11× too big** |
| 5 | 0.781 | 2.19 | unresolvable |
| 10 | 0.552 | 1.55 | unresolvable |
| 20 | 0.390 | 1.09 | unresolvable |
| 40 | 0.276 | 0.77 | marginal |
| 96 | 0.179 | 0.50 | resolves a 0.5 pp effect |
| 200 | 0.123 | 0.35 | resolves |
| 383 | 0.090 | 0.25 | resolves a 0.25 pp effect |

**Required N, unpaired: 38 seeds per group for 0.8 pp; 96 for 0.5 pp; 383 for 0.25 pp.** Times every (ticker × config-pair). Not viable.

**Table 2 — paired by seed (common random numbers).** SE = σ_d/√N with σ_d = σ√(2−2ρ):

| ρ (seed effect shared across a pair) | σ_d (pp) | N for 0.5 pp | N for 0.25 pp |
|---|---|---|---|
| 0.0 (no benefit) | 1.746 | 96 | 383 |
| 0.5 | 1.234 | 48 | 192 |
| 0.7 | 0.956 | 29 | 115 |
| **0.9** | **0.552** | **10** | **39** |

ρ is **unmeasured** — and it is *measurable from records the harness already has*, because `seed` is in every row. If ρ ≈ 0.5–0.7 (plausible: same init, same minibatch order, same bars, only the config differs), pairing halves the seeds needed. **This is the single cheapest power lever available, and it is a ~30-line addition to `summarize`/`cmd_report` with no new runs.**

### 4.3 The second axis the audit did not consider: eval-window L
The audit says "power is a function of depth". Measured on the Kraken archive (2,183 real 1h bars, three assets), **it is not**:

```
ETHUSD  sd(L):  L=24 → 2.58%   L=72 → 4.17%   L=168 → 6.72%   L=336 → 11.42%   L=720 → 15.41%
XBTUSD  sd(L):  L=24 → 1.92%   L=72 → 3.34%   L=168 → 5.86%   L=336 → 10.62%   L=720 → 16.32%
DOGUSD  sd(L):  L=24 → 3.81%   L=72 → 4.82%   L=168 → 8.33%   L=336 → 13.63%   L=720 → 17.23%
sd(L)/sqrt(L) is CONSTANT  →  sigma(L) = sigma_1 * sqrt(L),  sigma_1 ~ 0.40–0.60 %/sqrt(bar)
```

**So a single window's return noise grows as √L.** Lengthening the eval window makes each cell's measurement *noisier*, not quieter. And the effective-information content of a contiguous history is brutal — lag-1 autocorrelation of overlapping window returns:

| L | ETH/USD | XBT/USD | DOGE/USD |
|---|---|---|---|
| 168 (7d) | **+0.993 → 7.2 effective draws** (of 2,016 overlapping) | +0.994 → 6.0 | +0.981 → 19.8 |
| 336 (14d) | +0.995 → **4.3** | +0.996 → 3.2 | +0.989 → 9.8 |
| 720 (30d) | +0.997 → **2.2** | +0.998 → 1.8 | +0.994 → 4.2 |

**The shipped `matrix.example.yaml` window (456 bars ≈ 19 days) is worth roughly 3 independent observations — the same as the 3 seeds it already runs.** That is the whole problem, stated in one line.

**Table 4 — history required for a 0.5 pp MDD on the RAW return** (SE = σ₁·L/√H, n = H/L disjoint windows):

| L (bars) | L (days) | history H needed |
|---|---|---|
| **24** | 1.0 | 35,473 bars ≈ **4.0 years** |
| **48** | 2.0 | 141,891 bars ≈ **16.2 years** |
| 168 | 7.0 | 1,738,160 bars ≈ 198 years |
| 336 | 14.0 | 6,952,641 bars ≈ 794 years |
| 720 | 30.0 | 31,925,392 bars ≈ 3,644 years |

> **Caveat, stated honestly:** that table is for the **raw** return. `excess_return` subtracts buy-and-hold on the *same bars*, which cancels market direction — which is exactly why the harness's `HEADLINE = "excess_return"` (`:218`) is the right choice, and it is now quantified: the raw-return MDD on a 178-bar window is **18.7 pp**, i.e. the market direction would swamp everything. The honest H requirement therefore uses the **measured excess-return window spread, which does not exist yet** — that is precisely the number the harness must start reporting.

### 4.4 Table 3 — combining seeds and windows
SE = √(σ_train² + σ_win²/K)/√N. σ_win (the SD of excess return across disjoint windows) is **unmeasured**; parameterised as a multiple of σ_train:

| σ_win/σ_train | N seeds | K windows | SE (pp) | MDD (pp) |
|---|---|---|---|---|
| 1.0 | 3 | 1 *(today)* | 1.008 | **2.82** |
| 1.0 | 3 | 24 | 0.727 | 2.04 |
| 1.0 | 10 | 12 | 0.406 | 1.14 |
| **0.5** | 10 | 12 | 0.394 | 1.10 |
| **0.5** | 3 | 24 | 0.716 | 2.01 |

Note the shape: **adding windows helps far less than adding seeds** (K=1→24 buys 28%; N=3→10 buys 60%) — because σ_win enters *inside* a root-sum-of-squares with σ_train. So if σ_win is small, **seeds and pairing are the right lever; windows only pay once σ_win is comparable to σ_train.** Which is the first thing the harness should *measure*: σ_win. It currently measures neither.

### 4.5 The concrete harness slice — quantities computable from data ALREADY collected
`REQUIRED_BACKTEST_FIELDS` (`tools/model_matrix.py:124-143`) already carries everything except `fee_rate`-adjacent context. From existing result JSONs the harness can compute, per (ticker, config-axis-level) pair:

| # | Quantity | From what exists today | Formula | Decides |
|---|---|---|---|---|
| 1 | **σ_within (seed SD)** | `summarize` already groups by (ticker, config) | `sd(excess_return)` over seeds | the σ in every formula above |
| 2 | **ρ (pairing gain)** | `seed` is in every record | Pearson r of seed s across the two config levels | whether CRN is worth it; halves N if ρ≥0.5 |
| 3 | **Paired Δ per seed** | same | `r_{A,s} − r_{B,s}` for each shared s | **Wilcoxon signed-rank** over N pairs |
| 4 | **MDD (power-aware verdict)** | σ_within, N | `2.8016 · σ_within/√N` unpaired; `2.8016 · σ_d/√N` paired | **replaces `MIN_REPLICATES_FOR_A_CLAIM`** |
| 5 | **Cliff's δ** | all replicates | `(#A>B − #A<B)/(n_A·n_B)` | nonparametric effect size, valid at N=3 where a p-value is meaningless |
| 6 | **Bootstrap CI on Δmedian** | all replicates | 10k resamples, percentile CI | the direct answer to "do the IQRs overlap" |
| 7 | **Permutation p** | all replicates | all (n_A+n_B choose n_A) label shuffles | exact at small N, no normality assumption |
| 8 | **σ_win (across windows)** | needs walk-forward (§3.4) | sd of per-window `excess_return` for one frozen policy | decides whether more windows beat more seeds |
| 9 | **required N / required H** | (1),(8) | invert Table 1 / Table 4 | turns "add more seeds" into a number |
| 10 | **overlap flag on the table** | q1/q3 already printed | `q3_lo > q1_hi` for every pair of rows | the missing overlap check the audit names |

**The honest negative-result report shape**, which the harness should emit when nothing is resolvable:
> *"Config A and config B differ by 0.31 pp in median `excess_return`. The minimum difference this design can resolve at α=0.05/power 0.80 with N=3 seeds is 2.82 pp (σ_within = 1.23 pp). The observed difference is **9× below the detection floor**: this result is consistent with no effect, with a 0.3 pp effect, and with a 2.7 pp effect. **Required to resolve: 96 seeds/group, or 10 seeds/group paired by seed if ρ≥0.9.**"*
That is a **complete, defensible finding** — and it is available today from results that already exist.

### 4.6 What the harness can and cannot conclude TODAY (3 seeds, ~178-bar eval slice)
- **CAN:** that a difference of ≲2.8 pp in `excess_return` is unresolvable on a single pinned window — stated as a **design property**, not a result. And that in-sample cells and frictionless cells invalidate any tradeable claim (already correct at `:2172-2195`, `:2229-2245`).
- **CANNOT:** rank any two configs within 2.8 pp. Attribute anything to `fee_rate`/`slippage` (effect 0.25–0.8 pp). Say anything about generalization beyond the one window. Compare across tickers as if independent (`assets within a ticker are one market` — already correct at `:2278-2280`).
- **After pairing only (no new runs):** if ρ≥0.7, the floor drops from 2.82 pp to ~1.6 pp — still above the 0.25–0.8 pp effect. **Pairing alone does not fix it.** It must be combined with ≥10 seeds/group.
- **Bottom line: today the harness can conclude a NEGATIVE result honestly, and nothing else.** That is worth stating in the report, because a negative result is a result.

---

## 5. Part E — How G3 and G4 multiply

They are not additive; they are the *same* problem seen from two ends, and G3 is the **enabling** constraint for the only fix G4 has.

1. **G4's σ cannot be reduced by data at all.** σ_train = 1.23 pp is PPO's own variance. More bars do not touch it. The only G4 levers are seeds (N), pairing (ρ), and windows (K, which trades against σ_train).
2. **G3's contribution to a pinned single window is zero variance reduction.** Because the window is pinned, every cell sees the same bars, so the market path is already fully paired. Deep history does **not** reduce σ_within at all.
3. **G3's value is orthogonal and comes from a different mechanism: σ_win.** Depth is the *only* way to estimate (let alone reduce) the across-window component, which is what a claim about "in general" requires. Today σ_win is unmeasured *because* depth is zero.
4. **The multiplication.** With depth = 0, K = 1 is forced. With K = 1, only seeds and ρ are available, and Table 2 says the effect stays unresolvable below ρ≈0.9. With Table 3 saying windows only pay once σ_win ~ σ_train — **which cannot be known without depth — the system is stuck in a loop where each gap is separately unfixable and jointly fixable.**
5. **Explicit arithmetic for the shipped design.** L_window = 456 bars pinned → 137 eval bars → 178 replayed after warm-up. Effective independent draws from one 456-bar contiguous window ≈ **3** (interpolating the §4.3 table). Seeds = 3. Paired-by-window pairing is unavailable (one window). Unpaired SE = 1.2343·√(2/3) = **1.008 pp**; **MDD = 2.82 pp**; effect 0.25–0.8 pp. **The design resolves nothing below 2.8 pp. It is ~3.5–11× short.**
6. **The counterfactual.** Same σ_train = 1.23 pp, but with depth seeded from A1 (years, not 721 bars), walk-forward enabled, L_eval = 168 bars, ρ measured by pairing:
   - ρ = 0.5, N = 48 paired seeds, one window → MDD = 0.50 pp → **resolves the friction effect**.
   - ρ = 0.7, N = 29 paired seeds → MDD = 0.50 pp.
   - unpaired N = 96 at one window → MDD = 0.50 pp.
   None of these is cheap in seeds. **But 29–96 seeds at L=168 bars over a seeded store is ~10–30× less wall-clock than today's equivalent, because a longer window needs no more seeds and short windows amortise warm-up.** And *only* the depth is the hard prerequisite.
7. **And the cheaper answer still stands:** measure ρ and σ_win from the records that already exist, and **report the detection floor**. At N=3 the correct, honest output is a negative result with a required-N, not a median.

---

## 6. Part F — Ranked recommendation, scored on fit to what the RL pipeline consumes

**Consumption shape (this is the scoring key).** The pipeline consumes **one row per (ticker, bar timestamp)**: `time, open, high, low, close, vwap, volume, count` (`rl/data.py:691-727`), consumed positionally by `environment._raw_feature_array`. Anything that must be **re-aggregated, re-timestamped, or reshaped** before that row is cheap; anything that requires a new observation column, a new venue, or a new index is expensive.

| Rank | Action | Fits the consumption shape? | Cost | Why |
|---|---|---|---|---|
| **1** | **Replace the Binance seeder's source with Kraken's own OHLCVT archive** in `kraken-deep-history` (same `seed → upsert` seam, new `client.py` adapter). | **Perfect** — the archive row *is* the store row: `timestamp→time`, `volume→volume`, `trades→count`. Only `vwap` is missing. | **~1 file + 1 ticker map + tests.** Download verified working (538 MB/quarter, 10.5 GB full). | Kills the venue discontinuity **completely** (§2-B4), supplies **DOGE/USD**, and removes the Binance dependency. Zero paid cost. |
| **2** | **Make the harness report a detection floor instead of `MIN_REPLICATES_FOR_A_CLAIM`.** Compute σ_within per group, paired-Δ + Wilcoxon + Cliff's δ + bootstrap CI where ρ>0, and print `MDD = 2.8016·σ/√N` next to every median. | **Perfect** — consumes only `REQUIRED_BACKTEST_FIELDS`, including `seed`, **which is already in every record**. | **~30–60 lines in `summarize`/`cmd_report`, no new runs.** | Converts an unfounded claim into a founded negative result. Highest leverage per line in the entire audit. |
| **3** | **Turn on `market_data_store`** in `configs/default.yaml` once seeded, and set the example matrix window to something the store can serve. | Perfect — a config key that already exists (`default.yaml:120-142`). | 1 line + a seed run. | Makes `since`/`until` real (`data.py:1003`) and gives G4's σ_win something to measure. |
| **4** | **Enable walk-forward** by honouring `TradingEnvironment.reset(options=...)` (`environment.py:216-242`), plus the signposted `data.py:943-947` todo. | Perfect — produces more (ticker, window) rows of the *same* shape. | ~10 lines of already-signposted code, blocked only by G8. | The only way K > 1, i.e. the only way to widen a claim beyond one window. |
| **5** | **Fix `vwap_dev`'s availability** by dropping it from `feature_groups` for archive-backed models (or gating it on a `vwap`-present check — `data.py:668` is already presence-gated, so this is a one-line decision, not a refactor). | Perfect — it removes one column rather than adding one. | ~1 line. | Avoids fabricating `vwap` or reintroducing a normalisation seam on a *different* column. |
| **6** | **Fix the harness's push-down arithmetic:** `:1385` `pages * 300` → `KRAKEN_REST_BAR_CEILING` (`:226`); and raise (not warn) when `n_bars < span_bars_for(...)`. | Perfect. | ~5 lines. | Removes the silent-short-window failure and an unexplained magic constant. |
| **7** | **Add G5's retry/backoff to `_page_candles`** (`data.py:763-771`) **before** any long push-down. | Perfect. | ~20 lines. | Precondition for #3/#4: today one 5xx aborts a multi-thousand-bar read with no resumption. |
| **8** | Use **CryptoDataDownload**'s Kraken CSVs to bridge the 2026-07-01→now archive lag, and as a cross-check on A1. | Perfect — same CSV row shape. | ~1 file. | Keyless, daily-fresh. Verify against `ETHUSD_60.csv` first (paid-tier rows are cross-venue "reconciled"). |
| 9 | **Shorten eval windows, lengthen history.** Retune the example matrix to L ≈ 48–168 bars with many walk-forward segments rather than one 456-bar window. | Perfect. | Config + #4. | §4.3: σ(L) ∝ √L, so a long window is strictly worse per draw. |
| 10 | **Tardis.dev** — defer for G3; **flag to the lead as a G2 lead** (historical Kraken L2 `book` depth, since 2019-06-04). | Poor for G3 (needs trade→OHLCV re-aggregation). | Tiers need a live account check. | Right resource, wrong gap. |
| 11 | CoinAPI / Kaiko / Amberdata. | — | Sales-gated. | **Do not pursue.** A1 is keyless, first-party, and worked first try. |
| 12 | Kaggle / HuggingFace. | — | — | Reject: stale, venue-mismatched, licence-varied. |

### House-style fit for the new sibling work
Rank #1 is a change of *source adapter* inside the existing `kraken-deep-history` package — it already matches the house style exactly (`client.py` / `seeder.py` / `export.py` / `utils.py` / `errors.py` / `logging_config.py` behind one facade, typed models, thin `cli.py`, `pyproject.toml` + Nix flake dev shell, `.env.example` with **no secrets**, offline fixture tests, structured JSON logging). Swapping a Binance `urllib`+`zipfile` client for a Kraken one is a like-for-like module substitution; **no new project is warranted.** Ranks #2 and #6 are changes to `tools/model_matrix.py` in this repo, not a sibling at all.

---

## 7. Part G — What I could NOT determine

| Open question | Why | What would settle it |
|---|---|---|
| **Does the archive's `volume` equal Kraken REST's `volume` bar-for-bar?** | The Q2-2026 archive covers Apr–Jun 2026; Kraken REST only serves the most recent 720 bars, so there is **no overlap to compare** — and none is possible, because REST's window can never reach back that far. | Download the **Full** archive (10.5 GB) and compare its 2026-04…06 bars against a Binance-free Kraken reference; or wait for the Q3-2026 increment (should publish ~Oct 2026) and compare its last 720 bars against REST. **Until then this is an unverified assumption, and it is the load-bearing assumption of rank #1.** |
| **`vwap` for archive history** | Not in the OHLCVT schema. | Nothing — it does not exist. Decision #5 is the resolution. |
| **σ_win (SD of `excess_return` across disjoint windows)** | Requires walk-forward (#4), which is unreachable today. | The harness, after #3+#4, on one frozen policy across many windows. This is the number that decides whether seeds or windows are the better lever (§4.4). |
| **ρ (pairing gain)** | No records in `results/` from a paired run. | **Cheap and immediate:** run config A and config B at 3–5 *shared* seeds, or read any existing multi-seed matrix with ≥2 config levels. Zero new architecture. |
| **Tardis.dev, CoinAPI, Kaiko, Amberdata pricing** | No public list price; would require an account or a sales contact. I did not sign up or contact anyone. | A live account check. **Not recommended** — rank #1 makes them moot. |
| **σ_1 over a multi-year span** | I measured only 91 days (one quarter). Volatility clustering means σ_1 varies by regime; a multi-year figure would firm up Table 4. | The Full archive (10.5 GB) or CryptoDataDownload's Kraken 1-min history. |
| **Whether Binance's archive `DOGE` coverage would even help** | Moot — `DOGUSD_60.csv` exists in Kraken's own archive (verified). | — |
| **Rate-limit behaviour of `assets.kraken.com`** | I made 2 requests; no limits observed, none published. | Only relevant if a future pass pulls many quarters. |

---

## 8. Three sentences for the architect

1. **The G3 caveat is free to remove.** Kraken publishes a keyless, first-party OHLCVT archive (`Kraken_OHLCVT_{Q}.zip`, 538 MB/quarter; Full = 10.5 GB; `timestamp,open,high,low,close,volume,trades`) that I downloaded and parsed successfully — it includes `DOGUSD_60.csv`, eliminating both the venue discontinuity **and** the DOGE hole. The only loss is `vwap`.
2. **G4's arithmetic inverts the audit's intuition.** Measured σ(L) ∝ √L: a longer eval window is a *noisier* measurement, and one 456-bar window is worth ~3 effective independent draws. Power comes from more *short* windows and more seeds — never from one long window — so **G3 is what makes G4 fixable, and depth currently contributes exactly zero variance reduction.**
3. **The cheapest, highest-leverage change is 30–60 lines in `tools/model_matrix.py`.** `seed` is already in `REQUIRED_BACKTEST_FIELDS`, so pairing, σ_within, Cliff's δ, Wilcoxon, bootstrap CIs and a `MDD = 2.8016·σ/√N` detection floor are computable **from results that already exist** — turning "median 1.3 pp" into "**this design resolves nothing below 2.8 pp; N=96 required**", which is a *correct and defensible finding* rather than an unfounded one.

RESEARCH COMPLETE