"""Simple Moving Average (SMA) crossover strategy."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .base import Signal, Strategy


class SMAcrossoverStrategy(Strategy):
    """SMA crossover strategy for trend following.

    This strategy generates buy signals when the short-term SMA crosses
    above the long-term SMA (golden cross), and sell signals when the
    short-term SMA crosses below the long-term SMA (death cross).

    Parameters:
        pair: The trading pair to trade (e.g., "XBT/USD").
        short_period: Number of candles for the short-term SMA.
        long_period: Number of candles for the long-term SMA.
        volume_per_trade: Volume to trade per signal.
        use_limit_orders: If True, use limit orders at best bid/ask.
    """

    def __init__(
        self,
        pair: str = "XBT/USD",
        short_period: int = 10,
        long_period: int = 30,
        volume_per_trade: Decimal = Decimal("0.01"),
        use_limit_orders: bool = True,
    ) -> None:
        super().__init__(name=f"SMA_{short_period}_{long_period}")
        self.pair = pair
        self.short_period = short_period
        self.long_period = long_period
        self.volume_per_trade = volume_per_trade
        self.use_limit_orders = use_limit_orders

        # Initialize indicator state
        self.state.indicators["short_sma"] = None
        self.state.indicators["long_sma"] = None
        self.state.indicators["prev_short_sma"] = None
        self.state.indicators["prev_long_sma"] = None

    def _calculate_sma(self, closes: list[Decimal], period: int) -> Decimal | None:
        """Calculate Simple Moving Average for the given period."""
        if len(closes) < period:
            return None
        recent_closes = closes[-period:]
        total = sum(recent_closes)
        return total / Decimal(str(period))

    def tick(self, data: dict[str, Any]) -> Signal:
        """Process market data and generate SMA crossover signals.

        Args:
            data: Dictionary containing:
                - "candles": List of Candle objects (at least long_period + 1)
                - "ticker": Current Ticker object (for limit order pricing)

        Returns:
            Signal indicating buy, sell, or hold.
        """
        candles = data.get("candles", [])
        ticker = data.get("ticker")

        # Need enough candles for the long SMA
        if len(candles) < self.long_period + 1:
            self._logger.debug(
                "Not enough candles: %d < %d", len(candles), self.long_period + 1
            )
            return Signal(action="hold", pair=self.pair, reason="insufficient data")

        # Extract close prices
        closes = [candle.close for candle in candles]

        # Calculate current SMAs
        short_sma = self._calculate_sma(closes, self.short_period)
        long_sma = self._calculate_sma(closes, self.long_period)

        if short_sma is None or long_sma is None:
            return Signal(action="hold", pair=self.pair, reason="SMA calculation failed")

        # Store previous SMAs for crossover detection
        prev_short_sma = self.state.indicators.get("short_sma")
        prev_long_sma = self.state.indicators.get("long_sma")

        # Update current SMAs
        self.state.indicators["prev_short_sma"] = prev_short_sma
        self.state.indicators["prev_long_sma"] = prev_long_sma
        self.state.indicators["short_sma"] = short_sma
        self.state.indicators["long_sma"] = long_sma

        # Detect crossover
        signal = self._detect_crossover(
            short_sma=short_sma,
            long_sma=long_sma,
            prev_short_sma=prev_short_sma,
            prev_long_sma=prev_long_sma,
            ticker=ticker,
        )

        if signal.is_actionable:
            self.state.last_signal = signal
            self._logger.info(
                "Signal generated: %s (short=%.2f, long=%.2f)",
                signal.action,
                float(short_sma),
                float(long_sma),
            )

        return signal

    def _detect_crossover(
        self,
        short_sma: Decimal,
        long_sma: Decimal,
        prev_short_sma: Decimal | None,
        prev_long_sma: Decimal | None,
        ticker: Any,
    ) -> Signal:
        """Detect SMA crossover and generate appropriate signal.

        Golden Cross: short SMA crosses above long SMA -> BUY
        Death Cross: short SMA crosses below long SMA -> SELL
        """
        # If we don't have previous values, can't detect crossover
        if prev_short_sma is None or prev_long_sma is None:
            return Signal(action="hold", pair=self.pair, reason="no previous SMA data")

        # Golden Cross: short was below long, now above
        if prev_short_sma <= prev_long_sma and short_sma > long_sma:
            price = None
            if self.use_limit_orders and ticker:
                price = ticker.best_ask.price if ticker.best_ask else None
            return Signal(
                action="buy",
                pair=self.pair,
                volume=self.volume_per_trade,
                price=price,
                reason=f"Golden Cross: SMA{self.short_period} ({short_sma:.2f}) > SMA{self.long_period} ({long_sma:.2f})",
            )

        # Death Cross: short was above long, now below
        if prev_short_sma >= prev_long_sma and short_sma < long_sma:
            price = None
            if self.use_limit_orders and ticker:
                price = ticker.best_bid.price if ticker.best_bid else None
            return Signal(
                action="sell",
                pair=self.pair,
                volume=self.volume_per_trade,
                price=price,
                reason=f"Death Cross: SMA{self.short_period} ({short_sma:.2f}) < SMA{self.long_period} ({long_sma:.2f})",
            )

        return Signal(action="hold", pair=self.pair, reason="no crossover detected")
