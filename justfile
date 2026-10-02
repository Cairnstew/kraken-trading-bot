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
#   just funding-timer      enable the hourly systemd.user funding timer
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
funding-pull pair="ETH/USD" output="signals/eth_usd_funding.jsonl":
  #!/usr/bin/env bash
  set -euo pipefail
  root="{{justfile_directory()}}"
  mkdir -p "$root/$(dirname {{output}})"
  nix run ~/Projects/kraken-funding-rates#kraken-funding-rates -- \
    pull --pair {{pair}} --output "$root/{{output}}" --append

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
# window: a hole inside the training slice is a silent bias. Everything is
# passed straight to the sibling CLI (which requires --ticker).
# e.g. just store-verify --ticker ETH/USD --interval 60
#      just store-verify --ticker ETH/USD --since 2020-01-01 --until 2026-01-01
store-verify *ARGS="":
  {{dev}} 'PYTHONPATH={{deep_history_dir}}:$PYTHONPATH python -m kraken_deep_history.cli verify --store {{store_root}} {{ARGS}}'

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

# Clean generated artifacts (model configs stay tracked; bin+caches removed)
clean:
  rm -rf models/__pycache__ models/*/__pycache__
  rm -rf *.egg-info kraken_trading_bot.egg-info
  find . -name __pycache__ -type d -prune -exec rm -rf {} +
  rm -rf .pytest_cache
  @echo "Cleaned caches. Trained model zips/npz (gitignored) left in place."