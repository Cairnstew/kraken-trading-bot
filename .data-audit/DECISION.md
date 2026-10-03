# DECISION.md — data-pipeline pass 2026-10-03, Phase 3

**Architect. Read-only at HEAD `7534ace`.** This file overwrites the prior pass's
decision, which is preserved in git history. Nothing was committed. No source file was
modified. No code was written.

Every constant below is marked **[M] measured** (I ran it, in `nix develop`, command
quoted) or **[I] inferred**. Every direction is marked **[R] ran** (I executed the
producer/consumer and observed the behaviour) or **[G] guess** (asserted from reading).
Per the phase rule, **no `[G]` appears in any acceptance criterion.**

---

## 1. Decision

| field | value |
|---|---|
| **Outcome type** | **`IMPROVE-EXISTING`** |
| **Target** | **G-C** — the news + social exogenous channels (`AUDIT.md` §4/G-C) |
| **Repos touched** | 3 — this repo, `~/Projects/ticker-news-signals`, `~/Projects/kraken-social-signals` |
| **New repos** | **0.** No naming step, no `gh repo create`, no `../<name>/`. |
| **New observation columns** | **+6** width, **+4** usable ([M] delta, §3.2) |
| **Python change in this repo** | **none** — the merge seam is already complete ([M]/[C], §3.1) |

**One-paragraph summary.** `AUDIT.md` ranked G-C *"highest-directness of anything still
on the table"*, and `RESEARCH.md` §1.1/§1.2 **reversed its mechanism**: the two channels are
not unreachable for want of a schedule, they are unreachable because **both producers
truncate their output file** — so "add a timer" is not a neutral no-op, it is **data
destruction on a timer**. The correct work is therefore producer-side append semantics in
two existing sibling repos plus a gnews build fix, then scheduling and two config keys here.
That is `IMPROVE-EXISTING` across three repos, with **no new project** — the "new data
source" is not new, it is two shipped CLIs that were never run. `RESEARCH.md` §2 (G-C, §7
Q2) settles the timing question in favour of *now*: `models/` holds only `.gitkeep` **[M]**,
so the artifact-invalidation cost is currently **zero**, deferring is what *creates* that
cost, and the files are forward-only so every day of delay is unrecoverable signal history.

---

## 2. Why this target, against the RL pipeline's real shape

`AUDIT.md` §2 fixes the shape this has to fit. Re-derived, not inherited:

- **Config keys are the landing point.** `extra_features_file` (`configs/default.yaml:57`)
  and `social_features_file` (`:100`) are both `null`. **[C]** at HEAD. Each is read by
  four legs: `train.py:220`, `backtest.py:476`, `export.py:170`, `paper_trade.py:303`
  **[C] `AUDIT.md` §2.1, whose read inventory was `[M]` grep-verified.**
- **Exactly where the feature is read per ticker.** `_SIGNAL_CHANNELS` (`data.py:154-176`)
  is a **single tuple** driving both the fetch leg (`data.py:1215-1225`) and the store leg
  (`data.py:1408-1418`) **[C]**. The key resolves to a path, `merge_extra_features`
  (`data.py:612`) reads it, `_filter_ticker` (`data.py:863`) filters to the ticker, the
  index is hour-floored and left-joined. **One list, two legs — they cannot disagree.**
- **Where width is decided.** `FeaturePipeline.compute()` caches `_last_feature_names`
  (`features.py:~830-833`); `n_features()` returns its length (`features.py:~900`);
  `TradingEnvironment._raw_feature_array()` (`environment.py:470-495`) materialises it and
  `environment.py:200` builds the `Box`; `train.py:288` records the shape into
  `config.yaml`. **[C]** `AUDIT.md` §2.3.
- **Artifact layout.** `models/{TICKER_ID}/{model_name}/` = `model.zip`,
  `normalization.npz` (whose pickle-free `feature_names` array is the width authority,
  `features.py:~365-380`), `config.yaml`. **[C]**. `check_feature_width`
  (`features.py:~472-530`) is non-self-referential — expected names from the artifact,
  actual from `compute()` — so a widening genuinely fails loudly rather than silently.
  **[C]** `AUDIT.md` §2.3.

**The decisive structural fact, re-derived by me:** the consumer side of G-C is
**already finished**. All six columns are already in `_SIGNAL_COLUMNS`
(`features.py:94-125`: `sentiment_score`, `article_count`, `novelty_flag`,
`stt_mention_count`, `stt_tilt`, `fng_index`) **[C]**, and **none** of them is in
`_SIGNAL_BUILDER_INPUT_COLUMNS` (`features.py:144-149` = `("bid","ask","spread")`)
**[C]**. So they are carried by the merge seam and copied into the observation verbatim —
no `features.py` edit, no widening of the allow-list, no `POINT_IN_TIME_EXOGENOUS_COLUMNS`
change. **This collapses the in-repo blast radius to config + units + recipes + docs**, and
it is why G-C is cheap here despite being three repos wide.

### 2.1 The `NEW-DATA-SOURCE` question, answered explicitly

My brief flags that G-C "has a genuine `NEW-DATA-SOURCE` tail". I checked what that tail
actually is, and it is **fixes to two existing repos**, not a new project:

| supposed tail | what it is | outcome type |
|---|---|---|
| "append semantics" | `path.open("w")` → append mode in two **existing** sibling CLIs | `IMPROVE-EXISTING` |
| "a gnews build fix" | `nix/gnews.nix:19` in the **existing** `ticker-news-signals` | `IMPROVE-EXISTING` |

So: **`IMPROVE-EXISTING`**. There is no new source to ship — Google News and
StockTwits/Fear-&-Greed are *already* pulled by two working CLIs; nobody runs them. The
gap is activation and durability, not sourcing. `RESEARCH.md` §2 G-C.4 independently
reaches the same conclusion about flake wiring: **do not add these siblings as flake
inputs** (a pinned input would pin the gnews build failure into `flake.lock` and force a
commit→push→`--update-input` round trip per fix).

---

## 3. Re-derivation of the load-bearing facts

### 3.1 Both producers truncate — **[R] ran**, not read

`AUDIT.md` did not find this; `RESEARCH.md` §1.1 did. I re-derived it by execution
because it is the load-bearing *direction* of this entire decision.

**[R] news producer — marker-line experiment, run by me:**

```
$ cd ~/Projects/ticker-news-signals
$ OUT=/tmp/opencode/gc/news.jsonl
$ printf '{"ticker":"MARKER_SENTINEL_A","published":"2020-01-01T00:00:00Z"}\n' > $OUT
$ wc -l < $OUT                       # BEFORE: 1 lines
$ ./.venv/bin/python cli.py --no-log-file pull --ticker ETH/USD --output $OUT
signals -> /tmp/opencode/gc/news.jsonl
$ wc -l < $OUT                       # AFTER: 2 lines
$ grep -c MARKER_SENTINEL_A $OUT     # MARKER PRESENT: 0
```

The sentinel is **destroyed** by one pull. Truncation is observed, not inferred.
Mechanism **[C]**: `ticker_news_signals/export.py:123` `with path.open("w", encoding="utf-8")`.

**[C] social producer:** `kraken_social_signals/export.py:145-152`, `write_jsonl`, line
**149** `with path.open("w", encoding="utf-8")` — byte-identical construct to the news one,
which I ran. Revs match `RESEARCH.md`: news `23dc965`, social `71ca27d`.

**[M] neither CLI has `--append`:** `grep append` over `ticker_news_signals/cli.py` → no
flag; over `kraken_social_signals/cli.py` → only `records.append` list calls. Contrast
`kraken-funding-rates`, which has it and which the production timer already uses
(`systemd/kraken-trading-bot-funding.service:39`).

**[M] default lookbacks** (the window each pull therefore rewrites the file to):
`ticker_news_signals/cli.py:54` `--lookback-hours` **default 1**;
`kraken_social_signals/cli.py:64` **default 24**. **[C]** `RESEARCH.md` §1.1's consequence:
hourly news leaves ~1–2 h of coverage against a 721-bar frame; social leaves a sliding
24 h window that never accumulates.

**[C] cadence vs the bar grid.** `configs/default.yaml:11` `ohlcv_interval_minutes: 60`.
Hourly is the coarsest cadence that still fills every bar; duplicates inside one hour are
free at the seam (`data.py:776-781` keeps the last record per floored hour) **[C]**.

**Consequence, stated as the rule it obeys:** `RESEARCH.md` §3.3 — *truncate vs append must
be established per producer before any schedule is designed.* Established: both truncate.

### 3.2 The width delta — **[M] measured**, as a delta, on a stated axis

The width trap (`RESEARCH.md` §3.7: 49–66 across everything measured) is real. So I state
**no literal width anywhere**, and I measured the delta on two axes.

Fixture, stated so it can be reproduced: 200 synthetic hourly bars, `feature_windows
[1,4,24]`, all five groups, **`add_derived_ohlcv_features` applied** (the real read seam —
omitting it is the F-15 trap, `AUDIT.md` §0), `compute()` → `.shape[1]`. Script
`/tmp/opencode/gc/width.py`, run `nix develop --command python /tmp/opencode/gc/width.py`.

```
[M] baseline (no signal files)                       width=49
[M] + news (real producer file, 2 records)           width=54   delta=+5
[M] + social (synthetic 3-key file, 24 records)       width=54   delta=+5
[M] + news + social                                  width=57   delta=+8

--- shipped-default axis: funding channel ALREADY active ---
[M] funding only (the real 1-line shipped file)       width=57
[M] funding + news                                    width=60   delta=+3
[M] funding + news + social                           width=63   delta=+6
[M] freshness cols already present after funding-only merge: ['signal_observed','signal_age_hours']
```

**Three findings, and one of them corrects a number in circulation:**

1. **Each channel costs +5, not +3** — 3 signal columns **plus** the two freshness columns
   `signal_observed` / `signal_age_hours` **[C] `features.py:104-105`**. **[M] measured.**
2. **The freshness columns do not stack.** Two channels together are **+8, not +10**,
   because the second merge overwrites the same two names. **[M] measured.** Recorded so
   nobody double-counts.
3. **On the shipped-default axis the delta is exactly `+6`** (57→63), because funding is
   already active and has already introduced the freshness columns. This independently
   confirms `AUDIT.md` §4/G-C ("+6 at +6 width") and `RESEARCH.md` §1.11 by a third route.

**Honour the absolute widths' disagreement.** My funding-only axis reads **57** where
`AUDIT.md` §2.4 reads **52** for "all `*_features_file` null" and **60** for the shipped
default. Cause, **[C]**: `bid`/`ask`/`spread` are in `_SIGNAL_COLUMNS`
(`features.py:117-125`) and the **real funding file carries `bid` and `ask` keys**, so the
microstructure builder emits `spread` — a column a null-channel frame does not have. My
synthetic frame has no native bid/ask, so the spread column arrives only via the funding
merge. **This is exactly why only the delta transfers** (`RESEARCH.md` §1.11) and why no
literal width appears in §5.

### 3.3 Two hypotheses I checked and refuted — so Phase 4 does not re-derive them

- **"The news file's `ticker` is `ETH_USD` while the config says `ETH/USD`, so
  `signal_require_ticker: true` will reject it."** *Refuted, by running.*
  **[M]** the live pull wrote `ticker: 'ETH_USD'` (underscore form), and
  `nix develop --command python -c "…_canonical_ticker…"` returns
  `'ETH/USD'->'ETHUSD'`, `'ETH_USD'->'ETHUSD'`, `'eth/usd'->'ETHUSD'`, `'ETHUSD'->'ETHUSD'`,
  and `'news-file ticker equals config ticker: True'`. `data.py:909` maps the column through
  `_canonical_ticker` and `data.py:928` canonicalises the wanted key; `_canonical_ticker`
  is documented separator-free at `data.py:486-492`. **No defect, no fix needed.**
- **"A second channel adds a second `signal_observed`/`signal_age_hours`."** *Refuted,
  measured* — see §3.2 finding 2. The delta is +6, not +8, on the shipped axis.

### 3.4 Scheduling facts, re-derived at HEAD

- **[C]** `systemd/` contains exactly three files, all funding. `…funding.timer:19`
  `OnCalendar=*-*-* *:17:00`, `:21` `Persistent=true`, `:22` `RandomizedDelaySec=120`.
- **[M] the funding timer is not installed on this host.**
  `systemctl --user list-timers --all` lists **6 timers**, none of them
  `kraken-trading-bot-funding.timer`. **[M]** `loginctl show-user $USER | grep -i linger`
  → `Linger=yes`, so new user timers **will** fire logged-out. Confirms `RESEARCH.md` §2's
  host finding independently.
- **Timer minutes: `:23` and `:29`, not `:17`.** Funding fires at 17:00 **plus up to 120 s
  of jitter** (`timer:22`), i.e. it owns the 17:00–17:02 window. Two new timers sharing
  `:17` would land in that same window. `:23` and `:29` are clear of it and of each other.
- **[C] `nix/module.nix` is a NixOS module** and installs no `systemd.user.*` unit — the
  working pattern is the justfile-installed `systemd/user` pair, exactly as
  `justfile:163` `funding-timer` does (`sed` the `.service.in` into
  `~/.config/systemd/user/`, symlink the `.timer`, `daemon-reload`,
  `enable --now`).
- **[C] timer semantics, from local man pages (systemd 262), quoted in `RESEARCH.md` §2:**
  `Persistent=true` on `OnCalendar=` gives **one coalesced catch-up, not one per missed
  interval** → no catch-up storm. `Persistent=true` is **silently inert on
  `OnUnitActiveSec=`**, and monotonic timers pause on suspend and drift off the UTC bar
  grid → **reject `OnUnitActiveSec`**. **Reject cron** (zero catch-up → silent staleness).
  **Reject a Python scheduler library** (a new long-lived process to supervise, against the
  repo's systemd pattern).

### 3.5 State of the world this decision lands on

- **[M]** `models/` contains **only `.gitkeep`** (`find models -type f`). Artifact
  invalidation cost of the widening is **0 today**.
- **[M]** `.gitignore:65` is `signals/`. `git ls-files signals/` empty. **Keep it
  ignored** (`RESEARCH.md` §1.4) — an hourly append-only log that ages out is the wrong
  shape for git.
- **[M]** The store does not exist: `ls -d ~/Projects/kraken-market-data/store` → *No such
  file or directory*; `find ~/Projects -name '*.parquet' -not -path '*/.venv/*' | wc -l` →
  **0**. Every store-depth figure in any prior artifact is unverifiable here.
- **[C] execution path for the news CLI.** `nix run` does **not** work for
  `ticker-news-signals` — `nix/gnews.nix:19` sets `format="setuptools"`, gnews 0.8.2's sdist
  ships `requires.txt` but no `requirements.txt`, and its `setup.py:3` opens the latter
  (`RESEARCH.md` §1.2; **I did not re-run the build — [C] from `RESEARCH-1.md`, and no
  acceptance criterion rests on it**). **[M] the sibling's own `.venv` runs it** — that is
  the command in §3.1 and it succeeded. `kraken-social-signals`' `nix run` works
  **[C] `RESEARCH.md` §4**.

### 3.6 Honest gaps in the evidence — stated as gaps, not as negative results

- **`stt_tilt` permanently dead; `novelty_flag` dead at hourly cadence** — `[M]` in
  `RESEARCH-1.md` (live StockTwits v2: 0/30 messages carry a `sentiment` key, so tilt is
  always `0.0`; `NOVELTY_WINDOW = 15 min` vs an hourly sampler). **I did not re-run
  either** (both need live third-party calls I did not make). **Neither appears in any
  acceptance criterion in §5.** They are why the honest headline is **+6 width, +4 usable**
  — and they are harmless: `features.py:455-457` `safe_std = std if std > 1e-12 else 1.0`
  normalises a zero-variance column to exactly `0.0`, so no inf/NaN reaches the
  observation **[C]**.
- **News is market-wide, not ticker-specific.** `pipeline.py:59-60,135` merges a symbol
  search with a whole-category `DIGITAL_CURRENCIES` topic pull; `RESEARCH.md` §2 measured
  hour 08 = 24 articles for "ETH". **So two tickers' news files would be near-duplicates**
  — carry that warning into any per-ticker news claim. `fng_index` is a **daily** value
  stamped per hour.
- **Google News rate limits: undocumented. StockTwits registrations: closed.** No paid
  source is proposed, per the constraints. Volume is 2 requests/hour/ticker, keyless.

---

## 4. In-scope files and edit points, with current `file:line`

### 4.1 `kraken-trading-bot` (this repo)

| # | file:line (at `7534ace`) | change |
|---|---|---|
| 1 | `configs/default.yaml:57` | `extra_features_file: null` → `~/Projects/kraken-trading-bot/signals/eth_usd_news.jsonl` |
| 2 | `configs/default.yaml:100` | `social_features_file: null` → `~/Projects/kraken-trading-bot/signals/eth_usd_social.jsonl` |
| 3 | `configs/default.yaml:53-54` | doc: the `python ~/Projects/ticker-news-signals/cli.py …` recipe **cannot run in this dev shell** (gnews absent, sibling flake won't build) — replace with the working `.venv` invocation |
| 4 | `configs/default.yaml:96-98` | doc: same for the social channel's recipe |
| 5 | `configs/default.yaml:63-65` | doc: state the fresh-clone consequence — a non-null key with no file raises `SignalFileNotFoundError` (`RESEARCH.md` §1.4) |
| 6 | `kraken_trading_bot/rl/data.py:157` | docstring: producer string for the news channel — same correction as #3 |
| 7 | `kraken_trading_bot/rl/data.py:174` | docstring: producer string for the social channel — same as #4 |
| 8 | `kraken_trading_bot/rl/data.py:642` | **wrong comment**: *"re-appends the current hour on every pull"* → it **truncates** (§3.1) |
| 9 | `systemd/kraken-trading-bot-news.service.in` | **new**, modelled on `kraken-trading-bot-funding.service.in`; needs `@TICKER@` (these CLIs use `--ticker`; funding uses `--pair`) and `@OUTPUT@`; `ExecStart` must pass `--append` |
| 10 | `systemd/kraken-trading-bot-news.timer` | **new**: `OnCalendar=*-*-* *:23:00`, `Persistent=true`, `RandomizedDelaySec=120` |
| 11 | `systemd/kraken-trading-bot-social.service.in` | **new**, same template family |
| 12 | `systemd/kraken-trading-bot-social.timer` | **new**: `OnCalendar=*-*-* *:29:00` |
| 13 | `justfile` — new recipes after `:163` (`funding-timer`) | `news-timer`, `social-timer`, plus `news-pull` / `social-pull` hand-run recipes modelled on `:186` (`funding-pull`) |
| 14 | `justfile:11-14` (recipe index header) | list the four new recipes |
| 15 | `README.md` / `INTEGRATION.md` (bot side) | reflect the new channels and the fresh-clone consequence |

**Not in scope, deliberately:** `features.py` (**no change** — §2), `data.py`'s merge seam
(**no change**), `nix/module.nix` (**no change** — NixOS module, §3.4), `flake.nix`
(**no change** — §2.1), `.gitignore` (**no change** — `signals/` stays ignored).
**Do not** touch `data.py:161`'s funding `&&` hint — that comment is **correct** and
`AUDIT.md` §6.7 endorses it.

### 4.2 `~/Projects/ticker-news-signals` @ `23dc965` — the blocker

| # | file:line | change |
|---|---|---|
| 16 | `ticker_news_signals/export.py:123` | `path.open("w")` → append mode, gated on a new `append: bool` |
| 17 | `ticker_news_signals/cli.py` (next to `:54`) | add `--append` flag, default **off** (back-compat: today's truncate-by-default behaviour is preserved unless asked) |
| 18 | `nix/gnews.nix:19` | build the **wheel** instead of the sdist, so `nix run` works (`RESEARCH.md` §1.2) |
| 19 | `INTEGRATION.md:114-118` | **wrong**: "re-append" claim, and the documented cron recipe at `:117` (`--output f >> f`) produces a **corrupt JSONL line** |

### 4.3 `~/Projects/kraken-social-signals` @ `71ca27d` — the blocker

| # | file:line | change |
|---|---|---|
| 20 | `kraken_social_signals/export.py:149` | `path.open("w")` → append mode |
| 21 | `kraken_social_signals/cli.py` (next to `:64`) | add `--append`, default off |
| 22 | `.env.example:8-10` | **wrong**: *"a registered app token buys headroom"* — **registrations are closed** |

### 4.4 Integration sketch (what Phase 5 wires)

```
  ~/Projects/ticker-news-signals  ──(pull --ticker ETH/USD --output … --append)──┐
                                                                          hourly, :23
  ~/Projects/kraken-social-signals ──(pull --ticker ETH/USD --output … --append)─┤ hourly, :29
                                                                          hourly, :17 (exists)
                                                                                  │
                                        appends one hour-floored record per fire  │
                                          (duplicates free: data.py:776-781)      │
                                                                                  ▼
                              signals/eth_usd_{news,social,funding}.jsonl   [gitignored]
                                                                                  │
   configs/default.yaml:57  extra_features_file ──┐                              │
   configs/default.yaml:100 social_features_file ─┤  read by train:220 /         │
   configs/default.yaml:92  funding_features_file ┘  backtest:476 / export:170 / │
                                        paper_trade:303                          │
                                                                                  ▼
                        _SIGNAL_CHANNELS (data.py:154-176)  ── one list, two legs ──
                          fetch leg data.py:1215-1225 | store leg data.py:1408-1418
                                                                                  ▼
                    merge_extra_features (data.py:612)
                      _filter_ticker (data.py:863)  →  hour-floor left-join
                      bounded carry + signal_observed / signal_age_hours
                                                                                  ▼
        FeaturePipeline.compute() → _last_feature_names → n_features()
                      → TradingEnvironment._raw_feature_array() → Box((n_bars, n_features))
                      → train.py:288 writes n_features into models/{TICKER_ID}/{name}/config.yaml
                      → normalization.npz feature_names is the width authority
```

---

## 5. Acceptance criteria

Written so a **correct implementation passes them**. Every width criterion is a **delta
measured in-test against a same-fixture baseline**, never a literal — the width is
per-configuration and ranges 49–66 across everything measured this pass
(`RESEARCH.md` §3.7, `AUDIT.md` §2.4, §3.2 above).

**AC1 — the seam carries all six columns, by name.**
Merge a fixture frame with a 3-key news file; assert the set of columns the merge **adds**
equals `{sentiment_score, article_count, novelty_flag}`. Repeat for social with
`{stt_mention_count, stt_tilt, fng_index}`. Set equality, never a count.

**AC2 — the widening is a delta, and the freshness columns do not stack.**
With `funding_features_file` active, width(funding) is measured on frame *F*; then
width(funding+news+social) on the **same frame *F***. Assert the difference is exactly the
number of *newly reachable signal columns* (6 on my fixture — **the test computes the
expected value from the column set, it does not hardcode 6, because a fixture without
funding active yields 8**). Additionally assert the merged frame contains **exactly one**
`signal_observed` and **exactly one** `signal_age_hours` after all three merges. *This is
the AC most likely to encode a wrong constant; the test must derive it.*

**AC3 — producers append, not truncate.** Two consecutive `write_jsonl(path, docs,
append=True)` calls: the first file's line count and first-line content are **unchanged**
after the second. Assert content, not just count.

**AC4 — truncation is never silent.** With `append` not requested, the operation either
**refuses** (non-zero exit, message naming `--append`) or truncates **loudly**
(`stderr` warning naming the records dropped). Pick one; assert that one. A silent
truncate is a fail.

**AC5 — the timers exist and do not collide.** Both new `.timer` files present;
`OnCalendar` minute is **23** for news and **29** for social; `Persistent=true`; and
**no two of the three timers share a minute** (funding is 17, asserted in the same test).
Assert the parsed `OnCalendar`, not the file text.

**AC6 — the units append.** Each new `.service.in` `ExecStart` contains `--append` and
`--ticker`. Assert on the generated `.service` (the `.in` after substitution), matching how
`funding-timer` actually installs it.

**AC7 — fresh-clone behaviour is tested and documented.** With a non-null key whose file
does not exist, `read_ohlc_dataframe` raises `SignalFileNotFoundError`. `signals/` is
gitignored **[M] `.gitignore:65`**, so this is the *default* state of a fresh clone — it
must be a named, tested consequence, not a surprise. `RESEARCH.md` §1.4.

**AC8 — every doc site in §4.1/§4.3 reads correctly.** `data.py:642` no longer says
"re-appends"; `data.py:157`/`:174` and `configs/default.yaml:53-54`/`:96-98` no longer
advertise an invocation that cannot run. Plain string assertions are fine — **these files
carry no executable AST guard**, so a doc-only edit is always safe.

**Existing tests that already guard this change — reuse, do not rewrite:**
- `tests/test_rl_data_store.py` already exercises `extra_features_file` and
  `social_features_file` **end-to-end through `merge_extra_features`** at lines **263,
  322, 350, 391-393, 460, 472, 608** **[C]**. **The merge path needs no new mechanism
  test.** (Contrast: `RESEARCH.md` §2 G-A notes `tests/test_rl_data_store.py:194` already
  covers `since`/`until` end-to-end — the same lesson. That is *why* G-C's half has never
  been caught: the mechanism is tested, the activation is not.)
- `tests/test_rl_signal_config_wiring.py:497-551` is the **AST-forwarding-guard class** to
  reuse. A new sibling of that guard should assert that **every**
  `read_ohlc_dataframe(` call site's config hop carries all three `*_features_file` keys —
  the class of check that would catch the next leg that forgets.

---

## 6. Test strategy

**New tests**
1. `tests/test_gc_producer_append.py` (new file) — AC3, AC4, in each sibling's own test
   suite, since the write code lives there. Note both siblings already have `tests/`.
2. AC1 + AC2 in `tests/test_rl_data_store.py`, beside the existing merge tests at `:263`
   and `:350`.
3. AC5 + AC6 as a static parse of `systemd/*.timer` and the generated `.service` — the
   same shape as `tests/test_rl_signal_config_wiring.py:497-551`; no timer needs to fire.
4. AC7 in `tests/test_rl_data_store.py` (missing-file path).
5. AC8 as plain string assertions in the same new file.
6. A new AST forwarding guard over all four `read_ohlc_dataframe(` call sites asserting
   the three `*_features_file` keys — the generalization of the guard already at `:545-552`.

**Existing tests that guard the change:** the seven `test_rl_data_store.py` merge sites
above. They must stay green **unchanged** — that is the point: the merge seam is not
being modified, only fed.

**Not required, and why:** no new test of `FeaturePipeline.compute()` width arithmetic
(no Python change), no seeded-store test (the store is absent — §7), no test that fires a
timer.

---

## 7. Constraint compliance

- **The store does not exist here** **[M] §3.5**. **No criterion in §5 depends on it.**
  AC1/AC2 run on a synthetic frame; AC3–AC8 need no store. Nothing is conditional-but-
  unverifiable: I have simply excluded the store from the criteria.
- **`signals/` stays gitignored** — `.gitignore:65` **[M]**. Not proposed for commit. AC7
  exists because of it.
- **No paid source.** Google News undocumented, StockTwits registrations closed — both
  recorded (§3.6), neither bought.
- **`nix/module.nix` untouched** (NixOS module; `systemd.user.*` will not evaluate there).
  Units are justfile-installed, matching `justfile:163`.
- **No flake inputs added** for the siblings (`RESEARCH.md` §2 G-C.4). Private repos also
  need `git+https`, not `github:` — avoided entirely by not adding them.
- **Read-only.** No source modified; no commit; scratch only under `/tmp/opencode/gc/`;
  all Python inside `nix develop`. No `nix flake check`, no live train/backtest.

---

## 8. The RED-GRUN obligation

Phase 6 requires any guard, counter or validator to be **shown red on purpose** before it
counts as delivered: (1) the green result with its numbers, (2) one specific named defect,
(3) the red output **verbatim**. Per guard:

| guard | deliberate defect to introduce | red output must read |
|---|---|---|
| **RG1** social append (AC3/AC4) | revert `kraken_social_signals/export.py:149` to `path.open("w")` while leaving `--append` **accepted** — i.e. the flag lies | the preserved-first-line assertion fails, naming the sentinel: `AssertionError: sentinel record 'SOC_SENTINEL' was destroyed by a second write_jsonl(append=True) — lines_before=3, lines_after=2` |
| **RG2** news append (AC3/AC4) | same at `ticker_news_signals/export.py:123` | `AssertionError: sentinel record 'NEWS_SENTINEL' was destroyed by a second write_jsonl(append=True) — lines_before=2, lines_after=2, sentinel_present=False` — note the equal counts, which is exactly why the guard asserts **content**, not count (my §3.1 run reproduced this: 1 line in, marker gone, 2 lines out) |
| **RG3** width delta (AC2) | remove one name from `_SIGNAL_COLUMNS` (`features.py:94-125`) | a **delta** mismatch, e.g. `AssertionError: funding+news+social delta != number of newly reachable signal columns; delta=5, expected=6` — and it must **not** report a bare absolute width, which is the trap `RESEARCH.md` §1.11 records |
| **RG4** timer collision (AC5) | set the news timer's `OnCalendar` to `*-*-* *:17:00` | a message naming **both** units and the shared minute, e.g. `AssertionError: timers share OnCalendar minute 17: kraken-trading-bot-funding.timer, kraken-trading-bot-news.timer` |
| **RG5** column set (AC1) | rename `article_count` → `articles` in the news fixture | a **set-difference**, not a count, e.g. `AssertionError: merge did not add expected columns; missing={'article_count'}, unexpected={'articles'}` |
| **RG6** missing-file (AC7) | point a non-null key at a nonexistent path | `pytest.raises(SignalFileNotFoundError)` satisfied — and the red run must show the **wrong** exception class first (a bare `FileNotFoundError` or a `KeyError`) to prove the guard is real and not vacuous |

**Why RG3 and RG5 are stated as set/delta messages specifically.** A prior pass shipped a
regression test that passed with its fix reverted, and an `audit-findings` check that
reported "nothing found" forever because its regex did not match the repo's own `F-1`
convention. The defence is the same in both cases: **the red output must carry the value
that would have been wrong**, not merely a failure. A count-only assertion could pass
against the wrong axis; a set-difference cannot.

### 8.1 Output from a broken run is not evidence — DISCARD AND RE-RUN

The red-run obligation above says a guard must be *shown wrong* before it counts. Its
mirror is equally load-bearing: **output produced by a run whose environment was broken is
not evidence of anything, in either direction.** It is discarded and the run repeated. It
is not reported, not caveated, and not reasoned from.

**The measured case.** A worktree-audit loop in the 2026-10-03 matrix pass broke `PATH`
mid-iteration, so `git`, `wc`, `grep` and `sed` returned "command not found" for the
later worktrees. The loop's output still *looked* structured, and it was reported anyway:
five of six worktrees were described as "fully merged into master" when **five of six
held commits not in master**, and two were described as "gone" when only their directory
was absent. Nothing about the failure was visible in the output — the lines were
well-formed and confidently wrong. Had that not been caught by an independent patch-id
check before a destructive `team_cleanup` purge, unrecoverable work would have been
deleted on the strength of a broken shell.

**Why the shape is deceptive.** A broken environment does not crash loudly. It emits
plausible-looking values, and a loop that keeps going will happily summarise them. The
tell is not in the output — it is in whether the commands could have run at all.

**The rule.**

1. **Verify the toolchain first in any batch run.** One `command -v git` up front, not
   per-iteration.
2. **A non-zero exit or a "command not found" inside a loop DISCARDS that iteration's
   output** and the iteration is re-run. It is not passed to the next stage, not
   summarised, and not reported with a caveat.
3. **Never let a broken run's output reach a destructive decision.** If a purge, a
   rebase, a `git clean` or a branch delete is being considered on the strength of a
   batch report, that report is re-derived from a run whose exit status was checked.
4. **Cross-check the shape of the claim, not just its existence.** A per-item verdict
   ("fully merged") gets one independent verification (patch-id, tree diff, or a
   second method) before it is acted on. One method that silently degrades to a
   plausible default is not a check.
5. **A broken run is a finding about the harness, not about the subject**, and is
   recorded only AFTER the clean re-run exists — otherwise the record repeats the
   mistake it is describing.

This sits beside the red-run rule deliberately: one says *prove the guard can fail*, the
other says *prove the run could succeed*. A suite of self-certified greens and a pipeline
of confidently-wrong batches fail the same way — neither was ever tested against reality.

---

## 9. Runner-ups, and the specific evidence that sank each

### 🥈 G-A — push `since`/`until` into the read (the audit's own **#1**)

Kept as a **real** candidate and ranked above every other runner-up. It lost on
**verifiability**, not on merit:

- Its payoff is conditioned on an artifact that **does not exist on this host** **[M]**
  `~/Projects/kraken-market-data/store` absent, 0 parquet under `~/Projects`. `AUDIT.md`
  §1.4 and `RESEARCH.md` §1.12/§3.8. So a reviewer **cannot gate it here**, and I may not
  put an unverifiable thing in an acceptance criterion.
- **Its obvious implementation is a silent regression** (`RESEARCH.md` §1.5): `since` is
  *already* the live-fetch cursor at `data.py:1385`, so passing it at the call sites turns
  "fetch the trailing N pages" into "page forward from `since` for N pages" — on a deep
  store that fetches ancient bars and never reaches the tail.
- **The naive form destroys the honest error path** (`RESEARCH.md` §2 G-A): an empty
  *windowed* read raises the **generic** `NotEnoughDataError` (`data.py:1402-1404`) instead
  of `PinnedWindowUnavailableError`, and the 11-line `STORE_SEED_HINT` fix text only prints
  from `data_window._guard_pinned_coverage`, which needs the **whole frame** to quote
  `available_span`. **Pushing the read down replaces the honest error with a lie.**

**Not dropped.** It is cheap later and its harness already exists
(`tests/test_rl_data_store.py:194` covers `since`/`until` end-to-end), and it is
**independent of G-C** (`RESEARCH.md` §2) — the decision not to couple them is deliberate.

### 🥉 G-B — paper-trade venue provenance + per-tick fetch

**Highest value-per-line in the entire audit** (one-line forward at
`paper_trade.py:298-309`, plus an AST guard that catches the next leg that forgets) and
the natural follow-on. It lost to G-C on two axes only:

- **It adds zero observation columns.** G-C adds +6 width / +4 usable.
- **Its second half is not a data-pipeline decision.** `RESEARCH.md` §1.6 **[M]**: paper
  trade **does not refit** (`paper_trade.py:196-197` loads frozen stats from the artifact;
  `environment.py:189-190` fits only `if stats_for(...) is None`) — so the "live leg
  refits per tick" hazard is **already absent and any rewrite must preserve it** — and
  `compute()` is **flat in frame length** (26.13 / 26.25 / 25.84 / 25.02 ms at
  1440/800/400/200 rows). An incremental recompute buys ~1 ms of a 60 s tick, **0.04%**.
  **All the waste is two HTTP page calls per minute.** The fix is network/caching, and the
  incremental feature-matrix path is **not warranted** — despite `data.py:1319-1323`'s own
  `.. todo::` asking for it.

### 4️⃣ G-D — `order_book_imbalance` (prior G1, named time-critical twice)

Lost on a **measured physics** result, not on effort (`RESEARCH.md` §1.8 **[M]**):

- Top-10 imbalance autocorrelation **+0.51 at a 4 s lag, +0.01 at a 16 s lag** (20
  snapshots, 77.5 s span; sd 0.390). Bar-mean estimator standard error **0.390 at 1
  poll/bar vs 0.050 at 60 polls/bar (7.8×)**. **The bar is 3600 s; the signal decorrelates
  in tens of seconds.**
- Therefore **a REST poller cannot carry it.** G-D needs a **long-lived WebSocket
  subscriber** — a deployment surface this repo does not have (`nix/module.nix` installs no
  unit) and which the existing `just funding-timer` shape cannot express. That is a
  materially bigger commitment than `AUDIT.md` §4/G-D's blast-radius paragraph implies, and
  it is exactly the kind of commitment an architect should price before choosing.
- Plus `RESEARCH.md` §1.9 **[M]**: widening `_SIGNAL_COLUMNS` for `bid_vol`/`ask_vol` costs
  exactly +1 column, but on a **sparse** producer `first_tradable_index` moves **24 → 54**
  on a 200-bar frame, because `POINT_IN_TIME_EXOGENOUS_COLUMNS` (`features.py:171-173`)
  does not contain it, so its NaNs read as warm-up. **A new exogenous column must be
  declared point-in-time or it silently shortens every episode** — the exact defect class a
  prior pass found with `spread`.

### 5️⃣ G-E — retry/backoff on the fetch path

Lost on **scope**: the upstream half lives in `kraken_api`, which is a **flake input
pinned at `81de597`** — a lock bump invalidates the nix build cache and CI, and the
sibling must be pushed to GitHub first. Only the consumer half (a bounded retry around
`data.py:1126`) is reachable from this repo, and `RESEARCH.md` §1.7 shows it is a
**mis-classification bug** (`EService: Throttled:` matches neither pattern at
`transport.py:45-48`; Kraken signals limits **in the JSON body on HTTP 200** — there is no
429 and no `Retry-After`), not a missing-retry bug.

### 6️⃣ G-F — store holes invisible to the feature pipeline

Lost on **the machinery already existing** (`RESEARCH.md` §1.10): `tools/store_gap_scan.py`
is 433 lines, tested, wired to `just store-verify`, and already detects gaps, labels the
seam by name and flags off-grid bars. **Nothing consumes its verdict** — so this is a
plumbing gap, not a build gap. But every store-cost and store-hole figure is
`[unverified — needs a seeded store]` (§3.5), and the audit's *158 bars / 28 gaps / 39 h*
numbers are `[I]` inferred from a config comment. **Nothing to gate on.**

### Rejected outright

- **A new sibling project** — rejected in §2.1: the sources already exist as two working
  CLIs. Shipping a `NEW-DATA-SOURCE` here would be inventing a repo to re-implement
  `ticker-news-signals` and `kraken-social-signals`.
- **Four empty categories** (cross-exchange, macro, on-chain, research, options — `AUDIT.md`
  §5) — nothing here has a seam, a config key or a producer. Each is a scoping pass, not a
  slice (`PLAN.md` §4/G7).

---

## 10. What the next phase inherits

1. **`RESEARCH.md` §1 is binding and §1.1 is the load-bearing correction.** Both producers
   truncate — *I proved it by execution* (§3.1), not by reading. Do not schedule before
   `:16`–`:21` land.
2. **Two channels cost +5 each but only +8 together; +6 when funding is already active.**
   Never write a literal width (§3.2).
3. **Two hypotheses already refuted** — do not re-derive them: the news file's `ETH_USD`
   ticker is fine (§3.3), and freshness columns do not stack (§3.2).
4. **The consumer needs no Python change** — `_SIGNAL_COLUMNS` already carries all six
   columns and none is a builder-input (§2).
5. **Timer minutes 23 and 29, never 17** (§3.4), and the funding timer is **not installed**
   on this host **[M]** — so "the schedule already exists" is false and AC5 must not assume it.
6. **`+6 width, +4 usable`** is the honest headline; `stt_tilt` and `novelty_flag` are dead
   for reasons no schedule fixes (§3.6) — and **no acceptance criterion may depend on them**.

---

*Written by the architect, pass 2026-10-03, at HEAD `7534ace`. Read-only: no source file
modified, no code written, nothing committed. Every width is a delta on a stated fixture;
every direction marked [R] was executed, and no [G] appears in an acceptance criterion.*
