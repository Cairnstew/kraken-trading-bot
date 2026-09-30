# kraken-trading-bot

A trading bot framework using the [kraken-python](https://github.com/Cairnstew/kraken-python) API wrapper.

## Features

- **Strategy Framework**: Pluggable abstract base class for custom trading strategies
- **SMA Crossover Strategy**: Included trend-following strategy with golden/death cross detection
- **Trading Engine**: Orchestrates strategy execution with paper mode support
- **Nix Integration**: Flake packaging, NixOS module with credential management
- **CLI**: Commands for `balance`, `ticker`, `orders`, `run`, and the RL
  pipeline (`train`, `backtest`, `models`, `paper-trade`)

## Installation

### Using Nix (Recommended)

```bash
# Build the package
nix build .#kraken-trading-bot

# Or use the dev shell
nix develop
```

### Using pip

```bash
pip install -e .
```

## Configuration

Set your Kraken API credentials (or use the kraken-python .env file):

```bash
cp .env.example .env
# Edit .env with your API key and secret
```

Or export environment variables:

```bash
export KRAKEN_API_KEY="your_key_here"
export KRAKEN_API_SECRET="your_secret_here"
```

## Usage

### CLI Commands

```bash
# Show account balance
kraken-trading-bot balance

# Show current ticker
kraken-trading-bot ticker --pair XBT/USD

# Show open orders
kraken-trading-bot orders

# Run the trading bot (paper mode)
kraken-trading-bot run --paper

# Run with custom settings
kraken-trading-bot run --pair XBT/USD --pair ETH/USD --short-period 10 --long-period 30 --interval 30
```

### RL CLI

Train, evaluate, and list reinforcement-learning models. The RL pipeline
trains a PPO agent per ticker; artifacts live under
`models/{TICKER_ID}/{model_name}/` (`model.zip`, `normalization.npz`,
`config.yaml`). Tickers use underscore notation (`ETH_USD`).

```bash
# Train a new PPO model for ETH/USD (fetches public OHLC; no credentials needed)
kraken-trading-bot train --ticker ETH_USD --model ppo_eth_01

# Customise the run: config file, data pages, training budget, execution costs
kraken-trading-bot train --ticker ETH_USD --model ppo_eth_01 \
  --config configs/default.yaml --pages 8 --timesteps 20000 \
  --action-space discrete --fee-rate 0.0026 --slippage 0.0005

# Backtest a trained model on fresh OHLC data
kraken-trading-bot backtest --ticker ETH_USD --model ppo_eth_01

# List registered models (readable table, or JSON with --json; filter by ticker)
kraken-trading-bot models
kraken-trading-bot models --ticker ETH_USD --json

# Paper-trade a trained model (no real orders)
kraken-trading-bot paper-trade --ticker ETH_USD --model ppo_eth_01 --iterations 100

# Dump the data pipeline's frame to CSV (same stages `train` runs)
kraken-trading-bot export-data --ticker ETH_USD
```

### Observations

The policy is conditioned on the pipeline's `compute()` output,
forward-filled then zero-filled and z-scored per feature with the mean/std
moments in `normalization.npz` — the same map at train, backtest and paper
time, so all three share one scale. `prepare_episode` fits those moments on
the episode window only (never on a held-out tail), and `export-data`
renders both sides: the `features` block is the pre-transform frame the
moments were fitted on, and `--normalized` adds the `z_` block, which is
exactly the vector the agent sees. Changing the feature pipeline or its
window therefore requires a retrain — `normalization.npz` and `model.zip`
travel together under `models/{TICKER_ID}/{model_name}/`.

### Python API

```python
from kraken_api import KrakenManager
from kraken_trading_bot.strategies import SMAcrossoverStrategy
from kraken_trading_bot.engine import TradingEngine

# Create manager (uses kraken-python)
manager = KrakenManager.from_env()

# Create strategy
strategy = SMAcrossoverStrategy(
    pair="XBT/USD",
    short_period=10,
    long_period=30,
)

# Create and run engine
engine = TradingEngine(
    manager=manager,
    strategies=[strategy],
    pairs=["XBT/USD"],
    paper_mode=True,  # Set to False for live trading
)
engine.run()
```

## NixOS Module

Import in your NixOS configuration:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    kraken-trading-bot.url = "github:Cairnstew/kraken-trading-bot";
  };

  outputs = { self, nixpkgs, kraken-trading-bot, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        kraken-trading-bot.nixosModules.default
        {
          services.kraken-trading-bot = {
            enable = true;
            credentials.apiKeyFile = "/run/secrets/kraken_api_key";
            credentials.apiSecretFile = "/run/secrets/kraken_api_secret";
          };
        }
      ];
    };
  };
}
```

## Strategies

### SMA Crossover

The included SMA crossover strategy generates:
- **Buy signals** when the short-term SMA crosses above the long-term SMA (Golden Cross)
- **Sell signals** when the short-term SMA crosses below the long-term SMA (Death Cross)

```python
from kraken_trading_bot.strategies import SMAcrossoverStrategy
from decimal import Decimal

strategy = SMAcrossoverStrategy(
    pair="XBT/USD",
    short_period=10,      # 10-period SMA
    long_period=30,       # 30-period SMA
    volume_per_trade=Decimal("0.01"),
    use_limit_orders=True,
)
```

### Custom Strategies

Create your own strategy by inheriting from `Strategy`:

```python
from kraken_trading_bot.strategies.base import Strategy, Signal
from decimal import Decimal

class MyStrategy(Strategy):
    def __init__(self):
        super().__init__(name="my_strategy")
        self.pair = "XBT/USD"

    def tick(self, data: dict) -> Signal:
        # Your trading logic here
        ticker = data.get("ticker")
        if ticker and ticker.last_price < Decimal("50000"):
            return Signal(
                action="buy",
                pair=self.pair,
                volume=Decimal("0.01"),
                reason="Price below 50k",
            )
        return Signal(action="hold", pair=self.pair)
```

## Development

```bash
# Enter dev shell
nix develop

# Run tests
pytest

# Run with verbose output
pytest -v
```

## License

MIT
