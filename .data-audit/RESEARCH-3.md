# RESEARCH-3 — G3: fetch reliability (retry/backoff, throttle default, tick-level cache)

Read-only survey, researcher 3 of 3. Written against the working tree on disk
(2026-09-30). Overwrites the prior pass's `RESEARCH-3.md` (Candidates 3+4),
which is preserved in git at `17899e1`. Scope: **G3** only.

Every environment/library fact below was verified by running the commands shown,
not recalled. Evidence: `kraken-python/kraken_api/transport.py`,
`kraken_trading_bot/{engine.py,rl/data.py,rl/paper_trade.py}`,
`kraken-python/tests/test_transport.py`, both `pyproject.toml`, both
`nix/default.nix`, both `flake.nix`, and the bot's actual `.venv`.

---

## 0. The three findings that decide this gap

**(F1) Kraken signals rate limit in the BODY, with HTTP 200.**
`kraken-python/tests/test_transport.py:63-67` builds the rate-limit case as
`FakeResponse({"error": ["EAPI:Rate limit exceeded"], "result": {}})` with the
default `status=200`, and `transport.py:249-250` raises `RateLimitError` by
matching `_RATE_LIMIT_MARKERS` (`transport.py:45-48`) **against the decoded
JSON envelope**, not the HTTP status. So `urllib3.Retry(status_forcelist=[429])`
— the single most common "add retry to requests" recipe — **does not fire for
Kraken's rate limit at all**. Any retry design that only sees HTTP status is
solving the wrong failure mode. This is the single most important fact in the
survey and it eliminates the most popular option by inspection.

**(F2) `KrakenManager.from_env(min_interval=...)` is a silent no-op.**
`kraken-python/kraken_api/auth.py:58-90` — `client_from_env(**kwargs)` reads
`min_interval` **only** from the environment (`auth.py:73`:
`float(_env(ENV_MIN_INTERVAL) or 0.0)`); `kwargs` is consulted only for
`require_credentials`. So the natural-looking bot-side override
`KrakenManager.from_env(min_interval=0.08)` (the exact call at
`kraken_trading_bot/cli.py:328,355,374,398`) is accepted and discarded, with no
warning. `client_from_credentials` (`auth.py:104`) *does* honour the kwarg, so
the two constructors disagree — a live footgun, not a hypothetical.

**(F3) The throttle is not load-bearing at the current cadence; the *re-fetch*
is.** The engine does 3 calls/pair per 60 s (`engine.py:57-74`) and the paper
trader 2 per 60 s (`paper_trade.py:56,289-298`) — i.e. ~0.05 calls/s against
Kraken's documented ~15-20/s (`kraken-python/.env.example`, restated at
`transport.py:93-96`). That is ~300x under the limit. The real cost is not
"too many requests per second", it is **"the same ~720-bar window is
re-requested 60x an hour"** and **"each tick re-parses 3 whole JSONLs"**. So the
throttle-default sub-question is largely a documentation/footgun fix, and the
cache/append sub-question is where the actual savings are. Effort should be
spent accordingly.

---

## 1. RECOMMENDED DESIGN

### 1a. Retry policy — hand-rolled loop in the sibling transport, zero new deps

**Where:** `kraken-python/kraken_api/transport.py:188-261` (`_request`). This is
the only place in either repo that knows about HTTP, the envelope, and the
rate-limit markers. Putting it here fixes the engine, the paper trader, train,
backtest and export with one change, and the module's own docstring
(`transport.py:1-18`) already scopes it as "the part of the stack that knows
*how* to talk to Kraken".

**Shape (~25 lines, stdlib only):** wrap the existing `try` block
(`transport.py:202-221`) in an attempt loop. Retry when:

| condition | detection site | safe to retry? |
|---|---|---|
| connection error / timeout | `requests.RequestException` at `:210` | **yes** — no request reached Kraken |
| HTTP 5xx / 429 | `raise_for_status()` at `:208` | **yes** for GET |
| envelope rate limit | `_RATE_LIMIT_MARKERS` at `:249` (**F1**) | **yes** for GET |
| other `APIError` | `:251` | **no** — a deterministic rejection; retrying burns the budget |

**Backoff:** `delay = min(cap, base * 2**attempt)`, then **full jitter**
(`random.uniform(0, delay)`). Full jitter over equal jitter because a burst of
tickers all retrying in lockstep is exactly what trips the limiter again; full
jitter is the standard fix. Suggested constants: `base=0.5`, `cap=30.0`,
`max_attempts=5` (worst case ≈ 0.5+1+2+4 jittered ≈ ≤7.5 s, comfortably inside a
60 s tick so a retry storm cannot overrun the interval).

**Honor `Retry-After`:** when a response carries the header, use
`max(Retry-After, jittered_backoff)` — never less than the jittered value, or a
misbehaving/absent-jitter path could poll faster than the server asked. Kraken
does send it on some throttles, and the alternative is a blind 429 loop.

**The one real design trap — nonces.** `private()` (`transport.py:134-163`)
signs a payload with a **strictly-increasing nonce** (`transport.py:169-178`).
Blindly retrying a signed POST is how you double-place an order: the first
attempt may have reached Kraken and been executed while the client saw a
timeout. So: **retry `public()` unconditionally; retry `private()` only for
exceptions raised before any response was read** (connect failure / read
timeout), and even then expose `retry_private` as an opt-in that defaults to
off. Order placement (`engine.py:104-116`) gets no retry. This must be stated
explicitly, because a naive "wrap `_request` in a retry decorator" gets it wrong
and the failure mode is a real-money duplicate order.

**Cost of this option in the sibling:** one file
(`transport.py`), plus ~15 lines of new tests in `tests/test_transport.py`
alongside the existing `FakeSession`/`FakeResponse` fakes — which already
support exactly what is needed (inject a sequence of responses, then assert call
count and the `sleep` sequence). No `pyproject.toml`, no `nix/default.nix`, no
`flake.lock`.

### 1b. Throttle — fix the footgun, set one documented default, don't move the library default

**Do NOT change `min_interval: float = 0.0` (`transport.py:108`) in the
sibling.** Reasons, in order of weight:
1. It is a library default shared by every consumer (`auth.py:73,75,104`).
   Silently inserting a sleep into someone's test suite is a behaviour change
   with no migration note.
2. `transport.py:181` short-circuits on `<= 0`, so 0.0 is the "no throttle"
   opt-out and the code already reads as intentional.
3. Per **F3**, the current workloads are ~300x under the limit; changing the
   default buys nothing here.

**Do instead, two changes:**
- **Sibling (2 lines, real bug fix):** make `client_from_env` honour a
  `min_interval` kwarg the way `client_from_credentials` already does
  (`auth.py:104`) — `min_interval=kwargs.pop("min_interval", None) or
  float(_env(ENV_MIN_INTERVAL) or 0.0)`. This closes **F2**. Without it, every
  "just set it in the bot" attempt is a silent no-op.
- **Bot (no code change needed at all):** `KRAKEN_MIN_INTERVAL` is already
  honoured today by `auth.py:73`, and `.env.example` already documents the
  value. Set `KRAKEN_MIN_INTERVAL=0.08` in the bot's `.env.example` /
  `configs/default.yaml` guidance. **`0.08` is the number to use**: it is the
  repo's own documented advice (`kraken-python/.env.example` — "Spot REST is
  limited to ~15-20 calls/s by tier. Set e.g. 0.08 for ~12/s"), which is ~2x
  headroom under the lowest tier and costs 0.16 s per paged pull of 2 pages.
  For the 60 s engine the value is irrelevant (3 calls need ≥0.08x3), so one
  setting serves both paths.

**Where the paged-pull burst actually lives:** `kraken_trading_bot/rl/data.py:247-259`
(`_page_candles`) loops `pages` times back-to-back. Train/backtest use
`pages=6` → 6 rapid calls. That burst is the only place `min_interval` earns its
keep, and 0.08 handles it. (One caveat to verify before relying on call counts:
`kraken-python/kraken_api/manager.py:178-180` resolves the pair through
`self.catalog` on every `ohlc`; the catalog is documented as cached
(`kraken-python/README.md`, "a cached pair catalog"), so this should be free
after first use — but it is the one thing I would confirm with a call counter
before claiming an exact "3 calls/tick" number.)

### 1c. Tick-level cache / incremental read — three seams, ranked by value per line

**Seam 1 (highest value, ~20 lines): stop re-fetching the OHLC window every
tick in the engine.**
`engine.py:65-66` refetches the full OHLC window and discards all but
`candles[-100:]`. On a 60-minute candle interval the window's contents change
**once an hour**; 59 of 60 ticks re-request ~720 bars to read the same 100. The
correct trigger is **bar-close, not wall-clock**: keep the last bar's `time`
and re-fetch only when `now - last_bar_time >= interval_minutes`, then splice
the new bars in. Ticker (`engine.py:59`) and order book (`engine.py:72`) stay
per-tick — they are genuinely live and are the actual inputs. Result: **3
calls/pair/tick → 1 call/pair/tick steady-state**, which is the literal
"each tick makes ≤1 small request" target, plus one OHLC pull per bar close.
This is the change that actually pays for itself and it touches one function.

**Seam 2 (~30 lines): append + tail read in the paper trader.**
`paper_trade.py:289-298` calls `read_ohlc_dataframe(..., pages=2)` with **no
`since`**, every tick. The seam already exists: `since` is a parameter
(`data.py:335`), already forwarded to `_page_candles` (`data.py:314,250`) and
to the store read (`data.py:454`), and already unit-tested
(`tests/test_rl_data_store.py:170-181`). So:
- `PaperTrader` keeps the accumulated frame across ticks (it currently does not
  — `step()` at `paper_trade.py:358-359` gets a fresh `df` every tick);
- each tick passes `since=<last bar epoch>`, so `_page_candles` asks Kraken only
  for bars the client does not have;
- concat + dedupe. Dedup is already free: `data.py:322` does
  `df[~df.index.duplicated(keep="first")]`, and Kraken's `since` returns the
  boundary bar inclusively, so overlap is expected and handled.

**Two caveats to fix as part of this, both real:**
- **`until` is silently dropped on the live path.** `read_ohlc_dataframe`'s
  null-store branch (`data.py:402-412`) forwards `since` to
  `fetch_ohlc_dataframe` but **never forwards `until`** — it is accepted,
  documented (`data.py:364-365`) and then ignored. Any tail-read design that
  relies on an upper bound is broken on the default (null-store) path. One-line
  pass-through fix, and it should come with a test.
- Feature recompute. `_build_observation` (`paper_trade.py:316-318`) runs the
  **full** `FeaturePipeline.compute` over the whole window each tick, but
  `self.context_bars` already caps it (`:316-317`). Precompute on bar-append
  only and reuse the frame between appends — this is the same bar-close trigger
  as Seam 1, so it is the same one-line condition, not a second mechanism.

**Seam 3 (best value per line of the whole survey, ~15 lines): tokenize each
JSONL once.**
`merge_extra_features` opens and fully parses the file on **every** call
(`data.py:119` `path.open()`, `:125` `json.loads` per line) and is called
**three times per load** (`data.py:323-325` and again `:457-459`) — so one
paper tick is 3 × O(file) `json.loads` passes, every 60 s, forever. A
module-level cache holding the **already-built `signal_df`** (i.e. the value as
of `data.py:156`, after flooring and column selection) keyed by
`(resolved_path, st_size, st_mtime_ns)` turns calls 2 and 3 — and every
subsequent tick — into a `stat()` plus a dict lookup. `stat()` is O(1), and
size+mtime is a correct invalidator for the siblings' append-only JSONLs
(`kraken-funding-rates` `pull --append`; the news/social CLIs write per-hour
rows). No new dependency.

**Why not a byte-offset tail read of the JSONL** (the literal reading of the
`data.py:396-401` todo): `merge_extra_features` reindexes the signal frame onto
the *whole* OHLC index and then `.ffill().fillna(0.0)` (`data.py:161-166`).
That is a **prefix-dependent** operation — an hour's value depends on every
earlier hour. A tail read would need the last value before the window carried
forward as state. Doable, but strictly more state for a file that is
hourly-resolution and therefore small. Cache-the-parsed-frame gets ~99% of the
win at ~15 lines and no new failure modes. (The `data.py:396-401` todo also
spans `since`/`until`-from-config and walk-forward, which are **not** this gap.)

---

## 2. LIBRARY CANDIDATES

Environment fact that governs the whole table (**verified** in
`/home/seanc/Projects/kraken-trading-bot/.venv`):
`requests 2.34.2`, **`urllib3 2.8.0` present (transitive)**, `filelock 4.0.4`
(torch transitive via stable-baselines3), and **`tenacity`, `backoff`,
`requests-cache`, `cachetools`, `diskcache` all MISSING**.

Cost of adding *any* new dependency to either repo = **3 files**:
`pyproject.toml` `dependencies`, `nix/default.nix` `propagatedBuildInputs`
(bot `nix/default.nix:60-68`; sibling `nix/default.nix:40-44`), and the
`flake.nix` devShell `withPackages` list (bot `flake.nix:66-77`; sibling
`flake.nix:66-73`) — plus a `flake.lock` bump on the sibling input. For
reference the entire bot is 5,700 lines.

| candidate | license | last release | nixpkgs unstable | new dep? | catches Kraken's **body** rate limit (F1)? | verdict |
|---|---|---|---|---|---|---|
| **hand-rolled loop in `transport.py`** | MIT (repo's own) | n/a | n/a | **no** | **yes** (reuses `_RATE_LIMIT_MARKERS`, `:249`) | **RECOMMENDED** — only option that is correct for F1 at zero dep cost |
| `urllib3.Retry` via `requests.adapters.HTTPAdapter` | MIT | 2.8.0 runtime / 2.2.3 nix | `python3Packages.urllib3` | **no** (transitive) | **NO** — status-only | partial: connect/5xx/429-header only. Useful as a *complement* inside the hand-rolled loop, never as the retry policy |
| `tenacity` | Apache-2.0 | **9.1.4** | `python3Packages.tenacity` | **yes** (3 files) | yes (custom `retry=` predicate on the raised exception) | viable, 3x cost. See trap below |
| `backoff` | MIT | **2.2.1** | `python3Packages.backoff` | **yes** (3 files) | yes | viable but decorator-shaped; a decorator on `transport._request` is *exactly* the shape that gets the private-nonce retry wrong (1a) |
| `requests-cache` | BSD-3 | **1.3.3** | `python3Packages.requests-cache` | **yes** (3 files + attrs/cattrs transitive) | n/a | **rejected** — wrong failure mode (caches; does not retry). Also a *correctness hazard* here: a cached ticker is a stale trading input |
| `cachetools` / `diskcache` | MIT | in nixpkgs | yes | **yes** | n/a | **rejected** — 3 in-process call sites, single-writer, no cross-process need |
| `filelock` | MIT | 4.0.4 | yes | present **only** as an undeclared torch transitive | n/a | **rejected** — depending on an undeclared transitive is a silent breakage; and there is no writer race to lock against (the G4 schedulers are the thing that would create one) |

**Trap to flag if anyone revisits this:** bare `tenacity` in nixpkgs resolves to
the **GPL-2.0 audio editor** (`nixos_nix info tenacity` → 1.3.4,
tenacityaudio.org). The Python package is `python3Packages.tenacity` (9.1.4).
A typo in `propagatedBuildInputs` would pull a GPL binary into an MIT project
and still evaluate cleanly.

**Incidental finding (out of scope, flagging not fixing):**
`kraken-python/nix/default.nix:17` pins `version = "0.3.0"` while
`kraken-python/pyproject.toml` says `0.4.0`. The Nix package metadata is stale
and disagrees with the library it builds.

### Scoring against the gap's own acceptance target

*"each tick makes ≤1 small request"* + *"transient errors retry"*:

| option | ≤1 req/tick | retries transient | deps added | net |
|---|---|---|---|---|
| hand-rolled + Seam 1 | **yes** (3→1 calls/pair/tick) | yes (incl. F1 rate limit) | 0 | **ship this** |
| hand-rolled only | no (still 3) | yes | 0 | half a fix |
| `urllib3.Retry` only | no | **no** for Kraken rate limit | 0 | does not work |
| `tenacity` only | no | yes | 3 files | same outcome, 3x cost, plus nonce trap |
| `tenacity` + Seam 1 | yes | yes | 3 files | same outcome as hand-rolled + Seam 1 |
| `requests-cache` | no | no | 4 files | none of it |

The library choice is worth ~25 lines of code. **The cache/incremental seams are
worth the entire saving** — which is the opposite of the framing in the gap
description and is the main thing I would tell the architect.

---

## 3. REJECTED OPTIONS AND WHY

1. **`requests-cache` in front of the transport** — it is a *cache*, and G3's
   first requirement is *retry*. It also actively harms correctness here: the
   engine's ticker and order book are live trading inputs, and a TTL cache that
   serves a 30 s-stale book is a worse bug than the extra request. It would
   need per-endpoint TTL policy (OHLC long, ticker zero) which is more config
   than the thing it replaces.
2. **Changing the shared `min_interval=0.0` default in the sibling** — silent
   behaviour change for every consumer of a library, zero measured benefit at
   this cadence (**F3**), and it contradicts the documented meaning of `0.0`
   (`transport.py:181`). Fix the kwarg footgun instead (**F2**); the env var
   already works.
3. **A generic polling-loop framework** (APScheduler / schedule / a job runner)
   — G3's complaint is the *cost inside* the tick, not the scheduling of ticks.
   The tick cadence is a config value (`engine.py:36`). This would import a
   whole lifecycle model to fix an inner-loop problem.
4. **Byte-offset tail-read of the JSONLs** — prefix-dependent `ffill`
   (`data.py:161-166`) means a tail read needs the pre-window value carried
   forward as state; more state, more failure modes, for an hourly-resolution
   file that is small. Cache-the-parsed-frame is strictly better here.
5. **Caching at the `requests.Session` / adapter layer** — cannot express
   "one OHLC per bar close, ticker every tick" per-endpoint without the same
   TTL policy that killed `requests-cache`.
6. **A `diskcache`/`cachetools` TTL dict for the JSONL frames** — works, but
   the invalidator needed here is `(size, mtime_ns)`, which is 3 lines of
   `path.stat()`. A dependency for that is not a trade.
7. **Retrying `private()` order placement** — actively dangerous. Strictly
   increasing nonces (`transport.py:169-178`) mean a retried signed POST can
   double-place a real order after a read timeout. Out of scope for a public-
   data bot, but it is the reason the retry must be `public()`-scoped.
8. **Dependence on the `filelock` already in the venv** — it is a transitive of
   torch/stable-baselines3, not declared in `pyproject.toml` or
   `nix/default.nix`. Fine to observe; not fine to import.

---

## 4. SUGGESTED SLICE ORDER (if the architect picks G3)

1. **Seam 3** — `merge_extra_features` parsed-frame cache. ~15 lines, no dep,
   no behaviour change, removes 3 O(file) parses per tick. Lowest risk, and it
   exercises the `data.py` test surface that already exists.
2. **Seam 1** — engine bar-close gate. ~20 lines, 3 calls/pair/tick → 1.
3. **1a** — transport retry loop (sibling repo, with the `public()`-only scope
   and the `Retry-After`/jitter rules) + its tests.
4. **1b** — the 2-line `client_from_env` kwarg fix (closes F2) and documenting
   `KRAKEN_MIN_INTERVAL=0.08` in the bot.
5. **Seam 2** — paper-trader append + tail read, including the `until`
   pass-through fix at `data.py:402-412` and a test for it.

Steps 1-2 are entirely inside this repo and are independently shippable.
Steps 3-4 are in the sibling `kraken-python` and need a flake-input bump.

RESEARCH COMPLETE
