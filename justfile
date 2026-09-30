# justfile — standard commands for kraken-trading-bot
#
#   just --list             show all recipes
#   just train              train ppo_eth_01 on ETH/USD with defaults
#   just train --ticker XRP_USD --model ppo_xrp_02 --timesteps 100000
#   just bench SOL_USD ppo_sol_01 50000   train + backtest + show performance
#   just export-data --ticker SOL_USD      dump the data pipeline frame to CSV
#   just paper --ticker ETH_USD --model ppo_eth_01 --iterations 10
#   just test               run the pytest suite
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
# z_-prefixed columns for inspection; the agent itself reads the raw ones.
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