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

`normalization.npz` is not a passive artifact: it holds the per-ticker
mean/std the policy was **trained** on, and every consumer applies it —
the training environment, `backtest`, `paper-trade`, and the
`export-data --normalized` block. The agent therefore observes
`compute` → `ffill` → `fillna(0)` → `(x - mean) / std`, not raw
heteroscaled features. Editing `feature_windows`/`feature_groups` (or
adding a signal column) changes that vector, so retrain rather than
reuse a model across feature configs.

```bash
# Train a new PPO model for ETH/USD (fetches public OHLC; no credentials needed)
kraken-trading-bot train --ticker ETH_USD --model ppo_eth_01

# Customise the run: config file, data pages, training budget, execution costs
kraken-trading-bot train --ticker ETH_USD --model ppo_eth_01 \
  --config configs/default.yaml --pages 8 --timesteps 20000 \
  --action-space discrete --fee-rate 0.0026 --slippage 0.0005

# Backtest a trained model on fresh OHLC data
kraken-trading-bot backtest --ticker ETH_USD --model ppo_eth_01

# Evaluate under a config: real execution costs, a pinned data window, and
# the out-of-sample tail of it. Without --config the replay is frictionless
# whenever the model was trained frictionless — the result echoes back the
# fee_rate/slippage it actually applied, so that is visible rather than
# assumed.
kraken-trading-bot backtest --ticker ETH_USD --model ppo_eth_01 \
  --config configs/default.yaml --json

# Dump the data-pipeline stages for inspection (no model needed)
kraken-trading-bot export-data --ticker ETH_USD --output exports/ETH_USD.csv

# Add the z_-prefixed block: the agent's z-scored observation, column for
# column what the policy is fed. The unprefixed feature columns stay
# pre-transform, so the CSV shows both sides of the scaling.
kraken-trading-bot export-data --ticker ETH_USD --normalized

# List registered models (readable table, or JSON with --json; filter by ticker)
kraken-trading-bot models
kraken-trading-bot models --ticker ETH_USD --json

# Paper-trade a trained model (no real orders)
kraken-trading-bot paper-trade --ticker ETH_USD --model ppo_eth_01 --iterations 100
```

### Model evaluation matrix

A single backtest run is not a measurement. PPO on an **identical config**
has been measured replaying **576 trades in one run and 374 in another**,
and `train`/`backtest` each fetch fresh data — two fetches have disagreed
**16% on a fitted feature std**, so cells that look identical may not be.

`tools/model_matrix.py` runs a YAML matrix spec (tickers × seeds × friction
× …) and refuses to report comparisons it cannot support:

```bash
just matrix-plan    configs/matrix.example.yaml   # expand cells, WARN on validity gaps
just matrix-run     configs/matrix.example.yaml   # execute, resumable JSONL (one line per cell)
just matrix-report  configs/matrix.example.yaml   # median + IQR per axis, invalid cells listed
```

Start from `configs/matrix.example.yaml`, whose comments explain what
question each axis answers. Three behaviours worth knowing:

- **Multi-seed cells are reduced by median, never averaged.** The report
  prints `median [q1, q3]` so the spread stays visible.
- **Degenerate cells are INVALID, not zero.** A run can exit 0 having
  replayed **1 bar of 721 with 0 trades** — the width guard passes and it
  looks like a "0% return" result. Such cells are listed with their reason
  and excluded from every aggregate.
- **`plan` blocks what cannot be compared**: no pinned `data_window`
  (cells replayed different bars), fewer than 3 seeds, or an entirely
  frictionless matrix (**a frictionless Sharpe is not a Sharpe**).

The headline metric is `excess_return` — the return over `buy_hold_return`
— because a raw return has no reference point. Scratch models and results
go under `/tmp`; nothing under `models/` is ever written by the harness.

Full checklist, axis-selection guide, and current limits (PPO only, no
walk-forward re-fitting): `.opencode/skills/model-matrix/SKILL.md`.

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

### Observation width and the funding signal file

The observation width with the default `feature_windows: [1, 4, 24]` and
all five `feature_groups` is **dynamic**, because every builder is
presence-gated — a source that is absent contributes nothing instead of
failing the pipeline:

| Configuration | Width |
|---|---|
| Neither the OHLCV `vwap`/`count` columns nor a funding file | **49** (unchanged) |
| OHLCV `vwap`/`count` present, no funding file | **52** |
| Funding file present (the shipped `configs/default.yaml`) | **60** |

The jump from 49 to 60 is not all new work: it is six columns derived
from data the pipeline already carried but that no builder read —

| Column | Derived from |
|---|---|
| `vwap_dev` | the bar's own `vwap` (`close/vwap - 1`) |
| `trade_count_zscore_20` | the bar's own `count` (20-bar rolling z-score) |
| `volume_per_trade` | `volume / count` |
| `funding_rate_prediction` | the funding JSONL's forward funding estimate |
| `vol24h` | the funding JSONL's rolling 24h perp volume |
| `spread` | the funding JSONL's `bid`/`ask` |

The first three need nothing but the OHLCV frame, so they are live
immediately. The last three need a funding file, which
`configs/default.yaml` now points at:

```bash
# Fill HISTORY first (keyless; Kraken's own ~366-day hourly series), then
# the live snapshot.  Both append into the same file, and the order matters:
# backfill alone leaves `spread` a literal all-zero column.
just funding-backfill && just funding-pull

# ...or drive the same command by hand:
nix run ~/Projects/kraken-funding-rates#kraken-funding-rates -- \
  backfill --pair ETH/USD \
  --output ~/Projects/kraken-trading-bot/signals/eth_usd_funding.jsonl --append
```

`just funding-backfill` recovers **1 of 6** signal columns — `funding_rate`,
from ~8,800 hourly records instead of one. `basis`, `open_interest`,
`funding_rate_prediction`, `vol24h` and `spread` are not in that endpoint
and stay zero-fill. The window is a rolling ~366-day cap that ages out, so
re-run it periodically as a top-up (`SINCE=`/`UNTIL=` narrow it).

Keep it fresh with the shipped user timer:

```bash
just funding-timer   # links + enables systemd/kraken-trading-bot-funding.{service,timer}
```

`signal_max_age_hours: 12` is a deliberate **upper bound** on funding's
hourly settlement, sized to bridge more than one missed pull (measured gap
histogram over Kraken's own history: `{1.0h: 8783, 2.0h: 6, 3.0h: 1}`) — not
a statement that records arrive 12 hours apart. How much it matters depends
entirely on how deep the funding file is:

| funding file | `signal_observed` at 1h | at 12h |
|---|---|---|
| one live snapshot (no history) | **2 / 721** bars | **13 / 721** bars |
| backfilled with Kraken's hourly history | **721 / 721** bars | **721 / 721** bars |

So on a backfilled file the bound cannot change the observation at all —
every bar carries its own record. It still earns its keep on a shallow file
and for any pre-existing model that relies on the carry across missed pulls.

**The width is guarded.** `train` records `n_features` in each model's
`config.yaml` (`kraken-trading-bot models --json` surfaces it), and
`backtest`/`paper-trade` compare the model's fitted `feature_names`
against the live pipeline's columns and refuse to run on a mismatch. A
model trained before this widening has no `n_features` and no derived
columns, so it will not silently run column-less — retrain it.

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
