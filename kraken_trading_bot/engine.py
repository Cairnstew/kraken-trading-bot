"""Trading engine for the Kraken Trading Bot."""

from __future__ import annotations

import logging
import time
from typing import Any

from .client import KrakenClient
from .strategies.base import Strategy, Signal

_LOGGER = logging.getLogger(__name__)


class TradingEngine:
    """Main trading engine that orchestrates strategy execution.

    The engine connects a KrakenClient with one or more trading strategies,
    manages the trading loop, and handles order execution.

    Parameters:
        client: A configured KrakenClient instance.
        strategies: List of Strategy instances to run.
        pairs: List of trading pairs to monitor.
        interval: Seconds between trading loop iterations.
        paper_mode: If True, log signals but don't execute orders.
    """

    def __init__(
        self,
        client: KrakenClient,
        strategies: list[Strategy],
        pairs: list[str] | None = None,
        interval: float = 60.0,
        paper_mode: bool = False,
    ) -> None:
        self.client = client
        self.strategies = strategies
        self.pairs = pairs or ["XBT/USD"]
        self.interval = interval
        self.paper_mode = paper_mode
        self._running = False

    def fetch_market_data(self, pair: str) -> dict[str, Any]:
        """Fetch current market data for a trading pair.

        Args:
            pair: The trading pair to fetch data for.

        Returns:
            Dictionary containing ticker, candles, and order book data.
        """
        data: dict[str, Any] = {}

        try:
            # Fetch ticker
            tickers = self.client.ticker(pair)
            if tickers:
                data["ticker"] = tickers.get(pair)
        except Exception as e:
            _LOGGER.warning("Failed to fetch ticker for %s: %s", pair, e)

        try:
            # Fetch recent candles (1-hour candles, last 100)
            candles = self.client.ohlc(pair, interval=60)
            data["candles"] = candles[-100:] if candles else []
        except Exception as e:
            _LOGGER.warning("Failed to fetch candles for %s: %s", pair, e)

        try:
            # Fetch order book
            order_book = self.client.order_book(pair, count=10)
            data["order_book"] = order_book
        except Exception as e:
            _LOGGER.warning("Failed to fetch order book for %s: %s", pair, e)

        return data

    def execute_signal(self, signal: Signal) -> bool:
        """Execute a trading signal by placing an order.

        Args:
            signal: The Signal to execute.

        Returns:
            True if the order was placed successfully.
        """
        if not signal.is_actionable:
            return True

        if self.paper_mode:
            _LOGGER.info(
                "[PAPER] Would %s %s %s @ %s (reason: %s)",
                signal.action,
                signal.volume,
                signal.pair,
                signal.price or "market",
                signal.reason,
            )
            return True

        try:
            result = self.client.add_order(
                pair=signal.pair,
                side=signal.action,
                ordertype="limit" if signal.price else "market",
                volume=str(signal.volume),
                price=str(signal.price) if signal.price else None,
            )
            _LOGGER.info("Order placed: %s", result)
            return True
        except Exception as e:
            _LOGGER.error("Failed to place order: %s", e)
            return False

    def run_iteration(self) -> None:
        """Run a single iteration of the trading loop.

        Fetches market data for all pairs, runs each strategy,
        and executes any generated signals.
        """
        for pair in self.pairs:
            _LOGGER.debug("Fetching data for %s", pair)
            market_data = self.fetch_market_data(pair)

            for strategy in self.strategies:
                try:
                    signal = strategy.tick(market_data)
                    if signal.is_actionable:
                        _LOGGER.info(
                            "Strategy %s generated signal: %s %s",
                            strategy.name,
                            signal.action,
                            signal.pair,
                        )
                        self.execute_signal(signal)
                except Exception as e:
                    _LOGGER.error(
                        "Strategy %s error: %s", strategy.name, e, exc_info=True
                    )
                    strategy.on_error(e)

    def run(self) -> None:
        """Start the trading engine loop.

        Runs indefinitely until interrupted (Ctrl+C).
        """
        self._running = True
        _LOGGER.info(
            "Starting trading engine (pairs=%s, strategies=%s, interval=%ss, paper=%s)",
            self.pairs,
            [s.name for s in self.strategies],
            self.interval,
            self.paper_mode,
        )

        try:
            while self._running:
                self.run_iteration()
                time.sleep(self.interval)
        except KeyboardInterrupt:
            _LOGGER.info("Trading engine stopped by user")
        finally:
            self._running = False

    def stop(self) -> None:
        """Stop the trading engine loop."""
        self._running = False
