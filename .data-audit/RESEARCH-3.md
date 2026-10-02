# RESEARCH-3 — Gap G3: no retry / backoff / rate-limit handling on the OHLCV fetch path

**Role:** Researcher 3, Phase 1 data-pipeline audit. **READ-ONLY** — the only file this pass
writes is this one. Task `task_murj290u_0004_g9uh2j`.
**Checkout:** `/home/seanc/Projects/kraken-trading-bot` @ `4fd3b77`; sibling
`/home/seanc/Projects/kraken-python` @ `81de597`.
**Scope:** AUDIT.md §5 Q3 — *where should the retry live?* — plus the library/pattern survey
that answers it, and the visibility half of the gap (`engine.py`'s bare `except`).

---

## 0. Verdict up front

| Question | Answer |
|---|---|
| Where does the retry go? | **`data.py:_page_candles` now; upstream `transport.py` as a separate, later change.** (c) is the correct *end state*; (a) is the only *effective today*. |
| Which library? | **No new dependency.** `urllib3.Retry` for transport-level status/connection retries (already installed transitively) **+ a hand-rolled full-jitter resume loop in `_page_candles`** for the envelope-level case. `tenacity` is the right answer *if* a dependency is acceptable. |
| Why hand-rolled and not `tenacity`/`urllib3` alone | **Kraken signals a rate limit with HTTP 200 and an `{"error": [...]}` body.** `urllib3.Retry` is status-code-driven only and can never see it. Verified: `urllib3/util/retry.py:424-442` (`is_retry(self, method, status_code, has_retry_after)` — no body inspection anywhere). |
| How does the failure become visible? | A `RateLimitExhaustedError(ValueError)` in `data.py` beside the existing three, plus a `_reason` code on the engine's tick dict. `errors.py` is the wrong home — it re-exports the sibling's hierarchy verbatim. |
| Hard blocker for any fix | `EAPI` vs `EService` vs HTTP-429 currently produce **three different exception types** out of the wrapper. A retry keyed on `RateLimitError` alone silently misses two of the three. |

---

## 1. New evidence beyond the lead's verified list

The lead's list is correct and I re-confirmed every line. These four items are **additional**
and each one changes the shape of the fix.

### 1.1 The throttle is off by default — `min_interval` defaults to `0.0`

`kraken-python` has *a* rate-limit mechanism, and it is a no-op in the shipped configuration:

* `/home/seanc/Projects/kraken-python/kraken_api/transport.py:108` — `min_interval: float = 0.0`
* `kraken_api/auth.py:73` — `min_interval = float(_env(ENV_MIN_INTERVAL) or 0.0)`
* `kraken_api/auth.py:42` — `ENV_MIN_INTERVAL = "KRAKEN_MIN_INTERVAL"`
* `nix/module.nix:149-153` — `minInterval` is `lib.types.nullOr lib.types.str`, **`default = null`**
* `nix/module.nix:205` — the env line is `lib.optional (cfg.settings.minInterval != null)`, i.e. absent by default
* `.env.example:10-11` — `# KRAKEN_MIN_INTERVAL=0.1`, **commented out**

Both fetch legs build their manager with `KrakenManager.from_env()` (`data.py:1174-1177`,
`data.py:1359-1362`), which routes through `auth.py:83-90`. So `_throttle()`
(`transport.py:181-186`) returns immediately at `transport.py:181-182` and **the deployed bot
sends zero inter-call spacing**. The gap is worse than "fixed spacing instead of adaptive":
there is no spacing at all, and no retry.

> Consequence for the fix: an upstream-only "make the throttle adaptive" change would be a
> **no-op** for this repo as configured unless `nix/module.nix`'s `minInterval` default or the
> recipe wiring also moves. Worth calling out to the architect.

### 1.2 Three distinct failure shapes leave the wrapper, and a `RateLimitError` catch misses two

`transport.py` produces rate-limit information through three different channels, and only one
of them is a `RateLimitError`:

| # | Kraken signal | Path through `transport.py` | Exception the caller sees | `isinstance(exc, RateLimitError)`? |
|---|---|---|---|---|
| 1 | `EAPI:Rate limit exceeded` in a 200 body | `transport.py:249-250` (marker `_RATE_LIMIT_MARKERS`) | `RateLimitError` | **yes** |
| 2 | `EService: Throttled: [UNIX timestamp]` in a 200 body | `transport.py:249-251` — marker miss | `APIError` | **NO** |
| 3 | a genuine HTTP 429 | `transport.py:205-206` → `raise_for_status()` → `transport.py:210-221` logs and re-raises | `requests.HTTPError` | **NO** |

Evidence:

* `transport.py:45-48` — `_RATE_LIMIT_MARKERS = ("EAPI:Rate limit exceeded", "EGeneral:Too many requests")`.
  **`EService` appears nowhere** in the whole package — `grep -rn "EService\|Throttled\|Retry-After\|retry_after\|429" kraken_api/` returns **no hits**.
* Kraken's current docs (fetched live, see §4) list **`EService: Throttled: [UNIX timestamp]`
  "Try again after [timestamp]"** as one of two official rate-limit errors. It is the *only*
  one that tells the client when to come back, and the wrapper does not recognise it.
* `EGeneral:Too many requests` is in the marker tuple but does **not** appear anywhere in
  current Kraken documentation. It is a stale marker.
* A real HTTP 429 dies at `response.raise_for_status()` (`transport.py:205`), i.e. *before* the
  error-envelope branch that raises `RateLimitError`. `requests.HTTPError` escapes through the
  `except requests.RequestException` handler at `transport.py:210-221`, which **logs and re-raises**.

> **This is the single most important finding for the implementer.** The obvious fix —
> `except RateLimitError: backoff_and_retry(...)` — is **wrong**, and silently so: it catches
> case 1 and neither of the others. Any caller-side retry must catch on
> `(RateLimitError, APIError, requests.RequestException)` or, better, be keyed on a
> predicate that inspects the message.

### 1.3 `EService: Throttled` carries a retry-after timestamp, and it is Kraken's `Retry-After`

Kraken does **not** use an HTTP `Retry-After` response header. It embeds a UNIX timestamp in
the error string: `EService: Throttled: [UNIX timestamp]` — see §4 for the doc quote. That is
the functionally equivalent signal, and it is currently thrown away twice over: the wrapper
does not match the marker, and even if it did, `RateLimitError(errors, endpoint=path)`
(`/home/seanc/Projects/kraken-python/kraken_api/errors.py:31`) carries the raw list with no
parsed field.

So the correct `wait` is **not** a pure exponential schedule — it is
`max(exponential_full_jitter, parsed_retry_after - now)`. That is a genuinely better retry than
any library gives you out of the box, and it is the one Kraken asked for.

### 1.4 Upstream is a pinned flake input — an upstream-only fix would be invisible to `just`

This decides §5 Q3, so it is evidence rather than argument:

* `flake.nix:6` — `kraken-python.url = "github:Cairnstew/kraken-python"` — a **flake input**.
* `flake.lock` pins `rev = "81de5974ded7c17586eedc78158d1293d2f74c66"`, which is **exactly the
  local checkout's HEAD** (`git -C /home/seanc/Projects/kraken-python log --oneline -1` →
  `81de597`). The local tree and the pin are the same commit *today*.
* `nix/default.nix:17-61` builds it as a Nix package; `flake.nix:24,50` pass
  `kraken-python.outPath` into the dev shell that `{{dev}}` in the `justfile` uses.
* The `.venv`, by contrast, has it **editable**: `__editable___kraken_python_0_4_0_finder.py`
  maps `kraken_api` → `/home/seanc/Projects/kraken-python/kraken_api`.

So editing upstream is *immediately* live under pytest and *silently absent* from
`just bench`, which builds through Nix from `flake.lock`. An upstream-only fix would appear to
work in the test suite and never run in the recipe the user actually types. That asymmetry is
the whole reason the minimal fix must land in `data.py`.

---

## 2. The library survey

All facts below verified from PyPI's JSON API, the projects' own GitHub API metadata, and —
for `urllib3` — the **installed 2.8.0 source in this repo's `.venv`**, which is the version that
would actually run.

### 2.1 Summary table

| Library | Latest | Last release | Repo state | License | Zero deps? | Rate-limit capable? | Full jitter? | Verdict |
|---|---|---|---|---|---|---|---|---|
| `tenacity` | **9.1.4** | 2026-02-07 | active (commit 2026-10-01), 8.8k★, 67 open | Apache-2.0 | yes (`requires_dist` empty) | **yes** (`retry_if_exception_type`, predicate retries) | **yes** — `wait_random_exponential` cites the AWS post by name | **Strongest library.** One new dep. |
| `urllib3` | **2.8.0** | 2026-09-15 | very active (commit 2026-10-02), 4.1k★ | MIT | yes | **partial** — status codes + connection errors only | **no by default** (`backoff_jitter=0.0`); what it has is *additive*, not full | **Already installed.** Right tool for HTTP-level only. |
| `backoff` | 2.2.1 | **2022-10-05** | **`archived: true`**, last default-branch commit 2023-01-20, 64 open | MIT | yes | yes (via giveup/wait) | yes (`expo`, `jitter`) | **Disqualified — archived.** |
| `httpx` | 0.28.1 | 2024-12-06 | repo pushed 2026-10-02, last default-branch commit 2026-02-23, 15.5k★ | BSD-3-Clause | no (`anyio`,`httpcore`,`certifi`,`idna`) | **no** — see §2.4 | no | **Disqualified.** Would mean swapping the HTTP stack. |

### 2.2 `tenacity` — the strongest candidate, and it documents the jitter argument itself

* **Version/maintenance:** `9.1.4`, released **2026-02-07**; `9.1.3` two days earlier (2026-02-05);
  `9.1.2` 2025-04-02. PyPI provenance is signed by GitHub Actions and attributed to
  `github.com/jd/tenacity`. Last default-branch commit `2026-10-01`. Two maintainers.
* **License:** Apache-2.0. **Auth:** none — no account, no key, no telemetry.
* **Rate limits:** none of its own.
* **Zero dependencies** — `requires_dist` is `[]`. Wheel is 28.9 kB, pure `py3-none-any`.
* **Output/behaviour shape — this is the part that matters.** Tenacity decorates a function; the
  decorated callable returns the **same value on success** and raises the **last exception** once
  a `stop` strategy trips. So wrapping `manager.ohlc` is a drop-in with no return-shape change
  — which is what keeps the duck-typed `FakeSource`/`OneShotManager` fakes in
  `tests/test_rl_data_store.py:65-72`, `tests/test_rl_paper_trade.py:85-96` working untouched.
* **The exact primitives this gap needs, from `tenacity/wait.py` / `tenacity/retry.py`:**
  - `wait_random_exponential(multiplier=…, max=…)` (`wait.py:247`) — subclasses `wait_exponential`,
    `__call__` returns `random.uniform(self.min, high)`. Its own docstring names the AWS post and
    says it **"corresponds to the 'Full Jitter' algorithm described in this blog post"**.
  - `wait_exponential_jitter(initial=…, max=…, jitter=…)` (`wait.py:279`) — implements **Google's**
    documented strategy: `max(min, min(multiplier * 2**n + random.uniform(0, jitter), maximum))`.
    That is AWS "Equal Jitter", and the AWS post ranks Equal Jitter the loser of the two.
  - `retry_if_exception_type(...)`, `retry_if_exception(...)`,
    `retry_any(...)`/`retry_all(...)`, `retry_if_exception_message(...)` (`retry.py:107, 88, 284, 301, 232`).
  - `stop_after_attempt`, `before_sleep_log` for the visibility half.
* **Correct composition for this repo** (and note it is *not* pure exponential, per §1.3):
  `retry=retry_if_exception_type((RateLimitError, APIError, requests.RequestException))`,
  `wait=wait_combine(wait_random_exponential(multiplier=1, max=30), wait_retry_after)`,
  `stop=stop_after_attempt(5)`, `reraise=True`.

### 2.3 `urllib3.Retry` — free, already installed, and **blind to Kraken's actual signal**

`requests 2.34.2` and `urllib3 2.8.0` are already in `.venv` (verified via
`importlib.metadata`). `requests` exposes it cleanly — verified from the installed
`requests/adapters.py`:

* `HTTPAdapter.__init__(..., max_retries: int | Retry = DEFAULT_RETRIES, ...)`
* `HTTPAdapter.__attrs__ = ['max_retries', 'config', '_pool_connections', '_pool_max_nsize', ...]`
* `Session.mount(prefix, adapter)` exists

so `session.mount("https://", HTTPAdapter(max_retries=Retry(...)))` is a two-line change in
`transport.py` — **if** `transport.py` ever let a caller pass a session. It currently takes a
`session` parameter (`transport.py:91,114`) but constructs `requests.Session()` itself
(`transport.py:114`) with **no adapters mounted**. Verified defaults from the installed
`urllib3/util/retry.py:231-242`:

```python
status_forcelist: typing.Collection[int] | None = None,
backoff_factor: float = 0,          # ← no backoff unless you ask
backoff_max: float = DEFAULT_BACKOFF_MAX,          # 120
raise_on_status: bool = True,
respect_retry_after_header: bool = True,
backoff_jitter: float = 0.0,        # ← no jitter unless you ask
retry_after_max: int = DEFAULT_RETRY_AFTER_MAX,    # 21600
```

**Three reasons it cannot be the whole answer, all sourced:**

1. **It cannot see a Kraken rate limit.** `is_retry(self, method, status_code, has_retry_after=False)`
   (`urllib3/util/retry.py:424`) is the sole status gate at `:435` (`status_forcelist` membership)
   and `:442` (`RETRY_AFTER_STATUS_CODES`). There is no body inspection anywhere in `retry.py`.
   Kraken's `EAPI:Rate limit exceeded` arrives as **HTTP 200** — §1.2 case 1 — so `Retry` returns
   `False` and passes the response straight through. Confirmed by the wrapper's own control flow:
   `transport.py:205` `raise_for_status()` succeeds on a 200, and the rate limit is only detected
   later, at `:248-250`, by string-matching the JSON body. **A 200 is, to urllib3, a success.**
2. **Its jitter is additive, not full, and off by default.** `get_backoff_time()`
   (`urllib3/util/retry.py:321-338`), read from the installed file:
   ```python
   backoff_value = self.backoff_factor * (2 ** (consecutive_errors_len - 1))
   if self.backoff_jitter != 0.0:
       backoff_value += random.random() * self.backoff_jitter
   return float(max(0, min(self.backoff_max, backoff_value)))
   ```
   Jitter is `random.random() * backoff_jitter` **added to** the full exponential value — a
   widening band around the deterministic delay, which is strictly weaker than Full Jitter's
   `uniform(0, high)`. AWS's simulation (§3) is unambiguous that the *un-jittered* curve is the
   loser; this only becomes full jitter in the AWS sense if you both set `backoff_factor > 0`
   and accept the additive band as good enough. Treat it as "exponential backoff with optional
   additive jitter", not "jittered backoff".
3. **Layer violation.** Mounting `Retry` in `transport.py` protects the HTTP layer, which by §1.2
   is not where Kraken's rate limit is detected.

**Where `urllib3.Retry` *is* exactly right:** connection errors and read timeouts, and real
HTTP 429/5xx if Kraken or a proxy ever emits them (`status_forcelist={429,500,502,503,504}`,
`allowed_methods=frozenset({"GET"})`, `respect_retry_after_header=True`). Note `allowed_methods`:
**POST must not be retried** — `transport.py:157` stamps a monotonic nonce
(`_next_nonce()`, `transport.py:170-178`) into every private call, so an automatic POST retry
would replay an order against a stale nonce. A `Retry` mounted at the session layer therefore
needs `allowed_methods={"GET"}` explicitly, or private order calls become a duplicate-order bug.
That is a concrete, non-obvious safety constraint on option (b).

### 2.4 `httpx` — disqualified twice over

`httpx`'s own transport docs (`docs/advanced/transports.md`, read from the repo) state verbatim:

> "Connection retries are also available via this interface. Requests will be retried the given
> number of times in case an `httpx.ConnectError` or an `httpx.ConnectTimeout` occurs… **If you
> need other forms of retry behaviors, such as handling read/write errors or reacting to `503
> Service Unavailable`, consider general-purpose tools such as [tenacity].**"

i.e. `httpx.HTTPTransport(retries=1)` covers connect failures **only** — no status-code retry,
no backoff, no jitter, no `Retry-After`. And httpx is not installed (`kraken-python` uses
`requests`, `transport.py:204-206`), so adopting it means replacing the HTTP stack of the
foundation repo to obtain *less* retry capability than `requests` already has. Reject.

### 2.5 `backoff` — reject, and the reason is unambiguous

`litl/backoff` is **`archived: true`** on GitHub. Last PyPI release `2.2.1` on **2022-10-05**;
last commit on the default branch **2023-01-20** ("Add exponential decay (#189)"); 64 open issues
unanswered. An archived dependency cannot receive a security fix. Reject despite a clean API
(`@backoff.on_exception`, `expo`, `jitter`) and MIT licensing.

---

## 3. Jitter is the point — the authority

**Marc Brooker, "Exponential Backoff And Jitter", AWS Architecture Blog, 04 Mar 2015**
(https://aws.aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/ — fetched live).
Carries a **May 2023 update** stating that after eight years "this solution continues to serve as
a pillar for how Amazon builds remote client libraries", that "Most AWS SDKs now support
exponential backoff and jitter as part of their retry behavior", and pointing at the
[Amazon Builders' Library chapter "Timeouts, retries, and backoff with jitter"](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/).

The claims that bear on this gap, quoted:

* **"The solution isn't to remove backoff. It's to add jitter."**
* **"The no-jitter exponential backoff approach is the clear loser. It not only takes more work,
  but also takes more time than the jittered approaches. In fact, it takes so much more time we
  have to leave it off the graph to get a good comparison to the other methods."**
* On the mechanism: after plain exponential backoff the call timeseries still shows clusters —
  *"Instead of reducing the number of clients competing in every round, we've just introduced
  times when no client is competing."*
* Ranking: Full Jitter and Equal Jitter are "approximately the same" for call count with Equal
  slightly worse; Decorrelated is worse still; **"Of the jittered approaches, 'Equal Jitter' is
  the loser"**; the Full-vs-Decorrelated call is "less clear".
* Quantified: **"In the case with 100 contending clients, we've reduced our call count by more
  than half."**
* Recommendation: **"The return on implementation complexity of using jittered backoff is huge,
  and it should be considered a standard approach for remote clients."**

**Applied to this repo specifically.** The herd here is not hypothetical and it is *in the
repo*: `justfile:76-77`'s `bench` recipe runs `train` then `backtest`, and the recipe's own
comment says so — *"The backtest refetches the same `pages` window seconds after training"*. Each
`train`/`backtest` invocation is a fresh 6-page burst (`data.py:1112` `pages: int = 6`). N
matrix cells from `tools/model_matrix.py` swept back to back → N nearly-simultaneous 6-page
bursts against a **Starter-tier counter capped at 15 with decay of −0.33/sec**
(§4). Six pages per run against a 15-count ceiling that recovers 0.33/sec is one or two runs
away from the cap on a cold counter. That is precisely the contention AWS simulated, and it is
why the fix must be Full Jitter rather than `wait_fixed` — and why `wait_fixed` would be the
*actively harmful* choice here.

**Design consequence:** the two `data.py` legs must not share one RNG stream. Two concurrent
`just bench` invocations (the sweep case) that both start jitter at `uniform(0, 1)` on their
first retry are correlated at exactly the wrong moment. Seed per-attempt from
`random.SystemRandom()` or, better, jitter on a hash of `(pid, pair, page_index, attempt)`.

---

## 4. Kraken's documented rate-limit policy (fetched live)

Source: **https://docs.kraken.com/exchange/guides/rest/ratelimits** — "Spot REST Rate Limits"
(the old `support.kraken.com/hc/en-us/articles/360000426832-api-rate-limits` now **301**s to a
404; `docs.kraken.com/api/docs/rest-api/rate-limits` is **404**; the working canonical URL is
the one above, cross-linked from the support centre). Verbatim:

> "Every REST API user has a 'call counter' which starts at `0`. Ledger/trade history calls
> increase the counter by `2`. All other API calls increase this counter by `1` (except AddOrder,
> CancelOrder which operate on a different limiter detailed further below)."
>
> | Tier | Max API Counter | API Counter Decay |
> | Starter | 15 | -0.33/sec |
> | Intermediate | 20 | -0.5/sec |
> | Pro | 20 | -1/sec |
>
> "The user's counter is reduced every couple of seconds depending on their verification tier.
> Each API key's counter is separate, and if the counter exceeds the maximum value, subsequent
> calls using that API key would be rate limited. **If the rate limits are reached, additional
> calls will be restricted for a few seconds (or possibly longer if calls continue to be made
> while the rate limits are active).**"
>
> **Errors**
> * `"EAPI:Rate limit exceeded"` if the REST API counter exceeds the user's maximum.
> * `"EService: Throttled: \[UNIX timestamp\]"` if there are too many concurrent requests.
>   **Try after \[timestamp\]**.

Four things follow, each of which changes the fix:

1. **The limit is a decaying counter, not a fixed interval.** A fixed `min_interval` cannot
   express it — another reason `transport.py:181-186` is the wrong shape (§1.1).
2. **Kraken documents the thundering-herd penalty itself**: "possibly longer if calls continue
   to be made while the rate limits are active". Retrying *tightly* makes the punishment longer.
   This is the exchange, in its own docs, endorsing §3.
3. **`Retry-After`: not a header.** The signal is the UNIX timestamp inside
   `EService: Throttled: [UNIX timestamp]`, which is currently unrecognised by the wrapper (§1.2)
   and therefore unavailable to any caller. **Parsing it is the single highest-value, lowest-cost
   piece of this gap** — it replaces a guess with the server's own instruction.
4. **Numbers are small.** Starter = 15 count, −0.33/sec ⇒ full recovery from a blown counter in
   ~45 s. Any retry budget that gives up sooner than ~45 s is mis-tuned for the default tier;
   any budget that waits *longer* than ~45 s is waiting on nothing. Five attempts with
   `wait_random_exponential(multiplier=1, max=30)` (expected total ≈ 1+2+4+8+16 ≈ 31 s of
   *mean* window, tail to 30 s each) sits sensibly inside that. **Ceiling the total at ~60 s.**
   Note also `pages=6` means a *page* costs 1 count — so a 6-page burst costs 6 of 15.

---

## 5. Where should the retry live? — answering AUDIT.md §5 Q3

### (a) In `data.py:_page_candles` — **do this now**

* **Cost:** one function, both call sites, zero new dependencies (`urllib3` is already there
  transitively via `requests`).
* **Covers both legs automatically.** `data.py:1179` (fetch) and `data.py:1364` (store) share
  `_page_candles`; the AUDIT is right that one edit covers both. The docstring at `data.py:1071-1098`
  says so explicitly: *"This is shared by the live fetch and the market-data store read-through
  adapter so the two paths can never drift apart on pagination semantics."* — the retry inherits
  that guarantee.
* **Correct layer for the *cursor* concern.** Only the caller knows `cursor`, `collected`, and
  `pages`. A transport-level retry cannot resume a partially-collected page loop; it can only
  retry one HTTP call. The discarded-pages-1-to-3 failure (`data.py:1104-1111`) is a *caller*
  bug, so a caller-side fix is the only one that addresses it directly.
* **The governance asymmetry (§1.4) settles it.** A `data.py` fix runs in the Nix dev shell that
  `just` drives and in the lock-pinned build. An upstream-only fix does not, until a lock bump
  and a GitHub push.
* **Honest weakness:** it does not protect `engine.py`'s three direct calls
  (`engine.py:59, 65, 72`). That is a real gap and option (b) is the answer to it — but it is a
  *different* program (AUDIT §1.2: "a separate, disjoint program"), so it can be sequenced.

### (b) Upstream in `kraken-python`'s `transport.py` — **the right end state, wrong first move**

* **For:** fixes `engine.py`'s three calls and any future consumer; a `session.mount(...,
  HTTPAdapter(max_retries=Retry(...)))` is two lines; a retry-aware `RateLimitError` carrying
  `retry_after` would let *every* caller honour `EService: Throttled`.
* **Against, and it is substantial:** pinned flake input (`flake.nix:6`, `flake.lock` rev
  `81de5974`) ⇒ needs a lock bump + push to `github.com/Cairnstew/kraken-python` +
  `nix/default.nix` rebuild to affect `just`; it edits a repo this audit is scoped *out* of;
  a `Retry` mounted at the session layer will **replay private POSTs** unless
  `allowed_methods={"GET"}` is set explicitly (§2.3), because `transport.py:157` +
  `:170-178` stamp a monotonic nonce — an ordering/duplicate-order hazard in a *trading* client;
  and per §1.1 a throttle-only fix is a no-op under this repo's current config.
* **Also note:** the wrapper already has a structured error path — `transport.py:210-221` calls
  `log_event(_HTTP_LOG, …, status="error", reason=exc.__class__.__name__)` and
  `transport.py:245-251` logs `errors=[...]` before raising. So the wrapper has the observability
  plumbing; only the retry and the marker set are missing.

### (c) Layered — **correct end state, wrong order**

The layering is right: transport owns per-HTTP-call retries; the caller owns resume-from-cursor.
But land (a) first and (b) as a **separate PR with its own lock bump**, so that each half is
independently reviewable and the repo is never in a state where the fix exists only in a local
checkout that `flake.lock` does not know about.

**Answer to §5 Q3: (a) now, (b) after, (c) as the destination.**

---

## 6. Making the failure visible instead of a silent WARNING

### 6.1 The current collapse, precisely

`engine.py:57-74` — three bare `except Exception as e: _LOGGER.warning(...)`, one per key:

| `engine.py` | call | on failure |
|---|---|---|
| `:58-62` | `manager.ticker(pair)` | `data["ticker"]` **absent** |
| `:64-68` | `manager.ohlc(pair, interval=60)` | `data["candles"]` **absent** |
| `:71-75` | `manager.order_book(pair, count=10)` | `data["order_book"]` **absent** |

`strategies/sma.py:68-76` reads `data.get("candles", [])` and `data.get("ticker")`, so a
rate-limited tick and a genuinely empty market are the *same object*. `sma.py:72-76` then answers
`hold` with reason `"insufficient data"`. A 60-second tick loop (`engine.py:36, 166-168`) means
the strategy can emit `hold` for an hour with no signal that the exchange was refusing calls.

### 6.2 The fix, consistent with what the repo already does

The repo has an established, non-obvious pattern for exactly this: **absence is distinguishable
from neutral**. `data.py:806-816` never ffill-buries a missing signal into a real-looking `0.0`;
it emits `signal_observed` / `signal_age_hours` (`features.py:104-108`). The engine should do the
same with a **reason code**, not with an absent key.

**Concrete, minimal:**

1. **New exception in `data.py`, beside the existing three.** `NotEnoughDataError`
   (`data.py:220`), `MarketDataStoreUnavailableError` (`data.py:237`) and
   `SignalTickerMismatchError` (`data.py:367`) are all `ValueError` subclasses declared in
   `data.py` itself. Add a fourth in the same place and style:

   ```python
   class RateLimitExhaustedError(ValueError):
       """Every retry attempt was refused; the window is incomplete."""
       def __init__(self, pair, interval, attempts, last_error, retry_after=None): ...
   ```

   with `attempts`, the last exception's type, and the parsed `retry_after` timestamp on the
   instance so callers and the run log can read them. **A partial page-loop must never be
   persisted as a complete window** — so this must be raised *before* `store.upsert`
   (`data.py:1366`), not after. See §8.

2. **`engine.py`: replace the three bare `except Exception` with typed handlers plus a reason
   code.** A `RateLimitExhaustedError`/`RateLimitError` handler sets
   `data["degraded"] = "rate_limited"`; any other exception sets `data["degraded"] = repr(type(e).__name__)`;
   a partial-success tick keeps a populated key **and** a non-null `degraded`. Key *presence* then
   stops carrying the meaning of "no data" and `sma.py` can distinguish `hold/no-data` from
   `hold/unavailable` — a two-line change at `sma.py:72-76` once the code exists. Keep
   `_LOGGER.warning`, but log the **type name and the retry-after delta**, so the run log says
   `rate_limited (retry_after=41s)` rather than the current free-text `e`.

3. **`errors.py` is the wrong home — do not put it there.** `errors.py:1-29` is a pure re-export:
   it imports seven names from `kraken_api.errors` and adds exactly one (`StrategyError`). The
   three `data.py` errors are *not* in it and never were. Putting `RateLimitExhaustedError` there
   would be a first, and the wrong one: it is a bot-side data-completeness failure, not a Kraken
   transport error. **`data.py` is the consistent home.** (`RateLimitError` is already
   re-exported at `errors.py:11, 26`, so a caller that wants it has it.)

4. **`tools/store_guard.py` is also the wrong home, and it is worth reading anyway.** It is a
   *pipe-stage refusal* tool: `store_guard.py:82-122` exits `1` on a bad `store_mode` with the
   reason on stderr, consumed by `just store-seed`. Its lesson generalises (a refusal, not a
   warning; name the recipe and the fix in the message) but its mechanism does not apply — a
   retry policy inside a training read is not a seed-report gate. **Reuse its message style;
   do not wire it in.**

---

## 7. The MINIMAL correct fix

Five changes. Nothing else. No new required dependency.

### Step 1 — `data.py`: a jittered, envelope-aware resume wrapper inside `_page_candles`

Wrap the single `manager.ohlc(...)` call at `data.py:1105`. Requirements, each traceable to
evidence above:

* Retry on `(RateLimitError, APIError, requests.RequestException)` — **not** `RateLimitError`
  alone (§1.2). `requests` is available transitively; if importing it into `data.py` is
  considered unacceptable, `APIError` + a message predicate covers cases 1 and 2 and case 3
  degrades to the pre-existing behaviour.
* `wait = max(full_jitter(2**attempt), retry_after - now)` where `retry_after` is parsed out of
  `EService: Throttled: <epoch>` (§1.3, §4.3). Full jitter per §3 — **never `wait_fixed`**.
* Cap: 5 attempts, per-attempt ceiling 30 s, **total ≤ ~60 s** (§4.4).
* `reraise=True` semantics: on exhaustion raise `RateLimitExhaustedError` **carrying `collected`
  in the message** (pages 1-3 were real data; the operator needs to know a partial window existed).
* **Injectable sleep/RNG.** `def _page_candles(..., *, _sleep=time.sleep, _rng=random.random)`
  with `pages` untouched. Every existing fake (`tests/test_rl_data_store.py:65-72`,
  `tests/test_rl_paper_trade.py:85-96`, `tests/test_rl_export.py:86`,
  `tests/test_rl_signal_config_wiring.py:115`, `tests/test_rl_training.py:180,196,270,354`)
  keeps its current signature, and no test acquires real wall-clock delay.
* Log each retry at `WARNING` with `(pair, interval, page, attempt, kind, retry_after)` — the
  page-loop currently logs only at `DEBUG` (`data.py:1109`), so today *nothing above DEBUG*
  records that the loop retried.

### Step 2 — `data.py:1364-1372`: do not persist a partial window

Today `candles = _page_candles(...)` then `store.upsert(...)`. If `_page_candles` raises after
partial collection, `upsert` is skipped — good. But if the caller *catches* the exhaustion to
"be resilient", it must **not** upsert a partial window. The minimal correct shape:

* Either `_page_candles` returns `(collected, complete: bool)` and `data.py:1366` upserts only
  when `complete` is true; or
* keep the raise, and at the `data.py:1366` call site guard the fetch in `try/except
  RateLimitExhaustedError` → log, **skip the upsert**, and fall through to
  `store.read(pair, interval, since=since, until=until)` (`data.py:1374`), which is the
  read the store could already serve.

The second is preferred: it makes the store leg's value proposition real instead of theoretical
and it needs no signature change. It also turns the existing `else:` branch at `data.py:1368-1374`
("no candles — reading whatever the store already has") from an *unreachable* path into the
*intended* one.

### Step 3 — `data.py:1383`: distinguish "store empty" from "store rate-limited"

`NotEnoughDataError(1, 0, what="OHLC candles")` at `data.py:1383` fires when the read is empty.
If the fetch leg was rate-limited **and** the store came back empty, the honest error is
`RateLimitExhaustedError` (we were refused and have no data), not "not enough data" (we had a
normal, merely short, window). Chain the original as `__cause__` so the run log keeps both.

### Step 4 — `engine.py:57-74`: reason codes, per §6.2.

### Step 5 — upstream `kraken-python` (separate change, separate lock bump), per §5(b):

* add `"EService: Throttled"` to `_RATE_LIMIT_MARKERS` (`transport.py:45-48`);
* drop or verify the stale `"EGeneral:Too many requests"`;
* parse the `EService` timestamp onto `RateLimitError` as `retry_after`
  (`kraken_api/errors.py:31`);
* mount `HTTPAdapter(max_retries=Retry(..., allowed_methods={"GET"}, status_forcelist={429,500,502,503,504}))`
  at `transport.py:114` — **`allowed_methods={"GET"}` is mandatory**, per the nonce hazard in §2.3;
* bump `flake.lock` to the new rev in `kraken-python`.

---

## 8. What must NOT regress

1. **`data.py:1364`'s store leg must not re-page when the store can serve the read.** This is the
   strongest single argument for the §7 Step 2 shape. Today a transient 429 aborts a read the
   store already held. A naive "retry harder" fix makes this *worse* — more waiting, still an
   abort. The correct outcome is: fetch fails → **skip the upsert** → `store.read` still answers.
   Assert this in a test: a source that raises `RateLimitError` on page 1 against a
   **pre-populated** store must return that store's rows, not raise.
2. **A partial page-fetch must never be silently persisted as a complete window.** No path may
   reach `store.upsert` (`data.py:1366`) with a `complete=False` collection. A store seeded with
   3-of-6 pages looks identical, on the next run, to a complete seed — the exact failure
   `tools/store_guard.py`'s whole docstring exists to prevent ("a fallback-csv seed reports
   success, leaves a directory that *looks* seeded"). Same anti-pattern, same reason.
3. **Pagination semantics are load-bearing and already tested.** `data.py:1108` `if last == 0 or
   not batch` and `data.py:1111` `cursor = last` must not change; the boundary-duplicate drop at
   `data.py:1183-1184` (`~df.index.duplicated(keep="first")`) depends on it. Every fake returns
   `last=0` and must still terminate on page 1.
4. **No real sleeping in tests.** 9 fake `ohlc` implementations exist across 6 test files (see
   §7 Step 1). A non-injectable `time.sleep` turns any future failure-path test into a
   multi-second test and makes the suite flaky.
5. **`pages` stays the bound.** `data.py:1112` `pages: int = 6` is the operator's knob and the
   `ValueError` at `data.py:1101` (`pages < 1`) is a tested contract. Retries are per *page*;
   they must not multiply the number of pages fetched.
6. **Non-retryable errors must stay non-retryable.** `ConfigurationError` /
   `AuthenticationError` (`kraken_api/errors.py:10,14`) mean a wrong key — retrying 5× just
   delays the real error by a minute. Neither is a `RateLimitError`, so keying on the tuple in
   §7 Step 1 gets this right; do not widen it to bare `Exception`.
7. **POST must never be auto-retried** (§2.3, nonce at `transport.py:157`/`:170-178`).
8. **`derived-column parity between the two legs** — `tests/test_rl_data_store.py:687`
   (`test_store_and_live_legs_derive_identical_columns`) and `:650`
   (`test_store_read_leg_derives_the_ohlcv_scalars`) assert the store and fetch legs produce an
   identical column set. `add_derived_ohlcv_features` runs *after* the fetch/store fork in both
   (`data.py:1186`, `data.py:1387`), so a retry change must not move it.

---

## 9. Where this belongs in the repo

| Concern | Home | Why |
|---|---|---|
| Retry policy + resume-from-cursor | `data.py:_page_candles` (`data.py:1071-1111`) | The one function both legs share, by design (`data.py:1086-1088`). |
| The new error type | `data.py`, beside `NotEnoughDataError` (`:220`), `MarketDataStoreUnavailableError` (`:237`), `SignalTickerMismatchError` (`:367`) | All three are `ValueError` subclasses declared there. `errors.py` is a pure re-export (`:1-29`) and holds none of them. |
| Engine visibility | `engine.py:57-74` + `strategies/sma.py:72-76` | The only two consumers of the tick dict. |
| Not a store-guard concern | — | `tools/store_guard.py` is a pipe-stage refusal for seed reports; reuse its message style (name the recipe, name the fix), not its mechanism. |
| Upstream markers/`retry_after`/`GET`-only adapter | `kraken-python` `transport.py:45-48`, `:114`, `kraken_api/errors.py:31` | Separate repo, **separate lock bump** (`flake.lock` rev `81de5974`). |

**Dependency verdict, stated plainly:** the minimal fix requires **no new dependency** —
`urllib3` 2.8.0 and `requests` 2.34.2 are already in `.venv`, and §7 Step 1 is ~25 lines. If the
architect would rather not hand-roll, `tenacity` (9.1.4, Apache-2.0, zero deps, actively
maintained, and whose `wait_random_exponential` implements AWS Full Jitter by direct citation) is
the one library to take, and `backoff` and `httpx` are both rejected — one archived, one without
status-code retry.

---

RESEARCH COMPLETE
