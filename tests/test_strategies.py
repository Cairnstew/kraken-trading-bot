"""Tests for the trading strategies."""

from decimal import Decimal
from datetime import datetime
from typing import Sequence

from kraken_trading_bot.models import Candle, OrderBook, OrderBookLevel, Ticker
from kraken_trading_bot.strategies.sma import SMAcrossoverStrategy


def _make_candles(closes: Sequence[float | int]) -> list[Candle]:
    """Helper to create candles from close prices."""
    return [
        Candle(
            pair="XBT/USD",
            time=datetime(2024, 1, 1, i),
            open=Decimal(str(c - 100)),
            high=Decimal(str(c + 100)),
            low=Decimal(str(c - 100)),
            close=Decimal(str(c)),
            vwap=Decimal(str(c)),
            volume=Decimal("100"),
            count=100,
        )
        for i, c in enumerate(closes)
    ]


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


def test_sma_strategy_insufficient_data():
    """Test SMA strategy with insufficient candle data."""
    strategy = SMAcrossoverStrategy(
        pair="XBT/USD",
        short_period=10,
        long_period=30,
    )

    # Less than long_period + 1 candles should hold
    candles = _make_candles([50000 + i * 100 for i in range(20)])
    data = {"candles": candles, "ticker": _make_ticker(50000)}

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
    closes = [50000 + i * 100 for i in range(50)]
    candles = _make_candles(closes)
    data = {"candles": candles, "ticker": _make_ticker(54900)}

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
    closes1 = [50000, 49000, 48000, 47000, 46000, 45000]
    candles1 = _make_candles(closes1)
    data1 = {"candles": candles1, "ticker": _make_ticker(45000)}

    signal1 = strategy.tick(data1)
    assert signal1.action == "hold"  # No previous data for crossover

    # Second tick: short SMA crosses above long SMA (golden cross)
    closes2 = [45000, 46000, 47000, 48000, 49000, 50000]
    candles2 = _make_candles(closes2)
    data2 = {"candles": candles2, "ticker": _make_ticker(50000)}

    signal2 = strategy.tick(data2)
    # Should detect golden cross
    assert signal2.action == "buy"
    assert signal2.pair == "XBT/USD"
    assert signal2.volume == Decimal("0.01")


def test_sma_strategy_death_cross():
    """Test SMA strategy death cross detection."""
    strategy = SMAcrossoverStrategy(
        pair="XBT/USD",
        short_period=3,
        long_period=5,
        volume_per_trade=Decimal("0.01"),
    )

    # First tick: establish baseline where short > long
    closes1 = [50000, 51000, 52000, 53000, 54000, 55000]
    candles1 = _make_candles(closes1)
    data1 = {"candles": candles1, "ticker": _make_ticker(55000)}

    signal1 = strategy.tick(data1)
    assert signal1.action == "hold"

    # Second tick: short SMA crosses below long SMA (death cross)
    closes2 = [55000, 54000, 53000, 52000, 51000, 50000]
    candles2 = _make_candles(closes2)
    data2 = {"candles": candles2, "ticker": _make_ticker(50000)}

    signal2 = strategy.tick(data2)
    # Should detect death cross
    assert signal2.action == "sell"
    assert signal2.pair == "XBT/USD"
    assert signal2.volume == Decimal("0.01")


def test_sma_strategy_state_persistence():
    """Test that strategy state persists across ticks."""
    strategy = SMAcrossoverStrategy(
        pair="XBT/USD",
        short_period=3,
        long_period=5,
    )

    # First tick
    closes1 = [50000, 49000, 48000, 47000, 46000, 45000]
    candles1 = _make_candles(closes1)
    data1 = {"candles": candles1, "ticker": _make_ticker(45000)}
    strategy.tick(data1)

    # Check that indicators are stored
    assert "short_sma" in strategy.state.indicators
    assert "long_sma" in strategy.state.indicators

    # Second tick should have previous values
    closes2 = [45000, 46000, 47000, 48000, 49000, 50000]
    candles2 = _make_candles(closes2)
    data2 = {"candles": candles2, "ticker": _make_ticker(50000)}
    strategy.tick(data2)

    # Previous SMAs should be stored
    assert "prev_short_sma" in strategy.state.indicators
    assert "prev_long_sma" in strategy.state.indicators


def test_sma_strategy_reset():
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
