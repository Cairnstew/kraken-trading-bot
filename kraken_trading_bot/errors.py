"""Exception hierarchy for the Kraken Trading Bot."""

from __future__ import annotations

from kraken_api.errors import (
    APIError,
    AuthenticationError,
    ConfigurationError,
    KrakenError,
    OrderError,
    RateLimitError,
    WebSocketError,
)


class StrategyError(KrakenError):
    """A trading strategy encountered an error during execution."""


__all__ = [
    "KrakenError",
    "AuthenticationError",
    "ConfigurationError",
    "APIError",
    "RateLimitError",
    "OrderError",
    "StrategyError",
    "WebSocketError",
]
