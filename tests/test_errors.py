"""Tests for the error hierarchy."""

from kraken_trading_bot.errors import (
    APIError,
    AuthenticationError,
    ConfigurationError,
    KrakenError,
    OrderError,
    RateLimitError,
    StrategyError,
    WebSocketError,
)


def test_exception_hierarchy():
    """Test that all exceptions inherit from KrakenError."""
    assert issubclass(ConfigurationError, KrakenError)
    assert issubclass(AuthenticationError, KrakenError)
    assert issubclass(APIError, KrakenError)
    assert issubclass(RateLimitError, APIError)
    assert issubclass(OrderError, KrakenError)
    assert issubclass(StrategyError, KrakenError)
    assert issubclass(WebSocketError, KrakenError)


def test_strategy_error():
    """Test StrategyError is a KrakenError."""
    error = StrategyError("Strategy failed")
    assert isinstance(error, KrakenError)
    assert str(error) == "Strategy failed"
