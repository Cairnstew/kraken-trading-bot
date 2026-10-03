"""Command-line interface for the Kraken Trading Bot."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from . import __version__, setup_logging
from .depth_recorder import (
    DEFAULT_COUNT,
    DEFAULT_EXPECTED_INTERVAL_SECONDS,
    DEFAULT_GAP_FACTOR,
)
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
    train_parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit a single JSON object on stdout (ticker_id, model_name, "
            "n_features, n_bars, timesteps, seed, model_path) and nothing "
            "else; logs stay on stderr."
        ),
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
        "--config",
        default=None,
        help=(
            "YAML run config (default: none — the model's own config only). "
            "Supplies fee_rate, slippage, action_space, initial_balance, "
            "market_data_store and data_window. Without it a backtest "
            "replays frictionless whenever the model was trained "
            "frictionless."
        ),
    )
    backtest_parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit the full BacktestResult as a single JSON object on "
            "stdout and nothing else; logs stay on stderr. Includes "
            "buy_hold_return, excess_return, buy_hold_max_drawdown, "
            "n_bars, fee_rate and slippage."
        ),
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

    # ── export-data command ───────────────────────────────────────────────
    export_parser = sub.add_parser(
        "export-data",
        help="Export the RL data pipeline (OHLCV + features) to CSV.",
    )
    export_parser.add_argument(
        "--ticker",
        required=True,
        help="Ticker to export, e.g. SOL_USD.",
    )
    export_parser.add_argument(
        "--output",
        default=None,
        help="Destination CSV path (default: exports/{TICKER}.csv).",
    )
    export_parser.add_argument(
        "--config",
        default=None,
        help="YAML config to base the run on (default: configs/default.yaml).",
    )
    export_parser.add_argument(
        "--pages",
        type=int,
        default=6,
        help="OHLC pages to fetch (each ~720 candles).",
    )
    export_parser.add_argument(
        "--interval-minutes",
        type=int,
        default=None,
        help="Override the OHLC interval in minutes.",
    )
    export_parser.add_argument(
        "--episode-bars",
        type=int,
        default=None,
        help="Keep only the trailing N bars.",
    )
    export_parser.add_argument(
        "--normalized",
        action="store_true",
        help=(
            "Also append z_-prefixed columns: the agent's z-scored "
            "observation (mirrors what TradingEnvironment hands the "
            "policy)."
        ),
    )

    # ── record-depth command ─────────────────────────────────────────────
    # One keyless /0/public/Depth call, one JSONL line appended.  This is the
    # production path the kraken-trading-bot-order-book.timer unit runs.
    record_parser = sub.add_parser(
        "record-depth",
        help=(
            "Record ONE order-book depth snapshot and append it to a JSONL "
            "log (keyless). Unattended data collection: Kraken's book has no "
            "historical endpoint, so an hour not recorded cannot be recovered."
        ),
    )
    record_parser.add_argument(
        "--pair",
        default="ETH/USD",
        help="Spot pair, e.g. ETH/USD (default: ETH/USD).",
    )
    record_parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Append-only JSONL log to append the snapshot to.",
    )
    record_parser.add_argument(
        "--status-output",
        default=None,
        help=(
            "Path for the gap-report sidecar, rewritten each fire (default: "
            "<output>.status.json). The sidecar is what makes a hole visible "
            "in the artifact and not only on stdout."
        ),
    )
    record_parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=(
            f"Requested book depth per side (default: {DEFAULT_COUNT}, "
            "Kraken's served default). Depth is NOT comparable across a "
            "count change, and every record carries the depth actually "
            "returned -- measured 2026-10-03: count=1000 is served as 100 "
            "with no error."
        ),
    )
    record_parser.add_argument(
        "--expected-interval-seconds",
        type=float,
        default=DEFAULT_EXPECTED_INTERVAL_SECONDS,
        help=(
            "The cadence the gap counter holds the recorder to "
            f"(default: {DEFAULT_EXPECTED_INTERVAL_SECONDS:.0f})."
        ),
    )
    record_parser.add_argument(
        "--gap-factor",
        type=float,
        default=DEFAULT_GAP_FACTOR,
        help=(
            "An interval longer than expected x this factor is a hole "
            f"(default: {DEFAULT_GAP_FACTOR:g})."
        ),
    )

    # ── depth-gaps command ───────────────────────────────────────────────
    # Re-scan an existing log with no network access.  Exits non-zero on a
    # hole so it can gate a CI step or a cron health check, and so the
    # counter's RED verdict is observable by a caller and not only by a human
    # reading the report.
    gaps_parser = sub.add_parser(
        "depth-gaps",
        help=(
            "Scan a recorded depth log for holes (no network) and refresh "
            "its status sidecar. Exits 1 if any interval exceeded the "
            "expected cadence."
        ),
    )
    gaps_parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="The JSONL log to scan.",
    )
    gaps_parser.add_argument(
        "--status-output",
        default=None,
        help="Sidecar to rewrite (default: <output>.status.json).",
    )
    gaps_parser.add_argument(
        "--expected-interval-seconds",
        type=float,
        default=DEFAULT_EXPECTED_INTERVAL_SECONDS,
        help=(
            "The cadence to hold the log to "
            f"(default: {DEFAULT_EXPECTED_INTERVAL_SECONDS:.0f})."
        ),
    )
    gaps_parser.add_argument(
        "--gap-factor",
        type=float,
        default=DEFAULT_GAP_FACTOR,
        help=(
            f"Hole threshold multiplier (default: {DEFAULT_GAP_FACTOR:g})."
        ),
    )
    gaps_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the report as a single JSON object on stdout.",
    )

    return parser


def cmd_record_depth(args: argparse.Namespace) -> int:
    """Record one depth snapshot, append it, then report the log's gaps."""
    from pathlib import Path

    from kraken_trading_bot.depth_recorder import (
        format_report,
        record_once,
    )

    output = Path(args.output)
    status = Path(args.status_output) if args.status_output else Path(
        str(output) + ".status.json"
    )

    try:
        record, report = record_once(
            pair=args.pair,
            count=args.count,
            output=output,
            status_output=status,
            expected_interval_seconds=args.expected_interval_seconds,
            gap_factor=args.gap_factor,
        )
    except Exception as e:
        # A failed pull must not be mistaken for a fresh file: the previous
        # JSONL is left exactly as it was, and the unit's non-zero exit is
        # what marks the hour as missed.  Same contract as the funding unit.
        _LOGGER.error("Depth recording for %s failed: %s", args.pair, e)
        print(f"Error recording depth for {args.pair}: {e}", file=sys.stderr)
        return 1

    depth = record["depth"]
    print(f"Recorded {record['source']} snapshot for {record['pair']}")
    print(f"  recorded_at : {record['recorded_at']}  (this process's own clock)")
    print(f"  hour        : {record['hour']}")
    print(
        f"  depth       : requested {depth['requested_count']}, "
        f"got {depth['bid_levels']} bids / {depth['ask_levels']} asks"
        + ("  TRUNCATED" if depth["truncated"] else "")
    )
    print(f"  best        : bid {record['best_bid']} / ask {record['best_ask']}")
    print(f"  spread      : {record['spread']}")
    print(f"  appended to : {output}")
    print()
    print(
        format_report(
            report,
            artifact=output,
            status_file=status,
        )
    )

    # Non-zero when the log has a hole, so an unattended caller can gate on
    # it.  A hole is a data-loss event, not a cosmetic one.
    return 0 if report.ok else 1


def cmd_depth_gaps(args: argparse.Namespace) -> int:
    """Re-scan an existing depth log.  No network; refreshes the sidecar."""
    import json as _json

    from pathlib import Path as _Path

    from kraken_trading_bot.depth_recorder import (
        format_report,
        scan_gap_file,
        write_status,
    )

    output = _Path(args.output)
    status = _Path(args.status_output) if args.status_output else _Path(
        str(output) + ".status.json"
    )

    report = scan_gap_file(
        output,
        expected_interval_seconds=args.expected_interval_seconds,
        gap_factor=args.gap_factor,
    )
    write_status(status, report, artifact=str(output))

    if args.as_json:
        print(_json.dumps(report.to_dict(), indent=2))
    else:
        print(format_report(report, artifact=output, status_file=status))
    return 0 if report.ok else 1


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
    import json

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
    # `n_bars` is the window provenance written by train_ticker: the bars
    # this run actually trained on. None for an artifact trained before
    # that key existed.
    n_bars = record.config.get("n_bars")
    if args.json:
        # stdout is a single JSON object and nothing else, so a caller can
        # parse it without stripping anything. Logs went to stderr.
        print(
            json.dumps(
                {
                    "ticker_id": record.ticker_id,
                    "model_name": record.model_name,
                    "n_features": summary.get("n_features"),
                    "n_bars": None if n_bars is None else int(n_bars),
                    "timesteps": int(args.timesteps),
                    "seed": args.seed,
                    "model_path": (
                        str(record.model_path) if record.model_path else None
                    ),
                }
            )
        )
        return 0

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
    # The observation width, so a later `models` listing can show whether
    # this artifact predates a pipeline widening.
    print(f"  Obs features:    {summary.get('n_features')}")
    if n_bars is not None:
        # The magnitude of the training window: a 12-bar artifact and a
        # 3000-bar one produce returns of the same shape.
        print(f"  Training bars:   {int(n_bars)}")
    print(f"  Trained:         {record.is_trained()}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    """Replay a trained RL model on fresh OHLC data and print metrics."""
    import json

    from kraken_trading_bot.rl.backtest import backtest_model

    try:
        result = backtest_model(
            args.ticker,
            args.model,
            pages=args.pages,
            seed=args.seed,
            models_root=args.models_root,
            config_path=args.config,
        )
    except Exception as e:
        _LOGGER.error(
            "Backtesting %s/%s failed: %s", args.ticker, args.model, e
        )
        print(f"Error backtesting {args.ticker}/{args.model}: {e}", file=sys.stderr)
        return 1

    d = result.to_dict()
    if args.json:
        # One JSON object on stdout and nothing else: the module logs to
        # stderr, so a caller can pipe this straight into a parser.
        print(json.dumps(d))
        return 0

    print(f"Backtest {d['ticker_id']}/{d['model_name']}")
    # The scope label comes FIRST, before any number it qualifies. With
    # ``since``/``until`` null -- the shipped default -- training_frame and
    # evaluation_frame return the SAME object, so every return printed
    # below describes the bars the model was fitted on. Unlabelled, that
    # reads as a prediction; it is not one.
    print(f"  Evaluation:     {d['evaluation_scope_label']}")
    if d.get("evaluation_scope_reason"):
        print(f"                  ({d['evaluation_scope_reason']})")
    print(f"  Total return:   {d['total_return'] * 100.0:.2f}%")
    # The reference point. A strategy return on its own cannot distinguish
    # a working model from a lucky one on a rising asset.
    print(f"  Buy & hold:     {d['buy_hold_return'] * 100.0:.2f}%")
    print(f"  Excess return:  {d['excess_return'] * 100.0:.2f}%")
    print(f"  Sharpe:         {d['sharpe']:.3f}")
    print(f"  Max drawdown:   {d['max_drawdown'] * 100.0:.2f}%")
    print(f"  Buy&hold maxdd: {d['buy_hold_max_drawdown'] * 100.0:.2f}%")
    print(f"  Trades:         {d['num_trades']}")
    print(f"  Win rate:       {d['win_rate'] * 100.0:.2f}%")
    print(f"  Final equity:   {d['final_equity']:,.2f}")
    # Magnitude guard: how much data these numbers actually describe.
    print(f"  Bars replayed:  {d['n_bars']}")
    # Echo the costs that were actually applied, so this output cannot be
    # read as a frictionless result when it was one.
    print(
        f"  Fee / slippage: {d['fee_rate'] * 100.0:.4f}% / "
        f"{d['slippage'] * 100.0:.4f}%"
    )
    print(f"  Action space:   {d['action_space']}")
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

    header = (
        f"{'Ticker':<10} {'Model':<16} {'Created':<19} {'Trained':<8} "
        f"{'Action':<12} {'Reward':<12} {'Width':>5}"
    )
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
            # "-" means the artifact predates width provenance, i.e. it
            # cannot be shown to match the live pipeline (see
            # ModelRecord.is_stale_width).
            width = summary.get("n_features")
            print(
                f"{ticker:<10} {record.model_name:<16} {created:<19} "
                f"{'yes' if record.is_trained() else 'no':<8} "
                f"{str(summary.get('action_space') or '-'):<12} "
                f"{str(summary.get('reward_mode') or '-'):<12} "
                f"{'?' if width is None else width:>5}"
            )
    return 0


def cmd_export_data(args: argparse.Namespace) -> int:
    """Run the RL data pipeline and write the staged frame to CSV."""
    from kraken_trading_bot.rl.export import (
        build_export_frame,
        default_export_path,
        write_export_csv,
    )

    # Only flags the user actually set become config overrides, so the
    # rest of the run keeps its YAML config (defaults to configs/default.yaml).
    overrides = {}
    if args.interval_minutes is not None:
        overrides["ohlcv_interval_minutes"] = args.interval_minutes

    try:
        frame = build_export_frame(
            args.ticker,
            config_path=args.config,
            pages=args.pages,
            episode_bars=args.episode_bars,
            include_normalized=args.normalized,
            **overrides,
        )
    except Exception as e:
        _LOGGER.error("Exporting %s failed: %s", args.ticker, e)
        print(f"Error exporting {args.ticker}: {e}", file=sys.stderr)
        return 1

    path = write_export_csv(frame, args.output or default_export_path(args.ticker))
    stages = frame.attrs.get("stages", {})
    print(f"Exported {len(frame)} bars x {len(frame.columns)} columns -> {path}")
    if len(frame):
        print(
            f"  window:   {frame['timestamp'].iloc[0]}"
            f" -> {frame['timestamp'].iloc[-1]}"
        )
    print(
        "  stages:   {ts} timestamp, {ohlcv} OHLCV, {sig} signal, "
        "{feat} observation (raw), {norm} normalized, warmup flag".format(
            ts=len(stages.get("timestamp", [])),
            ohlcv=len(stages.get("ohlcv", [])),
            sig=len(stages.get("signals", [])),
            feat=len(stages.get("features", [])),
            norm=len(stages.get("normalized", [])),
        )
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
        "export-data": cmd_export_data,
        "record-depth": cmd_record_depth,
        "depth-gaps": cmd_depth_gaps,
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
