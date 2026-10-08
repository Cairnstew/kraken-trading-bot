# DECISION.md — Phase 3: pick ONE (gap, library/improvement) target

Team `audit-pipeline-1008`, architect. HEAD `1c63007`. Read-only except this file.
Inputs: `AUDIT.md` (ranked gaps + pipeline map), `RESEARCH.md` (synthesis),
`RESEARCH-{1,2,3}.md` (detail). `RUN-LOG.md` deliberately not read.

**Evidence tiers:** `[M]` measured on this host this pass · `[C]` cited to
`file:line` · `[I]` inferred. Every constant/direction below carries its tier and
citation, per the gate-authoring rules in the brief.

---

## 1. Outcome type — chosen FIRST: `IMPROVE-EXISTING`

The AUDIT's highest-directness item (`AUDIT.md:148-172`, G-1) is an
improvement to code that already exists, and the brief says such an item must be
a candidate outcome. It is: the recorded order-book depth is **fetched but not
consumed**, the consumer is **already coded**, and the only missing piece is
delivery through an existing seam.

Rejected default: "new project". A new sibling would be justified only if the
highest-directness gap needed a new source. It does not — the data is on disk
(`signals/eth_usd_orderbook.jsonl`, 103 records `[M]`) and the feature is coded
(`features.py:1018-1026` `[C]`). Creating a repo here would be motion, not
progress.

**Outcome: `IMPROVE-EXISTING`. No new repo. No new library.**

---

## 2. Chosen target — G-1: wire the recorded order-book depth into the RL observation

**Category:** market microstructure · **Outcome:** IMPROVE-EXISTING ·
**Library:** none · **Effort:** small–medium (~1–2 h) · **Width Δ:** **+1**
(per configuration; see §7) · **Retrain:** required (no models exist — `models/`
holds only `.gitkeep` `[M]`).

### 2.1 Why this one

- `feature_groups` ships with `"microstructure"` **enabled** (`configs/default.yaml:53` `[C]`),
  so the pipeline already reserves width for a microstructure reading it can
  never populate from depth today.
- `order_book_imbalance` is the **only** place `bid_vol`/`ask_vol` is consumed
  and it is gated on `{"bid_vol","ask_vol"}.issubset(df.columns)`
  (`features.py:1018-1026` `[C]`). The consumer is done; only delivery is missing.
- The data is already on disk and already shaped close enough: 103 snapshots
  `[M]`. Nothing new is fetched.
- No specialised library beats ~30 lines of pandas — RESEARCH-1 scored every
  third-party candidate ≤3/10 (matching engine, backtester, feed handler, paid
  vendor, or JAX-for-one-scalar). **Do not add a dependency.**

### 2.2 Landing point (exact)

| # | File / unit | Change |
|---|---|---|
| 1 | `kraken_trading_bot/rl/data.py` ~`777` (inside `merge_extra_features`, right after `pd.DataFrame(records)`) | call a new pure `_flatten_orderbook_records(records)`, gated on the `bids`/`asks` shape |
| 2 | `data.py` (new function) | `_flatten_orderbook_records` — maps `timestamp←recorded_at`, `ticker←pair`, sums top-N level volumes to `bid_vol`/`ask_vol`, emits `realized_spread_bps`, **never** `spread` |
| 3 | `data.py:167-198` `_SIGNAL_CHANNELS` | append a 4th `("orderbook_features_file", "<hint>")` entry (depth merged **last**) |
| 4 | `data.py:1243` and `data.py:1908` (the two iteration sites) | pass the new channel value into `_signal_channels(...)` |
| 5 | `data.py:1170-1172`, `data.py:1633-1635` (`fetch_data` / store-leg signatures) | add `orderbook_features_file: str \| None = None` |
| 6 | `features.py:94-125` `_SIGNAL_COLUMNS` | add `"bid_vol"`, `"ask_vol"` |
| 7 | `features.py:149` `_SIGNAL_BUILDER_INPUT_COLUMNS` | add `"bid_vol"`, `"ask_vol"` (so they reach the frame but **not** the observation) |
| 8 | `configs/default.yaml` | new `orderbook_features_file:` key + doc block |
| 9 | `train.py:251-253`, `backtest.py:564-566`, `export.py:178-180`, `paper_trade.py:312-314` | thread the new config key |

`order_book_imbalance` itself needs **no change** (`features.py:1018-1026` `[C]`).
The feature is read **per ticker** at the `merge_extra_features` seam
(`data.py:633` `[C]`): it filters records to the configured `ticker`, floors to
the hour, and writes the column onto the OHLCV frame that becomes the
observation. The `train`/`backtest` knobs that select it are `feature_groups`
(includes `"microstructure"`, `configs/default.yaml:53`) and the new
`orderbook_features_file` path key.

### 2.3 Artifact layout it feeds

`models/{TICKER_ID}/{model_name}/{model.zip, normalization.npz, config.yaml}`
(`registry.py:1-9` `[C]`). `normalization.npz`'s `feature_names` is the width
authority and `check_feature_width` (`features.py:483` `[C]`) is
non-self-referential, so adding one observation column is caught loudly at load
time rather than silently misaligning a model — which is why this is a
retrain-required change, not a drop-in. `models/` is empty `[M]`, so the retrain
cost is zero today.

---

## 3. Integration sketch (Phase 5 seam)

**Output contract.** The reader-side flattener turns each nested depth record
into exactly one **flat record per `(ticker, timestamp)`** carrying
`timestamp` (from `recorded_at`), `ticker` (from `pair`), `bid_vol` (Σ top-N
level volumes), `ask_vol` (Σ top-N), and `realized_spread_bps`; it never emits
`spread`. After the seam's hour-floor + `groupby(level=0).last()`
(`data.py:804-809` `[C]`), the bot reads one row per floored hour and the
existing `order_book_imbalance` builder (`features.py:1018-1026` `[C]`) turns
`bid_vol`/`ask_vol` into the single new observation column.

**Refresh cadence.** Hourly — the order-book producer fires at `:41`
(`AUDIT.md:120` `[C]`), staggered from funding `:17` / news `:23` / social `:29`.
The file is append-only; a duplicate hour is discarded at the seam
(`data.py:804-809` `[C]`).

**Where the bot reads it.** As the 4th entry of `_SIGNAL_CHANNELS`
(`data.py:167-198` `[C]`), iterated by the fetch leg (`data.py:1243`) and the
store leg (`data.py:1908`), and passed explicitly by the four external callers
(§2.2 row 9). Because the depth channel merges **last**, its columns are written
after funding's; that is safe only because the flattener emits
`realized_spread_bps`, not `spread` (§7 direction D-2).

**Absence.** A bar with no depth record gets `bid_vol = ask_vol = 0.0` from the
seam's zero-fill, so `order_book_imbalance = 0/0 = NaN`, then the environment's
`ffill().fillna(0.0)` policy yields a neutral 0.0 disambiguated by
`signal_observed = 0` (`data.py:855-877` `[C]`). No new absence machinery.

---

## 4. Runner-up options and why they lost

1. **G-2 — trade tape + realized spread (`NEW-DATA-SOURCE`).** Top pick was
   Kraken WS `trade` (live) + Binance aggTrades archive (seed); the wrapper
   already exists uncalled (`RESEARCH-2.md:0`). **Lost because:** it needs a new
   recorder *and* a new seam column, whereas G-1's consumer is already coded; the
   live leg is forward-only and the only deep seed is **Binance-USDT** (cross-venue
   contamination of a USD book); and it does not beat G-1 on directness. It is
   the natural **second** feature once G-1's reader pattern exists (RESEARCH-1
   Design C explicitly defers the generic reducer registry until the tape lands).

2. **G-6 — cross-exchange spot basis (`NEW-DATA-SOURCE`).** Keyless, one column
   `venue_basis_bps`, best implemented by extending the sibling
   `kraken-deep-history` with a `basis` subcommand (`RESEARCH-3.md:184-186`).
   **Lost because:** it is genuinely new sourcing (a producer + a second venue
   leg + a new config key), i.e. strictly more work than G-1 for a signal that is
   *orthogonal* rather than *missing-and-coded*; and it requires the
   "new sibling repo vs extend existing sibling" decision the brief asks me to
   avoid defaulting into when a higher-directness improvement exists. If the team
   wants a second slice, this is the cleanest `NEW-DATA-SOURCE`.

3. **G-3 — `microstructure` group degenerate (`IMPROVE-EXISTING`, `AUDIT.md:188-201`).**
   `bid`/`ask` null on 8,828/8,914 funding records, so `spread` is 0 for ~99% of
   history. **Lost because** it is a *data-quality fix to an existing degenerate
   column*, and the strongest form of it (source a real bid/ask series) reduces to
   G-1's depth feed. Its cheap form (drop `spread`) shrinks width and adds no new
   signal.

4. **G-4 — `stt_tilt`/`fng_index` inert (`AUDIT.md:203-213`).** **Lost because**
   it only *removes* dead columns; it adds no information. It also changes width
   and is worth folding into the same retrain as G-1, not leading it.

5. **G-9 — bar-count vs wall-clock feature windows (`AUDIT.md:251-260`).** High
   data-quality directness, but **large effort** (time-aware windows over a
   reindexed grid) and it is *detected, not fixed* today. Deferred.

---

## 5. Library decision

**None.** RESEARCH-1's survey (`RESEARCH-1.md:116-137`) scores every candidate
≤3/10 on reduce-to-scalar: `orderbook`/`lob` are matching engines, `hftbacktest`
a backtester, `cryptofeed` a feed handler, `tardis-client` a paid vendor,
`microprice` drags in JAX, `mlfinlab` is commercial/404. The reduction is
`sum(float(v) for _, v, _ in levels[:N])`. Adding a dependency would be a net
negative.

---

## 6. What the builder must implement (acceptance, satisfiable by a correct impl)

- `_flatten_orderbook_records(records)` is pure and unit-testable; it detects the
  depth shape by `bids`/`asks` presence, maps `timestamp←recorded_at` and
  `ticker←pair`, emits `bid_vol`/`ask_vol` (top-N sums, default N=10) and
  `realized_spread_bps`, and **never** emits `spread`.
- Guards: empty `bids`/`asks` → `0.0` (no `IndexError`); `depth.truncated == true`
  is recorded or the record is refused (a `count=1000` run silently serves 100 —
  see §7 C-6), so a future depth change cannot silently rescale the feature.
- With the shipped default config plus a non-null `orderbook_features_file`, the
  read yields `order_book_imbalance` **present and non-constant** over the depth
  file's hours, and `signal_observed == 1` on those hours.
- `bid_vol`/`ask_vol` do **not** appear as observation columns (they are
  builder-inputs; `_add_signals_features` skips them, `features.py:1063-1069` `[C]`).
- Width delta vs the **same config with the channel off** is exactly **+1**
  (`order_book_imbalance`), measured per configuration — **not** an absolute width.
- A fixture that exercises the multi-pair path proves the `pair→ticker` rename
  keeps the per-ticker filter on (a second pair's file cannot annotate ETH bars).

---

## 7. Gate-authoring: constants and directions, re-derived with citations

### Measured on the artifact (`signals/eth_usd_orderbook.jsonl`, this pass)

- **C-1 [M]** The depth record has **no `timestamp` key**; it carries
  `recorded_at` (ISO-8601 with µs, e.g. `2026-10-03T19:10:45.752567+00:00`) and
  `hour` (floored, `2026-10-03T19:00:00+00:00`). *Artifact: record 1 key list.*
  → A naive seam entry hits the bail at `data.py:778-780` `[C]` and returns the
  frame untouched with only a WARNING.
- **C-2 [M]** The depth record has **no `ticker` key**; it carries
  `pair: "ETH/USD"`. *Artifact: record 1.* → `_filter_ticker` treats it as a
  one-ticker file (WARNING, merges every record; `data.py:925-934` `[C]`).
- **C-3 [M]** `bids`/`asks` are **lists of `[price_str, vol_str, ts_int]`**
  (e.g. `["2683.52000","1.030",1791054645]`); `depth` and `interval` are **dicts**.
  *Artifact: record 1.* → `pd.DataFrame(records)` gives object cells; the
  allow-list intersection (`data.py:814-827` `[C]`) drops them.
- **C-4 [M]** The depth record carries its own **`spread`** (value `"0.01"`,
  a string; 3 distinct values across the file). *Artifact: record 1 + full scan.*
- **C-5 [M]** 103 records; **100 distinct `hour` values**; 100 distinct
  `recorded_at` floored to the hour; `recorded_at`-floor **equals** `hour` on
  **all 103** (mismatch count 0). → `recorded_at` is a safe source for
  `timestamp`; `hour` would give the identical floored key.
- **C-6 [M]** `depth.truncated == False` on all 103; `depth.requested_count == 100`;
  `depth.bid_levels == depth.ask_levels == 100`. `source == "kraken.public.Depth"`,
  `pair == "ETH/USD"`, `schema_version == 1`. → Kraken serves 100 levels; a future
  `count=1000` request would still be silently capped (RESEARCH-1 §3.7), so the
  flattener must carry/refuse `truncated`.
- **C-7 [M]** Coverage: `n_hours_covered 100`, `hours_in_span 101`, `n_gaps 1`,
  `depth_fraction 0.0114`, span `2026-10-03T19:37Z .. 2026-10-07T23:41Z`
  (`signals/eth_usd_orderbook.jsonl.status.json`). → On a multi-year training
  window the column is **0.0 for >98% of bars**; G-1 is a **live/forward-only**
  feature. This must be stated in any acceptance test.
- **C-8 [M]** Top-10 `order_book_imbalance` over the file is non-constant
  (min −0.915, max +0.871, mean −0.107), so the feature carries real variance on
  the hours it covers — a fixture can assert non-constancy.

### Cited from code

- **C-9 [C]** `_SIGNAL_COLUMNS` (`features.py:94-125`) contains no `bid_vol`,
  `ask_vol`, or `order_book_imbalance`.
- **C-10 [C]** `_SIGNAL_BUILDER_INPUT_COLUMNS = ("bid","ask","spread")`
  (`features.py:149`); members are skipped by `_add_signals_features`
  (`features.py:1063-1069`). Adding `bid_vol`/`ask_vol` here keeps the width
  delta at +1.
- **C-11 [C]** `_add_microstructure_features` computes
  `order_book_imbalance = (bid_vol − ask_vol)/(bid_vol + ask_vol)` gated on
  `{"bid_vol","ask_vol"}.issubset(df.columns)` (`features.py:1018-1026`).
- **C-12 [C]** The seam's hour-floor + `groupby(level=0).last()` is at
  `data.py:804-809`; each channel writes `df[col] = filled[col]…` at
  `data.py:864-865`.
- **C-13 [C]** `_SIGNAL_CHANNELS` order is **news → funding → social**
  (`data.py:167-198`); the two iteration sites are `data.py:1243` (fetch) and
  `data.py:1908` (store); the four explicit callers are `train.py:251-253`,
  `backtest.py:564-566`, `export.py:178-180`, `paper_trade.py:312-314`.
- **C-14 [C]** The reserved non-colliding name is `realized_spread_bps`, defined
  in the `_SIGNAL_BUILDER_INPUT_COLUMNS` comment at **`features.py:145-146`**.
  *(Correction to RESEARCH-1 §3.5, which also cited `depth_recorder.py:146` for
  it; `grep` finds no `realized_spread_bps` in `depth_recorder.py` — the name is
  reserved only in `features.py`.)*
- **C-15 [C]** `depth_recorder.py:45-55` freezes the recorder schema on purpose:
  *"writes data and reads none of it back … no `_SIGNAL_COLUMNS` entry, no
  observation-width change."* → the flattener must live **reader-side**; do not
  rewrite `book_to_record` (`depth_recorder.py:768`).
- **C-16 [C]** `configs/default.yaml:53` enables `"microstructure"` and
  `"signals"`; `models/` holds only `.gitkeep` `[M]` (retrain cost zero).

### Directions (which record/key/frame wins)

- **D-1 [M, C-5]** For `timestamp`, **`recorded_at` is authoritative** (it floors
  to `hour` on all 103 records; `hour` is its floor). Either gives the same
  floored key; `recorded_at` preserves the intra-hour stamp.
- **D-2 [I, from C-12 + C-13]** Depth must merge **last**, and the flattener must
  emit **`realized_spread_bps`, never `spread`**. Because channels are merged by
  separate calls that each write `df[col]` directly, a depth `spread` would
  **overwrite funding's `spread`** (last writer wins) and silently flip the
  microstructure column's source. Emitting the reserved name dissolves the
  collision. *(This is the direction the gate must pin.)*
- **D-3 [C, C-10]** `bid_vol`/`ask_vol` are **builder inputs**, not observation
  columns: the **derived `order_book_imbalance`** wins the width, not the raw
  volumes. Do **not** add them to the observation.
- **D-4 [C, C-11 + C-7]** An absent hour wins as **0.0** (via seam zero-fill →
  `0/0 = NaN` → `ffill().fillna(0.0)`), disambiguated by `signal_observed = 0`.
  Absence is not a neutral reading; the freshness pair is the discriminator.

---

DECISION COMPLETE
