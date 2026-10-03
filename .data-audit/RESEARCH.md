# RESEARCH.md — pass 2026-10-03 (assembled by the lead)

Synthesis of `RESEARCH-1.md` (987 lines, G-C), `RESEARCH-2.md` (762 lines, G-A+G-B) and
`RESEARCH-3.md` (1058 lines, G-D+G-E+G-F). Those three files are the evidence; this is the
decision-facing digest. Evidence tags are theirs: **[M]** measured, **[C]** cited to a
primary source or `file:line`, **[I]** inferred, **[unverified]** explicitly not checked.

**Bottom line up front: the audit's framing survives on ranking but not on mechanism.** Three
of the six gaps are not what AUDIT.md described, and in two cases shipping the audit's
prescribed fix would have caused a silent data regression. The corrections are in §1; they are
load-bearing, not editorial.

---

## 1. Corrections to AUDIT.md — READ THESE BEFORE THE DECISION

### 1.1 The audit's #3 gap (G-C) is not an activation gap. Both producers TRUNCATE. [M]

`AUDIT.md` ranked G-C "highest-directness of anything still on the table" on the reasoning
that the file format is already accepted verbatim and *only the schedule is missing*.

**Both producers open their output with `path.open("w")`** — `ticker_news_signals/export.py:123`
and `kraken_social_signals/export.py:149` — and neither CLI has `--append`. `kraken-funding-rates`
does. Proven with a marker line:

| Producer | Cadence | Result |
|---|---|---|
| news | hourly, default `--lookback-hours 1` | file rewritten to ONLY the current hour; history destroyed continuously. ~1–2 h coverage over a 721-bar frame. |
| social | default `--lookback-hours 24` | sliding 24 h window, ~9 lines, never accumulates. ~94% of a 30-day frame unobserved. |

**So "add a timer" is not a neutral no-op — it is data destruction on a timer.** G-C requires
producer-side work (append semantics) in two sibling repos before any scheduler is meaningful.
That is the `NEW-DATA-SOURCE` tail, and it is real.

### 1.2 `ticker-news-signals` does not build through `nix run`. [M]

`nix/gnews.nix:19` sets `format="setuptools"`; gnews 0.8.2's sdist ships
`gnews.egg-info/requires.txt` but **no `requirements.txt`**, while `setup.py:3` does
`open('requirements.txt')` → `FileNotFoundError`. Upstream packaging breakage, one-line fix in
the sibling (build the wheel).

The funding-rates production pattern (`nix run ~/Projects/<repo>#<binary>`) therefore **does not
transfer**. Interim that works: the sibling's own `.venv`. `kraken-social-signals`' `nix run`
works fine.

### 1.3 Two of G-C's six columns are dead on arrival. Honest headline: **+6 width, +4 usable.** [M]

- **`stt_tilt` — permanently dead.** Live StockTwits v2: **0 of 30 messages carry a `sentiment`
  key**; `entities.sentiment` is `null`. `from_api` reads `raw["sentiment"]["basic"]`
  (`models.py:47-49`) → `""` → tilt always `0.0`. No schedule fixes this.
- **`novelty_flag` — dead at hourly cadence.** `NOVELTY_WINDOW = 15 min` (`pipeline.py:39`) vs an
  hourly sampler. Measured 0/24 nonzero.

Harmless in practice: `features.py:455-457` `safe_std = std if std > 1e-12 else 1.0` normalises
a zero-variance column to exactly `0.0`, so no inf/NaN reaches the observation.

### 1.4 My §7 Q1 premise was wrong: `signals/` is gitignored on purpose. [M]

The audit asked "should the backfilled funding file be committed at all?". It framed the
un-committed file as an oversight. It is deliberate: `git ls-files signals/` is empty,
`git log --all --diff-filter=A -- 'signals/*'` is empty, and `.gitignore:65` ignores `signals/`
— consistently for signals *and* models.

**Recommendation: keep `signals/` gitignored.** An hourly append-only log that ages out is the
wrong shape for git; a committed snapshot buys a fixed window then lies (the ~366 d funding
window is recomputed per call). **Consequence to plan for: a fresh clone has no `signals/` dir,
so the already-non-null `funding_features_file` raises `SignalFileNotFoundError` there** — worth
knowing before adding two more non-null keys.

### 1.5 G-A: `since` is ALREADY the live-fetch cursor. The obvious fix silently breaks the fetch. [M]

`data.py:1385` calls `_page_candles(pair, interval, pages, source, since)`. Passing `since=` at
the call sites converts "fetch the trailing N pages" into **"page forward from `since` for N
pages"** — on a deep store that fetches ancient bars and never reaches the recent tail. The store
stops advancing and the leg trades stale bars. The only thing limiting the damage is `upsert`
deduping on `time` (`store.py:239`), which is luck, not design.

**Any G-A implementation must split the fetch cursor from the read window.**

Also new, unflagged by the audit: **on the live leg `until=` is silently ignored** —
`fetch_ohlc_dataframe` has no such parameter (`data.py:1136-1152`, `:1326-1335`). **[C]**

And G-A's "reads the whole store" is **half wrong**: `store.read` already prunes by month
*filename* from `since`/`until` (`store.py:168-171`, `utils.py:94-109`). The remaining waste is
whole-file reads inside a matched month. **[C]**

### 1.6 G-B(ii) is a NETWORK problem, not a compute problem — and the z-score hazard is absent. [M]

The audit said paper-trade "rebuilds the entire feature matrix and re-z-scores it" per tick.

**It does not refit.** `paper_trade.py:196-197` loads stats from the artifact via
`pipeline.load_normalization`; `environment.py:189-190` fits only `if stats_for(...) is None`;
`paper_trade.py:337` normalises with those frozen stats. The "a live leg that refits per tick is
not the same model" hazard is **already absent** — any rewrite must *preserve* that, not fix it.

And the per-tick compute is not the problem either. Measured on a synthetic frame (pandas
3.0.4), `compute()` cost is **flat in frame length**: 26.13 / 26.25 / 25.84 / 25.02 ms at
1440/800/400/200 rows. An incremental recompute buys ~1 ms of a 60 s tick — **0.04%**.

**All of the waste is the two HTTP page calls per minute.** So "append + tail read of the
feature matrix" (the audit's phrasing, and `data.py:1319-1323`'s own `.. todo::`) is
**not warranted**. The fix is a network/caching fix.

### 1.7 G-E's upstream half is a mis-classification bug, not a missing-retry bug. [C]/[M]

`kraken_api/transport.py:45-48` matches only `"EAPI:Rate limit exceeded"` and
`"EGeneral:Too many requests"`. Kraken's documented **concurrency** throttle is
`EService: Throttled: [UNIX timestamp]` — matches neither, arrives as `APIError`. A wrapper
keyed on `RateLimitError` misses the second documented rate-limit path.

Two more corrections:
- My brief's `EAPI:RateLimitExceeded` is the **wrong spelling** for v1 (verified by string
  test: `APIError`, not `RateLimitError`).
- Kraken's published OpenAPI (`spot-rest.yaml`, 652 KB, fetched) contains **no 429, no
  `Retry-After`, no rate-limit string**. Kraken signals rate limits **in the JSON body on HTTP
  200**, so `status_forcelist=[429]` is the wrong instrument — match on body strings.

### 1.8 G-D's blast radius is bigger than the audit implies: a REST poller cannot carry it. [M]

Live-measured ETH/USD top-10 imbalance autocorrelation: **+0.51 at a 4 s lag, +0.01 at a 16 s
lag** (20 snapshots, 4 s apart, 77.5 s span; sd 0.390). Bar-mean estimator standard error:
**0.390 at 1 poll/bar vs 0.050 at 60 polls/bar (7.8×)**. The bar is 3600 s; the signal
decorrelates in tens of seconds. Contango (arXiv 1011.6402 p.22) measures its 65% R² against
**10-second** mid-price changes — 360× shorter than this repo's bar.

**G-D needs a long-lived WebSocket subscriber**, which is a deployment decision the repo does not
currently accommodate (`nix/module.nix` installs no unit; `configs/default.yaml:75-78`). A REST
poller fits the existing `just funding-timer` shape unchanged — which is precisely the mismatch.

### 1.9 `order_book_imbalance` is a warm-up-bearing column today, not an exogenous one. [M]

Runtime-monkeypatched measurement (no file edited): widening `_SIGNAL_COLUMNS` costs **exactly
+1 observation column** (+0 with `microstructure` off), but on a **sparse** producer
`first_tradable_index` moves **24 → 54** on a 200-bar frame (176 → 146 tradable bars).
Cause also measured: excluding the column from the warm-up test restores 24.
`POINT_IN_TIME_EXOGENOUS_COLUMNS` (`features.py:171-173`) does not contain it, so its NaNs read
as warm-up. **Any G-D producer must declare it point-in-time or it silently shortens every
episode** — the exact defect class a prior pass found with `spread`.

### 1.10 G-F is a plumbing gap, not a machinery gap. [C]

**`tools/store_gap_scan.py` already exists** — 433 lines, tested, wired to `just store-verify`,
and it already detects gaps, labels the seam by name, and flags off-grid bars.
**Nothing consumes its verdict.**

### 1.11 Width figures are per-fixture; only deltas transfer. [M]

`RESEARCH-3.md`'s absolute widths (59/60) came from a different funding fixture than
`AUDIT.md §2.4` (52/60/61) and **must not be compared to them**. With the *real* producer files,
`RESEARCH-1.md` measures the true progression: **52 → 57 (news) → 60 (social) → 66 (all three)**.
So shipped-default 60 + 6 = **66**, confirming the audit's "+6 at +6 width" by a second route.

Range across every configuration measured in this pass: **49–66**.

### 1.12 Do NOT inherit these figures. [M]

- **`~/Projects/kraken-market-data/store` does not exist on this host** (0 parquet under
  `~/Projects`, `KTB_STORE_ROOT` unset). Prior passes' "~76.5k bars" **cannot be re-derived** and
  was not used. All G-F store-cost figures are marked `[unverified — needs a seeded store]`.
- The audit's **158 bars / 28 gaps / 39 h** store-hole figures are `[I] inferred from a comment`
  and remain unverifiable here. `RESEARCH-3.md` proves the **mechanism** on a synthetic frame
  only: 39 absent bars → `diff` = 40.0 and `pct_change` = **+40.404%** presented as a 1-bar move;
  `rolling(24)` then spans **62 hours**. N-missing ⇒ N+1 steps.

---

## 2. Per-gap recommendations

### G-A — window not reachable (`read_ohlc_dataframe`'s `since`/`until`) · IMPROVE-EXISTING

**Researcher 2's R2 is decisive: the post-read clip in `training_frame`/`evaluation_frame` MUST
stay the single source of truth; the read window is an optimisation *underneath* it.**

The reason is not aesthetic. An empty *windowed* store read raises the **generic**
`NotEnoughDataError` (`data.py:1402-1404`) instead of `PinnedWindowUnavailableError`, and the
11-line `STORE_SEED_HINT` fix text only prints from `data_window._guard_pinned_coverage` — which
needs the **whole frame** to quote `available_span`. **Pushing the read down would replace the
honest error with a lie.**

So a correct G-A is: split `read_since`/`read_until` from a separate fetch cursor (§1.5), pass the
window **only** on the store leg, keep the clip as the authority, and preserve
`PinnedWindowUnavailableError` with a non-empty `available_span`.

DuckDB predicate/zonemap pushdown was verified live against primary docs and is **recommended
against** here: it would add a flake dependency to save milliseconds, since `compute()` is already
flat (§1.6). `pyarrow.dataset` claims are `[unverified]` — pyarrow.apache.org was unreachable
from this host, so nothing rests on them.

**Scoping fact for the builder:** `tests/test_rl_data_store.py:194`
(`test_read_ohlc_dataframe_honors_since_until_window`) **already** tests `since`/`until`
end-to-end through `read_ohlc_dataframe`. The mechanism is covered and the four callers are not
— which is exactly why the gap survived. G-A needs no new mechanism test; it needs the four call
sites, plus (a) an AST forwarding guard over all call sites, (b) a property test that a windowed
read and a whole read clip identically, (c) an error-preservation test.

### G-B — paper-trade venue + per-tick fetch · IMPROVE-EXISTING

**(i) Venue provenance.** Keep the config key and fix the one-line forward — but change the
*default* from an assertion to **"unknown" + a loud warning**, and add an **AST forwarding guard
over every `read_ohlc_dataframe(` call site**. That guard would have caught G-B(i) and will catch
the next leg that forgets. `tests/test_rl_signal_config_wiring.py:497-551` already AST-asserts
this class of forwarding, so the guard belongs there.

Provenance cannot move into the store: `_meta.json` holds only `schema` + `cursors`
(`store.py:60,491-502`), and the Binance seeder **does** write cursors
(`kraken-deep-history/tests/test_seeder.py:66`), so the obvious "no cursor ⇒ not live"
store-side signal is **refuted**. Recorded so nobody re-derives it.

**(ii) Per-tick cost.** Network, not compute (§1.6). Fix the call pattern; do not build an
incremental feature-matrix path.

**Constraint on any tail-read rewrite:** the bounded-recompute radius is **400 bars**, and
**`obv` never converges** — it is a `cumsum()` level (`features.py:1000`), not a window, and
stays 1.14–1.62 σ off at *every* `k<N` while every other family converges (k=25 → 2.67σ on
`rsi_24`; k=100 → 0.061σ; k=200 → 0.0011σ; k=400 → 4.9e-8σ). No tail length fixes it; its
*slopes* do. An incremental path also **inherits and adds an exposure** to the `ewm`-skips-NaN
trap (`features.py:809-818`): a shorter tail changes *which* ewm values are stale rather than
making them correct. Carry a poison timestamp and assert `now - t_poison < k_min*alpha_max`.

**Open, flagged as out-of-slice and `[unverified]`:** the live observation is a **single row**
(`paper_trade.py:339`) while the training Box is `(n_bars, n_features)` (`environment.py:200`),
with no reshape in `agent.py:179-182`. Worth checking — it changes what a tail read must produce,
which is the assumption the 400-bar radius rests on.

### G-C — exogenous channels unreachable · IMPROVE-EXISTING **+ NEW-DATA-SOURCE tail (real)**

Not an activation gap — see §1.1/§1.2/§1.3. The work splits:

1. **Producer-side append semantics** in both siblings (the blocker; without it a timer destroys
   history).
2. **A build fix** in `ticker-news-signals` (gnews wheel).
3. **Scheduling** — and this part is settled. From `systemd.timer(5)` (systemd 262), local man
   pages:
   - `Persistent=true` gives **ONE coalesced catch-up, not one per missed interval** → **there is
     no catch-up storm**. Quote captured in `RESEARCH-1.md §2`.
   - `Persistent=true` is **silently inert on `OnUnitActiveSec=`**, and monotonic timers pause on
     suspend and drift off the UTC bar grid. **Reject `OnUnitActiveSec`.**
   - **Reject cron** (zero catch-up → silent staleness). **Reject a Python scheduler library**
     (a new long-running process to supervise, against the repo's systemd pattern).
   - NixOS constraint **confirmed twice**: `nix/module.nix:212-238` sets only
     `environment.systemPackages` + one `systemd.services`; NixOS `systemd.user.*` has only
     `.generators`/`.paths` (no `.timers`), while home-manager has exactly `systemd.user.timers`.
     **The justfile-installed `systemd/user` pattern must stay.**
   - The existing template extends as-is, but needs a `@TICKER@` placeholder (these CLIs use
     `--ticker`; funding uses `--pair`) and **two new timers must not share `:17`** — propose
     **`:23` and `:29`**.
   - **Host finding the audit missed: the funding timer is NOT installed or enabled on this host**
     (6 timers listed, none is it). `Linger=yes`, so new user timers will fire logged-out.

4. **Flake wiring — recommendation is NOT to add them as flake inputs.** `justfile:287-290`
   already establishes the PYTHONPATH-by-path pattern for the other private sibling
   (`kraken-deep-history`, deliberately not an input). Measured: `github:` → **HTTP 404** for
   private repos; `git+https:` works via the `~/.git-credentials` helper, and the locked revs
   (`23dc965…`/`71ca27d…`) match the audit. But a pinned input would **pin the gnews build failure
   into `flake.lock`** and require a commit→push→`--update-input` round trip per fix; and
   `gnews`/`vaderSentiment` are not in nixpkgs, so a dev-shell merge means copying the broken
   overlay into this repo. Lock-bump was proven to resolve to a `/nix/store/…-source`, NOT the
   local checkout — so a bump stops being a no-op the moment either sibling is fixed.

**Rate limits and ToS** (primary sources, `RESEARCH-1.md §5`):
- **Google News: UNDOCUMENTED.** The gnews README is qualitative only ("rate-limits
  aggressively… eventually 429"). No number exists and none was invented. Our volume is **2 RSS
  requests/hour/ticker** — orders below the qualitative threshold. **Do NOT buy SearchApi or a
  proxy** ($0.55/GB).
- **StockTwits: registrations CLOSED** — `api.stocktwits.com/developers` states they will not
  accept new registrations. No published rate limit, no terms, `curl -I` shows **no
  X-RateLimit/Retry-After headers**. Volume is 2 requests/hour and keyless works (HTTP 200), but
  **this is not a production-grade dependency.**
- Hourly remains `[I]` but is now argued as *sufficient*: the coarsest cadence that still fills
  every 60-min bar, and duplicates are free at the seam (`data.py:776-781`).

**§7 Q2 — widen now?** **Yes.** `models/` is empty and tracks only `.gitkeep`, so the "+6
invalidates every artifact" cost is **currently 0** — deferring is what *creates* the cost. These
files are forward-only, so every day of delay is unrecoverable signal history. G-C is independent
of G-A, so do not couple them.

**Data-quality warning to carry into any claim about a per-ticker news effect:** news merges a
symbol search **with a whole-category `DIGITAL CURRENCIES` topic pull**
(`pipeline.py:59-60,135`), so `article_count`/`sentiment_score` are **market-wide, not
ticker-specific** — measured hour 08 = 24 articles for "ETH". Two tickers' news files would be
near-duplicates. Also `fng_index` is a **daily** value stamped per hour (distinct = 2 over 24 h),
and `_MAX_PAGES=50` but the walk stops on the first old page, so thin symbols may silently
under-cover 24 h.

### G-D — `order_book_imbalance` has no producer · market microstructure

**The aliasing measurement (§1.8) is what decides this: a REST poller cannot carry a book signal
into a 60-minute bar.** It needs a long-lived WS subscriber, which is a deployment surface
`nix/module.nix` does not currently provide. That is a materially bigger commitment than
`AUDIT.md §4/G-D`'s blast-radius paragraph implies, and it is the kind of commitment the
architect should price before choosing it.

Supporting findings:
- **`count=10` is not a ceiling problem.** Kraken's `/public/Depth` spec allows `count` max
  **500**, default 100. The reason for 10 is unrecorded.
- **An un-wrapped endpoint already exists: `/public/GroupedBook`**, in the pinned spec, wrapped by
  nothing. It has a **`grouping`** param (enum 1…1000) — server-side price-band volume
  aggregation, i.e. the `features.py:1018` numerator in one request at one bar's resolution.
  Live-verified 200.
- **The upstream WS book recorder is a trap.** `kraken-python ws book ETH/USD --output x.jsonl`
  exists (`cli.py:179-188`, `export.py:290-303`) but `models.py:760-802` has **no delta
  application and no CRC32 verification**. It writes a raw event tape, not a book.
- **Depth N, measured** (10 paired `count=500` snapshots, 8 s): N=1 lag-1 ac **+0.18**
  (unpersistent, despite being the literature's canonical Level-I); **N=10 ac +0.72**, which is
  what the code already fetches and Kraken's WS default; **N=500 sd = 0.0071** — effectively a
  constant, the same degenerate-column failure mode `AUDIT §2.4` measures. Hard argument against
  large N.
- **Idempotency, proven not assumed [M]:** local 429 server + `HTTPAdapter(max_retries=Retry(total=3, status_forcelist=[429]))`
  → GET sent 4×, **POST sent 1×, not retried**, because `DEFAULT_ALLOWED_METHODS` excludes POST.
  (Also `total=3` means 4 wire requests.) **POST order placement must never be retried blindly.**

**Honest negative results, stated as gaps in the evidence rather than as negative results:**
- No depth-weighted imbalance variant was found in any primary source → **do not ship one.** Use
  a small vector over a fixed N ladder if more than a scalar is wanted.
- **No primary source was found claiming an imbalance signal survives to a 3600 s horizon.**
- The repo already ships `obv` + `obv_slope_{1,4,24}` (`features.py:985-995`) — the
  OHLCV-derivable cousin, free, over all history. **G-D's only claim to novelty is that
  *displayed* liquidity leads the flow `obv` records late.** Free alternatives (CLV,
  Parkinson/Garman-Klass, Kyle's lambda, volume-clock) are all absent-but-derivable or not
  derivable from a 60-min bar, so per the brief none was filed as a new candidate.

### G-E — no retry/backoff/rate-limit anywhere on the fetch path · resilience

See §1.7: the upstream half is a **mis-classification** bug (`EService: Throttled:` unmatched;
Kraken signals in the body on HTTP 200, no 429). Retry libraries assessed: `backoff` last
released 2022-10-05 (~4 y stale), `httpx` 0.28.1 still Beta, and **urllib3 in the dev shell is
2.7.0, not PyPI's 2.8.0** — patch against 2.7.0.

**Lock-bump cost, stated:** `kraken-python` is pinned at `81de5974…`, narHash
`sha256-GoR8qRBTz…`, lastModified 2026-09-26. A bump must be **pushed to GitHub first**, pulls
the whole sibling (currently 0.4.0), and invalidates the nix build cache and CI.
**A bot-local wrapper is a defensible interim on three grounds — additive, sits at the only
suffering site, removable** — with three things it must NOT do (invent a rate limit; retry POST;
treat `Retry-After` as present, since Kraken does not send it).

### G-F — store holes invisible to features · data quality

See §1.10: `tools/store_gap_scan.py` already detects and labels; **nothing consumes its verdict.**
Verified recipe: `date_range` → `Index.difference` → consecutive-run grouping →
`r[0].month != r[-1].month` for the seam (matching `store_gap_scan.py:279`). Two traps the repo
already documents: off-grid bars make `missing` a **lower bound**, and duplicates are silently
collapsed.

**Recommendation: flag loudly (in the artifact, like `market_data_store_venue`), repair never,
fail opt-in.** `fillna` is not an option — §1.12 shows the damage is in *finite values*, so no
NaN policy fires. An honest "gap-filled grid" is **grid + NaN + an explicit `gap_bars` flag**,
**not** carried-forward values: `ffill` across a 39 h hole fabricates "price did not move". Cost
is **+1/+2 width, per configuration**.

The **seam** deserves its own stricter assertion: it is this repo's own join point (whole-month
archive seed → live `upsert` at `data.py:1385-1387`), categorically different from an upstream
archive hole, and **only the seam is fixable here**.

---

## 3. Cross-cutting constraints any decision must respect

1. **Fetch cursor ≠ read window.** `since` is currently both (§1.5). Conflating them is a silent
   data regression on a seeded store.
2. **The post-read clip is the honest error path.** `PinnedWindowUnavailableError` +
   `available_span` needs the whole frame; a windowed read would degrade it to a generic
   `NotEnoughDataError` (§2 G-A).
3. **Truncate vs append must be established per producer before any schedule is designed**
   (§1.1). Two of six already-shipped producers truncate.
4. **NaN-absence and NaN-warm-up are not separable from the pattern**, so `fillna`-style fixes
   cannot work, and a new exogenous column must be declared in
   `POINT_IN_TIME_EXOGENOUS_COLUMNS` or it silently shortens every episode (§1.9).
5. **`ewm` skips NaN exactly as it skips inf** — a poisoned bar is carried as a stale *finite*
   value that no `isnan`/`isinf` assertion can see.
6. **`obv` is a `cumsum` level**, not a window; it never converges under any tail length (§2 G-B).
7. **Width is per-configuration and dynamic: 49–66 across everything measured this pass.** Any
   gate must state the axis its fixture included. Never a single constant.
8. **The store is absent on this host** — every store-depth, store-cost and store-hole figure is
   either `[I]` or `[unverified — needs a seeded store]` (§1.12).

## 4. Sibling CLI / production-path facts (measured)

| Sibling | `nix run` | Notes |
|---|---|---|
| `kraken-python` | ✅ | **public** → `flake.nix:6` `github:` is correct |
| `kraken-market-data` | ✅ | private → `git+https` |
| `kraken-funding-rates` | ✅ | private; has `--append` |
| `ticker-news-signals` | ❌ **does not build** | gnews sdist packaging (§1.2); sibling `.venv` works |
| `kraken-social-signals` | ✅ | private; truncates (§1.1) |

The mixed spelling in `flake.nix` is **deliberate and correct** — do not "fix" it.

## 5. Three wrong long-standing comments found (this pass's stated failure mode)

1. `data.py:648-652` **and** `INTEGRATION.md:114-118` claim the sibling producers "re-append" —
   **they truncate** (§1.1). Worse, `INTEGRATION.md:116-117`'s documented cron recipe
   (`--output f >> f`), run verbatim, produces a **corrupt JSONL line**. Severity LOW: `data.py:722-727`
   warns per line and the merge still succeeds (verified).
2. `kraken-social-signals/.env.example:8-10` — "a registered app token buys headroom".
   **Registrations are closed.**
3. `configs/default.yaml:53-54` and `data.py:157-161` document
   `python ~/Projects/ticker-news-signals/cli.py …` — **cannot run in this dev shell** (gnews
   absent, measured) and the sibling's flake does not build.

Bonus: `INTEGRATION.md:56-80` still calls the consumer a stub with a "TODO(next pass)" merge.
Stale — all 3 real files merge verbatim today.

**Pattern worth carrying forward:** every one of these is a *documented intent* that the code
contradicts. The RUN LOG's recurring theme — the shipped defaults and comments are a live source
of silent lies — reproduced here in a sibling's README, an env example, a config comment and the
consumer's own INTEGRATION doc, all in one pass.

---

*Assembled by the lead from `RESEARCH-{1,2,3}.md` at `f6d9118`. No new measurement was performed
in assembling this file; every number here is tagged by the researcher who produced it. The
architect should treat §1 as binding: it is corrections to the audit, not commentary on it.*
