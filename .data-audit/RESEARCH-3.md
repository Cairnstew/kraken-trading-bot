# RESEARCH-3 — Market microstructure: trade tape + spread recorder (AUDIT Rank 3)

Read-only survey, researcher 3 of 3. Written against the working tree on disk
(2026-10-01). Overwrites the prior `RESEARCH-3.md` (G3 fetch reliability, kept
in git at `18343ce`/`17899e1`). Scope: **AUDIT.md Rank 3 — market
microstructure**, the one wholly-unbuilt producer: `recent_trades` (trade
tape) and `spread` (best bid/ask series) in `kraken-python` are called by
nothing, and `order_book_imbalance` (`features.py:414-418`) — the only
microstructure feature — has no producer for its `bid_vol`/`ask_vol`.

Anchored to the committed AUDIT Rank-3 text (git `18343ce`); the live on-disk
AUDIT.md is being re-titled "Gap 3 / deferred Candidate 6" by a concurrent
writer, with the same evidence: `engine.py:72` order-book fetch is discarded,
`recent_trades`/`spread` have zero bot call sites, `bid_vol`/`ask_vol` exist
only inside `order_book_imbalance`. Everything below was verified by reading
the running/committed code and the live Kraken API reference (URLs cited),
never recalled.

---

## 1. Endpoint / field confirmations

### 1a. `recent_trades` — `/0/public/Trades`

- **Sibling wrapper** `kraken_api/manager.py:192-196`:
  `recent_trades(pair, since=None) -> (list[Trade], last: str)`. It calls
  `client.trades(pair, since=)` (`client.py:123-129`), resolves the pair via
  the cached catalog, and reads `data.get(pub)`, then
  `str(data.get("last") or "")`.
- **API row** (live docs, `get-recent-trades`):
  `[<price>, <volume>, <time>, <buy/sell>, <market/limit>, <miscellaneous>,
  <trade_id>]`. Returned **up to 1000 trades per call** (`count` max 1000).
  `last` is described as *"ID to be used as since when polling for new trade
  data"* — an **opaque string id**, not a clock timestamp (`last` example
  `1688671969993150842`; a real epoch would be ~1.6e9).
- **`Trade` model** `models.py:359-410`: fields `price, volume, time, side
  (b/s), order_type (m/l), misc` parsed from the row. Two lossy details worth
  knowing before a tape recorder consumes it:
  - The public row's **7th element (`trade_id`) is dropped** by
    `Trade.from_public_row` (`models.py:368-372` pads to 6 and reads 0-5).
    Not a problem for aggregation (hours don't need per-trade ids; the cursor
    is `last`, which is all one needs to resume), but a raw-tape archive
    cannot recover ids from `Trade`.
  - `time` is stored as `int(vals[2] or 0)`; the API sends a **float with
    sub-second precision** (`1688669597.8277369`). Truncation is fine for
    hour buckets, loses nothing structural.
- **Keyless**: `/public/Trades` needs no credentials. `security: []` in the
  OpenAPI entry.

### 1b. `spread` — `/0/public/Spread`

- **Sibling wrapper** `manager.py:200-204`:
  `spread(pair, since=None) -> (list[SpreadPoint], last: int)`. Calls
  `client.spread` (`client.py:130-136`) — `public("Spread", params={pair, since})`.
- **API row** (live docs, `get-recent-spreads`): `[int <time>, string <bid>,
  string <ask>]`. *"Returns the last ~200 top-of-book spreads for a given
  pair."* `last: int` — *"ID to be used as since when polling for new spread
  data"* — an integer (epoch-ish), **not** an opaque id.
- **`SpreadPoint` model** `models.py:552-585`: `pair, time (int), bid (str),
  ask (str)` with `bid_decimal`/`ask_decimal`. The class docstring is
  explicit: **"there is no volume or size component"** — a spread-only route
  yields `spread` but never an imbalance, exactly as AUDIT Rank 3 notes.
- **`since` caveat** (live docs): spread data "since given timestamp… intended
  for incremental updates within available dataset (**does not contain all
  historical spreads**)". So the REST spread series is a rolling ~200-sample
  window; catch-up after a gap is bounded by what the window retained. This is
  the mechanism behind Rank 3's "the spread only exists if someone was
  listening".

### 1c. `order_book` — `/0/public/Depth` (the engine's already-running call)

- `manager.py:187-190`: `order_book(pair, count=None) ->
  OrderBook{asks, bids: list[BookLevel]}`. `BookLevel` (`models.py:189-211`)
  is `price, volume, timestamp` — **all strings**, with
  `volume_decimal`. Depth `count` max is 500 (live docs); the engine asks 10.
- `engine.py:72` fetches `order_book(pair, count=10)` every 60 s per pair and
  drops the sizes into `data["order_book"]`; `strategies/base.py:92` documents
  it as a tick input that `sma.py` never reads. **The level-1..10 volumes for
  `bid_vol`/`ask_vol` are already in RAM once a minute and thrown away.**
- `features.py:414-418`: `order_book_imbalance = (bid_vol - ask_vol) /
  (bid_vol + ask_vol)`, reading columns that **nothing anywhere emits.**

### 1d. The cursor pattern to reuse — `_page_candles` (`rl/data.py:691-703`)

```python
cursor = since
for _ in range(pages):
    batch, last = manager.ohlc(pair, interval=interval, since=cursor)
    if batch: collected.extend(batch)
    if last == 0 or not batch: break
    cursor = last
```

The shape transfers 1:1 to `recent_trades`/`spread` — both wrappers already
return `(rows, last)`. Two adaptations, both small:
- _Stop condition._ `_page_candles` tests `last == 0`; the tape wrappers return
  `last` as a string (trades) or int (spread). "Continue" becomes
  `bool(batch) and str(last) not in ("", "0")`.
- _Cursor type and fast-forward._ OHLC `since` is an epoch second; **trades
  `since` is the opaque id returned as `last`** — you cannot skip an hour by
  setting the clock, you can only resume from a persisted `last`. Spread
  `since`/`last` is an integer, so resume-from-`last` is equally mandatory but
  the number is a timestamp-ish id.

### 1e. Transport behaviour the recorder inherits (`kraken_api/transport.py`)

- **Keyless endpoints stay keyless** — `public()` needs no key/secret and the
  whole `kraken_api` public surface is credential-less (`auth.py:58-90`
  builds a client with `api_key=None` fine).
- **No automatic retry.** `transport.py` raises `RateLimitError` on the body
  markers `EAPI:Rate limit exceeded` / `EGeneral:Too many requests`
  (`:45-48`, `:249-250`) and re-raises `RequestException` (`:208-219`); there
  is no retry/backoff anywhere. The recorder must have its own (prior
  RESEARCH-3 G3 §1a already specs the correct shape: body-marker-aware, full
  jitter, `public()`-only safe).
- **Throttle footgun still live:** `KrakenManager.from_env(min_interval=…)`
  silently drops the kwarg — `auth.py:58-90` reads `KRAKEN_MIN_INTERVAL`
  **only from the env** — while `client_from_credentials` honours it
  (`auth.py:104`). A recorder that wants a 0.08 s minimum must set
  `KRAKEN_MIN_INTERVAL=0.08` in its environment (the `.env.example`
  documented value), not pass a kwarg.

---

## 2. Rate limits

- **Official (live `llms.txt`, quoted):** *"Rate limits: Spot REST has a call
  counter (max 15-20 depending on tier). Trading engine has per-pair counters
  with decay."* The sibling's `.env.example` matches verbatim: *"Spot REST is
  limited to ~15-20 calls/s by tier. Set e.g. 0.08 for ~12/s."*
- **Per-call ceilings that determine *cadence*, not budget:** Trades ≤1000
  rows, Spread ~200 latest samples, Depth ≤500. Completeness of the tape is a
  **frequency** question: at 3 pairs and a 30 s poll of both endpoints the
  recorder burns 3×2×2 = **12 calls/min = 0.2/s**, ~75-100× under the tier
  counter, while any busy pair stays ~hundreds of trades per 30 s window (well
  under the 1000-row cap). Polling 60 s is still fine (~6 calls/min). An
  hourly or 5-min cron is NOT a cadence question the limits answer — it is a
  **data-loss** question (§3, option ii); the 1000-row cap and the ~200-sample
  spread window both mean a long interval silently drops the tape.
- **Shared budget:** the counter is per-IP across all calls, so the recorder
  adds on top of the engine (3 calls/pair/min), paper trader (2/pair/min) and
  `kraken-market-data`'s timer. Sum is still ~0.5-1/s worst-case with the
  recorder at 60 s. No new throttling machinery is needed; keep the family
  default and set `KRAKEN_MIN_INTERVAL=0.08`.

---

## 3. Recorder shape options (per-hour scalars each can emit)

All scalars below are per-(ticker, hour) floats/ints written to a JSONL record
`{timestamp (floored hour), ticker, <scalars>}`, merged through
`merge_extra_features` (`data.py:281-497`) via a new `*_features_file` config
key joined into the tuple at `data.py:779-786`. **None of these names exists
in `_SIGNAL_COLUMNS` today** (`features.py:42-54`); each shipped scalar is a
one-line allow-list addition (see §5).

### Option (i) — In-repo: reuse the engine's `order_book` fetch, write sizes + spread to JSONL

- **What:** the engine already has `OrderBook{asks,bids}` for every pair every
  60 s (`engine.py:72`); accumulate per-hour and flush a row at hour close (or
  on tick at `:00`, latching the previous hour) via the same
  `write_jsonl(append=True)` pattern the funding sibling uses.
- **Scalars:** `spread` (mean relative top-of-book spread, bps), `bid_vol`
  (sum of the 10 bid-deck volumes), `ask_vol`. The existing
  `_add_microstructure_features` (`features.py:405-413`) emits `spread` from a
  `spread`/`bid`/`ask` column set and `order_book_imbalance` from
  `bid_vol`/`ask_vol` — so **exactly these three names light up both
  microstructure branches**.
- **Cannot emit:** any trade-tape scalar (taker imbalance, count, mean size,
  VWAP pressure) — the engine never fetches Trades.
- **Rate cost:** zero new REST calls; pure reuse.
- **Process:** none of its own — rides the engine. Consequence: the book
  series exists **only while the bot trades**; it is not a data source that
  survives idle periods, and a cron cannot reconstruct history it never
  polled. No cursor, no catch-up.

### Option (ii) — Sibling `kraken-*` recorder polling `recent_trades` + `spread` at its own cadence (RECOMMENDED)

- **What:** a keyless poller, one per pair (or one process over N pairs),
  `recent_trades(pair, since=<persisted last>)` + `spread(pair, since=
  <persisted last>)` on a 15-60 s loop; appends raw rows to an hourly raw file
  and persists both cursors in a `_meta.json` sidecar after every successful
  poll (the `kraken-market-data` cursor pattern, see §4), so a crash resumes
  losslessly. A `finalize` (hour-roll) pass folds the raw hour into the
  per-(ticker, hour) signal record and clears the raw file.
- **Scalars it can emit:**
  - `taker_buy_vol`, `taker_sell_vol` (base-currency volume by `side`),
    `taker_imbalance = (buy-sell)/(buy+sell)` — signed tape pressure.
  - `trade_count` (trades in the hour), `mean_trade_size` (base volume).
  - `vwap` (volume-weighted over the hour, quote/base), `last_price`, and
    `vwap_pressure = (vwap - last_last)/last_last` — how far the hour closed
    above/below its own VWAP.
  - `realized_spread_bps` (mean `(ask-bid)/mid` over spread samples) — the tape
    recorder's own best-bid/ask width.
  - Optionally (by also polling `Depth` every cycle, +1 call/pair): `bid_vol`,
    `ask_vol` → `order_book_imbalance`. This makes the sibling **the single
    microstructure producer** and renders `engine.py:72` redundant-but-harmless;
    a minimal v1 can omit Depth and leave the imbalance producer to the engine
    reuse (option i) or the later fold-in.
- **Rate cost:** 2 (trades+spread) or 3 (+depth) calls/pair/cycle → ~0.1-0.4
  calls/s for 3 pairs, ~50-150× under tier (§2).
- **Process:** needs a real listener, not a sparse cron. Trades titles the
  1000-row cap and Spread holds only ~200 samples, so a 5-min+ interval drops
  the tape on any active pair; 15-60 s is the working range. That is exactly
  the `kraken-market-data` model: a systemd `Type=oneshot` + `OnCalendar`
  timer every 15-30 s (or `Type=simple` long-runner), `DynamicUser`,
  `StateDirectory`, journald structured logs. Cron-less, keyless, immutable
  store root.

### Option (iii) — cross-exchange via `ccxt` (considered, rejected as producer)

- `ccxt`'s `fetch_trades`/`fetch_order_book`/`fetch_l2_order_book` exist and
  would permit cross-checking Kraken tape against Binance/Coinbase. It is **not
  the producer to build** for three reasons, stated plainly:
  1. The seam is Kraken-spot-aligned (`_SIGNAL_COLUMNS`, OHLCV, funding all
     Kraken-native); a second venue adds pair-normalisation and venue-consistency
     questions to a gap whose target is a per-(ticker, hour) scalar on Kraken
     pairs.
  2. `since`/`limit` semantics differ per exchange in `ccxt`; pagination is
     less uniform than the sibling's own `(rows, last)` contract.
  3. It is a 3-file dependency (`pyproject.toml` + both `nix/default.nix` +
     `flake.nix`/`flake.lock` — prior RESEARCH-3 G3 measured this at 3 files)
     to buy a validation tool, not a feed.
  Note it as a **manual cross-check script** at most, never a scheduled
  producer.

---

## 4. Recommended shape: a **sibling** recorder (option ii), with rationale

**Recommendation:** a new keyless sibling recorder (project name e.g.
`kraken-microstructure`, or folded into `kraken-market-data` as a second
poller mode) that polls `recent_trades` + `spread` on a 15-60 s loop, persists
both `last` cursors in a `_meta.json` sidecar per pair, and flushes a
per-(ticker, hour) JSONL record in the exact `merge_extra_features` shape
(floored `timestamp`, `ticker` in `ETH/USD` spelling, float scalars). This is
the "recorder with its own cadence" AUDIT Rank 3 names as the missing piece.

**Why sibling over in-repo:**
1. **The tape exists only if someone listened.** Rank 3's data is historical
   by construction: an hour's taker imbalance / VWAP pressure / realised
   spread cannot be reconstructed from later polls (Trades cap 1000, Spread
   holds ~200). In-repo (option i) records books **only while the engine is
   up** — which is exactly the "data gap whenever the bot is down" failure the
   AUDIT calls out — and produces **no tape scalars at all**.
2. **Blast radius.** The engine is the live-trading path. Adding hour buckets,
   cursor persistence, raw-file appends and catch-up to it widens the surface
   that can take the live loop down. Acquisition belongs in a process whose
   failure loses data, not capacity to trade.
3. **The family already owns the scaffolding.** `kraken-market-data` is
   precisely a since-cursor poller with `_meta.json` + a thin NixOS module
   (`nix/module.nix`: `Type=oneshot`, `DynamicUser`, `StateDirectory`,
   `OnCalendar`, keyless by design, journald). The recorder is the same shape
   on two more public endpoints, so `flake.nix`, `pyproject.toml`,
   `nix/module.nix`, dev-shell and the offline test suite are copy-verified
   patterns — the difficulty is the poller/aggregation logic, not infra (see
   §6).
4. **Collision-free column ownership.** Rank 2 (activate the dormant
   microstructure group) takes `spread` from the funding JSONL's `bid`/`ask`
   (futures-based, zero new calls). A sibling recorder emitting its own
   best-bid/ask width under the same column name `spread` would double-define
   that column across files — `merge_extra_features` applies files in sequence
   onto one frame (`data.py:779-786`), so last-write-wins per floored hour is
   undocumented and easy to get wrong. The sibling therefore names its own
   width `realized_spread_bps` (distinct, unambiguous), and `spread` stays
   single-writer (funding). In-repo book reuse does not have this problem only
   because the engine is not a separate file — but it is a live-path file, per
   (2).

**In-repo (option i) remains the correct *fallback* if the team wants
`order_book_imbalance` with zero new processes:** the engine already owns the
call; the three emitted names (`spread`, `bid_vol`, `ask_vol`) light up both
microstructure branches as-is. It is strictly inferior on tape coverage but is
~30 lines and no infra. The AUDIT's Rank-1-item-2 overlap note stands: the
book sizes come from a call the engine already makes; the tape is the new
acquisition surface.

**Interplay to note for the architect:** Rank 2 and Rank 3 are complementary,
not competing — Rank 2 is the free `spread`, Rank 3 is the data-source build.
If Rank 2 ships first, the recorder must not claim the `spread` name (§4.4).

---

## 5. Feed-directness score: **4/5**

- The aggregated outputs are named floats fitting the `_SIGNAL_COLUMNS` shape
  (`features.py:42-54`); the merge seam (`merge_extra_features`, `data.py:281-497`)
  is the one door, already exercised by three siblings. Per-hour rows, hour-floored
  `timestamp`, `ticker` in `ETH/USD`, float values — identical to funding/news/social.
- **Costs to the 5:** (a) each shipped scalar is a new `_SIGNAL_COLUMNS` entry
  (one line each; the width guard already protects consumers); (b) a new
  `*_features_file` config key joined into the tuple at `data.py:779-786`;
  (c) an **asset→pair mapping** that already exists — `kraken-funding-rates`
  `models.py:96` maps `"ETH:USD"` → `"ETH/USD"` via
  `pair_raw.replace(":", "/")`; the recorder keys its JSONL by the bot's own
  `"ETH/USD"` spelling so **no mapping work is needed** (the pair is the input
  to the poller, not a discovery problem);
  (d) unlike Rank 1/2 this is a **data-acquisition surface**: the seam exists,
  the producer does not. Minus 1 for that, per the AUDIT's own framing.
- Degree of fit to live observation: the tape-derived scalars are 1-hour
  aggregates exactly in the `signal_max_age_hours=1` horizon; they are not
  sub-hourly snapshots that would go stale inside the bar. `vwap_pressure`
  is window-native by construction.

---

## 6. Difficulty score: **3/5**

Matches the AUDIT's estimate; the prior pass's "needs a recorder with its own
cadence" is the whole of the 3.

- **Infra is cheap**: copy the `kraken-market-data` flake/module pattern
  (verified sibling, §4.3). No new dependency beyond what the sibling already
  pins (`kraken-python`).
- **Real work is the poller:** (1) since-cursor persistence in a `_meta.json`
  sidecar, crash-safe resume — including the trades cursor being an **opaque
  string** (Section 1d), so restart == read `last` and go; (2) hour bucketing
  + flush-on-hour-roll with raw-file append so a crash mid-hour loses minutes,
  not the hour; (3) an idle-gap policy (what the recorder does on downtime — it
  just resumes from `last`; completeness degrades gracefully on trades, more
  so on spread's rolling window).
- **Bot-side**, each new column is an allow-list line; the new config key rides
  an existing tuple; nothing in `features.py`/`data.py` changes structurally
  except optional `_SIGNAL_COLUMNS` rows. Retrain is implied whenever the
  observation widens — same burden as Rank 2.
- **Not counted as difficulty:** rate limits (50-150× headroom), keyless
  access, output format (a named-float JSONL row), asset→pair mapping (none
  needed).
- The **one genuinely novel risk** is tape completeness after downtime (the
  1000-row / ~200-sample ceilings are the reason the cadence can't be a sparse
  cron). Mitigation is the cursor sidecar + 15-60 s timer — the same
  mechanism the family already runs.

---

## Sources

- `kraken-python/kraken_api/manager.py:187-204` (`order_book`, `recent_trades`,
  `spread`), `models.py:189-211` (`BookLevel`), `359-410` (`Trade`),
  `552-585` (`SpreadPoint`), `client.py:116-136` (`depth`/`trades`/`spread`),
  `transport.py:45-48,108,208-219,249-250` (rate markers, no retry),
  `auth.py:58-90,104` (min_interval env-only footgun), `.env.example` (rate
  guidance), `README.md`.
- `kraken_trading_bot/rl/data.py:691-703` (`_page_candles` cursor),
  `779-786` (3-file merge loop), `281-497` (`merge_extra_features`),
  `rl/features.py:42-54` (`_SIGNAL_COLUMNS`), `405-418` (microstructure
  group incl. `order_book_imbalance`), `engine.py:59-74,161-168` (60 s tick,
  discarded `order_book(count=10)`), `strategies/base.py:92`.
- `kraken-funding-rates/kraken_funding_rates/models.py:33-48,96,113-114`
  (live `bid`/`ask` floats, `"ETH:USD"`→`"ETH/USD"`), `export.py:120-152`
  (hour-floored JSONL write, `append`), `kraken-market-data/nix/module.nix`,
  `market_data/store.py` (`_meta.json` cursor), `README.md`.
- Live Kraken API reference: `https://docs.kraken.com/api/docs/rest-api/get-recent-trades`
  (row shape, `last` = "ID to be used as since", 1000-row default),
  `.../get-recent-spreads` (row shape, ~200 samples, rolling window),
  `.../get-order-book` (Depth `count` ≤ 500), and `https://docs.kraken.com/llms.txt`
  ("Spot REST has a call counter (max 15-20 depending on tier)").
- Prior research: `.data-audit/RESEARCH.md`, `.data-audit/RESEARCH-1/2/3.md`
  (git `18343ce` and prior), `DECISION.md` §6.7 (prior rejection of the
  depth-recorder on cost, reframed by the "machinery already running" fact),
  `PLAN.md` §6 (NEW-DATA-SOURCE candidates ranked low pending this survey).

RESEARCH COMPLETE