# RESEARCH-3 — CAND-3 (the ~721-bar ceiling), CAND-4 (no retry), CAND-5 (no dispersion gate)

**Pass:** PHASE 2 research, `audit-pipeline-1002`, agent `researcher3`
**HEAD researched:** `ca107fa` ("docs: data-pipeline pass 2026-10-02 — Phase 1 audit artifact")
**Reads:** `AUDIT.md` §2 rows G3/G8, §3 CAND-3/CAND-4/CAND-5
**Mode:** read-only except this file. No code written, nothing committed, nothing deleted. All
measurement done in `/tmp/krb-cand3/`.

Every number below is measured on this host unless it is quoted from a first-party document, in
which case the source is named. Nothing is estimated.

---

## 0. Executive answer

| Question | Answer |
|---|---|
| Store build cost | **158 s wall-clock, 13 MB, 3 commands, zero code.** `market_data_store` is a pure config flip — **proved**: `read_ohlc_dataframe` returned **76,561 bars in 0.48 s** from a seeded store with no code change. |
| Kraken OHLCVT vs Binance archive | Kraken publishes a real, keyless, venue-consistent archive. **It is still the wrong source, and the reason is publication lag, not basis.** Binance monthly + Kraken-REST forward accumulation beats it on every axis that matters to this pipeline. |
| Cross-venue basis | Measured on 2,184 real common hourly bars: **+5.41 bps mean, 6.52 bps std, drifting −0.7 → +13.4 bps across the quarter.** The level offset is harmless (everything is z-scored). The *liquidity* gap is not: **18.6× notional, 126× trade count.** |
| `since`/`until` push-down | **~15 lines, and worth almost nothing on its own.** `since` is a no-op on the non-store leg — Kraken's own OpenAPI says *"older data cannot be retrieved, regardless of the value of `since`."* With a store it is an efficiency win only. **Do not lead with it.** |
| CAND-4 | **~40 lines, no new dependency needed** — the retrying client is already shipped in the sibling (`market_data.client.KrakenClient`). Plus a 2-line cursor-advance guard that kills a **measured 5-of-6 wasted API calls**. |
| CAND-5 | **~50 lines, all inputs already computed and printed.** A count gate cannot distinguish a resolved effect from a noise-ranked one; the harness already holds the numbers that can. |
| Do 4 and 5 gate the others? | **CAND-5 gates every claimed effect, including CAND-3's.** CAND-4 does **not** gate the G3 push-down — nothing gates the push-down. See §7. |

---

## 1. Store build cost, precisely (Q1)

### 1.1 Measured: it is three commands and under three minutes

The store root `~/Projects/kraken-market-data/store` does not exist (confirmed: `ls` → *No such
file or directory*). I built a real one under `/tmp/krb-cand3/store2` and measured it.

```
# The sibling store's own dev shell (pandas 3.0.4, pyarrow 24.0.0, market_data 0.1.0)
cd ~/Projects/kraken-market-data
nix develop --command bash -c \
  "PYTHONPATH=$HOME/Projects/kraken-deep-history:$HOME/Projects/kraken-market-data \
   python $HOME/Projects/kraken-deep-history/cli.py seed \
     --ticker ETH/USD --interval 60 --from 2018-01-01 --store /tmp/krb-cand3/store2"
```

| Ticker | Range | Months requested | Months downloaded | Bars | Wall clock | On disk |
|---|---|---|---|---|---|---|
| `ETH/USD` | 2018-01-01 → now | 106 | 104 | 75,840 | **57.9 s** | 4.6 MB |
| `BTC/USD` | 2018-01-01 → now | 106 | 104 | 75,840 | **~60 s** | 4.4 MB |
| `SOL/USD` | 2020-09-01 → now | 74 | 72 | 52,565 | **~40 s** | 4.0 MB |
| **total** | | | **280** | **204,245** | **~158 s** | **13 MB** |

Cost per bar: **60 bytes** (parquet, month-sliced). Cost per month: **~0.56 s**, ~42 KiB. All
three seeds report `"store_mode": "market-data"` — i.e. real parquet, readable by the bot.

A 12-month ETH/USD seed for comparison: **9.3 s, 956 KB** (that run fell into `fallback-csv`
because I had not put `kraken-market-data` on `PYTHONPATH` — see §1.3).

### 1.2 `plan` is free and the on-disk shape is stable

```
python cli.py plan --ticker ETH/USD --interval 60 --from 2018-01-01 --store /tmp/krb-cand3/store
# -> 106 monthly URLs, 1.4 s, zero network
```
`plan` emits `https://data.binance.vision/data/spot/monthly/klines/ETHUSDT/1h/ETHUSDT-1h-YYYY-MM.zip`.

Measured on-disk shape after the seed — **exactly** what `INTEGRATION.md` documents:

```
/tmp/krb-cand3/store2/
├── _meta.json                     # {"schema":1,"cursors":{"ETH_USD:60":{"since":1788217200,"updated_iso":"..."}}}
├── ETH_USD/60/2018-01.parquet      # 104 files for ETH_USD/60, 42.5 KiB avg
└── ...                            # XBT_USD/60/, SOL_USD/60/
```

Read back:

```
MarketDataStore('/tmp/krb-cand3/store2').read('ETH/USD', 60)
  shape (75840, 8)
  time int64 | open/high/low/close/vwap/volume float64 | count int64
  index: DatetimeIndex tz=UTC, name='time'
```

### 1.3 The one build trap: `store_mode` silently degrades to `fallback-csv`

`kraken-deep-history`'s own `nix develop` has **only pytest** — no pandas, no pyarrow, and
`market_data` is not importable (verified: `ModuleNotFoundError: No module named 'market_data'`).
`_open_store` (`seeder.py:409-418`) catches `ImportError` and silently returns
`FallbackStoreWriter`. The JSON report then says `"store_mode": "fallback-csv"` — and that mode
writes **`.csv` files**, which `MarketDataStore.read` cannot see (it globs `*.parquet`).

I hit this on my first run without noticing the field. **The report's `store_mode` key is the
only signal**, and it is easy to miss. Two ways out, both fine:
- run the seed from `kraken-market-data`'s dev shell with `PYTHONPATH` covering both repos (what
  I did), or
- seed in `fallback-csv` and then `kraken-market-data backfill --pair X --interval 60 --csv …`
  (`store.py:423` `upsert_csv`).

This is a 1-line-of-documentation fix in `kraken-deep-history/README.md`, not a code change.

### 1.4 Is `market_data_store` a config flip? **Yes — proved end to end.**

The bot's dev shell *already* carries the sibling (`flake.nix:62,74` →
`"sibling deps: kraken-market-data, pyarrow, requests"`). No install work. And the store leg is
already wired (`data.py:1052-1208` `read_ohlc_dataframe`, `data.py:1211` `_resolve_store`).

Measured, against the store I just seeded, from `~/Projects/kraken-trading-bot`:

```python
read_ohlc_dataframe('ETH/USD', interval=60, pages=1,
                    market_data_store='/tmp/krb-cand3/store2')
# shape (76561, 11) elapsed 0.48 s
# index 2018-01-01 00:00:00+00:00 -> 2026-10-02 12:00:00+00:00
# cols  time open high low close vwap volume count
#       vwap_dev trade_count_zscore_20 volume_per_trade
```

**76,561 bars — 106× the 721-bar ceiling — from one config value, 0.48 s, zero code changes.**
(76,561 > 75,840 because the `pages=1` live-append leg upserted the current 721 bars on top of the
seed: fetch → upsert → read, exactly as documented.)

### 1.5 Read/write contract, measured

| | |
|---|---|
| `read(pair, interval, since=None, until=None)` | inclusive `since`, exclusive `until`, on bar-bucket start; globs only the months the window touches (`store.py:168-169`); empty window → empty frame with the same columns, caller decides |
| `upsert(pair, interval, candles)` | dedupes on bar `time`, **keeps last** (re-poll overwrites the still-forming bar with Kraken's revision) |
| `set_cursor` / `cursor` | `since`-cursor sidecar per `(PAIR_ID, interval)` in `_meta.json`; this is what makes the forward poller append-only |
| whole-store read | 75,840 rows × 5 reads = **0.71 s** → 0.142 s/read |
| windowed read (2 months) | **0.010 s** — 14× faster, because it touches 2 parquet files instead of 104 |

**Answers:** yes, it is a config flip once seeded. **There is no code work on the bot side at
all.** The whole of CAND-3's store half is: seed once (158 s, 13 MB, `/tmp`-safe), set one YAML
key. `INTEGRATION.md §5` already lists the *follow-on* code (honour `since`/`until`, train/eval
split, walk-forward) — see §4 for which of those are worth doing now.

---

## 2. Kraken's OHLCVT archive vs Binance's — the central judgement (Q2)

### 2.1 What Kraken actually publishes

Source: <https://support.kraken.com/hc/en-us/articles/360047124832-Downloadable-historical-OHLCVT-Open-High-Low-Close-Volume-Trades-data>
("Last updated: September 16, 2026"). Quoted and measured:

| Fact | Value | How verified |
|---|---|---|
| Coverage | "each pair from its first trade on Kraken through **30 June 2026**" | page text |
| Increments | "at the end of each **quarter**", single ZIP, no reassembly | page text |
| Complete dataset | 5 parts × `2097152000` B = **10,485,760,000 B ≈ 10.5 GB**, all 1,399 pairs | `curl -I` `content-length` on `.part00` |
| Reassembled ZIP sha256 | `fc81b54cba6e12af3e9422dde9416179e6ef76af4831d48d839fbdb43018eaa4` | page text |
| Per-part sums | `https://assets.kraken.com/marketing/institutions/OHLCVT_Full_PARTS_SHA256SUMS.txt` | fetched, 515 B, 5 lines |
| Incremental ZIP | `Kraken_OHLCVT_2026Q2.zip` = **537,866,903 B ≈ 538 MB** | `curl -I` |
| Throughput (this host) | **~21 MB/s** single-stream (40 MiB range GET × 2) | `curl -r 0-41943039` |
| ⇒ full-archive time | ~500 s one stream, ~100–170 s across 5 parallel parts | arithmetic |
| ⇒ incremental time | ~26 s | arithmetic |
| Rate limits | **None published.** No `x-ratelimit`, no `retry-after` header on `assets.kraken.com`; `cache-control: public,max-age=86424`, `last-modified: 2026-09-14` | `curl -I` |
| Auth | none | — |
| Terms | Kraken's general ToS / legal disclosures. Nothing in the article restricts redistribution or automated download; it says "These materials are for general information purposes only." | page text |

I read the real archive rather than the marketing page. `Kraken_OHLCVT_2026Q2.zip` central
directory (ranged GET of 589,176 B at offset 537,277,705) parses to **9,795 entries**; its
`MANIFEST.json` (extracted via ranged GET):

```json
{"product":"ohlcvt","schema_version":1,"release":"r1",
 "columns":["timestamp","open","high","low","close","volume","trades"],
 "has_header":false,
 "coverage":{"start":"2026-04-01","end":"2026-06-30"},
 "pairs":1399,"files":9793,"rows":35247327,"generated":"2026-08-17"}
```

Real extracted data (`ETHUSD_60.csv`, 2,184 rows, flat, no directories):

```
1775001600,2103.25,2115.26,2095.55,2112.38,1259.76951889,761
1775005200,2113.11,2113.56,2090.14,2091.15,1216.79762041,785
```

Five hard facts fall out of this that the audit did not record:

1. **No `vwap` column.** `columns` is 7 long; the store contract is 8 (`time, open, high, low,
   close, vwap, volume, count`, `store.py:63`). Kraken's own REST `OHLC` row schema *does* carry
   `vwap` (Kraken OpenAPI `tickData`: `[time, open, high, low, close, vwap, volume, count]`). So
   the bulk archive is **strictly poorer than the REST endpoint on shape**. Consequence measured in
   §2.4.
2. **No header row** (`has_header: false`), so a CSV writer that assumes one silently misreads.
3. **Gaps are not zero-filled** — "Only intervals in which trades occurred are included". At 60 m
   this is a non-issue for liquid pairs: ETHUSD_60 has exactly 2,184 rows = 91 d × 24, i.e.
   perfectly dense for Q2. It bites at 1 m/5 m.
4. **The article's interval list contradicts its own MANIFEST.** The intro says "1, 5, 15, 30,
   60, 240, 720 and 1440"; the "About the data" section and every actual filename is
   `1/5/15/60/240/720/1440` — **30 is absent** (see `0GEUR_1.csv … 0GEUR_720.csv`, no `_30`).
   A reader built from the intro paragraph would request a file that does not exist.
5. **Naming is Kraken's WS notation**: `{PAIR}_{INTERVAL}.csv`, e.g. `XBTUSD_60.csv` — note
   **XBT, not BTC**, and it does *not* match the bot's `normalize_ticker_id`. A reader needs the
   same alias table the bot already has (`data.py:105` `_TICKER_ALIASES`, `XBT↔BTC`).

### 2.2 The killer: publication lag, not basis

| | Binance archive | Kraken OHLCVT |
|---|---|---|
| Cadence | **monthly** (only the incomplete current month 404s) | **quarterly**, ~6-week publish lag |
| Newest data available today (2026-10-02) | 2026-09 | **2026-06-30** (`coverage.end`), `generated: 2026-08-17` |
| Staleness at the newest bar | ~3–30 days | **~94 days** |

Measured on my own seed: the ETH seed reported
`months_downloaded: 104, months_requested: 106, skipped: ["2026-09: … 404", "2026-10: … 404"]`.

**A Kraken-archive-only store is three months stale, and Q3-2026 has not been published at all.**
So a Kraken-native reader does not replace the store poller — it *joins* it, and you would still
need a Kraken REST forward leg to cover the gap. Which means the cross-venue basis is **not
eliminated by using Kraken's own archive**: the tail of your training data comes from REST either
way, and any seam where a Binance-seeded month meets a Kraken-REST week is a cross-venue seam.

**The correct architecture is the one already in the repo:** deep backfill from Binance (cheap,
monthly, 2018→) **+ forward accumulation from Kraken REST** (the `kraken-market-data` poller,
which the store's `update()` already implements and which writes the same `_meta.json` cursor).
Deep past is cross-venue and *doesn't need to be venue-exact*; the tail — the part the policy is
actually evaluated on, and the part `backtest.py` replays — is Kraken's.

### 2.3 What the cross-venue basis actually costs — measured, 2,184 real bars

Kraken `ETHUSD_60` vs Binance `ETHUSDT-1h`, Q2-2026 (2026-04-01 → 2026-06-30), both extracted
from the real archives, joined on bar time. **2,184 of 2,184 matched — zero missing on either
side.**

**Price level** (close, basis = `(binance − kraken)/kraken`):

| statistic | value |
|---|---|
| mean | **+5.41 bps** |
| median | +4.22 bps |
| std | 6.52 bps |
| min / max | −9.28 / +21.48 bps |
| mean \|diff\| | 6.63 bps |
| \|diff\| > 5 bps | **48.4 %** of bars |
| \|diff\| > 10 bps | **29.2 %** of bars |
| \|diff\| > 25 bps | 0.00 % of bars |

**It is not zero-mean noise — it is a slow level drift** (this is USDT appreciating against USD
over the quarter):

| period | mean daily basis |
|---|---|
| 2026-04 | **−0.72 bps** |
| 2026-05 | **+6.63 bps** |
| 2026-06 | **+9.97 bps** |
| 2026-06-30 (last day) | **+13.38 bps** |
| weekly series | +3.05 → −2.85 → +3.41 → **+14.12** → +6.34 → **+14.03** → +14.85 bps |

**The two books are genuinely different, not just offset:**

| microstructural fact | value |
|---|---|
| bars where Binance `high` > Kraken `high` | **81.3 %** |
| bars where Kraken's H/L range does **not** contain Binance's close | **11.3 %** |
| notional traded, Binance ÷ Kraken (median) | **18.6×** |
| trades per bar, Binance ÷ Kraken (median) | **126×** (97,298 vs 792) |
| OHLC max abs diff | `open` 21.9 bps, `high` 34.0 bps, **`low` 923 bps** (a Kraken flash-move artefact), `close` 21.5 bps |

**But the quantity the policy trades on is nearly identical:**

| statistic | value |
|---|---|
| hourly-return correlation | **0.999081** |
| hourly-return std | Kraken **58.29 bps** vs Binance **58.30 bps** (indistinguishable) |
| bars disagreeing on the **sign** of the 1 h return | **2.06 %** |
| bars where return magnitude differs by >50 % | **3.89 %** |
| 24 h return correlation | **0.999931** |
| 24 h return mean \|diff\| / p95 / max | **2.4 / 6.3 / 14.5 bps** |
| 24 h return sign disagreement | **0.37 %** |

### 2.4 The judgement (not hedged)

**Do not build a Kraken OHLCVT reader. Use Binance for the deep backfill and Kraken REST for the
forward tail — which is what the repo already does.**

The reasoning, in the order that actually decides it:

1. **The level basis costs nothing here.** Every observation column is z-scored
   (`environment.py:488-496`), so a *constant* multiplicative offset cancels exactly. And the
   measured +5.4 bps mean is **9 % of the 58 bps hourly-return std** — it does not compete with
   the signal. Even the drift (−0.7 → +13.4 bps across a quarter, i.e. ~14 bps total) is 0.24× one
   hourly sigma.
2. **The liquidity basis costs a lot, and no source fixes it.** The 18.6× notional and 126×
   trade-count gap means every volume- and count-normalised feature is measuring a different
   thing on Binance bars than on Kraken bars: the `volume` group's 6 columns, plus
   `volume_per_trade` and `trade_count_zscore_20` (`data.py:812-873`). But this is **inherent to
   venue, not to archive choice** — Kraken's own OHLCVT carries Kraken's own (tiny) trade counts
   because it is a different book. The pipeline would need a venue handle either way; the
   honest fix is to *label* the venue per store (`_meta.json` is already where that belongs), not
   to pay 10.5 GB for the privilege of a second data source.
3. **The 11.3 % out-of-range bars and 2.06 % sign disagreements are the real cost, and they are
   irreducible.** No archive makes two venues the same venue. A Kraken reader would remove the
   basis from the *deep past* while the *tail* — where the policy is scored — stayed cross-venue
   against the REST poller anyway (§2.2).
4. **Kraken's archive is strictly poorer on shape.** **No `vwap` column** (`MANIFEST.columns` is 7
   long vs the store's 8). Consequence, measured on the next line of code:
   `add_derived_ohlcv_features` (`data.py:846`) gates on `"close" in df.columns and "vwap" in
   df.columns` — a frame without `vwap` **silently loses `vwap_dev`**, one of the 52 bare
   observation columns, and the docstring is explicit that "Nothing here raises or warns when the
   inputs are absent — silently narrower is the honest answer." So a Kraken-OHLCVT store buys you
   years of bars at the cost of a **silently narrower observation**, which is precisely the failure
   mode AUDIT §4.5 already flags ("The observation width is a function of runtime file contents,
   not config"). Adding 7 years to a 51-column observation to lose a column in exchange is a bad
   trade.
5. **Cost.** Kraken reader: ~10.5 GB (or 538 MB/quarter) + a new ZIP central-directory reader +
   `XBT`/alias translation + headerless-CSV handling + a sparse-gap policy + quarterly
   re-download orchestration. Binance route: **already implemented, tested, and 158 s.**
6. **Cadence.** Binance monthly vs Kraken quarterly-with-6-week-lag (§2.2).

**One thing the cross-venue route genuinely does not give you, and it should be written down
rather than argued away:** `Kraken_OHLCVT_Full_2026Q2.zip` is the only venue-exact route to
*pre-2018-free* history for 1,399 pairs including pairs Binance never listed. If the roadmap ever
needs a pair outside `TICKER_SYMBOL_MAP`, Kraken's archive is the fallback — and a ranged GET of
the central directory makes a targeted `ETHUSD_60.csv` extraction 54 KB, not 538 MB. I verified
that trick works. It is a footnote, not a plan.

**Recommended accompanying change (cheap, and it discharges the audit's caveat properly):** write
the venue into the store's `_meta.json` alongside the cursor, and surface it in `read_ohlc_dataframe`'s
log line. Six lines. It converts an unstated data context into a stated one, which is what
`kraken-deep-history/INTEGRATION.md §6` already promises and does not deliver.

---

## 3. The `since`/`until` push-down (Q3)

### 3.1 `since` is a no-op on the non-store leg — this is the headline

Kraken's own OpenAPI description for `GET /0/public/OHLC`, verbatim:

> "Returns up to 720 of the most recent entries (**older data cannot be retrieved, regardless of
> the value of `since`**)."
>
> `since`: "Return OHLC entries since the given timestamp (**intended for incremental updates**)"

Measured live against the real endpoint, four different `since` values:

| `since` | rows | `last` | range returned |
|---|---|---|---|
| `None` | 721 | 1790938800 | 2026-09-02 12:00 → 2026-10-02 12:00 |
| now − 30 d | 720 | 1790938800 | 2026-09-02 13:00 → 2026-10-02 12:00 |
| now − 2 y | **721** | 1790938800 | **2026-09-02 12:00** → 2026-10-02 12:00 |
| 2018-01-01 | **721** | 1790938800 | **2026-09-02 12:00** → 2026-10-02 12:00 |

Identical `last`, identical range. `since` is an **incremental-update cursor, not a history seek**.
721 is a **hard REST ceiling** and I am stating that plainly because it changes what the store is
*for*: **the store is not an optimisation of the REST read, it is the only way to reach bars older
than 30 days, full stop.** Any design that treats `pages` or `since` as a depth lever is wrong.

### 3.2 `pages` is worse than inert — it is a replay loop (new finding)

`_page_candles` (`data.py:946-956`) breaks on `last == 0` or an empty batch. Kraken never returns
`last == 0` on a live pair; it returns the *forming* bar's timestamp, which is non-zero. So the
break never fires and `cursor = last` never advances past page 1. Measured:

```
pages=1 ->   721 raw candles (721 unique), 1 API call, 0.2s
pages=2 ->   723 raw candles (721 unique), 2 API calls, 0.1s
pages=3 ->   725 raw candles (721 unique), 3 API calls, 0.1s
pages=6 ->   731 raw candles (721 unique), 6 API calls, 0.3s
```

Instrumented call-by-call for `pages=6`:

```
call 1 (since=None):              721 rows, last=1790942400, NEW bar times=721
call 2 (since=1790942400):          2 rows, last=1790942400, NEW bar times=0
call 3 (since=1790942400):          2 rows, last=1790942400, NEW bar times=0
call 4 (since=1790942400):          2 rows, last=1790942400, NEW bar times=0
call 5 (since=1790942400):          2 rows, last=1790942400, NEW bar times=0
call 6 (since=1790942400):          2 rows, last=1790942400, NEW bar times=0
TOTAL: 6 API calls, 721 new bar times, 10 duplicate rows discarded by data.py:1031
```

**Calls 2–6 are byte-identical requests.** The default `pages=6` (`data.py:962`, `data.py:1056`)
buys 5 wasted API calls and 0 extra bars per read, on every `train`, `backtest` and `paper-trade`
tick. A `if last == cursor: break` guard is the whole fix (and belongs in CAND-4 — see §6.2).

### 3.3 Sizing the push-down

| | |
|---|---|
| Change | `train.py:215-226` and `backtest.py:400-419` gain `since=`/`until=` from `resolve_data_window(cfg)`, converted to epoch seconds |
| Plumbing that already exists | `read_ohlc_dataframe(since=, until=)` (`data.py:1057-1058`) → `store.read` (`data.py:1191`). `resolve_data_window` already parses ISO-8601 bounds into `pd.Timestamp` (`data_window.py:100+`). Nothing in the seam needs touching. |
| Sibling to copy | `paper_trade.py` and `backtest.py` already read `market_data_store` from run config (`backtest.py:414-419`) — same pattern, same line |
| Non-store leg | **Still `fetch_ohlc_dataframe` and still 721 bars.** `fetch_ohlc_dataframe` accepts `since` and forwards it to `_page_candles` (`data.py:964,1021`) — where §3.1 proves it does nothing. So the push-down must be documented as store-only, or it will read as a fix that isn't one. |
| Diff size | **~15 lines** across two files |
| Risk if done without a store | **None** (verified inert) but also **no benefit**, and it invites the reader to believe `since` works on REST. |

### 3.4 Is it correctness or efficiency? **Efficiency — measured.**

With a seeded store, `data_window.clip_to_window` (the post-hoc clip both callers use today)
produces **the same bars as the pushed-down read**:

```
post-hoc clip   : 2158 bars 2021-01-01 00:00 -> 2021-03-31 23:00
pushed-down read: 2158 bars 2021-01-01 00:00 -> 2021-03-31 23:00
IDENTICAL bar times: True
```

One real difference, found by comparing NaN counts rather than values:

```
NaN counts, post-hoc clip : vwap 1, vwap_dev 1, trade_count_zscore_20 0,  volume_per_trade 1
NaN counts, pushed-down    : vwap 1, vwap_dev 1, trade_count_zscore_20 19, volume_per_trade 1
max abs diff where both defined: 1.46e-13
```

Pushing `since` in means `trade_count_zscore_20`'s 20-bar rolling window has **19 bars of warm-up
inside the returned frame instead of reaching back across the window boundary**, so those 19 bars
become NaN → `ffill().fillna(0.0)` → a 0.0 z-score instead of a real one. Beyond bar 20 the two
agree to 1.5e-13. So the push-down is a small *regression* at the leading edge, in exchange for:

| | whole store | pinned window |
|---|---|---|
| `read_ohlc_dataframe` | 0.42 s (76,561 bars) | **0.187 s** |
| `FeaturePipeline.compute` | 0.12 s | **0.04 s** |

**Verdict: worth ~15 lines, but only as a documented store-only efficiency change, and only
after the store is seeded. It is not a prerequisite for anything else and should not be led
with.** If a `since`-aware walk-forward (CAND-7) ever needs per-episode bounded reads, the same
15 lines become the enabling seam — so the cheap thing is to land them *with* CAND-3's store, not
as a separate claim.

**One genuine correctness win that *does* need the push-down, and is worth naming:** with a store,
`read_ohlc_dataframe` currently reads the **whole store** and then clips. The audit's claim that
"`data_window` is only a post-hoc mask on a trailing fetch" is *true of the non-store leg* and
**mildly overstated for the store leg** — with the store there is no clipping-to-empty failure
mode, only wasted work. The real `NotEnoughDataError` risk (`data.py:1297-1298`) is confined to
runs **without** `market_data_store`.

---

## 4. CAND-4 — retry / backoff / partial salvage (Q4)

### 4.1 The gap, restated exactly

`data.py:946-956`:

```python
collected: list[Any] = []
cursor: int | None = since
for _ in range(pages):
    batch, last = manager.ohlc(pair, interval=interval, since=cursor)   # no try
    if batch: collected.extend(batch)
    if last == 0 or not batch: break
    cursor = last
return collected
```

No `try`, no retry, no backoff, no sleep, no salvage. Pages 0..k−1 are discarded and the exception
propagates through `fetch_ohlc_dataframe` → `read_ohlc_dataframe` → `train_ticker` →
`cmd_train`'s blanket handler (`cli.py:511-516`), which prints `Error training …` and returns 1.

### 4.2 What already exists — this is the whole answer to "which library"

The repo's dependency set is `kraken-python, pandas, numpy, gymnasium, pyyaml` plus sibling
`kraken-market-data, pyarrow, requests` (`flake.nix:62,74`). **No `tenacity`. No
`HTTPAdapter`/`urllib3.Retry` mounting.** But:

| layer | retry? | backoff? | throttle? | where |
|---|---|---|---|---|
| `kraken_api/transport.py:_request` | **no** — `raise_for_status()` then bare `raise` | no | `_throttle()` (`min_interval`, **default `0.0`**) | `transport.py:179-217` |
| `kraken_api` error classification | yes — `RateLimitError` on `_RATE_LIMIT_MARKERS = ("EAPI:Rate limit exceeded", "EGeneral:Too many requests")` | — | — | `transport.py:45-48, 249-250` |
| **`market_data.client.KrakenClient._public`** | **YES** | **YES** — `backoff_seconds(attempt, base=retry_backoff)` | **YES** — `RateLimiter(min_interval)` | `client.py:114-147`, `utils.py:180-211` |
| `data._page_candles` | no | no | no | `data.py:946-956` |

**The retrying client is already written, already tested, already a dependency of the bot, and
already implements the exact duck type `_page_candles` consumes** (`ohlc(pair, interval, since) ->
(candles, last)`). Its knobs are env-overridable (`KRAKEN_RETRY_BACKOFF`) and it defaults to
`retry_backoff=0.5`, `timeout=20.0`.

So the options, cheapest first:

1. **Swap the source — 1 line per call site.** `read_ohlc_dataframe` already has
   `market_data_source` (`data.py:1066, 1165`) documented as "Anything exposing
   `ohlc(pair, interval, since) -> (candles, last)` works (a live `KrakenManager`, **the store's
   own thin client**, or a fake in tests)." Passing `market_data_source=market_data.client.KrakenClient.from_env()`
   buys retry+backoff+throttle for free. **This is the recommendation** — it is house-style
   (duck-typed seam already declared), needs no new dependency, and is testable with the existing
   fake-source pattern in `tests/test_rl_data_store.py`.
2. **`HTTPAdapter(max_retries=urllib3.Retry(...))` on the session** — `requests` and `urllib3` are
   both already present transitively. Fine, but it puts the policy inside a third-party object where
   the repo's structured-logging convention (`log_event`) cannot see it, and it cannot do
   **partial salvage** (which is the more valuable half).
3. **`tenacity`** — a new dependency. The retry logic here is ~15 lines; adding a dependency for
   it is not worth it, and it would be the only one in the repo.

### 4.3 Rate limits: published, and they did not reproduce — reported honestly

Kraken's published spot REST limits (`docs.kraken.com/api/docs/guides/spot-rest-ratelimits`):
Starter **max counter 15, decay −0.33/sec** (≈ one call per 3 s sustainable); Intermediate 20 /
−0.5/s; Pro 20 / −1/s. Error strings: `"EAPI:Rate limit exceeded"` and
`"EService: Throttled: [UNIX timestamp]"` — the latter **carries a retry-after timestamp**.
"If the rate limits are reached, additional calls will be restricted for a few seconds (or
possibly longer if calls continue to be made while the rate limits are active)."

**Measured: a burst of 16 rapid keyless `/public/OHLC` calls from this host returned 16/16 OK, and
8 calls at 2.5/s returned 8/8 OK.** No throttle. The counter is documented as per-API-key
("Each API key's counter is separate"), and the RL read path is keyless. So:

- **Refinement of the audit's implicit framing:** rate limiting is **not** the observed exposure on
  the keyless public path. Transient network failure and 5xx are. CAND-4 should be scoped as
  *resilience*, not *rate-limit compliance*.
- **The retry parser should still handle `EService: Throttled: [UNIX timestamp]`** (honour the
  timestamp) and `EAPI:Rate limit exceeded` (back off). Both are cheap and both are the documented
  behaviour if a key or a higher tier is ever introduced.
- **The 5-wasted-calls finding (§3.2) is the concrete rate-limit win** available today: it takes
  the default read from 6 calls to 1 with no behaviour change.

### 4.4 Proposed shape (~40 lines, no new dependency)

`_page_candles` gains three things, all inside the one function:

```python
_RETRYABLE = (KrakenError, requests.RequestException, OSError, TimeoutError)
_CAND4_MAX_ATTEMPTS, _CAND4_BACKOFF = 4, 0.5        # base seconds; cap 8.0

for page in range(pages):
    for attempt in range(_CAND4_MAX_ATTEMPTS):
        try:
            batch, last = manager.ohlc(pair, interval=interval, since=cursor)
            break
        except _RETRYABLE as exc:
            if attempt == _CAND4_MAX_ATTEMPTS - 1 or not collected:
                raise                       # nothing to salvage -> fail loud
            delay = backoff_seconds(attempt, base=_CAND4_BACKOFF, cap=8.0)
            if isinstance(exc, RateLimitError):   # honour "EService: Throttled: <ts>"
                delay = max(delay, _retry_after(exc) - time.time())
            _LOGGER.warning("ohlc page %d attempt %d/%d failed (%s); retrying in %.1fs",
                            page, attempt + 1, _CAND4_MAX_ATTEMPTS, exc, delay)
            time.sleep(delay)
    if last == 0 or not batch or last == cursor:   # <-- the cursor-advance guard
        break
    cursor = last
```

Five behaviours, and the reasons each earns its lines:

1. **Retry with capped exponential backoff** — `backoff_seconds` is already in the sibling
   (`utils.py:205`), so the house primitive is reused rather than reinvented.
2. **Fail loud on page 0, salvage after.** A failure on page 0 has nothing to salvage. A failure on
   page *k* with pages 0..k−1 already collected should **return what it has and WARN** — this is
   precisely `engine.py:57-74`'s pattern, and it is why the audit's contrast is the right standard.
   Note that because of §3.2 the salvage is almost always sufficient: pages 2+ are byte-identical
   replays of page 1, so **any single successful call already yields the full 721 bars.**
3. **Parse `EService: Throttled: [UNIX timestamp]`** and honour it — the only documented
   retry-after signal Kraken emits.
4. **`if last == cursor: break`** — kills the replay loop, 1 line, and turns `pages=6` into 1 call.
5. **Gap detection on the way out.** `fetch_ohlc_dataframe` de-dupes by timestamp
   (`data.py:1031`) but never checks for **missing bars**. A one-line
   `(df.index.to_series().diff() > interval).sum()` log line — not an error, a measurement —
   would make the salvage path honest: "I gave you 480 of 721 bars and here is the hole."

Lines-to-value: **~40 lines, zero new dependencies, zero data cost, and it removes both the
abort-on-transient failure and the 5× API amplification.** This is the best value-per-line item in
the whole audit.

### 4.5 Contrast with `engine.py:57-74`

`engine.py` is the correct model and should be quoted as such: three independent `try/except
Exception` blocks, each degrading to `_LOGGER.warning` and a *partial* dict
(`data["candles"] = candles[-100:] if candles else []`, `engine.py:66`). Three differences worth
copying deliberately:

- `except Exception` is broader than my `_RETRYABLE` — right for `engine.py`, because a *strategy*
  must never die on a market-data hiccup. For `_page_candles` a narrower tuple is better, because
  a `ValueError` from `manager.ohlc` (bad interval, bad pair — `manager.py:172-175`) is a
  programming error that retrying cannot fix.
- `engine.py` degrades each key independently; `_page_candles` must degrade *as a whole*, because
  a partial candle list that silently loses its tail is worse than a loud failure — unless it is
  logged with the bar count, which is why item 5 above is in scope.

---

## 5. CAND-5 — the matrix gates on replicate *count*, never on observed *dispersion* (Q5)

### 5.1 The gap, restated exactly

| Fact | Where |
|---|---|
| `MIN_REPLICATES_FOR_A_CLAIM = 3`, used **only** as a count | `model_matrix.py:243`; seeds `:1276-1283`; groups `:1339-1347`; claims `:2259-2280` |
| The dispersion machinery is **already computed** and printed, never compared | `summarize()` → `q1`/`q3`/`min`/`max` (`:328-339`); `quartile()` (`:310-325`); `fmt_iqr()` (`:363-365`); rendered `:1952`, `:2094` |
| `is_valid()` checks only process failure, required fields, `n_bars` vs a denominator, `num_trades`, NaN metrics | `is_valid` `:963-978` → `assess_cell` `:786-878` |
| The docstring's own framing ("separated from noise") is never operationalised | `:241-242`, `:1281-1282` |
| Motivating measurement in-repo: two fetches of the same pair "disagree by 16% on the fitted std of `rsi_24`" | `data_window.py:5-8` |

The prose in `_build_claims` (`:2208-2340`) is genuinely good and already says the right thing in
words — *"a median cannot be separated from seed noise"*, *"PPO on one config has been measured
replaying 576 trades and 374"*, and the whole `n=1 → anecdote` branch. **It never checks.** The
report tabulates `median [q1, q3]` per group and lets a reader eyeball it. Nothing computes
whether two arms separate.

### 5.2 What a count gate cannot see — demonstrated with the harness's own functions

Run against `model_matrix.summarize` / `fmt_iqr` / `MIN_REPLICATES_FOR_A_CLAIM` as they stand:

| # | arms | median | IQR `[q1, q3]` | n | count gate | pooled IQR | gap | dispersion gate |
|---|---|---|---|---|---|---|---|---|
| 1 | control / treatment | +2.00% / +6.00% | `[+2.00,+2.05]` / `[+5.95,+6.05]` | 3 / 3 | **ok** | 0.075 pp | 4.00 pp = **53×** | **RESOLVED** |
| 2 | control / treatment | +30.00% / +33.00% | `[+16.00,+35.00]` / `[+18.00,+37.50]` | 3 / 3 | **ok** | **19.25 pp** | 3.00 pp = **0.16×** | **NOISE** |
| 3 | control / treatment | +2.00% / +4.00% | `[+1.60,+3.75]` / `[+3.50,+5.75]` | 3 / 3 | **ok** | 2.20 pp | 2.00 pp = **0.91×** | **NOISE** |
| 4 | A(576 trades) / B(374 trades) | +2.00% / −3.00% | `[+2.00,+2.00]` / `[−3.00,−3.00]` | 1 / 1 | FAILS | **0.00 pp** | 5.00 pp = **∞** | **UNDEFINED** |

Case 2 is the whole argument: a **3 pp difference with a 19 pp within-group spread** sails through
`len(seeds) >= 3` and is tabulated as `+30.00% [+16.00%, +35.00%]` vs `+33.00% [+18.00%,
+37.50%]` — two intervals that overlap over almost their entire length. Case 3 is the realistic
middle: a 2 pp effect against 2.2 pp of seed spread, which the count gate reports and a reader
should not believe.

**Case 4 is a design constraint I hit while building this, and it must be respected by the fix:**
when every arm has `n == 1`, `q3 − q1 == 0`, so a pooled-IQR gate is **degenerate** and would
report `5.00 pp = ∞ → RESOLVED`, i.e. it would happily bless the exact anecdote the surrounding
prose is trying to kill. **The dispersion gate must sit *behind* the count gate, not beside it**,
and must additionally require `n ≥ MIN_REPLICATES_FOR_A_CLAIM` **and** `pooled_iqr > 0` on both
arms before it emits anything.

### 5.3 Sizing the fix (~50 lines, zero new data, zero new deps)

**Estimator.** Pooled within-group spread = **median of the per-group IQRs** (`q3 − q1`). Chosen
over `max` or `mean`: `max` lets one wild group condemn every comparison (I hit this — my first
draft used `max` and case 1 collapsed from 53× to 0.21×), `mean` is not robust to the same group.
`median` of the IQRs is the same order-statistic the harness already uses in `median()`
(`model_matrix.py:296-308`), so it is house-consistent and needs no new concept. It also degrades
gracefully at n=3, where a group's own IQR is a 50th-percentile-of-2 estimate — which is exactly
why the gate reports a **ratio**, not a boolean, and why `n=3` is already labelled "ordering
hints, not evidence" in the prose.

**Four edits, all inside existing functions:**

| # | Site | Change | ~lines |
|---|---|---|---|
| 1 | new `pooled_within_spread(summaries)` next to `summarize()` (`:328`) | median of `q3 − q1` over groups with `n ≥ MIN_REPLICATES_FOR_A_CLAIM`; `None` if fewer than 2 such groups or if the result is `0` | 15 |
| 2 | `_group_summaries()` (`:1345`) row dict | add `"separation"`: the same ratio per group pair is computed in `_build_claims`, so here just carry `summary` through (already does) | 0 |
| 3 | `_build_claims()` (`:2208`) | for each adjacent pair of arms in the headline cohort, emit one line: `gap = \|median_A − median_B\|`, `ratio = gap / pooled`, verdict `RESOLVED (≥1×) / NOT SEPARATED (<1×) / UNDEFINED (n<3 or zero spread)`. Replaces the `thin`/else branch at `:2259-2280` rather than adding to it | 25 |
| 4 | `_print_group_table()` (`:1930`) header | print the pooled IQR once under the table, so a reader sees the yardstick the verdicts used | 10 |

**Why this is ~50 lines and not a rewrite:** `q1`/`q3`/`median`/`n` are already in every row
(`_group_summaries` `:1345` → `"summary": summarize(values)`), already survive into the JSON
report (`:2079-2082` `"per_ticker"/"per_config"/"per_axis"`), and are already printed
(`fmt_iqr` `:363`). **The measurement exists. Only the comparison is missing.** There is no new
data collection, no new run, no schema change — the gate can be validated against any existing
results JSONL the moment it lands.

**Why the ratio is the right output, not a pass/fail.** A boolean needs a threshold, and the
threshold is a judgement call. A ratio is a fact: *"this 2 pp gap is 0.9× the within-group spread
you measured."* The prose can then say what to do with it, which is the harness's established
voice. The report should still be able to fail a run — `cmd_report` already returns non-zero on
no-valid-cells, so a `separated: false` can raise a `warn(...)` in `cmd_plan`'s
`thin_replication` neighbourhood (`:1339`) with zero new plumbing.

**One honest limitation to state in the docstring.** With n=3, a group IQR is a percentile of two
values; the pooled estimate inherits that. The gate is therefore a **necessary, not sufficient**,
condition — it catches "the seeds you have do not resolve this effect", which is exactly the
wording in my brief. It does not manufacture power. Its value is that it stops a matrix report from
presenting case 2 as a finding, and the cheapest way to get more power remains what the harness
already says: 5+ seeds.

---

## 6. Do CAND-4 and CAND-5 gate the others? (Q6)

### 6.1 CAND-5 is a **precondition** for gating any other change on a claimed effect — including CAND-3's

Stated plainly, because this is the load-bearing conclusion of my brief:

- **Nothing can be validated by this repo's own harness until CAND-5 lands.** `tools/model_matrix.py`
  is the only instrument in the project that turns a config change into a number, and today it
  reports a 3 pp difference across a 19 pp spread (case 2) identically to a 53× resolved one
  (case 1). Any claim of the form "change X improves the model" is therefore unmeasurable until
  the dispersion gate exists.
- **This includes CAND-3.** The moment a store is seeded, the first question is "does 8 years of
  bars beat 721 bars?" — which is a headline-metric comparison between two arms. Without CAND-5
  that comparison's verdict is a coin flip dressed as a measurement. **Seed the store and evaluate
  it without CAND-5, and you will produce a number you cannot defend.**
- **It also gates CAND-1, CAND-2, CAND-4, CAND-6, CAND-7** by the same argument. CAND-1's
  `microstructure` group is one column; CAND-2's signal channels are 8 columns; both are
  effect-size questions.
- **But CAND-5 does not gate the *build*.** It gates the *claim*. The store, the retry guard, the
  signal backfill are all worth building blind, because their value does not depend on the harness
  measuring them correctly — they are unambiguous. What CAND-5 gates is the decision to **ship
  or revert based on a reported effect**.
- **Ordering that follows:** land CAND-5 first or in the same change as anything whose acceptance
  criterion is a matrix number. It is 50 lines and no data. It is the cheapest gate in the repo.

### 6.2 CAND-4 is **not** a precondition for the G3 push-down — but it lands *with* the store change, for a different reason

- **Not a correctness gate.** §3.4 proved the pushed-down read and the post-hoc clip produce
  identical bar times, with values agreeing to 1.5e-13 beyond the 19-bar warm-up edge. Retrying
  does not change which bars you get.
- **Not a sequencing gate.** Nothing in CAND-3 fails without it, and nothing in CAND-3 is fixed by
  it.
- **But they should land together**, for a reason that is specific and cheap: the store change is
  the moment the pipeline starts reading years of bars, and `_page_candles` is invoked on the
  store leg too (`data.py:1174`). At that point the 5-wasted-calls-per-read replay loop (§3.2)
  goes from "annoying" to "6 API calls per tick, per pair, forever", and the salvage path stops
  being hypothetical because a read failure now means "the tail of my training window is missing".
  Landing the cursor-advance guard (`if last == cursor: break`) **with** the store flip costs
  1 line and removes 5/6 of the API load the store introduces.
- **So: CAND-4 is a precondition for the store change being safe to operate, not for it being
  correct.** If the store work slips, CAND-4 can land alone and still pay for itself (it removes
  5 wasted API calls from every existing read *today*).

### 6.3 Recommended landing order

| order | item | ~lines | data cost | why here |
|---|---|---|---|---|
| 1 | **CAND-5** dispersion gate | ~50 | none | gates every claimed effect, incl. #3's own acceptance |
| 2 | **CAND-4** cursor guard + retry/salvage | ~40 | none | removes 5 wasted calls *today*; makes #3 safe to run |
| 3 | **CAND-3a** seed the store + flip `market_data_store` | **0** | 158 s, 13 MB, `/tmp` | pure config; 721 → 76,561 bars proved |
| 4 | **CAND-3b** `since`/`until` push-down + venue label in `_meta.json` | ~21 | none | store-only efficiency; documented as such |
| 5 | *(deferred)* Kraken OHLCVT reader | ~300 | 10.5 GB | **not recommended** — §2.4 |

**CAND-3a is the cheapest item in the entire audit: 158 seconds, 13 megabytes, 3 commands, and one
YAML key. It should not be waiting behind anything.** What waits behind CAND-5 is only the claim
that it worked.

---

## 7. Findings not in the audit

1. **§3.2 — `pages` is not merely non-scaling, it is a replay loop.** `last` never advances past
   page 1 (`last == 0` is dead code on a live pair), so calls 2..N are byte-identical requests.
   Measured: `pages=6` → 6 calls, 721 unique bars, **identical to `pages=1`**. 10 duplicate rows
   are collected and discarded at `data.py:1031`. Fix: `if last == cursor: break`.
2. **§1.3 — `kraken-deep-history`'s dev shell cannot produce a bot-readable store.** It has only
   pytest; `market_data` is not importable, so `_open_store` silently degrades to
   `fallback-csv` and writes `.csv` files that `MarketDataStore.read` (which globs `*.parquet`)
   cannot see. Only the `store_mode` field in the JSON report distinguishes them.
3. **§3.4 — the audit's "`data_window` is only a post-hoc mask → clips to empty" is overstated for
   the store leg.** With a store there is no clipping-to-empty failure mode; there is wasted work.
   `NotEnoughDataError` from a pinned window is confined to runs **without** `market_data_store`.
4. **§3.4 — the push-down has a 19-bar warm-up cost.** `trade_count_zscore_20` goes from 0 to 19
   NaNs at the window's leading edge, because the 20-bar rolling window can no longer reach back
   across the boundary. Beyond bar 20 the two paths agree to 1.5e-13.
5. **§4.3 — Kraken's published rate limits did not reproduce on the keyless public path.** A burst
   of 16 rapid `/public/OHLC` calls and 8 calls at 2.5/s both returned 100 % OK. CAND-4 should be
   scoped as *transient-failure resilience*, not *rate-limit compliance* — the exposure is 5xx and
   connection resets, not 429s.
6. **§2.1 — Kraken's OHLCVT article contradicts its own MANIFEST on intervals.** The intro
   paragraph lists 30 minutes; the "About the data" section and every real filename omit it. A
   reader written from the prose would request a non-existent file.
7. **§2.1 — Kraken's bulk OHLCVT has no `vwap` column**, so a store seeded from it would
   **silently lose `vwap_dev`** — one of the 52 bare observation columns — via the presence gate at
   `data.py:846`. The bulk archive is strictly poorer on shape than the REST endpoint it replaces.
8. **§1.1 — a store is 60 bytes/bar.** 204,245 bars across three tickers and 8.7 years of 1 h data
   is 13 MB. Any future "we cannot afford the disk" objection is answered.

---

## 8. Reproduce this

```bash
# 1. store build cost (158 s, 13 MB, no code)
cd ~/Projects/kraken-market-data
nix develop --command bash -c "
  PYTHONPATH=\$HOME/Projects/kraken-deep-history:\$HOME/Projects/kraken-market-data \
  python \$HOME/Projects/kraken-deep-history/cli.py seed \
    --ticker ETH/USD --interval 60 --from 2018-01-01 --store /tmp/krb-cand3/store2"
du -sh /tmp/krb-cand3/store2          # 4.6M, 104 parquet files

# 2. it is a config flip (76,561 bars, 0.48 s, zero code)
cd ~/Projects/kraken-trading-bot
nix develop --command bash -c "python -c \"
from kraken_trading_bot.rl.data import read_ohlc_dataframe
df = read_ohlc_dataframe('ETH/USD', 60, pages=1, market_data_store='/tmp/krb-cand3/store2')
print(df.shape, df.index[0], df.index[-1])\""

# 3. since is a no-op on REST (Kraken's own words + measurement)
curl -s https://docs.kraken.com/api/docs/rest-api/get-ohlc-data | grep -o 'older data cannot be retrieved[^.]*'

# 4. pages replays (5 of 6 calls are identical)
nix develop --command bash -c "python -c \"
from kraken_api import KrakenManager
from kraken_trading_bot.rl.data import _page_candles
m = KrakenManager.from_env()
for p in (1,2,6):
    print(p, len(_page_candles('ETH/USD',60,p,m,None)))\""

# 5. cross-venue basis (Kraken OHLCVT vs Binance, 2,184 real bars)
#    ranged GET of the ZIP central directory + MANIFEST.json, then diff vs
#    https://data.binance.vision/data/spot/monthly/klines/ETHUSDT/1h/ETHUSDT-1h-2026-0{4,5,6}.zip
#    (both archives extracted under /tmp/krb-cand3/ — not committed)
```

Everything lives under `/tmp/krb-cand3/`. Nothing was written to the repo but this file.

---

*End of RESEARCH-3. Nothing was written, committed or deleted outside this file.*
