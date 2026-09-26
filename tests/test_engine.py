"""Tests for the trading engine."""

from decimal import Decimal
from datetime import datetime
from unittest.mock import MagicMock, patch

from kraken_trading_bot.engine import TradingEngine
from kraken_trading_bot.models import Candle, OrderBook, Ticker
from kraken_trading_bot.strategies.base import Signal
from kraken_trading_bot.strategies.sma import SMAcrossoverStrategy


def _make_ticker(last: float) -> Ticker:
    """Helper to create a ticker."""
    return Ticker(
        pair="XBT/USD",
        bid=Decimal(str(last - 10)),
        ask=Decimal(str(last + 10)),
        last=Decimal(str(last)),
        volume_24h=Decimal("1000"),
        vwap_24h=Decimal(str(last)),
        high_24h=Decimal(str(last + 500)),
        low_24h=Decimal(str(last - 500)),
        trades_24h=5000,
        timestamp=datetime.utcnow(),
    )


def test_engine_paper_mode():
    """Test that paper mode doesn't execute real orders."""
    client = MagicMock()
    strategy = SMAcrossoverStrategy(pair="XBT/USD")

    engine = TradingEngine(
        client=client,
        strategies=[strategy],
        pairs=["XBT/USD"],
        paper_mode=True,
    )

    # Create a buy signal
    signal = Signal(
        action="buy",
        pair="XBT/USD",
        volume=Decimal("0.01"),
        price=Decimal("50000"),
        reason="test",
    )

    # Execute in paper mode
    result = engine.execute_signal(signal)

    assert result is True
    # Client should not be called
    client.add_order.assert_not_called()


def test_engine_real_mode():
    """Test that real mode executes orders."""
    client = MagicMock()
    client.add_order.return_value = {"txid": ["TX123"]}

    strategy = SMAcrossoverStrategy(pair="XBT/USD")

    engine = TradingEngine(
        client=client,
        strategies=[strategy],
        pairs=["XBT/USD"],
        paper_mode=False,
    )

    # Create a buy signal
    signal = Signal(
        action="buy",
        pair="XBT/USD",
        volume=Decimal("0.01"),
        price=Decimal("50000"),
        reason="test",
    )

    # Execute
    result = engine.execute_signal(signal)

    assert result is True
    client.add_order.assert_called_once()


def test_engine_hold_signal():
    """Test that hold signals are not executed."""
    client = MagicMock()
    strategy = SMAcrossoverStrategy(pair="XBT/USD")

    engine = TradingEngine(
        client=client,
        strategies=[strategy],
        pairs=["XBT/USD"],
        paper_mode=False,
    )

    # Create a hold signal
    signal = Signal(
        action="hold",
        pair="XBT/USD",
        reason="no signal",
    )

    # Execute
    result = engine.execute_signal(signal)

    assert result is True
    client.add_order.assert_not_called()


def test_engine_fetch_market_data():
    """Test market data fetching."""
    client = MagicMock()

    # Mock ticker response
    client.ticker.return_value = {
        "XBT/USD": _make_ticker(50000),
    }

    # Mock candles response
    client.ohlc.return_value = [
        Candle(
            pair="XBT/USD",
            time=datetime(2024, 1, 1, i),
            open=Decimal("50000"),
            high=Decimal("50100"),
            low=Decimal("49900"),
            close=Decimal("50000"),
            vwap=Decimal("50000"),
            volume=Decimal("100"),
            count=100,
        )
        for i in range(10)
    ]

    # Mock order book response
    client.order_book.return_value = OrderBook(
        pair="XBT/USD",
        asks=[],
        bids=[],
    )

    engine = TradingEngine(
        client=client,
        strategies=[],
        pairs=["XBT/USD"],
    )

    data = engine.fetch_market_data("XBT/USD")

    assert "ticker" in data
    assert "candles" in data
    assert "order_book" in data
    client.ticker.assert_called_once_with("XBT/USD")
    client.ohlc.assert_called_once()
    client.order_book.assert_called_once()
