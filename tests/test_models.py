"""Tests for the trading bot models and strategies."""

from decimal import Decimal
from datetime import datetime

from kraken_api.models import Candle, OrderBook, Ticker

from kraken_trading_bot.strategies.base import Signal, StrategyState
from kraken_trading_bot.strategies.sma import SMAcrossoverStrategy


def test_signal_properties():
    """Test Signal class properties."""
    buy_signal = Signal(action="buy", pair="XBT/USD", volume=Decimal("0.01"))
    sell_signal = Signal(action="sell", pair="XBT/USD", volume=Decimal("0.01"))
    hold_signal = Signal(action="hold", pair="XBT/USD")

    assert buy_signal.is_actionable is True
    assert sell_signal.is_actionable is True
    assert hold_signal.is_actionable is False


def test_strategy_state():
    """Test StrategyState class."""
    state = StrategyState()

    # Initial state
    assert state.has_position() is False
    assert state.is_long() is False
    assert state.is_short() is False
    assert state.position == Decimal("0")

    # Set long position
    state.position = Decimal("0.01")
    assert state.has_position() is True
    assert state.is_long() is True
    assert state.is_short() is False

    # Set short position
    state.position = Decimal("-0.01")
    assert state.has_position() is True
    assert state.is_long() is False
    assert state.is_short() is True


def test_strategy_reset():
    """Test strategy state reset."""
    strategy = SMAcrossoverStrategy(
        pair="XBT/USD",
        short_period=3,
        long_period=5,
    )

    # Set some state
    strategy.state.indicators["short_sma"] = Decimal("50000")
    strategy.state.position = Decimal("0.01")

    # Reset
    strategy.reset()

    # State should be cleared
    assert strategy.state.indicators == {}
    assert strategy.state.position == Decimal("0")


def test_sma_strategy_insufficient_data():
    """Test SMA strategy with insufficient candle data."""
    strategy = SMAcrossoverStrategy(
        pair="XBT/USD",
        short_period=10,
        long_period=30,
    )

    # Create mock candles with insufficient data
    # Need at least long_period + 1 = 31 candles
    candles = [
        Candle(
            pair="XBT/USD",
            time=1625097600 + i * 3600,
            open=str(50000 + i * 100),
            high=str(50100 + i * 100),
            low=str(49900 + i * 100),
            close=str(50000 + i * 100),
            vwap=str(50000 + i * 100),
            volume="100",
            count=100,
        )
        for i in range(20)  # Less than 31 candles
    ]

    ticker = Ticker(
        pair="XBT/USD",
        bid=["49990"],
        ask=["50010"],
        last=["50000"],
        volume=["100", "1000"],
        vwap=["50000", "50000"],
        trade_count=[100, 5000],
        low=["49500", "49500"],
        high=["50500", "50500"],
        open_price=["50000"],
    )

    data = {"candles": candles, "ticker": ticker}
    signal = strategy.tick(data)

    assert signal.action == "hold"
    assert signal.reason == "insufficient data"


def test_sma_strategy_no_crossover():
    """Test SMA strategy when no crossover occurs."""
    strategy = SMAcrossoverStrategy(
        pair="XBT/USD",
        short_period=5,
        long_period=10,
    )

    # Create stable uptrend where short SMA stays above long SMA
    candles = [
        Candle(
            pair="XBT/USD",
            time=1625097600 + i * 3600,
            open=str(50000 + i * 100),
            high=str(50100 + i * 100),
            low=str(49900 + i * 100),
            close=str(50000 + i * 100),
            vwap=str(50000 + i * 100),
            volume="100",
            count=100,
        )
        for i in range(50)  # More than 10 + 1 candles
    ]

    ticker = Ticker(
        pair="XBT/USD",
        bid=["54990"],
        ask=["55010"],
        last=["55000"],
        volume=["100", "1000"],
        vwap=["55000", "55000"],
        trade_count=[100, 5000],
        low=["54500", "54500"],
        high=["55500", "55500"],
        open_price=["55000"],
    )

    data = {"candles": candles, "ticker": ticker}

    # First tick sets up SMAs
    signal1 = strategy.tick(data)
    assert signal1.action == "hold"

    # Second tick with same trend should hold
    signal2 = strategy.tick(data)
    assert signal2.action == "hold"


def test_sma_strategy_golden_cross():
    """Test SMA strategy golden cross detection."""
    strategy = SMAcrossoverStrategy(
        pair="XBT/USD",
        short_period=3,
        long_period=5,
        volume_per_trade=Decimal("0.01"),
    )

    # First tick: establish baseline where short < long
    # Need at least long_period + 1 = 6 candles
    candles1 = [
        Candle(
            pair="XBT/USD",
            time=1625097600 + i * 3600,
            open=str(50000 - i * 1000),  # Downtrend
            high=str(50100 - i * 1000),
            low=str(49900 - i * 1000),
            close=str(50000 - i * 1000),
            vwap=str(50000 - i * 1000),
            volume="100",
            count=100,
        )
        for i in range(6)
    ]

    ticker1 = Ticker(
        pair="XBT/USD",
        bid=["44990"],
        ask=["45010"],
        last=["45000"],
        volume=["100", "1000"],
        vwap=["45000", "45000"],
        trade_count=[100, 5000],
        low=["44500", "44500"],
        high=["45500", "45500"],
        open_price=["45000"],
    )

    signal1 = strategy.tick({"candles": candles1, "ticker": ticker1})
    assert signal1.action == "hold"  # No previous data for crossover

    # Second tick: short SMA crosses above long SMA (golden cross)
    candles2 = [
        Candle(
            pair="XBT/USD",
            time=1625097600 + i * 3600,
            open=str(45000 + i * 1000),  # Uptrend
            high=str(45100 + i * 1000),
            low=str(44900 + i * 1000),
            close=str(45000 + i * 1000),
            vwap=str(45000 + i * 1000),
            volume="100",
            count=100,
        )
        for i in range(6)
    ]

    ticker2 = Ticker(
        pair="XBT/USD",
        bid=["49990"],
        ask=["50010"],
        last=["50000"],
        volume=["100", "1000"],
        vwap=["50000", "50000"],
        trade_count=[100, 5000],
        low=["49500", "49500"],
        high=["50500", "50500"],
        open_price=["50000"],
    )

    signal2 = strategy.tick({"candles": candles2, "ticker": ticker2})
    # Should detect golden cross
    assert signal2.action == "buy"
    assert signal2.pair == "XBT/USD"
    assert signal2.volume == Decimal("0.01")
