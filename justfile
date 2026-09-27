# justfile — standard commands for kraken-trading-bot
#
#   just --list             show all recipes
#   just train              train ppo_eth_01 on ETH/USD with defaults
#   just train --ticker XRP_USD --model ppo_xrp_02 --timesteps 100000
#   just paper --ticker ETH_USD --model ppo_eth_01 --iterations 10
#   just test               run the pytest suite
#
# The RL recipes take **any** additional arguments and pass them straight
# to the `kraken-trading-bot` CLI (all flags are optional; omitted ones
# fall back to the CLI's own defaults). The `paper` subcommand maps the
# `paper-trade` subcommand, so `just paper --dry-run ...` works too.
#
# The CLI is invoked through whichever environment is available:
#   - .venv/bin/kraken-trading-bot  (pip install -e ., see setup-venv)
#   - `nix develop -c kraken-trading-bot`  (flake dev shell, fallback)
cli := `if [ -x .venv/bin/kraken-trading-bot ]; then echo ./.venv/bin/kraken-trading-bot; else echo "nix develop -c kraken-trading-bot"; fi`

# ── Environment ────────────────────────────────────────────────────────

# Print this help
default:
  @just --list

# Drop into the Nix dev shell (full RL stack)
shell:
  nix develop

# Create ./venv with the local kraken-python fork + this package
setup-venv:
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -e /home/seanc/Projects/kraken-python
  ./.venv/bin/pip install -e .

# ── RL workflow ────────────────────────────────────────────────────────

# Train a PPO model. Args pass through to `kraken-trading-bot train`
# e.g. just train --ticker XRP_USD --model ppo_xrp_02 --timesteps 100000
train *CLI_ARGS="":
  {{cli}} train {{CLI_ARGS}}

# Backtest a trained model on fresh OHLC data
# e.g. just backtest --ticker ETH_USD --model ppo_eth_01
backtest *CLI_ARGS="":
  {{cli}} backtest {{CLI_ARGS}}

# List models. Pass --ticker T or --json to filter/format
# e.g. just models --ticker ETH_USD — just models --json — just models
models *CLI_ARGS="":
  {{cli}} models {{CLI_ARGS}}

# Paper trade a trained model (simulated orders). Map to paper-trade
# e.g. just paper --ticker ETH_USD --model ppo_eth_01 --iterations 10
#      just paper --ticker ETH_USD --model ppo_eth_01 --dry-run
paper *CLI_ARGS="":
  {{cli}} paper-trade {{CLI_ARGS}}

# Dry-run paper trading (print would-be orders, execute nothing)
# e.g. just paper-dry --ticker ETH_USD --model ppo_eth_01
paper-dry *CLI_ARGS="":
  {{cli}} paper-trade {{CLI_ARGS}} --dry-run

# ── Account / market (needs KRAKEN_API_KEY/SECRET for private calls) ──────

# Show current ticker for a pair
# e.g. just ticker --pair ETH/USD
ticker *CLI_ARGS="--pair ETH/USD":
  {{cli}} ticker {{CLI_ARGS}}

# Show account balances (requires credentials)
balance:
  {{cli}} balance

# Show open orders (requires credentials)
orders:
  {{cli}} orders

# ── Tests / checks ───────────────────────────────────────────────────────

# Run the full pytest suite
test:
  nix develop -c pytest tests/ -q

# Run one test file
# e.g. just test-one test_rl_training.py
test-one file="test_rl_training.py":
  nix develop -c pytest tests/{{file}} -q

# Evaluate the flake (packages, module, dev shell)
check:
  nix flake check

# Show the flake outputs
show:
  nix flake show

# Clean generated artifacts (model configs stay tracked; bin+caches removed)
clean:
  rm -rf models/__pycache__ models/*/__pycache__
  rm -rf *.egg-info kraken_trading_bot.egg-info
  find . -name __pycache__ -type d -prune -exec rm -rf {} +
  rm -rf .pytest_cache
  @echo "Cleaned caches. Trained model zips/npz (gitignored) left in place."