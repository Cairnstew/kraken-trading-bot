# justfile — standard commands for kraken-trading-bot
#
#   just --list             show all recipes
#   just train              train ppo_eth_01 on ETH/USD with defaults
#   just train --ticker XRP_USD --model ppo_xrp_02 --timesteps 100000
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

# ── RL workflow ────────────────────────────────────────────────────────

# Train a PPO model. Args pass through to `kraken-trading-bot train`
# e.g. just train --ticker XRP_USD --model ppo_xrp_02 --timesteps 100000
train *CLI_ARGS="":
  {{dev}} 'kraken-trading-bot train {{CLI_ARGS}}'

# Backtest a trained model on fresh OHLC data
# e.g. just backtest --ticker ETH_USD --model ppo_eth_01
backtest *CLI_ARGS="":
  {{dev}} 'kraken-trading-bot backtest {{CLI_ARGS}}'

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