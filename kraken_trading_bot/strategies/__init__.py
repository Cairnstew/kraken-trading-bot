"""Trading strategies for the Kraken Trading Bot."""

from .base import Strategy, StrategyState
from .sma import SMAcrossoverStrategy

__all__ = [
    "Strategy",
    "StrategyState",
    "SMAcrossoverStrategy",
]
