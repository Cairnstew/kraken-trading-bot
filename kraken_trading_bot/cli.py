"""Command-line interface for the Kraken Trading Bot."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from . import __version__, setup_logging
from .engine import TradingEngine
from .strategies.sma import SMAcrossoverStrategy

_LOGGER = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="kraken-trading-bot",
        description="A trading bot for the Kraken cryptocurrency exchange.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging.",
    )

    sub = parser.add_subparsers(dest="command", help="Command to run")

    # ── run command ────────────────────────────────────────────────────────
    run_parser = sub.add_parser("run", help="Start the trading bot")
    run_parser.add_argument(
        "--pair",
        action="append",
        default=["XBT/USD"],
        help="Trading pair(s) to monitor (can be specified multiple times).",
    )
    run_parser.add_argument(
        "--strategy",
        choices=["sma"],
        default="sma",
        help="Trading strategy to use (default: sma).",
    )
    run_parser.add_argument(
        "--short-period",
        type=int,
        default=10,
        help="Short SMA period (default: 10).",
    )
    run_parser.add_argument(
        "--long-period",
        type=int,
        default=30,
        help="Long SMA period (default: 30).",
    )
    run_parser.add_argument(
        "--volume",
        type=str,
        default="0.01",
        help="Volume per trade (default: 0.01).",
    )
    run_parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds between trading loop iterations (default: 60).",
    )
    run_parser.add_argument(
        "--paper",
        action="store_true",
        help="Enable paper trading mode (no real orders).",
    )

    # ── balance command ────────────────────────────────────────────────────
    sub.add_parser("balance", help="Show account balances")

    # ── ticker command ─────────────────────────────────────────────────────
    ticker_parser = sub.add_parser("ticker", help="Show current ticker")
    ticker_parser.add_argument(
        "--pair",
        action="append",
        default=["XBT/USD"],
        help="Trading pair(s) to show (can be specified multiple times).",
    )

    # ── orders command ─────────────────────────────────────────────────────
    sub.add_parser("orders", help="Show open orders")

    # ── paper-trade command ────────────────────────────────────────────────
    paper_parser = sub.add_parser(
        "paper-trade",
        help="Run a trained RL model in live paper-trade mode (no real orders).",
    )
    paper_parser.add_argument(
        "--ticker",
        required=True,
        help="Ticker the model was trained for, e.g. ETH_USD.",
    )
    paper_parser.add_argument(
        "--model",
        required=True,
        help="Model name under the ticker, e.g. ppo_eth_01.",
    )
    paper_parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between ticks (default: 60).",
    )
    paper_parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="Run this many ticks then stop (default: run until Ctrl-C).",
    )
    paper_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print would-be orders without executing on the paper manager.",
    )
    paper_parser.add_argument(
        "--models-root",
        default="models",
        help="Model registry root (default: models).",
    )

    # ── train command ─────────────────────────────────────────────────────
    train_parser = sub.add_parser(
        "train",
        help="Train a PPO RL model for one ticker and register it.",
    )
    train_parser.add_argument(
        "--ticker",
        required=True,
        help="Ticker the model will trade, e.g. ETH_USD.",
    )
    train_parser.add_argument(
        "--model",
        required=True,
        help="Model name under the ticker, e.g. ppo_eth_01.",
    )
    train_parser.add_argument(
        "--config",
        default=None,
        help="Path to a YAML training config (default: configs/default.yaml).",
    )
    train_parser.add_argument(
        "--pages",
        type=int,
        default=6,
        help="OHLC pages to fetch, each ~720 candles (default: 6).",
    )
    train_parser.add_argument(
        "--episode-bars",
        type=int,
        default=None,
        help="Cap the training episode on the trailing N bars (default: all).",
    )
    train_parser.add_argument(
        "--timesteps",
        type=int,
        default=10_000,
        help="PPO training budget (default: 10000).",
    )
    train_parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Training seed for reproducibility (default: 42).",
    )
    train_parser.add_argument(
        "--interval-minutes",
        type=int,
        default=None,
        help="OHLCV bar interval in minutes, overrides config (default: config).",
    )
    train_parser.add_argument(
        "--action-space",
        choices=["continuous", "discrete"],
        default=None,
        help="Action space, overrides config (default: config).",
    )
    train_parser.add_argument(
        "--initial-balance",
        type=float,
        default=None,
        help="Starting quote balance, overrides config (default: config).",
    )
    train_parser.add_argument(
        "--fee-rate",
        type=float,
        default=None,
        help="Fractional fee per executed order, overrides config (default: config).",
    )
    train_parser.add_argument(
        "--slippage",
        type=float,
        default=None,
        help="Fractional adverse price move on fills, overrides config (default: config).",
    )
    train_parser.add_argument(
        "--models-root",
        default="models",
        help="Model registry root (default: models).",
    )

    # ── backtest command ──────────────────────────────────────────────────
    backtest_parser = sub.add_parser(
        "backtest",
        help="Replay a trained RL model on fresh OHLC data and print metrics.",
    )
    backtest_parser.add_argument(
        "--ticker",
        required=True,
        help="Ticker the model was trained for, e.g. ETH_USD.",
    )
    backtest_parser.add_argument(
        "--model",
        required=True,
        help="Model name under the ticker, e.g. ppo_eth_01.",
    )
    backtest_parser.add_argument(
        "--pages",
        type=int,
        default=6,
        help="OHLC pages to fetch, each ~720 candles (default: 6).",
    )
    backtest_parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Seed for the deterministic replay (default: 42).",
    )
    backtest_parser.add_argument(
        "--models-root",
        default="models",
        help="Model registry root (default: models).",
    )

    # ── models command ────────────────────────────────────────────────────
    models_parser = sub.add_parser(
        "models",
        help="List registered RL models grouped by ticker.",
    )
    models_parser.add_argument(
        "--ticker",
        default=None,
        help="Only show models for this ticker, e.g. ETH_USD.",
    )
    models_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a table.",
    )
    models_parser.add_argument(
        "--models-root",
        default="models",
        help="Model registry root (default: models).",
    )

    return parser


def cmd_run(args: argparse.Namespace) -> int:
    """Run the trading bot."""
    from decimal import Decimal

    from kraken_api import KrakenManager

    # Create manager (paper or live)
    if args.paper:
        manager = KrakenManager.paper()
    else:
        manager = KrakenManager.from_env()

    # Create strategy
    strategy = SMAcrossoverStrategy(
        pair=args.pair[0],
        short_period=args.short_period,
        long_period=args.long_period,
        volume_per_trade=Decimal(args.volume),
    )

    # Create and run engine
    engine = TradingEngine(
        manager=manager,
        strategies=[strategy],
        pairs=args.pair,
        interval=args.interval,
        paper_mode=args.paper,
    )

    engine.run()
    return 0


def cmd_balance(args: argparse.Namespace) -> int:
    """Show account balances."""
    from kraken_api import KrakenManager

    manager = KrakenManager.from_env()

    try:
        trade_balance = manager.trade_balance()
        print(f"Equity: {trade_balance.equity}")
        print(f"Trade Balance: {trade_balance.trade_balance}")
        print(f"Unrealized P&L: {trade_balance.unrealized_pnl}")
        print(f"Cost Basis: {trade_balance.cost_basis}")
        print(f"Free Margin: {trade_balance.free_margin}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_ticker(args: argparse.Namespace) -> int:
    """Show current ticker."""
    from kraken_api import KrakenManager

    manager = KrakenManager.from_env()

    try:
        tickers = manager.tickers(args.pair)
        for pair, ticker in tickers.items():
            print(f"\n{pair}:")
            print(f"  Bid: {ticker.decimal('bid')}")
            print(f"  Ask: {ticker.decimal('ask')}")
            print(f"  Last: {ticker.last_price}")
            print(f"  24h Volume: {ticker.volume[1] if len(ticker.volume) > 1 else 'N/A'}")
            print(f"  24h VWAP: {ticker.vwap[1] if len(ticker.vwap) > 1 else 'N/A'}")
            print(f"  24h High: {ticker.high[1] if len(ticker.high) > 1 else 'N/A'}")
            print(f"  24h Low: {ticker.low[1] if len(ticker.low) > 1 else 'N/A'}")
            print(f"  24h Trades: {ticker.trade_count[1] if len(ticker.trade_count) > 1 else 'N/A'}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_orders(args: argparse.Namespace) -> int:
    """Show open orders."""
    from kraken_api import KrakenManager

    manager = KrakenManager.from_env()

    try:
        orders = manager.open_orders()
        if not orders:
            print("No open orders")
            return 0

        for order in orders:
            print(f"\n{order.txid}:")
            print(f"  Pair: {order.pair}")
            print(f"  Side: {order.side}")
            print(f"  Type: {order.order_type}")
            print(f"  Volume: {order.volume}")
            print(f"  Price: {order.price or 'market'}")
            print(f"  Status: {order.status}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_paper_trade(args: argparse.Namespace) -> int:
    """Run a trained RL model in live paper-trade mode (no real orders)."""
    from kraken_trading_bot.rl.paper_trade import run_paper_trader
    from kraken_trading_bot.rl.registry import scan_model

    record = scan_model(args.ticker, args.model, root=args.models_root)
    if not record.is_trained():
        print(
            f"Model {args.ticker}/{args.model} is not trained: no model.zip / "
            f"normalization.npz found under {record.root}. Train it first "
            f"(kraken-trading-bot rl-train or rl.train_ticker).",
            file=sys.stderr,
        )
        return 1

    try:
        run_paper_trader(
            args.ticker,
            args.model,
            iterations=args.iterations,
            interval=args.interval,
            dry_run=args.dry_run,
            models_root=args.models_root,
        )
    except KeyboardInterrupt:
        print("\nInterrupted.")
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    """Train a PPO RL model for one ticker and register it."""
    from kraken_trading_bot.rl.train import train_ticker

    # Only flags the user actually set become config overrides, so the
    # rest of the run keeps its YAML config (defaults to configs/default.yaml).
    overrides = {}
    if args.interval_minutes is not None:
        overrides["ohlcv_interval_minutes"] = args.interval_minutes
    if args.action_space is not None:
        overrides["action_space"] = args.action_space
    if args.initial_balance is not None:
        overrides["initial_balance"] = args.initial_balance
    if args.fee_rate is not None:
        overrides["fee_rate"] = args.fee_rate
    if args.slippage is not None:
        overrides["slippage"] = args.slippage

    try:
        record = train_ticker(
            args.ticker,
            args.model,
            config_path=args.config,
            pages=args.pages,
            episode_bars=args.episode_bars,
            total_timesteps=args.timesteps,
            seed=args.seed,
            models_root=args.models_root,
            **overrides,
        )
    except Exception as e:
        _LOGGER.error(
            "Training %s/%s failed: %s", args.ticker, args.model, e
        )
        print(f"Error training {args.ticker}/{args.model}: {e}", file=sys.stderr)
        return 1

    summary = record.config_summary()
    print(f"Trained model {record.ticker_id}/{record.model_name}")
    print(f"  Model path:      {record.model_path}")
    print(f"  Normalization:   {record.normalization_path}")
    print(f"  Config path:     {record.config_path}")
    print(
        "  Config summary: "
        f"action_space={summary.get('action_space')}, "
        f"reward={summary.get('reward_mode')}, "
        f"windows={summary.get('feature_windows')}"
    )
    print(f"  Trained:         {record.is_trained()}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    """Replay a trained RL model on fresh OHLC data and print metrics."""
    from kraken_trading_bot.rl.backtest import backtest_model

    try:
        result = backtest_model(
            args.ticker,
            args.model,
            pages=args.pages,
            seed=args.seed,
            models_root=args.models_root,
        )
    except Exception as e:
        _LOGGER.error(
            "Backtesting %s/%s failed: %s", args.ticker, args.model, e
        )
        print(f"Error backtesting {args.ticker}/{args.model}: {e}", file=sys.stderr)
        return 1

    d = result.to_dict()
    print(f"Backtest {d['ticker_id']}/{d['model_name']}")
    print(f"  Total return:   {d['total_return'] * 100.0:.2f}%")
    print(f"  Sharpe:         {d['sharpe']:.3f}")
    print(f"  Max drawdown:   {d['max_drawdown'] * 100.0:.2f}%")
    print(f"  Trades:         {d['num_trades']}")
    print(f"  Win rate:       {d['win_rate'] * 100.0:.2f}%")
    print(f"  Final equity:   {d['final_equity']:,.2f}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """List registered RL models grouped by ticker."""
    import json
    from datetime import datetime

    from kraken_trading_bot.rl.features import normalize_ticker_id
    from kraken_trading_bot.rl.registry import list_models

    grouped = list_models(root=args.models_root)
    if args.ticker is not None:
        key = normalize_ticker_id(args.ticker)
        grouped = {key: grouped[key]} if key in grouped else {}

    if not grouped:
        if args.ticker is not None:
            print(f"No models registered for {normalize_ticker_id(args.ticker)}")
        else:
            print(f"No models registered in {args.models_root}")
        return 0

    if args.json:
        payload = {
            ticker: [
                {
                    "ticker_id": record.ticker_id,
                    "model_name": record.model_name,
                    "created": record.created,
                    "is_trained": record.is_trained(),
                    "config_path": (
                        str(record.config_path) if record.config_path else None
                    ),
                    "model_path": str(record.model_path) if record.model_path else None,
                    "normalization_path": (
                        str(record.normalization_path)
                        if record.normalization_path
                        else None
                    ),
                    "config_summary": record.config_summary(),
                }
                for record in records
            ]
            for ticker, records in grouped.items()
        }
        print(json.dumps(payload, indent=2, sort_keys=False))
        return 0

    header = f"{'Ticker':<10} {'Model':<16} {'Created':<19} {'Trained':<8} {'Action':<12} {'Reward':<12}"
    print(header)
    print("-" * len(header))
    for ticker in sorted(grouped):
        for record in grouped[ticker]:
            created = (
                datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M")
                if record.created
                else "-"
            )
            summary = record.config_summary()
            print(
                f"{ticker:<10} {record.model_name:<16} {created:<19} "
                f"{'yes' if record.is_trained() else 'no':<8} "
                f"{str(summary.get('action_space') or '-'):<12} "
                f"{str(summary.get('reward_mode') or '-'):<12}"
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Main entry point for the CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Set up logging
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)

    # Dispatch command
    commands = {
        "run": cmd_run,
        "balance": cmd_balance,
        "ticker": cmd_ticker,
        "orders": cmd_orders,
        "paper-trade": cmd_paper_trade,
        "train": cmd_train,
        "backtest": cmd_backtest,
        "models": cmd_models,
    }

    if args.command is None:
        parser.print_help()
        return 1

    handler = commands.get(args.command)
    if handler:
        return handler(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
