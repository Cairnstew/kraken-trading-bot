"""Tests for the trading engine."""

from decimal import Decimal
from datetime import datetime
from unittest.mock import MagicMock, patch

from kraken_api.models import Candle, OrderBook, Ticker

from kraken_trading_bot.engine import TradingEngine
from kraken_trading_bot.strategies.base import Signal
from kraken_trading_bot.strategies.sma import SMAcrossoverStrategy


def _make_ticker(last: float) -> Ticker:
    """Helper to create a ticker."""
    return Ticker(
        pair="XBT/USD",
        bid=[str(last - 10)],
        ask=[str(last + 10)],
        last=[str(last)],
        volume=["100", "1000"],
        vwap=[str(last), str(last)],
        trade_count=[100, 5000],
        low=[str(last - 500), str(last - 500)],
        high=[str(last + 500), str(last + 500)],
        open_price=[str(last)],
    )


def test_engine_paper_mode():
    """Test that paper mode doesn't execute real orders."""
    manager = MagicMock()
    strategy = SMAcrossoverStrategy(pair="XBT/USD")

    engine = TradingEngine(
        manager=manager,
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
    # Manager should not be called
    manager.buy.assert_not_called()
    manager.sell.assert_not_called()


def test_engine_real_mode():
    """Test that real mode executes orders."""
    manager = MagicMock()
    manager.buy.return_value = {"txid": ["TX123"]}

    strategy = SMAcrossoverStrategy(pair="XBT/USD")

    engine = TradingEngine(
        manager=manager,
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
    manager.buy.assert_called_once()


def test_engine_hold_signal():
    """Test that hold signals are not executed."""
    manager = MagicMock()
    strategy = SMAcrossoverStrategy(pair="XBT/USD")

    engine = TradingEngine(
        manager=manager,
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
    manager.buy.assert_not_called()
    manager.sell.assert_not_called()


def test_engine_fetch_market_data():
    """Test market data fetching."""
    manager = MagicMock()

    # Mock ticker response
    manager.ticker.return_value = _make_ticker(50000)

    # Mock candles response
    candles = [
        Candle(
            pair="XBT/USD",
            time=1625097600 + i * 3600,
            open="50000",
            high="50100",
            low="49900",
            close="50000",
            vwap="50000",
            volume="100",
            count=100,
        )
        for i in range(10)
    ]
    manager.ohlc.return_value = (candles, 1625097600)

    # Mock order book response
    manager.order_book.return_value = OrderBook(
        pair="XBT/USD",
        asks=[],
        bids=[],
    )

    engine = TradingEngine(
        manager=manager,
        strategies=[],
        pairs=["XBT/USD"],
    )

    data = engine.fetch_market_data("XBT/USD")

    assert "ticker" in data
    assert "candles" in data
    assert "order_book" in data
    manager.ticker.assert_called_once_with("XBT/USD")
    manager.ohlc.assert_called_once()
    manager.order_book.assert_called_once()
