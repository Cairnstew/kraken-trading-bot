---
description: Audit the bot's data pipeline, find gaps, and scaffold a standalone data-extraction project beside this repo
---

You are the **lead** of a data-pipeline audit team working on the `kraken-trading-bot` repository.
You orchestrate a team of parallel agents via the opencode-ensemble plugin; you do not do the
audit work yourself. Your job: audit the bot's current data pipeline, identify gaps or ways to
improve its data infrastructure, then design and scaffold a new standalone Python project (its own
git repo, in a directory parallel to this checkout) that fills the best gap and can feed the bot's
pipeline.

Optional focus from the user (may be empty; if empty, audit everything): $ARGUMENTS

Load and follow the `opencode-ensemble` skill for the lead workflow before starting.

**Non-negotiable rules (apply to every teammate, every phase):**
- Each phase must write its handoff artifact before the next phase starts. Keep artifacts in
  `./.data-audit/` (create it). The artifact dir IS tracked per repo convention — commit the
  final pass artifacts (AUDIT/RESEARCH/DECISION/VALIDATION/PLAN) as one docs commit at run end,
  as prior passes do (`docs: data-pipeline pass <date> artifacts`). Never commit scratch
  models/store from `/tmp/...`.
- Do not skip ahead. Do not write code before Phase 4.
- The audit is category-agnostic. Do not assume news, or any other specific data source, is the
  right gap; the audit has to find that out by reading the code.
- Never commit real API keys; follow the `.env.example` convention.
- Respect each source's rate limits and terms of use; prefer keyless sources over paid ones unless
  DECISION.md justifies the cost.

---

## ENSEMBLE GATING

Detect whether the team tools are actually present in this session (config can list the plugin
but it may not have loaded):

- If tools named `team_create`, `team_spawn`, `team_tasks_add`, `team_status`, `team_results`,
  `team_merge`, `team_shutdown`, `team_cleanup` are available to you right now → **`ENSEMBLE_ACTIVE=true`**.
- Otherwise → **`ENSEMBLE_ACTIVE=false`**, regardless of config.

- **If `ENSEMBLE_ACTIVE=true`** — run the team orchestration in this command exactly. **Always
  spawn and manage a team** — a team is the default execution path for this command, not an
  optimization.
- **If `ENSEMBLE_ACTIVE=false`** — run a single-agent fallback: you play every role (auditor,
  researcher, architect, builder, integrator, reviewer) in order, applying the **same** decision
  protocol. The team decision rules still replace any human confirmation gates; the "confirm with
  the human" steps in this file are never taken.

---

## Context: house style to mimic

The foundation package is `kraken-python` (github.com/Cairnstew/kraken-python). Any new project
should mirror it:

- `pyproject.toml` plus a Nix flake (`flake.nix`/`flake.lock`) with a dev shell; a NixOS module
  (`services.<name>`, agenix/sops-compatible `credentials.*File` options) only if the source needs
  a long-running poller.
- Small single-purpose modules (`auth.py`, `transport.py`, `client.py`, `manager.py`, `models.py`,
  `utils.py`, `errors.py`, `logging_config.py`) fronted by one high-level "manager" facade.
- An `export.py` extraction registry: `extract(mgr, "resource", **kwargs)`, `extract_many(...)`,
  `extract_snapshot(...)`, returning only JSON-safe primitives, plus `write_json` / `write_jsonl`
  helpers.
- Typed dataclasses with `to_dict()` / `from_*()`; raw string/Decimal values, never
  silently-lossy floats.
- Thin `cli.py` over the package; offline no-network unit tests in `tests/` plus a separate
  live-verification script; `.env.example` for config; structured JSON logging with secret redaction.
- README order: Features, Setup, Quickstart, CLI, Project layout, Extending, Tests.

---

## Current data-source projects (registry)

The data-source projects relevant to this bot, their GitHub URLs, and local source paths.
**Keep this list current.** When a new project is created (Phase 4), add an entry with the
real URL after the repo is pushed; when an existing project is edited or pushed for the first
time, update its row.

| Project | GitHub URL | Local source |
|---------|-----------|--------------|
| `kraken-trading-bot` (this repo) | https://github.com/Cairnstew/kraken-trading-bot | `/home/seanc/Projects/kraken-trading-bot` |
| `kraken-python` (foundation wrapper) | https://github.com/Cairnstew/kraken-python | `/home/seanc/Projects/kraken-python` |
| `ticker-news-signals` (news → sentiment) | https://github.com/Cairnstew/ticker-news-signals (private) | `/home/seanc/Projects/ticker-news-signals` |
| `kraken-market-data` (OHLC store) | https://github.com/Cairnstew/kraken-market-data (private) | `/home/seanc/Projects/kraken-market-data` |
| `kraken-funding-rates` (funding/basis) | not pushed yet — no remote set | `/home/seanc/Projects/kraken-funding-rates` |
| `kraken-social-signals` (StockTwits + Fear & Greed) | https://github.com/Cairnstew/kraken-social-signals (private) | `/home/seanc/Projects/kraken-social-signals` |
| `kraken-deep-history` (deep OHLCV via Binance archive → store seeder) | https://github.com/Cairnstew/kraken-deep-history (private) | `/home/seanc/Projects/kraken-deep-history` |

Related siblings (not data sources): `opencode-ensemble`, `spotify-playlist-manager`,
`x-python-api`, `steam-mcp`, `nixos-minecraft-modpacks` (all under `https://github.com/Cairnstew/…`,
check `gh repo list Cairnstew --limit 100` for the full inventory).

---

# TEAM ORCHESTRATION

## Team shape

| Role | Agent | Worktree | Ownership |
|------|-------|----------|-----------|
| Auditor | `general` | `false` | Read-only code audit, pipeline mapping, gap enumeration → `AUDIT.md` |
| Researcher ×2 | `general` | `false` | Library/API research for top gaps → `RESEARCH.md` |
| Architect | `general` | `false` | Decision: pick one (gap, library) pair → `DECISION.md` |
| Builder | `build` | `true` | Scaffold the project (new sibling repo) or implement the improvement per DECISION.md |
| Integrator | `build` | `true` | Wire minimum integration seam in this repo |
| Reviewer | `qa` | `false` | Validate tests, flake check, write `PLAN.md` |

Spawning: read-only roles (auditor, researchers, architect, reviewer) must be spawned with
`worktree: false` explicitly — otherwise the plugin may give them a worktree and their handoff
artifact (e.g. `PLAN.md`) lands in that worktree instead of the main repo's `./.data-audit/`,
and `team_cleanup` later asks to acknowledge their uncommitted changes. If that happens anyway,
copy the artifact into the main repo before cleanup. After Phase 6, the reviewer is followed by
zero or more builder slices in Phase 7 (fix loop / further development); each is a normal
`build` + worktree + merge cycle.

## Task board

Create the team (`team_create` — name it `audit-pipeline`), then record the tasks up front with
`team_tasks_add`. `team_tasks_add` returns per-task IDs like `task_XXXX_0001_yyyy`. **Capture
those returned IDs and pass them to `depends_on` — never label names, and never type an ID from
memory.** If any `depends_on` references a label or a mistyped ID, every downstream task shows as
`blocked`: teammates still RUN (spawn works unclaimed, work proceeds, they just can't `claim_task`),
but there is **no board-edit API** — `team_tasks_complete` refuses to complete a blocked task, so
the board stays wrong for the whole run. Accepted fallback (verified 2026-10-01): ignore the board
once it is broken, track the chain yourself by the spawn order, complete whatever completes, and
don't block teammates on claims. The only real cure is to write the `depends_on` list from the
exact response of the *same* `team_tasks_add` call — double-check each ID before sending.

| Task key (label) | Description | depends_on (real IDs from `team_tasks_add`) |
|------------------|-------------|------------------------|
| `audit` | Auditor: read code, map pipeline, enumerate gaps → `AUDIT.md` | — |
| `research-1` | Researcher: library survey for top gap #1 | `audit` |
| `research-2` | Researcher: library survey for top gap #2 | `audit` |
| `research-3` | Researcher: library survey for top gap #3 | `audit` |
| `decision` | Architect: pick one (gap, library) pair → `DECISION.md` | all `research-*` |
| `scaffold` | Builder: create new project (4A) or implement improvement (4B) per DECISION.md | `decision` |
| `integrate` | Integrator: wire minimum seam in this repo | `scaffold` |
| `verify` | Reviewer: run tests, flake check, write `PLAN.md` | `integrate` |

**CHECKPOINT:** After the `decision` task completes, stop and show the user DECISION.md. Wait for
an explicit go before spawning the `scaffold` task. A new repo and new dependencies are a bigger
commitment than a code edit.

## Waiting on teammates (important — read before spawning anyone)

Teammates message the lead **automatically** via `team_message` when they finish (the plugin
delivers their result and any task-completed status). Do **not** poll.

- **Do NOT** loop on `team_status`, `team_results`, or the filesystem to check progress.
- **Do NOT** `sleep` between checks. There is nothing to sleep for — the notification arrives
  when it arrives; idle time is just idle time.
- When you do need results, call `team_results` **once and only once** (messages accumulate and
  a single call returns everything unread). If it returns nothing, the teammate is still
  working — end your turn and wait for the next `[System: New team message …]` notification.
  Do not re-call it in the same turn.
- `team_status` is for exception handling (who is still active), not progress torture. Check it
  at most once after a teammate has been "working" for an abnormally long time (≥5+ minutes
  with no message) and only to decide whether to send a single status-check message.
- If a teammate is quiet for a long time, send **one** concise `team_message` nudge naming the
  blocker you suspect and the acceptable fallback (e.g. "don't run nix flake check, lead
  already validated; just write PLAN.md"). Do not send repeated nudges.

## Spawn sequence

1. `team_create`, `team_tasks_add` (board above — capture the returned IDs), then **spawn the
   auditor** (`general`, `worktree: false`, `claim_task: audit`). Give it the full Phase 1 spec
   below. It is read-only and ends its report with `AUDIT COMPLETE`.

2. Wait for the auditor's `team_message` (do not poll; see "Waiting on teammates" above). Read
   AUDIT.md. Then **spawn the three researchers in parallel** (`general`, `worktree: false`,
   `claim_task: research-1/2/3`). Each gets: the relevant gap from AUDIT.md, the Phase 2 spec,
   and the house style context.

3. Wait for all three researchers' messages. Read RESEARCH.md. Spawn the architect (`general`,
   `worktree: false`, `claim_task: decision`). Give it: AUDIT.md, RESEARCH.md, the Phase 3 spec.

4. **CHECKPOINT:** Read DECISION.md. Show it to the user. Wait for explicit go.

5. After user go: spawn the builder (`build`, own worktree, `claim_task: scaffold`). Give it:
   DECISION.md, the house style context, and the Phase 4 spec (4A for `NEW-DATA-SOURCE`, 4B for
   `IMPROVE-EXISTING`). The builder creates the sibling repo **or implements the improvement in
   the existing target(s)**, commits everything, and **creates + pushes the GitHub remote when
   `gh` is authenticated** (see Phase 4 step 8 / 4B) or pushes the touched existing repo.

6. When the builder reports done: `team_shutdown`, `team_merge`. Inspect the merged diff. **Then
   COMMIT the merged builder land in the main repo before any further worktree teammate is
   spawned** (`team_merge` leaves the builder's work as *unstaged changes* in the lead's working
   tree, and a downstream worktree branches from the last **commit** — the integrator will start
   one commit behind and see a pre-4B tree, exactly what blocked the 2026-10-01 integrator).
   Landing it as its own commit keeps every later worktree base current. Then
   spawn the integrator (`build`, own worktree, `claim_task: integrate`). Give it: DECISION.md,
   the Phase 5 spec. The integrator adds the minimum adapter stub in this repo (for a
   `NEW-DATA-SOURCE`); for an `IMPROVE-EXISTING` outcome whose work already lands in this repo,
   the integrator's job is to confirm the seam is wired through train/backtest/paper and close
   any call-site gaps found.

7. When the integrator reports done: merge. Then spawn the reviewer (`qa`, `worktree: false`,
   `claim_task: verify`). Give it: the full diff scope, the Phase 6 spec. The reviewer runs
   tests, flake check, **the end-to-end integration test of the bot against the new data
   source** (store-backed train/backtest vs baseline control — see Phase 6), writes
   `VALIDATION.md` + `PLAN.md`, and returns a gate verdict.

8. After the reviewer reports: read the gate verdict. If `NEEDS_FIX`, run Phase 7 fix loop
   (spawn a builder, merge, re-verify) until the node turns `PASS`. Then run final
   verification yourself (Phase 6 commands), push both repos if `gh` is ready, and
   `team_cleanup`. Finally run the **Phase 8 self-improvement checkpoint** (append RUN LOG,
   commit it separately) before writing the final summary.

Note for cleanup: teammates that wrote handoff artifacts into the main repo's `./.data-audit/`
directly need no merge; teammates with a worktree you did not merge (the reviewer in this run)
leave an uncommitted `./.data-audit/PLAN.md` behind — copy it into the main repo's
`./.data-audit/` before `team_cleanup` if it is not already there, then acknowledge uncommitted
changes if cleanup asks.

---

# PHASE 1 — AUDIT (auditor, read-only)

Do not propose solutions yet.

1. Read the actual code, not just the README: strategies, engine, RL train/backtest code,
   `configs/*.yaml`, the `models/` layout, and every place `kraken-python` is called (which
   resources, what cadence).
2. Map the pipeline end to end: what is fetched, how often, how it is stored or cached (if at
   all), and exactly where it enters the RL feature space or a strategy's `tick()` inputs.
3. Enumerate every plausible gap or improvement vector you can find evidence for. Do not
   pre-filter toward one category. Illustrative, not exhaustive:
   - Improve what already exists (the highest-directness items are often here): dead/ceremonial
     code paths, effective-but-unapplied machinery, missing schedulers/timers, duplicated
     constants, config keys that exist but nothing reads, unthrottled/unretried fetches, thin
     historical depth vs. what models need
   - Text/news signal (headlines, articles, press releases for a ticker or sector)
   - Research/academic signal (papers, citation trends)
   - On-chain/crypto-native data (exchange flows, whale activity, network/gas metrics, stablecoin
     supply)
   - Macro/economic calendars (rate decisions, CPI, scheduled events)
   - Social/sentiment data (forums, social media, search trends)
   - Market microstructure (deeper order-book history, cross-exchange comparison, funding rates)
   - Data quality/reliability of what is already pulled (no cache, no backfill/replay, weak
     rate-limit handling, thin historical depth vs. what models need)
4. For each candidate gap record: the evidence in the code, how directly it could feed the RL
   features or `tick()`, and how hard it is to source (keyless API vs. paid vs. scraping).
5. Output the pipeline map plus a ranked list of at least 3 candidate gaps spanning more than one
   category. Do not commit to one yet.

If the ranked list covers only one category, redo the audit before continuing.

Write `.data-audit/AUDIT.md` and end with `AUDIT COMPLETE`.

---

# PHASE 2 — RESEARCH (researchers, read-only)

Do not write code yet.

Each researcher takes one of the top 2-3 gaps from AUDIT.md and searches out concrete
libraries/APIs/packages that could source it — or, for an `IMPROVE-EXISTING` gap (a data-quality,
reliability, or operational-improvement candidate), the concrete approaches/libraries/patterns
that could implement the improvement (e.g. a scheduler pattern for the pullers, a
normalization/z-scoring reference, a config-driven seam). Search broadly; do not limit yourself
to any fixed list.

As a reference example of the rigor expected, a prior survey of text/news sourcing covered
`newspaper4k`, `trafilatura`, `feedparser`, `newsapi-python`, `GNews`, and for academic sources
`arxiv`, `pyalex`, `semanticscholar`, `habanero`/`crossrefapi`, and `Bio.Entrez`. Apply the same
rigor to whatever categories the audit surfaced.

For each serious candidate record: maintenance status and last release, license, auth (keyless /
API key / paid), rate limits, and output shape (structured JSON vs. raw HTML/text needing parsing).

Score each option partly on how cheaply it reduces to what the RL pipeline can consume: a
per-ticker, per-timestamp scalar or small vector beats a pile of raw unstructured output.

Each researcher writes its findings to a per-researcher file — `.data-audit/RESEARCH-1.md`,
`.data-audit/RESEARCH-2.md`, `.data-audit/RESEARCH-3.md` — to avoid write races between three
parallel writers, and the lead assembles `.data-audit/RESEARCH.md` from them once all three
report. End with `RESEARCH COMPLETE`.

---

# PHASE 3 — DECISION (architect)

Do not scaffold yet.

1. **Choose the pass outcome type first.** Both are legitimate; let the AUDIT.md evidence decide,
   never default to "new project" just because that is the familiar shape:
   - **`NEW-DATA-SOURCE`** — a new keyless/paid source shipped as a new sibling project (the
     historical default).
   - **`IMPROVE-EXISTING`** — improving code/infra that already exists: this repo's own pipeline
     (actually applying the normalization stack, a wired scheduler, a proven seam refactor such
     as reading `_SIGNAL_COLUMNS` from config, fixing a duplicated tuple) **or** an existing
     sibling project (wider/denser store, backfill, cache, ops hardening, flake-input wiring).
     An improvement that AUDIT.md ranks highest-directness must be a *candidate outcome*, not a
     footnote, even though it creates no new repo.
   Then pick exactly one (gap, improvement-or-library) target for that outcome. One clean
   outcome beats three half-built ones.
2. Justify it against the RL pipeline's real shape from AUDIT.md: config inputs, the
   `train`/`backtest` knobs, and the `models/{TICKER_ID}/{model_name}/` artifact layout. State the
   exact landing point — a feature-engineering step, the store adapter, the scheduler, a config
   key — where the feature or fix is read per ticker.
3. For `NEW-DATA-SOURCE`: name the project (in the `kraken-*` family if Kraken/crypto-specific,
   otherwise a source-oriented name) and state why. Target location: a new git repo in a sibling
   directory, `../<new-project-name>/`, not nested inside this repo and not a submodule.
   For `IMPROVE-EXISTING`: name the touched repo(s) — this repo, a sibling, or both — and the
   in-scope files/units. There is no new repo and no naming step.
4. Write DECISION.md: outcome type, chosen gap, library(ies) if any, project name and path (or
   improvement scope), a one-paragraph integration sketch (see Phase 5), and the runner-up
   options and why they lost.

Write `.data-audit/DECISION.md` and end with `DECISION COMPLETE`.

---

# PHASE 4 — SCAFFOLD (builder)

Execute DECISION.md's outcome type. The house style below (pyproject/flake/package layout/
cli/tests/env/README) applies to **new** projects; for an `IMPROVE-EXISTING` outcome, skip the
repo-creation steps and apply the same discipline (typed dataclasses, export registry,
offline tests, `.env.example`, README updates) to the existing target.

## 4A. `NEW-DATA-SOURCE` — create the sibling repo per DECISION.md

1. `git init` the new directory.
2. `pyproject.toml` (same metadata shape as `kraken-python`) and a Nix flake dev shell.
3. Package layout adapted to the chosen source: `client.py` (thin wrapper around the chosen
   library/API), `models.py` (typed dataclasses with `to_dict()`), `export.py` (registry pattern
   as in `kraken_api.export`, everything JSON-safe, `write_json`/`write_jsonl`), `errors.py`,
   `logging_config.py`, `utils.py` (rate-limit/backoff and timestamp helpers as needed).
4. `cli.py` exposing at least a `pull`/`extract` command that writes JSON/JSONL to a file,
   matching `python cli.py extract ... --output out.json`.
5. `tests/` with offline unit tests using recorded or mocked responses (no live network), plus a
   separate `scripts/verify_live.py`-style manual check.
6. `.env.example` and `.gitignore` for any API keys, following the `KRAKEN_API_KEY` /
   `KRAKEN_API_SECRET` naming style. Never commit a real key.
7. `README.md` in the house section order.
8. **Push to GitHub if `gh` is authenticated and ready** — after committing locally, run
   `gh repo create <project-name> --source . --private --push` (or `--public` if the project is
   a public data-source mirror like `kraken-python`; default to private for personal/bot
   tooling). If `gh` is not authenticated (`gh auth status` fails) or the network is
   unavailable, **skip the push, note it prominently in the report, and leave the remote
   unset** — a later run can push with `gh repo create <name> --source . --push`. Never invent
   a remote URL; record the real one in the registry (see the project list above) after
   pushing.

## 4B. `IMPROVE-EXISTING` — implement the decision in the existing target(s)

1. No `git init`, no `gh repo create`. Work in the repo(s) DECISION.md names.
2. Implement exactly the in-scope units listed in DECISION.md — no adjacent rewrites.
3. Follow the same house style for any code added: typed dataclasses, `to_dict()`/`from_*()`,
   export registry, offline tests for new behaviour, `.env.example` updated if a config key is
   added, README/flake updated if the repo's surface changes.
4. Commit in the worktree. If the target repo has a remote, push when `gh` is ready; a fix that
   is pushed first time gets its registry row updated (see registry note above).

Commit everything in the worktree. Report done via `team_message` with the diff and commit hash
(and, for 4A, the new GitHub URL, or "not pushed — no gh/auth" if skipped; for 4B, the repo and
commit touched, or "not pushed — no gh/auth" if skipped).

---

# PHASE 5 — INTEGRATION SEAM (integrator)

Do not deep-modify the RL internals in this pass. Design the seam and wire only the minimum to
prove it.

1. Define the output contract: one JSON/JSONL record per `(ticker, timestamp)` with the chosen
   feature(s), written where this bot can pick it up (shared local path or piped output; state
   which and why).
2. In this repo add the smallest adapter that proves the seam, e.g. an optional extra-features
   file path in `configs/default.yaml` and a note or stub at the point in the RL feature-loading
   code where it would merge. A stub or TODO is fine; a feature-engineering rewrite is out of
   scope.
3. Write `INTEGRATION.md` in the new project (linked from its README): output schema, refresh
   cadence, how this bot consumes it today, and how a follow-up pass would wire it fully into
   `train`/`backtest`. For an `IMPROVE-EXISTING` outcome that lands here rather than in a new
   project, skip `INTEGRATION.md`; the improvement's wiring notes belong in the commit message
   and `PLAN.md`.

Commit in the worktree. Report done via `team_message` with the diff and commit hash.

---

# PHASE 6 — VALIDATE, INTEGRATION-TEST, AND REPORT (reviewer)

The reviewer must do **both** unit validation and a **real end-to-end
integration test of the bot against the pass's outcome** — the point of the
outcome is that the bot consumes it, so a positive result is an actual
train/backtest reading through it (a new source) **or** an actual
train/backtest proving the improvement changes what the bot sees
(`IMPROVE-EXISTING` — e.g. the normalization stack is now applied, the
scheduler keeps the signal fresh, the widened seam reaches the observation).
Everything below was live-verified for the `kraken-market-data` pass (period:
real Kraken data, 2026-09-28); adapt the commands to the chosen outcome.

1. **Sub-project validation**: new project's offline tests + `nix flake check`
   if applicable. Then this repo's `pytest` to confirm the adapter broke
   nothing (expected: pre-existing count + new adapter tests, all green).

2. **Seed the data source live** (needs network, keyless source): for the
   market-data store,
   `cd ~/Projects/kraken-market-data && nix develop --command bash -c
   "MARKET_DATA_DIR=/tmp/kmd-test/store python cli.py update --pair ETH/USD --interval 60"`
   then confirm with `... python cli.py stats` (bars, span, contiguous via
   `... python cli.py verify --pair ETH/USD --interval 60`).

3. **Train the bot through the source** (store-backed): write a scratch config
   (e.g. `/tmp/kmd-test/config.yaml`) with `market_data_store: /tmp/kmd-test/store`
   and small budget (`--pages 2 --timesteps 3000`, discrete for speed), then
   from this repo:
   `nix develop --command bash -c "kraken-trading-bot train --ticker ETH_USD
   --model ppo_store_smoke --config /tmp/kmd-test/config.yaml --pages 2
   --timesteps 3000 --models-root /tmp/kmd-test/models"`.
   Success looks like: `Upserted N bars into market-data store` then
   `Training PPO on N bars`.

4. **Backtest through the source**:
   `nix develop --command bash -c "kraken-trading-bot backtest --ticker ETH_USD
   --model ppo_store_smoke --pages 2 --models-root /tmp/kmd-test/models"`
   — record return/Sharpe/drawdown/trades.

5. **Baseline control (no source)**: repeat train + backtest with a config
   where `market_data_store: null` (fresh leaf config, same seed/timesteps) and
   record the same metrics.

6. **Positive-result gate** (this is what "results of the new data source are
   positive" means):
   - Store-backed train + backtest complete without error (no feature-width
     mismatch, no `NotEnoughDataError`) and the store **persists**: `stats`
     after the run shows bars ≥ seeded, `verify` still contiguous.
   - **Magnitude evidence is mandatory** (applies to every outcome type, and
     is the clause that catches silent defects): record **bars replayed** and
     **trades taken**, and assert the new columns are present **by name** in
     the observation (export CSV / trained `normalization.npz` `feature_names`
     / the train log's feature count). A run that reports no error but replays
     ~1 bar of 721 with 0 trades has silently failed — "completes without
     error" is NOT sufficient. See the 2026-10-01 entry for the mechanism.
   - Store-backed backtest is **equivalent** to the baseline within training
     stochasticity (same window → |store − baseline| return within ~5 pts /
     shares the same sign; do NOT gate on exact equality — PPO with a seed is
     still stochastic across runs; the feature *vectors* must be identical).
   - Optional but strong: show the source **unlocks depth** — for the store,
     run `update` again and confirm bars grow beyond the seed (~721), i.e. the
     accumulation that breaks the REST ~720-bar ceiling is observable.
   - If the store-backed run is *worse* than baseline beyond stochasticity, or
     errors occur, that is **not a pass**: record the failure in
     `.data-audit/VALIDATION.md` and proceed to Phase 7.

7. Write `.data-audit/VALIDATION.md`: the integration-test matrix above
   (store-backed vs baseline metrics), the persistence proof, the gate
   verdict (`PASS` / `NEEDS_FIX` + evidence). Write `.data-audit/PLAN.md`:
   what was audited, what was deferred (the other candidate gaps), what was
   built, and concrete next steps for fully wiring the signal into the RL
   pipeline.

8. Report to the user: new repo's path, how to run it (`nix develop`,
   `cli.py extract ...`), the integration-test results, and the deferred work.

Never commit scratch models/store from a smoke test (keep them in `/tmp/...`).

---

# PHASE 7 — FURTHER DEVELOPMENT (lead + builder, when the gate fails OR on request)

The command does not stop at "scaffolded" (or "improvement landed"). The user
wants the pass's outcome (new data source **or** improvement slice) to be
*positive for the bot*, and continued development of the sub-project(s) and
main repo alike.

1. **If Phase 6 returns `NEEDS_FIX`** (errors, regressions, feature-width
   mismatch, store not persisting): spawn a builder (`build`, own worktree,
   `claim_task: scaffold` or a new `dev-fix` task) with the VALIDATION.md
   evidence and a scoped fix brief. The fix may touch **either** repo: the
   sibling project (store extraction/CLI bugs) or `kraken-trading-bot` (the
   `read_ohlc_dataframe` adapter, config wiring, dev-shell imports). The
   builder commits in its worktree; merge, re-run the Phase 6 integration test,
   and repeat until the gate passes.
2. **Iterative development loop** (the normal happy path): after a PASS, and
   whenever the user asks for further development, spawn one developer at a
   time on a named slice (e.g. "surface `since`/`until` on the train CLI",
   "walk-forward via `TradingEnvironment.reset(options=...)`", "wire the NixOS
   module into the bot host", "add `kraken-market-data` flake input to the bot
   dev shell"). Each slice: spawn → merge → **re-run the relevant Phase 6
   checks** (bot suite + integration test for that slice) → update PLAN.md /
   VALIDATION.md → next slice. One slice per pass, tests green before moving
   on.
3. Both repos' changes are committed in their own worktrees and pushed when
   `gh` is ready (the sibling pushes to its own origin; this repo pushes to
   `github.com/Cairnstew/kraken-trading-bot`). The registry in this command
   stays the single source of truth for repo URLs.

---

# PHASE 8 — SELF-IMPROVEMENT (lead, required after every run)

The command improves itself. After `team_cleanup` and before the final summary,
run a short, cheap checkpoint that captures what this run taught us about
*running the command itself* (not the task's own findings). This keeps future
runs cheaper and less error-prone.

1. **Capture run-time lessons.** Notes about how THIS run exercised the
   guidance in this file (or the repo/tooling it touches): a step that misled,
   wasted effort, or was stale; a command in this file that no longer works or
   proved awkward; a tool/CLI quirk discovered the hard way; a convention that
   had to be rediscovered. Be concrete and evidence-grounded — never
   aspirational.

2. **Audit the guidance you relied on** against the run and the current repos:
   are the paths real? do the CLI commands work as written? is the project
   registry current (new repos pushed, URLs updated)? is any step now redundant?

3. **Act per grounded lesson** (append-only to the RUN LOG below, or fix the
   guidance directly):
   - If the lesson is a CHEAP correction to this file (wrong command, stale
     path, missing step), fix the relevant section **and** append a short RUN
     LOG entry citing it.
   - If the lesson is a tip/observation without a fix, append it to the RUN
     LOG only.
   - If the lesson belongs to a sibling repo (a bug in `kraken-market-data`,
     `ticker-news-signals`, etc.) or an upstream tool, record it in the RUN
     LOG as a **link** and, if it blocks the next run, note it in the final
     summary — do not fix other repos during this checkpoint.
   - Never invent lessons. If nothing concrete surfaced, append
     `no lessons this run — <date>`.

4. **Efficiency lens.** If a repeated pattern in this run (multiple identical
   calls, an expensive full re-run when an incremental existed, a slow step
   with a known faster alternative) is worth collapsing, append a
   **proposal-only** line to the RUN LOG (`proposal: ...`). Do not build the
   proposal this run — it is inventory for a future Phase 7 slice.

5. **Commit the checkpoint.** The RUN LOG / guidance edit is its **own
   commit** (`docs: audit-pipeline run log <date>`) in this repo, pushed when
   `gh` is ready. Rollback is `git revert`.

## RUN LOG

Append-only. One entry per run. Format: `- <date> — lesson (what happened →
what changed in this file).` Prefix a `proposal:` line when it is efficiency
inventory only.

- 2026-09-28 — private flake inputs need `git+https`, not `github:` (the
  `github:` prefetcher cannot authenticate private repos; `git+https` uses the
  `~/.git-credentials` helper `gh` already manages). When wiring a new sibling
  into this repo's flake, use `git+https` and let `nix flake lock` verify.
- 2026-09-28 — `kraken-market-data` CLI puts `--store` (or `MARKET_DATA_DIR`)
  **before** the subcommand: `MARKET_DATA_DIR=... python cli.py update --pair
  ETH/USD --interval 60` works; `python cli.py update ... --store` errors.
  Phase 6 commands reflect this — keep them that way if the CLI changes.
- 2026-09-28 — end-to-end integration smoke test of the store-backed path was
  validated on real Kraken data (train + backtest through `market_data_store`,
  baseline control, gate PASS). The bot's dev shell needed `pyarrow`+`requests`
  and the sibling on `PYTHONPATH` before `market_data_store` worked — Phase 4's
  scaffold step for a `kraken-*` data source should mention wiring the flake
  input into the bot dev shell as a first-class deliverable.
- 2026-09-28 — Phase 6's gate wording is store-specific ("persists", "bars grow
  beyond seed", "verify contiguous") and did not apply verbatim to a JSONL-seam
  source (kraken-social-signals). The reviewer adapted it to: (a) feature-width
  proof — `normalization.npz` feature_names + train log "N obs features" vs
  baseline, instead of store-persistence; (b) the source-is-consumed proof
  (`Merged N signal records from ... onto M OHLCV bars` merge log); (c) the
  equivalent-metrics gate unchanged (|Δ return| within 5 pts, same sign). When
  the next source is an exogenous JSONL seam, use that adapted shape; only
  store-backed sources need the persistence proof.
- 2026-09-28 — the registry was stale: `kraken-funding-rates` (built the
  previous pass) had no row and was never pushed (no remote). Registry now has
  a "not pushed yet" row for it and a row for `kraken-social-signals`. Check
  `gh repo list` against the registry after each scaffold, not only when a new
  repo is created.
- 2026-09-28 — a sibling-repo pass scaffolded by a worktree builder makes no
  bot-repo commits: `team_shutdown` says "made no changes" and `team_merge`
  says "Nothing to merge". That is the expected outcome when the deliverable
  lands in the standalone sibling (the worktree is just cwd), not a failure to
  repair. Do not force a merge; verify the sibling repo has the commit instead.
  proposal: read `_SIGNAL_COLUMNS` from a single source (config) — the 2-file
  sync (data.py + features.py) is now a 3-pass repeated dance (news, funding,
  social) with a silent-drop failure mode; AUDIT.md and PLAN.md both flag it.
- 2026-09-28 — the command only shipped NEW-DATA-SOURCE outcomes: Phase 3
  mandated "name the project / new sibling repo" and Phase 4 was `git init` +
  `gh repo create`, so when the audit ranked a pure-code improvement as the
  highest-directness item (normalization wiring), the architect had no routing
  and deferred it as "companion hardening". Added an explicit outcome-type
  branch — `NEW-DATA-SOURCE` vs `IMPROVE-EXISTING` — to Phase 3/4 (4A/4B), the
  spawn sequence, Phase 5/6 wording, the team-shape/task-board tables, and the
  registry guardrail. The audit now lists "improve what already exists" as the
  first gap category. Improvement outcomes are decided by AUDIT.md evidence,
  never defaulted to new-project.
- 2026-09-29 — kraken-deep-history pass (NEW-DATA-SOURCE): Binance public archive
  seeder into the kraken-market-data store; gate PASS, registry row added. Lessons:
  - The three parallel researchers are told to WRITE SEPARATE files
    (`.data-audit/RESEARCH-{1,2,3}.md`) and the lead assembles `RESEARCH.md` from
    them — Phase 2 previously said "appends its findings to RESEARCH.md", which
    would have clobbered under three concurrent writers. Guidance fix applied so
    future runs don't have to improvise it.
  - A store-backed source keeps the Phase 6 gate cleanly applicable (persistence +
    verify contiguous + bars > REST ceiling), so a live bounded seed (e.g.
    eth/usd 60m from 2025-01-01) is a strong smoke; the reviewer proved depth by
    seeding 14592 bars (~20x ceiling) and showed the LIVE leg stays capped at
    ~721 bars even at `--pages 21` — that page-count gold control is worth keeping
    as the "depth is real and only the store unlocks it" proof.
  - When the sibling store package (`market_data`, `pyarrow`) isn't importable in
    the seeder's own dev shell, the seeder falls back to CSV mode and the BOT
    reads 0 bars (parquet glob); the bot-facing seed must run where the store
    package is importable (the bot's dev shell with the seeder on PYTHONPATH).
    Reviewer hit this live and the README/INTEGRATION docs were extended to say
    so — record "seed in market-data mode, not fallback" in future store-fronting
    passes.
  - Pre-existing uncommitted work in the tree (untracked `rl/export.py` while
    committed `rl/__init__.py` imports `.export`) breaks the Nix-packaged
    `kraken-trading-bot` binary even though pytest passes (source-tree
    resolution). The reviewer worked around it by invoking the CLI in-process
    from the source tree (the tests' own pattern). When a tree has uncommitted
    work, note that the packaged-binary check may be unreliable and prefer the
    in-process main() route.
  - Phase 7 dev-fix slice (stats crash + docs) ran as a one-task builder exactly
    like the fix loop; 57 tests green, pushed. Two-repo loop confirmed: both
    repos pushed in one pass.
  proposal: give the kraken-* seeder a `--check-store-mode` guard (refuse to
  seed a bot-bound root in fallback-CSV mode, or auto-warn) so the 0-bars trap is
  caught at seed time, not at bot read time.
- 2026-09-30 — IMPROVE-EXISTING pass (Candidate 1, normalization.npz now shapes
  the RL observation); gate PASS; two commits + a regression-test commit pushed.
  Lessons:
  - `team_merge` is NOT trustworthy as the only transport of a builder's fix
    COMMIT: merging the builder branch brought in the WIP commit's content but
    silently dropped the fix commit (`e57056d`) that sat on top of it — the
    working tree's `data.py:547` still called `features.fit(df)` before slicing
    after the merge said "Merged". Catch is cheap: after every `team_merge`,
    verify the merged working tree against the builder's reported HEAD
    (`git show <commit> --stat` and spot-check one landed line) before building
    on it. The lead re-applied the fix via `git diff <wip> <fix> | git apply`
    and re-ran pytest before committing.
  - A force-shutdown for a model switch can race a builder that has genuinely
    finished: the original builder had already completed and pushed the fix
    when the shutdown landed, and the respawned model-switched builder then
    duplicated the entire deliverable on a second branch (later force-shut,
    unmerged). Before aborting a builder to change models, check its latest
    team_message / branch HEAD first — if the work is done, just merge it.
  - A builder worktree directory can be deleted mid-task (left `prunable` by a
    concurrent `git worktree` operation) while the branch survives: teammate
    recovered with `git worktree prune` + re-add. If a builder reports a lost
    worktree but clean commits, trust the branch, not the directory.
  - The qa reviewer's "stalled (low output tokens)" system notice is a false
    positive during long `nix develop` builds + a live train smoke: reviewer was
    nudged at ~47s working and had already completed all three integration steps
    before the message arrived. Treat low-token silence on a `qa`/`build`
    teammate as normal for at least ~5 min; only nudge on elapsed time.
  - `--ticker USD_SOL` fails with `Unknown Kraken pair: 'USD/SOL'` (CLI expects
    `BASE/QUOTE`, real Kraken base asset); `ETH_USD` is what `default.yaml`
    sets and works. Future passes that assume a pair should default to
    `ETH_USD` on this host.
  - Live training windows are not reproducible while `market_data_store: null`:
    measured this pass, one CLI train vs a later fetch disagreed 16% on
    `rsi_24`'s fitted std (two fresh fetches agree to 1.2e-4; repeat train
    reproduces to 6.5e-7) — the snapshot, not the code path, varies. For
    cumulative features like `obv` compare fitted STDs across snapshots, never
    MEANs (a cumsum start-level drifts 16% by design). This is now the measured
    core of the Candidate-2 (store-seed) case, recorded in PLAN.md.
  - `.data-audit/` is tracked in git despite the command's "do not commit it"
    note (prior passes committed it, this pass re-committed audit/decision/
    plan/validation docs). The note is stale guidance; decide once whether the
    artifact dir is committed or ignored and say so — today repo history and
    the note disagree.

- 2026-10-01 — IMPROVE-EXISTING pass (Candidate 1, make the three exogenous signal seams sound:
  A1 ticker filter, A2 hour dedup, A3 bounded ffill + freshness columns, A4 absence != neutral in
  `merge_extra_features`; integrator proved config wiring end-to-end and fixed a sub-hourly
  carry-bound bug). Gate PASS (119 tests, flake check green, Leg A 49-wide at defaults, Leg B
  49→60 with freshness pair finite in the z-scored obs). Pushed `5951f72` + `1e404ba` + docs
  `18343ce`. Lessons:
  - A `team_merge` lands the builder's work as UNSTAGED changes in the lead's working tree, and a
    downstream worktree teammate branches from the last COMMIT. The integrator started one commit
    behind (its HEAD was the builder commit's parent) and correctly blocked reporting a false
    base. Fix: after `team_merge`, COMMIT the merged builder land before spawning any further
    worktree teammate — guidance fix applied to spawn-sequence step 6. If a teammate still reports
    being behind, authorize `git merge --ff-only <upstream-commit>` explicitly (proven clean).
  - A second `team_merge` of a branch that itself contains an upstream land commit conflicts with
    the already-landed tree (`Recorded preimage`). Fix used successfully: commit the upstream land
    first, then on conflict take the downstream's version of shared files (`git checkout --theirs
    <file>`) — it contains upstream + the downstream delta.
  - `depends_on` IDs written from memory can be fabricated silently: I typed a non-existent ID for
    the integrate/verify tasks, the board showed both blocked for the whole run, and
    `team_tasks_complete` REFUSED to complete a blocked task manually — there is NO board-edit
    API, so the board stayed wrong. Work still ran (teammates spawn unclaimed and report fine),
    but the chain had to be tracked by hand. Guidance fix: the Task board section now says to
    write depends_on from the same team_tasks_add response and to treat a poisoned board as
    permanent (spawn anyway, track manually).
  - The force-shutdown/respawn race bit twice this run in different disguises: the first auditor
    was force-shut for a model change but its completion message and an AUDIT.md write were still
    in flight, while the respawned auditor ALSO wrote an AUDIT.md — producing two different
    audits on disk (the second overwrote the first) and a researcher/architect brief mismatch.
    When a force-shutdown precedes a respawn, check for a late completion AND reconcile which
    artifact is actually on disk before spawning downstream; tell the architect explicitly which
    audit is authoritative.
  - Repeated "stalled (low output tokens)" notices fired for read-only general/qa agents
    (researcher2/3, architect, reviewer) while they were mid-`nix develop` build or quiet-reading;
    each was false. Confirms the RUN LOG's prior qa/build note extends to read-only roles: treat
    low-token silence as normal for ≥5 min and nudge only on elapsed time, not on the notice.
  - `export-data` is a first-class route for proving a JSONL-seam source reaches the observation
    without a full train: the reviewer showed `signal_age_hours`/`signal_observed` in the export
    CSV, ages {-1,0,1}, observed 301/721, → normalization.npz 60 features all finite. For a seam
    pass, an export-route Leg B is a faster consumed-proof than a live train.
  - .data-audit/ tracking note fixed in the command: artifacts ARE committed as a docs commit per
    convention; "do not commit it" was stale for two passes.
- 2026-10-01 (later) — IMPROVE-EXISTING pass (Gap 1, activate six already-stored columns), started
  from Phases 1–3 artifacts left UNCOMMITTED by an interrupted run and re-verified at HEAD before
  reuse. Gate NEEDS_FIX then PASS; 6 commits pushed (`ca360a8..70e062a`), 143 -> 147 tests.
  Lessons:
  - **The pass's own dense fixtures hid a silent defect; the gate caught what the suite could not.**
    143 green tests, then the gate found a one-record funding file left `spread` NaN on 720/721
    rows (the seam zero-fills `bid`, and the micro builder does `(ask-bid)/bid.replace(0,nan)`), so
    `_first_valid_index()` pushed the start index to 720 and backtest replayed **1 bar / 0 trades**
    — no exception, and the width guard PASSED because it checks width, not NaN. Every fixture
    populated funding on EVERY row, so `spread` was never NaN: the tests encoded the unproblematic
    case, which is exactly the case the shipped hourly-timer activation does NOT produce.
    Guidance fix applied: Phase 6 §6 now has a mandatory **magnitude clause** (bars replayed,
    trades taken, new columns present BY NAME) and the Guardrails no longer treat "completes without
    error" as sufficient. A run can complete cleanly and still be a fail.
  - The fix needed **provenance, not a NaN-pattern heuristic**: with one record on the last bar,
    `spread` is NaN on rows 0..719, byte-identical to a 720-bar rolling window's warm-up, so warm-up
    NaN and absence NaN are not separable from the pattern and a "leading prefix" test cannot work.
    Declaring which columns are exogenous (point-in-time by definition, no look-back, never
    warming up) generalises to any future sparse exogenous column. Rejected and why, all worth
    keeping: computing `spread` only where observed = fabricating 0.0; selecting the start index
    from the filled matrix = `ffill()` is a no-op (a bar is warm for a column exactly when that
    column has a value) and `fillna(0)` collapses the index to 0 and trains on zero-filled garbage —
    a WORSE silent defect; changing the seam's zero-fill does not fix it and the zero is the
    load-bearing absence sentinel shared with `signal_observed`.
  - **A duplicated rule is how two consumers drifted**: `export.py::_warmup_mask` was a restatement
    of `TradingEnvironment._first_valid_index` ("Mirrors ..."), which is why export claimed 720
    warm-up rows while the environment traded 1 bar. Collapse duplicated rules into one function
    when a defect shows the same invariant enforced twice. `proposal:` grep for "Mirrors" /
    "see also" comments between env/export/train code paths — each is a candidate rule that can
    drift silently.
  - **Require the reviewer to prove a regression test is non-vacuous, as a gate criterion.** The
    sharpest evidence was reverting ONLY the source change (not the test) in a /tmp COPY and
    watching the new test fail with the original symptom
    (`assert 1 > (0.9 * 721)`). Make this an explicit step for every Phase 7 fix: a test that would
    pass without its fix proves nothing.
  - **`git diff <commit>` ignores UNTRACKED files** — a builder's NEW files (here `systemd/*`)
    looked deleted after `team_merge` and I nearly reported a false merge failure. The only
    reliable equality check is per-file content: `git show <commit>:<path> | diff -q - <path>`
    for every path in the commit. Also: a `reviewer`/`qa` teammate writing artifacts directly into
    the lead's tree AND committing them is fine — `team_merge` refuses with "local changes to the
    same files", and that refusal is not an error: compare per-file, then commit the lead's copy.
  - **The harness can reassign a teammate's worktree path while its branch survives** (the builder's
    path was replaced mid-run; `team_merge` then said "No branch to merge"). Take the commit by
    BRANCH ref (`git checkout <commit> -- <path>` for a single file, or `git merge`) instead. The
    branch is the reliable handle, never the directory — reinforces the 2026-09-30 entry.
  - **`team_status` counts the stall notice itself as a "nudge"** (showed "nudged 5m ago" for
    teammates I had never messaged, with a nudge time EARLIER than the working time). The
    low-output-token "appears stalled" notice is therefore pure noise — it fired 5 times this run,
    every one false, typically within a minute of spawn. Ignore it entirely; nudge on elapsed time.
  - **DECISION figures can be a fixture artifact, not a production truth.** DECISION's gate "55" was
    the width of a synthetic test frame carrying exactly the six new columns. Because every builder
    is presence-gated the real width is dynamic: 49 (no vwap/count, no funding) / 52 (vwap/count, no
    funding) / 60 (funding present) — 52->60 is +8, not +6, because turning on
    `funding_features_file` also switches on the columns already on the allow-list
    (`funding_rate`, `basis`, `open_interest`) plus the freshness pair. Ask the architect for the
    width **per configuration**, and have the README state the range rather than one number.
  - I repeated the `depends_on` mistake from the 2026-10-01 entry verbatim: wrote placeholder
    strings (`__SCAFFOLD__`) into `depends_on` instead of the IDs from the same `team_tasks_add`
    response, and two tasks showed `blocked` for the whole run with no board-edit API. Cost was low
    only because the documented fallback is to track the chain by spawn order — but this is now the
    SECOND time, so: compose the `depends_on` list from the literal response of the SAME call, and
    re-read it before sending. If the board is already poisoned, ignore it and do not retry.
  - **Teammates must not delete files in the lead's main checkout.** The builder deleted
    `models/{ETH,SOL,XRP}_USD/` (correctly untracked scratch, per DECISION §2.2) but did it in MAIN,
    not its worktree, and admitted one accidental edit there. Ask builders to report what they
    touched outside the worktree instead of tidying; the lead owns MAIN's state.
  - **Cost reality for an in-repo IMPROVE-EXISTING pass: no new repo, no `gh repo create`, no flake
    input to wire, no NixOS module to add** (Phase 4B skips all of it, and the shipped timer became
    a plain `systemd/user` unit because `nix/module.nix` is a NixOS module and `systemd.user.*` is a
    home-manager option that would not evaluate there). The whole pass is a handful of RL commits in
    one repo. So don't estimate this as a "multi-repo loop".
  - **What the fix did NOT do, and PLAN.md must say so:** it restored the trading WINDOW, it did not
    enrich the SIGNAL — bars carrying a funding reading stay 1/721 before and after. Signal QUALITY
    is Gap-2 work (accumulate coverage by running the timer, then re-derive `signal_max_age_hours`
    from measured cadence). Also flagged: the live Kraken OHLC endpoint carrying `vwap`/`count` is
    the only reason 52/60 are reachable at all, so that provenance is now the durability risk for
    this pass's gains.
  - Efficiency, measured: this session cost ~15.9M tokens (input 12.1M / output 1.6M / reasoning 2.1M,
    cache read 1.26M) across ~104 tool calls. Most avoidable cost is the repeated `nix develop`
    rebuild of the dev shell — 5 invocations at 17–28s each, once per phase-level verification.
    proposal: batch the lead's own verification (pytest + flake check) into a SINGLE `nix develop`
    call per checkpoint instead of one per phase, and let the reviewer own the heavy end-to-end runs.
  - proposal: the three `.data-audit/RESEARCH-*.md` files plus the assembled `RESEARCH.md` are ~93KB
    of markdown and are read by every downstream phase from the MAIN checkout via absolute path (the
    worktrees do not have them, since the artifact dir is uncommitted until run end). Consider
    committing the artifact dir EARLY (or having the lead copy artifacts into each worktree at spawn)
    so teammates can `git show` them instead of being handed absolute paths — this also removes a
    class of "the artifact I was told to read is not in my worktree" failures.
---

## Guardrails

- The audit must genuinely span multiple categories before narrowing.
- One data vector (new source) or one improvement slice per pass.
- Never commit real API keys; follow the `.env.example` convention.
- Respect each source's rate limits and terms of use; prefer keyless sources over paid ones unless
  DECISION.md justifies the cost.
- **New repos are pushed to GitHub automatically when `gh` is authenticated** (`gh auth status`
  succeeds): run `gh repo create <name> --source . -p/--public --push` as part of Phase 4. Do
  not ask permission for repo creation/push when `gh` is ready — this is the intended workflow.
  Only ask before opening a PR or changing remote visibility after the push. If `gh` is
  unavailable, record the local-only state in the registry and note it in the report.
- **Keep the "Current data-source projects" registry current**: on every run, after scaffold (4A)
  or after an `IMPROVE-EXISTING` pass that pushes an existing project for the first time, add or
  update the row — new project gets its GitHub URL + local path; an existing project (e.g.
  `kraken-funding-rates`) with no URL yet and none was pushed gets "not pushed yet" and an offer
  to push it as a follow-up.
- If a later phase invalidates an earlier decision (e.g. the chosen library turns out to be
  unmaintained), stop and redo the earlier phase rather than pushing on.
- Do not poll teammates or sleep while waiting — see "Waiting on teammates".
- **The integration test is mandatory, not optional.** A pass is not "scaffolded and unit-tested";
  it is "the bot trained and backtested reading through the pass's outcome, proving the bot
  actually CONSUMES it" (Phase 6 gate). For a `NEW-DATA-SOURCE` that means the source reaching
  the observation and, if store-backed, persistence observed; for an `IMPROVE-EXISTING` it means
  the proof that the bot's behaviour/observation actually changed. In every case the gate must
  include **evidence of magnitude, not just absence of error**: bars replayed, trades taken, and
  the new columns present BY NAME. A run that completes with no exception while silently
  collapsing to ~1 bar / 0 trades is a FAIL — see the 2026-10-01 entry. If the gate fails, Phase 7
  must fix it before the run is reported as done.
- **Two repos, one loop.** Development continues in both the sub-project and `kraken-trading-bot`
  after scaffold; every dev slice ends with both repos' tests green and, when `gh` is ready, both
  pushed. Do not leave a dev slice half-pushed.
- Scratch stores/models from integration tests live under `/tmp/...` only — never commit them.
- **Phase 8 self-improvement runs after every run.** The command file is not frozen; grounded
  lessons shorten the next run. Skip it only when the run was aborted before any phase executed.
