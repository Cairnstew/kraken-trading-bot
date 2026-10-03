# justfile — standard commands for kraken-trading-bot
#
#   just --list             show all recipes
#   just train              train ppo_eth_01 on ETH/USD with defaults
#   just train --ticker XRP_USD --model ppo_xrp_02 --timesteps 100000
#   just bench SOL_USD ppo_sol_01 50000   train + backtest + show performance
#   just export-data --ticker SOL_USD      dump the data pipeline frame to CSV
#   just paper --ticker ETH_USD --model ppo_eth_01 --iterations 10
#   just matrix-plan                       expand a model-evaluation matrix
#   just test               run the pytest suite
#   just funding-pull       pull one Kraken funding snapshot (keyless)
#   just funding-backfill   fill the funding channel's file with Kraken's
#                           own ~366-day hourly history (keyless, idempotent)
#   just funding-timer      enable the hourly systemd.user funding timer
#   just news-pull         pull one hour of Google News sentiment (keyless,
#                           --append)
#   just social-pull       pull StockTwits + Fear & Greed (keyless, --append)
#   just news-timer        enable the hourly systemd.user news timer (:23)
#   just social-timer      enable the hourly systemd.user social timer (:29)
#   just depth-timer       enable the hourly order-book DEPTH recorder timer
#                           (:41) — G1; the only irreversible-data recipe here
#   just depth-pull        record one depth snapshot (what the timer runs)
#   just depth-gaps        scan the depth log for holes (no network)
#
# Every recipe that needs the project environment runs **inside the Nix
# dev shell** automatically via `nix develop --command bash -c ...`, so you
# never have to `nix develop` by hand. The first run evaluates the flake;
# later runs reuse the cached build.
#
# The RL recipes take **any** additional arguments and pass them straight
# to the `kraken-trading-bot` CLI (all flags are optional; omitted ones
# fall back to the CLI's own defaults). The `paper` subcommand maps to the
# `paper-trade` CLI subcommand, so `just paper --dry-run ...` works too.
# Avoid single quotes inside the extra arguments (they delimit the
# bash -c script).
#
# ── A DEFAULT RUN IS **NOT** AN OUT-OF-SAMPLE MEASUREMENT ─────────────
# `configs/default.yaml` ships `data_window.since: null` / `until: null` on
# purpose: pinning a window with no market-data store gives 0 train and 0
# eval bars, so a pinned default would make the bot refuse to run rather
# than measure anything honestly. The cost is that `train` + `backtest` on
# the default config is **IN-SAMPLE** -- it describes the fit, not a
# prediction -- and the bot prints that warning itself every run.
#
# Do NOT read a return off a default run as evidence of an edge. With one
# run per arm and PPO's run-to-run stochasticity unquantified, the spread
# between two configs routinely exceeds the effect being measured.
#
# THE OOS PATH: seed the store (the bot only READS one; REST caps at ~720
# bars), then run with the example config that pins the window to match.
#
#   just store-plan      # zero-network dry run: which ZIPs it would fetch
#   just store-seed      # download, and assert store_mode == "market-data"
#   just store-verify    # count missing bars, list gaps, name the seam hole
#   just train    --config configs/deep-history.example.yaml ...
#   just backtest --config configs/deep-history.example.yaml ...
#
# Read `configs/deep-history.example.yaml`'s own header before trusting a
# store-backed number: the store buys depth but also buys HOLES, and
# features are computed on bar counts, not wall-clock, so a multi-hour gap
# reads as a one-bar step. `just store-verify` is what detects that.
dev := "nix develop --command bash -c"

# ── Environment ────────────────────────────────────────────────────────

# Print this help
default:
  @just --list

# Drop into the interactive Nix dev shell (full RL stack)
shell:
  nix develop

# Create ./venv with the local kraken-python fork + this package
# (faster interactive alternative to `just shell`; still optional)
setup-venv:
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -e /home/seanc/Projects/kraken-python
  ./.venv/bin/pip install -e .

# ── Classic strategy runner ────────────────────────────────────────────

# Run the SMA strategy loop against live Kraken data (no RL). Passes
# straight through to `kraken-trading-bot run`. Prefer --paper: without it
# the engine places real orders and needs KRAKEN_API_KEY/SECRET.
# e.g. just run --paper --pair ETH/USD
run *CLI_ARGS="":
  {{dev}} 'kraken-trading-bot run {{CLI_ARGS}}'

# ── RL workflow ────────────────────────────────────────────────────────

# Train a PPO model. Args pass through to `kraken-trading-bot train`
# e.g. just train --ticker XRP_USD --model ppo_xrp_02 --timesteps 100000
train *CLI_ARGS="":
  {{dev}} 'kraken-trading-bot train {{CLI_ARGS}}'

# Train and immediately backtest in one shot, so the performance block
# follows the training log. Positional params, all optional and defaulted:
# ticker, model, timesteps, pages, seed, models-root — so `just bench`,
# `just bench SOL_USD ppo_sol_01`, `just bench SOL_USD ppo_sol_01 50000`
# and `just bench SOL_USD ppo_sol_01 50000 6 42` all work. Unlike the
# other RL recipes this does not take free pass-through, because the same
# values have to be handed to *both* subcommands — only flags shared by
# `train` and `backtest` can be forwarded blindly, and `--timesteps` is
# not one of them. `*TRAIN_ARGS` is appended to `train` only, for
# train-exclusive flags (e.g. --config, --action-space, --episode-bars);
# `backtest` re-reads those from the saved model config. The backtest
# refetches the same `pages` window seconds after training, so the
# numbers are in-sample — for a genuine holdout re-run `just backtest`
# later, or with a smaller --pages.
# e.g. just bench SOL_USD ppo_sol_01 50000 6 42 --action-space discrete
bench ticker="ETH_USD" model="ppo_eth_01" timesteps="10000" pages="6" seed="42" models_root="models" *TRAIN_ARGS="":
  {{dev}} 'kraken-trading-bot train --ticker {{ticker}} --model {{model}} --timesteps {{timesteps}} --pages {{pages}} --seed {{seed}} --models-root {{models_root}} {{TRAIN_ARGS}} && kraken-trading-bot backtest --ticker {{ticker}} --model {{model}} --pages {{pages}} --seed {{seed}} --models-root {{models_root}}'

# Backtest a trained model on fresh OHLC data
# e.g. just backtest --ticker ETH_USD --model ppo_eth_01
backtest *CLI_ARGS="":
  {{dev}} 'kraken-trading-bot backtest {{CLI_ARGS}}'

# Run the data pipeline (OHLCV -> signal merges -> features) and write the
# staged frame to CSV. Columns keep their pipeline order: timestamp, OHLCV,
# any merged signals, then the raw observation features (plus a warmup flag
# for the look-back rows the agent never trades on). --normalized appends
# the z_-prefixed columns: the agent's z-scored observation (mirrors what
# TradingEnvironment hands the policy, per normalization.npz).
# e.g. just export-data --ticker SOL_USD
#      just export-data --ticker SOL_USD --episode-bars 500 --normalized
export-data *CLI_ARGS="":
  {{dev}} 'kraken-trading-bot export-data {{CLI_ARGS}}'

# List models. Pass --ticker T or --json to filter/format
# e.g. just models --ticker ETH_USD — just models --json — just models
models *CLI_ARGS="":
  {{dev}} 'kraken-trading-bot models {{CLI_ARGS}}'

# Paper trade a trained model (simulated orders). Map to paper-trade
# e.g. just paper --ticker ETH_USD --model ppo_eth_01 --iterations 10
#      just paper --ticker ETH_USD --model ppo_eth_01 --dry-run
paper *CLI_ARGS="":
  {{dev}} 'kraken-trading-bot paper-trade {{CLI_ARGS}}'

# Dry-run paper trading (print would-be orders, execute nothing)
# e.g. just paper-dry --ticker ETH_USD --model ppo_eth_01
paper-dry *CLI_ARGS="":
  {{dev}} 'kraken-trading-bot paper-trade {{CLI_ARGS}} --dry-run'

# ── Account / market (needs KRAKEN_API_KEY/SECRET for private calls) ──────

# Show current ticker for a pair
# e.g. just ticker --pair ETH/USD
ticker *CLI_ARGS="--pair ETH/USD":
  {{dev}} 'kraken-trading-bot ticker {{CLI_ARGS}}'

# Show account balances (requires credentials)
balance:
  {{dev}} 'kraken-trading-bot balance'

# Show open orders (requires credentials)
orders:
  {{dev}} 'kraken-trading-bot orders'

# ── Exogenous signals ───────────────────────────────────────────────────

# Enable the hourly funding-snapshot systemd.user timer, so the funding
# file `configs/default.yaml` points at stays current: one keyless API
# call and one appended line per hour.
#
# `pair` and `output` must match the `funding_features_file` your model
# config points at; the unit is generated (not symlinked) because
# ExecStart embeds both.
funding-timer pair="ETH/USD" output="signals/eth_usd_funding.jsonl":
  #!/usr/bin/env bash
  set -euo pipefail
  unit_dir="$HOME/.config/systemd/user"
  root="{{justfile_directory()}}"
  mkdir -p "$unit_dir" "$root/$(dirname {{output}})"
  sed -e "s|@PAIR@|{{pair}}|" -e "s|@OUTPUT@|$root/{{output}}|" \
      "$root/systemd/kraken-trading-bot-funding.service.in" \
      > "$unit_dir/kraken-trading-bot-funding.service"
  ln -sf "$root/systemd/kraken-trading-bot-funding.timer" \
      "$unit_dir/kraken-trading-bot-funding.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now kraken-trading-bot-funding.timer
  echo "enabled. Next fire:"
  systemctl --user list-timers kraken-trading-bot-funding.timer --no-pager

# Pull one funding snapshot by hand (the same command the timer runs)
#
# `output` is repo-relative by default and absolute if it looks absolute.
# An earlier revision always prefixed $root, so `just funding-pull /tmp/x.jsonl`
# wrote to "<repo>//tmp/x.jsonl" — a path under the repo, created by
# `mkdir -p`, that no caller asked for.  funding-backfill had the same bug and
# is fixed identically here so the two recipes cannot drift apart again.
funding-pull pair="ETH/USD" output="signals/eth_usd_funding.jsonl":
  #!/usr/bin/env bash
  set -euo pipefail
  root="{{justfile_directory()}}"
  dest="{{output}}"
  case "$dest" in /*) ;; *) dest="$root/$dest" ;; esac
  mkdir -p "$(dirname "$dest")"
  nix run ~/Projects/kraken-funding-rates#kraken-funding-rates -- \
    pull --pair {{pair}} --output "$dest" --append

# Pull one hour of Google News sentiment into the news channel's file.
# This is the command kraken-trading-bot-news.service runs; run it by hand
# to seed a fresh clone, which the non-null `extra_features_file` key
# otherwise refuses to read past (SignalFileNotFoundError).
#
# `--append` is load-bearing, not decoration.  The sibling's default
# --lookback-hours is 1, so a pull WITHOUT it replaces the file with a
# one-hour window — hourly, that is a file one hour deep forever, and the
# loss is silent because the file still parses.
#
# Invoked through the sibling's own flake, not `python .../cli.py`: gnews
# and vaderSentiment are not on THIS flake's PYTHONPATH, so the repo-root
# cli.py cannot import them here.  The sibling is named by path and built,
# not added as a flake input — it is private (`github:` would 404) and a
# pinned input would freeze every sibling fix into this repo's flake.lock.
#
# `output` is repo-relative by default and absolute if it looks absolute,
# matching `funding-pull`.
news-pull ticker="ETH/USD" output="signals/eth_usd_news.jsonl":
  #!/usr/bin/env bash
  set -euo pipefail
  root="{{justfile_directory()}}"
  dest="{{output}}"
  case "$dest" in /*) ;; *) dest="$root/$dest" ;; esac
  mkdir -p "$(dirname "$dest")"
  nix run ~/Projects/ticker-news-signals#ticker-news-signals -- \
    pull --ticker {{ticker}} --output "$dest" --append

# Pull StockTwits + Fear & Greed into the social channel's file.  Same shape
# as `news-pull`, and the same reason for `--append`: the default
# --lookback-hours is 24 and that window SLIDES, so replacing means the
# file is rewritten to the last day on every fire and never accumulates.
social-pull ticker="ETH/USD" output="signals/eth_usd_social.jsonl":
  #!/usr/bin/env bash
  set -euo pipefail
  root="{{justfile_directory()}}"
  dest="{{output}}"
  case "$dest" in /*) ;; *) dest="$root/$dest" ;; esac
  mkdir -p "$(dirname "$dest")"
  nix run ~/Projects/kraken-social-signals#kraken-social-signals -- \
    pull --ticker {{ticker}} --output "$dest" --append

# Enable the hourly news-snapshot systemd.user timer, so the news file
# `configs/default.yaml` points at stays current: two keyless Google News
# requests per hour (one symbol search, one whole-category pull) and one
# appended line.
#
# `ticker` and `output` must match the `extra_features_file` your model
# config points at; the unit is generated (not symlinked) because ExecStart
# embeds both.  Fires at :23 — clear of the funding timer's :17 plus its
# 120s jitter, and clear of the social timer's :29.
news-timer ticker="ETH/USD" output="signals/eth_usd_news.jsonl":
  #!/usr/bin/env bash
  set -euo pipefail
  unit_dir="$HOME/.config/systemd/user"
  root="{{justfile_directory()}}"
  mkdir -p "$unit_dir" "$root/$(dirname {{output}})"
  sed -e "s|@TICKER@|{{ticker}}|" -e "s|@OUTPUT@|$root/{{output}}|" \
      "$root/systemd/kraken-trading-bot-news.service.in" \
      > "$unit_dir/kraken-trading-bot-news.service"
  ln -sf "$root/systemd/kraken-trading-bot-news.timer" \
      "$unit_dir/kraken-trading-bot-news.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now kraken-trading-bot-news.timer
  echo "enabled. Next fire:"
  systemctl --user list-timers kraken-trading-bot-news.timer --no-pager

# Enable the hourly social-snapshot systemd.user timer.  Fires at :29, the
# third of the three non-colliding channel minutes (17 funding, 23 news,
# 29 social).
social-timer ticker="ETH/USD" output="signals/eth_usd_social.jsonl":
  #!/usr/bin/env bash
  set -euo pipefail
  unit_dir="$HOME/.config/systemd/user"
  root="{{justfile_directory()}}"
  mkdir -p "$unit_dir" "$root/$(dirname {{output}})"
  sed -e "s|@TICKER@|{{ticker}}|" -e "s|@OUTPUT@|$root/{{output}}|" \
      "$root/systemd/kraken-trading-bot-social.service.in" \
      > "$unit_dir/kraken-trading-bot-social.service"
  ln -sf "$root/systemd/kraken-trading-bot-social.timer" \
      "$unit_dir/kraken-trading-bot-social.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now kraken-trading-bot-social.timer
  echo "enabled. Next fire:"
  systemctl --user list-timers kraken-trading-bot-social.timer --no-pager

# Enable the hourly ORDER-BOOK DEPTH recorder systemd.user timer — the G1
# recorder (.data-audit/PLAN.md §8.1).
#
# THIS IS THE ONLY RECIPE IN THE REPO WHOSE MISSING COST IS IRREVERSIBLE.
# Kraken's order book is a live snapshot with NO historical endpoint, so an
# hour this timer does not fire is an hour nothing can ever recover.  If
# this recipe is the difference between a recorder that exists and one that
# accumulates, run it.
#
# Fires at :41 — a fourth minute, clear of funding :17, news :23 and social
# :29 so the four hourly timers do not land in the same window.
#
# The unit is GENERATED, not symlinked, because ExecStart embeds the repo
# root, the pair, the output path and the requested depth.  `root` is
# `justfile_directory()`, so the timer's `nix run <root>#kraken-trading-bot`
# builds from THIS working tree (src = ./.., no flake-lock entry) and the
# timer runs the committed tree rather than a pinned revision of it.
#
# Re-run it after moving or re-cloning the repo: ExecStart holds an absolute
# path, so a stale root is a unit that looks enabled and accumulates nothing.
depth-timer pair="ETH/USD" output="signals/eth_usd_orderbook.jsonl" count="100":
  #!/usr/bin/env bash
  set -euo pipefail
  # ONE timer path, never two. `nix/module.nix` declares a SYSTEM unit
  # (systemd.services/systemd.timers, off by default behind `ob.enable`);
  # this recipe installs a USER unit. They are independent units and neither
  # suppresses the other, so enabling both snapshots TWICE an hour and every
  # depth series silently doubles its own cadence. Refuse rather than allow it.
  if systemctl is-enabled --quiet kraken-trading-bot-order-book.timer 2>/dev/null; then
      echo "REFUSING: the SYSTEM timer kraken-trading-bot-order-book.timer is enabled." >&2
      echo "  Two timers would each fire hourly. Pick one path:" >&2
      echo "    - keep the system timer: skip this recipe, set ob.enable = true" >&2
      echo "      in nix/module.nix and rebuild the host instead;" >&2
      echo "    - keep this user timer: disable the system unit first, e.g." >&2
      echo "        systemctl disable --now kraken-trading-bot-order-book.timer" >&2
      echo "      or set ob.enable = false, then re-run." >&2
      exit 1
  fi
  unit_dir="$HOME/.config/systemd/user"
  root="{{justfile_directory()}}"
  mkdir -p "$unit_dir" "$root/$(dirname {{output}})"
  sed -e "s|@ROOT@|$root|" -e "s|@PAIR@|{{pair}}|" \
    -e "s|@OUTPUT@|$root/{{output}}|" -e "s|@COUNT@|{{count}}|" \
    "$root/systemd/kraken-trading-bot-order-book.service.in" \
    > "$unit_dir/kraken-trading-bot-order-book.service"
  ln -sf "$root/systemd/kraken-trading-bot-order-book.timer" \
    "$unit_dir/kraken-trading-bot-order-book.timer"
  systemctl --user daemon-reload
  systemctl --user enable --now kraken-trading-bot-order-book.timer
  echo "enabled. Next fire:"
  systemctl --user list-timers kraken-trading-bot-order-book.timer --no-pager
  echo
  echo "ENABLED IS NOT RUNNING. Confirm it has actually fired:"
  echo "  systemctl --user list-timers kraken-trading-bot-order-book.timer"
  echo "  just depth-gaps"
  echo "  systemctl --user status kraken-trading-bot-order-book.service"

# Record ONE order-book depth snapshot by hand — the exact command the
# kraken-trading-bot-order-book.timer runs.  Use it to seed a fresh clone, or
# to confirm the path works before trusting the timer.
#
# Invoked through this repo's OWN flake output by path rather than
# `python cli.py`, because ExecStart invokes it that way and "it works when I
# run it by hand" is a different claim from "the timer runs the committed
# tree" — only the second accumulates data.
#
# A path reference to a git checkout only sees TRACKED files, so an
# uncommitted recorder module makes this fail with ModuleNotFoundError while
# looking otherwise healthy.  Commit first.
depth-pull pair="ETH/USD" output="signals/eth_usd_orderbook.jsonl" count="100":
  #!/usr/bin/env bash
  set -euo pipefail
  root="{{justfile_directory()}}"
  dest="{{output}}"
  case "$dest" in /*) ;; *) dest="$root/$dest" ;; esac
  mkdir -p "$(dirname "$dest")"
  nix run "$root"#kraken-trading-bot -- \
    record-depth --pair {{pair}} --output "$dest" --count {{count}}

# Re-scan the depth log for holes.  No network.  Exits 1 when any interval
# exceeded the expected cadence, so it is usable as a health gate.
#
# Run this after every reboot, after any laptop suspend, and before quoting
# the log's coverage: F-6's lesson is that a producer that stopped for a week
# and appended happily afterwards looks identical to one that never stopped
# unless somebody counts.
depth-gaps output="signals/eth_usd_orderbook.jsonl" *ARGS="":
  #!/usr/bin/env bash
  set -euo pipefail
  root="{{justfile_directory()}}"
  dest="{{output}}"
  case "$dest" in /*) ;; *) dest="$root/$dest" ;; esac
  nix run "$root"#kraken-trading-bot -- depth-gaps --output "$dest" {{ARGS}}

# Fill funding HISTORY from Kraken's own /historical-funding-rates, into the
# SAME file `funding-pull` writes (--append).  This is the difference between
# a funding channel with one record and one with ~8,800 (measured 8,792 on
# 2026-10-02; the count grows about a record a day, since the window is
# recomputed on every call): the endpoint is
# keyless, one request, and serves a rolling ~366-day HOURLY window.
#
# Why the same file and not a second one — and note the reason is NOT width.
# A history-only file does NOT narrow the observation: measured on a 721-bar
# 60m frame, a full 14-key history file with every quote field null still
# composes 60 observation columns, the same as the baseline, because
# `available_cols` intersects against the file's keys and the history file
# carries all of them (as null).  The real cost is that `spread` goes DEAD:
# 24 nonzero bars (max 3.695e-05) with the live leg present, 0 without — a
# literal constant-zero column at unchanged width, which `check_feature_width`
# cannot see.  So run BOTH legs, in this order.
#
# Recovery is "1 of 6": `funding_rate` only.  `basis`, `open_interest`,
# `funding_rate_prediction`, `vol24h` and `spread` are not in that endpoint
# and are written as JSON null, never 0.0.
#
# The ~366-day window AGES OUT (it is recomputed on every call), so run this
# periodically as a top-up.  Re-running is idempotent: an hour the file
# already holds is left alone, so it will not duplicate rows.
#
# `--since` is optional and is filtered client-side — the endpoint ignores
# range parameters.  A `--since` older than the rolling window warns and
# names both dates.
# The date range comes from the environment, NOT from a just parameter and
# NOT from an extra arg: `just` binds bare positionals to `pair` then
# `output` in declaration order, so `just funding-backfill --since X`
# silently overwrote `output` with "--since" and moved X into the passthrough.
# An env var cannot be stolen by that binding, and it is also the spelling
# `funding-pull`'s callers already have in their shell.
# `output` is repo-relative unless absolute — see the note on funding-pull.
#   just funding-backfill
#   SINCE=2026-01-01 just funding-backfill
#   SINCE=2026-01-01 UNTIL=2026-06-01 just funding-backfill XBT/USD
#   just funding-backfill ETH/USD /tmp/scratch.jsonl
funding-backfill pair="ETH/USD" output="signals/eth_usd_funding.jsonl":
  #!/usr/bin/env bash
  set -euo pipefail
  root="{{justfile_directory()}}"
  dest="{{output}}"
  case "$dest" in /*) ;; *) dest="$root/$dest" ;; esac
  mkdir -p "$(dirname "$dest")"
  args=(backfill --pair {{pair}} --output "$dest" --append)
  if [ -n "${SINCE:-}" ]; then args+=(--since "$SINCE"); fi
  if [ -n "${UNTIL:-}" ]; then args+=(--until "$UNTIL"); fi
  nix run ~/Projects/kraken-funding-rates#kraken-funding-rates -- "${args[@]}"

# ── Model evaluation matrix ─────────────────────────────────────────────
# Three stages over a YAML matrix spec: expand+warn, execute (resumable
# JSONL, one line per cell), aggregate (median + IQR, invalid cells listed
# and excluded). Start from configs/matrix.example.yaml.
# Read .opencode/skills/model-matrix/SKILL.md before trusting a number.

# Expand the design, count cells, warn when it cannot support a comparison
# e.g. just matrix-plan configs/matrix.example.yaml
matrix-plan spec="configs/matrix.example.yaml":
  {{dev}} 'python tools/model_matrix.py plan {{spec}}'

# Execute the cells. Resumable: re-running skips cells already recorded.
# Extra args pass through, e.g. --force / --limit 4 / --dry-run
matrix-run spec="configs/matrix.example.yaml" *ARGS="":
  {{dev}} 'python tools/model_matrix.py run {{spec}} {{ARGS}}'

# Aggregate into per-axis medians + IQRs, listing invalid cells
matrix-report spec="configs/matrix.example.yaml" *ARGS="":
  {{dev}} 'python tools/model_matrix.py report {{spec}} {{ARGS}}'

# ── Deep-history store (market_data_store) ───────────────────────────────
# `market_data_store: null` in configs/default.yaml is a LIVE fetch, and
# Kraken's REST API serves up to 720 of the most recent candles — older
# data cannot be retrieved regardless of `since`. A seeded local store is
# the only route past that ceiling. These four recipes seed and check it.
#
# They run the sibling `kraken-deep-history` seeder under THIS repo's dev
# shell with its checkout on PYTHONPATH, because kraken-deep-history's own
# dev shell carries only pytest (no pandas, no pyarrow). Seeding there
# makes `_open_store` fall back to `FallbackStoreWriter`, which writes
# `.csv` that `MarketDataStore.read` (which globs `*.parquet`) cannot see:
# the seed reports success, the directory looks seeded, and the bot then
# reads ~721 live bars. `store-seed` therefore pipes the report through
# `tools/store_guard.py`, which REFUSES store_mode != "market-data"
# instead of merely printing it.
#
# Cost: ~158s and ~13MB for the four mapped USDT pairs at 60-minute bars.
# Deep bars buy normalization-sample size, regime diversity and a longer
# out-of-sample span — NOT more gradient steps (--timesteps is unchanged).

# Shared locations: the sibling seeder is not a flake input here, so it is
# named by path and put on PYTHONPATH rather than built.
store_root := env_var_or_default("KTB_STORE_ROOT", "~/Projects/kraken-market-data/store")
deep_history_dir := env_var_or_default("KTB_DEEP_HISTORY_DIR", "~/Projects/kraken-deep-history")

# Dry run: which monthly Binance-archive ZIPs a seed would download. No
# network, no writes. Run this first — it is how you check the store path
# and the symbol map before spending 158s.
# e.g. just store-plan                       (ETH/USD since 2018)
#      just store-plan "BTC/USD" "2021-01-01"
store-plan ticker="ETH/USD" since="2018-01-01" interval="60" *ARGS="":
  {{dev}} 'PYTHONPATH={{deep_history_dir}}:$PYTHONPATH python -m kraken_deep_history.cli plan --ticker {{ticker}} --interval {{interval}} --from {{since}} --store {{store_root}} {{ARGS}}'

# Seed: download + upsert into the store root. Fails (non-zero) unless the
# report says store_mode == "market-data"; a fallback-csv seed is refused.
# Re-running is safe — upsert dedupes on bar time, keeping the newest.
# e.g. just store-seed                       (ETH/USD since 2018)
#      just store-seed "SOL/USD" "2021-01-01" 60 "~/Projects/kraken-market-data/store"
store-seed ticker="ETH/USD" since="2018-01-01" interval="60" store=store_root:
  #!/usr/bin/env bash
  set -euo pipefail
  root="{{justfile_directory()}}"
  report="$(mktemp -t ktb-seed-report.XXXXXX.json)"
  trap 'rm -f "$report"' EXIT
  # The redirect MUST live inside the `bash -c` string.  `{{dev}}` is
  # `nix develop --command bash -c ...`, and this dev shell prints a
  # six-line banner ("kraken-trading-bot dev shell", the store path, the
  # version and the two dep lines) to STDOUT on every entry (flake.nix:70,
  # :74).  A `> "$report"` outside the quotes would therefore capture the
  # banner *plus* the seeder's JSON, and store_guard would die with
  # "cannot read the seed report: Expecting value: line 1 column 1" — the
  # seed had in fact succeeded.  Redirecting inside the inner shell keeps
  # only the seeder's own stdout, which is the report (cli.py:151); the
  # seeder's progress and warnings go to stderr (logging_config.py:153).
  {{dev}} 'PYTHONPATH={{deep_history_dir}}:$PYTHONPATH python -m kraken_deep_history.cli seed --ticker {{ticker}} --interval {{interval}} --from {{since}} --store {{store}} > '"$report"
  {{dev}} 'python '"$root"'/tools/store_guard.py '"$report"
  echo "seeded {{store}}. Check it with: just store-stats && just store-verify"

# Per-source summary of what is in the store: bar counts and the span of
# each (pair, interval). Read the spans before pinning data_window — a
# `since` older than a source's start clips to empty.
store-stats *ARGS="":
  {{dev}} 'PYTHONPATH={{deep_history_dir}}:$PYTHONPATH python -m kraken_deep_history.cli stats --store {{store_root}} {{ARGS}}'

# Gap-scan one source for missing bars. Exits non-zero when the window is
# not contiguous, which is the check to run before trusting a pinned
# window: a hole inside the training slice is a silent bias.
#
# Both tools get the SAME ARGS. The sibling seeder owns the contiguity
# verdict and its exit code; `tools/store_gap_scan.py` (Phase 6 finding
# F6) then counts the missing bars, lists the gaps, and **names any gap at
# the seed/live-append seam** -- the month-file boundary where the archive
# seed ends and this repo's live append leg takes over, which is the region
# a live deployment actually trades. It detects and labels only; the fix is
# CAND-3b.
#
# `set -e` is deliberately NOT used: the seeder's verify exits non-zero
# precisely when the series has gaps, which is exactly when the gap report
# matters most. Both commands must run; the seeder's status is what this
# recipe returns.
# e.g. just store-verify --ticker ETH/USD --interval 60
#      just store-verify --ticker ETH/USD --since 2020-01-01 --until 2026-01-01
store-verify *ARGS="":
  #!/usr/bin/env bash
  set -uo pipefail
  root="{{justfile_directory()}}"
  {{dev}} 'PYTHONPATH={{deep_history_dir}}:$PYTHONPATH python -m kraken_deep_history.cli verify --store {{store_root}} {{ARGS}}'
  status=$?
  {{dev}} 'python '"$root"'/tools/store_gap_scan.py --store {{store_root}} {{ARGS}}' || true
  exit "$status"

# ── Tests / checks ───────────────────────────────────────────────────────

# Run the full pytest suite
test:
  {{dev}} 'pytest tests/ -q'

# Run one test file
# e.g. just test-one test_rl_training.py
test-one file="test_rl_training.py":
  {{dev}} 'pytest tests/{{file}} -q'

# Evaluate the flake (packages, module, dev shell). No dev shell needed.
check:
  nix flake check

# Show the flake outputs. No dev shell needed.
show:
  nix flake show

# ── Audit checks (read-only; each prints a compact receipt) ────────────────
# Added after the 2026-10-02 pass. These exist because that pass re-derived its
# four structural checks by hand ~12 times, and because its width-hash assertion
# depended on a script in /tmp that was not in the repo -- so nobody could
# reproduce the gate's central invariant. Nothing here commits.
#
# Args are passed through verbatim (same convention as `train *CLI_ARGS`), so
# the Python argparse surface is the single source of truth for flags.

# The four structural gate checks, one receipt.
#   --prereg <c>  estimator+threshold must be BYTE-IDENTICAL to this commit
#   --since  <c>  executable AST must be unchanged since this commit
# These are usually DIFFERENT commits: pointing --since at the pre-registration
# commit reports CHANGED for files a later review round legitimately rewrote,
# which is indistinguishable from a real regression.
# e.g. just audit-verify --prereg 97a2a52 --since 91c76a9 \
#         --frame /tmp/krb-verify/live721.parquet --expect-obs 93edc733
audit-verify *args:
  {{dev}} '.venv/bin/python tools/audit_checks.py verify {{args}}'

# Finding <-> disposition parity between VALIDATION.md and DECISION.md.
audit-findings *args:
  {{dev}} '.venv/bin/python tools/audit_checks.py findings {{args}}'

# Flag evidence in .data-audit/ a fresh clone cannot re-derive, and check a
# falsification record exists. Read-only.
audit-evidence *args:
  {{dev}} '.venv/bin/python tools/audit_checks.py evidence {{args}}'

# Pushed-state check plus the ordered close-out steps.
audit-closeout *args:
  {{dev}} '.venv/bin/python tools/audit_checks.py closeout {{args}}'

# Observation-matrix fingerprint for a cached frame. Prints the FILE hash and
# the ARRAY hash separately: they are DIFFERENT values for the same matrix and
# conflating them looks like a regression.
# e.g. just width-check --frame /tmp/krb-verify/live721.parquet --expect-obs 93edc733
width-check *args:
  {{dev}} '.venv/bin/python tools/width_check.py {{args}}'

# Re-derive the cost-aware CAND-5 figures from per-seed records, using the
# estimator and threshold IMPORTED from tools/model_matrix.py. Records are run
# artifacts and stay out of git. Annotate every cell id:seed -- these records
# all carry the same `seed` field, so pairing cannot be verified without it.
# e.g. just cost-aware-replay --records /tmp/r/cost --control-records /tmp/r/ctrl \
#         --cells live=a:42,b:43,c:44 --cells store=d:42,e:43,f:44
cost-aware-replay *args:
  {{dev}} '.venv/bin/python tools/cost_aware_gate.py {{args}}'

# Clean generated artifacts (model configs stay tracked; bin+caches removed)
clean:
  rm -rf models/__pycache__ models/*/__pycache__
  rm -rf *.egg-info kraken_trading_bot.egg-info
  find . -name __pycache__ -type d -prune -exec rm -rf {} +
  rm -rf .pytest_cache
  @echo "Cleaned caches. Trained model zips/npz (gitignored) left in place."