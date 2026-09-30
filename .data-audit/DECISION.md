# DECISION — kraken-trading-bot data-pipeline pass (2026-09-30, pass 2)

Overwrites the prior pass's DECISION.md (that one chose Candidate 1 =
"normalization.npz is applied to the observation", which LANDED — see
`PLAN.md`/`VALIDATION.md` and AUDIT.md §5 "ALREADY DONE"). This pass is
decided on the FRESH AUDIT.md (`master` @ `17899e1`, 106 tests green), not on
the earlier audit the researchers were assigned from.

---

## 1. OUTCOME TYPE

**IMPROVE-EXISTING.**

Chosen on AUDIT.md evidence, not by default. AUDIT.md §7 states the finding
directly: *"this repo's data gap is not 'we need another source' — it is that
the three sources already wired in cannot currently be trusted, and the one
window the model was trained on is not recorded"* and *"the Decision phase must
not treat NEW-DATA-SOURCE as the default outcome — on current code evidence the
cheapest high-directness work is not a new project."* The fresh audit's top-3
are all zero-new-sourcing IMPROVE-EXISTING / in-place-activation items; the
genuinely-new-source directions (Candidates 6–9: book/trade recorder, on-chain,
macro, academic) all lose on directness-vs-cost (AUDIT.md §4: D1 low-medium +
mostly paid + needs an asset→pair mapping layer; D2 low + weekly/daily horizon
vs an hourly observation; D3 very low, no route from a paper to a per-(ticker,
hour) numeric column).

**No new repo. No naming step. No submodule.** Phase 4 is **4B — implement in
the existing target**.

## 2. CHOSEN TARGET (one)

**Candidate 1 — make the three existing signal seams sound before anything is
added to them.** AUDIT.md §3 A1–A4; §4 Candidate 1.

- **A1 ticker-blind join** — `data.py:134-166` never reads the `ticker` field
  that is present in *every* record shape (`ticker_news_signals/models.py:97-104`,
  `kraken_social_signals/models.py:34-40`, `kraken_funding_rates/models.py:53-66`).
  Reproduced: a `BTC_USD` signal file merges onto an ETH/USD frame with **no
  error, no log** — `sentiment_score = -0.9` on ETH bars. All 8 merge tests
  (`tests/test_rl_data_store.py:211-374`) write records with **no `ticker`
  field**, so the join key is untested.
- **A2 duplicate-hour hard crash** — `data.py:161`
  `signal_df.reindex(ohlc_index)` on a non-unique index raises
  `ValueError: cannot reindex on an axis with duplicate labels`. Reproduced two
  ways. **The sibling's own documented cadence produces it**:
  `ticker-news-signals/INTEGRATION.md:114-120` documents an hourly cron that
  appends to the same JSONL, and `fetch_signals(lookback_hours=1)`
  (`ticker_news_signals/pipeline.py:96`) re-emits the current hour every run.
  A second pull inside the same hour kills every train, backtest, paper tick
  and export.
- **A3 unbounded ffill, no freshness** — `data.py:165-166`
  `.ffill().fillna(0.0)`; grep for `stale|age_hours|asof|freshness|last_seen`
  over the package returns one unrelated hit (`paper_trade.py:314`). Reproduced:
  one record at h0 propagates *unchanged* to h1, h2. Funding settles ~8-hourly
  and news/social are hand-pulled, so multi-hour gaps are the normal case, and
  the agent cannot tell a 3-day-old `funding_rate` from a current one.
- **A4 absence == a genuine extreme** — `kraken_social_signals/pipeline.py:38`
  `_FNG_MISSING = 0` collides with the low end of the real 0–100 scale
  ("extreme fear"), and `:52-62` `_tilt` returns `0.0` for "no tagged message",
  which is also "perfectly balanced"; `data.py:166`'s `fillna(0.0)` then makes
  *no record at all* identical to *neutral*.

**Why this is the winner, not a footnote:** it is the only candidate that is
**upstream of the other three**.

1. It blocks Candidate 4 (schedulers + flake) *mechanically*: A2 means the
   cadence AUDIT.md C1 wants to ship is the thing that crashes the merge. You
   cannot schedule a pull loop onto a seam that raises `ValueError` on the
   second pull of the hour.
2. Candidate 2 (activate `spread` from the funding JSONL, 49→51 features, zero
   new API calls) rides **the same allow-list at `data.py:147,156`**. Widening
   that allow-list before the join is sound means shipping *more* columns
   through a join that can silently attach BTC's numbers to ETH and then
   forward-fill them for days. Worse: without A3's age column, an 8-hourly
   `spread` forward-filled across a whole session is precisely the
   stale-read-looks-fresh defect — so Candidate 2 is only safe *after* this one.
3. Candidate 3 (record the window + holdout) is eval integrity: it decides
   whether a feature's measured value is real. It is meaningless if the 9
   exogenous columns feeding the A/B are silently wrong (A1) or stale (A3).
4. Its own blast radius is the largest per unit of work: these 9 columns are
   the **only** non-OHLC inputs that reach the observation
   (`features.py:404-417`), and the defect is repeated on every train, every
   backtest and every 60 s paper tick because the merge runs 3× per read
   (`data.py:323-325` live path, `data.py:457-459` store path).

Zero new sourcing. Low risk. The only behavioural change is that a
mis-configured or duplicated file **fails loudly** instead of silently.

## 3. LIBRARY(IES)

**None. Stdlib + pandas only.** No new dependency, no new transitive closure.

The four defects are join-key selection, de-duplication, time-based ffill
bounding, and missing-value encoding — all expressible in the pandas already a
hard dependency of the package (`flake.nix:58-64`). Anything else would be a
re-plumbing of a working seam: `duckdb` (storage/query layer, not a join
policy), `dvc`/`lakeFS` (dataset versioning, for a question about *stale
values*, not dataset identity), `polars`/`dask` (parallelism this frame size
does not need). RESEARCH-1 §7 reached the same conclusion for the adjacent
window-recording gap and its library survey is reused here; RESEARCH-2 and
RESEARCH-3 likewise returned "no new deps" (measured, not assumed). Consensus
across all three researchers: **there is no library-shaped win on this board.**

## 4. IMPROVEMENT SCOPE (Phase 4B)

**Touched repo: this one only — `/home/seanc/Projects/kraken-trading-bot`.**
No sibling repo is modified. (Rationale, consistent with PLAN.md §2.2: a
one-repo outcome keeps Phase 4B inside this repo's boundary. The source-side
half of A4 in `kraken-social-signals` is therefore deliberately left as an
*upstream, deferred* item — see §7 — and the bot-side disambiguation is
achieved in-repo instead, which is both sufficient and cheaper.)

### Primary unit — the merge seam

`kraken_trading_bot/rl/data.py:79-176` `merge_extra_features()` — the **exact
landing point**. It is the single door through which every exogenous column
enters the OHLCV frame for all four consumers. It is called with the caller's
`pair`/`ticker_id`, so it is where the feature is **read per ticker**. Four
changes:

| # | Change | Site | Effect |
|---|---|---|---|
| 1 | **Ticker filter** — require/derive the `ticker` field and filter records to the requested pair *before* building `signal_df`; treat a file with no `ticker` field as **explicitly opted-in to the one-ticker file** (log at WARNING, so the existing 8 tests and the documented one-ticker cron keep working) and hard-fail on a file whose tickers do not include the requested pair | `data.py:134-141` | Kills A1. A `BTC_USD` file can no longer silently annotate ETH bars |
| 2 | **De-duplicate the floored hour** — `groupby(level=0).last()` (last write wins, i.e. the most recent pull for that hour) before the reindex; also `drop_duplicates` on the raw record index | `data.py:144-156` | Kills A2. The documented hourly-append cron stops raising `ValueError`; a two-ticker file stops crashing even before the filter above |
| 3 | **Bounded ffill + freshness column** — replace `.ffill()` with `.ffill(limit=<hours derived from the bar interval>)`, and add a `signal_age_hours` (float, per source) plus a `signal_observed` bool so "no record within the window" is a first-class value instead of a silent zero | `data.py:161-166` | Kills A3. A 3-day-old `funding_rate` is now visibly 72h old in the observation |
| 4 | **Absence != neutral** — with the `signal_observed` flag from (3), never encode "absent" as the same float as "genuinely 0 / balanced"; `_SIGNAL_COLUMNS` members that are absent entirely keep their existing `fillna(0.0)` behaviour for backward compatibility, but a *present-but-stale* or *present-but-null* value is now distinguishable | `data.py:165-166` | Kills A4 from the bot's side, without a sibling edit |

### Secondary units (same slice, same file family)

- `kraken_trading_bot/rl/features.py:35-45` `_SIGNAL_COLUMNS` — add the
  new provenance columns to the tuple **once** (it is already the single
  canonical source; `data.py:37-41` imports it — AUDIT.md's correction: this
  centralization is DONE, not duplicated). The new age/observed columns ride
  the existing `signals` group (`features.py:25,48`) into the observation with
  **zero new plumbing**.
- `configs/default.yaml:57` (and the sibling keys at `:65,:73`) — two new keys
  beside the three existing `extra_features_file` / `funding_features_file` /
  `social_features_file`:
  `signal_max_age_hours:` (int, default `null` = current unbounded behaviour,
  so this lands safe) and `signal_require_ticker:` (bool, default `true` once
  the file set is ticker-tagged). Same in `configs/deep-history.example.yaml`.
- `tests/test_rl_data_store.py:211-374` — extend the 8 existing merge tests,
  they are the right home: **ticker mismatch must not merge**; **duplicate
  floored hour must not raise**; **ffill is bounded and `signal_age_hours`
  grows**; **`fng_index` absence is distinguishable from `fng_index=0`**.

### Explicitly OUT of scope for this slice (so Phase 4B stays one clean outcome)

A5 (defaults-drift centralization — real, but orthogonal bookkeeping),
A6/A7 (fetch reliability + store collapse — RESEARCH-3's measured answer is a
sibling-repo `transport.py` change), A8/A9 (window recording + provenance —
RESEARCH-1, the next slice), the `_SIGNAL_COLUMNS` widening that turns on
`spread` (Candidate 2 — **downstream**, it needs `signal_age_hours` to be
correct), and the schedulers/flake wiring (Candidate 4 — **downstream**, it
needs A2 fixed).

## 5. INTEGRATION SKETCH (one paragraph)

A follow-up pass turns the seam sound without touching the feature pipeline's
shape, because the observation door is already open: `read_ohlc_dataframe`
(`data.py:329`) takes `extra_features_file` / `funding_features_file` /
`social_features_file` and calls `merge_extra_features` three times on both the
live leg (`data.py:323-325`) and the store leg (`data.py:457-459`); the fix
lands entirely inside that one function, so `fetch_ohlc_dataframe` →
`prepare_episode` (`data.py:505-567`) → `TradingEnvironment._raw_feature_array`
(`environment.py:445-464`) → `_observe` (`environment.py:367-375`) →
`PPO(MlpPolicy)` (`agent.py:179`) is unchanged, and `backtest_model`
(`backtest.py:84-262`) and `PaperTrader.step` (`paper_trade.py:343-380`) inherit
it for free. What changes is what arrives at the frame: with a per-ticker
filter, a de-duplicated hour index, a bounded ffill and a `signal_age_hours` /
`signal_observed` pair appended to `_SIGNAL_COLUMNS`, the `signals` feature
group (`features.py:25,48,404-417`) forwards both raw value and its freshness,
so the observation gains 1–2 *correctness* columns per source while every
exogenous column becomes either a genuine per-ticker reading or a visibly
absent one. Fit stays slice-then-fit (`prepare_episode`, `data.py:550-559`) so
no look-ahead is introduced, and the artifacts at
`models/{TICKER_ID}/{model_name}/{model.zip,normalization.npz,config.yaml}`
keep their contract — with one caveat that must land in the same slice or be
filed loudly: widening the observation invalidates any existing `model.zip`,
`models/` is currently empty (`.gitkeep` only, AUDIT.md C3), so this costs no
retrain today, and the RESEARCH-2 width guard (`feature_names` already in the
npz, `features.py:78,101-106`) is the cheap insurance that the next widening —
Candidate 2's `spread` — cannot be loaded against a stale policy.

## 6. RUNNER-UPS (and exactly why each lost)

1. **Candidate 2 — activate dormant `microstructure` from on-disk funding data**
   (49→51 features, zero new API calls; `bid`/`ask` already emitted at
   `kraken_funding_rates/models.py:47-48,62-63` and dropped at `data.py:147,156`).
   **Lost by ordering, not by value.** It rides the *same* allow-list
   (`data.py:147`) this target repairs, and `_add_microstructure_features`
   (`features.py:389-402`) would emit an `spread` forward-filled from an
   8-hourly funding snapshot — the exact A3 defect. Cheapest width win on the
   board; **the immediate follow-up** once `signal_age_hours` exists.
2. **Candidate 3 — record the training window, then split it** (A8/A9;
   fully researched in RESEARCH-1: one `data_window: {since,until}` config
   block after `market_data_store` at `configs/default.yaml:97`, four one-line
   caller edits, provenance after `prepare_episode`, no library). **Lost on
   severity, not cost.** A3-equivalent: it does not *corrupt* the observation,
   it makes every reported return/Sharpe/drawdown less trustworthy (the 16 %
   `rsi_24` fitted-std drift, VALIDATION §4). Correctness beats
   reproducibility. Fully specced and ready.
3. **Candidate 4 — schedule the three signal projects + put them in the flake**
   (C1/C2: only `kraken-market-data` has a timer; `flake.nix:4-8,71` cannot run
   the other three CLIs; all three keys null). **Blocked by this target** —
   shipping the documented hourly cron before A2 is fixed ships a crash loop.
4. **Candidate 5 — retry/backoff + bar cache** (A6/A7; RESEARCH-3's measured
   answers: Kraken rate-limits in the **body** with HTTP 200 so
   `urllib3.Retry(429)` never fires; `from_env(min_interval=)` is a silent
   no-op at `kraken_api/auth.py:73`; the throttle is ~300× under limit so the
   real cost is the re-fetch and the 3 O(file) JSONL parses per tick). **Lost
   on boundary** — the load-bearing half is a sibling-repo `transport.py`
   change, and reliability is not signal content.
5. **Candidate 6 — order-book depth + trade-tape recorder** (B3/D4: the only
   feature with no producer; `manager.py:185,192,200` keyless and uncalled).
   **Lost on directness vs cost** — medium directness, keyless but high ops,
   and a 10-level snapshot at 60 s is a weak estimator, not book history.
6. **Candidate 7 — on-chain** (D1). Low-medium directness, mostly paid/API-keyed,
   needs an asset→pair mapping layer that does not exist. Rejected.
7. **Candidate 8 — macro calendar** (D2). Low directness — macro events move
   this on a weekly/daily horizon; the observation is hourly. Rejected.
8. **Candidate 9 — research/academic as a data source** (D3). Very low
   directness; no route from a paper to a per-(ticker, hour) numeric column.
   Rejected.

## 7. DEFERRED, WITH WHAT MUST HAPPEN FIRST

- **Candidate 2 (`spread`/`order_book_imbalance`)** — after this slice. Needs
  `signal_age_hours`; then widening `_SIGNAL_COLUMNS` is one line. The
  `bid_vol`/`ask_vol` half additionally needs a recorder (Candidate 6).
- **Candidate 3 (window + provenance)** — independent; can run in parallel, but
  sequence it here so the A/B it enables measures a *correct* observation.
- **Candidate 4 (schedulers + flake)** — after this slice; the hourly-append
  cron is the reproducer for A2, so it is the natural acceptance test.
- **A5 (defaults drift: `_FEATURE_GROUPS` re-spelled 4× + `pages=6` ×5 + the
  `export.py:73-79` fifth spelling; `[1,4,24]` ×4)** — real and cheap, but
  orthogonal bookkeeping, not a correctness defect. Filename-free follow-up.
- **A6/A7 + `transport.py` retry** — sibling-repo change; RESEARCH-3's design
  is ready when that boundary is opened.
- **A4 source-side in `kraken-social-signals`** (`_FNG_MISSING = 0` at
  `pipeline.py:38,136`; `_tilt` `0.0` at `:52-62`) — the bot-side
  `signal_observed` flag makes the ambiguity harmless from here; changing the
  sibling's on-disk encoding is an upstream improvement, deliberately not
  bundled.
- **RESEARCH-3's bonus bug** — `until` is silently dropped on the live
  (null-store) path (`data.py:402-412`); one-line fix + test, file it with the
  Candidate 3 slice where `until` starts mattering.
- **Run the `kraken-deep-history seed`** (`RESEARCH-1.md` §5) — still ops, not
  code, and still un-run on this host. The store root is absent and
  `market_data_store: null` everywhere.

---

## 8. PHASE 4 VERDICT

**Phase 4B — implement in the existing target**, inside
`kraken_trading-bot` alone. `merge_extra_features` (`data.py:79-176`) plus
`_SIGNAL_COLUMNS` (`features.py:35-45`), two config keys, and four new
regression tests in `tests/test_rl_data_store.py`. No new repo, no submodule,
no new dependency.

DECISION COMPLETE