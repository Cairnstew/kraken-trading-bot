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
  `./.data-audit/` (create it; do not commit it).
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
| Builder | `build` | `true` | Scaffold the new project per DECISION.md |
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
those returned IDs and pass them to `depends_on` — never label names.** If any `depends_on`
references a label instead of a real ID, every downstream task shows as `blocked` and teammates
cannot `claim_task` (the work still runs, but the board lies and `team_tasks_complete` is refused
— correct the board by editing the tasks with real IDs before spawning, or accept the broken
board and complete tasks manually as each agent reports).

| Task key (label) | Description | depends_on (real IDs from `team_tasks_add`) |
|------------------|-------------|------------------------|
| `audit` | Auditor: read code, map pipeline, enumerate gaps → `AUDIT.md` | — |
| `research-1` | Researcher: library survey for top gap #1 | `audit` |
| `research-2` | Researcher: library survey for top gap #2 | `audit` |
| `research-3` | Researcher: library survey for top gap #3 | `audit` |
| `decision` | Architect: pick one (gap, library) pair → `DECISION.md` | all `research-*` |
| `scaffold` | Builder: create new project per DECISION.md | `decision` |
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
   DECISION.md, the house style context, and the Phase 4 spec. The builder creates the sibling
   repo, commits everything, and **creates + pushes the GitHub remote when `gh` is
   authenticated** (see Phase 4 step 8).

6. When the builder reports done: `team_shutdown`, `team_merge`. Inspect the merged diff. Then
   spawn the integrator (`build`, own worktree, `claim_task: integrate`). Give it: DECISION.md,
   the Phase 5 spec. The integrator adds the minimum adapter stub in this repo.

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
libraries/APIs/packages that could source it. Search broadly; do not limit yourself to any fixed
list.

As a reference example of the rigor expected, a prior survey of text/news sourcing covered
`newspaper4k`, `trafilatura`, `feedparser`, `newsapi-python`, `GNews`, and for academic sources
`arxiv`, `pyalex`, `semanticscholar`, `habanero`/`crossrefapi`, and `Bio.Entrez`. Apply the same
rigor to whatever categories the audit surfaced.

For each serious candidate record: maintenance status and last release, license, auth (keyless /
API key / paid), rate limits, and output shape (structured JSON vs. raw HTML/text needing parsing).

Score each option partly on how cheaply it reduces to what the RL pipeline can consume: a
per-ticker, per-timestamp scalar or small vector beats a pile of raw unstructured output.

Each researcher appends its findings to `.data-audit/RESEARCH.md`. End with `RESEARCH COMPLETE`.

---

# PHASE 3 — DECISION (architect)

Do not scaffold yet.

1. Pick exactly one (gap, library) pair for this pass. One clean pipeline beats three half-built
   ones.
2. Justify it against the RL pipeline's real shape from AUDIT.md: config inputs, the
   `train`/`backtest` knobs, and the `models/{TICKER_ID}/{model_name}/` artifact layout. The new
   feature must land somewhere a feature-engineering step can read it per ticker.
3. Name the project: in the `kraken-*` family if Kraken/crypto-specific, otherwise a
   source-oriented name. State why.
4. Target location: a new git repo in a sibling directory, `../<new-project-name>/`, not nested
   inside this repo and not a submodule.
5. Write DECISION.md: chosen gap, library(ies), project name and path, a one-paragraph
   integration sketch (see Phase 5), and the runner-up options and why they lost.

Write `.data-audit/DECISION.md` and end with `DECISION COMPLETE`.

---

# PHASE 4 — SCAFFOLD (builder)

Create the sibling repo per DECISION.md, mirroring the house style above.

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

Commit everything in the worktree. Report done via `team_message` with the diff and commit hash
(and the new GitHub URL, or "not pushed — no gh/auth" if skipped).

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
   `train`/`backtest`.

Commit in the worktree. Report done via `team_message` with the diff and commit hash.

---

# PHASE 6 — VALIDATE, INTEGRATION-TEST, AND REPORT (reviewer)

The reviewer must do **both** unit validation and a **real end-to-end
integration test of the bot against the new data source** — the point of the
source is that the bot consumes it, so a positive result is an actual
train/backtest reading through it. Everything below was live-verified for the
`kraken-market-data` pass (period: real Kraken data, 2026-09-28); adapt the
commands to the chosen source.

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

The command does not stop at "scaffolded". The user wants the new data source
to be *positive for the bot* and continued development of **both** the
sub-project and main repo.

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

---

## Guardrails

- The audit must genuinely span multiple categories before narrowing.
- One data vector per pass.
- Never commit real API keys; follow the `.env.example` convention.
- Respect each source's rate limits and terms of use; prefer keyless sources over paid ones unless
  DECISION.md justifies the cost.
- **New repos are pushed to GitHub automatically when `gh` is authenticated** (`gh auth status`
  succeeds): run `gh repo create <name> --source . -p/--public --push` as part of Phase 4. Do
  not ask permission for repo creation/push when `gh` is ready — this is the intended workflow.
  Only ask before opening a PR or changing remote visibility after the push. If `gh` is
  unavailable, record the local-only state in the registry and note it in the report.
- **Keep the "Current data-source projects" registry current**: on every run, after scaffold,
  add the new project's row with its GitHub URL + local path; if an existing project (e.g.
  `ticker-news-signals`, `kraken-market-data`) has no URL yet and none was pushed, note
  "not pushed yet" and offer to push it as a follow-up.
- If a later phase invalidates an earlier decision (e.g. the chosen library turns out to be
  unmaintained), stop and redo the earlier phase rather than pushing on.
- Do not poll teammates or sleep while waiting — see "Waiting on teammates".
- **The integration test is mandatory, not optional.** A pass is not "scaffolded and unit-tested";
  it is "the bot trained and backtested reading through the new source, equivalent to baseline,
  with persistence observed" (Phase 6 gate). If the gate fails, Phase 7 must fix it before the
  run is reported as done.
- **Two repos, one loop.** Development continues in both the sub-project and `kraken-trading-bot`
  after scaffold; every dev slice ends with both repos' tests green and, when `gh` is ready, both
  pushed. Do not leave a dev slice half-pushed.
- Scratch stores/models from integration tests live under `/tmp/...` only — never commit them.
- **Phase 8 self-improvement runs after every run.** The command file is not frozen; grounded
  lessons shorten the next run. Skip it only when the run was aborted before any phase executed.
