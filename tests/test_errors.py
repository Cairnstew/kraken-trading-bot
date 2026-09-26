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


def test_api_error_message():
    """Test API error message formatting."""
    errors = ["EGeneral:Invalid parameters", "EService:Unavailable"]
    error = APIError(errors, endpoint="Ticker")

    assert str(error) == "Ticker: Kraken API error: EGeneral:Invalid parameters; EService:Unavailable"
    assert error.errors == errors
    assert error.endpoint == "Ticker"


def test_api_error_no_endpoint():
    """Test API error without endpoint."""
    errors = ["EGeneral:Invalid parameters"]
    error = APIError(errors)

    assert str(error) == "Kraken API error: EGeneral:Invalid parameters"
    assert error.endpoint == ""


def test_api_error_empty_errors():
    """Test API error with empty errors list."""
    error = APIError([])

    assert str(error) == "Kraken API error: "
    assert error.errors == []


def test_rate_limit_error():
    """Test rate limit error."""
    errors = ["EAPI:Rate limit exceeded"]
    error = RateLimitError(errors, endpoint="Balance")

    assert isinstance(error, APIError)
    assert isinstance(error, KrakenError)
    assert error.errors == errors
    assert error.endpoint == "Balance"
