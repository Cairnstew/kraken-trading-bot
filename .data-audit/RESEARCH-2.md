# RESEARCH-2 — Market-data depth & clean splits for the RL pipeline (AUDIT Candidate 2)

Date: 2026-09-30. Fresh audit pass (begins a new 720-cap / absent-store state).
Researcher: researcher-2, audit-pipeline team. Read-only pass; findings only.

**Gap (AUDIT Candidate 2, market-data depth):** Kraken REST OHLC caps at ~720
recent bars/call regardless of `since` (≈30 d at 1 h, ≈12 h at 1 m), so the
single-call live path is ~30 d and even `pages=6` paging collapse at ~720 bars
(empirically re-proven at `--pages 21` → 721 bars). The fixes already exist as
*parts* — a store seam (`market_data_store:`) + a Binance-archive seeder
sibling (`kraken-deep-history`) — but the store root does not exist on disk and
the training/backtest readers never pass `since`/`until` to the store, so deep
history is unreachable and train/eval splits are not sliceable today.

Everything below was verified against the code on disk (uncommitted working
tree) and against the exchange/archive APIs on 2026-09-30 (probes noted inline).

---

## 0. The reduction target (what every candidate must produce)

`read_ohlc_dataframe` → `MarketDataStore.read(pair, interval, since, until)` ->
pandas DataFrame with a UTC `DatetimeIndex` named `time` and columns
`time, open, high, low, close, vwap, volume, count` (prices float at the
boundary; `time`/`count` int; since inclusive / until exclusive; month-sliced
parquet at `{root}/{PAIR_ID}/{interval_min}/{YYYY-MM}.parquet` + `_meta.json`
cursor). `upsert(pair, interval, candles)` takes Candle-like rows and dedupes on
`time` keep-last (so re-polls overwrite the still-forming bar). The store is
**venue-agnostic**: `OHLC_INTERVALS = (1,5,15,30,60,240,1440,10080,21600)` is the
only interval gate, and the bot never sees the venue.

So every candidate reduces to: an OHLC(P) series → per-(ticker, bar-bucket-start)
rows in that 8-column schema → `upsert_csv`/`upsert_jsonl` (bulk backfill) or a
`store.update(source=...)` duck-typed live source
(`ohlc(pair, interval, since) -> (candles, last)`).

---

## 1. The 720-cap: verified from Kraken's own spec (not folklore)

Kraken's OpenAPI spec for `GET /0/public/OHLC` (fetched today) reads verbatim:

> "Returns up to 720 of the most recent entries (older data cannot be
> retrieved, regardless of the value of `since`)."

And the `interval` enum is exactly `1, 5, 15, 30, 60, 240, 1440, 10080, 21600`
— **bit-for-bit the store's `OHLC_INTERVALS`**, so every store interval has a 720
bar/call ceiling = 30 d at 1 h, 12 h at 1 m, 5 d at 5 m. Paging (`pages=N`) does
**not** extend depth: the exchange re-returns the same recent ~720 window; the
DEEP-TEST run trained on 721 bars at `--pages 21`. This makes Kraken a
*forward-poll* venue (append the tail every poll) and never a *backfill* venue.

There is **no** official Kraken spot-OHLC archive dump (futures CSV
downloads exist but are a different instrument family).

---

## 2. Store seam: `since`/`until` ARE plumbed into `store.read` — but no caller supplies them

Precise state of the read path (verified line-by-line):

- `read_ohlc_dataframe` **does** forward both bounds to the store:
  `df = store.read(pair, interval, since=since, until=until)` (`data.py:459`).
- But the **four production call sites** all omit them, so they always ask for
  the whole store (or the live ≤720 tail when unseeded):
  - `train_ticker` — `train.py:185-194`
  - `backtest_model(data=None)` — `backtest.py:138-147`
  - `PaperTrader._fetch_data` — `paper_trade.py:289-298`
  - `export` — `export.py:159-167`
- Nothing in `configs/default.yaml` (or `build_train_config`) carries
  `since`/`until`; the documented adapter to-dos (honour since/until from
  config; drive walk-forward via the currently-unused
  `TradingEnvironment.reset(options=...)` at `environment.py:211-221`) are exactly
  the clean-split work this pass scopes.

**Source of the audit's phrasing "never passes since/until":** true at the
config/call-chain level, not in `read_ohlc_dataframe` itself. The kwargs are
plumbed and unit-tested (`tests/test_rl_data_store.py:170-182`); the config
chain just never populates them.

### Clean-split support is already IN the store read
`MarketDataStore.read` does **month partition pruning**: `months_between(since,
until)` (`store.py:168`, `utils.py:94-109`) builds the exact `YYYY-MM` key set,
so a read touches only the parquet files in `[since, until)` and then filters
rows (`store.py:180-184`). A train window and eval window are thereby disjoint
*by construction* with zero cross-listen. This is the month-sliced key design
that makes "sliceable splits" a config/CLI problem, not a store problem.

### The look-ahead trap for splits (shared with Candidate 1)
`prepare_episode` (`data.py:547`) calls `features.fit(df)` on the **full**
frame, then slices `df.tail(episode_bars)` (`data.py:554-557`) — normalization
stats therefore leak the held-out future. A cleaned split must **fit on the
train slice only**, then read/transform the eval slice separately. The seeded
store makes that a two-line read change (read train window → fit → read eval
window), and it doubles as part of the Candidate-1 fix.

---

## 3. Candidate inventory (scored: depth / keyless / maintenance / store-fit / clean-split × exercise)

### B1 — Binance public data archive (`data.binance.vision`) — **PRIMARY (already shipped as `kraken-deep-history`)**
- **Maintenance:** official Binance bucket + helper repo (MIT); the archive is
  actively corrected retroactively — there is an exhaustive `updates/`
  changelog (e.g. 2022-08-08 kline corrections). One-time seeds can miss
  later corrections; re-verify periodically.
- **License:** data under Binance's public-data terms (free; redistribution
  allowed for backup/cache); helper repo MIT.
- **Auth:** keyless (S3-style GET, no key, no login).
- **Rate limits:** effectively none for file GETs (CDN-backed; don't hammer). A
  5-year 1 h seed ≈ 60 monthly ZIPs, a few hundred MB total.
- **Output shape:** monthly (and daily) kline ZIPs per `(symbol, timeframe)`.
  All spot intervals: `1s,1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,3d,1w,1mo`
  (confirmed from the helper README today). CSV row = `open_time, O, H, L, C,
  volume, close_time, quote_vol, n_trades, taker_buy_base, taker_buy_quote,
  ignore`; `open_time` is **milliseconds before 2025-01-01, microseconds from
  2025-01-01** (confirmed today). Has `n_trades` (= store `count`), no `vwap`
  (derive `quote_vol/volume`).
- **Depth (probed live today):** `ETHUSDT-1m-2017-08` and `BTCUSDT-1m-2017-08`
  ZIPs return HTTP 200 → **~9 years to listing**; `SOLUSDT` from 2020-08,
  `XRPUSDT` from 2018-08, `DOGEUSDT` from 2019-07 (all 200). 1 h ≈ 744
  bars/month → ETHUSDT ≈ 84 k bars since 2017-08; 1 m ≈ 44.6 k rows/month →
  ≈4.5 M bars over 9 years.
- **Reduction:** already built. `kraken-deep-history` download→unzip→parse
  (ms/µs→epoch-s, ticker map `ETH/USD→ETHUSDT` etc.)→store schema→
  `MarketDataStore.upsert_csv` (market-data mode) or store-shaped fallback CSVs;
  `set_cursor` then `verify`. Tick-map is *explicit and refuse-unknown*
  (`DOGE/USD` unmapped). Zero new adapter code in the bot.
- **Gap-fill / continuity (observed, real):** not a filled source. A 5.75-year
  live seed found **14 genuinely-absent 1 h bars across 7 runs in 2021/2023**
  (upstream archive absences, verified against raw ZIPs) and **two zero-volume
  bars** (2021-02-11, 2023-03-24). The zero-volume bars are the one hard crash:
  `volume.pct_change()` → `inf` → PPO `NaN logits` at step 1, surfaced only at
  multi-year depth; recorded follow-up is "treat non-finite like NaN" in both
  observation constructors.
- **Scores:** depth 5 · keyless 5 · maintenance 4 (retro corrections, upstream
  gaps) · store-fit 5 (ships its own seam) · clean-split 5 (arbitrary
  [since, until) windows, month-pruned). **Depth-per-effort: maximal — the
  machinery exists and has simply never populated a bot-visible store.**

### B2 — Binance REST `GET /api/v3/klines` (or via ccxt) — the live/forward leg
- **Maintenance:** official binance-spot-api-docs, active.
- **Auth:** keyless for public klines. **Rate:** public weight limits; ~1000
  bars/call with `startTime`+`endTime` pagination (full history reachable), a
  few thousand calls/min budget — fine as a poller, heavy as a 5-year 1 m
  backfill (~2600 calls).
- **Output shape:** same kline fields as B1 (12-array rows).
- **Reduction:** the store's `update(source=...)` duck-type is the intended
  socket — a thin `ohlc(pair, interval, since) -> (candles, last)` wrapper over
  `fetch_ohlcv` makes the existing poller venue-agnostic. This is the natural
  **live** leg (paper-mode tail pinning) beside the B1 backfill; it does not
  replace B1 for deep one-shot seeding.
- **Scores:** depth 5 · keyless 5 · maintenance 5 · store-fit 5 (drop-in update
  source) · clean-split 5. Effort 2-3 for the wrapper if ever built; **low
  priority while the B1 seed path is unused.**

### K1 — Kraken's own REST (the status quo)
- **Maintenance/license/auth:** Kraken public API, keyless. **Rate:** the
  720/call cap (verified §1) plus the bot's transport has no retry/backoff
  (Candidate 4).
- **Role:** the store's own poller is the *correct* Kraken use — forward
  accumulate ~720 bars per `update` every 30 s (`nix/module.nix` systemd timer
  `OnCalendar=*-*-* *:*:30`, `StateDirectory`+`DynamicUser`, keyless). Kraken is
  not a depth source. **Scores: depth 1** — replaced-by, not a competitor.

### CB1 — Coinbase Exchange `GET /products/{product_id}/candles`
- **Maintenance:** official Coinbase Exchange REST docs, active.
- **Auth:** keyless public candles. **Rate:** public keyless limits; **max 300
  candles/request** (official), `start`/`end` pagination therefore mandatory;
  granularity restricted to `{60,300,900,3600,21600,86400}` — **no 15/30/240**
  so it maps onto the store's interval set only partially (1,5,60,1440 exist;
  15,30,240,10080,21600 do not).
- **Output shape:** `[time, low, high, open, close, volume]` — no vwap, no
  count. Official warning: "Historical rate data may be incomplete. No data is
  published for intervals where there are no ticks" → **gap-prone** for thinner
  products.
- **Depth:** reachable by pagination (300/call) but poor depth-per-effort: a
  5-year 1 h window ≈ 43.8 k bars ≈ **147 calls** (vs 1 GET/month ZIP from
  Binance); 1 m ≈ 2.6 M bars ≈ **8760 calls**. Different venue/instrument family
  (ETH-USD, not USD⸮ class).
- **Reduction:** same converter shape as the seeder (a mini
  `kraken-deep-history` for Coinbase), or a live `update(source)` wrapper using
  start/end pagination. No count/vwap improved.
- **Scores:** depth 3 · keyless 5 · maintenance 4 · store-fit 3 (interval gaps +
  separate converter) · clean-split 5. *Viable only if the Coinbase venue is
  specifically wanted; strictly worse than B1 on depth-per-effort.*

### CC1 — CryptoCompare (min-api) — ruled out
Free tier caps at ~2000 bars/call and ~1000 keyed calls/day; older than ~2
months needs the paid Data API. That is the *same clamp* as Kraken, more effort.
**Not a depth source.** (Depth 2 · keyless 2.)

### Not shortlisted (why)
- **Bitfinex via ccxt:** deep keyless history but a separate venue family
  (`t:ETHUSD`); would re-build the seeder for a niche venue. Prior pass scored
  ≈21/25 as a ccxt venue; keep as a backup only.
- **CoinGecko / Gemini:** no per-bar OHLCV depth (CoinGecko `market_chart` ≤90
  d coarse; Gemini candles shallow).
- **Paid archives (CoinAPI/Polygon/Tiingo/Databento):** add normalized bars and
  guaranteed regularity, but free B1/CB1 cover the ETH-class gap; only worth it
  for a venue/time-gap Binance lacks. CoinAPI free ≈100 OHLCV calls/day.
- **`kurosearch` and `researcher-python` — confirmed NOT market-data-adjacent:**
  `kurosearch` is a Rule34 image-search app (Svelte+API, `brand/`,
  `playwright.config.ts`); `researcher-python` aggregates academic papers over
  OpenAIRE/SemanticScholar/arXiv/Crossref. Neither is in the data-source
  registry. Nothing to chase.

**Scores table**

| Candidate | depth | keyless | maint | store-fit | clean-split | depth-per-effort |
|---|---|---|---|---|---|---|
| **B1 Binance archive (shipped)** | 5 | 5 | 4 | 5 | 5 | **● maximal (build it exists, seed it)** |
| B2 Binance REST / ccxt | 5 | 5 | 5 | 5 | 5 | ○ wrapper needed; live leg |
| K1 Kraken REST (status quo) | 1 | 5 | 4 | 5 | 5 | ✗ forward-poll only |
| CB1 Coinbase candles | 3 | 5 | 4 | 3 | 5 | ○ 300/call, interval gaps |
| CC1 CryptoCompare min-api | 2 | 2 | 4 | 4 | 4 | ✗ same clamp, keyed |

---

## 4. Backfill vs continuous poller; `verify` & gap-fill semantics

- **`verify` EXISTS in BOTH siblings**, with the same JSON shape and exit code
  (0 contiguous / 1 gaps): `kraken-market-data verify` (`cli.py:147-153` →
  `store.verify`, `store.py:337-378`) and `kraken-deep-history verify`
  (`cli.py:170-174`, plus `FallbackStoreWriter.verify`). What it checks:
  `expected = span//step + 1` unique bars at exact `interval` regularity,
  reporting `missing`, `first_missing_time`, `contiguous`. NaN-`vwap` rows still
  count as present.
- **What `verify` does NOT do:** repair or fill. It is a read-only gate. The
  correct composition is *seed (B1) → verify (gate) → forward-accumulate
  (K1 poller or the bot's own fetch→upsert leg on each read)*.
- **Gap-fill semantics for an RL frame:** missing bars are **absent rows** (no
  row at all), not NaN columns. The feature pipeline's `ffill().fillna(0)` fills
  *column* gaps inside an existing index, not missing timestamps — so rolling
  windows that cross an absent hour see fewer rows and emit NaN on the following
  bar. Two concrete consequences for clean training on a seeded store:
  1. `verify()` the elected train/eval windows before training (cheap, already
     built).
  2. The one *hard crash* observed is not a missing row but a present
     zero-volume row → `volume.pct_change()` → `inf` (2021-02-11, 2023-03-24)
     reaching PPO as NaN logits. This is a real, previously-hit blocker at
     multi-year depth, with a recorded upstream fix ("treat non-finite like
     NaN" in both observation constructors).

---

## 5. Interval choice (1 m / 5 m vs 1 h) for more bars per calendar day

- **Store gate:** `OHLC_INTERVALS` above; **seeder gate:**
  `INTERVAL_TO_TIMEFRAME` maps `1,5,15,30,60,240,1440,10080` — `21600` (Kraken's
  15-day) deliberately unmapped (Binance has no 15-day kline). So both 1 m and
  5 m are fully supported end-to-end.
- **Bars/calendar-day:** 1 m=1440, 5 m=288, 15 m=96, 1 h=24, 1 d=1. The RL env
  maps **one bar = one PPO step**, so the meaningful quantity is steps-per-day:
  10 k timesteps ≈ **416 days of 1 h**, ≈ **35 days of 5 m**, ≈ **7 days of
  1 m**. Deeper calendar coverage per training budget wants coarser bars;
  finer bars buy observation density per calendar day (a cheaper
  "microstructure proxy" than a true book/trades recorder — Candidate 5's own
  sub-point).
- **Feature-window coupling:** `feature_windows: [1, 4, 24]` literally means
  "1 bar, 4 bars, 24 bars" — at 1 m that day-view is only 24 minutes until the
  windows are re-tuned (e.g. `[60, 240, 1440]` at 1 m to restore the 1 h/4 h/1 d
  semantics). This isn't a store question; it belongs in per-run config.
- **Data size:** measured ~3.1 MB parquet per ~50 k 1 h bars (DEEP-TEST) →
  5 y of 1 h ≈ 44 k bars ≈ ~3 MB; 5 y of 1 m ≈ 2.6 M bars ≈ ~150-200 MB —
  fine on disk, but `MarketDataStore.read` *concats every month file in the
  window* (`store.py:180`), so a full-history 1 m read is the heavy tail.
- **Recommendation:** seed **1 h** for train/eval depth; add **1 m/5 m** only
  for the paper/live or microstructure path, preferably as a separate
  `(pair, interval)` sub-store so bulk reads stay month-pruned. Do not mix
  both into one unbounded `read()` unless the window is bounded.

---

## 6. Flake input + `PYTHONPATH` wiring (how the store becomes importable in the bot dev shell)

- **Today:** `flake.nix` inputs = `nixpkgs`, `kraken-python`,
  `kraken-market-data`. The dev shell already carries the store's runtime deps
  (`requests`, `pyarrow` in the `python.withPackages` set, `flake.nix:62-63`)
  and prepends the sibling on the path:
  `export PYTHONPATH="${kraken-market-data.outPath}:$PYTHONPATH"`
  (`flake.nix:71`). That combination — pyarrow+requests present + `market_data`
  importable — is precisely what made `market_data_store` work in the dev shell
  when it was wired (the RUN-LOG/VALIDATION note).
- **The missing half:** `kraken-deep-history` is **not** a flake input and not
  on `PYTHONPATH`. In the bot dev shell `import kraken_deep_history` fails, so
  the seeder silently falls into **fallback-CSV** mode — which the bot cannot
  read (`MarketDataStore.read` globs `*.parquet` → 0 bars). This is the exact
  reason the sibling README instructs running the seed "from the bot dev shell
  with this repo on PYTHONPATH".
- **Concrete wiring (2 small edits):**
  1. `inputs.kraken-deep-history.url = "git+https://github.com/Cairnstew/kraken-deep-history";`
     (any sibling-named input works; its own flake is standalone/nixpkgs-only).
  2. `shellHook`: `export PYTHONPATH="${kraken-market-data.outPath}:${kraken-deep-history.outPath}:$PYTHONPATH"`.
     `outPath` is the repo root; `kraken_deep_history/` is stdlib-only and
     importable without a build, so no packaging step is needed.
  Then from the bot dev shell: `python .../kraken-deep-history/cli.py seed
  --ticker ETH/USD --interval 60 --from 2017-08-01 --store <bot-visible root>`
  produces a **market-data-mode (parquet)** store the bot reads directly.
- **Keep `market_data_store: null` as the default**; enable per-run via a config
  copy (the shipped `configs/deep-history.example.yaml` precedent) so each
  registered model records the store root it saw (`models/{T}/"{m}/config.yaml`).

---

## 7. Ranked recommendations (for the Decision phase)

1. **Populate a bot-visible store (highest depth-per-effort):** wire the flake +
   `PYTHONPATH` (§6), run the `kraken-deep-history` seed once (1 h, 2017-08 /
   listing-date → now) into a bot-visible root, `verify` it, and point a per-run
   config at it. This unlocks ~9 years / ~84 k bars for ETHUSD vs today's ~30 d
   — no consumer code change required for *depth* (the seam is documented,
   tested, and gate-passed for 20-month and 5.75-year seeds).
2. **Surface `since`/`until` + fit-on-train-slice for clean splits:** plumb
   `since`/`until` (ISO or epoch) from config/CLI into the four read call sites
   (§2); change `prepare_episode` to fit normalization on the **train** slice
   only before producing eval windows (kills the Candidate-1 look-ahead leak at
   the same time); add an optional "contiguous window" assertion that reuses
   `store.verify` before training on a seeded window. This is a config/CLI +
   one-function change; the store's month pruning already makes it free.
3. **Keep Kraken as the forward poller, not a depth source:** deploy/import the
   store's systemd poller (or run `update` manually) so the live 30-s tail
   appends on top of seeded depth; the B2 (Binance REST/ccxt) live wrapper is a
   low-priority nicety while the K1 poller is unimplemented on this host.

**Hand-offs to the decision phase (already-evidenced fixes, not research):**
zero-volume-bar → `inf` crash (fix at both observation constructors);
Binance archive retro-corrections (`updates/` changelog) → re-run `verify` /
re-seed rare windows; `DOGE/USD` unmapped in the ticker map → 3-line extension
to seed it; the packaged-binary `rl.export` import gap is pre-existing WIP, not
this gap.

```
RESEARCH COMPLETE
```