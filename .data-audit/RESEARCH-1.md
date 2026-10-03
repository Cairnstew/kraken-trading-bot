# RESEARCH-1.md — G-C: the two unreachable exogenous signal channels

**Phase 2 research, read-only. Pass dated 2026-10-03.**
Slice: gap **G-C** from `AUDIT.md` §4 (plus §2.4 widths, §1.4 host state, §7 Q1/Q2).
Repo: `/home/seanc/Projects/kraken-trading-bot`, HEAD `f6d9118`.
Nothing was modified in any repo. Nothing committed. All scratch in `/tmp/opencode/`.

Evidence tiers, per `AUDIT.md` §0: **[M]** measured (command run, result quoted),
**[C]** cited (read off a file at the line given), **[I]** inferred,
**[unverified]** could not be checked.

---

## 0. Headline: the gap is real, but the task framing is wrong in three places

`AUDIT.md` §4 G-C says the missing work is "the schedule and the wiring". That is
true of the **consumer**, and I confirmed the consumer is genuinely ready (§1.3
below, measured). It is **not** true of the **producers**. Three corrections, each
of which changes what the builder has to do:

| # | AUDIT.md / task premise | What I measured | Consequence |
|---|---|---|---|
| **C1** | "the *file format* is done… only the schedule and the wiring are missing" | The file format is done, but **neither producer can append**. Both `write_jsonl` open the file with `"w"`. **[M]** | An hourly `--output` run **destroys** history every hour. A timer wired naively produces a permanently-1-hour file. This is producer-side work, not wiring. |
| **C2** | `ticker-news-signals` is available like `kraken-funding-rates` | **`nix run ~/Projects/ticker-news-signals#ticker-news-signals` does not build.** `nix/gnews.nix` is broken. **[M]** | The funding-rates production pattern does **not** transfer to news. The news channel has no hermetic build. |
| **C3** | activating the 6 columns is the win | **2 of the 6 are structurally degenerate on day one**: `stt_tilt` (StockTwits removed the field) and `novelty_flag` (a 15-min window vs an hourly pull). **[M]** | +6 width for +4 usable dimensions. Safe but partly wasted. |

**Answering the `NEW-DATA-SOURCE` tail the brief asked about: yes, partially.**
The *consumer* tail is `IMPROVE-EXISTING` and is nearly free. The *producer* tail
is real: **`ticker-news-signals` needs a one-line fix in its own repo
(`nix/gnews.nix`) before it can be scheduled at all**, and **both** repos need an
append mode (or the timer needs a shell-level append wrapper) before an hourly
schedule is safe. This is the gap's `NEW-DATA-SOURCE` component and it is not
optional.

---

## 1. The two producers' real CLIs (question 1)

### 1.1 They are NOT on PATH, and NOT in this repo's dev shell — same as funding-rates **[M]**

The brief said to establish this rather than assume it. Established:

```
$ for b in ticker-news-signals kraken-social-signals kraken-funding-rates kraken-trading-bot; do
    command -v $b || echo "$b NOT-ON-PATH"; done
ticker-news-signals      NOT-ON-PATH
kraken-social-signals    NOT-ON-PATH
kraken-funding-rates     NOT-ON-PATH
kraken-trading-bot       NOT-ON-PATH
```

Inside this repo's dev shell (`flake.nix:52-64` builds `python3.withPackages`,
which lists neither package):

```
$ nix develop --command bash -c '...'
ticker-news-signals      NOT-ON-PATH
kraken-social-signals    NOT-ON-PATH
kraken-funding-rates     NOT-ON-PATH
kraken-trading-bot       /nix/store/592rc607…-kraken-trading-bot-0.1.0/bin/kraken-trading-bot
--- importable? ---
ModuleNotFoundError: No module named 'ticker_news_signals'
ModuleNotFoundError: No module named 'kraken_social_signals'
--- gnews/vader present? ---
ModuleNotFoundError: No module named 'gnews'
ModuleNotFoundError: No module named 'vaderSentiment'
```

**The prior pass's finding holds for these two as well.** Note the last two lines:
`gnews` and `vaderSentiment` are absent too, so the documented
`python ~/Projects/ticker-news-signals/cli.py pull …` string in
`configs/default.yaml:53-54` **cannot work as written** from this dev shell — it
would `ModuleNotFoundError` on `from gnews import GNews`
(`ticker_news_signals/client.py:23`). That hint string is one of the stale
comments this pass was told to look for.

### 1.2 Real CLI surfaces **[C]** from argparse, confirmed by running `--help`

**`ticker-news-signals`** (`ticker_news_signals/cli.py:51-59`):

```
subcommands: pull, version
pull --ticker TICKER      (required)
     --output PATH        (default: stdout)
     --lookback-hours N   (default: 1)
     --json               (pretty envelope instead of NDJSON)
```

**`kraken-social-signals`** (`kraken_social_signals/cli.py:61-74`):

```
subcommands: pull, list, version
pull --ticker TICKER      (required)
     --output PATH        (default: stdout)
     --lookback-hours N   (default: 24)   <-- NOTE: 24, not 1
     --json
```

**Neither has an `--append` flag.** That is the whole problem in one sentence.
`kraken-funding-rates` has one (`justfile:194`, `.service.in:39`); these two do not.

The default-`--lookback-hours` asymmetry matters for scheduling: news defaults to
a 1-hour window, social to 24. See §1.5.

### 1.3 **Append vs overwrite: MEASURED, truncate** — the decisive finding **[M]**

Both `write_jsonl` implementations open the file for **writing**, not appending:

- `ticker_news_signals/export.py:123` — `with path.open("w", encoding="utf-8") as fh:`
- `kraken_social_signals/export.py:149` — `with path.open("w", encoding="utf-8") as fh:`

A `grep -rn "open(" --include='*.py'` across both repos finds **no** append-mode
file write anywhere in either package. `pipeline.py` uses `list.append` (in-memory)
only.

Proven empirically with a marker line:

```
news   RUN1 --lookback-hours 3  → 4 lines;  echo MARKER >> file → 5 lines
news   RUN2 --lookback-hours 1  → 2 lines;  grep -c MARKER → 0   ← MARKER GONE
social RUN1 --lookback-hours 24 → 9 lines;  echo MARKER >> file → 10 lines
social RUN2 --lookback-hours 24 → 9 lines;  grep -c MARKER → 0   ← MARKER GONE
```

**Truncate confirmed for both.** Consequences:

- **news, hourly, default `--lookback-hours 1`**: the file is rewritten to hold
  *only the current hour* every hour. History is destroyed continuously. Over a
  721-bar backtest frame there would be **at most 1–2 hours of coverage**, so
  `signal_max_age_hours: 12` would be satisfied only for the newest bars and the
  other ~719 bars would be `signal_observed=False` with a historical 0.0 fill.
- **social, hourly, default `--lookback-hours 24`**: the file is a **sliding
  24-hour window**, ~9–10 lines, refreshed each hour. This is *self-healing* (a
  re-fire inside the same hour is idempotent because the whole window is
  recomputed and the last pull wins per hour at the seam, `data.py:776-781`), but
  it is a **window, not a log** — it never accumulates beyond 24 h. For a
  721-bar (30-day) training frame, ~94% of bars would be unobserved.

So the fix is **not** "add a timer". The fix is "add a timer **and** stop the
producer from truncating", i.e. either an `--append` flag in each sibling or a
shell-level append in the unit (`>> "$OUT"` with `--output` pointed at stdout,
i.e. omit `--output` entirely).

### 1.4 Auth / `.env` needs **[C]** — both fully keyless

- `ticker-news-signals/.env.example:3-5` — "ticker-news-signals needs NO API keys:
  GNews (Google News RSS) is keyless, and VADER sentiment is a local rule-based
  scorer." Only optional tuning: `TICKER_NEWS_LANGUAGE`, `TICKER_NEWS_COUNTRY`,
  `TICKER_NEWS_PERIOD`.
- `kraken-social-signals/.env.example:3-11` — "needs NO API keys: both sources are
  keyless plain REST." Optional: `STOCKTWITS_ACCESS_TOKEN`, `SOCIAL_LOOKBACK_HOURS`,
  `STOCKTWITS_BASE_URL`, `FNG_BASE_URL`.

Both CLIs also write a JSON log to `$XDG_STATE_HOME/<name>/…` unless
`--no-log-file` (`cli.py:104-122` news, `:124-142` social). Under a systemd user
unit `$XDG_STATE_HOME` may be unset; the `_xdg_state_dir()` helper should be read
before relying on it in a unit — **[unverified]** whether it falls back cleanly
outside a login session.

**One stale comment found** (`kraken-social-signals/.env.example:8-10`):
"Optional: a StockTwits application access token. StockTwits throttles the
keyless path harder; a registered app token buys headroom." See §5.2 — **new
registrations are closed**, so that token is not obtainable going forward. The
comment is wrong as advice.

### 1.5 Installability: pip vs flake, and the news flake is broken **[M]**

Both are ordinary PEP 621 projects with `[project.scripts]` console entry points
(`ticker-news-signals/pyproject.toml:16-17`, `kraken-social-signals/pyproject.toml:16-17`).
Both have `packages.default`, so `nix run ~/Projects/<repo>` *should* work.

**`kraken-social-signals` — `nix run` works. [M]**
```
$ nix run /home/seanc/Projects/kraken-social-signals#kraken-social-signals -- version
kraken-social-signals 0.1.0
$ nix run … -- pull --help
usage: kraken-social-signals pull [-h] --ticker TICKER [--output OUTPUT]
                                  [--lookback-hours LOOKBACK_HOURS] [--json]
```

**`ticker-news-signals` — `nix run` FAILS TO BUILD. [M]**
```
$ nix run /home/seanc/Projects/ticker-news-signals#ticker-news-signals -- version
…
> Executing setuptoolsBuildPhase
> File "/build/gnews-0.8.2/nix_run_setup", line 8, in <module>
>   File "setup.py", line 3, in <module>
>     with open('requirements.txt') as f:
> FileNotFoundError: [Errno 2] No such file or directory: 'requirements.txt'
error: Cannot build '/nix/store/9z97rmk22p0hi25l173yn979475aibs4-ticker-news-signals-0.1.0.drv'.
```

**Root cause, confirmed against the PyPI sdist.** `nix/gnews.nix:19` sets
`format = "setuptools"`, which runs `python setup.py`. The unpacked
`gnews-0.8.2/setup.py:3` is:
```python
with open('requirements.txt') as f:
    requirements = f.read().splitlines()
```
and the sdist **does not contain `requirements.txt`**. Listing it:
```
$ tar tzf gnews-0.8.2.tar.gz
gnews-0.8.2/LICENSE.txt   gnews-0.8.2/PKG-INFO   gnews-0.8.2/README.md
gnews-0.8.2/gnews/…      gnews-0.8.2/gnews.egg-info/requires.txt   ← requires.txt, not requirements.txt
gnews-0.8.2/setup.cfg    gnews-0.8.2/setup.py   gnews-0.8.2/tests/…
```
So this is **upstream gnews packaging breakage** (its sdist is not
`setup.py`-buildable), not a bad hash in `nix/gnews.nix`. It has simply never
been exercised, because nothing has ever built this flake.

The fix is a one-line change **in the `ticker-news-signals` repo**: build from
the wheel rather than the sdist (`format = "wheel"`, or `format = "pyproject"`
with `pname`/`version` resolving to the `.whl`; `fetchPypi` will pick the wheel).
Note that `format = "pyproject"` alone is **not** obviously sufficient, because
setuptools' PEP 517 backend still executes `setup.py` for a project with no
`[project]` table — **[unverified]**, I did not test it.

**Interim path that does work on this host [M]:** `ticker-news-signals/.venv`
exists and is functional —
```
$ .venv/bin/python -c "import gnews, vaderSentiment, ticker_news_signals; print('OK', …)"
OK 0.1.0
$ .venv/bin/python cli.py pull --help      # → works
```
All my news measurements used it. `kraken-social-signals` has **no** `.venv`; its
`nix run` path is the working one.

**Recommendation for the builder:** fix `nix/gnews.nix` in the sibling (small,
well-understood, testable with one `nix run`), and until then do **not** point a
timer at the news CLI. A timer that fails on first fire and never recovers is
worse than no timer, because `Persistent=true` will keep re-firing it.

---

## 2. Scheduling patterns for hourly per-ticker pollers (question 2)

### 2.1 The `nix/module.nix` constraint still holds — confirmed twice **[M]/[C]**

`nix/module.nix:28` is `{ config, lib, pkgs, ... }` with
`options.services.kraken-trading-bot` (`:45`) and its `config` block
(`:212-238`) sets only `environment.systemPackages` and
`systemd.services."kraken-trading-bot-env"`. **There is no `systemd.user.*`
anywhere in it** — I grepped; there is nothing to grep.

Confirmed against the option namespaces:

- **NixOS** `systemd.user.*` contains only `systemd.user.generators` and
  `systemd.user.paths.<name>.*` — **no `systemd.user.<name>.timer`**.
- **home-manager** has exactly the missing option:
  `systemd.user.timers` — *"Definition of systemd per-user timer units."*

So the existing decision (`configs/default.yaml:75-78`,
`systemd/kraken-trading-bot-funding.service.in:8-10`) is correct and still
necessary: **a NixOS module cannot declare a per-user timer.** Installation must
stay with `just funding-timer` (`justfile:163-177`) writing into
`$HOME/.config/systemd/user/`.

### 2.2 Does the existing template extend as-is? Mostly — with three edits **[C]**

`justfile:163-177` is a clean, reusable pattern: `sed` two placeholders
(`@PAIR@`, `@OUTPUT@`) out of a checked-in `.service.in` into
`~/.config/systemd/user/`, symlink the `.timer`, `daemon-reload`,
`enable --now`.

For two more channels it extends as-is **structurally** — but the `.service.in`
placeholder set needs to grow, because the new `ExecStart` lines need a
**third** substitution: the **ticker**, which for these two CLIs is `--ticker`
(whereas funding uses `--pair`):

| | funding | news | social |
|---|---|---|---|
| flag | `--pair @PAIR@` **[C]** `.service.in:39` | `--ticker <PAIR>` **[C]** `_SIGNAL_CHANNELS` hint, `data.py:157-161` | `--ticker <PAIR>` **[C]** `data.py:173-175` |
| append | `--append` (has the flag) | **no flag** | **no flag** |
| binary | `nix run …/kraken-funding-rates#kraken-funding-rates` | **builds broken** | `nix run …/kraken-social-signals#kraken-social-signals` |

So: `@TICKER@` (or reuse `@PAIR@` for the value and just spell the flag
per-unit), plus a redirect strategy for the two that cannot append.

**Note the timer itself is reusable verbatim** — `kraken-trading-bot-funding.timer`
is already at `OnCalendar=*-*-* *:17:00` (`:19`), `Persistent=true` (`:21`),
`RandomizedDelaySec=120` (`:22`). Copying it and changing `Unit=` (`:23`) is
sufficient; **but two timers must not share the same minute** or they fire
together and spike. `:17` is already taken; use e.g. `:23` and `:29`, and keep
`RandomizedDelaySec` to spread them.

### 2.3 `OnCalendar` + `Persistent` vs `OnUnitActiveSec` vs cron — the real semantics **[C]**

All from `systemd.timer(5)`, https://man7.org/linux/man-pages/man5/systemd.timer.5.html
(man page obtained from systemd's upstream git repo 2026-08-03; local systemd
`262~devel`).

**`Persistent=` — catch-up is ONE run, not one per missed interval. No storm.**
> "If true, the time when the service unit was last triggered is stored on disk.
> When the timer is activated, the service unit is triggered immediately **if it
> would have been triggered at least once** during the time when the timer was
> inactive. […] Note that **this setting only has an effect on timers configured
> with `OnCalendar=`.**"

This is the single most important sentence for this gap. Three consequences:

1. **There is no catch-up storm.** 48 hours of downtime produce **one**
   activation on resume, not 48. Whatever hour it lands in, one record is written.
2. **`Persistent=true` is silently inert on `OnUnitActiveSec=`.** A monotonic
   timer built that way would get no catch-up at all.
3. Because catch-up fires *once*, **the run must be idempotent within its
   hour** — otherwise a resume at 11:47 writes an 11:00 record with stale data
   and the next scheduled run at 12:17 rewrites the same hour.

**Suspend behaves the same way — also coalesced to one activation.**
> "When a calendar timer elapses while the system is sleeping it will not be
> acted on immediately, but once the system is later resumed it will catch up
> and process all timers that triggered while the system was sleeping. **Note
> that if a calendar timer elapsed more than once while the system was
> continuously sleeping the timer will only result in a single service
> activation.**"

So "catch-up storm after downtime" **does not exist** on this pattern. Good news,
and it means a single hourly timer is safe on a laptop.

**`OnUnitActiveSec` is the wrong choice here, for two reasons [C]:**
> "These are monotonic timers, independent of wall-clock time and timezones. If
> the computer is temporarily suspended, the monotonic clock generally pauses,
> too."

1. It **drifts** against the UTC bar grid (a poll at boot+1h lands at an
   arbitrary minute, so `novelty_flag`'s 15-minute window — §5.3 — becomes a
   lottery rather than a systematic miss).
2. It **pauses during suspend**, so hours are silently skipped with no catch-up.

**Clock/timezone.** `OnCalendar=` is wall-clock. A timer with `OnCalendar=` gets
`After=time-set.target time-sync.target` automatically ("Timer units with at
least one `OnCalendar=` directive acquire a pair of additional `After=`
dependencies on time-set.target and time-sync.target"). Accuracy defaults to
`AccuracySec=1min`. `OnCalendar=*-*-* *:17:00` is interpreted in the **local**
timezone — on this host that is BST (`systemctl list-timers` shows "BST"), so it
fires at :16 UTC in summer. **For an hourly cadence this is harmless** (it still
hits every hour, just at a shifted minute); it would matter only for a
sub-hourly cadence. **[I]** — I did not verify the local-timezone parsing rule
against `systemd.time(7)` directly.

**Overlapping runs are already impossible.** "in case the unit to activate is
already active at the time the timer elapses it is not restarted, but simply
left running" — relevant only if a pull ever exceeds the hour; with
`TimeoutStartSec=300` (`.service.in:40`) a wedged pull is killed, not stacked.

**Comparison summary**

| pattern | catch-up after downtime | drift vs UTC bar grid | verdict here |
|---|---|---|---|
| `OnCalendar=` + `Persistent=true` | **1 coalesced run** (never a storm) | none (wall-clock, hourly) | **use this** — already shipped |
| `OnCalendar=` without `Persistent` | none | none | acceptable, but loses the coalesced resume run for free |
| `OnUnitActiveSec=` | **none** — `Persistent` is inert on it | drifts; pauses on suspend | reject |
| plain cron (`crontab -l`) | **none** | none | reject: no coalescing, no `systemctl list-timers` visibility, and the repo would need a second install mechanism |
| Python scheduler lib (APScheduler etc.) | whatever you write | whatever you write | reject: adds a long-running process to supervise for what three `OnCalendar` lines do. Also would need its own persistence story to survive restart |

**Why cron loses, specifically:** with `Persistent=true`, a 3-day-laptop sleep
degrades to *one* current pull. With cron, the same event produces *zero* pulls
and the channel silently goes stale until the next fire — and `signal_age_hours`
is the only thing that notices. That is the "absence is not neutral" machinery
(`data.py:833-849`) doing its job, but it converts a scheduling gap into dead
observation columns rather than a single stale hour.

### 2.4 Making a run idempotent **[C]** + the seam already tolerates duplicates

The seam is explicitly built for a re-appending producer —
`merge_extra_features` docstring property 2 (`data.py:648-652`):
> "**De-duplicated hours** — the sibling's documented hourly cron re-appends the
> current hour on every pull, so the floored index repeats. Records are
> de-duplicated on the raw timestamp and then collapsed to one per floored hour
> (last write wins, i.e. the most recent pull) […]"

and the code does it (`data.py:776-781`): exact-duplicate timestamps keep the
later line, stable sort, floor to hour, `groupby(level=0).last()` — and `.last()`
is per-column and skips nulls. `.service.in:29-34` states the same contract for
the funding unit: "A re-fire inside the same hour […] appends a duplicate line,
which is harmless and self-limiting."

So **duplicates are already free.** Idempotency therefore needs no extra code
once append exists. **The corollary is the actual risk: a non-appending producer
is worse than a duplicating one**, because truncation is not something the seam
can repair — the hour is simply gone.

### 2.5 Host state relevant to scheduling **[M]**

- **The existing funding timer is NOT installed or enabled on this host.**
  `systemctl --user list-timers --all` lists 6 timers; none is
  `kraken-trading-bot-funding.timer`. `~/.config/systemd/user/` holds only
  home-manager symlinks. Consistent with `signals/eth_usd_funding.jsonl` being
  1 line with mtime 2026-10-02 01:28. **`AUDIT.md` §1.3 describes the shipped
  units but does not say they are inactive — they are.**
- **`Linger=yes`** (`loginctl show-user $USER -p Linger`). The
  `.service.in:21` recommendation (`loginctl enable-linger $USER`) is already
  satisfied, so a new user timer **will** fire while logged out. Good.
- systemd `262~devel`.

---

## 3. Flake wiring for two more private siblings (question 3)

### 3.1 `github:` cannot fetch them; `git+https:` can — measured both ways **[M]**

Visibility via `gh repo view` **[M]**:

| repo | visibility | default branch | spelling that works |
|---|---|---|---|
| `kraken-python` | **PUBLIC** | main | `github:` — which is what `flake.nix:6` uses |
| `kraken-market-data` | **PRIVATE** | main | `git+https:` — which is what `flake.nix:7` uses |
| `kraken-funding-rates` | **PRIVATE** | **master** | not a flake input here; used via `nix run ~/Projects/…` |
| `ticker-news-signals` | **PRIVATE** | main | see below |
| `kraken-social-signals` | **PRIVATE** | main | see below |

The repo's existing mixed spelling is therefore *correct and deliberate*, not
inconsistent: public → `github:`, private → `git+https:`.

`nix config show` has **`access-tokens = ` (empty)** **[M]** — no GitHub token is
configured for flake fetching — while `git config --global credential.helper` is
`store --file ~/.git-credentials` with a GitHub credential present. That is
exactly the asymmetry the prior pass described.

**Proof, both directions, in a scratch flake (`/tmp/opencode/flaketest`):**

```
TEST A — inputs as github:
  error: unable to download
    'https://api.github.com/repos/Cairnstew/kraken-social-signals/commits/HEAD':
    HTTP error 404
  { "message": "Not Found", … }

TEST B — inputs as git+https:
  • Added input 'kraken-social-signals':
    'git+https://github.com/Cairnstew/kraken-social-signals?ref=refs/heads/main&rev=71ca27d67…'
  • Added input 'ticker-news-signals':
    'git+https://github.com/Cairnstew/ticker-news-signals?ref=refs/heads/main&rev=23dc965696…'
```

Both locked revs — `23dc965…` and `71ca27d…` — **match the revs `AUDIT.md` §4
G-C cites**, which independently corroborates the audit. Required spelling:

```nix
ticker-news-signals.url   = "git+https://github.com/Cairnstew/ticker-news-signals";
kraken-social-signals.url = "git+https://github.com/Cairnstew/kraken-social-signals";
```

and `outputs` must thread both through
(`outputs = { self, nixpkgs, kraken-python, kraken-market-data, ticker-news-signals, kraken-social-signals }:`, `flake.nix:10`).

### 3.2 Lock-bump implication — proven, not assumed **[M]**

A pinned input resolves to a **store path keyed on the locked `rev`**, not to the
local checkout:

```
$ nix eval --raw --impure \
    --expr '(builtins.getFlake (toString ./.)).inputs.ticker-news-signals.outPath'
/nix/store/lam3jdnkq5wf9vlm115zq000vzkfhy11-source     ← rev 23dc965…
   vs
/home/seanc/Projects/ticker-news-signals               ← the actual checkout
```

Today the two happen to coincide **[M]**: local `HEAD` == locked `rev` for both,
and both trees are clean (`git status --porcelain` → 0 lines each). So a lock
bump is currently a no-op. **It stops being a no-op the moment either sibling is
edited — which §1.3 and §1.5 require.** After fixing `nix/gnews.nix` and adding
append, the pinned input will still serve the *old* `nixpkgs`-era gnews until
`nix flake lock --update-input ticker-news-signals` (or
`nix flake update ticker-news-signals`) runs, and until that commit is pushed.

### 3.3 Recommendation: **do not add these as flake inputs at all**

There is a **third option already established in this repo** for exactly this
situation, and it is used for the *other* private sibling:
`justfile:287-290` —
> "Shared locations: the sibling seeder is not a flake input here, so it is named
> by path and put on PYTHONPATH rather than built."

`deep_history_dir := env_var_or_default("KTB_DEEP_HISTORY_DIR", "~/Projects/kraken-deep-history")`
(`justfile:290`), consumed as `PYTHONPATH={{deep_history_dir}}:$PYTHONPATH`
(`justfile:298, 321, 329, 353`). `kraken-deep-history` is **private and not a
flake input** — confirmed: `flake.nix:4-8` lists only nixpkgs, kraken-python,
kraken-market-data.

For G-C the path-based approach is **strictly better**, because:

1. **Sibling edits are visible immediately.** No commit → push →
   `nix flake lock --update-input` round trip per fix. Given §1.3/§1.5 both
   siblings *must* change, this removes a whole class of "I fixed it and the
   timer still runs the old code" failure.
2. **A pinned input would pin the news build failure** (§1.5) into
   `flake.lock` — making the breakage durable and reproducible rather than a
   local accident.
3. It needs **no new flake input, no lock churn, and no `access-tokens`**, so
   §3.1's 404 hazard does not arise at all.
4. It matches `justfile:288`'s stated reasoning verbatim.

The cost is loss of hermeticity — the same trade-off `justfile:273-281`
already documents at length for the seeder (a fallback-CSV seed writes files
`MarketDataStore.read` cannot see, and the seeder's own dev shell lacks
pandas/pyarrow so seeding there silently degrades). **That same hazard applies
here and must be checked, not assumed:** `ticker-news-signals`' dev shell carries
only `gnews vaderSentiment pytest`, and `kraken-social-signals`' only
`requests python-dotenv pytest` **[C]** — neither carries `requests`-for-`gnews`'s
own needs beyond the overlay, and `kraken-social-signals`' `python-dotenv` is a
hard import at `cli.py:30-35` (guarded by `try/except ImportError`, so it
degrades). Running under the **main repo's** dev shell
(`flake.nix:52-64`, which has `requests`, `pyyaml`, `python-dotenv`) plus
`PYTHONPATH=<sibling>` would satisfy both. **The builder should verify that
combination before shipping it** — I did not, because it is a build decision and
the brief reserves `nix flake check` for the lead.

**Dev-shell addition, if wanted anyway:** `flake.nix:52-64` would need `gnews` and
`vaderSentiment`, which **are not in nixpkgs** — `ticker-news-signals/flake.nix:13-15`
says so and injects them via an overlay from `./nix/gnews.nix`,
`./nix/vader-sentiment.nix`. So adding news to the main dev shell means copying
two overlay derivations into this repo — i.e. duplicating the very packaging that
is currently broken. **Another reason to use `nix run` / `PYTHONPATH` instead of
a dev-shell merge.**

---

## 4. Retention / aging — AUDIT.md §7 Q1, answered directly

### 4.1 The premise is factually wrong: the funding file is **not** at HEAD **[M]**

AUDIT.md §1.4 and §7 Q1 both describe `signals/eth_usd_funding.jsonl` as "1 line
**at HEAD**". It is 1 line **on disk**, and it is **not tracked by git**:

```
$ git ls-files signals/ models/
models/.gitkeep                     ← models/ tracks nothing but .gitkeep

$ git check-ignore -v signals/eth_usd_funding.jsonl
.gitignore:65:signals/    signals/eth_usd_funding.jsonl

$ git log --oneline -- signals/
(empty)

$ git log --all --oneline --diff-filter=A -- 'signals/*'
(empty)                             ← never added, in any branch

$ git status --short
(empty)                             ← invisible: ignored, not untracked
```

`.gitignore:63-65`:
```
# Exogenous-signal JSONL (regenerated by `just funding-pull` / the
# kraken-trading-bot-funding.timer unit; see configs/default.yaml)
signals/
```

**So the question "should the backfilled funding file be committed?" is already
answered by the repo, and the answer is no.** A fresh clone has **no `signals/`
directory at all** — and the shipped default points at it
(`configs/default.yaml:96`, `funding_features_file: ~/Projects/kraken-trading-bot/signals/eth_usd_funding.jsonl`,
non-null). Per `merge_extra_features` property 5 (`data.py:655-668`), a non-null
key whose file is missing **raises** `SignalFileNotFoundError`. So on a fresh
clone with no `just funding-pull` ever run, `train` **fails loudly** rather than
silently dropping the channel. That is a defensible design (a declared intent
must be backed by a file) but it does mean "clone and run" does not work today,
and this is worth knowing before adding two more non-null keys.

### 4.2 Recommendation: **keep `signals/` gitignored. Do not commit any signal file.**

Reasoning, not a survey:

1. **The repo already decided, and consistently.** `signals/` is ignored, has
   never been tracked in any branch, and `.gitignore`'s own comment states the
   regeneration path. `models/` follows the same discipline for the binary
   artifacts (`.gitignore:51-54` ignores `model.zip` / `normalization.npz`;
   `justfile:427` "Trained model zips/npz (gitignored) left in place"). Only
   `.gitkeep` is tracked. **Two channels, one rule: generated data is not
   versioned.** Breaking that for `signals/` alone would be the odd move.

2. **A committed snapshot buys a fixed window and then lies.** The funding
   history is a rolling ~366-day hourly window **recomputed on every call**
   (`justfile:200-201, 217-219`), so it ages out from the far end. A snapshot
   committed today is simultaneously (a) increasingly irrelevant as history and
   (b) the *only* thing a fresh clone has. Every reader of that clone would
   train on a window whose newest bar is as old as the last commit — and nothing
   in the artifact records that age. `signal_age_hours` is computed against the
   *bar* index, so it would read healthy on exactly the stale bars that matter.

3. **The size argument cuts the other way than one might assume, which weakens
   the "too big to commit" objection but does not rescue committing.** A full
   backfill is ~8,792 records ≈ 4 MB of JSONL (`justfile:198-199`, measured
   2026-10-02). That is committable. The reason not to is **staleness, not
   size** — and by the same token there is no reason to commit a *small* one
   either, since a small one is strictly more stale per byte.

4. **Git is the wrong tool for a rolling append-only log.** The file's natural
   lifecycle is "append ~1 line/hour, grow, occasionally top up". That is a log,
   and `AUDIT.md` §4 G-C already names the right discipline: *"the same 'log, not
   state' discipline the funding channel uses."* A git-tracked file under
   continuous hourly append is a merge-conflict generator across every clone.

**What I would do instead** (concrete, and it costs nothing):
- Leave `.gitignore:65` alone.
- Make the **regeneration path** discoverable instead of vendoring the data —
  which `justfile:163-177` + `justfile:186-194` already do for funding, and which
  the two new recipes should mirror.
- Record the *provenance* in git, not the *payload*: the `signal_max_age_hours`
  bound plus the `signal_observed` / `signal_age_hours` pair the agent already
  sees (`data.py:833-849`) is the designed answer to "how old is this?". That
  mechanism is stronger than a git timestamp, because it is per-bar and visible
  to the policy.
- **A pre-flight guard would be the one genuinely useful addition**: a check that
  the configured signal files exist and are non-empty, so "fresh clone" fails
  with an actionable message naming the justfile recipe, rather than at the first
  `merge_extra_features` call.

### 4.3 One thing that *should* change: `models/` tracks nothing

`git ls-files models/` → `models/.gitkeep` only **[M]**, and `models/` is empty
on disk except `.gitkeep` **[M]**, matching AUDIT.md §1.4. So `AUDIT.md`'s
"every `normalization.npz` in `models/` becomes stale" is true in principle and
**vacuous today** — there is nothing to invalidate. This is the strongest
argument that the width change is currently free (§6).

---

## 5. Freshness/cadence vs the bar grid — real limits and ToS posture (question 5)

Context **[C]**: bar interval `configs/default.yaml:11`
`ohlcv_interval_minutes: 60`; bound `configs/default.yaml:153`
`signal_max_age_hours: 12`.

### 5.1 Google News (via `gnews`) — **no numeric limit is documented anywhere**

I searched the `gnews` project's own README (the authoritative statement from
the library that actually does the fetching),
https://github.com/ranahaani/GNews — the only quantitative statement is
**qualitative**:

> "Google News rate-limits aggressively. If you are pulling more than a few
> hundred articles, or calling `get_full_article()` across many domains, you will
> eventually see HTTP 429s and blocked requests from a single IP."

**There is no requests/hour, no burst figure, no documented window. The limit is
UNDOCUMENTED.** I will not invent one. The only quantitative anchor offered is
the maintainer's *paid* remedy (residential proxy from $0.55/GB, or SearchApi).

What *is* documented and relevant:
- `max_retries` (default `3`) — "Retry attempts on HTTP 429 from Google News".
  Set `0` to disable.
- `retry_backoff_base` (default `1.0`), `retry_backoff_max` (default `60.0`) —
  `min(retry_backoff_max, retry_backoff_base * 2 ** attempt)` plus jitter.
- 429 handling exists in the library: `gnews/gnews.py:422-432`
  (`if feed_data.status == 429: … self._sleep(delay)`), and
  `ticker_news_signals/client.py:32-40` passes those three knobs through, plus
  its own connection-level retry loop (`client.py:8-14`).

**Our actual request volume is tiny [M]:** `collect_articles` makes exactly
**two** RSS calls per pull — `client.search(base)` and
`client.search_by_topic("DIGITAL CURRENCIES")` (`pipeline.py:59-60`), and
`search`/`search_by_topic` each wrap one `get_news`/`get_news_by_topic`
(`client.py:100, 115`). **Hourly = 2 requests/hour/ticker.** That is three orders
of magnitude below "a few hundred articles". **The 429 risk at this cadence is
negligible and needs no paid proxy.** Do not buy SearchApi for this.

### 5.2 StockTwits — **registrations closed, no limits published, no headers**

Primary source, https://api.stocktwits.com/developers **[M, fetched]** — the
entire developer page reads:

> "In an effort to continually improve our offerings and value to the community,
> we are currently reviewing all of our APIs, documentation and terms. We
> unfortunately won't be accepting new registrations until we have finished our
> review and made the necessary improvements and upgrades.
> For any questions please email developers@stocktwits.com."

Consequences, all **[C]** from that page plus **[M]** measurement:

1. **No published rate limit, no published terms, no API reference at all.**
   The limit is **UNDOCUMENTED**.
2. **No rate-limit headers are returned.** `curl -I` on
   `/api/2/streams/symbol/ETH.X.json` returns only `x-content-type-options` and
   `x-frame-options` — no `X-RateLimit-*`, no `Retry-After`. A client cannot
   self-regulate from headers; it can only react to a 429.
3. **The `.env.example:8-10` advice about a registered app token is stale.**
   New registrations are closed, so the "buys headroom" path is not available to
   anyone starting today. This is the second wrong long-standing comment in this
   slice.
4. **The keyless path is served, and currently works [M]** — HTTP 200 with a
   browser-grade User-Agent.

**Our volume [M]:** one `pull` = **1 StockTwits request + 1 Fear & Greed
request**. The verbose log for a real run reported
`{"event": "fetched stocktwits messages", "symbol": "ETH.X", "pages": 1, "messages": 30}`
— one cursor page (`_PAGE_SIZE = 30`, `client.py:45`; `_MAX_PAGES = 50`,
`client.py:44`), plus `fetch_fear_greed`. **2 requests/hour/ticker.**

So the ToS posture is: *keyless, undocumented, no enforcement signal, tiny
volume.* Acceptable for an hourly personal pull; **not** something to build a
production dependency on without noting that it could change without notice.
The client already degrades correctly — a persistent 403 Cloudflare challenge
is "non-fatal: returns whatever was collected (usually nothing)"
(`client.py:196-199`), and a 429 raises `RateLimitError` immediately rather than
retrying harder (`client.py:134-139`).

**A coverage caveat worth recording [M]/[C]:** the walk stops when the oldest
message on a page falls outside the window (`_HOURS_TO_STOP_AFTER_OLD_PAGE`,
`client.py:50, 250-252`). My run fetched **1 page (30 messages)** and that was
enough to cover 24 h for `ETH.X`. On a thin symbol, 30 messages may span more
than 24 h and **the window is then under-covered** — silently, because missing
hours simply produce no bucket. `INTEGRATION.md:35-36` states the same about
news: "No records are emitted for empty hours, so a file is a *sparse* hourly
series."

### 5.3 Is "hourly is right" still `[I]`? Yes — and I can now say why it is *sufficient*

`AUDIT.md` §4 labels hourly an inference from the *absence* of a documented
limit. That remains an inference; I did not find a number to replace it with
(§5.1, §5.2). But two measured facts make hourly *sufficient* rather than merely
unjustified:

- **Requests: 2/hour/ticker/channel.** 4 requests/hour for both channels
  together. Nothing close to any plausible limit.
- **The bar grid is 60 min**, so an hourly poll produces at most one record per
  bar — the seam's de-dup (`data.py:776-781`) then makes any duplicate free.
  Hourly is the *coarsest* cadence that still fills every bar. Faster gains
  nothing; slower leaves bars unobserved.

**One cadence consequence that is NOT neutral: `novelty_flag`.**
`NOVELTY_WINDOW = timedelta(minutes=15)` (`pipeline.py:39`) and
`_is_novel` (`pipeline.py:91-97`) mark an hour novel only if some headline is
younger than 15 minutes **at pull time**. An hourly pull fires at a fixed
minute (`:17` for funding; `:23`/`:29` proposed), so whether an article falls
inside the 15-minute window is a **lottery won once per hour**. Measured: see
§6 — `novelty_flag` was **0 nonzero on 24/24 bars**. This is a *design*
mismatch between a 15-minute novelty window and an hourly sampler, independent
of rate limits.

### 5.4 alternative.me Fear & Greed **[C]**

`fetch_fear_greed(limit=0)` "returns the full 2018+ daily history in one keyless
call" (`client.py:266-268`) — i.e. **every hourly pull re-downloads the entire
daily history** to obtain today's value. Wasteful but harmless (1 request), and
worth a one-line `limit=` optimisation if the payload ever matters.
`fng_index` is a **daily** value stamped onto each hour of that day
(`models.py:138-139`), so it is **constant within a UTC day** — measured
`distinct=2` over 24 h in §6. It is a real feature at multi-day horizons and a
near-constant at intraday ones. Same class of artefact as the funding file's
2-distinct-value problem (§2.4 of AUDIT.md).

---

## 6. Width consequence — AUDIT.md §7 Q2 (question 6)

### 6.1 Measured widths, with the **real** producer files **[M]**

Frame: 24 hourly bars for 2026-10-03 UTC, `feature_windows [1,4,24]`, all five
groups, `add_derived_ohlcv_features` applied (the real read seam — omitting it
is the documented F-15 trap, `AUDIT.md` §0). Signal files are the **actual live
output** of the two producers (news: 2 hours; social: 9 hours) plus the shipped
1-line funding file.

```
A no signal file at all (both keys null)                        52
B + news ONLY  (real sparse 2-hour file)                        57   (+5)
C + social     (real 9-hour file)                               60   (+3)
D + funding    (shipped 1-line HEAD file)  = ALL THREE          66   (+6)
```

**This resolves a presentational inconsistency in AUDIT.md.** §2.4's headline
"activating all three channels takes the observation **60 → 61**" compares two
*different funding shapes* — the 61 row uses **funding-dense** (55, no bid/ask),
while the 60 row is the **shipped 1-line file** (60, with bid/ask → `spread`).
§4 G-C then says "+6 columns at +6 width". **My measurement confirms the latter:
shipped-default 60 + 6 = 66.** Both AUDIT.md numbers are internally consistent;
they just answer different questions. The honest statement is:

> **52 → 66 is the full range across "no channels" → "all three, as actually
> produced". The shipped default is 60. Activation is +6.**

### 6.2 But **2 of the 6 new columns are degenerate on day one** **[M]**

Same run, per-column on the all-three frame (24 bars):

```
sentiment_score      nonzero= 13/24  distinct=  2  std=0.0810292
article_count        nonzero= 14/24  distinct=  3  std=1.99955
novelty_flag         nonzero=  0/24  distinct=  1  std=0        ← DEGENERATE
stt_mention_count    nonzero= 21/24  distinct=  7  std=1.42188
stt_tilt             nonzero=  0/24  distinct=  1  std=0        ← DEGENERATE
fng_index            nonzero= 21/24  distinct=  2  std=22.6347
```

For reference, every funding column was 0/24 on this frame (the 1-line file's
record is 2026-10-02T00:00Z, outside this window) — consistent with the
`signal_age_hours = -1.0` I measured in the merge probe.

**Why each is degenerate:**

- **`stt_tilt` — structurally dead, permanently.** `SocialRecord.stt_tilt` is
  `(bull - bear) / (bull + bear)` over messages carrying a `Bullish`/`Bearish`
  tag (`models.py:135-137`). `StockTwitsMessage.from_api` reads
  `raw["sentiment"]["basic"]` (`models.py:47-49`). I fetched the live v2 stream:
  ```
  $ curl … 'https://api.stocktwits.com/api/2/streams/symbol/ETH.X.json?limit=30'
  n= 30
  messages carrying a sentiment key: 0
  union of message keys: [body, conversation, created_at, discussion, entities,
                          id, likes, links, mentioned_users, prices,
                          reshare_message, reshares, source, symbols,
                          tokenized_body, user]
  entities sample: { "media": [], "sentiment": null, "discussable": null }
  ```
  **StockTwits no longer returns entity sentiment on the public v2 stream** —
  `entities.sentiment` is `null` on every message. So `sentiment=""` always,
  `bull = bear = 0`, and `stt_tilt = 0.0` always. Reproduced on a second symbol
  (`BTC.X`, 30 messages, 0 with a `sentiment` key). **This is not a cadence
  problem and no schedule fixes it.** `stt_tilt` will be a constant column
  forever unless the API changes back or an app token restores it — and
  registrations are closed (§5.2). **AUDIT.md lists `stt_tilt` among the 6
  recoverable columns; it is not recoverable.**
- **`novelty_flag` — dead at an hourly cadence.** §5.3. It *is* reachable at a
  faster cadence, but 4 requests/hour is already free, so this is fixable by
  choice — unlike `stt_tilt`.

**Is a constant column harmful?** **No, and this is worth stating precisely
because it looks alarming.** `NormalizationStats.normalize` guards it
(`features.py:455-457`):
```python
safe_std = std if std > 1e-12 else 1.0
out[name] = (frame[name] - mean) / safe_std
```
A `std=0` column normalises to exactly `0.0`, and the `inf`/`NaN` check
(`_require_finite`, `features.py:459-463`) passes. So adding degenerate columns
costs two useless input dimensions — PPO's first layer grows by 2, and a constant
input is an available free bias term — and buys **no crash, no `inf`, no NaN**.

### 6.3 A data-quality caveat on `article_count` **[M]/[C]**

The news pipeline merges a **ticker-specific search with a whole-category topic
pull** (`pipeline.py:59-60`):
```python
raw.extend(client.search(base, max_results=max_results))          # e.g. "ETH"
raw.extend(client.search_by_topic("DIGITAL CURRENCIES", max_results=max_results))
```
and then buckets **all of it** into `base`'s ticker (`pipeline.py:135` passes
`base`, not the full ticker). So `article_count` for `ETH_USD` counts **all
crypto headlines**, not ETH headlines. Measured on my real run: hour 08 had
`article_count = 24`, hour 09 `11`, hour 10 `4` — for `ETH` specifically, a
symbol-search would not plausibly return 24 headlines in one hour. The same
generic-crypto contamination applies to `sentiment_score`.

Consequence: **these two columns are market-wide, not ticker-specific**, and two
tickers' news files would be near-duplicates of each other differing only by the
symbol-search component. Worth knowing before anyone reads a per-ticker news
effect out of a model trained on them. Not a blocker; a labelling caveat.

### 6.4 Answer to §7 Q2: widen **now** — the cost is currently zero and the deferral is not free

**Recommendation: activate now, not later.** Reasons:

1. **Nothing is invalidated today.** `models/` is empty and tracks only
   `.gitkeep` **[M]**. `AUDIT.md` §1.4 and §4 agree. The "+6 invalidates every
   artifact" cost is *currently 0*. It stops being 0 the moment anyone trains —
   so **deferring is what creates the cost, not activating.** After a training
   run lands, the same change becomes a real migration.
2. **`check_feature_width` is non-self-referential by design**
   (`AUDIT.md` §2.3; `features.py:~472-530` compares artifact `feature_names`
   against live `compute()` names) — it **fails loudly and correctly**, by name,
   rather than silently mis-sizing an observation. The migration cost is
   "retrain", which is the intended, already-exercised path. That is a much
   better failure mode than the alternatives.
3. **Waiting for a longer episode does not make the widening more informative.**
   The channels' *content* over a longer window gets better; the *decision* is
   whether to have the dimensions at all. And per the `model-matrix` skill's own
   discipline, a wider observation on a short episode is not a measurement
   problem — it is simply not yet a measurable difference. Activating now means
   the log starts accumulating **from today**, which is exactly the input the
   later decision needs. Every day of delay is a day of signal history that
   cannot be recovered, because these files are forward-only (§4).
4. **The producer fixes are on the critical path anyway** (§1.3, §1.5). The
   append work and the `gnews.nix` fix must happen regardless of when the config
   keys flip. Bundling the config flip into the same pass costs nothing extra.

**Counter-argument I considered and rejected:** "wait until G-A/G4 make the
episode long enough to see them" (`AUDIT.md` §7 Q2's phrasing). Beyond the
point-3 answer above, note that G-A (`since`/`until` unreachable) and G4 (store
depth) are **independent** of this change — nothing about the store makes
`sentiment_score` more or less informative. Coupling them would import two
unresolved gaps' uncertainty into a change that has none.

**One honest qualification:** of the 6 columns, **4 will carry signal
(`sentiment_score`, `article_count`, `stt_mention_count`, `fng_index`) and 2 will
not (`stt_tilt` permanently, `novelty_flag` at hourly cadence)**. So the honest
headline is **+6 width for +4 usable dimensions**. If the lead wants the width to
*mean* something, the cheapest honest move is to record the two degenerate ones
now (so the schema is stable, per `AUDIT.md` §4's "activation must not change the
schema later") and revisit `novelty_flag`'s cadence separately. **Do not** drop
`stt_tilt` from the schema to "save" width: dropping it later is the exact
schema change `AUDIT.md` warns against, and it costs the same width migration
either way.

---

## 7. What I could not verify

- **[unverified]** Whether `format = "pyproject"` alone fixes the `gnews` build
  (vs. needing the wheel). I tested `nix run` as-shipped (fails) and did not test
  candidate fixes — that is a change to the sibling repo, out of this pass's scope.
- **[unverified]** `_xdg_state_dir()` fallback behaviour under a systemd user unit
  with `$XDG_STATE_HOME` unset. The two CLIs both write a JSON log unless
  `--no-log-file`.
- **[unverified]** The exact local-timezone parsing rule for `OnCalendar=*-*-* *:17:00`
  (should read `systemd.time(7)`). Immaterial at hourly cadence; argued in §2.3.
- **[unverified]** Whether the `PYTHONPATH`-sibling pattern actually resolves both
  CLIs' dependency closures under the **main** repo's dev shell. §3.3 flags this
  as the check the builder must do before shipping that path.
- **[unverified]** Whether a 30-message StockTwits page under-covers the 24 h
  window on thin symbols. I observed 1 page sufficing for `ETH.X`; I did not test
  a thin symbol.
- **Not measured:** no `nix flake check`, no train, no backtest (lead/reviewer own
  those, per the brief). No live train/backtest numbers are quoted anywhere above.

## 8. Three wrong long-standing comments found (this pass's stated failure mode)

| # | Claim | Where | Reality |
|---|---|---|---|
| 1 | "the sibling's documented hourly cron **re-appends** the current hour on every pull" — and `INTEGRATION.md:114-117` gives a cron recipe using `--output f >> f` | `data.py:648-652`; `ticker-news-signals/INTEGRATION.md:114-118` | **The producers truncate.** `export.py:123` / `:149` both `open("w")`. The seam's dedup is real but is defending against a producer behaviour that does not exist. |
| 2 | "a registered app token buys headroom" | `kraken-social-signals/.env.example:8-10` | **Registrations are closed** (api.stocktwits.com/developers, §5.2). Not obtainable. |
| 3 | `python ~/Projects/ticker-news-signals/cli.py pull …` is the way to produce the file | `configs/default.yaml:53-54`; `data.py:157-161` | **Cannot run in this dev shell** (`gnews` absent, §1.1) **and the sibling's own flake does not build** (§1.5). |

Bonus, same class: `INTEGRATION.md:56-80` describes the consumer as a **stub**
whose merge logic is a "`TODO(next pass)`". That is **stale** — the merge is
fully implemented (`data.py:612-850`) and I verified all three real files merge
verbatim through it today (§1.3 of this doc's evidence, the merge probe):
```
[OK]   news     added=['sentiment_score','article_count','novelty_flag','signal_observed','signal_age_hours']
[OK]   social   added=['stt_mention_count','stt_tilt','fng_index','signal_observed','signal_age_hours']
[OK]   funding  added=['funding_rate','basis','open_interest','funding_rate_prediction','vol24h','bid','ask', …]
```
And the `INTEGRATION.md:116-117` cron recipe, run verbatim, produces a **corrupt
JSONL file** — measured:
```
1 {"article_count":15,…,"timestamp":"2026-10-03T09:00:00Z"}
2 {"article_count":5,…,"timestamp":"2026-10-03T10:00:00Z"}
3 signals -> /tmp/opencode/crontest.jsonl        ← not JSON
```
because `--output f` truncates while `>> f` re-appends the CLI's own
`signals -> …` stdout line. **Severity: low, not fatal** — `data.py:722-727`
catches `json.JSONDecodeError` per line and warns:
`WARNING Skipping malformed JSONL line 3: Expecting value: line 1 column 1 (char 0)`
and the merge still succeeds. So it is a documentation defect worth fixing, not a
data-loss bug.

---

## 9. Concrete hand-off for the builder

Ordered by dependency. **None of this is done; all of it is read-only findings.**

**In `ticker-news-signals` (its own repo — the `NEW-DATA-SOURCE` tail):**
1. Fix `nix/gnews.nix:19` so `nix run ~/Projects/ticker-news-signals#ticker-news-signals` builds (fetch the wheel). **Gate: `nix run … -- version` prints `ticker-news-signals 0.1.0`.**
2. Add append semantics (`--append`, or make `--output` append) to `export.py:120-126` and a matching CLI flag. **Gate: two `pull --lookback-hours 1` runs leave 2 distinct hours, not 1.**

**In `kraken-social-signals` (its own repo):**
3. Same append change to `export.py:145-152`. **Note:** its default
   `--lookback-hours 24` makes a naive hourly schedule a *sliding window*, not a
   log; pick the lookback deliberately.
4. `stt_tilt` cannot be fixed from here — StockTwits removed the field (§6.2).
   Decide explicitly: record it as a permanent constant, or drop it from
   `_SIGNAL_COLUMNS` **now** (before any artifact exists) rather than later.

**In `kraken-trading-bot`:**
5. Two `just` recipes modelled on `justfile:163-177`, plus two `.service.in` +
   `.timer` pairs. Extend the placeholder set for `--ticker`; give the timers
   distinct minutes (`*-*-* *:23:00`, `*-*-* *:29:00` — `:17` is taken by
   funding, `systemd/kraken-trading-bot-funding.timer:19`). Keep `Persistent=true`
   + `RandomizedDelaySec`.
6. `ExecStart` for social: `nix run /home/seanc/…/kraken-social-signals#kraken-social-signals -- pull --ticker @PAIR@` (verified working). For news: **blocked on step 1**; do not ship a timer that fails on first fire.
7. Flip `configs/default.yaml:57` and `:100` from `null`, with the merge-order
   comment (`data.py:151-176`) reflected. Width goes 60 → **66** (§6.1).
8. Prefer the `justfile:290` `PYTHONPATH`-by-path pattern over new flake inputs
   (§3.3) — with the §7 verification caveat.

**Do not:** commit anything under `signals/` (§4.2); add `systemd.user.*` to
`nix/module.nix` (it cannot evaluate there, §2.1); use `OnUnitActiveSec`
(§2.3); buy SearchApi or a proxy (§5.1).

---

*Written by Researcher 1, pass 2026-10-03, read-only. Every number carries its
frame or is labelled `[I]`/`[unverified]`. Widths dated 2026-10-03. No source file
was modified in any repo; nothing was committed.*
