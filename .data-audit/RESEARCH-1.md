# RESEARCH-1 — AUDIT.md Rank 1: "produced-but-unused data" (four dead data paths)

Researcher: researcher-1, team `audit-pipeline`. Pass of 2026-10-01. This file
**overwrites** the prior-pass RESEARCH-1.md (which covered the pass-2 G1
window-recording gap; that research is preserved in git). This pass's brief is
AUDIT.md Rank 1 (§5) / §4.1 — the four datasets already on disk or already
fetched that are discarded at the last step. Outcome type: **IMPROVE-EXISTING**.
Placement candidates: this repo, and possibly the funding sibling's emit side.

**Read-only pass.** No code changed, nothing committed. All claims verified by
grep/read on the working tree (commit `ca360a8` plus sibling working trees at
`~/Projects/kraken-{python,funding-rates,market-data}`). Where the audit's
group counts differ from measured reality, the measured number is quoted and
flagged (see §1.1).

---
## 0. The four items and one headline number

| # | Dataset | Produced at | Last consumer | What it should become | Width |
|---|---|---|---|---|---|
| 1 | `vwap`, `count` | `data.py:649,651` (per candle) | store persist + export, **no feature read** | `vwap_dev`, `trade_count_zscore_20`, `volume_per_trade` | +3 |
| 2 | `order_book` sizes | `engine.py:72` (every 60 s) | `sma.py` never reads it | `order_book_imbalance` (needs `bid_vol`/`ask_vol` producer) | +1 |
| 3 | funding `bid`/`ask` | `kraken-funding-rates/models.py:63,64` | dropped at `data.py:420-424` allow-list | `spread` (builder exists, `features.py:410-413`) | +1 (clean) or +3 (naive) |
| 4 | `funding_rate_prediction`, `vol24h` | `kraken-funding-rates/models.py:58,65` | dropped at the same allow-list | pass-through signal columns | +2 |

**Measured base width today: 49** (verified by running the pipeline:
price=10, technical=33, volume=6, microstructure=0, signals=0 — with the
config `feature_groups` = all five and no signal files configured). The
audit's prose rounds groups to 11/30/5; the **sum is 49 either way**. All
seven additions land → **49 → 56**, matching the audit's claim.

The universal wiring rule from the audit (§3) holds here too: every candidate
that produces `K` named per-ticker-per-hour floats costs `K` names on
`_SIGNAL_COLUMNS` (plus, for item 2 and 3, one recorder or one allow-list
extension). No new repo, no new dependency, no new Kraken endpoint for items
1, 3, 4.

---
## 1. Item 1 — `vwap` / `count`: parsed, stored, exported, never observed

### Verified producer
- `candles_to_dataframe` parses `vwap` (`data.py:649`: `float(c.vwap) if c.vwap
  else float("nan")`) and `count` (`data.py:651`: `int(c.count)`) on **every
  candle**. `_OHLCV_COLUMNS = ("time","open","high","low","close","vwap",
  "volume","count")` (`data.py:58`) carries both end-to-end.
- The sibling store persists both: `kraken-market-data/store.py:58,92,94`
  (the store's own `_OHLCV_COLUMNS`, parquet write of `vwap`/`count`) and
  `kraken-market-data/market_data/export.py:52,54` (JSON export of both).
- The bot's export (`rl/export.py:62`, docstring `:16-17`) forwards the raw
  OHLCV columns untouched.

### Verified no consumer
- `grep -n "vwap" kraken_trading_bot/rl/` → hits only in `data.py` (parse +
  docstrings) and `export.py` docstring. **Zero hits in `features.py`,
  `environment.py`, `backtest.py`, `paper_trade.py`, `train.py`.**
- `grep -n "\bcount\b" features.py environment.py` → only
  `features.py:444` (a docstring word "count"). No column read anywhere.
- `FeaturePipeline._add_volume_features` (`features.py:393-403`) builds the
  whole `volume` group from `close` + `volume` alone; `vwap` and `count` are
  never inputs to a builder.

### Wiring anchor
Two viable seams, one of which already exists:

1. **Derive at the seam, pass through the `signals` group.** Add the three
   names to `_SIGNAL_COLUMNS` (`features.py:42-54`) and compute them where the
   frame is assembled — either in `candles_to_dataframe` (`data.py:623`) or
   in `fetch_ohlc_dataframe`/`read_ohlc_dataframe` post-merge
   (`data.py:775-787` / `:929-940`). `_add_signals_features`
   (`features.py:434-436`) copies any `_SIGNAL_COLUMNS` member present on the
   frame, so the three land in the observation with **zero new plumbing**.
   This is the `49→56` accounting's assumption: the three are code-added on
   the OHLCV path and carry no dimension cost beyond the names.
2. **Add a real builder in `FeaturePipeline`** — e.g. extend
   `_add_volume_features(out, close, volume)` to take `vwap` + `count` and
   emit the three there. More "correct" (they are OHLCV-derived, not signal
   columns) but touches `_OHLCV_COLUMNS` validation at `features.py:292`
   (the required-column check) — **no**, do **not** add `vwap`/`count` to the
   required list: a frame from a source without them must still compute. A
   builder that reads them `if present` (gated exactly like the
   microstructure builder at `features.py:408-418`) is the safe form.

**Recommended shape: seam-derive + pass-through (option 1).** Rationale:
identical width for a fraction of the diff, and it reuses the one documented
door (`_SIGNAL_COLUMNS`). Formulae, all vectorized, all already computable
from the frame the pipeline already holds:

```python
df["vwap_dev"] = df["close"] / df["vwap"] - 1.0          # close-vs-VWAP deviation
df["trade_count_zscore_20"] = (df["count"] - df["count"].rolling(20).mean()) \
                              / df["count"].rolling(20).std(ddof=0).replace(0, 1.0)
df["volume_per_trade"] = df["volume"] / df["count"].replace(0, np.nan)
```
Guard the `replace(0, np.nan)` path exactly as the volume z-score at
`features.py:397-399` does; the observation then ffill/zero-fills the NaN
warmup rows identically to every other feature.

### Standard-feature note (why these are textbook intraday features)
- **`close/vwap - 1`** — signed deviation of the bar close from the
  volume-weighted average price. Positive = price finished above the
  volume-weighted mean (buy-pressure), negative below (sell-pressure).
  Canonical in the market-microstructure/price-impact literature (Bouchaud,
  Farmer & Lillo, *How markets slowly digest changes in supply and demand*,
  2008) and the standard "VWAP dev" feature in crypto feature libraries
  (e.g. spent volume relative-to-VWAP classifications on Binance/Kraken data).
- **trade-count z-score over 20 bars** — a surprise measure of *activity*:
  same-price moves with unusually many executions are participation events;
  volume alone mixes one huge print with a thousand tiny prints. The volume
  group (`features.py:393-403`) already models `volume` but never `count`,
  which is Kraken's trade-execution count for the bar.
- **`volume_per_trade`** — average fills per execution; a persistent
  *composition* feature (institutional-size vs retail-size prints). It is
  exactly the bar-scale analogue of the tick-level "mean trade size" the
  audit's Rank 3 wants from `recent_trades`, obtainable for free today from
  the candle's own `volume`/`count`.

### Width guard consequence (task section 2 requirement)
- Yes — the three (plus the 4 others) **change the fitted width**: a retrain
  writes a 56-wide `normalization.npz` (`features.py:80-99`:
  `feature_names`/`means`/`stds` arrays) where the six on-disk models are
  49-wide.
- `paper_trade.py:327-340` guard is **tautological for this threat**
  (RESEARCH-2's finding, re-verified this pass): `expected =
  env.observation_space.shape[0]` and `actual = obs.shape[0]` both derive
  from the *same* loaded `NormalizationStats`, and
  `NormalizationStats.normalize` (`features.py:138-144`) **drops any frame
  column not in `feature_names`** — so a stale 49-wide `.npz` against a now
  56-wide `compute()` normalizes back down to 49 and the guard sees
  `49 == 49`. **Result: the six existing models load and run, silently
  ignoring the 7 new columns.** Safe, but invisible — exactly the failure the
  audit's Rank 4 width-guard item describes, and the reason widening should
  ship together with the `n_features` provenance fix (`registry.py:60-76`
  omits it), not before.
- `backtest.py` has **no guard at all** (grep for
  `observation_space`/`shape[0]`/`_validate_observation` returns nothing).
  A genuinely mismatched pair (56-wide model + 49-wide pipeline, or vice
  versa) surfaces **late, at `agent.predict`** (`backtest.py:161`, `paper_trade
  .py:367`) as an SB3 shape error, not at a Dutch-door. With the _dropping_
  `normalize`, both directions silently shrink to the stale width instead of
  crashing — so today the failure mode is "missing columns with a working
  policy," tomorrow (once a guard exists) it is a crisp assert.
- **Practical rule for the integrator:** widen the pipeline (`_SIGNAL_COLUMNS`
  + the four ink spots in §3) and **retrain**, or the new columns exist only
  in the code and never in a normalization artifact. The six existing models
  are 49-wide and stay usable.

### Difficulty: 1/5 (easiest item). In-repo, no new calls, ~12 lines + 3
names on `_SIGNAL_COLUMNS`, mirrors existing vectorized patterns.

---
## 2. Item 2 — `order_book` fetched every 60 s, discarded by every strategy

### Verified producer
`TradingEngine.fetch_market_data` (`engine.py:70-74`) calls
`manager.order_book(pair, count=10)` every `interval` (default 60 s), stores
it as `data["order_book"]`, and `run_iteration` (`engine.py:132-136`) passes
the dict to `strategy.tick(market_data)`.

`kraken_api.models.OrderBook` (`kraken-python/models.py:214-251`) carries
`asks: list[BookLevel]`, `bids: list[BookLevel]`; `BookLevel`
(`models.py:189-210`) has `price`/`volume` (strings) — **volume is the level
size**; there is no `bid_vol`/`ask_vol` field name, they are derived by
summing the top-N `BookLevel.volume`s. `manager.order_book` → `client.depth`
(`manager.py:185-190`, `client.py:116-122`) → REST `Depth` (public).

### Verified no consumer
- `grep -rn "order_book" kraken_trading_bot/strategies/` → only a docstring
  in `base.py:92`. `sma.py` reads `candles` (`:68`) and `ticker` (`:69`); the
  only two `ticker` fields used are `ask`/`bid` for limit pricing
  (`sma.py:140,154`). `order_book` never reaches a `Signal`.
- `grep -rn "order_book\|bid_vol\|ask_vol" rl/backtest.py rl/export.py
  rl/environment.py` → zero. The RL pipeline has no producer and no reader.
- In `features.py`, `bid_vol`/`ask_vol` appear **only** inside
  `_add_microstructure_features` (`features.py:414-418`); grep confirms no
  producer anywhere in this repo or any sibling. `order_book_imbalance` is
  the sole feature in the codebase with no producer (audit §5 Rank 3 agrees).

### Wiring anchor
`features.py:414-418` needs exactly `{"bid_vol","ask_vol"}` on the frame
(both float columns; denom = `bid_vol+ask_vol`, replace-zero→NaN, output
`(bid_vol-ask_vol)/denom`, `[-1,1]`). The column names come into the frame
through the same merge seam as everything else, so the *consequence* of item
2 is "get `bid_vol`/`ask_vol` into the JSONL/CSV the seam reads, then add
them to `_SIGNAL_COLUMNS`." They would then also surface in the `signals`
group as raw pass-through columns (+2) **unless** they are excluded like the
freshness pair (`_add_signals_features` copies every `_SIGNAL_COLUMNS`
member; to keep width at +1, either compute `order_book_imbalance` at the
seam and whitelist only that, or add the pair to `_SIGNAL_FRESHNESS_COLUMNS`-
like exclusion — the cleaner read is: whitelist `bid_vol`/`ask_vol`, accept
+3 columns, OR derive imbalance at the recorder and whitelist one name; see
§5 accounting).

### The three concrete options for the `bid_vol`/`ask_vol` producer

| Option | Where | Cadence | New code | Rate-limit friendliness | Verdict |
|---|---|---|---|---|---|
| **(i) recorder in `engine.py`** | append an aggregator into `TradingEngine.run_iteration` | write one hourly JSONL record per pair (aggregate the 60 s snapshots) | ~25 lines + one config key | **Excellent.** Reuses the *existing* `Depth` call — zero added REST volume. 1 book call / 60 s / pair; Kraken public `Depth` is subject to the ~1-per-500 ms per-IP public guidance, so even 10 pairs at 60 s is 600× under | **Cheapest and recommended.** The engine already pays for the call; the only cost is the recorder exists only while the engine runs (acceptable — the engine is the live loop that exists to run) |
| **(ii) standalone recorder in a sibling** | new small writer, e.g. in `kraken-market-data` or a new microproject | same hourly JSONL, but its own schedule | new repo/script + a scheduler | Same as (i) per call, but it is a **new data-acquisition surface** and there is **no scheduler anywhere** (audit Rank 2 item 5: `nix/module.nix` has one EnvironmentFile service and zero timers) | Reject for the minimal set. Correct eventual home if the engine is not meant to keep running, but it triples the operational footprint for the same bytes |
| **(iii) at training time via `manager.depth`/`order_book`** | snapshot the live book once during `read_ohlc_dataframe` | point-in-time at each load | one call at `data.py` seam | Fine (one call) | **Semantically wrong for history.** Kraken `Depth` is a live snapshot with **no `since` cursor and no pagination** — you cannot reconstruct the book for a 6-week training window. Only defensible for live paper rollouts or forward-filling one snapshot across the frame, which would lie about the past. Reject as the primary producer; note for a future live-only feature |

The audit's Rank 1/3 overlap note stands: item 2 is the *same work* as Rank 3's
book-recorder; the difference is that Rank 1 item 2 only needs the **sizes from
the call the engine already makes**, while Rank 3 wants a new tape/spread
series acquisition surface.

### Suggested implementation shape (option i)
In `engine.py` add an hourly-window aggregator per pair: on each
`run_iteration`, after `fetch_market_data`, fold
`sum(level.volume_decimal for level in book.bids[:10])` →
`bid_vol`, same for `asks`; keep `bid` = `book.best_bid()`, `ask` =
`book.best_ask()`; on a bar-hour boundary write
`{"ticker": pair, "timestamp": <floored hour>, "bid_vol": ..., "ask_vol": ...}`
to a JSONL the seam can read (either a new
`order_book_features_file` config key on the same seam, or folded into an
existing signal file). Engine swallows fetch exceptions per-call
(`engine.py:60,67,73`) — the recorder must tolerate a sparse file (it will,
because `merge_extra_features` bounded-ffills + `signal_observed` already
handle gaps). A fresh depth feed with a NaN bid_vol must not poison the
z-score — use the seam's existing zero-fill.

### Difficulty: 2/5. Not "plug a name in" like items 1/3/4 — it needs an
aggregator cursor and a new JSONL, but reuses an existing API call and the
existing seam. Cheap enough that it never justifies option (ii)/(iii).

---
## 3. Item 3 — funding `bid`/`ask` dropped at the seam; `spread` builder ready

### Verified producer
`FundingSnapshot` (`kraken-funding-rates/models.py:41-49`) carries
`bid: float`, `ask: float`; `to_dict()` (`:51-66`) emits **both as top-level
keys** (`"bid"`, `"ask"`). The JSONL writer is `write_jsonl`
(`kraken-funding-rates/export.py:120-156`), which dumps exactly
`to_dict()` with the `timestamp` floored to the hour for the seam. So every
funding JSONL record on disk carries `bid` and `ask`, already floored,
already ticker-tagged.

### Verified no consumer
`merge_extra_features` computes `available_cols` as the intersection of the
record columns with `_SIGNAL_COLUMNS` minus the freshness pair
(`data.py:420-424`). `_SIGNAL_COLUMNS` (`features.py:42-54`) is
`sentiment_score, article_count, novelty_flag, funding_rate, basis,
open_interest, stt_mention_count, stt_tilt, fng_index, signal_age_hours,
signal_observed` — `bid`/`ask` are **not members**, so the merged funding
frame drops them silently before the join. Verified: grep for `"bid"`/`"ask"`
as a whitelisted name in `features.py` returns nothing (the only `bid`/`ask`
mentions are inside `_add_microstructure_features` as *frame-column reads*,
`features.py:410-413`).

### Wiring anchor and what `spread` needs
`features.py:408-413`:
- If the frame carries `"spread"` → pass through (col already on the frame).
- **elif** the frame carries `{"bid","ask"}` → `out["spread"] =
  (ask - bid) / bid.replace(0, np.nan)` (float columns; bid=0 guarded to
  NaN, then zero/ffill-handled by the observation pipeline).

So the funding seam surfaces `bid`/`ask` merely by adding the two names to
`_SIGNAL_COLUMNS`. Then the micro group emits `spread`. **Two accounting
choices, stated plainly:**
- *Clean (+1):* whitelist `bid`/`ask`, and exclude them from the `signals`
  pass-through the way `_SIGNAL_FRESHNESS_COLUMNS` is excluded, or compute
  `spread` at the seam and whitelist only `spread`. Net width 49→50 from this
  item.
- *Naive (+3):* whitelist `bid`/`ask` and let `_add_signals_features` copy
  them verbatim → 2 raw dollar-price signal columns **plus** the 1 micro
  `spread` = +3. Raw `bid`/`ask` at dollar scale z-score fine per-ticker
  (`NormalizationStats` is per-ticker), but they duplicate information the
  price group already prices, and the audit's §4.1 rationale for
  `mark`/`index` ("duplicated information against close") applies equally.
  **Recommend the clean +1.**

The audit's §4.1 table has exactly one nuance to carry forward: `mark_price` /
`index_price` are **not** worth whitelisting — `(mark-index)/index` *is*
`basis`, already merged. `bid`/`ask` are different: there is no spread-like
column anywhere, and `features.py:408` is the only pre-written spread builder.

### Difficulty: 1/5 — two names on one tuple, zero new calls, and the
builder is already test-covered (`tests/test_rl_environment.py:205-215`
exercises exactly `{bid,ask,...} → spread`). This is the prior-pass Candidate
2, whose only historical blocker (the A3 freshness seam) is gone.

---
## 4. Item 4 — `funding_rate_prediction` + `vol24h`: genuinely new numbers, emitted and dropped

### Verified producer (emit side)
`kraken-funding-rates/models.py`:
- `funding_rate_prediction` — `from_api` reads `fundingRatePrediction`
  (`:108`); `to_dict` emits it (`:58`).
- `vol24h` — `from_api` reads `vol24h` (`:115`); `to_dict` emits it (`:65`).
Both flow into every record `write_jsonl` writes (`export.py:145-153`), so
**both are already on disk inside the funding JSONL the seam already reads**
when `funding_features_file` is configured (today `null` everywhere, but the
seam is live and fixture-tested).

### Verified no consumer
Same allow-list drop as item 3: neither name is in `_SIGNAL_COLUMNS`
(`features.py:42-54`), so `data.py:420-424` discards both every merge.

### Feed-directness vs `_SIGNAL_COLUMNS`, and how they differ from what's merged
- Both are **per-ticker-per-hour floats on the exact JSONL shape the seam
  already left-joins** — feed-directness **5/5**. Wiring cost is exactly two
  names on `_SIGNAL_COLUMNS` (plus, for *training*, a config file to point
  `funding_features_file` at — which is a config edit, not code).
- `funding_rate_prediction` is the **forward** (next-settlement estimate)
  funding rate — a *leading* number, distinct from `funding_rate` (the
  settled/last rate, already merged). The two are adjacent in the record but
  different objects: prediction is the exchange's estimate for the funding
  event ~8 h ahead. This is the audit §4.1's "genuinely different number"
  claim, confirmed by the sibling's own documentation
  (`FundingSnapshot` docstring: "Predicted next funding rate").
- `vol24h` is a **different-horizon volume** than bar `volume`: the funding
  source's `vol24h` is the perp contract's rolling 24 h volume (base units)
  at snapshot time, versus the OHLCV bar's 60-min volume already observed in
  the `volume` group (`features.py:393-403`). It is a per-ticker level that
  only changes when the source polls it (hourly), so it is the one genuinely
  *new* quantity in the set — nothing on the current frame approximates a
  trailing 24 h volume.

Caveat worth one line in the implementation note: `vol24h` is a *level*, not a
ratio, and its scale differs by an order of magnitude from `volume` — the
per-ticker z-scoring handles that automatically, but the raw value will move
little between bars (near-constant within a day), so its *fitted* std will be
small. That is fine for a z-scored input; just don't confuse "flat" with
"useless" when reading feature importance.

### Difficulty: 1/5 — two names on one tuple, zero new calls; the only
non-code dependency is a funded `model/config` receiving
`funding_features_file`.

---
## 5. Item 2 and 3 accounting reconciliation (width honesty)

Because the reply must give the architect a number that will not break at the
width guard, the two "microstructure" items bracket the `49→56` claim the
audit makes:

- If **items 2 and 3 whitelist `bid_vol`/`ask_vol`/`bid`/`ask` naively**,
  `_add_signals_features` copies them as raw columns and the delta is
  4 raw + `spread` + `order_book_imbalance` = **49→59**, not 56.
- Keeping the audit's 49→56 requires the *clean* accounting:
  **seam-compute the derived scalars and whitelist only the derived names**:
  `spread` (+1), `order_book_imbalance` (+1) — computed at the funding/engine
  recorder seam respectively — plus the +3 (item 1) and +2 (item 4).

The minimal set below therefore states **49→56 with the clean accounting**,
and flags that the naive whitelist-of-inputs path yields 59 and 3 redundant
columns. Either is coherent; the guard math just must match the chosen one.

---
## 6. Recommended minimal set (49 → 56, no new repo)

Ranked by effort→width payoff (all four items are "difficulty 1–2"; the
ranking below is width-per-keystroke and risk):

1. **Item 1 (vwap/count, +3).** Lowest risk, entirely in-repo, pure
   derivation on a frame already held, no seam change: derive the three in
   `read_ohlc_dataframe`'s return path (or `candles_to_dataframe`), add the
   three names to `_SIGNAL_COLUMNS`. This is the highest ratio of new
   information per line of this set.
2. **Item 4 (funding prediction + vol24h, +2).** Two names on the same tuple,
   and unlike item 1 it reuses a *sibling-emitted* number the seam already
   parses — zero derivation code in this repo.
3. **Item 3 (funding bid/ask → spread, +1 clean).** Two names + the
   pre-written micro builder; test-covered. Batch with item 4 since both are
   the same two-line `_SIGNAL_COLUMNS` edit against the same funding file.
4. **Item 2 (order_book → imbalance, +1).** The only one with a producer
   cost: the engine recorder. Do it last *only if* the engine loop is meant to
   keep running and the 60-s cadence is acceptable — otherwise defer to the
   Rank 3 microstructure candidate, having first decided the engine should
   **stop paying for the Depth call it discards** (`engine.py:72`) or start
   recording it at the same cost.

**MINIMAL SET (recommended): items 1 + 3 + 4 together** — +6 columns
(49→55), all three are pure `_SIGNAL_COLUMNS`/derivation edits, zero new
API calls, zero new repos, and all exercised by the seam that exists. Item 2
(+1, 49→56) is the honest "and if the engine keeps running these 60 s it
already pays for" addition, and the audit's own Rank 1 headline uses 56 — so
the set is: **items 1, 3, 4 for certain; item 2 if the engine run-path is
the deployment the project wants** (it is currently the only thing that
fetches an order book at all).

**Each item must ship with a retrain** (width guard reality, §1.1): the six
on-disk models are all 49-wide; a widened pipeline leaves them functionally
usable but silently column-less until `normalization.npz` and `model.zip`
are regenerated. Recommend adding `n_features` to the registry snapshot
(`registry.py:60-76`) in the same change so `cli.py models` can show which
artifacts predate the widening.

### Verification hooks for whoever implements
- `tests/test_rl_environment.py:205-215` already pins `{bid,ask}/{bid_vol,
  ask_vol} → {spread, order_book_imbalance}`; extend with a
  `{vwap,count,volume} → {vwap_dev, trade_count_zscore_20, volume_per_trade}`
  compute assertion and a `_SIGNAL_COLUMNS`-member round-trip through
  `merge_extra_features` for the four new funding/seam names.
- `tests/test_rl_data_store.py` — assert the store-read path carries the new
  OHLCV-derivation columns through `prepare_episode`.
- Re-run the width probe: `FeaturePipeline(windows=[1,4,24]).compute(frame)
  .shape[1]` must read 56 with all inputs and 49 unchanged when every new
  input is absent (the builders must be presence-gated, not required-column).

## RESEARCH-1 COMPLETE