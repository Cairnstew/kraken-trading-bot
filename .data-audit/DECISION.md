# DECISION.md — Phase 3 architecture decision

**Pass:** 2026-10-02 · **Phase:** 3 (architect) · **Author:** architect
**Inputs:** [`AUDIT.md`](AUDIT.md) (7 gaps, `4fd3b77`), [`RESEARCH.md`](RESEARCH.md)
(synthesis + 24-check receipt), [`RESEARCH-1.md`](RESEARCH-1.md) / [`RESEARCH-2.md`](RESEARCH-2.md) /
[`RESEARCH-3.md`](RESEARCH-3.md).

This file is the only thing this pass wrote. No source file was modified.

Every `file:line` below was re-derived against the tree at `e027621` this pass, and every
number was **measured**, not quoted — including a live call to the Kraken Futures history
endpoint, which re-confirmed RESEARCH-2 §1.5 independently (see §7.1).

---

## 1. Decision

| | |
|---|---|
| **Outcome type** | **`IMPROVE-EXISTING`** |
| **Gap** | **G2** — the checked-in exogenous signal log is one record deep |
| **Library** | **none.** No new dependency. `requests` is already in `kraken-funding-rates/pyproject.toml:11`. |
| **Primary repo** | `~/Projects/kraken-funding-rates` — a new `backfill` subcommand |
| **Wiring repo** | `~/Projects/kraken-trading-bot` — one `just` recipe, two comments, one string |

**In one sentence:** add `--backfill` to `kraken-funding-rates`, reading Kraken's own keyless
`/historical-funding-rates`, appending the **full 13-key record** into the **existing**
`signals/eth_usd_funding.jsonl` — which takes `funding_rate` from **2 distinct values on 13 of
721 bars** to **720 distinct values on 721 of 721 bars** at an **unchanged observation width
of 57**.

---

## 2. Why `IMPROVE-EXISTING`, considered as a first-class option

`NEW-DATA-SOURCE` was evaluated first and is not a weak reading. RESEARCH §1.2's argument
against a new repo is specifically about **G1**, and I am not obliged to accept it — but for
**G2** it is stronger than "already wrapped", and it is a supply-chain fact as much as a
plumbing one:

1. **The producer's plumbing for this record already exists and is already correct.**
   `export.py:120-156 write_jsonl` already hour-floors the timestamp; `models.py:51-83
   to_dict()` already emits the 13-key record with `ticker` = the **spot** pair — and its
   docstring already explains *why*, naming the exact failure I would otherwise have to
   rediscover (a `PF_ETHUSD` ticker would trip `signal_require_ticker: true` on every
   record). That reasoning was paid for in commit `935dfc0` and is reusable verbatim.
2. **The new source is not a new venue, not a new host, and not a new stack.**
   `GET futures.kraken.com/derivatives/api/v3/historical-funding-rates` is the **same host**
   the client already calls twice (`client.py:105 /tickers`, `client.py:157 /instruments`),
   on the same `_BASE_URL` (`client.py:23`), through the same `_get` that already retries
   429/5xx with backoff (`client.py:53-95`, `_MAX_RETRIES = 3` at `:25`).
3. **The delta is a subcommand, not a project.** Measured against the real files:
   `client.py` 172 lines, `cli.py` 148, `export.py` 192, `models.py` 226. The whole change is
   one method, one dataclass, one extractor, one subparser, plus tests. RESEARCH-2 §8 puts it
   the same way. A new repo would add a flake, a Nix module, a CI pipeline and a license
   question to house ~90 added lines inside an existing 738-line package.
4. **Supply chain — the decisive point, and it is the mirror image of RESEARCH §1.10.**
   `flake.nix:4-7` pins exactly two siblings: `kraken-python` (lock rev `81de5974…`) and
   `kraken-market-data` (lock rev `055d7f6…`). **`kraken-funding-rates` is not a flake input
   at all.** `justfile:157` and `systemd/kraken-trading-bot-funding.service.in` invoke it as
   `nix run ~/Projects/kraken-funding-rates#kraken-funding-rates`, and its own
   `flake.nix:26` builds `src = ./.` — a path build with no self-pin. So an edit there is
   **live in the recipe the operator types, immediately, with no `flake.lock` bump and no
   push to a GitHub remote**. RESEARCH §1.10 established that an edit to the *pinned*
   `kraken-python` would be live under pytest and **invisible to `just bench`** until a lock
   bump. This target sits on the good side of that asymmetry; G3's upstream half sits on the
   bad side.
5. **Nothing in the consumer needs writing.** `_SIGNAL_COLUMNS` (`features.py:94-125`) already
   carries `funding_rate`; `merge_extra_features` (`data.py:591-842`) is the hardened seam;
   `first_tradable_index` (`features.py:176-206`) already exempts
   `POINT_IN_TIME_EXOGENOUS_COLUMNS`, and its docstring already records the exact
   2026-10-01 bug a dense series was feared to re-expose. A new source landing on an already-
   complete consumer is an `IMPROVE-EXISTING` by construction.

**Which gap *would* have justified a new repo, and why not now.** AUDIT G7's on-chain /
macro / options-surface set is the honest `NEW-DATA-SOURCE` territory — different host,
different auth model, no existing sibling, and `data.py:144-156` + `features.py:94-125` are
genuinely additive there. G1's only backfill route is **Tardis.dev, paid** (RESEARCH-1 §3.8,
the sole verified source of Kraken L2 history from 2019-06-04). Neither is a good Phase-4
slice: G7 has no consumer at all today (`Directness: none *today*`), and G1-with-Tardis adds
a paid dependency to buy history for a feature whose consumer is two tuple edits away. If a
new repo is ever the right answer, **G1-on-Tardis is the one**, and it should be its own pass
with its own budget conversation.

---

## 3. Why G2, against the RL pipeline's real shape

### 3.1 What the agent actually sees today, measured

On a 721-bar 60-minute frame ending at the record's right edge, with the **real**
`signals/eth_usd_funding.jsonl` (1 line) and `feature_windows: [1, 4, 24]` plus all five
`feature_groups` (`configs/default.yaml:43,48`):

| Observation column | nonzero bars | distinct values | column std over the frame |
|---|---|---|---|
| `funding_rate` | **13 / 721** | **2** | **0.00336** |
| `basis` | 13 / 721 | 2 | — |
| `open_interest` | 13 / 721 | 2 | — |
| `funding_rate_prediction` | 13 / 721 | 2 | — |
| `vol24h` | 13 / 721 | 2 | — |
| `spread` | 24 / 721 | 2 | — |
| `signal_observed` | 13 / 721 | 2 | — |

`funding_rate` is **98.2% the literal constant `0.0`** (the historical fill at `data.py:816`).
After z-scoring, 708 of 721 rows carry the *same* value in that column. That is a
near-dead input to the policy: it is not "sparse", it is constant.

**DEV-1 — correction to AUDIT.md.** AUDIT.md:153 says *"≈709 of 721 bars read these six
columns as their historical `0.0` fill"*, i.e. 12 observed. Measured: **13 of 721**. The
record sits at its own bar plus the 12-hour carry (`signal_max_age_hours: 12`), and because
the record is at the *right edge* of a trailing window the carry is clipped by the frame end,
not by the bound. 13 is the number the gate should use.

### 3.2 What it becomes after the backfill, measured

Same frame, same config, same seam — a `backfill` file appended with the full 13-key record:

| Observation column | nonzero bars | distinct values | column std over the frame |
|---|---|---|---|
| `funding_rate` | **721 / 721** | **720** | **0.02332** (×6.9) |
| `basis` | 13 / 721 | 2 | — *(unchanged)* |
| `open_interest` | 13 / 721 | 2 | — *(unchanged)* |
| `funding_rate_prediction` | 13 / 721 | 2 | — *(unchanged)* |
| `vol24h` | 13 / 721 | 2 | — *(unchanged)* |
| `spread` | 24 / 721 | 2 | — *(unchanged, byte-for-byte)* |
| `signal_observed` | **721 / 721** | 2 | — |

And on the source series itself, over the same trailing 721 hours: `max` inter-record gap
**1.0 h**, **721 distinct** rates, `min −0.03619`, `max +0.12163`, **619 positive / 102
negative** — it genuinely crosses zero, so this is a distribution, not a level.

**This is the largest single change to what the agent sees available anywhere in this repo.**
G1 adds a column; G3 adds none; G4 adds none. G2 replaces a constant column with a real
one across an entire training run.

### 3.3 Why it is the *cleanest* of the three top gaps, not merely the largest

* **`ohlcv_interval_minutes: 60` (`configs/default.yaml:11`) makes the hourly series map 1:1
  onto the bar grid** — no resampling, no aggregation choice, no fudge factor. Contrast G1:
  a `/Depth` response is an *instant* with **no timestamp of its own** (RESEARCH-1 §4.4), so
  landing it on a bar grid forces a last-sample-vs-mean decision that has to be settled
  before anything can be written. G2 has no such decision.
* **Width is provably unchanged, so no model artifact is invalidated.** `available_cols`
  (`data.py:765-769`) is an intersection, so the record's key *set* is load-bearing — but
  writing the same 13 keys the live producer writes keeps the intersection identical.
  `check_feature_width` (`features.py:483`) compares by **name** against the artifact's own
  `feature_names`, so 57 → 57 means every existing
  `models/{TICKER_ID}/{model_name}/normalization.npz` stays valid and no
  `FeatureWidthMismatchError` can fire. G1, by contrast, **must** edit `features.py:94-125`
  and `features.py:149` — and `features.py` is in `tools/audit_checks.py`'s `PY_FILES`
  executable-AST guard.
* **`just audit-verify`'s `executable-ast` check stays MATCH.** `features.py` and the seam
  logic in `data.py` are untouched. The one `data.py` edit (§5.2) is a **string literal**
  inside `_SIGNAL_CHANNELS`, and `executable_ast` blanks docs and strings by design
  (`audit_checks.py:186`).
* **Not episode truncation, and provably so.** `first_tradable_index` measured **24** before
  and **24** after, on both frames. RESEARCH §1.6's reading is confirmed.
* **Keyless, one HTTP request, no new dependency, no scraping.** Measured §7.1.

### 3.4 The exact landing point

There are three, and **all three already exist** — that is the point.

1. **Config key** — `funding_features_file`, `configs/default.yaml:92`. Already non-null,
   already a "DECLARED INTENT to use the channel" per its own comment (`:85-91`). **No new
   config key.**
2. **Read site, per ticker, per run** — `train.py:220-221` and the equivalent in `backtest.py`
   pass `cfg.get("funding_features_file")` and `cfg.get("signal_max_age_hours")` into
   `read_ohlc_dataframe`, which iterates `_SIGNAL_CHANNELS` (`data.py:144-156`) at both the
   fetch leg (`:1194`) and the store leg (`:1387`) and calls `merge_extra_features`
   (`data.py:591`). The seam floors to the hour (`:759`), intersects against `_SIGNAL_COLUMNS`
   (`:765-769`), bounds the carry by measured age (`:798-804`), and zero-fills absence
   (`:816`).
3. **Feature-engineering steps that turn it into observation columns** —
   `_add_signals_features` (`features.py:1017`, passthrough at `:1052-1059`) for
   `funding_rate` / `basis` / `open_interest` / `funding_rate_prediction` / `vol24h`, and
   `_add_microstructure_features` (`features.py:986-1015`) for `bid`/`ask → spread`. Then
   `normalization.npz`'s `feature_names` (`features.py:377`) → `environment._builtin_features`
   (`environment.py:498`).

**No step of the pipeline is modified to read this feature. It is read today.** That is the
cleanest possible outcome shape and the reason G2 beats G1 despite G1's larger information
content.

### 3.5 Honest cost of G2, stated up front

**§1.7 is respected literally: this recovers 1 of 6 columns, not 6.**

| Column | Verdict | Evidence |
|---|---|---|
| `funding_rate` | **recovered exactly** | bit-identical, hourly, 366 d — §7.1 |
| `basis` | **NOT recovered.** Kraken's history has no `markPrice`/`indexPrice`, so `basis = (mark−index)/index` is not derivable. Writing `relativeFundingRate` here is a **silent unit substitution** and is forbidden. | field union is exactly 3 |
| `funding_rate_prediction` | **NOT recoverable, provably** — a forward estimate cannot be in a history | absent |
| `open_interest` | **NOT recoverable** — Kraken publishes no historical OI | 3 spellings 404 |
| `vol24h` | **NOT recoverable at sane cost** — trade history is 100 trades/request | 10⁵–10⁶ requests |
| `spread` | **NOT recoverable** — the trade payload has no bid/ask at all | absent |

The Phase 6 receipt and the config comment must say **"1 of 6"** in those words.

---

## 4. Deviation: do **not** widen `_SIGNAL_COLUMNS` for `relativeFundingRate`

RESEARCH-2 §2 recommends adding `relativeFundingRate` as a **new** column rather than
overloading `basis`, describing it as optional. **I am declining to add it, and here is the
measurement.**

Over the trailing 721-hour window, `corr(funding_rate, relativeFundingRate) = **0.99753**`.
`relativeFundingRate` is `fundingRate / indexPrice` — RESEARCH-2 measured the ratio at 4147.45
and 2664.89 across the span, tracking the ETH price, so it is the funding rate re-expressed
per dollar of notional. Adding it to the observation buys **+1 width** (a new column, new
`feature_names`, a new artifact contract, a `FeatureWidthMismatchError` risk against every
existing model) for a column that is **0.9975 collinear with one you already have**.

So: **record it in the JSONL, do not widen the allow-list.**
`data.py:765-769` intersects against `_SIGNAL_COLUMNS`, so a key outside it is carried in the
file for any future analyst and costs the observation **zero** columns. That is the strictly
dominant third option between RESEARCH-2's two, and it keeps the width gate simple.

---

## 5. Improvement scope — repos and in-scope files

### 5.1 `~/Projects/kraken-funding-rates` (primary)

| File | Change | Not in scope |
|---|---|---|
| `kraken_funding_rates/client.py` | **+1 method** `historical_funding_rates(symbol)` calling `_get("/historical-funding-rates", {"symbol": symbol})`, returning `data.get("rates", [])`. Send **no** `since`/`until` — they are ignored (RESEARCH-2 §3(b); the endpoint is a ~366-day rolling cap, not paginated). | **`_get` must not be touched.** No retry work here — that is G3's runner-up, and a blanket retry in a trading client risks POST replay against a stale nonce (RESEARCH §1.15). |
| `kraken_funding_rates/models.py` | **+1 dataclass** `FundingHistoryRow` (`symbol`, `spot_pair`, `ticker`, `timestamp`, `funding_rate`, `relative_funding_rate`) with symmetric `to_dict()`/`from_dict()`. `ticker` = **spot pair**, per the existing `FundingSnapshot.to_dict` docstring. | **`FundingSnapshot`'s 11 mandatory fields must not be made optional.** That would let a 3-key history row masquerade as a full snapshot. |
| `kraken_funding_rates/export.py` | **+1** `@_register("funding_history") extract_history(symbol, since=None, until=None, client=None)`. **Client-side** date filtering. **Reuse `write_jsonl`** — widen its timestamp-flooring to any row exposing a `timestamp`, do not fork a second writer. | Registry keys `funding_snapshot`/`funding_all`/`funding_many` unchanged. |
| `kraken_funding_rates/cli.py` | **+1** `backfill` subparser (`--pair`, `--since`, `--until`, `--output/-o`, `--append`) and `cmd_backfill`. `pull`/`extract`/`snapshot`/`list` unchanged. | No change to existing subcommands' flags or output. |
| `tests/test_client.py`, `tests/test_export.py`, `tests/test_models.py` | New cases: endpoint spelling, client-side filter, the 366-day warning, `ticker` = spot pair, full key set. | Existing tests must pass untouched. |

**Explicitly out of scope for this repo:** `errors.py`, `logging_config.py`, `utils.py`,
`flake.nix`, `flake.lock`, `nix/`, `scripts/verify_live.py`, `README.md` beyond a short
usage note, and any change to `pull`.

### 5.2 `~/Projects/kraken-trading-bot` (wiring + honesty only)

| File | Change | Note |
|---|---|---|
| `justfile` | **+1 recipe** `funding-backfill`, beside `funding-pull` (`:152-158`), mirroring its `nix run …#kraken-funding-rates --` shape. Also add it to the recipe list in the header (`:11-12`). | `grep -nE "backfill\|replay\|history" justfile` returns only the *store* recipes (`:180-266`) — confirmed no funding backfill exists. |
| `configs/default.yaml:117-126` | Correct the **factually wrong** claim: the comment says *"kraken-funding-rates settles ~8-hourly"*. Measured cadence is **hourly** (gap histogram `{1.0h: 8783, 2.0h: 6, 3.0h: 1}` over 8,791 records). The same wrong claim is in `systemd/kraken-trading-bot-funding.timer`. Also record, in-file, the recorder-start boundary and the `signal_observed` semantics change (§6). | **Do not change `signal_max_age_hours: 12`'s value** — see DEV-2. |
| `systemd/kraken-trading-bot-funding.timer` | Same one-line cadence correction. | No schedule change. |
| `kraken_trading_bot/rl/data.py:150` | The `_SIGNAL_CHANNELS` producer hint for the funding channel is the string `"just funding-pull"`. It becomes `"just funding-backfill # then just funding-pull"`, so `SignalFileNotFoundError` names the recipe that fills history. | **String literal only** — invisible to `executable_ast`, so `just audit-verify`'s executable-AST check stays MATCH. |

**Explicitly out of scope for this repo — no adjacent rewrites:**
`kraken_trading_bot/rl/features.py`, the seam logic in `rl/data.py`,
`rl/train.py`, `rl/backtest.py`, `rl/environment.py`, `rl/registry.py`, `engine.py`,
`strategies/`, `tools/*`, `tests/`, and `configs/default.yaml:17-18` (`fee_rate`/`slippage`
are G6, a separate decision).

### 5.3 DEV-2 — overriding RESEARCH-2 §7.2 on `signal_max_age_hours`

RESEARCH-2 §7.2 notes 12 h is ~12× looser than the hourly series needs and calls fixing it
"a free fix alongside the backfill". **I am fixing the comment, not the value.** Reasons from
the consumer's own code:

* On a dense series the bound **cannot change anything observable**: every bar carries its own
  record, so `_signal_ages` (`data.py:544-588`) returns `0.0` on every bar and the mask at
  `:800` (`ages <= bound_hours`) passes for any bound ≥ 0. Measured `signal_age_hours` max
  after the backfill is **1.0** at a bound of 12. Lowering it to 1 h would produce the *same*
  observation on a backfilled window — so it buys nothing here.
* But it **would** change behaviour for any *pre-existing* model or forward-only file that
  relies on the 12-hour carry across missed hourly pulls, and `configs/default.yaml:113-115`
  documents that the 12 h is deliberately there to bridge more than one missed pull. Changing
  it is a **behavioural** change to a config key that already ships, bundled silently into a
  data-provenance change.
* The **comment** is the actual defect: a stated cadence that is wrong by 8× is what would
  mislead the next reader into widening it again. Correct that; leave the number alone and say
  in the comment that 12 h is a deliberate upper bound on a max-3 h-gap series.

---

## 6. Phase 5 contract — the integration sketch

**Transport: a shared local path, not a pipe.** The contract that already exists *is* a
shared local path, and the producer is already a separate repo on the same machine invoked by
absolute path (`nix run ~/Projects/kraken-funding-rates#kraken-funding-rates`). Reasons:

* `configs/default.yaml:92` already names the path; `justfile:157` writes it; the systemd unit
  writes it; `merge_extra_features` reads it. A pipe would add a second transport and break
  the shipped default's offline story (`market_data_store: null`, `configs/default.yaml:185`).
* The merge seam is **file-based by construction** — `merge_extra_features` takes a path
  (`data.py:593`), opens it (`:698`), and raises `SignalFileNotFoundError` naming the config
  key, the value as written, and the expanded path (`:689-694`). A pipe is not representable
  in that signature, and changing it is out of scope.
* Append-only JSONL is the producer's existing contract (`justfile:158 --append`).

**Record: one JSON object per `(ticker, floored UTC hour)`, 13 keys, appended to the existing
file.** Never a second file — `available_cols` is a per-file intersection (`data.py:765-769`),
so a separate file would narrow the observation (measured: **57 → 52**, §6.1).

| key | source | value |
|---|---|---|
| `ticker` | mapped spot pair | `"ETH/USD"` — **never** `PF_ETHUSD` (`signal_require_ticker: true`, `configs/default.yaml:137`) |
| `symbol` | history `symbol` | `"PF_ETHUSD"` |
| `spot_pair` | mapped spot pair | `"ETH/USD"` |
| `timestamp` | history `timestamp`, hour-floored | `"2025-10-01T08:00:00+00:00"` — normalised through the same `write_jsonl` path so the file holds **one** timestamp spelling, not two (RESEARCH-2 §6(b)) |
| `funding_rate` | history `fundingRate` | **the recovered column** |
| `relative_funding_rate` | history `relativeFundingRate` | recorded, **not** merged (§4) |
| `funding_rate_prediction` | not in history | `null` |
| `mark_price` | not in history | `null` |
| `index_price` | not in history | `null` |
| `basis` | not derivable | `null` — **never** `relativeFundingRate` |
| `open_interest` | not published | `null` |
| `bid` | not published | `null` |
| `ask` | not published | `null` |
| `vol24h` | not affordable | `null` |

### 6.1 The absent value is JSON `null` — DEV-3, and measured as observationally equivalent

RESEARCH-2 §6(2) recommended `0.0` to preserve today's "structurally zero" meaning. I
measured both, on the identical frame:

| absent value written | `n_features` | `spread` distinct | `spread` nonzero | `spread` max | non-finite obs cells |
|---|---|---|---|---|---|
| `null` | **57** | 2 | **24** | 3.695e-05 | **0** |
| `0.0` | **57** | 2 | **24** | 3.695e-05 | **0** |
| today (1 live record) | **57** | 2 | **24** | 3.695e-05 | **0** |

**They are observationally identical**, because `data.py:816 fillna(0.0)` already converts
either NaN or `0.0` to the same value, and the exact-duplicate-hour dedup at `data.py:755`
(`keep="last"`) lets the appended live record win the overlap hour wholesale. So this choice
is **purely about record honesty**, and I choose `null`:

* `write_jsonl` does `json.dumps(record, default=str)` (`export.py:153`), so `0.0` is written
  as `0.0` — **byte-identical to a genuine reading of exactly zero**. For `funding_rate` that
  is not hypothetical: the measured trailing window has 102 negative hours and crosses zero, so
  `0.0` is a plausible real value. A file that cannot distinguish "measured zero" from "never
  read" is the defect class this repo has already been bitten by.
* The repo's own declared semantics say zero *is* absence here: `features.py:28-48` puts
  `0.0` in `_NON_FINITE_INPUTS` and says so explicitly. Writing `0.0` for "not read" would
  contradict the pipeline's own vocabulary, and `_add_microstructure_features` coerces a `0.0`
  bid to NaN anyway (`features.py:1008-1015`), so `0.0` buys no semantics — only a
  fabricated-looking number in the file.
* `null` still keeps `relative_funding_rate` and every `null` field out of the observation for
  free, because `data.py:765-769` intersects against `_SIGNAL_COLUMNS` and
  `_SIGNAL_COLUMNS` does not contain them.

**The docstring must say which was chosen and why** (RESEARCH-2 §6(2)'s own instruction).

### 6.2 Scheduler

* **Newest hour:** unchanged — `systemd/kraken-trading-bot-funding.timer`,
  `OnCalendar=*-*-* *:17:00`, `Persistent=true`.
* **History:** `just funding-backfill`, run manually and documented as **needing periodic
  top-up**, because the server window is a rolling ~366-day cap that **ages out** — after ~1
  year the oldest records fall off the back. A daily or weekly invocation is belt-and-braces,
  not a new timer. RESEARCH-2 §8 records this; it belongs in the recipe's comment.

---

## 7. Acceptance criteria the Phase 6 gate will check

### 7.0 First: the two traps this repo has shipped defects through

**TRAP 1 — width is PER CONFIGURATION. There is no single width.** Because consumers are
presence-gated, the observation width is dynamic. Measured on a 721-bar 60m frame with
`feature_windows: [1, 4, 24]` and all five `feature_groups`:

| Configuration | `n_features` | the 6 funding columns present | `signal_observed` bars |
|---|---|---|---|
| all three `*_features_file` = `null` | **49** | 0 / 6 | n/a |
| shipped default, live 1-record file (**baseline**) | **57** | 6 / 6 | 13 / 721 |
| shipped default + `backfill --append`, full 13-key records (**target**) | **57** | 6 / 6 | **721 / 721** |
| full 13-key records minus `bid`/`ask` (loses `spread`) | **56** | 5 / 6 | 721 / 721 |
| ⚠️ **HAZARD** — backfill writing only `funding_rate` | **52** | 1 / 6 | 721 / 721 |

**Sane range: 49 – 57.** The gate must assert **57 for the shipped-default configuration** and
**49 for the all-null configuration**, and must treat the range as legitimate. A gate that
asserts a bare number taken from a synthetic fixture becomes a false gate — and note that
`features.py:494-496` **already contains one**: *"the shipped `configs/default.yaml` composes
52"*. Measured, the shipped default composes **57**. The docstring omits the signal-channel
contribution entirely. Recorded as **F-1** below; it is a docstring defect in a file this
decision does not edit.

**TRAP 2 — every criterion below names the artifact AND the literal token that must be true in
it.** A check that asserts a fact about a library without being bound to a substring in a
named file is the defect class that let two restored defects pass 11/11.

### 7.1 Producer correctness

| # | Criterion | Artifact | Literal token that must be present |
|---|---|---|---|
| A1 | The history endpoint is spelled **kebab-case**. Five other spellings return 404. | `kraken-funding-rates/kraken_funding_rates/client.py` | the string `"/historical-funding-rates"` — assert this **substring**, not merely "the backfill runs" |
| A2 | The field union is exactly `{fundingRate, relativeFundingRate, timestamp}`; `fundingRate` is written to `funding_rate` | `client.py`, `models.py` | `FundingHistoryRow`, `funding_rate`, `relative_funding_rate` |
| A3 | **`relativeFundingRate` is NEVER written to `basis`.** This is a silent unit substitution (`relativeFundingRate == fundingRate / indexPrice`, measured ratio 4147.45 / 2664.89 across the span). | `models.py` | `FundingHistoryRow` must exist AND no assignment of `relative_funding_rate` to `basis` anywhere in the file |
| A4 | `--since`/`--until` are filtered **client-side** and a `--since` older than the earliest returned record **warns with both dates named** | `export.py`, `cli.py` | `funding_history` registry key; the warning text |
| A5 | `ticker` is the **spot pair**, never `PF_ETHUSD` | `models.py` | `to_dict` emits `self.spot_pair` under `"ticker"` — the existing `FundingSnapshot.to_dict` docstring explains the failure mode; A5 asserts the *history* dataclass does the same |
| A6 | Timestamps are normalised through the **existing** `write_jsonl` hour-flooring path, so the file has **one** timestamp spelling | `export.py` | `write_jsonl` — assert no second writer function was added |
| A7 | Bit-identity: `funding_rate` for the checked-in hour equals `0.02527185133308243` exactly | `tests/test_export.py` | the literal `0.02527185133308243` — this value matches **exactly 1 of 8,791** records, at `2026-10-02T00:00:00Z`, and is the strongest available proof the history is the *same series*, not a proxy |

### 7.2 File shape

| # | Criterion | Artifact | Token |
|---|---|---|---|
| B1 | `signals/eth_usd_funding.jsonl` holds **≥ 1000** lines (was 1) | the file | line count |
| B2 | **Every** line carries **exactly the same 13 keys** as the live record | the file | `sorted(set(keys))` identical across all lines |
| B3 | `ticker == "ETH/USD"` on **every** line | the file | `"ticker": "ETH/USD"` — `PF_ETHUSD` here trips `signal_require_ticker: true` on every record |
| B4 | The file was **appended to**, not replaced; the original live record is still present | the file | the `2026-10-02T00:00:00+00:00` line survives |
| B5 | One timestamp spelling only (no mixed `Z` and `+00:00`) | the file | every `timestamp` ends in `+00:00` |
| B6 | No backfilled record fabricates a measurement: `funding_rate_prediction`, `mark_price`, `index_price`, `basis`, `open_interest`, `bid`, `ask`, `vol24h` are JSON `null` on backfilled rows | the file | `"basis": null` |

### 7.3 Observation-level, on a real end-to-end train/backtest run

Measured on a **synthetic** 721-bar 60m frame. The gate must **re-derive** these on a real
frame; the fixture-independent ones are marked ★ (those are the ones safe to assert as
constants), the frame-dependent ones are marked ⚑ with the measured value as *evidence for a
floor*, never as a constant.

| # | Criterion | Expected | Kind |
|---|---|---|---|
| C1 | ★ `n_features` for the **shipped-default** config | **== 57** (baseline 57, target 57; sane range 49–57) | exact |
| C2 | ★ `n_features` with **all three** `*_features_file` = `null` | **== 49** | exact |
| C3 | ★ The six funding columns are all present in `FeaturePipeline.compute` output | 6 / 6 | exact |
| C4 | ★ `tools/width_check.py` / `check_feature_width` does not raise; `normalization.npz`'s `feature_names` set is unchanged | `FeatureWidthMismatchError` absent | exact |
| C5 | ★ `first_tradable_index` unchanged | **== 24** before and after | exact |
| C6 | ★ `basis`, `open_interest`, `funding_rate_prediction`, `vol24h` remain **constant 0.0** on the backfilled window | 721/721 zeros — **this is the expected result, not a failure**, and the receipt must say "1 of 6" | exact |
| C7 | ★ `spread` is **unchanged**: exactly 24 nonzero bars, max `3.695e-05` | identical before and after | exact |
| C8 | ★ observation has **zero** non-finite cells | 0 | exact |
| C9 | ⚑ `funding_rate` distinct values over the 721-bar training frame | **≥ 700** (measured 720; baseline **2**) | floor |
| C10 | ⚑ `funding_rate` column std over the frame | **≥ 0.015** (measured 0.02332; baseline 0.00336 — ×6.9) | floor |
| C11 | ⚑ `signal_observed` fraction of bars | **≥ 0.98** (measured 721/721; baseline 13/721) | floor |
| C12 | ⚑ `signal_age_hours` max | **≤ 2.0** (measured 1.0 at a 12 h bound) | ceiling |

### 7.4 The `signal_observed` semantics change — REQUIRED disclosure, not a footnote

**F-2.** `observed = filled.notna().any(axis=1)` (`data.py:812`), combined across sources by
`_combine_freshness` (`:828`). It is therefore a **per-channel OR**. After the backfill
`signal_observed` becomes **1.0 on 721 of 721 bars** while **four of the six** funding columns
are still **100% zero-fill with no reading behind them**.

Today `signal_observed == 0` is what tells the agent those are fabricated. After this change
that per-column diagnostic is gone at the channel level.

This is **not fixable for free** and must not be papered over. It is the honest price of the
gap's own definition — per `configs/default.yaml:107-109`, *"1.0 when a record for `ticker`
landed in the window"*, and a record **does** now land on every bar. The required response:

* state it in `configs/default.yaml` at the `signal_max_age_hours` comment, and
* state it in the Phase 6 receipt, in the words *"funding covers the most recent year;
  `basis`/`open_interest`/`funding_rate_prediction`/`vol24h` are still structurally zero and
  `signal_observed` no longer distinguishes them per column"* — which is precisely the
  framing RESEARCH §1.17 prescribes for the G4 coupling.

Redesigning `signal_observed` into a per-column mask is **out of scope** (it would widen the
observation, invalidate every artifact, and is a different decision).

### 7.5 Recorder start — the §1.1 cold-start criterion, as an acceptance criterion

**F-3.** RESEARCH §1.1: G1 and G2 are both forward-only and share one trap. For **G2** the trap
is *bounded and stated*: the backfilled file's coverage starts at the **earliest record Kraken
returns**, which is a rolling cap recomputed on every call. Measured now:
**`2025-10-01T08:00:00Z`**, i.e. **366.58 days / 8,791 hourly bars**. The gate must assert:

* the **earliest timestamp in the file** and the **latest**, and print the span in days and in
  bars;
* that the span covers the whole training window
  (`first_tradable_index`..end), or — if it does not — that the uncovered prefix is explicitly
  reported with `signal_observed == 0` on exactly those bars;
* that no bar **before** the file's earliest record is claimed as observed.

Per this repo's own policy (*absence ≠ neutral*, `data.py:806-816`), the honest framing is
*"funding covers the most recent ~1 year; `signal_observed`/`signal_age_hours` are how you
tell"*. **Neither feature may be gated on `signal_observed`** — that is G1's feature.

**Coupling to G4 (RESEARCH §1.17), stated in the receipt:** 366 d ≈ 8,766 hourly bars covers the
~721-bar live ceiling and any hourly store. On **15-minute** bars it is ~35k bars, so a
multi-year G4 store would have the **last two years** silently zero-filled. That is the honest
limitation to record, and it is a reason to prefer this keyless feed over chasing deeper
third-party archives.

### 7.6 Supply-chain and structural regression gates

| # | Criterion | How |
|---|---|---|
| D1 | ★ `kraken_trading_bot/rl/features.py` and `rl/data.py` **executable AST unchanged** | `just audit-verify --prereg <ref> --since <ref>` must print `executable-ast  MATCH` and `measurement-track  MATCH` (`tools/audit_checks.py`; `PY_FILES` at `:52-58`). The only `data.py` edit is a **string literal** at `:150`, and `executable_ast` blanks strings. |
| D2 | ★ `pytest tests/ -q` passes, and `audit-verify`'s `suite` check reports `PASS` | `just audit-verify` |
| D3 | ★ `tools/model_matrix.py` byte-identical to its pre-registration commit | `cmd_verify` check #1 (`:157-168`) — never edit the measurement track to make a number come out right |
| D4 | ★ The backfill is reachable through the recipe, not only by hand | `justfile` must contain the literal `funding-backfill` |
| D5 | ★ The wrong cadence claim is gone from both places | `configs/default.yaml:117-126` and `systemd/kraken-trading-bot-funding.timer` must **not** contain the substring `8-hourly`; both must state the measured hourly cadence |
| D6 | ★ The refusal names the history recipe | `kraken_trading_bot/rl/data.py:150` must contain `funding-backfill` (string-only edit, so D1 still passes) |
| D7 | ★ No POST-retry was introduced | `kraken-funding-rates` has no private/order endpoints, and `client.py:_get` is unchanged — no session-level `Retry` was mounted. RESEARCH §1.15: an auto-retried POST risks replaying an order against a stale per-key nonce. Assert `_MAX_RETRIES` is still `3` at `client.py:25` and that `client.py`'s `_get` is byte-identical to its pre-change blob. |
| D8 | ★ Nothing cited is `/tmp`-only | `just audit-evidence` |

### 7.7 Finding dispositions

`just audit-findings` requires every `F<n>` in `VALIDATION.md` to appear in **this file**
(`audit_checks.py:358-381`). Dispositions recorded so far:

| id | finding | disposition |
|---|---|---|
| **F-1** | `features.py:494-496` states the shipped `configs/default.yaml` "composes 52" width. Measured: **57**. The docstring's explanation ("depends on `feature_windows` and `feature_groups`") omits the signal-channel contribution. | **Accepted, not fixed here** — it is a docstring in a file this decision does not edit, and `features.py` is under the `executable-ast` guard (D1). Phase 6 must confirm the measurement and either correct the docstring in a separate, string-only commit (invisible to D1) or record it as known. **This is the in-tree instance of Trap 1.** |
| **F-2** | `signal_observed` flips to 1 on 721/721 bars while 4 of 6 funding columns remain zero-fill, removing the per-column absence diagnostic. | **Accepted and disclosed** — §7.4. Structural to `data.py:812`'s per-channel OR. Not fixable without widening the observation. |
| **F-3** | Forward-only cold start: the file's coverage starts at a rolling ~366-day cap, measured from `2025-10-01T08:00:00Z`. | **Accepted, bounded, and asserted** — §7.5. Not a footnote. |
| **F-4** | DEV-1: AUDIT.md:153 says 12 of 721 bars observed; measured **13**. | **Correction recorded.** AUDIT's arithmetic ignored that the record sits at the frame's right edge, so the 12 h carry is clipped by the frame end. The gate uses 13. |
| **F-5** | DEV-3: RESEARCH-2 §6(2) preferred `0.0` for unrecoverable fields; this decision uses `null`. | **Override, measured equivalent** — §6.1 shows the observation is byte-identical either way; the choice is record honesty only. |
| **F-6** | DEV-2: RESEARCH-2 §7.2 suggested tightening `signal_max_age_hours` 12 → ~1. | **Override: fix the comment, not the value** — §5.3. The bound cannot change the observation on a dense series, and changing a shipped behavioural key inside a provenance change is out of scope. |

Any `F<n>` raised in Phase 6 that is not in this table **must be appended here** before
`just audit-findings` is allowed to pass.

### 7.8 Phase 4/5 finding dispositions (lead-appended after `aba7b9b`)

Raised during Phase 4B (`8cada4e`) and Phase 5 (`aba7b9b`). Recorded here so their
disposition is decision-level and not living only in a commit message — the gap
`just audit-findings` exists to catch.

| id | finding | disposition |
|---|---|---|
| **F-7** | **§6.1's dedup direction was inverted.** It claimed the appended *live* record wins the overlap hour. `data.py:758-763` runs `signal_df[~signal_df.index.duplicated(keep="last")]` **before** the hour-`floor()` and the `groupby(level=0).last()`, so the loser's whole row is discarded by **file order** and `last()` never sees it. Whichever record is **last in the file** wins the hour wholesale. | **Corrected and acted on.** Builder `--append` now **skips hours the file already holds**, which also enforces §6's own "one object per `(ticker, floored hour)`" contract. Measured without the skip: 13 real `spread` readings → `NaN`, spread max → `nan`, +13 non-finite cells. The consumer-side alternative (drop the row-level dedup so `groupby` merges per column) is a seam change that flips D1 to CHANGED — **declined, out of scope.** |
| **F-8** | **§7.3's exact width constants are wrong by 3, so a gate asserting them WILL fail.** C1 says `n_features == 57`; C2 says `== 49`. Measured on the shipped live-fetch leg: **60** and **52**. Every §7 width figure is 3 low because the frame it was measured on lacked `vwap`/`count`, so the three presence-gated derived features (`vwap_dev`, `volume_per_trade`, `trade_count_zscore_20`) were absent. **Sane range is 52–60, not 49–57.** | **Superseded — Phase 6 must use 52–60.** The *deltas* in §7 are still right (the `funding_rate`-only hazard is −5: 60 → 55). Do **not** assert C1/C2's literals. This is the same class as the 2026-10-01 "width per configuration" lesson: **ask which axis the earlier figure's fixture included.** |
| **F-9** | **C8 ("0 non-finite") is unsatisfiable as written on one arm and satisfied on the other.** Builder measured **684** non-finite cells (identical in baseline *and* target, all `spread` where there is no bid/ask); integrator measured **0**. | **Open — reviewer must re-derive and name the frame.** The likely reconciliation is *which frame is counted*: 684 pre-zero-fill NaN cells in the merged frame vs 0 post-zero-fill in the observation (`data.py:816` `fillna(0.0)`). **Do not** restate either number until that is shown. |
| **F-10** | **C12 (`signal_age_hours` max ≤ 2.0) is unachievable as written; measured max 12.0.** `signal_age_hours` is `ages.max(axis=1)` — the **stalest** column wins — and the backfill carries no `bid`/`ask`/`basis`/`open_interest`/`vol24h`, so those read the appended live row and ramp 1…12 before the 12 h bound retires them. | **Restate per shape.** C12 holds on the *shipped* shape (live snapshot on the **newest** bar, carry runs backwards) and not on a file seeded with a **stale** snapshot. This also **confirms F-6**: DEV-2 was right to leave the bound at 12. Phase 6 states which shape it measured. |
| **F-11** | **`spread` nonzero count is measured two ways and they disagree.** Builder: **13** (arguing §3.1/§3.2's "24" must equal the 13 observed bars, since both come from one live record plus one 12 h carry). Integrator: **24/721**, byte-identical both arms. | **Open — reviewer re-derives and states the frame and the right edge.** Not resolved here; two agents measured different arms. This repo's record is that a bar count must be **timestamped** or it reads as an error. |
| **F-12** | **§6 says the record carries 13 keys; the backfilled file carries 14** (the extra is `relative_funding_rate`, which §4/DEV-4 says to record), so B2's "exactly the same 13 keys" is unsatisfiable. | **Restated as a superset.** What the consumer needs is *backfilled ⊇ live*; that holds and is what the builder's test asserts. `relative_funding_rate` costs **0** observation columns because `data.py:765-769` intersects on keys. |
| **F-13** | **F-1 is now resolved, and its number was wrong.** F-1 read the shipped default as **52**; the integrator corrected the docstring: **52 is the all-null width**, the shipped default composes **60**. | **Closed** by a docstring-only edit in `aba7b9b` (invisible to D1's `executable-ast`, which is what F-1's own disposition required). |
| **F-14** | **The two characterisation tests added in Phase 5 are VACUOUS for `069a826..HEAD`.** Both pass with `data.py` / `features.py` reverted, because `executable_ast` is byte-equal on both files across the whole pass — this pass made **zero** executable pipeline change, so no test *could* be non-vacuous. | **Accepted, not a defect.** Recorded so a later reader does not mistake them for regression coverage. They remain valid future-seam guards. This is the honest form of the RUN LOG's non-vacuity rule: the rule caught the producer-hint pin as **genuinely non-vacuous** (reverting `&&`→`#` alone → `1 failed`, with the exact symptom), and caught these two as coverage-free for *this* diff. |
| **F-15** | **`tools/width_check.py` is a tool trap, not a doc trap.** `width_check.py:53-55` reads the parquet and calls `pipe.compute(df)` directly; it never runs `add_derived_ohlcv_features`, the `data.py` read-seam step that derives `vwap_dev` / `volume_per_trade` / `trade_count_zscore_20` from `vwap`/`count`. So a **raw OHLCV parquet reports 49** where every shipped state composes 52/60. Same root cause as **F-8** (the fixture lacked `vwap`/`count`), but reachable by pasting a receipt. | **Accepted — docstring-only note**, no behaviour change: auto-calling the derived step would change every historical fingerprint and invalidate the committed CAND-3a gate figures. The reviewer's own `verify --frame` receipt printed 49 and it was flagged in VALIDATION §7.3 as **do-not-paste**, because it reads as a 60→49 regression and is not one. **Pre-existing** (committed in the 2026-10-02 pass), not introduced here. |
| **F-16** | **`tools/audit_checks.py:find_in_repo` CRASHED on a legitimate citation**, making `just audit-evidence` unrunnable. `direct = REPO / name` yields the **absolute** path when `name` is absolute, and this audit legitimately cites a sibling checkout (`kraken-python/kraken_api/transport.py`, `kraken-market-data`'s `client.py`), so `direct.relative_to(REPO)` raised `ValueError`. A crash asserts nothing — the check could not have caught the defect it hunts. | **Fixed** in this pass: degrade to the absolute path when the target is outside `REPO`. `audit_checks.py` is **not** in `PY_FILES`, so `executable-ast` still reports MATCH and the pre-registered `model_matrix.py` stays byte-identical — the fix cannot weaken any assertion. `just audit-evidence` now completes: **RESULT PASS**, and it confirms a **falsification record exists: True**. |
| **F-17** | **`just audit-findings` was structurally incapable of binding this repo's own finding convention, and had been passing VACUOUSLY.** `cmd_findings` scanned `\bF(\d+)\b` and then searched `DECISION.md` for the literal token `F{n}` — but the convention in both artifacts is **hyphenated** (`F-1`, `F-7`). So every run reported "no F<n> identifiers in VALIDATION.md" and returned 0: the R5 defect class the check exists to catch, in the check itself. | **Fixed** in this pass: scan `\bF-?(\d+)\b` and match dispositions with `re.search(rf"\bF-?{n}\b", line)`, which also stops `F1` matching inside `F11`. **Proven biting, not assumed**: on its first real run it reported 16 findings with line numbers and returned exit 1 on **F-16 MISSING** — a genuine gap created minutes earlier. Before the fix the same tree reported zero findings and PASS. |

---

## 8. Runner-ups, and why they lost

### 8.1 `NEW-DATA-SOURCE` — a new sibling repo

Lost to §2. The producer already exists, on the same host, through the same client, with the
same retry policy; the record shape and the `ticker`-is-the-spot-pair rule are already encoded
and documented; and the target is on the **un-pinned path flake** side of the supply-chain
asymmetry, so a new repo would add a flake, a Nix module and a CI pipeline to make the fix
*harder* to reach the actual run path.

*Best future form of this outcome type:* **G1-on-Tardis** — a `kraken-order-book` sibling
backed by a paid historical-L2 feed, the only verified route to Kraken depth history
(RESEARCH-1 §3.8). Separate pass, separate budget conversation.

### 8.2 G1 — `microstructure` dead machinery / `order_book_imbalance`

Lost, narrowly, and it is the strongest runner-up.

*For:* maximum directness (the consumer is finished, guarded and unit-tested at
`features.py:1007-1015`); the keyless source is verified live; the producer is two lines of
arithmetic over a library already in-tree; RESEARCH §1.2 is right that no new repo is needed.
It is the only gap that adds **information the pipeline cannot otherwise express** —
contemporaneous liquidity skew vs. lagged price/volume statistics.

*Why it lost — three reasons, in order of weight:*
1. **Its gate cannot pass on the artifact this phase can produce.** G1 is **un-backfillable,
   ever** — Kraken's archive is OHLCVT-only (RESEARCH-1 §3.4) and Binance Vision returns
   `KeyCount=0` on both `bookDepth` and `depth` prefixes (§3.5). So a Phase 6 run against
   today's trailing 721-bar window measures `order_book_imbalance` as a **721/721 zero-fill**.
   The acceptance criterion "the feature appears and is non-degenerate" is *unsatisfiable*
   until a forward-only recorder has accumulated months of history. G2's criterion is
   satisfiable today and I measured it.
2. **It must edit `features.py`** — `_SIGNAL_COLUMNS` (`features.py:94-125`) **and**
   `_SIGNAL_BUILDER_INPUT_COLUMNS` (`features.py:149`). RESEARCH §1.3 corrected AUDIT here:
   it is **two** tuple edits, not one, or the observation widens by 3 instead of 1. `features.py`
   is in `audit_checks.py`'s `PY_FILES`, so `just audit-verify`'s `executable-ast` flips to
   CHANGED and every existing `normalization.npz` needs a retrain. G2 needs **zero** of that.
3. **It forces an aggregation decision G2 does not.** A `/Depth` response is an *instant* with
   no timestamp of its own (RESEARCH-1 §4.4), so last-sample-per-bar vs mean-per-bar must be
   settled before anything is written — and depth is not comparable across a `count` change, so
   the depth must be recorded or the series silently breaks.

*G1 is the correct next slice.* This decision does not foreclose it.

### 8.3 G3 — no retry / backoff / rate-limit handling

Lost, but it is the one to do second.

*For:* it protects **every** one of the ~57 columns; only the caller knows
`cursor`/`collected`/`pages`, so the "page 4 of 6 raised and pages 1–3 were discarded"
defect is irreducibly a caller bug; RESEARCH §1.12's finding that `except RateLimitError` alone
misses two of three failure paths is genuinely load-bearing.

*Why it lost:*
1. **It adds nothing to the observation.** Magnitude today: zero — the bug only fires when
   Kraken rate-limits, and no backtest number moves.
2. **Its effective half flips `executable-ast` to CHANGED** — `data.py` is in `PY_FILES`
   (D1).
3. **Its upstream half is on the wrong side of the supply chain.** `flake.lock` pins
   `kraken-python` at rev `81de5974…`, which **equals local HEAD**; the `.venv` has it
   editable. So an upstream-only fix would be **live under pytest and invisible to
   `just bench`** (RESEARCH §1.10). RESEARCH §1.11 adds that the throttle is **off by default**
   (`transport.py:108 min_interval = 0.0`, `auth.py:73 … or 0.0`, `nix/module.nix` `minInterval`
   default `null`), so "make the throttle adaptive" is a **no-op** as configured.
4. **The throttle being off is a shared exposure** for any recorder — but it does **not** block
   this backfill: the history endpoint needs **one** request per symbol, and RESEARCH-2 §3
   measured 25 back-to-back requests at **25 × HTTP 200, zero 429**. So G3 is not a prerequisite
   here (RESEARCH §1.9 agrees).

*If taken:* caller in `data.py:_page_candles` first, keyed on
`(RateLimitError, APIError, requests.RequestException)` — **not** `RateLimitError` alone —
with **Full Jitter** (`wait_random_exponential`; the AWS post is explicit that the no-jitter
curve is "the clear loser"), parsing the `EService: Throttled: <epoch>` timestamp Kraken
already sends in place of a `Retry-After` header, and **any** upstream session `Retry` set to
`allowed_methods={"GET"}` (RESEARCH §1.15).

### 8.4 G4 — thin historical depth behind `market_data_store: null`

Lost on **sequencing**, not merit. 366 d of funding ≈ 8,766 hourly bars comfortably covers the
~721-bar live ceiling, but if G4 lands a multi-year store at finer intervals the older span
would have **no funding and would silently zero-fill** (RESEARCH §1.17). Landing G2 first means
the two compose correctly; landing G4 first makes G2's coverage the binding constraint sooner.
G4 is also a scheduler plus a config flip plus a ~158 s / ~13 MB seed per pair — larger surface,
smaller observation effect.

### 8.5 G5 / G6 / G7

* **G5** (dead `order_book` plumbing, the wrong `interval=60` comment, duplicated
  `_OHLCV_COLUMNS`): repo hygiene on the **disjoint** engine/strategy program. `rl/` never
  imports `engine.py`. Real, cheap, not a data-provenance outcome.
* **G6** (`fee_rate: 0.0`, `slippage: 0.0`): arguably higher leverage per line than anything
  here — every artifact trained from the default learned a policy under a frictionless market.
  But it is **two config values**, not a pipeline outcome, and it invalidates every existing
  model's headline number. A separate decision, explicitly out of scope here (§5.2).
* **G7** (on-chain / macro / options / cross-venue): the honest `NEW-DATA-SOURCE` territory,
  but `Directness: none *today*` — no consumer exists. The landing point is cheap when it comes
  (two tuples plus the hardened seam), which is exactly why it should be a deliberate later
  pass rather than this one.

---

## 9. What a downstream agent must not do

1. **Do not claim 6 columns.** Say "1 of 6 recovered exactly". `basis`,
   `funding_rate_prediction`, `open_interest`, `vol24h` and `spread` keep their zero-fill.
2. **Do not write `relativeFundingRate` into `basis`.** It is funding-per-notional
   (measured `fundingRate / relativeFundingRate` = 4147.45 and 2664.89 across the span). It is
   recorded in the JSONL and **not** added to the observation — measured
   `corr(funding_rate, relativeFundingRate) = 0.99753`, so it is 0.9975 collinear with a column
   already present (§4).
3. **Do not widen to Binance/Bybit.** 8 h cadence needs resampling to 60 m, the rate is a
   fraction (Kraken's is percent-like — a **1000×** scale conversion), Binance's
   `openInterestHist` retention is **~30 days** on a different venue, and Bybit's OI is a
   different venue's number. That is a **semantic substitution**, not a recovery.
4. **Do not scrape anything.** Kraken's own API carries the column.
5. **Do not write the backfill to a second file.** Measured: **57 → 52**.
6. **Do not quote a single width.** Per configuration: **49** (all channels null), **57**
   (shipped default). Range 49–57.
7. **Do not widen `_SIGNAL_COLUMNS`** in this slice. It is not needed and it would invalidate
   every artifact.

---

---

**CARRIED-FORWARD SECTION 14 — read below, preserved verbatim.**

> **CARRIED FORWARD UNCHANGED from the superseded 2026-10-02 DECISION.md of the prior pass.**
>
> This section is **not** part of the G2 decision above. It is preserved verbatim because four
> in-tree artifacts cite it by section number, and rewriting this file without it would have
> left them dangling:
>
> | Citing artifact | Reference |
> |---|---|
> | `configs/default.yaml:184` | `time-aware feature windows over a reindexed bar grid (DECISION.md §14)` |
> | `configs/deep-history.example.yaml:35` | `(DECISION.md §14)` |
> | `tools/store_gap_scan.py:24` | `see DECISION.md section 14` |
> | `tools/store_gap_scan.py:206` | `feature windows over a reindexed bar grid (DECISION.md 14.)` |
> | `tools/cost_aware_gate.py:61` | `DECISION.md section 14.0 / finding R3` |
>
> **Interaction with the G2 decision above: none, and this is worth stating.** That caveat is
> about the **store arm** (`market_data_store` non-null, G4). The G2 outcome runs on the **shipped
> default**, where `configs/default.yaml:185 market_data_store: null` takes the pure live-fetch
> leg and no store gap exists. If G4 is later enabled, the 366-day funding window (§7.5) and the
> bar-count window semantics compose as follows: a bar-gap makes `funding_rate` land on a row that
> is 39 hours after its neighbour, so a "1-hour" funding change may span 39 hours — and
> `signal_age_hours` is computed from **timestamps** (`data.py:584`, `(index - seen) / hours`),
> not row counts, so the seam's freshness accounting is **already time-aware** and correctly
> reports the gap. The feature *window* is not. Both statements are true at once, and the honest
> framing is that the G2 change does not introduce this hazard and does not fix it either.

---

## 14. KNOWN LIMITATION — features are computed on bar counts, not wall-clock

**Status: DETECTED AND LABELLED, NOT FIXED.** Recorded 2026-10-02 from Phase 6 finding F6.
**This section is a standing caveat on every number computed from the store arm.**

### 14.1 What was measured

`just store-verify` now runs `tools/store_gap_scan.py` alongside the sibling seeder's own
contiguity check. On the shipped ETH/USD 60-minute store (106 month files):

| | |
|---|---|
| bars present | **76,564** (was 76,563 at Phase 6 review; the live append leg adds ~1 bar per run) |
| span | `2018-01-01T00:00Z` → `2026-10-02T17:00Z`, **76,721 hours** |
| **missing bars** | **158** across **28 gaps** (157 at review time; the +1 is the live leg) |
| largest gap | `2026-08-31 23:00` → `2026-09-02 14:00` — **38 missing bars**, spanning **39 elapsed hours** |
| other gaps | 32 (2018-02, off-grid boundary), 10 (2018-06-26), 10 (2019-05-15), 8 (2019-08-15), 7 ×2, 6, … |

**Units, because this gap has already been reported both ways and read as a contradiction:**
*38 missing bars* and *39 hours* are both correct. `N` missing bars means the two surviving bars
are `N+1` steps apart.

### 14.2 Why it matters

Every feature in `features.py` is computed over a **window of rows**, and the environment's
`start_index = 24` skips 24 **rows**:

- `return_1` reports the return between whichever two rows happen to be adjacent. Across the
  39-hour seam that is a **39-hour return presented as a 1-hour return**.
- `sma_24`, `rsi_24`, `obv_slope_24` and `bollinger_24` span 24 rows that may cover **more than
  a day**.
- After z-scoring, a 39-bar jump is indistinguishable from a 1-bar jump. **No guard in this repo
  can detect it**, because every value involved is finite and correctly computed from the rows it
  was given. Only the timestamps know.

**The 38-bar hole is at the seed/live-append seam**, in the most recent month: the month-file
boundary where the Binance-archive seed ends and this repo's live append leg takes over. That is
precisely the region a live deployment trades, so the hole is not confined to the training slice.
The tool labels that gap **by name** (`seed/live-append seam`) and flags it on its own line.

### 14.3 What was deliberately NOT done

**No reindexing, no interpolation, no gate.** `store_gap_scan.py` detects and labels; it does not
repair and does not fail the run. Adding a hard failure would have broken the store arm on data
that is otherwise usable, which is a bigger decision than a bug-fix pass should make.

### 14.4 The real fix, and why it is not called CAND-3b

The fix is to compute features over a **reindexed, gap-filled bar grid**, so a window means N
*hours* rather than N *rows* — i.e. make the windows time-aware and decide explicitly what a
window spanning a gap means (hold last value, or mask the row).

**It is not called CAND-3b here on purpose.** `CAND-3b` is **already taken** in this document:
§10 item 4 defines it as the `since`/`until` push-down — a ~21-line, store-only **efficiency** item,
explicitly DEFERRED as not load-bearing (whole-store read 0.142 s vs windowed 0.010 s). *(The venue
label is **not** part of item 4: §10 item 2 moved it **out** of the sibling's `_meta.json` and into
the model's own `config.yaml`, and treats the `_meta.json` label as a follow-up, not dropped. §10.2
therefore says the opposite of what attributing it to item 4 would imply.)* Reusing that id for a
correctness fix would be the same false-claim failure this pass exists to remove, so this item needs
its own registration before it is referred to by id. *Raised with the lead rather than invented here.*

### 14.5 The §14.4 correction above was itself imprecise in one half — a definitional mix, not only staleness

The CORRECTION block in §14.4 concedes that its pooled-IQR half was "stale". That undersells it, and
the independent F1/F2 re-review caught the sharper defect. The two figures were not merely drawn at
different times — **they were not the same statistic**:

- **11.68pp** was the Phase 6 **store-arm-only** IQR, computed on a **76,539-bar** snapshot, before
  the cost-aware estimator existed.
- **3.97pp** is the **shipped estimator's** pooled-within-spread, which takes the **median of BOTH
  arms'** IQRs, on a **76,540-bar** snapshot.

So the 1.4 → 82.91 ratio compares a one-arm spread against a two-arm median. Two independent errors
were folded into that half: the **snapshot** was one bar short, and — the material one — the
**definition** changed underneath it. The gap half (16.24pp → 328.79pp) is unaffected; that
comparison is like-for-like on the same metric.

**This does not move the verdict, and the reason is worth stating precisely.** Both figures sit on
the same side of a threshold that was committed (`97a2a52`) before the run and never touched since.
A ratio's *magnitude* was mispredicted; its *sign against a fixed threshold* was not in question.
The correction is recorded here rather than folded into §14.4's text so the original wording stays
visible as the reviewer saw it.

### 14.6 Phase 6 findings F2 and F3, at decision level

F4 and F6 got records at the point they arose (§9A.3's CORRECTION block, §14). **F2 and F3 did not** —
their dispositions lived only in a docstring and an error string, so a reader auditing this gate's
findings would have had to read diffs to learn them. Recorded here now.

**F2 — the vacuous seam test and five false docstrings.** *Finding:* deleting `compute`'s non-finite
seam left **all 31** non-finite tests green, because pandas' `rolling` masks an infinity on its own;
and five docstrings asserted the *opposite* of pandas' behaviour while claiming to have measured it.
Severity MAJOR — a test that cannot fail, asserting a mechanism that does not exist.

*Option (a) — delete the test and the docstrings.* **Rejected.** The underlying hazard is real; deleting
the evidence would hide it and re-invite the same discovery as a "bug" later. *Option (b) — replace
with a test that pins the seam by an effect pandas does **not** have on its own.* **Chosen**, on the
reason that the seam's only defensible justification is the **zero** price, not the infinity: zero is
in `_NON_FINITE_INPUTS`, a zero price is reachable in real OHLCV, and without the seam a zero enters
a rolling window as a real number and yields `sma_4` **91.41** against a ~122 price and `bb_lower_4`
**−14.14** — finite, plausible, and silent. So the replacement pins that symptom, and the old inf
assertion was **kept and labelled** rather than deleted (it passes with the seam removed; its label
now says so). *Option (c) — invert the claim to "the seam prevents infinities".* **Rejected as still
false**: neither aggregation emits an infinity, so no `isinf` assertion can observe the seam.

*Consequence accepted:* with the seam mapped to NaN, `rolling` reports a poisoned window as NaN
("unknown") while `ewm` **skips** it and reports a **stale carried value** — finite and wrong, and
invisible to `_require_finite`. That is **out of scope for this pass** and is pinned by
`test_rolling_masks_but_ewm_skips_a_non_finite_price_this_guard_cannot_see`, which asserts `ewm`
still skips a non-finite input so a future pandas change surfaces here. See the `KNOWN GAP` note in
`compute` and the `_rsi` docstring.

**F3 — `market_data_store: null` does not turn the store off.** *Finding:* `data.py`'s
`MarketDataStoreUnavailableError` told the reader to "Set `market_data_store: null` in the config",
but `_resolve_env_setting` treats a YAML `null` as *unset* and **skips** that source, so a run config
saying `null` falls through to the **model's own** `config.yaml` — where the store path comes from —
and the store silently stays on. A store-trained model would keep 76k bars while believing it went
live. *Option (a) — honour an explicit sentinel (`""` / `--no-store`).* **Rejected**: it changes
resolver semantics, which is outside this pass and would need its own test matrix. *Option (b) —
correct the message to name the file that actually has to change.* **Chosen**: the advice now names
**the model's own `config.yaml`**, because that is the file the reader must edit. The deliberate
`null`-means-unset semantics are **kept** — YAML `key: null` legitimately means *unset*, and
overriding it would break every other config layer that relies on it.

*Residual, accepted and recorded:* with option (b) the trap is still reachable by editing the wrong
file, but the message no longer *instructs* it. A working override remains the correct future fix.

---
**DECISION COMPLETE**
