# RESEARCH-2.md — gaps G-A and G-B (`.data-audit/AUDIT.md` §4)

**Phase 2 (researcher2), read-only, 2026-10-03.** Repo
`/home/seanc/Projects/kraken-trading-bot`. HEAD `f6d9118`.
**Not committed. No source file modified. No other `.data-audit/` file touched.**

Source layout note for the assembler: the RL package is `kraken_trading_bot/rl/`,
not `kraken_trading_bot/`. AUDIT.md §1.1 already says this; it is restated here
because three of my `file:line` citations had to be re-resolved against it.

## Method and evidence tiers

Same three tiers as AUDIT.md §0, used identically:

- **[M] measured** — I ran the command; it is quoted. Scripts in `/tmp/opencode/`.
- **[C] cited** — read directly off the source or a local man page / installed
  docstring at the line quoted.
- **[I] inferred** — reasoned, never presented as measured.

Environment: everything Python ran inside
`nix develop --command python …` (pandas **3.0.4**, pyarrow **24.0.0**, measured).
Two scripts, both reproducible:

```
nix develop --command python /tmp/opencode/r2_tail_exact.py   # tail(k) exactness + timing
nix develop --command python /tmp/opencode/r2_tail_z.py      # tail(k) error in sigma
```

Both use a **synthetic** frame and **no store and no network**. The store
`~/Projects/kraken-market-data/store` does not exist on this host (AUDIT.md §1.4),
so **no store-read cost in this document is measured**; every store cost claim is
[C] from the reader's source or [I].

### What I did NOT measure, and will not re-derive

`~/Projects/kraken-market-data/store` is absent (AUDIT.md §1.4, re-confirmed by the
lead). Therefore I **cannot** reproduce the prior passes' "76.5k bars" / "158
missing bars across 28 gaps" figures (configs/default.yaml:198-209 records the
latter). Nothing below inherits a store-size number. Where a number would be
needed I have written `[unverified — needs a seeded store]`.

---

## 0. Bottom line — five recommendations

| # | Recommendation | Effort | Why |
|---|---|---|---|
| R1 | **Do not pass `since` into `read_ohlc_dataframe` as-is.** Split the parameter: add a distinct read window (`read_since`/`read_until`, or `fetch_cursor` kept separate) so a pinned window cannot silently retarget the live append. | small | `since` is *already* the fetch cursor (`data.py:1385`). Naïve plumbing turns "fetch the last N pages" into "page forward from `since`", which never reaches the recent tail. §1.2 |
| R2 | **Keep the post-read clip in `training_frame`/`evaluation_frame` as the single source of truth**; push the window down only as an *optimisation*, under it. | small | The live leg drops `until` entirely (no such parameter on `fetch_ohlc_dataframe`, `data.py:1136-1152`), and an empty windowed store read raises the *generic* `NotEnoughDataError` (`data.py:1402-1404`) instead of `PinnedWindowUnavailableError` (`data.py:370-380`). Moving the authority destroys the named error. §1.3 |
| R3 | **Fix G-B(i) as a one-line forward plus a guard, and change the default from "assert Kraken" to "unknown".** | very small | The store's own metadata cannot identify a venue: `_meta.json` has exactly two top-level keys, `schema` and `cursors` (store.py:60, :491-502), and the deep-history seeder *does* write a cursor (`kraken-deep-history/tests/test_seeder.py:66`), so "no cursor ⇒ not live" is refuted. §2 |
| R4 | **G-B(ii) is a *network* problem, not a compute problem — measured.** The full re-derive costs ~26 ms; tail-ing the frame saves nothing (compute is 26.1 ms on 1440 rows and 25.0 ms on 200). Fix the 2 HTTP page calls per minute; leave `compute()` alone. | small | §3.1 timing table. `[M]` |
| R5 | **A bounded recompute is required and the radius is measurable: `k ≥ 400` bars makes every feature family except `obv` exact to float noise. `obv` never converges and must be carried forward.** | medium | §3.2 measured σ-error table. `[M]` |

---

## 1. G-A — pushing the time window down into the read

### 1.1 The cost model in AUDIT.md G-A is wrong about where the cost is [M][C]

> AUDIT.md §4 G-A: *"with a seeded store, `train` reads **the whole store** and
> then throws most of it away."*

Half of that is already untrue. `store.read` prunes by **filename** before
opening anything:

```python
# kraken-market-data/market_data/store.py:168-171   [C]
for month in months_between(since_i, until_i):
    path = _month_file(self.root, pair_id, interval, month)
    if not path.exists():
        continue
```

and `months_between` is a pure `YYYY-MM` key enumerator over the requested range
(`kraken-market-data/market_data/utils.py:94-109`, safety-bounded at 2400 months).
**A windowed read touches only the months the window covers.** The
`pd.concat` + post-read row filter that follows (`store.py:181-184`) is a
correctness belt-and-braces, not the cost centre.

So G-A's real saving is **proportional to months spanned**, not to store depth:
a 90-day window over a 6-year store opens ~3 of ~72 files. That is a large
absolute saving but it is already implemented and already reachable — no
library work is needed for it. What remains unimplemented is that **no caller
passes the arguments**.

**What *would* help, and is genuinely unimplemented:** the row filter is
post-read per month (`store.py:181-184`), i.e. every matching month file is read
**whole** even if the window covers 2 hours of it. That is where predicate
pushdown is real. See §1.4 — and note that at this repo's scale it is not worth
a dependency, for reasons given.

### 1.2 The blocker nobody has flagged: `since` is already the FETCH cursor [C]

```python
# kraken_trading_bot/rl/data.py:1385   [C]
candles = _page_candles(pair, interval, pages, source, since)
```

`_page_candles` uses `since` as the *starting cursor of the live page loop* and
then walks forward with `manager.ohlc(pair, interval=…, since=cursor)`
(`data.py:1119-1133`). So `read_ohlc_dataframe`'s `since` today means **two**
things at once:

1. where the live append starts paging (fetch cursor), and
2. what window `store.read` returns (read filter).

The four call sites pass neither, so today both meanings are `None` and the
behaviour is "fetch the trailing window, read the whole store". **Turning on
`since=` at a call site changes both meanings at once**, and the second change
is silent and wrong:

- `train.py:232` resolves a pinned window and calls `training_frame`, which is a
  *post-read clip*. If `since` is additionally passed to the read, the fetch
  loop starts at `since` and pages forward `pages` times from there. With
  `pages=6` (`cli.py:154`) and `since` years back, the append re-fetches six
  ancient pages, upserts candles that are already in the store, and **never
  touches the recent tail** — so the store stops advancing and a paper/backtest
  run silently trades on stale bars.
- The severity is bounded by one thing: `upsert` dedupes on bar `time`
  (`store.py:239`), so the damage is *wasted requests*, not *corrupted rows*.
  That is luck, not design.

**R1.** Two shapes, both small:

- **(a) Split the parameter.** `read_ohlc_dataframe(..., read_since=None,
  read_until=None, fetch_cursor=None)`; `_page_candles` takes `fetch_cursor`,
  `store.read` takes `read_since`/`read_until`. Deprecate the shared `since`
  with the existing `DEFAULT_STORE_VENUE`-style constant discipline. This makes
  the two meanings nameable, which is the actual defect.
- **(b) Refuse the combination.** In the store branch, if `since` is not `None`,
  log that the fetch cursor is being retargeted and keep fetching from the
  *trailing* edge (`fetch_cursor=None`) so the append still advances. Fewer
  moving parts, but leaves one parameter meaning two things.

(a) is the honest fix. Either way **the live leg (`market_data_store is None`)
must be unaffected** — see §1.3.

### 1.3 The double-clip authority question — answered explicitly

**Question asked:** if `since`/`until` move into the read, does the post-read
clip become redundant, or must it stay as the authority?

**Answer: it must stay. The clip is the authority; the read window becomes a
pure optimisation *underneath* it. The clip must not be deleted, and it must
not be moved above the read either.**

Five reasons, in descending order of force. The first is decisive.

**(1) Moving the window into the read destroys the named error [C].** This is
the load-bearing one. Today the pin's failure mode is specific and good:

```python
# data.py:1402-1404   [C]
df = store.read(pair, interval, since=since, until=until)
if isinstance(df, pd.DataFrame) and df.empty:
    raise NotEnoughDataError(1, 0, what="OHLC candles")
```

An empty *windowed* store read raises the **generic** error. The diagnostic the
audit calls "the honest answer" lives in `data_window.py` and is raised by
`_guard_pinned_coverage` (`data_window.py:474-508`), which reaches it only
because the frame it is handed is still **whole** and it can quote the available
span:

> *"Pinned data_window … does not overlap the N bar(s) that were actually read:
> requested …, available …"* — `data.py:371-380`

Push `since` into the read and `available_span` becomes `"None"`, the reason
degrades to *"the read returned no bars at all"* (`data_window.py:489-492`), and
the fix text — the whole `STORE_SEED_HINT`, 11 lines naming `just store-seed`
(`data.py:90-99`) — never gets printed. AUDIT.md is explicit that
`NotEnoughDataError` on a `since` older than the store is the honest answer and
must not be papered over; **the clip is the mechanism that produces the honest
answer, and pushing the read down would replace it with a lie.** If G-A is
implemented, the empty-window guard must move *inside* the store branch too, or
be given the span the read would have discarded.

**(2) The live leg has no `until` at all [C].** `fetch_ohlc_dataframe` takes no
`until` parameter (`data.py:1136-1152`), and `read_ohlc_dataframe` does not
forward one (`data.py:1326-1335`). So on the live leg `until=` is accepted by the
signature and **silently dropped**. A pin's upper bound is enforced on the live
leg *only* by `clip_to_window` (`data_window.py:334-337`, `mask &=
comparable < window.until` at `:317-320`). Delete the clip and `until` becomes a
no-op on exactly the leg that needs it. `[I]` A caller reading the signature
would reasonably not discover this.

**(3) The pin is a bar-count fraction, so the clip's input must be the whole
window [C].** `training_frame` takes the *leading* `eval_split` of the clipped
window (`data_window.py:386-412`), and `split_index` is
`round(n_bars * eval_split)` (`:369-380`). If the read were handed a
`read_until` computed to deliver exactly the training half, the split would be
computed on an already-halved frame and `eval_split` would be applied twice —
once as a boundary and once as a fraction. The read window must therefore be the
**union** (the full pin), and the split stays where it is.

**(4) Kraken's REST ceiling means the window is an upper bound on availability,
not a selector [C].** `data.py:283-287` and `data.py:378` both state the ~720-bar
ceiling and that older data "cannot be retrieved regardless of `since`". So on
the live leg the pin can only ever select from what `--pages` reached. The clip
is what turns "asked for the wrong window" into
`PinnedWindowUnavailableError`; without it, a pin overlapping nothing looks
identical to a pin overlapping half the frame.

**(5) `eval_split` is inert while unpinned, and that must stay true [C].**
`DataWindow.has_split` requires `is_pinned` (`data_window.py:127-137`), and
`clip_to_window` returns *the same object* when unpinned (`:352-353`) so an
unpinned run is byte-identical to one from before the module existed. Any design
that makes the read window authoritative risks turning the unpinned default into
a path that computes something new — the one outcome this repo has explicitly
forbidden twice (module docstring `:26-34`, and "do not change the default path"
repeated at `data_window.py:29-34`).

**Concretely, the recommended shape:**

```
resolve_data_window(cfg)                       # one source of truth, unchanged
  -> since/until            (the pin; stays authoritative)
  -> read_since/read_until  (same bounds, or None; optimisation only)
     pass to the store branch ONLY (market_data_store is not None)
     NOT to _page_candles  -- see §1.2
  -> df (whole read result)
  -> training_frame / evaluation_frame          # the clip, UNCHANGED
```

Invariants to assert in tests, which are what makes this safe:

- `read_window=None` ⇒ today's behaviour, byte-identical.
- live leg (`market_data_store is None`) ⇒ `read_since`/`read_until` are ignored
  entirely, `fetch_cursor` still `None`, `pages` still bounds the loop.
- A pin that overlaps nothing ⇒ **still** `PinnedWindowUnavailableError` with a
  non-empty `available_span`, on **both** legs.
- `read_since`/`read_until` and the clip must produce the *same* row set. The
  cheap way to say that: after the change, assert `clip_to_window(windowed_read,
  w)` and `clip_to_window(whole_read, w)` are equal — a property test, not a
  golden file.

**Half-open semantics already agree, and that is not a problem [C].** `since`
inclusive / `until` exclusive is stated in `data_window.py:16-19` and implemented
at `:317-320` (`>=` and `<`); the store uses the same convention, documented at
`store.py:155-156` and implemented at `:182-184` (`>= since_i`, `< until_i`).
**Verified consistent — so no boundary bug is introduced by unifying them.** The
one asymmetry to watch is the type: the clip compares tz-aware `Timestamp`s
against a UTC `DatetimeIndex`, while the store compares **int epoch seconds**.
`data_window._parse_bound` accepts epoch seconds too (`data_window.py:229`:
`pd.Timestamp(value)`), so the conversion has to be explicit at the seam — a
`pd.Timestamp` passed to `store.read` would be `int()`-cast by `store.py:163-164`
(`int(since)`), which is epoch **nanoseconds** if it came from
`.value` and **seconds** if it came from `.timestamp()`. Getting this wrong
silently returns the whole store (a nanosecond epoch is year 2554, so
`months_between` hits its 2400-month safety bound and enumerates everything).
**Use `.timestamp()` and assert it in a test.**

### 1.4 Library survey — predicate pushdown on a partitioned store

The pattern set, with what each buys *here*.

**DuckDB `read_parquet`.** Primary doc, "Partial Reading" section:
<https://duckdb.org/docs/current/data/parquet/overview.html#partial-reading> [C]:

> "DuckDB supports projection pushdown into the Parquet file itself… DuckDB
> also supports filter pushdown into the Parquet reader. When you apply a filter
> to a column that is scanned from a Parquet file, the filter will be pushed
> down into the scan, and **can even be used to skip parts of the file using the
> built-in zonemaps**. Note that this will depend on whether or not your Parquet
> file contains zonemaps."

Same page: a glob (`read_parquet('…/*.parquet')`) or a file list is one scan;
`hive_partitioning` is auto-detected; a virtual `filename` column is available
automatically from DuckDB v1.3.0; `ROW_GROUP_SIZE` is a *write* parameter. [C]
This is the cleanest available expression of "prune by partition, then
predicate-push inside the surviving files" — `WHERE time >= ? AND time < ?` over
a month glob, with the store's `months_between` doing the partition pruning in
Python exactly as it does today. **Recommendation: not worth it for this repo.**
The win is bounded by a month file holding ~730 hourly rows; the current
`pd.read_parquet` of one such file is single-digit milliseconds, and — per §3.1 —
this repo's whole compute budget per tick is 26 ms. Adding a DuckDB dependency
to a flake-pinned dev shell to save milliseconds is the wrong trade. **Record
the option; do not take it.**

**pyarrow.dataset.** `[unverified — pyarrow.apache.org was unreachable from this
host on 2026-10-03; no claim below is measured]`. The API surface is
`ds.dataset(source, format="parquet", partitioning=…)` plus a `to_table(filter=…)`,
and it exposes the same two ideas (partition discovery, row-group/bloom-filter
skips). pyarrow **24.0.0 is already in the dev shell** (`kraken-market-data`
depends on it — AUDIT.md §1.2), so this option needs no new dependency, only a
sibling edit. But the sibling is a **pinned flake input** (`flake.nix:7`), so
editing it requires a lock bump (the same constraint `configs/default.yaml:216-224`
gives as the reason the venue label lives in this repo's config). If a pushdown
change is ever wanted, this is the lower-friction of the two, and the honest
framing is: **it is a `kraken-market-data` change, not a `kraken-trading-bot`
change.**

**Row-group / zonemap prerequisite [I].** Pushdown only helps if the month files
carry statistics. `store.upsert` writes with plain
`merged.to_parquet(path, engine="pyarrow", index=False)` (`store.py:245`), i.e.
default row-group size and default statistics. Whether row-group min/max is
actually present is `[unverified — needs a seeded store to inspect]`. Note the
column that would carry the filter is `time`, written **first** in
`_OHLCV_COLUMNS` (`data.py:1078`, `candles_to_dataframe` at `:1073-1082`), which
is the friendly ordering, and `store.stats()` already reads exactly this metadata
(`store.py:393-401`, `pf.metadata.row_group(0).column(0).statistics.min`) — so
the sibling already knows how to read it. That is the cheapest possible proof
once a store exists.

**Compare against the naive path actually in use [C]:** `glob` + `read_parquet`
per month + `pd.concat` + boolean row filter (`store.py:168-184`). Note the
audit's G-F "no reindex, no gap assertion" is **not** what I need to correct
here — the row filter is present and correct; what is absent is any *time*
semantics, which is a different gap and out of my slice.

### 1.5 The stale `.. todo::` [C]

`data.py:1319-1323`:

```
.. todo:: Follow-up integration pass …: honour ``since``/``until`` from config;
   drive train/eval split and walk-forward through the currently-unused
   ``TradingEnvironment.reset(options=...)``; let backtest skip the re-fetch and
   paper trade collapse to append + tail read.
```

Three separate claims in five lines, and **they have different truth values
today** — which is exactly why it misleads:

| clause | truth at HEAD | action |
|---|---|---|
| "honour since/until from config" | **already done one layer up**, as a post-read clip (`train.py:232-233`, `backtest.py:518-519`). What is missing is only that it is not *pushed into* the read. | rewrite, do not delete — a reader needs to know the clip exists |
| "let backtest skip the re-fetch" | **still open.** `backtest.py:471-499` always calls `read_ohlc_dataframe`, which on the store leg always fetches+upserts before reading (`data.py:1385-1402`). A backtest that only needs already-stored bars still makes N page calls. | keep |
| "let paper trade collapse to append + tail read" | **still open** — §3. | keep |
| "walk-forward via `TradingEnvironment.reset(options=…)`" | **still open** — AUDIT.md §3 G5: no caller. | keep |

Recommended replacement text: name G-A precisely ("the window is applied as a
post-read clip; it is **not** pushed into `store.read`, so a seeded store is
read whole; pushing it down must not move the authority — see `data_window.py`"),
and drop the "honour from config" clause entirely.

---

## 2. G-B(i) — venue provenance as a pattern

### 2.1 The defect, restated from the source [C]

`market_data_store_venue` is forwarded by `train.py:226`, `export.py:176` and
`backtest.py:493`; `paper_trade.py:298-309` omits it. So on the one leg that
places orders, `data.py:1362` (`venue = market_data_store_venue or
DEFAULT_STORE_VENUE`) silently substitutes `"kraken-live-rest"`
(`data.py:84`) and `data.py:1367-1374` logs it.

`SEEDED_STORE_VENUE = "binance-spot-archive-seeded"` **already exists**
(`data.py:85`) — so the vocabulary is already right; only the forward is missing.

### 2.2 Is the config-key the right shape? Weighing it properly

The audit asks whether provenance should be carried as data. I went looking for a
store-side signal and **found one that looks right and then refuted it** — the
refutation is the useful part.

**The store's `_meta.json` has exactly two top-level keys** [C]:
`_META_SCHEMA_VERSION = 1` (`store.py:60`), and the payload is
`{"schema": …, "cursors": {…}}` (`store.py:491-502`, `:309-315`). There is **no**
venue field, and `_write_meta` is private — a consumer has no supported way to
add one. So "put it in `_meta.json`" is a **sibling change**, and
`configs/default.yaml:216-224` already gives the correct reason:

> *"It lives here rather than in the seeder's `_meta.json` because this repo
> consumes kraken-market-data as a PINNED flake input (rev `055d7f6`): a sibling
> edit would not reach `nix develop` without a lock bump."*

That reasoning holds. **But** it argues only for where the *default* is written.
It does not address the actual failure, which is that **one of four call sites
forgets to pass it**. A provenance channel whose failure mode is "a new call site
silently gets the default" is not a provenance channel, it is a convention.

**The refuted shortcut, stated so nobody re-derives it.** A tempting store-side
inference is: *"`_meta.json.cursors` has no entry for `PAIR:interval` ⇒ this
store was never appended to by a live Kraken pull ⇒ the venue is not
`kraken-live-rest`."* This is **false**, and here is the proof:
`kraken-deep-history/tests/test_seeder.py:66` asserts
`meta["cursors"]["ETH_USD:60"]["since"] == 1735862400` — the Binance-archive
seeder **writes a cursor**. `[C]`. A store seeded from Binance has cursors, so
cursor presence proves nothing about venue. (For completeness: a store populated
by `kraken-market-data pull` also sets cursors, via `store.py:309-315`. The two
are indistinguishable.) Any design leaning on `_meta.json` for venue must first
extend its schema — which is the same lock bump the config key avoids.

### 2.3 Recommendation [I, from the above]

**Keep the config key. Change what the default *means*, and make the omission
loud instead of silent.** Three parts, in priority order:

1. **Forward it on the paper leg** (`paper_trade.py:308`, one line) — the actual
   defect. The artifact config is already the right source: `PaperTrader.__init__`
   sets `self.config = record.config` (`paper_trade.py:163`), the same
   `record.config` that carries `market_data_store` at `:308`, so the value is
   *already in scope* at the call site. It is not a missing lookup; it is a
   missing kwarg.
2. **Stop defaulting to an assertion.** Add `DEFAULT_STORE_VENUE = None`
   semantics (or a distinct `"unknown"`), and on the store leg log a **warning**
   when no venue was supplied, naming both candidate values. `data.py:82-83`
   currently justifies the default as *"the config key … states which venue the
   bars came from"* — on the live leg it states nothing, so the justification
   does not hold and the comment should say so. A missing label that logs loudly
   is recoverable; one that logs `kraken-live-rest` is not.
3. **Make the artifact the durable record (already true — protect it).**
   `register_model` writes the resolved config beside `n_features`/`n_bars`
   (`train.py:288-306`, config key at `train.py:139`), so the venue travels with
   the model. `tests/test_market_data_store_seeding.py:462,475` already asserts
   the default and the non-default case at the *config* level — the missing guard
   is a **forwarding** guard, and the audit is right that
   `tests/test_rl_signal_config_wiring.py:497-551` (AST-asserts this class of
   forwarding) is where it belongs. **The highest-value single test in this whole
   slice** is an AST/keyword assertion that *every* `read_ohlc_dataframe(` call
   site in the tree passes both `market_data_store_venue=` and the new read
   window — one test that would have caught G-B(i) and would catch the next leg
   that forgets.

**Explicitly rejected:** writing a venue sidecar next to the store root. It
duplicates the config key, is not written by the consumer that would read it,
and re-creates the "label lives in two places" hazard the single key avoids.

**Sequencing note for the leader:** if only one part ships, ship part 1. Parts 2
and 3 are hardening.

---

## 3. G-B(ii) — the per-tick loop, and what "incremental" can honestly mean

### 3.1 The measured cost: the compute is not the problem [M]

AUDIT.md §4 G-B(ii) says the loop *"rebuilds the entire feature matrix and
re-z-scores it … to read one row."* Two corrections:

**Correction 1 — the normalisation is already artifact-frozen, not refitted per
tick [C].** The audit's premise ("a live leg that refits per tick is not the same
model") describes a hazard this repo does not have:

```python
# paper_trade.py:196-197   [C]
if record.normalization_path is not None:
    pipeline.load_normalization(self.ticker_key, record.normalization_path)
# environment.py:189-190   [C]
if self.pipeline.stats_for(self.ticker_id) is None:
    self.pipeline.fit(self.data, ticker_id=self.ticker_id)
# paper_trade.py:337       [C]
stats = self.pipeline.stats_for(self.ticker_key)
```

Stats load from `normalization.npz`; the environment fits **only if they are
absent**; `_build_observation` normalises with those loaded stats. So the live
leg is already the "VecNormalize loaded-stats-at-eval" pattern it claims to be
(`paper_trade.py:311-318`). **The design constraint for any incremental rewrite
is therefore a *preservation* constraint: do not introduce a per-tick `fit()`.**
That is the real form of the crux the brief poses, and it is currently satisfied.

**Correction 2 — `compute()` costs the same on 1440 rows as on 200 [M].**
`/tmp/opencode/r2_tail_exact.py`, 1440 synthetic hourly bars, 52 columns on this
all-signal-null configuration (the low end of AUDIT §2.4's measured 49–61 range —
**there is no single width**), mean of 10 reps:

| frame | `FeaturePipeline.compute()` |
|---|---|
| `tail(1440)` (all of it) | **26.13 ms** |
| `tail(800)` | 26.25 ms |
| `tail(400)` | 25.84 ms |
| `tail(200)` | 25.02 ms |

Flat to within 5%. The cost is per-column Python/pandas overhead, not per-row.
Against a 60 s tick that is **0.04 %**. **Conclusion: an incremental-recompute
design buys ~1 ms per tick and is not worth its risk.** The waste is the **2
HTTP page calls per minute** (`paper_trade.py:65, :301`, driven from `step()` at
`:386` on the `interval=60` sleep at `:597`/`:665`).

**So the G-B(ii) fix is, in order:** (a) stop re-paging every tick — the store
leg already upserts+reads, so the *fetch* is the redundancy, and `pages=2` buys
nothing on a store that this same loop is appending to; (b) make the fetch
cursor-monotonic (`fetch_cursor=` from `_meta.json`, `store.cursor()` at
`store.py:296-303`) so an unchanged bar issues **zero** requests; (c) leave
`compute()` alone; (d) `data.py:1125-1133` has no `try`, so one 429 kills the
tick (AUDIT §3 G3) — the poller needs the retry/backoff of §4.

Note `paper_trade.py:328-329` already has a `context_bars` tail knob
(`df.tail(self.context_bars)` before `compute()`). Per the table above it
saves nothing, and per §3.2 it would introduce error unless `context_bars` is
≥ the bounded-recompute radius. **If it is used, its floor must be documented as
400, not left implicit.**

### 3.2 What a bounded recompute costs in accuracy — measured, in σ [M]

The live leg z-scores with the artifact's stats, so the only error that matters
is `|Δrow| / artifact_std`. `/tmp/opencode/r2_tail_z.py`: 4000 synthetic hourly
bars, `windows=[1,4,24]`, all five groups, stats fitted on the full frame
(`pipe.fit`, i.e. exactly the `normalization.npz` that `paper_trade.py:196-197`
loads), then the last row recomputed on `df.tail(k)`.

| k | cols \|dz\|>0.01 | max \|dz\| (σ) | worst offender |
|---:|---:|---:|---|
| 25 | 12 | **2.67** | `rsi_24` |
| 50 | 12 | 0.72 | `rsi_24` |
| 100 | 3 | 0.061 | `atr_24` |
| 200 | 1 | 0.0011 | `rsi_24` |
| 400 | 1 | **4.9e-8** | `rsi_24` (float noise) |
| 800 | 1 | ~3e-10 | `bb_*` (float noise, *constant* in k) |
| 4000 | 0 | 0 | — (the full frame, by construction) |

Per-family worst member, σ:

| family | k=25 | k=50 | k=100 | k=200 | k=400 | k=800 |
|---|---:|---:|---:|---:|---:|---:|
| `obv*` | 1.40 | 1.44 | 1.50 | 1.14 | 1.53 | **1.62** |
| `rsi_*` | 2.67 | 0.72 | 0.055 | 0.0011 | 4.9e-8 | 1.2e-14 |
| `atr_*` | 0.829 | 0.353 | 0.062 | 0.00022 | 1.9e-8 | 8.0e-15 |
| `macd*` | 0.173 | 0.046 | 0.0020 | 8.6e-7 | 1.6e-13 | 0 |
| `ema_*` | 0.0042 | 0.00072 | 2.3e-5 | 5.3e-9 | 0 | 0 |
| `volume_zscore_20` | 4.5e-16 | 6.7e-16 | 1.1e-15 | 4.5e-16 | 1.1e-15 | 2.2e-16 |
| `bb_*` | 2.4e-10 | 2.4e-10 | 2.4e-10 | 2.2e-10 | 2.0e-10 | 2.9e-10 |

Four things follow, and they are the design spec for any incremental path.

**(a) `k ≥ 400` is the bounded-recompute radius [M].** At 400 bars every family
except `obv` is at float64 noise. `k = 200` is already < 0.002 σ, so 200–400 is
the defensible band; **400 is the number to write down**, and it is derived from
the decay of `ewm(alpha = 1/period)` and `ewm(span=26)`, not from taste. Bitwise
equality is never reached below the full frame (`/tmp/opencode/r2_tail_exact.py`:
"exact at k=1440", scanning every k from 25) — so an incremental path must be
specified in **σ tolerance**, not "exactly", or it will never pass an equality
test.

**(b) `obv` is NOT tail-recomputable — at any k [M].** It stays 1.1–1.6 σ off
while every other family converges. `[C]` the reason: `obv` is
`(np.sign(close.diff()) * volume).fillna(0.0).cumsum()` (`features.py:1000`) — a
level, not a window, and it grows without bound over the store's history. Its
*slopes* (`obv_slope_{w} = obv.diff(w)`, `features.py:1010`) are differences and
**do** converge, which is why the family row is driven by the level alone.
**Design consequence: any tail path must carry `obv`'s level forward as state**
(seed it from a full-prefix `compute()` at startup), **or** read `obv` from a
full read. There is no tail length that fixes it. This is the single hardest
constraint in §3 and it is invisible from reading the column names.

**(c) The `ewm` families are the only reason a radius exists at all [C].**
`ewm(..., adjust=False)` is a recursion `y_0 = x_0`, `y_t = (1-α)·y_{t-1} + α·x_t`
— pandas 3.0.4's own docstring, read in the dev shell (`pd.Series.ewm.__doc__`).
Infinite memory ⇒ a tail changes the seed ⇒ truncation error `(1-α)^k`. Every
`ewm` in this repo: `ema_{w}` span=w (`:956`), `rsi_{w}` `alpha=1/period`
(`:1149-1150`), `macd` spans 12/26/9 (`:1160-1163`), `atr_{w}` `alpha=1/period`
(`:1243`). Slowest is `α = 1/24` ⇒ `(0.9583)^k`, i.e. ~340 bars to 1e-12. The
measured table above confirms the arithmetic (`rsi_24` at 2.67 σ on k=25,
1.2e-14 on k=800). pandas exposes **no public state API** to carry the recursion
across ticks, so "carry the state" means either a manual recursion or a
`tail(400)` prefix — which is why (a) is the honest answer and why (b) is the
exception that must be special-cased.

**(d) The `ewm` NaN trap is inherited, and it is invisible [C].**
`features.py:809-818` records the measured behaviour: `rolling(w)` **masks** a
bad value to NaN, `ewm()` **skips** it and carries on, returning a finite wrong
value. `_require_finite` (`features.py:640-651`) then cannot see it, because
there is nothing non-finite to reject. **An incremental path inherits this
verbatim, and adds one new exposure: a tail read silently *shortens the history*
a poisoned bar sits in, which changes which `ewm` values are stale rather than
making them correct.** So the incremental design must carry a poison flag
forward, not just values: if any bar in the carried state window produced a
`_NON_FINITE_INPUTS` hit (`features.py:796-830`), the tick must log
`ewm-stale-carry` with the bar's timestamp, and must **not** be allowed to look
identical to a clean tick. The cheapest honest form: since a poisoned bar's
effect on `ewm` decays as `(1-α)^k`, record the offending timestamp and
`assert now - t_poison < k_min·α_max` — or simply refuse to trade the affected
feature and say so.

### 3.3 Recommendation [I]

**Do not implement an incremental feature pipeline.** §3.1 says it saves ~1 ms of
a 60 s tick; §3.2 says it costs a hard `obv` state machine, a σ-tolerance
specification instead of an equality one, and a new class of silent staleness.
Instead:

1. Make the **fetch** incremental (§3.1 a–d). That is where 100 % of the waste
   is: 2 page calls/minute to move at most 1 bar. With `fetch_cursor` from
   `store.cursor()` (`store.py:296-303`) and `pages` meaning "how many pages the
   gap actually needs", an unchanged bar costs **zero** requests.
2. Keep the whole-frame `compute()`. It is 26 ms and it is *the same code path
   training used*, which is the property that makes the live leg defensible.
3. Add the AST forwarding guard (§2.3 item 3), which is the durable fix.
4. **Separately worth flagging, outside my slice and unverified:** the live
   observation is a **single row** — `features.to_numpy(...)[-1]`
   (`paper_trade.py:339`) — while the training `Box` is `(n_bars, n_features)`
   (`environment.py:200`). `RLAgent.predict` forwards straight to SB3
   (`agent.py:179-182`) with no reshape. Whether a 1-D obs is accepted against a
   `(n_bars, n_features)` Box is SB3's shape detection, which I did not verify
   and which is `[unverified — outside this slice]`. Flagging it because it
   would change what any tail read has to produce (one row vs `context_bars`
   rows), which is exactly the assumption §3.2's radius is built on.

---

## 4. Backoff, rate limiting, idempotency, scheduling

Both G-A and G-B change *how often* and *how much* is fetched, so the schedule
becomes load-bearing. All citations below are from the **local man pages**
(`systemd.timer(5)`, `systemd.service(5)`, `flock(1)`), read 2026-10-03.

**`OnCalendar=` vs `OnUnitActiveSec=`.** `OnUnitActiveSec=` "defines a timer
relative to when the unit the timer unit is activating was last activated"
(`systemd.timer(5)`, Table 1). `OnCalendar=` "defines realtime (i.e. wallclock)
timers with calendar event expressions" and warns "calendar timers might be
triggered at unexpected times if the system's realtime clock is not set
correctly" (`systemd.timer(5)`, `OnCalendar=`). **For this repo: `OnCalendar=`.**
The bar grid is wall-clock — features are computed on bar *counts*, and the
config's own known limitation is that features are "computed on BAR COUNTS, not
wallclock" across gaps (`configs/default.yaml:198-209`). A monotonic
`OnUnitActiveSec=` poller drifts against the hourly grid; a calendar poller
re-anchors. The existing funding timer already made this choice:
`OnCalendar=*-*-* *:17:00` (`systemd/kraken-trading-bot-funding.timer:19`) —
off the hour by 17 min so it never races the settlement.

**`Persistent=true` has a precondition that matters here [C].**
`systemd.timer(5)`: "If true, the time when the service unit was last triggered
is stored on disk… **Note that this setting only has an effect on timers
configured with `OnCalendar=`.**" So `Persistent=true` is available *only* under
the choice above — another reason not to reach for the monotonic timer. The
existing unit's comment states the intent precisely: "Catch up after a
suspend/lid-close instead of silently skipping hours" (`…funding.timer:20`).

**`RandomizedDelaySec=` is not jitter for your protection [C].**
`systemd.timer(5)`: "Delay the timer by a randomly selected, evenly distributed
amount of time between 0 and the specified time value… **The delay is added on
top of the next determined elapsing time**." Its purpose is decorrelating *this*
host's wake-ups, not avoiding a remote's rate limit. `AccuracySec=` (default
**1min**, same page) is the coalescing knob and the man page explicitly says it
"should not be confused with `RandomizedDelaySec=`". **Use `RandomizedDelaySec=`
for what it does** (as `…funding.timer:22` does, `:22` value `120`) and do not
treat it as backoff — §4's backoff belongs in the client.

**`Restart=` / `RestartSec=` are the wrong tool for a `Type=oneshot` poller [C].**
`systemd.service(5)`: `Restart=` "configures whether the service shall be
restarted when the service process exits"; `RestartSec=` "configures the time to
sleep before restarting a service… Defaults to 100ms". Restarting a pull in a
tight loop against an endpoint that is already rate-limiting is how a poller
becomes the problem. The existing funding unit sets neither — correct — and
instead bounds the run (`TimeoutStartSec=300`, `…funding.service:40`) and makes
failure legible (`SuccessExitStatus=0`, `:42`, with the comment "A failed pull
must not be mistaken for a fresh file: leave the previous JSONL alone and let the
staleness bound (`signal_max_age_hours`) mark the bars unobserved"). **Copy that
discipline.** Retry belongs in the client with backoff, not in `Restart=`.

**Single-instance locking — required once runs can overlap [C].** A calendar
timer fires on schedule regardless of whether the previous run is still going;
`flock(1)` "wraps the lock around the execution of a command", `-n` "fail[s]
rather than wait if the lock cannot be immediately acquired", `-E` sets the exit
status for that case. Without it, a run slower than its interval produces
overlapping appenders — and `store.upsert` is a **read-modify-write** of a whole
month file (`store.py:231-246`: read, `concat`, `drop_duplicates`, `to_parquet`),
so two concurrent upserts on the same month can lose bars. That is not a
hypothetical: it is the store's write shape. **Recommendation: a `--no-block`
`flock` on the store root's lock file, with the conflict exit status mapped to a
"skipped, previous run still in flight" log line** — not an error, because a
skipped tick on a 60-minute bar grid is a non-event. `[I]` No `flock` appears in
any of the three files in `systemd/` today [C: `ls systemd/` → 3 files].

**Idempotency: the repo already has the pattern, in prose [C].**
`…funding.service:35-38`:

> "A re-fire inside the same hour (Persistent catch-up after a short suspend)
> appends a duplicate line, which is harmless and self-limiting:
> `merge_extra_features` keeps the last record per floored hour, so the extra
> line is discarded at the read seam. The file grows by ~1 line per hour, so it
> is a log, not state."

That is the correct shape for the store leg too, and the store already satisfies
it structurally: `upsert` dedupes on `time` (`store.py:239`) and `store.read`
dedupes too. **So the store leg is already idempotent and a `Persistent=true`
catch-up will not duplicate bars** — provided it is serialised by the `flock`
above. State that as an invariant and test it, rather than rediscovering it.

**Client-side backoff is the missing half, and it is a known open gap.**
`data.py:1125-1133` (`_page_candles`) has no `try`; `kraken_api/transport.py`
re-raises on `RateLimitError` (`transport.py:250`) and `min_interval` defaults to
`0.0` (`:108`) — AUDIT.md §3 G3, confirmed, with `grep` for
`retry|backoff|MAX_RETR` in `kraken_api/` returning zero hits [C]. The sibling
that *does* retry is `kraken-funding-rates/client.py:53-95` with `_MAX_RETRIES = 3`
at `:25`. **Recommendation: the retry policy lives in `kraken_api` (shared), the
schedule lives in `systemd`, and the `flock` lives in the unit's `ExecStart`.
Three layers, three homes.** G-A/G-B make the store leg the primary reader, so
the store leg's failure mode changes from "rare" to "every tick" — which is
precisely why §4 has to land with them, not after them.

**Rate-limit budget, stated as arithmetic and marked as such.** `OnCalendar=*-*-*
*:17:00` with `pages=2` ⇒ 2 calls/hour ≈ 5.6e-4 calls/s. The funding timer's own
comment quotes "~0.0003 calls/s" for its cadence (`…funding.timer:8`) — so the
two units together are ~1e-3 calls/s. `[I]` No published Kraken limit for
`/0/public/OHLC` was read in this pass, so **no headroom figure is claimed**;
`[unverified]`. The point is only that the *unpaged* store leg is the one that
must not be allowed to retry-loop.

---

## 5. Evidence appendix

### 5.1 Every measurement I made

| claim | command | result |
|---|---|---|
| `compute()` cost is flat in frame length | `nix develop --command python /tmp/opencode/r2_tail_exact.py` | 26.13 / 26.25 / 25.84 / 25.02 ms for tail 1440/800/400/200 |
| tail(k) last-row error in σ | `nix develop --command python /tmp/opencode/r2_tail_z.py` | table in §3.2 |
| tail(k) is not bitwise exact below the full frame | same, "smallest k" scan | exact at k=1440 |
| `obv` never converges | both | 1.14–1.62 σ for every k<N |
| installed versions | same | pandas **3.0.4**, pyarrow **24.0.0** |
| width of the synthetic frame | both | 52 columns, all signal files null (low end of AUDIT §2.4's 49–61) |

Scripts: `/tmp/opencode/r2_tail_exact.py`, `/tmp/opencode/r2_tail_z.py`. Nothing
written into the repo.

### 5.2 Corrections to AUDIT.md that this research produced

| # | AUDIT.md says | This pass says | Evidence |
|---|---|---|---|
| C1 | G-B(ii): the loop *"rebuilds the entire feature matrix and re-z-scores"* per tick | The **z-score stats are loaded from the artifact** and are never refitted per tick; the "re-z-score" is one affine map with frozen stats. The per-tick cost is `compute()` (26 ms, flat) + 2 HTTP calls. | `paper_trade.py:196-197`, `environment.py:189-190`, `paper_trade.py:337` [C]; timing [M] |
| C2 | G-A: `train` *"reads the whole store and then throws most of it away"* | `store.read` already prunes by month filename from `since`/`until`; the waste is proportional to months spanned, and the *remaining* waste (whole-file reads inside a matched month) is unimplemented. | `store.py:168-171`, `utils.py:94-109` [C] |
| C3 | G-B(ii)'s fix is *"append + tail read"* | A tail read of the **feature matrix** is not warranted — it saves ~1 ms and requires an `obv` state machine. The tail read that matters is the **store's** (fewer files/months), which already exists. | §3.1, §3.2 [M] |
| C4 | — (not raised anywhere) | `since` is already the **fetch cursor** (`data.py:1385`), so G-A's fix has a live-fetch failure mode that no prior artifact names. | §1.2 [C] |
| C5 | — (not raised anywhere) | On the **live** leg `until=` is silently ignored — `fetch_ohlc_dataframe` has no such parameter. | `data.py:1136-1152`, `:1326-1335` [C] |
| C6 | — (not raised anywhere) | `_meta.json` cannot identify a venue: only `schema` + `cursors`, and the Binance seeder writes cursors too, so the obvious store-side provenance signal is refuted. | `store.py:60,491-502`; `kraken-deep-history/tests/test_seeder.py:66` [C] |

### 5.3 Explicitly not measured / not claimed

- **Any store-read cost.** The store is absent on this host (§1.4). All store
  cost statements are [C] from the reader's source or [I]. `[unverified]`:
  whether the month files carry row-group min/max statistics (needs a store);
  whether pushdown would help at this file size (needs a store).
- **Any store-size figure.** The prior passes' "~76.5k bars" is not reproduced and
  not used. Same for "158 missing bars across 28 gaps" — that one *is* recorded in
  `configs/default.yaml:198-209` as a prior measurement and is cited only as
  such.
- **Any published rate limit** for `/0/public/OHLC`. §4 quotes no headroom.
- **The SB3 observation-shape question** in §3.3(4). Outside my slice.
- **All the `[unverified]` pyarrow.dataset claims** — pyarrow.apache.org was
  unreachable from this host on 2026-10-03.
- **Whether a pinned `since` on a deep store currently *works*.** Untestable here
  (no store). §1.3's recommendation is derived from the code, not from a run.

### 5.4 Suggested test guards (for the builder, not for me)

1. **AST forwarding guard** — every `read_ohlc_dataframe(` call site in
   `kraken_trading_bot/rl/` passes `market_data_store_venue=`, and passes the
   read-window kwargs. Would have caught G-B(i); catches the next leg.
   Belongs in `tests/test_rl_signal_config_wiring.py` (which already AST-asserts
   this class of forwarding at `:497-551`).
2. **The property test in §1.3** —
   `clip_to_window(windowed_read, w)` equals `clip_to_window(whole_read, w)`, on
   both legs, pinned and unpinned.
3. **The error-preservation test** — a `since` older than the store, on the store
   leg with the window pushed down, must still raise
   `PinnedWindowUnavailableError` with a **non-empty** `available_span`
   (`data_window.py:474-508`). This is the test that protects the §1.3 argument.
4. **The unit test that already exists and proves the mechanism** —
   `tests/test_rl_data_store.py:194`
   `test_read_ohlc_dataframe_honors_since_until_window` already asserts
   `since` inclusive / `until` exclusive end-to-end through `read_ohlc_dataframe`.
   **G-A needs no new unit test for the mechanism; it needs the four call sites
   to pass the arguments.** Worth telling the builder: the feature is already
   covered, which is precisely why the gap survived — the test asserts the
   mechanism in isolation and nothing asserts the callers.
5. **`obv`-carry test** if any tail path is ever built — assert `obv` at the last
   row matches the full-history value within tolerance when seeded from a
   full-prefix `compute()`, and **fails loudly** when not.