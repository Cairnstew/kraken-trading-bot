# RESEARCH-3 — Candidate 3 (exogenous seams, ops) + Candidate 4 (fetch reliability)

Read-only survey, researcher 3 of 3. Written against the working tree on disk
(2026-09-30). Own file; does not touch RESEARCH.md.

Scope: **Candidates 3 + 4** from the fresh AUDIT.md.

---

## CANDIDATE 3 — Operations: the three signal seams are unscheduled, staleness-blind, and not flake inputs

### 3.1 Maintenance status of each seam

All three signal siblings ship exactly one pull path (`cli.py pull`) and a
flake with `packages` + `devShells` only — **no `nixosModules`, no timer**:

| Sibling | CLI | Scheduling | Flake module | Notes |
|---|---|---|---|---|
| `ticker-news-signals` | `pull --ticker --output --lookback-hours` | none | none | flake uses an **overlay** for `gnews` + `vaderSentiment` (not in nixpkgs) |
| `kraken-funding-rates` | `pull --pair --output [--append]` | none | none | plain `buildPythonPackage`; `extract --all` exists |
| `kraken-social-signals` | `pull --ticker --output --lookback-hours` | none | none | plain `buildPythonPackage` |
| `kraken-market-data` (**the exemplar**) | `market update --pair --interval` | **systemd service+timer** | **yes** (`nix/module.nix`) | per-(pair, interval) unit; `DynamicUser`, `StateDirectory`, `OnCalendar *-*-* *:*:30`, `Persistent=true` |

The only scheduler in the family is `kraken-market-data/nix/module.nix`:

- one `systemd.services.kraken-market-data-<pair>-<interval>` per pair
  (`Type=oneshot`, `DynamicUser=true`, `StateDirectory`, `ExecStart` =
  `pkg/bin/kraken-market-data market update --pair ... --store dataDir`);
- a matching `systemd.timers.*` with `OnCalendar` (default `*-*-* *:*:30`),
  `Persistent=true`, wired `wantedBy = timers.target`.

Key detail: `Persistent=true` means a missed run fires at the next boot/start —
exactly the property a funding/∂‑news poller needs. The market-data module is
the reference implementation for Candidate 3; the other three just never got
one.

### 3.2 The bot flake cannot run the three CLIs at all

`flake.nix:4-8` lists **only** `kraken-python` + `kraken-market-data` as
inputs. The devShell puts `kraken-market-data` on `PYTHONPATH`
(`flake.nix:71`) and imports `kraken_api`, `market_data` — but there is no
path to `ticker_news_signals`, `kraken_funding_rates`, or
`kraken_social_signals`, so `nix develop` cannot run their `pull` commands and
the config-default `null` signal files stay `null`.

Patch shape (additive, no bot code risk): three new `inputs`
(`github:Cairnstew/<repo>`), each `PKG.outPath` on `PYTHONPATH` in the
devShell like `kraken-market-data` already is (`flake.nix:71`). **Caveat:**
`ticker-news-signals`' package build needs its overlay (`nix/gnews.nix`,
`nix/vader-sentiment.nix`); the bot flake would have to import that overlay or
pin a prebuilt package. Funding and social are plain `buildPythonPackage`
and drop straight in.

### 3.3 Staleness is silent; no age/asof column

`merge_extra_features` (`data.py:168-171`):
```python
merged = signal_df.reindex(ohlc_index)
for col in available_cols:
    df[col] = merged[col].ffill().fillna(0.0).values
```
A `funding_rate`/`sentiment_score`/`fng_index` from 5 days ago is byte-identical
to one from 5 minutes ago. Staleness-relevant facts found:

- `novelty_flag` is computed **only at pull time** — `ticker-news-signals/
  pipeline.py:159` `_is_novel(items, current)`; once written to JSONL the flag
  never decays. The bot sees "novel today" for a week.
- Funding is a per-moment snapshot: `kraken-funding-rates/export.py` floors
  the timestamp to the hour on **write** (`:149-150`), and funding settles
  ~8-hourly (`models.py:25`, `funding_rate_coefficient` default 8 = 3× daily).
  Pulling hourly and ffill'ing 8 identical values is the current semantics.
- Social's missing-F&G-day is `fng_index: 0` (`pipeline.py:38,136` `_FNG_MISSING=0`)
  — the zero-fill in `data.py:171` then makes absence indistinguishable from
  "extreme fear" (already flagged as Candidate 6).

**Approach — a `signal_age_hours` column.** Two clean options, both reduce to
per-(ticker, hour) scalar columns that flow through the existing `_SIGNAL_COLUMNS`
allow-list:

1. **Emitter-side (best provenance):** each sibling writes `fetched_at` (or a
   per-record `age_at_pull`) next to the signal in its JSONL. `merge_extra_features`
   then merges it like any other column and the agent/export CSV can discount
   `age > k` rows. Minimal per-sibling change; the bot change is one allow-list
   entry + forwarding (both copies of `_SIGNAL_COLUMNS` are already duplicated,
   `data.py:50` + `features.py:36`).
2. **Merge-side (zero sibling change):** in the loop above, also emit
   `(now - merged_timestamp).total_seconds()/3600` ffilled alongside the
   values — i.e. keep the ffilled **row timestamp** as an `asof` column and
   let the feature pipeline convert it to `signal_age_hours`. This is the
   pandas `merge_asof` idea in spirit without replacing the existing
   index-based join.

**Reference:** pandas `merge_asof`
(https://pandas.pydata.org/docs/reference/api/pandas.merge_asof.html) is the
canonical backward-asof join (each OHLCV row gets the most-recent-prior signal
plus a distance). The repo already uses `reindex().ffill()` which is the
index-aligned special case; adding the ffilled timestamp back as a column is
strictly less invasive and drops the same age signal. Directionally, anything
fancier (SQL `ASOF JOIN`) is unnecessary at one JSONL file per ticker.

**Score by cost-to-protect:** the audio pitch in AUDIT.md is "scheduler +
`signal_age_hours` so the agent can discount staleness; protects the three
completed seams". The scheduler is copy-the-market-data-module (same module
shape, `OnCalendar=*-*-* *:00:00`, `Persistent=true`) — near-zero new code
with the flake-input add as the only prerequisite. The staleness column is
~10 lines. Both together are the cheapest way to make the three seams
trustworthy.

### 3.4 Forwarding `funding_rate_prediction` / `index_price` — is it a real win?

`kraken-funding-rates/models.py` `FundingSnapshot` has 12 fields; the siblings'
JSONL carries the full `to_dict()`. `_SIGNAL_COLUMNS` currently forwards only
`funding_rate`, `basis`, `open_interest`.

- `funding_rate_prediction`: **real win, zero cost.** It is the exchange's own
  predicted *next* funding rate — the current `funding_rate` is stale-by-design
  (settles 8-hourly), so the prediction is plausibly the more predictive scalar
  of the two at hour resolution. Adding it is one string in both
  `_SIGNAL_COLUMNS` tuples. It is already written into every funding JSONL
  record (verified in `models.py:54-65,76,108`).
- `index_price`: **marginal.** It is the perp's spot index; for the RL
  observation it is a dollar-denominated price level, collinear with `close`,
  `mark_price`, and `basis` (which is already computed from index). Given
  Candidate 1 (no z-scoring applied), feeding another dollar-scale column
  into the raw Box adds scale-noise, not signal.
- `bid`/`ask`/`vol24h`: not in the allow-list, and better left to the
  microstructure recorder (Candidate 5) than bolted onto the signal seam.

So the concrete win is forwarding **one** column (`funding_rate_prediction`),
not the whole snapshot. The `_SIGNAL_COLUMNS` duplication (data.py vs features.py)
means a widening must touch both places; there is no test locking the two in
sync (confirmed in AUDIT §3/§5).

---

## CANDIDATE 4 — Fetch-layer reliability: no retry, no throttle, uncached re-fetch

### 4.1 Maintenance status

`kraken-python/transport.py` — the only transport in the family without
retry/backoff:

- `min_interval` defaults `0.0` (constructor `transport.py:108`), `KRAKEN_MIN_INTERVAL`
  exists (`auth.py:42`) but also defaults `0.0` (`auth.py:73,104`); `_throttle`
  (`transport.py:180-186`) is a no-op at 0.
- `_request` (`transport.py:188-261`): `requests.RequestException` → logged +
  re-raised immediately (`:210-219`); envelope errors → `RateLimitError`/`APIError`
  raised immediately (`:249-250`). Timeout=30, no retry.
- The `requests.Session` is a plain session (`:114`); no `HTTPAdapter`/`Retry`.

**Contrast — the same family already ships the pattern.** Both siblings'
clients wrap requests with retry/backoff + throttle:

- `kraken-market-data/market_data/client.py` `_public`: `self._limiter.wait()`
  (rate limiter) then `for attempt in range(max_retries+1)` with
  `backoff_seconds(attempt, base=retry_backoff)` on `RequestException`/`ValueError`
  (`:115-142`).
- `kraken-funding-rates/kraken_funding_rates/client.py` `_get`: exponential
  backoff `_RETRY_BACKOFF * 2**attempt` on HTTP 429, ≥500, and
  `requests.RequestException` (`:70-95`), `max_retries=3`.

So Candidate 4 is **not** a design problem — kraken-python's transport simply
predates its own siblings' clients. The fix has an in-family reference.

### 4.2 Callers that pay for the gap

- **Engine loop** (`engine.py`): per pair per 60 s, `ticker` + `ohlc` +
  `order_book` = 3 unthrottled public calls; no cache; `candles[-100:]`
  re-fetched every tick and ~620 bars discarded.
- **Paper trader** (`paper_trade.py:280-298`): `pages=2` OHLC fetch **every
  tick**; `_build_observation` recomputes the whole window
  (`pipeline.compute` on the full frame, `:315`); additionally
  `merge_extra_features` re-parses each sibling JSONL **on every call**, 3×
  per `read_ohlc_dataframe` (`data.py:328-330`), O(file) per paper tick.
  `data.py:401-406` already carries the append+tail-read TODO.

### 4.3 Approach — three layered, all reference-only

1. **Retry + backoff in `transport._request`.** Minimal, dependency-free loop
   mirroring `kraken-funding-rates/client.py:70-95`: retry on
   `requests.RequestException`, on HTTP 429/5xx, and on `RateLimitError`, with
   exponential backoff (base ~1 s, capped) + small jitter, `max_retries`
   default ~3. Non-retryable: 4xx envelope errors other than 429, malformed
   envelope, auth failures. This single change hardens **every** sibling and
   every bot path (engine, train, backtest, paper) at once.
   Libraries: `tenacity` and `backoff` are the two standard decorators, but both
   are new deps; the 25-line hand-rolled loop is already proven in-repo and
   keeps kraken-python dependency-light (it currently depends on requests +
   python-dotenv only).
2. **Throttle default.** Flip `min_interval` default from `0.0` to something
   small but non-zero (e.g. `0.05`) in `transport.py:108` and/or
   `auth.py:73,104` — or land it purely in the env default so two clients
   sharing one key don't fight. Kraken spot REST is ~15-20 calls/s; the engine's
   3 calls/pair/60 s is nowhere near that, but the paper trader's 2 pages/tick
   and any `extract --all` fan-out is.
3. **TTL cache for the 100-bar window.** A tiny `(pair, interval, since) ->
   (candles, monotonic_ts)` map with a 60 s TTL in the engine and paper-trader
   fetch path kills redundant re-fetches; combine with the `data.py:401-406`
   append+tail TODO for the paper window. Standard library answer is
   `cachetools.TTLCache` (https://cachetools.readthedocs.io/en/latest/) or a
   5-line `time.monotonic()` dict; both are reference options, no new dep
   strictly needed. Note `functools.lru_cache` is wrong here (no TTL, no
   clock-based expiry).

**Score by cost-to-protect:** pure code, zero sourcing; the transport fix is a
prerequisite that makes every other fetch (Candidates 2/3 included) cadence-safe.
The 100-bar TTL cache is independent and immediate. The paper-trader
recompute is the biggest per-tick waste and the append+tail TODO already names
the fix.

---

## Per-candidate reduction to shape

- **Candidate 3** reduces cleanly to per-(ticker, hour) scalar columns: a
  scheduler (systemd timer, `Persistent=true`) produces/refreshes the JSONL;
  `signal_age_hours` (and optionally `fetched_at`) is another per-hour scalar;
  `funding_rate_prediction` joins the allow-list. All three land in the existing
  `merge_extra_features` → `_SIGNAL_COLUMNS` → `signals` group path — no
  architectural change, only tuple widening (both copies) + one module.
- **Candidate 4** is per-fetch: one transport function (`_request`) gains
  retry/backoff; one default (`min_interval`) changes; one small cache object
  (TTL dict) at the two refetch call sites. No column shape change at all.

## Recommendation ranking (cost-to-protect vs the AUDIT pitch)

1. **Transport retry/backoff + non-zero `min_interval` default in kraken-python**
   — cheapest, protects every path and sibling; in-family reference exists
   (`kraken-funding-rates/client.py`). Candidate 4 core.
2. **Forward `funding_rate_prediction`** (one allow-list entry ×2) — zero-cost,
   adds the exchange's next-funding estimate the current `funding_rate` lacks.
3. **`signal_age_hours` staleness column** (merge-side, or emitter-side
   `fetched_at`) so ffill no longer lies — protects the three completed seams.
4. **Flake inputs for the 3 signal siblings + market-data-style systemd
   timer** (`OnCalendar=*-*-* *:00:00`, `Persistent=true`, `DynamicUser`,
   `StateDirectory`) — the "scheduler half"; zero new code, requires the flake
   inputs (news needs its gnews/vader overlay) and is the one genuinely new
   operational surface.
5. **TTL bar cache + paper-trader append/tail** — efficiency win, independent,
   medium effort.

`index_price` forwarding is **not recommended** (collinear dollar-scale
column; interacts badly with open Candidate 1 normalization). `tenacity`/
`backoff`/`cachetools` are cited as library references; the in-repo hand-rolled
patterns are preferred over new deps.

RESEARCH COMPLETE