"""Kraken Trading Bot.

A trading bot framework for the Kraken Spot REST and WebSocket v2 APIs.
Provides a client, data models, error handling, and pluggable trading
strategies for automated trading on the Kraken exchange.
"""

from __future__ import annotations

import logging
from typing import Any

from .client import KrakenClient
from .errors import (
    APIError,
    AuthenticationError,
    ConfigurationError,
    KrakenError,
    OrderError,
    RateLimitError,
    StrategyError,
    WebSocketError,
)
from .models import (
    Balance,
    Candle,
    Order,
    OrderBook,
    Ticker,
    Trade,
    TradeBalance,
)
from .strategies.base import Strategy, StrategyState
from .strategies.sma import SMAcrossoverStrategy

__version__ = "0.1.0"

# Library-safe default: attach a NullHandler so that importing the package
# never configures logging or emits output.  Call setup_logging() from the
# CLI entry-point (or your application) to attach real handlers.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "KrakenClient",
    "Strategy",
    "StrategyState",
    "SMAcrossoverStrategy",
    "Balance",
    "Ticker",
    "Candle",
    "OrderBook",
    "Order",
    "Trade",
    "TradeBalance",
    "KrakenError",
    "AuthenticationError",
    "ConfigurationError",
    "APIError",
    "RateLimitError",
    "OrderError",
    "StrategyError",
    "WebSocketError",
    "__version__",
]


def setup_logging(level: int = logging.INFO) -> None:
    """Configure logging for the trading bot."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
