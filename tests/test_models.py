"""Tests for the data models."""

from decimal import Decimal
from datetime import datetime

from kraken_trading_bot.models import (
    Balance,
    Candle,
    Order,
    OrderBook,
    OrderBookLevel,
    Ticker,
    Trade,
    TradeBalance,
)


def test_ticker_from_api():
    """Test parsing a ticker response from the Kraken API."""
    data = {
        "a": ["50000.00", "1", "1.000"],  # ask
        "b": ["49999.00", "1", "1.000"],  # bid
        "c": ["50001.00", "0.001"],  # last
        "v": ["100.0", "1000.0"],  # volume
        "p": ["49950.0", "49980.0"],  # vwap
        "h": ["50500.0", "50500.0"],  # high
        "l": ["49500.0", "49500.0"],  # low
        "t": [1000, 5000],  # trades
    }

    ticker = Ticker.from_api("XBT/USD", data)

    assert ticker.pair == "XBT/USD"
    assert ticker.bid == Decimal("49999.00")
    assert ticker.ask == Decimal("50000.00")
    assert ticker.last == Decimal("50001.00")
    assert ticker.volume_24h == Decimal("1000.0")
    assert ticker.vwap_24h == Decimal("49980.0")
    assert ticker.high_24h == Decimal("50500.0")
    assert ticker.low_24h == Decimal("49500.0")
    assert ticker.trades_24h == 5000


def test_candle_from_api():
    """Test parsing a candle response from the Kraken API."""
    data = [1625097600, "50000", "50500", "49500", "50200", "50100", "100.5", 500]

    candle = Candle.from_api("XBT/USD", data)

    assert candle.pair == "XBT/USD"
    assert candle.open == Decimal("50000")
    assert candle.high == Decimal("50500")
    assert candle.low == Decimal("49500")
    assert candle.close == Decimal("50200")
    assert candle.vwap == Decimal("50100")
    assert candle.volume == Decimal("100.5")
    assert candle.count == 500


def test_order_book():
    """Test order book parsing and properties."""
    asks = [
        OrderBookLevel(price=Decimal("50001"), volume=Decimal("1.0"), timestamp=1.0),
        OrderBookLevel(price=Decimal("50002"), volume=Decimal("2.0"), timestamp=1.0),
    ]
    bids = [
        OrderBookLevel(price=Decimal("50000"), volume=Decimal("1.5"), timestamp=1.0),
        OrderBookLevel(price=Decimal("49999"), volume=Decimal("2.5"), timestamp=1.0),
    ]

    book = OrderBook(pair="XBT/USD", asks=asks, bids=bids)

    assert book.best_ask == asks[0]
    assert book.best_bid == bids[0]
    assert book.spread == Decimal("1")


def test_order_book_empty():
    """Test empty order book."""
    book = OrderBook(pair="XBT/USD", asks=[], bids=[])

    assert book.best_ask is None
    assert book.best_bid is None
    assert book.spread is None


def test_trade_balance_from_api():
    """Test parsing a trade balance response."""
    data = {
        "eb": "10000.00",  # total equity
        "mh": "10000.00",  # margin equity
        "n": "50.00",  # unrealized P&L
        "c": "100.00",  # realized P&L
        "m": "5000.00",  # margin used
        "mf": "5000.00",  # free margin
        "ml": "2.0",  # margin level
    }

    balance = TradeBalance.from_api(data)

    assert balance.total_equity == Decimal("10000.00")
    assert balance.margin_equity == Decimal("10000.00")
    assert balance.unrealized_pnl == Decimal("50.00")
    assert balance.realized_pnl == Decimal("100.00")
    assert balance.margin_used == Decimal("5000.00")
    assert balance.free_margin == Decimal("5000.00")
    assert balance.margin_level == Decimal("2.0")


def test_order_from_api():
    """Test parsing an order response."""
    data = {
        "descr": {
            "pair": "XBT/USD",
            "type": "buy",
            "ordertype": "limit",
            "price": "50000.00",
        },
        "vol": "0.01",
        "status": "open",
        "opentm": 1625097600,
        "closetm": None,
    }

    order = Order.from_api("TX123", data)

    assert order.txid == "TX123"
    assert order.pair == "XBT/USD"
    assert order.side == "buy"
    assert order.ordertype == "limit"
    assert order.volume == Decimal("0.01")
    assert order.price == Decimal("50000.00")
    assert order.status == "open"
    assert order.opened is not None
    assert order.closed is None
