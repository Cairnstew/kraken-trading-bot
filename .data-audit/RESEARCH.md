# RESEARCH.md — synthesis of the three Phase-2 surveys (2026-10-08)

Team `audit-pipeline-1008`. Assembled by the lead from `RESEARCH-1.md`,
`RESEARCH-2.md`, `RESEARCH-3.md` (each ~13–19 KB; read those for the full
detail). Each survey answered: which libraries/APIs/patterns could source or
implement one top gap from `AUDIT.md`, scored partly on how cheaply they reduce
to a per-`(ticker, timestamp)` scalar the RL seam can consume.

---

## Bottom-line comparison

| Gap | Category | Outcome type | Top recommendation | New deps | Effort | Width Δ |
|---|---|---|---|---|---|---|
| **G-1** depth→RL | microstructure | IMPROVE-EXISTING | Reader-side flattener in `data.py`; 2 new allow-list cols; `order_book_imbalance` lights up unchanged | none (pandas/numpy) | small–medium | **+1** |
| **G-2** trade tape | microstructure | NEW-DATA-SOURCE | Kraken WS `trade` (live) + Binance aggTrades archive (seed) | none beyond sibling pattern (ccxt optional) | medium | +2–3 |
| **G-6** cross-exchange basis | cross-exchange | NEW-DATA-SOURCE | Extend `kraken-deep-history` with a `basis` subcommand; Kraken store vs Coinbase USD | none | medium | **+1** |

All three reduce to the same consumer tail: add column name(s) to
`_SIGNAL_COLUMNS` (`kraken_trading_bot/rl/features.py:94-125`), optionally a 4th
`_SIGNAL_CHANNELS` entry (`data.py:167-198`) + config key, and the observation
picks them up. **No specialised microstructure library is worth adding for any
of the three** — the reduction is ~20 lines of pandas `groupby` in each case.

---

## G-1 — wire the recorded order-book depth into the RL observation (IMPROVE-EXISTING)

**The headline correction:** the merge seam **cannot read the depth file as-is**
— a naive `_SIGNAL_CHANNELS` entry would *silently return the frame untouched*
(the width guard still passes; the column is just 0). Three shape mismatches:

| # | seam expects (`data.py:633`) | depth record carries | consequence |
|---|---|---|---|
| C1 | a `timestamp` column (`data.py:778-780`) | `recorded_at` + `hour`; no `timestamp` | merge logs a WARNING and `return df` |
| C2 | `ticker` (`data.py:160`, `_filter_ticker`) | `pair: "ETH/USD"`; no `ticker` | per-ticker filter silently off |
| C3 | flat scalar columns | nested `bids`/`asks` object cells | `_SIGNAL_COLUMNS` intersection drops them |
| C4 | — | depth `spread` | **collides** with funding's `spread` under `groupby.last` |

**Recommended design (Design A — reader-side flattener):**
- Add a pure ~30-line `_flatten_orderbook_records()` in `data.py`, gated on the
  `bids`/`asks` shape, emitting `timestamp←recorded_at`, `ticker←pair`,
  `bid_vol`/`ask_vol` (top-10 level-volume sums), and `realized_spread_bps`
  (**never** `spread`). Keeps `depth_recorder.py`'s append-only schema frozen.
- Add `bid_vol`/`ask_vol` to **both** `_SIGNAL_COLUMNS` and
  `_SIGNAL_BUILDER_INPUT_COLUMNS` (`features.py:94-125,149`) so only the derived
  `order_book_imbalance` reaches the observation → net width **+1**.
- `order_book_imbalance` (`features.py:1018-1026`) then lights up **unchanged** —
  no `features.py` math change.
- Append a 4th `_SIGNAL_CHANNELS` entry + config key; thread through 6 call
  sites (`data.py:1243,1908`; train/backtest/export/paper_trade).

**Libraries:** none worth adding (`orderbook`/`lob` are matching engines,
`hftbacktest` is a backtester, `mlfinlab` is commercial/404, `cryptofeed` is a
feed). Pure pandas/numpy scores 5/5 on reduce-to-scalar.

**Risks:** (1) +1 width invalidates existing trained models (retrain required —
none exist yet, `models/` is empty); (2) coverage is forward-only ~100 h, so the
column is 0.0 for >98% of multi-year history; (3) the flattener must handle
`depth.truncated` (Kraken silently serves 100) and empty sides.

---

## G-2 — trade tape + realized spread (NEW-DATA-SOURCE)

**Top pair: Kraken WS `trade` channel (v2, live) + Binance aggTrades archive
(deep seed).** The Kraken REST tape is too shallow to seed (`/0/public/Trades`
caps at 1000 trades; `/0/public/Spread` at ~200 samples); it is good only for
live top-ups. The Kraken WS `trade` channel is keyless with a 50-trade snapshot
plus live stream and taker side; the full `SpotWebSocket` wrapper already exists
upstream (`websocket.py:54`; `cli.py:179` even has a `ws` subcommand) — nothing
calls it. Binance aggTrades (`data.binance.vision`) is keyless, years deep since
2017 (~567 MB/month BTCUSDT), and the sibling `kraken-deep-history` already
proved the stdlib-only client pattern; Kraken has no equivalent archive.

**Libraries:** none specialised. ccxt (MIT, v4.5.85, actively maintained) is the
only library worth considering, and only for multi-exchange sourcing. The
reduction to per-bar scalars (signed volume, OFI, taker ratio,
`realized_spread_bps`, effective spread, VPIN) is ~20 lines of pandas `groupby`.

**Seam:** output per-`(ticker, bar_hour)` JSONL via `merge_extra_features`
(`data.py:633`); use the already-reserved `realized_spread_bps`
(`features.py:145-146`), never `spread`.

---

## G-6 — cross-exchange spot basis (NEW-DATA-SOURCE)

**Top recommendation:** extend the sibling `kraken-deep-history` with a `basis`
subcommand (reusing its Binance-archive client + `ETH/USD→ETHUSDT` map); base leg
= the Kraken store, counter-venue = **Coinbase USD**; emit `venue_basis_bps`
JSONL; add **one** `_SIGNAL_COLUMNS` entry. No new dep, no store-schema change.

**Key measurement [M]:** on 350 common hourly ETH closes, Kraken–Coinbase(USD)
basis mean **+0.07 bp**, sd **1.02**; Kraken–Binance(USDT) mean **−2.80 bp**, sd
1.69; Binance–Coinbase **+2.87 bp**. The Binance leg carries a **USDT/USD
stablecoin premium** (a level shift) — the audit's `+5.41 bp` is exactly that. A
**USD-vs-USD** pair (Kraken vs Coinbase) is the clean venue-dislocation signal.

**Sources, all keyless:** Binance archive `data.binance.vision` monthly klines
(HTTP 200, 38 KB/mo, 2017→); Coinbase Exchange `/products/{id}/candles` (keyless
to ≥2020-06, 350 rows/req, 10 req/s); Binance REST `/api/v3/klines`; Kraken
`/0/public/OHLC` (721-bar cap, confirmed). Top scored: Binance archive 24/25,
Coinbase 22/25.

**Constraints:** the store is single-venue keyed
`store/{PAIR}/{interval}/*.parquet` (no venue dimension), so the basis must be a
**signal JSONL**, not a second store leg. It is **orthogonal** to the existing
perp basis (`kraken-funding-rates` `basis = (mark−index)/index`, one venue);
G-6 is spot-vs-spot between venues. Name must be `venue_basis_bps` (`basis`,
`spread`, `realized_spread_bps` are taken).

---

## Cross-cutting notes for the architect

- **G-1 is the highest-directness item and is IMPROVE-EXISTING**: the data is
  already on disk and the feature column already coded. Its one real cost is the
  record-shape flattener (C1–C4), not a new fetch.
- **G-2 and G-6 are both NEW-DATA-SOURCE** and both reuse the sibling
  `kraken-deep-history` Binance-archive pattern (G-2 as a tape seed; G-6 as a
  venue leg) — so either could be implemented *by extending that sibling* rather
  than creating a brand-new repo. Weigh "new sibling repo" vs "extend
  `kraken-deep-history`" explicitly.
- **All three change observation width**, so any is a retrain trigger (no models
  exist yet — `models/` is empty).
- **Coverage caveat:** G-1's depth is forward-only (~100 h) and G-2's live tape
  is forward-only; only the archive-seeded legs (G-2 deep tape, G-6 Coinbase)
  give multi-year depth. A gap whose value is "0 for 98% of history" should say
  so in DECISION.md.
