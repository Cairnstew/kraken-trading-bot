"""Data models for the Kraken Trading Bot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass
class Ticker:
    """Current market ticker for a trading pair."""

    pair: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    volume_24h: Decimal
    vwap_24h: Decimal
    high_24h: Decimal
    low_24h: Decimal
    trades_24h: int
    timestamp: datetime

    @classmethod
    def from_api(cls, pair: str, data: dict[str, Any]) -> "Ticker":
        """Parse a Kraken API ticker response into a Ticker model."""
        return cls(
            pair=pair,
            bid=Decimal(data["b"][0]),
            ask=Decimal(data["a"][0]),
            last=Decimal(data["c"][0]),
            volume_24h=Decimal(data["v"][1]),
            vwap_24h=Decimal(data["p"][1]),
            high_24h=Decimal(data["h"][1]),
            low_24h=Decimal(data["l"][1]),
            trades_24h=int(data["t"][1]),
            timestamp=datetime.utcnow(),
        )


@dataclass
class Candle:
    """OHLC candle data for a trading pair."""

    pair: str
    time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    vwap: Decimal
    volume: Decimal
    count: int

    @classmethod
    def from_api(cls, pair: str, data: list) -> "Candle":
        """Parse a Kraken API OHLC candle array into a Candle model."""
        return cls(
            pair=pair,
            time=datetime.utcfromtimestamp(float(data[0])),
            open=Decimal(str(data[1])),
            high=Decimal(str(data[2])),
            low=Decimal(str(data[3])),
            close=Decimal(str(data[4])),
            vwap=Decimal(str(data[5])),
            volume=Decimal(str(data[6])),
            count=int(data[7]),
        )


@dataclass
class OrderBookLevel:
    """A single level in the order book."""

    price: Decimal
    volume: Decimal
    timestamp: float


@dataclass
class OrderBook:
    """Order book for a trading pair."""

    pair: str
    asks: list[OrderBookLevel]
    bids: list[OrderBookLevel]

    @classmethod
    def from_api(cls, pair: str, data: dict[str, Any]) -> "OrderBook":
        """Parse a Kraken API depth response into an OrderBook model."""
        asks = [
            OrderBookLevel(
                price=Decimal(level[0]),
                volume=Decimal(level[1]),
                timestamp=float(level[2]),
            )
            for level in data.get("asks", [])
        ]
        bids = [
            OrderBookLevel(
                price=Decimal(level[0]),
                volume=Decimal(level[1]),
                timestamp=float(level[2]),
            )
            for level in data.get("bids", [])
        ]
        return cls(pair=pair, asks=asks, bids=bids)

    @property
    def best_bid(self) -> OrderBookLevel | None:
        """Return the highest bid (best buy price)."""
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> OrderBookLevel | None:
        """Return the lowest ask (best sell price)."""
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        """Return the bid-ask spread."""
        if self.best_bid and self.best_ask:
            return self.best_ask.price - self.best_bid.price
        return None


@dataclass
class Balance:
    """Asset balance in the account."""

    asset: str
    balance: Decimal
    hold: Decimal

    @property
    def available(self) -> Decimal:
        """Return the available (not held) balance."""
        return self.balance - self.hold


@dataclass
class TradeBalance:
    """Trade balance summary for the account."""

    total_equity: Decimal
    margin_equity: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    margin_used: Decimal
    free_margin: Decimal
    margin_level: Decimal | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "TradeBalance":
        """Parse a Kraken API TradeBalance response into a TradeBalance model."""
        return cls(
            total_equity=Decimal(data.get("eb", "0")),
            margin_equity=Decimal(data.get("mh", "0")),
            unrealized_pnl=Decimal(data.get("n", "0")),
            realized_pnl=Decimal(data.get("c", "0")),
            margin_used=Decimal(data.get("m", "0")),
            free_margin=Decimal(data.get("mf", "0")),
            margin_level=Decimal(data["ml"]) if "ml" in data and data["ml"] else None,
        )


@dataclass
class Order:
    """A trading order."""

    txid: str
    pair: str
    side: str  # "buy" or "sell"
    ordertype: str  # "market", "limit", etc.
    volume: Decimal
    price: Decimal | None
    status: str
    opened: datetime | None
    closed: datetime | None

    @classmethod
    def from_api(cls, txid: str, data: dict[str, Any]) -> "Order":
        """Parse a Kraken API order response into an Order model."""
        descr = data.get("descr", {})
        return cls(
            txid=txid,
            pair=descr.get("pair", ""),
            side=descr.get("type", ""),
            ordertype=descr.get("ordertype", ""),
            volume=Decimal(data.get("vol", "0")),
            price=Decimal(descr.get("price", "0")) if descr.get("price") else None,
            status=data.get("status", ""),
            opened=_parse_timestamp(data.get("opentm")),
            closed=_parse_timestamp(data.get("closetm")),
        )


@dataclass
class Trade:
    """A completed trade."""

    txid: str
    pair: str
    side: str
    volume: Decimal
    price: Decimal
    cost: Decimal
    fee: Decimal
    timestamp: datetime

    @classmethod
    def from_api(cls, txid: str, data: dict[str, Any]) -> "Trade":
        """Parse a Kraken API trade response into a Trade model."""
        return cls(
            txid=txid,
            pair=data.get("pair", ""),
            side=data.get("type", ""),
            volume=Decimal(data.get("vol", "0")),
            price=Decimal(data.get("price", "0")),
            cost=Decimal(data.get("cost", "0")),
            fee=Decimal(data.get("fee", "0")),
            timestamp=_parse_timestamp(data.get("time")) or datetime.utcnow(),
        )


def _parse_timestamp(ts: Any) -> datetime | None:
    """Parse a Unix timestamp into a datetime object."""
    if ts is None:
        return None
    try:
        return datetime.utcfromtimestamp(float(ts))
    except (ValueError, TypeError):
        return None
