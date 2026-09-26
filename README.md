# kraken-trading-bot

A trading bot framework for the Kraken Spot REST + WebSocket v2 APIs.

## Features

- **Kraken API Client**: Typed Python wrapper for Kraken's REST API
- **Trading Strategies**: Pluggable strategy framework (SMA crossover included)
- **Trading Engine**: Orchestrates strategy execution and order management
- **Paper Trading**: Simulate trades without real money
- **Nix Integration**: Flake-based packaging, NixOS module, and dev shell

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

Set your Kraken API credentials:

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

### Python API

```python
from kraken_trading_bot import KrakenClient
from kraken_trading_bot.strategies import SMAcrossoverStrategy
from kraken_trading_bot.engine import TradingEngine

# Create client
client = KrakenClient.from_env()

# Create strategy
strategy = SMAcrossoverStrategy(
    pair="XBT/USD",
    short_period=10,
    long_period=30,
)

# Create and run engine
engine = TradingEngine(
    client=client,
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
        if ticker and ticker.last < Decimal("50000"):
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
