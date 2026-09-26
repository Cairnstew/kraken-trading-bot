"""Command-line interface for the Kraken Trading Bot."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from . import __version__, setup_logging
from .client import KrakenClient
from .engine import TradingEngine
from .strategies.sma import SMAcrossoverStrategy


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

    return parser


def cmd_run(args: argparse.Namespace) -> int:
    """Run the trading bot."""
    from decimal import Decimal

    client = KrakenClient.from_env()

    # Create strategy
    strategy = SMAcrossoverStrategy(
        pair=args.pair[0],
        short_period=args.short_period,
        long_period=args.long_period,
        volume_per_trade=Decimal(args.volume),
    )

    # Create and run engine
    engine = TradingEngine(
        client=client,
        strategies=[strategy],
        pairs=args.pair,
        interval=args.interval,
        paper_mode=args.paper,
    )

    engine.run()
    return 0


def cmd_balance(args: argparse.Namespace) -> int:
    """Show account balances."""
    client = KrakenClient.from_env()

    try:
        trade_balance = client.trade_balance()
        print(f"Total Equity: {trade_balance.total_equity}")
        print(f"Margin Equity: {trade_balance.margin_equity}")
        print(f"Unrealized P&L: {trade_balance.unrealized_pnl}")
        print(f"Realized P&L: {trade_balance.realized_pnl}")
        print(f"Margin Used: {trade_balance.margin_used}")
        print(f"Free Margin: {trade_balance.free_margin}")
        if trade_balance.margin_level:
            print(f"Margin Level: {trade_balance.margin_level}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_ticker(args: argparse.Namespace) -> int:
    """Show current ticker."""
    client = KrakenClient.from_env()

    try:
        tickers = client.ticker(args.pair)
        for pair, ticker in tickers.items():
            print(f"\n{pair}:")
            print(f"  Bid: {ticker.bid}")
            print(f"  Ask: {ticker.ask}")
            print(f"  Last: {ticker.last}")
            print(f"  24h Volume: {ticker.volume_24h}")
            print(f"  24h VWAP: {ticker.vwap_24h}")
            print(f"  24h High: {ticker.high_24h}")
            print(f"  24h Low: {ticker.low_24h}")
            print(f"  24h Trades: {ticker.trades_24h}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_orders(args: argparse.Namespace) -> int:
    """Show open orders."""
    client = KrakenClient.from_env()

    try:
        orders = client.open_orders()
        if not orders:
            print("No open orders")
            return 0

        for txid, order in orders.items():
            print(f"\n{txid}:")
            print(f"  Pair: {order.pair}")
            print(f"  Side: {order.side}")
            print(f"  Type: {order.ordertype}")
            print(f"  Volume: {order.volume}")
            print(f"  Price: {order.price or 'market'}")
            print(f"  Status: {order.status}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


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
