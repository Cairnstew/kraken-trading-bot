# justfile — standard commands for kraken-trading-bot
#
#   just --list            show all recipes
#   just train             train ppo_eth_01 on ETH/USD (defaults)
#   just backtest          evaluate a trained model
#   just paper             live paper trade with a trained model
#   just test              run the pytest suite
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

# Train a PPO model for one ticker
train ticker="ETH_USD" model="ppo_eth_01" timesteps="20000" action_space="continuous":
  {{cli}} train --ticker {{ticker}} --model {{model}} --timesteps {{timesteps}} --action-space {{action_space}}

# Backtest a trained model on fresh OHLC data
backtest ticker="ETH_USD" model="ppo_eth_01":
  {{cli}} backtest --ticker {{ticker}} --model {{model}}

# List models registered in the registry (all, or one ticker)
models ticker="":
  @if [ -z "{{ticker}}" ]; then \
    {{cli}} models; \
  else \
    {{cli}} models --ticker {{ticker}}; \
  fi

# Paper trade with a trained model (simulated orders, no real money)
paper ticker="ETH_USD" model="ppo_eth_01" iterations="10" interval="60":
  {{cli}} paper-trade --ticker {{ticker}} --model {{model}} --iterations {{iterations}} --interval {{interval}}

# Dry-run paper trading (print would-be orders, execute nothing)
paper-dry ticker="ETH_USD" model="ppo_eth_01" iterations="5" interval="60":
  {{cli}} paper-trade --ticker {{ticker}} --model {{model}} --iterations {{iterations}} --interval {{interval}} --dry-run

# ── Account / market (needs KRAKEN_API_KEY/SECRET for private calls) ──────

# Show current ticker for a pair
ticker pair="ETH/USD":
  {{cli}} ticker --pair {{pair}}

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
test-one file="test_rl_training.py":
  nix develop -c pytest tests/{{file}} -q

# Evaluate the flake (packages, module, dev shell)
check:
  nix flake check

# Show the flake outputs
show:
  nix flake show

# Clean generated artifacts (models, caches). Keeps the venv.
clean:
  rm -rf models/__pycache__ models/*/__pycache__
  rm -rf *.egg-info kraken_trading_bot.egg-info
  find . -name __pycache__ -type d -prune -exec rm -rf {} +
  rm -rf .pytest_cache
  @echo "Cleaned caches. Model artifacts (models/**/model.zip etc.) are gitignored."