# RESEARCH-2 — CAND-2: the thin exogenous-signal channel

**Phase 2, researcher 2, read-only.** Scope: `.data-audit/AUDIT.md` §2 row **G6** and §3
**CAND-2**. Nothing was written outside this file; nothing was committed, deleted or tidied.
All measurements were taken on this host on **2026-10-02** (a Friday, market open).

---

## 0. Method and honesty notes

* Every API claim below was **measured**, not recalled. Live keyless calls were made from this
  host; where a call failed or returned an unexpected shape, that is recorded rather than
  smoothed over.
* Frame measurements use the **real** merge seam
  (`kraken_trading_bot.rl.data.merge_extra_features`) and the **real** `FeaturePipeline`, over a
  synthetic 721-bar hourly frame with `signal_max_age_hours: 12` (the shipped value) — the same
  harness the audit's 4/721 came from. Synthetic prices are used only because the *seam*, not the
  market, is what is under test; the coverage and correlation figures are properties of the join
  and are price-independent.
* Two artefacts in my own measurements are **synthetic, not findings**: `vwap_dev` reads as
  constant because the synthetic `vwap` is a fixed fraction of `close`, and `spread` is derived
  from the same synthetic `bid`/`ask`. Neither is a claim about production data.
* Scripts were written to `/tmp/opencode/` only. Nothing in the repo was touched except this file.
* `nix develop --command bash -c "python -m pytest tests/test_rl_signal_config_wiring.py -q"`
  → **21 passed in 1.40s**. The consumer side of the seam is green; only its *inputs* are missing.

---

## 1. HEADLINE: two of the audit's four premises do not survive measurement

The audit's CAND-2 rests on four load-bearing claims. Two are confirmed; two are wrong, and one of
those two costs us the most.

| # | Audit's claim | Verdict | The measurement |
|---|---|---|---|
| 1 | Exogenous coverage is ~0.5% | **CONFIRMED** (range now measured) | **0.14% – 1.80%**, depending on where the single record sits relative to the frame's right edge. `1/721` when the record is at the edge, `13/721` when it is >=12 h inside. The audit's 4/721 (0.55%) sits inside that band. |
| 2 | "Kraken Futures funding is not backfillable; it is inherently forward-only" | **REFUTED** | `GET /derivatives/api/v3/historical-funding-rates?symbol=PF_ETHUSD` is **keyless**, returns **8781 hourly records spanning 366 days** (2025-10-01T08:00Z -> 2026-10-02T12:00Z), and its `fundingRate` is **byte-identical** to the `/tickers` `funding_rate` already in the log. |
| 3 | News "needs an API key for the article source" | **REFUTED** | `ticker-news-signals` is **keyless by design** — GNews reads Google News RSS (`README.md`: "No API keys"). The depth problem is real but is a *result-count* cap, not an auth problem. |
| 4 | F&G 2018+ daily history is fetched on every social pull and discarded | **CONFIRMED, and worse** | Confirmed at `pipeline.py:123,130-144`. But the value fetched for a day is that day's **final** value, so a naive backfill is not merely wasteful — it is **look-ahead bias**. See §3. |

### The one number that reframes the whole candidate

The audit says "8 of 60 observation columns carry no information." That is right but soft. Measured
on the shipped 1-record file, Spearman rank correlation of the exogenous block over 721 bars at
`signal_max_age_hours: 12`:

```
                        fund_rate  basis  open_int  fund_pred   vol24h  observed  spread  age_h
funding_rate              1.000  1.000     1.000       1.000   1.000     1.000   0.963   1.000
basis                     1.000  1.000     1.000       1.000   1.000     1.000   0.963   1.000
open_interest             1.000  1.000     1.000       1.000   1.000     1.000   0.963   1.000
funding_rate_prediction   1.000  1.000     1.000       1.000   1.000     1.000   0.963   1.000
vol24h                    1.000  1.000     1.000       1.000   1.000     1.000   0.963   1.000
signal_observed           1.000  1.000     1.000       1.000   1.000     1.000   0.963   1.000
spread                    0.963  0.963     0.963       0.963   0.963     0.963   1.000   0.963
signal_age_hours          1.000  1.000     1.000       1.000   1.000     1.000   0.963   1.000
```

**Seven of the eight exogenous columns are rank-identical to each other** (rho = 1.000 exactly);
the eighth is rho = 0.963. The exogenous block is not eight numbers that happen to be
uninformative — it is **one bit replicated eight times**. The observation's nominal width is 60; its
*exogenous* rank is 1.

And the shape is not "missing data". Normalised through `NormalizationStats`, each of those eight
columns is a **+7.4 sigma step** lasting 13 bars and then vanishing:

```
funding_rate   zmin=-0.136   zmax=+7.380      (708 of 721 bars are the zero-fill)
basis          zmin=-0.136   zmax=+7.380
signal_age_hours zmin=-0.119 zmax=+12.165
```

The current state is therefore not neutral absence — it is an 8-dimensional, perfectly-collinear
**impulse artifact**. A policy sees a coordinated +7 sigma exogenous shock for half a day,
repeatedly, and learns whatever it learns from that. This makes CAND-2 a *correctness* item, not
a depth nicety.

---

## 2. Q1 — Per channel: exactly what history is recoverable, and by what mechanism

### 2.1 Summary table

| Channel | Config key | Honest classification | Reachable depth | Mechanism | Consumer change? |
|---|---|---|---|---|---|
| **Funding** | `funding_features_file` (**ON**) | **BACKFILLABLE — 366 days, keyless, one call.** Accumulate-only beyond 366 days | 8781 hourly records, 2025-10-01 -> 2026-10-02 | `GET /derivatives/api/v3/historical-funding-rates?symbol=PF_<ASSET>USD` | **None** |
| **News** | `extra_features_file` (off) | **ACCUMULATE-ONLY.** Not an auth problem — a **100-item** result cap | <=100 articles per query per pull | GNews -> Google News RSS, keyless | None |
| **Social / F&G** | `social_features_file` (off) | **BACKFILLABLE — 3162 daily records, 2018-02-01 -> today, keyless, one call** | 8.66 years, daily | `GET https://api.alternative.me/fng/?limit=0` (already implemented, already called, already thrown away) | **None** |
| **Social / StockTwits** | same key | **EFFECTIVELY DEAD from this host.** 30 messages, pagination non-functional | 30 messages ~= **3.9 h** for ETH.X | `api.stocktwits.com/api/2/streams/symbol/ETH.X.json` | None — but the sibling itself is broken (§2.5) |

### 2.2 Funding — the audit's "inherently forward-only" is wrong, and it matters most

The Derivatives v3 REST spec (`https://docs.kraken.com/openapi/futures-rest.yaml`, 45 paths)
contains exactly three historical-data endpoints: `/history`, `/assignmentprogram/history`, and
**`/historical-funding-rates`**. That last one is the one the audit missed. Its path uses
**hyphens**, which is why a casual probe of `/historicalfundingrates` returns
`{"status":"NOT_FOUND"}` and makes the capability look nonexistent.

Measured, keyless, no headers beyond a UA:

```
GET /derivatives/api/v3/historical-funding-rates?symbol=PF_ETHUSD
-> {"result":"success","rates":[ ... 8781 entries ... ]}
   oldest 2025-10-01T08:00:00Z   newest 2026-10-02T12:00:00Z   span 366 days
   each: {timestamp, fundingRate, relativeFundingRate}
```

Depth is per-instrument and consistent across the assets I checked:

| Symbol | Records | Span | Periods/day |
|---|---|---|---|
| `PF_ETHUSD`   | 8781 | 366 d | ~24 |
| `PF_SOLUSD`   | 8782 | 366 d | ~24 |
| `PI_XBTUSD`   | 8782 | 366 d | ~24 |
| `PF_DOGEUSD`  | 8782 | 366 d | ~24 |
| `PF_BTCUSD`   | **HTTP 400** | — | Kraken Futures spells BTC **`PI_XBTUSD`**; `PF_BTCUSD` is not a valid symbol |

Two things to correct in the surrounding prose while we are here:

* It is **hourly**, not 8-hourly settlement. The shipped `default.yaml` comment ("kraken-funding-rates
  settles ~8-hourly, so ... a bound above 1") describes the *live snapshot* cadence. The historical
  series is hourly and does not need the 12-hour bound at all.
* The sibling's own docstring says `funding_rate` is "per interval, typically 8 h"
  (`models.py:26-27`). For the backfilled hourly series that description would be wrong. Whatever
  docstring lands must state the interval the records actually carry.

**`fundingRate` is the same series as `/tickers` `funding_rate`** — proven by exact float equality at
the same hour:

```
/tickers snapshot     @ 2026-10-02T00:00:00Z : funding_rate = 0.02527185133308243
historical-funding    @ 2026-10-02T00:00:00Z : fundingRate  = 0.02527185133308243
```

The backfill is a **drop-in**, not a proxy. This is the strongest form the option could take.

**`relativeFundingRate` is NOT `funding_rate_prediction`.** At the same hour,
`relativeFundingRate = 9.34e-06` versus the snapshot's `fundingRatePrediction = 1.14e-02` — three
orders of magnitude apart and semantically different (an index/interest-normalised rate versus the
next-settlement estimate). Aliasing one onto the other would be a silent substitution.
**Recommendation: map `fundingRate -> funding_rate` only**, and either drop `relativeFundingRate` or
give it its own allow-list entry with its own documented meaning.

**What the backfill does not give you.** `basis`, `open_interest`, `vol24h`, `bid`, `ask`,
`mark_price`, `index_price` have **no** historical endpoint in the v3 REST spec: measured 404 on
`/openinterest` and `/historicalfundingrates`-variants; `/instruments` is spec-only metadata; the
`api/charts/v1` OHLC path returned `{"error":"Invalid market symbol"}` for every spelling I tried.
Those five remain **accumulate-only** and must still come from the hourly timer. They are also the
columns that keep the `microstructure` group alive, so **the timer is the complement of the
backfill, not a substitute for it.**

**Forward-compatibility caveat.** 366 days comfortably covers a 721-bar hourly frame (30 days) but
**not** a multi-year one. If CAND-3 lifts the ~721-bar ceiling to years of seeded OHLCV, funding
coverage starts falling again unless a second source is added. This coupling should be recorded
wherever the two candidates are discussed.

### 2.3 The effect, measured end-to-end through the real seam

I wrote the live backfill to a scratch file and merged it through `merge_extra_features` +
`FeaturePipeline` at the shipped settings:

| Scenario | Coverage | Obs. width | Distinct raw values in window | z-range of exogenous block |
|---|---|---|---|---|
| **A.** shipped file (1 record) | **13/721 = 1.80%** | 60 | `funding_rate` = **1** | -0.14 .. **+7.38** (all 8 cols identical) |
| **B.** funding backfill, `funding_rate` only | **721/721 = 100.00%** | 55 | `funding_rate` = **721** | -2.55 .. +4.23 |

Further facts about B:

* `funding_rate` takes **720 distinct values over 721 bars** — genuinely hourly data, not a held
  constant.
* Lag-1 autocorrelation **0.7496** — a persistent series with real structure, not noise.
* The z-range collapses from a +7 sigma step to a well-behaved -2.5 .. +4.2 distribution.
* `signal_observed` becomes **constant 1.0**, which `NormalizationStats` correctly z-scores to
  `0.000`. The freshness column *correctly carries no information* once coverage is complete. Worth
  stating explicitly, because it means the fix does not spend observation budget on a flag.

**The honest trade:** width falls 60 -> 55, because `basis` / `open_interest` / `vol24h` / `spread`
drop out when the file does not carry them. Five columns of noise are exchanged for one column of
signal — the right trade. It is also reversible: because the seam de-duplicates per floored hour
**keeping the last write** (`data.py:594-599`), a backfill file can simply be **concatenated ahead
of** the timer's appended live records, and the live values win on overlapping hours. Backfill +
timer together give `funding_rate` at 366 days of depth and the other five accumulating forward
from install day.

### 2.4 News — keyless, but capped at 100 items per query

`ticker-news-signals` is keyless by design (GNews reads Google News RSS; `README.md`: "No API keys.
No long-running service."). The audit's "needs an API key" is wrong. The real constraint is a
result cap:

```
https://news.google.com/rss/search?q=ethereum+when:<W>&hl=en-US&gl=US&ceid=US:en
  when:1d -> 100 items, newest 2026-10-02 11:29, oldest 2026-10-02 05:07   (6.4 h)
  when:7d -> 100 items, newest 2026-10-01 19:02, oldest 2026-10-01 00:24   (18.6 h)
  when:1m ->   0 items                                                       (empty response)
  when:1y -> 100 items, newest 2026-08-31 07:00, oldest 2026-06-05 07:00   (87 days)
```

**Hard cap: 100 items, always** — and for `when:1y` the window is neither the most recent 100 nor
the oldest 100: it is 87 days ending *three months ago*. Google's sampling is opaque and not
ordered. Consequences:

* **`extra_features_file` cannot be backfilled.**
* Depth per pull is ~100 articles. The sibling issues 2 queries (keyword + `DIGITAL_CURRENCIES`
  topic), so <=200 pre-dedup.
* The sibling's own `--lookback-hours` default is **1** (`cli.py:54`), not 24.
* Scoring is VADER on `html.unescape(article.title)` only (`pipeline.py::_headline`) — article
  bodies are never fetched. Legitimate and cheap, but it means the column is "how the headline
  reads", and it should not be described downstream as article sentiment.

**Classification: accumulate-only, and honestly thin.** Not worth enabling for depth. Worth enabling
only if someone wants the forward signal and accepts that history must be built.

### 2.5 Social — F&G is the deep prize; StockTwits is measurably broken

**Fear & Greed — measured, keyless, 2018+, one call:**

```
GET https://api.alternative.me/fng/?limit=0
-> 3162 records, 2018-02-01 -> 2026-10-02 (8.66 years), one per UTC day
   value range 5-95
   classifications: Fear 909, Extreme Fear 739, Neutral 401, Greed 831, Extreme Greed 282
   metadata.error = null      (no key, no quota, no auth of any kind)
```

The audit's headline is right: **the full 2018+ daily history is already downloaded on every social
pull and thrown away.** The mechanism is exactly as described — `pipeline.py:123` fetches it, then
`:130-144` emits records **only for hours present in the StockTwits `buckets`** inside
`--lookback-hours`, using `_fng_by_day` as a per-day *lookup*.

Three qualifications the audit does not carry:

1. **It is a Bitcoin index.** alternative.me's F&G is BTC-centric; the API exposes no per-coin F&G.
   Applying it to an ETH/USD model is a **cross-asset proxy** — the same class of caveat the audit
   raises for the Binance-USDT OHLCV seeder (G3), and it deserves the same scrutiny.
2. **The daily value is final-at-fetch.** I polled `?limit=1` 19 times over ~9 minutes: today's
   value held at 72 while `time_until_update` counted down monotonically (40017 -> 39472 s). The
   day's record is a single live-updating number, refreshed until midnight; past days are frozen. So
   the number attached to day *D* is day *D*'s **end-of-day** value. See §3 for the look-ahead
   consequence.
3. **It is slow.** One value per day; in a 30-day window it took **15 distinct readings**.

**StockTwits — measured broken, twice, on this host.**

*Defect A — the client's User-Agent loses the Cloudflare gate.* The sibling sends
`Mozilla/5.0 (compatible; kraken-social-signals/0.1.0)` (`client.py:37-39`):

```
that UA         -> HTTP 403, 5609 bytes, "<title>Just a moment...</title>"  (Cloudflare challenge)
full Chrome UA  -> HTTP 200, 125427 bytes, valid JSON
```

Because `_get` treats 403 as "retryable, non-fatal" and returns no data, the channel **silently
produces nothing** on this host for most requests. It is intermittent — I got a 200 on `ETH.X` and
a 403 on `BTC.X` minutes apart with identical headers — but the shipped default loses.

*Defect B — pagination is written against a cursor key the API does not return.* The client loops
`nxt = cursor_info.get("after")` (`client.py:243`) but the live response carries
`{"more": true, "since": 665613006, "max": 665602296}` — **no `after` key**. So `nxt is None` and
the walk breaks after page one. And the `cursor` **query parameter is ignored** anyway:

```
page1  30 messages  oldest 2026-10-02 08:57:28  cursor {more:True, since:665613006, max:665602296}
page2  30 messages  oldest 2026-10-02 08:57:28  cursor {more:True, since:665613006, max:665602296}  <- identical
page3  30 messages  oldest 2026-10-02 08:57:28  cursor {more:True, since:665613006, max:665602296}  <- identical
```

**Hard ceiling: 30 messages, ~3.9 h, for ETH.X.** Of those 30, 16 carried a `Bullish`/`Bearish` tag
(10 bullish / 6 bearish). The `--lookback-hours 24` default is unreachable in a single call. This
confirms the audit's "no credible deep history" and goes further: the sibling cannot currently
paginate at all, and its default UA loses the bot gate more often than not.

`kraken-social-signals` has **one commit in its entire history** (2026-09-28, a scaffold). That is
consistent with what I found, and it belongs in any cost/benefit judgement about investing in its
`stt_*` columns.

**Practical conclusion:** enabling `social_features_file` *without* fixing the sibling is a trap. If
StockTwits 403s, `buckets` is empty, `pipeline.py:132-141` emits **zero** records, and the file is
either absent (-> `SignalFileNotFoundError`, loud) or empty (-> also a refusal, loud). You get no
F&G either. **The F&G backfill must be a code path that does not depend on StockTwits having
succeeded.**

---

## 3. Q2 — Sizing the F&G backfill precisely

### (a) The minimal change to the sibling

The minimal change is **one branch that emits from `fng_by_day` instead of from `buckets`**, plus a
CLI verb to reach it. Concretely:

1. Add a producer that iterates `client.fetch_fear_greed(limit=0)` and yields one record per day,
   **independent of whether StockTwits returned anything**. It must not live inside the current
   loop, which is bucket-driven — that is precisely the coupling that discards the history today.
2. Emit at **hourly** resolution (see (b)) so it lands in the seam unchanged: the seam already floors
   both sides to the hour and forward-fills on a bounded age, so an hourly-stamped record needs **no
   consumer change at all**.
3. Expose it as a verb (`backfill-fng --since <iso> --output <path>`) or a `--fng-only` flag on
   `pull`, writing `{"ticker", "timestamp", "fng_index"}` — the exact shape `_SIGNAL_COLUMNS` already
   allows and the exact shape `SocialRecord.to_dict()` already emits.

Sizes measured: 3162 daily records -> **256 KB** daily-stamped, **6.1 MB** hourly-expanded (8760
rows/year, 75888 rows for 2018+). Both trivial.

**House style.** The sibling already mirrors `kraken-python` closely — `client.py` / `models.py` /
`export.py` / `pipeline.py` / `cli.py` / `errors.py` / `logging_config.py`, an `EXTRACTORS`
registry, `write_jsonl`, `scripts/verify_live.py`, offline tests. A F&G backfill needs **no new
architectural idea**: a second registry entry and a second CLI verb. That is the whole of "cheaply
reduces to what the pipeline consumes" for this item.

### (b) Daily stamps versus hourly stamps — and why daily stamps are *worse than useless*

Measured through the real seam at the shipped `signal_max_age_hours: 12`, 721 bars:

| Variant | Records | Coverage | Distinct `fng_index` values | z-range |
|---|---|---|---|---|
| daily-stamped | 3162 (30 in window) | **390/721 = 54.09%** | **15** | -1.07 .. +1.21 |
| hourly-expanded | 75888 (720 in window) | **721/721 = 100.00%** | **15** | -2.48 .. +1.45 |

Two findings, and they point the same way:

* **Hourly expansion buys coverage, not information.** Distinct raw values are **15 in both
  cases**. The z-range widening (-1.07..+1.21 -> -2.48..+1.45) is a pure artifact of the
  normalization baseline moving, because a constant run over the whole frame changes the fitted std.
  It is not new signal.
* **Daily stamps produce a bogus 54% coverage and a sawtooth freshness pair.** A daily source read
  at a 12-hour bound is live for 13 of every 24 hours — hence 390/721 ~= 13/24. The seam correctly
  marks the other 11 hours `signal_observed=False`, which is **wrong about the data**: the value is
  perfectly well known for the whole day. You would ship a permanently half-observed channel whose
  `signal_age_hours` ramps 0->12 and snaps back to -1 once a day.

**So: emit hourly-expanded.** Not because it adds information — it demonstrably does not — but
because it stops the freshness semantics from lying. A reader must not mistake this for "F&G at
hourly resolution".

### (c) What it buys the observation — honestly

* **It is a daily macro regime variable, not a signal.** ~0.5 changes/day. In an hourly frame it
  contributes one observation column carrying one daily step. If it helps at all, it helps by letting
  the policy condition on a slow risk-appetite state, not by timing anything.
* **It is BTC, applied to ETH.** Defensible for a BTC/USD model. For ETH/USD it is a cross-asset
  proxy whose correlation to ETH sentiment is unmeasured and unmodelled. Nothing in `_SIGNAL_COLUMNS`
  or the merge seam carries asset provenance beyond `ticker`, so the observation cannot express "this
  column is a proxy for another asset".
* **Look-ahead, precisely stated.** The API returns one value per UTC day, stamped 00:00Z, and that
  value is the day's final number. So a record stamped `2026-10-02T00:00:00Z` carrying the value for
  2026-10-02 asserts a number that was not knowable at 00:00 on 2026-10-02 — up to **24 h of
  look-ahead**. Hourly-expanding it *maximises* that exposure (24 records all asserting the same
  future-informed value) rather than minimising it.
  **The fix, if F&G is backfilled, is to stamp each day's value at the *start of the following*
  day** (`D+1 00:00Z`), or equivalently to lag the merge by one day. That is a one-line producer
  change and it converts the column from a bias source into a legitimate slow feature.
  This is not in the audit and it matters more than the coverage question: a look-ahead bias
  *inflates* results, so it will not be caught by "the numbers looked reasonable".

---

## 4. Q3 — The other three blame targets, ranked

### Rank 1 — `nix/module.nix` declares zero timers and zero flake inputs for three of five siblings

This is the root cause of the 1.80% coverage, and it is the cheapest to fix. Measured:

* `nix/module.nix` is 240 lines and contains **exactly one** systemd unit:
  `systemd.services."kraken-trading-bot-env"` at `:220` with `wantedBy = ["multi-user.target"]`.
  **Zero timers.** It is a NixOS *system* module, so `systemd.user.*` is not in its option
  namespace — it **structurally cannot** carry these units. The `.service.in` comment already
  concedes this.
* `flake.nix` declares flake inputs for **`kraken-python` and `kraken-market-data` only**. The
  three signal siblings — `kraken-funding-rates`, `ticker-news-signals`, `kraken-social-signals` —
  have **no flake input at all**. Consequence: the shipped unit's `ExecStart` is
  `/run/current-system/sw/bin/nix run /home/seanc/Projects/kraken-funding-rates#...` — an
  **absolute host path**, not a locked flake ref. It is not hermetic and it is not portable.
* On this host: `systemctl --user list-timers --all` lists **6 timers**, none of them
  `kraken-trading-bot-funding.timer`. And `signals/eth_usd_funding.jsonl` holds **one** record.

**Verdict: fix.** Concretely — add flake inputs for the three signal siblings and change `ExecStart`
to the locked refs (the `.service.in` already has the right shape; only the ref is wrong), then run
`just funding-timer`. Even with no backfill at all, an hourly timer takes coverage from 1.80% to
**100%** within a day, because the channel is hourly and the bound is 12 h. This is the single
highest ratio of coverage gained to lines changed in the entire candidate.

**One incidental defect found while reading the units:** the shipped `systemd/kraken-trading-bot-funding.service`
has a corrupted header comment — the sentence describing the template was overwritten by the sed
substitutions, so line 2 now reads
`# Template for the funding-pull user unit.  Installed (with ETH/USD//home/seanc/...jsonl`.
Harmless at runtime; it makes the file look machine-generated in a way it is not. Trivial to repair.

### Rank 2 — `signal_observed` is written but read by nothing, and is *structurally unable* to express per-channel failure

The audit is right that `signal_observed` has no consumer. The mechanism it does not name is the
sharper problem: **`_combine_freshness` (`data.py:779-809`) combines the three channels with a
logical OR and a `maximum`**:

```
df["signal_observed"]  = previous_observed | new_observed
df["signal_age_hours"] = maximum(previous_age, new_age)
```

So the pair answers *"did **any** channel report"*. With three channels of wildly different
coverage, one dead channel is invisible the moment any other is healthy — and with only the funding
channel configured today, the pair is a **funding-only coverage flag wearing a channel-neutral
name**. There is no per-channel coverage anywhere.

And there is no consumer at all downstream: `BacktestResult.to_dict()` (`backtest.py:174-196`)
emits 18 fields and **none of them is a coverage number**. `assess_cell`
(`tools/model_matrix.py:786-878`) has reason codes for `n_bars`, `zero_trades`, `nan_metric`,
`config_not_found`, `action_space_mismatch`, `no_tradable_bar`, `signal_file_not_found` — and
**no coverage code**. So the coverage ratio is not merely unused: it is *unreachable*, because it is
never written to the JSON the harness reads.

**Verdict: fix, in two cheap steps.**

*Step 1 — the zero-overlap case costs almost nothing.* `classify_process_failure` already parses
`record["stderr_tail"]` for substrings. The seam already emits, at `data.py:662-671`:

```
"No %s signal record overlaps the %d-bar read window from %s — every %s value is
 zero-filled with signal_observed=False (widen signal_max_age_hours if the records are simply older)"
```

A reason code keyed on `"signal record overlaps"` is a **four-line change** to
`classify_process_failure` and it catches the 0% case in every existing run with no RL-side change
at all. This is the highest value-per-line item in the whole candidate.

*Step 2 — the partial case needs one field.* Add `signal_coverage: float` (the `signal_observed`
mean over the replayed frame) to `BacktestResult` and `to_dict()`, add it to `NULLABLE_BACKTEST_FIELDS`'s
neighbourhood so a pre-provenance artifact is UNKNOWN rather than 0, and add
`signal_coverage:{x}<{floor}` to `assess_cell` beside the `n_bars` check — reusing the pattern that
already works there.

*Per-channel coverage is the honest version of this.* Three columns (`signal_coverage_funding`,
`..._news`, `..._social`) or one JSON object in `BacktestResult` would survive the OR-combination
problem; a single merged scalar cannot.

### Rank 3 — Non-overlap is WARNING-not-refusal (`data.py:662-671`)

The audit calls this deliberate and quotes `data.py:489-491` ("the expected state of a young
forward-only log"). I agree with the *reasoning* and disagree with the *consequence*, and the
resolution is not "make it raise".

**A refusal would be wrong.** Raising on low coverage would make a legitimately young forward-only
log unusable — which is the case the comment is defending. It would also fire on every run before
the first hourly pull has happened, turning a cold start into a hard failure. Coverage is a
**continuum**, and refusals are for **states**; "your file has one record" is a state, "your file
covered 1.8% of the window" is not.

**The right fix is a configurable coverage floor with two severities.** Mirror the existing
`min_bar_ratio` pattern in `assess_cell` rather than inventing one:

| Coverage | Severity | Rationale |
|---|---|---|
| `== 0` (no overlap) | **WARNING → keep**, but *also* emit it in a machine-readable form so Rank 2 Step 1 can classify it | A cold start is legitimately expected; a matrix cell that scores well on a channel that reported nothing is not. Refusing would break cold start for no gain. |
| `0 < coverage < floor` | **WARNING, named with the number** | The state is transient and self-healing once a timer runs. Failing hard would make an hourly restart an outage. |
| `coverage >= floor` | silent | Normal. |

**What floor?** Not a constant. It should derive from the channel's own cadence and the bar
interval — the same reason `signal_max_age_hours` exists. Concretely: `floor = min(1.0, bar_hours /
cadence_hours)` adjusted by how long the log has existed:

* A **backfilled** channel (funding 366 d, F&G 2018+) should sit at **≥0.95** — anything less means
  the backfill did not run or the frame outran the history. This is where a floor earns its keep: it
  is the guard that stops CAND-3 (a multi-year OHLCV frame) silently starving a 366-day channel.
* An **accumulate-only** channel on a young log should be floored by **log age**, not by a constant:
  `observed_window_hours / log_age_hours`, so a 3-hour-old log is not asked for 95% coverage. Until
  the log is at least one full `signal_max_age_hours` old, coverage cannot be judged at all and the
  honest answer is "too early to say".

That second formula is the one I would ship. A constant floor would either be uselessly low (and
catch nothing) or break every cold start.

---

## 5. Q4 — Is there a NEW exogenous source worth adding here?

**The case for adding one.** Two of the four channels are structurally dead — StockTwits is capped
at 30 messages with broken pagination, news at 100 articles with an opaque sampler. Even after the
backfills, the observation would carry **funding rate + F&G**: one hourly crypto-derivatives series
and one daily BTC macro series. That is thin for an exogenous block. Keyless sources that emit
scalars directly do exist and I verified two.

**Measured candidates:**

* **Deribit `public/get_funding_rate_history`** — keyless, hourly, and I confirmed
  `ETH-PERPETUAL` returns 24 rows for 2020-01-01, 0 rows for 2019-01-01 and 24 rows for
  **2019-07-01**: so **~7.2 years of hourly funding**, far deeper than Kraken's 366 days. Each row
  carries `{timestamp, index_price, interest_8h, interest_1h, prev_index_price}` — and
  `index_price` + `prev_index_price` means a **basis series is derivable**, which Kraken does not
  offer historically at all. This would fill the single biggest hole in the backfill (§2.2: `basis`
  is accumulate-only today).
* **Binance `fapi/v1/fundingRate`** — keyless, 500 rows per call (returns 500 even when asked for
  1000), 8-hourly, pageable via `startTime`; rows carry `fundingRate`, `markPrice`, `fundingTime`.
  **Binance `futures/data/openInterestHist`** — keyless, hourly `sumOpenInterest` +
  `sumOpenInterestValue`, confirmed live for ETHUSDT. That would fill `open_interest` historically.
  I did **not** establish the OI history depth: a 2020-01-01 `startTime` window returned HTTP 400, so
  the endpoint may be bounded to a recent window. **Depth unknown — must be measured before anyone
  builds on it.**

**The case against, and it is stronger.** Three reasons, in order of weight:

1. **Cross-venue contamination.** Deribit funding is not Kraken funding. Binance OI is not Kraken
   OI. The audit has *already* flagged exactly this hazard for the OHLCV seeder ("Kraken-style
   ticker -> Binance \*USDT spot symbol", a basis the pipeline never models) and correctly refused to
   let CAND-3 proceed without confronting it. Adding a second venue's derivatives series to the
   exogenous block repeats that hazard in a place with **no cross-venue check at all**, and where
   the affected columns are the ones a leverage-driven policy would lean on hardest. Funding rates
   are precisely where venues most diverge: Deribit and Binance perps have their own OI and basis
   regimes, and a Kraken-traded ETH/USD bar paired with Deribit funding is a mismatched pair by
   construction.
2. **It does not fix the actual defect.** The channel is at 1.80% coverage because **nothing is
   scheduled**, not because there is a shortage of sources. The audit's own framing — "the cheapest
   real win in the audit" — is right, and adding a fifth source before the timer exists means
   shipping a fifth thing that produces nothing.
3. **Deribit/Binance are not lower-maintenance than what we have.** Both are undocumented-shape,
   both have changed endpoints before, neither is a repo we control, and a new sibling for each is a
   new licence-free scaffold with its own flake input and timer. Compare: one `backfill` verb in an
   existing sibling, or a **one-line** `ExecStart` ref change.

**Verdict.** This pass should be **strictly about making what exists produce**. The ordering that
falls out of that:

1. Install/fix the timer (Rank 1). Coverage 1.80% -> 100% on the funding channel alone, in a day.
2. Backfill funding from the **keyless Kraken endpoint** (366 d, drop-in, no cross-venue question —
   it is the *same venue* as the bars, which is exactly why it beats Deribit despite being shallower).
3. Backfill F&G hourly-expanded, **lagged one day** to kill the look-ahead (§3c).
4. Fix the two coverage blind spots (Rank 2) so all three steps above are *visible*.
5. Only then, and only as a separate decision with the cross-venue basis question answered first,
   consider Deribit/Binance for `basis` and `open_interest`.

Step 5 is genuinely open — Deribit's ~7.2 years of funding with a derivable basis is a real prize
and 366 days will not survive CAND-3. But it is a *different candidate* with a *different*
prerequisite, not part of CAND-2.

---

## 6. Q5 — Candidate metadata

| Source | Maintenance / last activity | Licence | Auth | Rate limits | Output shape | Reduces to a scalar per (ticker, hour)? |
|---|---|---|---|---|---|---|
| **Kraken Futures `/historical-funding-rates`** | Kraken production API, documented in the official OpenAPI spec (`docs.kraken.com/openapi/futures-rest.yaml`, `operationId: historicalFundingRates`) | Kraken ToS (public data) | **Keyless** | Derivatives REST uses a **cost-per-request budget, 500 per 10 s** (per Kraken docs index). One 8781-row call is one request. | `{"rates":[{timestamp, fundingRate, relativeFundingRate}]}` | **YES — exact.** `fundingRate` -> `funding_rate`, hour-stamped |
| **alternative.me `/fng/?limit=0`** | Live, `metadata.error = null`, no deprecation notice, no version header | alternative.me ToS | **Keyless** | None published, none observed (19 polls / 9 min, no throttle) | `{"data":[{value, value_classification, timestamp, time_until_update}], "metadata":{error}}` | **YES** — `value` -> `fng_index`; needs hourly expansion + 1-day lag |
| **Google News RSS (via GNews)** | Live; opaque sampler, **hard 100-item cap** measured across `when:1d/7d/1m/1y` | Google News ToS; `gnews` is MIT | **Keyless** | No published limit; heavy polling risks 429 (gnews retries internally) | RSS 2.0 -> ~100 `<item>` | Partially — needs per-ticker keyword search; capped at 100 |
| **StockTwits v2 `/streams/symbol`** | **Broken on this host** — Cloudflare 403 on the shipped UA; `cursor` param ignored; cursor key mismatch | StockTwits ToS | **Keyless** (optional `STOCKTWITS_ACCESS_TOKEN` for headroom) | Undocumented; client docstring cites "the StockTwits rate limit" | 30 messages, `cursor.more/since/max`, no working pagination | No — 30 messages, ~3.9 h, non-paginating |
| **Deribit `public/get_funding_rate_history`** | Live, JSON-RPC, keyless; ~7.2 y hourly for `ETH-PERPETUAL` (measured) | Deribit ToS | **Keyless** | Undocumented rate limit; observed generous | `{timestamp, index_price, interest_8h, interest_1h, prev_index_price}` | **YES** — but **cross-venue** |
| **Binance `fapi/v1/fundingRate` / `futures/data/openInterestHist`** | Live, keyless; funding 500 rows/call confirmed; **OI history depth unverified** (2020 window → HTTP 400) | Binance ToS | **Keyless** | `fapi` weight-based (2400/min typical) | `{fundingRate, markPrice, fundingTime}`; `{sumOpenInterest, sumOpenInterestValue, timestamp}` | **YES** — but **cross-venue** |

### Sibling project state (all local, none published)

| Repo | Commits | Last commit | Version | Licence |
|---|---|---|---|---|
| `kraken-python` (the reference) | 7 | 2026-09-26 | 0.4.0 | **no LICENSE file** |
| `kraken-funding-rates` | 3 | 2026-10-02 | 0.1.0 | **no LICENSE file** |
| `kraken-social-signals` | **1** | 2026-09-28 | 0.1.0 | **no LICENSE file** |
| `ticker-news-signals` | 4 | 2026-09-27 | 0.1.0 | **no LICENSE file** |
| `kraken-market-data` | 2 | 2026-09-28 | 0.1.0 | **no LICENSE file** |

Two observations worth carrying: **no sibling has a licence file**, so "mirror the house style"
has no licence precedent to follow and none to violate — and `kraken-social-signals` being a
single-commit scaffold is consistent with the two live defects found in §2.5.

---

## 7. Recommended order, with the measured payoff of each step

| # | Action | Where | Consumer change? | Coverage effect (measured / projected) |
|---|---|---|---|---|
| 1 | Fix `ExecStart` to a locked flake ref; add flake inputs for the 3 signal siblings; run `just funding-timer` | `flake.nix`, `systemd/*.service.in`, operator | none | 1.80% -> **100%** within one day |
| 2 | Add a `backfill` verb calling `historical-funding-rates`; concat **ahead** of the timer's output | `kraken-funding-rates` | none | extends `funding_rate` to **366 days** |
| 3 | Add a `backfill-fng` verb emitting hourly, **lagged to `D+1 00:00Z`** | `kraken-social-signals` | none | adds `fng_index` at **100%** over **8.66 years** |
| 4 | Add a `signal_coverage` field + `assess_cell` reason code | `backtest.py`, `tools/model_matrix.py` | additive | makes 1-3 **verifiable** |
| 5 | Add a `signal_coverage:<x>` reason code keyed on the existing stderr WARNING | `classify_process_failure` | none | catches the 0% case in **existing** runs |
| 6 | Coverage floor derived from log age, WARNING-only | `data.py` | none | closes the silent-gap class |
| 7 | Repair the corrupted header in `systemd/kraken-trading-bot-funding.service` | `systemd/` | none | n/a |
| — | **Deribit / Binance for `basis` + `open_interest`** | *separate candidate* | none | ~7.2 y, but **cross-venue** — needs the G3 basis question answered first |

Steps 1-3 need **no change to the RL consumer at all** — which is what makes this the cheapest real
win in the audit, exactly as the audit said. The audit was right about the mechanism and wrong about
the ceiling: the recoverable history is not "F&G alone", it is **366 days of on-venue hourly funding
plus 8.66 years of daily macro**, and the biggest single lever is a `just funding-timer` that has
never been run.

---

## 8. Reproduction

All commands are read-only against public endpoints; scripts live in `/tmp/opencode/`.

```bash
# 1. 366 days of keyless hourly funding
curl -s 'https://futures.kraken.com/derivatives/api/v3/historical-funding-rates?symbol=PF_ETHUSD'

# 2. the 2018+ daily F&G history the sibling already fetches and discards
curl -s 'https://api.alternative.me/fng/?limit=0'          # -> 3162 records

# 3. StockTwits: the shipped UA loses, a browser UA wins; cursor is ignored
curl -s -A 'Mozilla/5.0 (compatible; kraken-social-signals/0.1.0)' \
     'https://api.stocktwits.com/api/2/streams/symbol/ETH.X.json'   # 403 Cloudflare

# 4. Google News RSS: exactly 100 items whatever `when:` says
curl -s 'https://news.google.com/rss/search?q=ethereum+when:1y&hl=en-US&gl=US&ceid=US:en'

# 5. the consumer side is green (21 passed)
cd ~/Projects/kraken-trading-bot
nix develop --command bash -c "python -m pytest tests/test_rl_signal_config_wiring.py -q"

# 6. coverage / rank-correlation / z-profile measurement (real seam, 721 synthetic hourly bars,
#    signal_max_age_hours=12): scripts in /tmp/opencode/measure_cov.py .. measure5.py
```

*Caveat on measurement 1:* all frame figures come from a synthetic price frame, so they are
properties of the **join** (coverage, rank correlation, z-profile), which is what is under test.
They are not claims about how a trained model would perform. No trained artifact exists in `models/`
(0 files), so there is no empirical baseline for any of this — as the audit itself noted.

---

*Nothing was written, committed or deleted outside this file. No code was changed; no config was
edited; no timer was installed.*
