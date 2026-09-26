"""Kraken Trading Bot.

A trading bot framework using the kraken-python API wrapper.
Provides pluggable trading strategies for automated trading on the Kraken exchange.
"""

from __future__ import annotations

import logging
from typing import Any

from .errors import StrategyError
from .strategies.base import Strategy, StrategyState
from .strategies.sma import SMAcrossoverStrategy

__version__ = "0.1.0"

# Library-safe default: attach a NullHandler so that importing the package
# never configures logging or emits output.  Call setup_logging() from the
# CLI entry-point (or your application) to attach real handlers.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "Strategy",
    "StrategyState",
    "SMAcrossoverStrategy",
    "StrategyError",
    "__version__",
]


def setup_logging(level: int = logging.INFO) -> None:
    """Configure logging for the trading bot."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
