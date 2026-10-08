# RESEARCH-1.md — G-1: wiring the recorded order-book depth into the RL observation

**Phase-2 research, READ-ONLY. Pass dated 2026-10-08.**
Gap: **G-1** from `AUDIT.md` §G-1 (market-microstructure / IMPROVE-EXISTING).
Repo: `/home/seanc/Projects/kraken-trading-bot`, read-only. Nothing modified, nothing committed.
Sibling read: `kraken-python` (`BookLevel`/`OrderBook`), PyPI metadata for candidate libraries.

Evidence tiers, per `AUDIT.md` §0:
**[M]** measured (command run, result quoted) · **[C]** cited (read off a file at the line given) · **[I]** inferred · **[U]** unverified.

---

## 0. Headline: the gap is real and the consumer tail is cheap — but the seam as written **cannot read this file**

The premise "only the merge seam and one `_SIGNAL_COLUMNS` entry are missing"
(`AUDIT.md:165-166`) is **directionally right and understates the seam work by a
record-shape conversion**. Measured against the live artifact and the seam code,
three shape mismatches mean a naive `_SIGNAL_CHANNELS` entry would **silently
return the frame untouched** — the worst failure mode, because the width guard
would still pass and the column would just be 0.

| # | Seam expects (`data.py:633`) | Depth record actually carries (`signals/eth_usd_orderbook.jsonl`) | Consequence |
|---|---|---|---|
| **C1** | a **`timestamp`** column (`data.py:778-780`) | `recorded_at` + `hour`; **no `timestamp` key at all** **[M]** | `merge_extra_features` logs *"No 'timestamp' column … skipping merge"* and `return df` — the depth channel is dropped with a WARNING, not an error. |
| **C2** | ticker field `ticker` (`data.py:160`, `_filter_ticker` 894-934) | `pair: "ETH/USD"`; **no `ticker` key** **[M]** | `_filter_ticker` treats it as a *one-ticker file* and merges every record at WARNING. Works for a single pair, but the per-ticker filter is silently off — a second pair's file would cross-contaminate. |
| **C3** | flat per-hour scalar columns, intersected against `_SIGNAL_COLUMNS` (`data.py:814-827`) | nested `bids`/`asks` arrays of `[price, volume, ts]` **[M]** | `pd.DataFrame(records)` yields **object cells holding lists**; the allow-list intersection drops them. The reduction to scalars must happen **in the reader**, before the join. |

Plus the already-known **C4 collision**: the depth record's own `spread`
(`book_to_record` `depth_recorder.py:867`) collides with the funding-derived
`spread` (`features.py:1008-1017`); `depth_recorder.py:146` and `features.py:145`
both reserve `realized_spread_bps` as the non-colliding name. Since the depth
channel would merge **last** (append to `_SIGNAL_CHANNELS`), its `spread` would
overwrite funding's `spread` in `groupby(level=0).last()` (`data.py:809`) and the
microstructure column would silently flip source. **The flattener must rename it.**

So the real G-1 work is:

1. a **pure, ~30-line flatten function** that turns one nested depth record into
   flat `bid_vol`/`ask_vol` (+ optional `realized_spread_bps`, `microprice_dev`);
2. **`bid_vol`/`ask_vol`** added to `_SIGNAL_COLUMNS` **and**
   `_SIGNAL_BUILDER_INPUT_COLUMNS` (so only the derived `order_book_imbalance`
   reaches the observation — the same pattern `bid`/`ask`→`spread` already uses);
3. **one new config key** + threading through `_SIGNAL_CHANNELS` and its 6 call
   sites;
4. **no new dependency** — the reduction is arithmetic on a list of strings.

Net observation width: **+1** (`order_book_imbalance`), same accounting style as
the funding bid/ask pair (`features.py:134-149`).

> **Width risk.** `microstructure` ships **enabled** (`configs/default.yaml:53`),
> so `order_book_imbalance` is a **new observation column**. Any existing trained
> model (fixed input width) is invalidated by this change — the same caveat
> `AUDIT.md:212` records for G-4. This is a *retrain-required* change, not a
> drop-in.

---

## 1. The reduction: what scalar to compute from a nested snapshot

The coded consumer already fixes the *formula*: `features.py:1018-1026` computes

```
order_book_imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)          # OBI
```

gated on `{"bid_vol","ask_vol"}.issubset(df.columns)`. So research question #1 is
not "what formula" but **"what should `bid_vol`/`ask_vol` be, and what else is
cheap to emit in the same pass"**.

### 1.1 Standard formulas (so the choice is defensible)

| Feature | Formula | Needs | Notes |
|---|---|---|---|
| **Static OBI (top-of-book)** | `(V_bid0 − V_ask0)/(V_bid0 + V_ask0)` ∈ [−1,1] | level 0 only | The formula already coded. Cheapest, noisier. |
| **Static OBI (top-N)** | `(Σᵢ≤N V_bidᵢ − Σᵢ≤N V_askᵢ)/(Σᵢ≤N V_bidᵢ + Σᵢ≤N V_askᵢ)` | N levels | Robust to one spoofed level; still one scalar. **Recommended default N≈10** (Kraken serves 100). |
| **Notional (dollar) imbalance** | same with `Σ priceᵢ·volᵢ` | N levels | Scales across pairs; but the repo's OHLCV is ETH-denominated so base-volume is fine. |
| **Exponentially-decayed depth imbalance** | weight level *i* by `exp(−λi)` (or `1/(i+1)`) before summing | N levels | Front-loads book pressure; one extra knob. |
| **Microprice / weighted mid (Stoikov 2018)** | `P_micro = (P_ask·V_bid + P_bid·V_ask)/(V_bid + V_ask)` | level 0 | `microprice_dev = P_micro − mid`, a small signed number; a stronger short-horizon predictor than OBI in the literature. |
| **Order Flow Imbalance (Cont–Kukanov–Stoikov 2014)** | event-based change in best bid/ask size between consecutive snapshots | **two consecutive snapshots** | Best predictor, but needs the *previous* record at read time and is not a pure per-row function. Natural **second pass**, not v1. |
| **Realized spread (bps)** | `(best_ask − best_bid)/mid · 1e4` | level 0 | The **reserved name** `realized_spread_bps`; lets the book spread into the observation without colliding with funding's `spread`. |

### 1.2 What the recorded snapshot actually gives (measured, one record)

```
bids[i] = ["2683.52000", "1.030", 1791054645]   # [price, volume, order_ts]  (strings)
asks[i] = ["2683.53000", "0.616", 1791054645]
depth   = {requested_count:100, bid_levels:100, ask_levels:100, truncated:false}
best_bid=2683.52  best_ask=2683.53  mid=2683.525  spread="0.01"           [M]
```

Volumes are **strings** and prices are **strings** — the flattener must
`float()` them (and `BookLevel.volume_decimal` exists in `kraken-python` if a
Decimal path is preferred, but the seam already reads raw JSON, so plain
`float()` matches the rest of the seam). `truncated` is `False` on all 103
records **[M]**, so depth is comparable across the file today; a future
`count=1000` run would set it `True` (Kraken silently serves 100 —
`depth_recorder.py:81-91`) and the flattener should record it (see §3.7).

### 1.3 Recommendation for v1

Emit **`bid_vol` = Σ top-10 bid volumes, `ask_vol` = Σ top-10 ask volumes**
(plus optionally `realized_spread_bps` and `microprice_dev` in the same pass).
Top-10 is one line more than top-of-book, is robust to a single level, and is
comparable to the top-of-book figure the funding channel would have given. Emit
top-of-book **and** top-N only if a later ablation wants to compare them; one
scalar keeps the width at +1.

---

## 2. Library survey — does any library reduce this more cheaply than ~30 lines?

Scored on **how cheaply it reduces to a per-(ticker, timestamp) scalar the RL
pipeline can consume** (5 = drop-in scalar, 1 = wrong tool / adds a framework),
then maintenance/license/auth/rate-limit/output-shape.

| Candidate | Reduce-to-scalar | Maintenance (last release) | License | Auth | Rate limit | Output shape | Verdict |
|---|---|---|---|---|---|---|---|
| **Pure `pandas`/`numpy` reducer (in-repo)** | **5** | n/a (repo code) | repo's own | none | none | float column | **Winner** |
| `hftbacktest` | 3 | active (2.4.4, 2025-12) **[M]** | MIT **[M]** | none | none (local) | own L2/L3 format, numba | overkill |
| `mlfinlab` (Hudson & Thames) | 3 | **404 on PyPI [M]**; now commercial/keyed **[U]** | commercial | license key | n/a | bar-level microstructure | rejected |
| `orderbook` (PyPI) | 2 | 0.1.2, **2013-12** **[M]** | MIT-ish | none | none | full matching engine | wrong tool |
| `lob` (PyPI) | 2 | 4.5.4, 2022-02 **[M]** | MIT **[M]** | none | none | C++ LOB bindings | wrong tool |
| `microprice` (PyPI) | 2 | 0.1.1, 2024-07 **[M]** | MIT **[M]** | none | none | **JAX** micro-price | adds JAX for one scalar |
| `tardis-client` | 1 | 1.4.2, 2026-03 **[M]** | MPL-2.0 **[M]** | **API key, paid** | tiered | historical replay | data vendor, not features |
| `cryptofeed` | 1 | 3.0.1, 2026-09 **[M]** | (OSI) | none (public WS) | exchange WS limits | live feed handler | feed, not features |
| `polars` | 3 | 2.0.0, 2026-10 **[M]** | MIT **[M]** | none | none | DataFrame | second engine for one list-sum |
| `kraken-python` `BookLevel` | 4 | sibling dep, already present | repo's own | none | none | `.volume_decimal` | optional parser, not needed |

**Reading of the table.** Every third-party candidate either (a) is a **matching
engine / backtester / feed handler**, not a feature extractor (`orderbook`,
`lob`, `hftbacktest`, `cryptofeed`), (b) is a **paid data vendor**
(`tardis-client`), (c) **adds a heavy framework** for one scalar (`microprice` →
JAX; `polars` → a second DataFrame engine), or (d) is **commercially licensed**
(`mlfinlab`). None beats ~30 lines of `sum(float(v) for _, v, _ in levels[:N])`.

**Conclusion: do not add a dependency.** The only library-shaped decision worth
making is *where* the reducer lives (see §3).

---

## 3. The merge-seam pattern: where the flattener goes

### 3.1 The seam's contract, as it actually is

`merge_extra_features` (`data.py:633-891`) is the single seam every exogenous
column travels through. Its pipeline, in order:

```
read JSONL (746-755)
  → pd.DataFrame(records) (777)
  → require a `timestamp` column, else return df (778-780)          ← C1
  → _filter_ticker on `ticker` (785, 894-934)                        ← C2
  → floor index to the hour, dedup, groupby(level=0).last() (804-809)
  → intersect columns with _SIGNAL_COLUMNS (814-827)                 ← C3
  → reindex onto floored OHLCV index, bounded ffill, freshness (829-877)
  → df[col] = filled.fillna(0.0) (864-865)
```

`_SIGNAL_CHANNELS` (`data.py:167-198`) is a tuple of `(config_key, producer_hint)`
and `_signal_channels(*values)` (`data.py:226-241`) **zips it against the
channel values in merge order**, so the channel list and the caller arguments are
kept in lockstep. There are exactly **two iteration sites** —
`fetch_data` (`data.py:1243`) and the store leg (`data.py:1908`) — plus four
external callers that pass the three keys explicitly:
`train.py:251-253`, `backtest.py:564-566`, `export.py:178-180`,
`paper_trade.py:312-314`.

### 3.2 Design A — reader-side flattener (**recommended**)

Add a pure function in `data.py`:

```python
def _flatten_orderbook_records(records: list[dict], *, levels: int = 10) -> list[dict]:
    """Reduce nested bids/asks records to flat per-hour scalars.

    Detected by the presence of `bids`/`asks` (or `source == "kraken.public.Depth"`).
    Emits the columns the seam already understands: `timestamp` (from
    `recorded_at`), `ticker` (from `pair`), `bid_vol`, `ask_vol`, and
    `realized_spread_bps` — NEVER `spread` (reserved; see features.py:145).
    """
```

and call it once, right after `pd.DataFrame(records)` (`data.py:777`), gated on
the depth shape. It fixes **C1/C2/C3/C4 in one place**, leaves the generic seam
unchanged, and keeps `depth_recorder.py`'s append-only schema **frozen** — which
the recorder explicitly requires (`depth_recorder.py:45-55`: *"writes data and
reads none of it back … consuming it in the same slice would create a reason to
change its schema … and the history already written would become incompatible
with itself"*).

**Minimal change set (file:line):**

| File | Change |
|---|---|
| `data.py` (~777) | insert `records = _flatten_orderbook_records(records)` when depth-shaped |
| `data.py` | add `_flatten_orderbook_records` (pure, testable) |
| `data.py:167-198` | append `("orderbook_features_file", "<hint>")` to `_SIGNAL_CHANNELS` |
| `data.py:1170-1172, 1633-1635` | add `orderbook_features_file: str \| None = None` to both signatures |
| `data.py:1243, 1908` | pass the new value into `_signal_channels(...)` |
| `features.py:94-125` | add `"bid_vol"`, `"ask_vol"` to `_SIGNAL_COLUMNS` |
| `features.py:149` | add `"bid_vol"`, `"ask_vol"` to `_SIGNAL_BUILDER_INPUT_COLUMNS` |
| `configs/default.yaml` | new `orderbook_features_file:` key (path + doc block) |
| `train.py/backtest.py/export.py/paper_trade.py` | pass the new key from config |

`order_book_imbalance` itself needs **no change** — `features.py:1018-1026`
already produces it the moment `bid_vol`/`ask_vol` reach the frame. This is the
whole point of G-1: the consumer is done; only the delivery is missing.

### 3.3 Design B — producer-side (recorder writes derived scalars) — **REJECTED**

Add `bid_vol`/`ask_vol` to `book_to_record` (`depth_recorder.py:768-874`).
Rejected because it **rewrites a schema the recorder froze on purpose**: the 103
existing records lack the keys, so the file becomes self-inconsistent (some
records with, some without), and `depth_recorder.py:45-55` names exactly this
failure. The recorder's own docstring is the authority here.

### 3.4 Design C — generic nested-source reducer registry — **defer**

A small `{shape_detector: reducer}` table so future nested sources (trade tape,
G-2) plug in without touching the seam. More elegant, more code and more
surface; **premature for one source**. Revisit when G-2's tape lands.

### 3.5 Collision handling (C4) — the exact rule

- The flattener **never emits `spread`**; it emits **`realized_spread_bps`**
  (reserved at `depth_recorder.py:146`, `features.py:145`). If the book spread is
  wanted in the observation, add `realized_spread_bps` to `_SIGNAL_COLUMNS` as a
  plain signal column (it is not a builder input); if not, emit it anyway and let
  the allow-list drop it — **either way `spread` stays funding's alone.**
- **Channel order:** append the depth channel **last** in `_SIGNAL_CHANNELS`.
  Order matters because `groupby(level=0).last()` (`data.py:809`) and the
  `df[col] =` writes (`data.py:864-865`) are last-writer-wins; depth last means
  funding's `spread` and the depth `bid_vol`/`ask_vol` never contest a name.
- **Width accounting:** `bid_vol`/`ask_vol` are raw base-asset volumes, not
  observation columns — listing them in `_SIGNAL_BUILDER_INPUT_COLUMNS` makes
  `_add_signals_features` skip them (`features.py:1063-1069`) exactly as it skips
  `bid`/`ask`/`spread`, so the net width delta is **+1** (the derived
  `order_book_imbalance`), not +3.

### 3.6 Freshness / absence (already handled by the seam)

The seam zero-fills every value column and records `signal_observed` /
`signal_age_hours` (`data.py:855-877`). So a bar with no depth record gets
`bid_vol = ask_vol = 0.0` → `order_book_imbalance = 0/0 = NaN` →
`ffill().fillna(0.0)` → a **neutral 0.0**, disambiguated by
`signal_observed = 0`. No new absence machinery is needed. **But note the
coverage reality:** the file holds **100 distinct hours** and one hole
(`status.json`: `n_hours_covered 100`, `n_gaps 1`, `n_seeds 2`) **[M]** against a
history of ~8,914 funding records — so on any multi-year training window this
column is 0.0 for **>98% of bars**. G-1 delivers a *live* feature, not a
historical one; that is the same forward-only physics the recorder documents.

### 3.7 Two small hardening items the flattener should carry

- **`depth.truncated`**: if a future run sets `count=1000`, Kraken silently
  serves 100 (`depth_recorder.py:81-91`); the flattened `bid_vol`/`ask_vol` would
  change scale. The flattener should either record `truncated` as a companion
  signal column or refuse to reduce a `truncated:true` record — otherwise a
  depth change is invisible in the observation, which is the same silent-reduction
  trap the recorder was built to expose.
- **Empty sides**: guard `bids`/`asks` being `[]` (return `0.0`, not `IndexError`).

---

## 4. Scoring matrix of approaches

| Approach | Effort | Risk | Width Δ | New dep | Correctness | Score |
|---|---|---|---|---|---|---|
| **A: reader-side flattener, top-10 OBI** | ~1–2 h | low (pure fn, testable) | +1 | none | matches coded formula | **9/10** |
| A′: + `realized_spread_bps` + `microprice_dev` | +1 h | low | +1..+3 | none | adds literature-standard signals | 8/10 |
| A″: + OFI (needs prev snapshot) | +3–4 h | medium (stateful) | +1 | none | strongest predictor | 7/10 |
| B: producer-side scalars | 1 h | **high** (schema rewrite) | +1 | none | violates recorder freeze | 2/10 |
| C: generic reducer registry | 3–4 h | medium | +1 | none | premature | 5/10 |
| Any library (`hftbacktest`/`mlfinlab`/…) | ≥1 day | high | +1 | yes | wrong tool / paid | ≤3/10 |

---

## 5. Top recommendation

**Design A: a reader-side `_flatten_orderbook_records` that emits top-10
`bid_vol`/`ask_vol` (+ `realized_spread_bps`), wired as a fourth
`_SIGNAL_CHANNELS` entry, with `bid_vol`/`ask_vol` added to `_SIGNAL_COLUMNS` and
`_SIGNAL_BUILDER_INPUT_COLUMNS`.** No new dependency. The existing
`order_book_imbalance` (`features.py:1018-1026`) lights up unchanged; the
recorder's frozen schema is respected; the `spread` collision is dissolved by the
reserved `realized_spread_bps` name.

The three things the task brief called "the seam and one list entry" are in fact
**one pure function + one list entry in each of two tuples + one config key +
six call-site threads** — and the single highest-value sentence to carry into
`DECISION.md` is:

> The depth record has **no `timestamp` field** (`recorded_at`/`hour` only) and
> uses **`pair`, not `ticker`**; without a flatten step the seam logs
> *"No 'timestamp' column … skipping merge"* and returns the frame untouched.

**Do not** add `bid_vol`/`ask_vol` to the observation directly (use
`_SIGNAL_BUILDER_INPUT_COLUMNS`), and **do not** let the flattener emit `spread`.

---

## 6. Key file:line index

- `features.py:94-125` `_SIGNAL_COLUMNS` · `:134-149` builder-input rule ·
  `:171-173` `POINT_IN_TIME_EXOGENOUS_COLUMNS` · `:997-1026`
  `_add_microstructure_features` (the OBI formula) · `:1063-1069`
  `_add_signals_features` skip logic.
- `data.py:160-198` `_SIGNAL_CHANNELS` · `:226-241` `_signal_channels` ·
  `:633-891` `merge_extra_features` (timestamp bail `:778-780`, allow-list
  `:814-827`) · `:1243`, `:1908` iteration sites · `:1170-1172`, `:1633-1635`
  signatures.
- `depth_recorder.py:45-55` schema freeze · `:81-91` measured truncation ·
  `:146` reserved name · `:768-874` `book_to_record`.
- `configs/default.yaml:53` microstructure group on · `:89/124/144` channel keys.
- Callers: `train.py:251-253`, `backtest.py:564-566`, `export.py:178-180`,
  `paper_trade.py:312-314`.
- Artifact: `signals/eth_usd_orderbook.jsonl` (103 records, 100 hours, 1 gap, 2
  seeded) + `.status.json`; tests `tests/test_depth_recorder.py`,
  `tests/test_rl_environment.py:259-262`, `tests/test_feature_nonfinite_guards.py:297-313,622-645`.

RESEARCH COMPLETE
