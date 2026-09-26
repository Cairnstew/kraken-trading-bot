"""Kraken API client for the Trading Bot."""

from __future__ import annotations

import logging
from typing import Any

from decimal import Decimal

from .errors import OrderError
from .models import Balance, Candle, Order, OrderBook, Ticker, Trade, TradeBalance
from .transport import KrakenTransport

_LOGGER = logging.getLogger(__name__)


class KrakenClient:
    """High-level Kraken REST client with typed responses.

    Provides a friendly interface for interacting with the Kraken API,
    returning parsed data models instead of raw dictionaries.

    Parameters
    ----------
    api_key:
        Kraken API key (public key).
    api_secret:
        Kraken API secret (private signing key).
    base_url:
        REST API base URL. Defaults to Kraken's production endpoint.
    min_interval:
        Minimum seconds between API requests for rate limiting.
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "https://api.kraken.com",
        min_interval: float = 0.1,
    ) -> None:
        self.transport = KrakenTransport(
            api_key=api_key,
            api_secret=api_secret,
            base_url=base_url,
            min_interval=min_interval,
        )

    @classmethod
    def from_env(cls, **kwargs: Any) -> "KrakenClient":
        """Build a client from ``KRAKEN_*`` environment variables."""
        from .auth import client_from_env

        return client_from_env(**kwargs)

    # ------------------------------------------------------------------ #
    # Market data (public)
    # ------------------------------------------------------------------ #

    def server_time(self) -> dict[str, Any]:
        """Return the exchange server's time (UTC)."""
        return self.transport.public("Time")

    def ticker(self, pair: str | list[str]) -> dict[str, Ticker]:
        """Return current ticker for one or more trading pairs.

        Args:
            pair: A single pair string or list of pairs.

        Returns:
            Dictionary mapping pair names to Ticker objects.
        """
        pairs = [pair] if isinstance(pair, str) else list(pair)
        result = self.transport.public("Ticker", params={"pair": ",".join(pairs)})
        return {k: Ticker.from_api(k, v) for k, v in result.items()}

    def ohlc(
        self,
        pair: str,
        interval: int = 5,
        since: int | None = None,
    ) -> list[Candle]:
        """Return OHLC candles for a trading pair.

        Args:
            pair: The trading pair (e.g., "XBT/USD").
            interval: Candle interval in minutes (1, 5, 15, 30, 60, 240, 1440, 10080, 21600).
            since: Optional timestamp to fetch candles since.

        Returns:
            List of Candle objects.
        """
        params: dict[str, Any] = {"pair": pair, "interval": interval}
        if since is not None:
            params["since"] = since
        result = self.transport.public("OHLC", params=params)

        # Find the candles array (key is pair-dependent)
        for key, value in result.items():
            if key != "last":
                return [Candle.from_api(pair, candle) for candle in value]
        return []

    def order_book(self, pair: str, count: int | None = None) -> OrderBook:
        """Return the order book for a trading pair.

        Args:
            pair: The trading pair.
            count: Optional number of price levels per side.

        Returns:
            OrderBook object with asks and bids.
        """
        params: dict[str, Any] = {"pair": pair}
        if count is not None:
            params["count"] = count
        result = self.transport.public("Depth", params=params)

        # Find the order book data (key is pair-dependent)
        for key, value in result.items():
            return OrderBook.from_api(pair, value)
        return OrderBook(pair=pair, asks=[], bids=[])

    # ------------------------------------------------------------------ #
    # Account (private)
    # ------------------------------------------------------------------ #

    def balance(self) -> dict[str, Balance]:
        """Return all asset balances.

        Returns:
            Dictionary mapping asset codes to Balance objects.
        """
        result = self.transport.private("Balance") or {}
        return {
            k: Balance(asset=k, balance=Decimal(v), hold=Decimal("0"))
            for k, v in result.items()
        }

    def trade_balance(self, asset: str | None = None) -> TradeBalance:
        """Return the account's trade balance summary.

        Args:
            asset: Optional asset to denominate balances in.

        Returns:
            TradeBalance object with equity, margin, P&L, etc.
        """
        data = {} if asset is None else {"asset": asset}
        result = self.transport.private("TradeBalance", data) or {}
        return TradeBalance.from_api(result)

    def open_orders(self) -> dict[str, Order]:
        """Return currently open orders.

        Returns:
            Dictionary mapping transaction IDs to Order objects.
        """
        result = self.transport.private("OpenOrders", {"trades": "true"}) or {}
        return {
            txid: Order.from_api(txid, order)
            for txid, order in result.get("open", {}).items()
        }

    def closed_orders(
        self,
        start: int | None = None,
        end: int | None = None,
    ) -> dict[str, Order]:
        """Return closed orders.

        Args:
            start: Optional start timestamp.
            end: Optional end timestamp.

        Returns:
            Dictionary mapping transaction IDs to Order objects.
        """
        data: dict[str, Any] = {"trades": "true"}
        if start is not None:
            data["start"] = start
        if end is not None:
            data["end"] = end
        result = self.transport.private("ClosedOrders", data) or {}
        return {
            txid: Order.from_api(txid, order)
            for txid, order in result.get("closed", {}).items()
        }

    def add_order(
        self,
        pair: str,
        side: str,
        ordertype: str,
        volume: str | float,
        price: str | float | None = None,
        validate: bool = False,
        timeinforce: str | None = None,
    ) -> dict[str, Any]:
        """Submit an order.

        Args:
            pair: The trading pair.
            side: "buy" or "sell".
            ordertype: "market", "limit", "stop-loss", "take-profit", etc.
            volume: Order volume.
            price: Limit price (required for limit orders).
            validate: If True, validate without placing the order.
            timeinforce: "GTC" (good-til-cancelled), "IOC", "GTD".

        Returns:
            Dictionary with txid and order description.
        """
        data: dict[str, Any] = {
            "pair": pair,
            "type": side,
            "ordertype": ordertype,
            "volume": str(volume),
        }
        if price is not None:
            data["price"] = str(price)
        if validate:
            data["validate"] = "true"
        if timeinforce is not None:
            data["timeinforce"] = timeinforce

        result = self.transport.private("AddOrder", data) or {}
        _LOGGER.info(
            "Order submitted: %s %s %s %s @ %s (txid=%s)",
            side, volume, pair, ordertype, price or "market",
            result.get("txid"),
        )
        return result

    def cancel_order(self, txid: str) -> dict[str, Any]:
        """Cancel an open order.

        Args:
            txid: The transaction ID of the order to cancel.

        Returns:
            Dictionary with count of cancelled orders.
        """
        result = self.transport.private("CancelOrder", {"txid": txid}) or {}
        _LOGGER.info("Order cancelled: txid=%s, count=%s", txid, result.get("count"))
        return result

    def cancel_all(self) -> dict[str, Any]:
        """Cancel every open order.

        Returns:
            Dictionary with count of cancelled orders.
        """
        result = self.transport.private("CancelAll") or {}
        _LOGGER.info("All orders cancelled: count=%s", result.get("count"))
        return result

    def trades_history(
        self,
        start: int | None = None,
        end: int | None = None,
    ) -> dict[str, Trade]:
        """Return trade history.

        Args:
            start: Optional start timestamp.
            end: Optional end timestamp.

        Returns:
            Dictionary mapping transaction IDs to Trade objects.
        """
        data: dict[str, Any] = {}
        if start is not None:
            data["start"] = start
        if end is not None:
            data["end"] = end
        result = self.transport.private("TradesHistory", data) or {}
        return {
            txid: Trade.from_api(txid, trade)
            for txid, trade in result.get("trades", {}).items()
        }
