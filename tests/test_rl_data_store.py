"""Offline tests for the kraken-market-data integration seam.

Proves :func:`read_ohlc_dataframe` against the sibling store's read
contract (``upsert(pair, interval, candles)`` + ``read(pair, interval,
since, until) -> DataFrame``) without touching the network or importing
the sibling package: the store is a small in-memory shim with the exact
same shape semantics (UTC ``DatetimeIndex`` named ``time``, columns
``time, open, high, low, close, vwap, volume, count``, sorted oldest
first).  The path-branch error (sibling package not installed) is
exercised deterministically by removing ``market_data`` from
``sys.modules``.

None of these tests require pyarrow, the real kraken-market-data package,
or credentials.
"""

from __future__ import annotations

import pandas as pd
import pytest
from kraken_api.models import Candle

from kraken_trading_bot.rl import fetch_ohlc_dataframe, read_ohlc_dataframe

# 2026-08-01 12:00 UTC, the same anchor the sibling store's fixtures use.
_BASE = 1785585600
_HOUR = 3600


def _candle(i: int, close: float | None = None) -> Candle:
    """One hourly bar ``i`` hours after _BASE with a flat OHLCV."""
    ts = _BASE + i * _HOUR
    c = f"{1000.0 + i * 10.0 if close is None else close:.2f}"
    return Candle(
        pair="ETH/USD",
        time=ts,
        open=c,
        high=c,
        low=c,
        close=c,
        vwap=c,
        volume="10.5",
        count=5,
    )


class FakeSource:
    """Kraken-compatible source: returns a canned page and a cursor."""

    def __init__(self, candles: list[Candle], last: int | None = None) -> None:
        self.candles = candles
        self.last = last if last is not None else (max(int(c.time) for c in candles) if candles else _BASE)
        self.calls: list[int | None] = []

    def ohlc(self, pair: str, interval: int = 60, since: int | None = None) -> tuple[list[Candle], int]:
        self.calls.append(since)
        return self.candles, self.last


class FakeStore:
    """In-memory shim for ``MarketDataStore`` (the sibling read contract).

    Behaves like the real store: internally it holds RangeIndexed row frames
    (exactly how ``MarketDataStore.upsert`` merges parquet months — dedupe on
    the ``time`` column, keep last) and ``read`` filters ``since`` inclusive
    / ``until`` exclusive and builds the pipeline's UTC ``DatetimeIndex``
    shape as its final step, exactly like the real store's ``_frame_shape``.
    """

    _COLUMNS = ["time", "open", "high", "low", "close", "vwap", "volume", "count"]

    def __init__(self, candles: list[Candle] | None = None) -> None:
        self.rows = pd.DataFrame(columns=self._COLUMNS)
        self.upserted: list[Candle] = []
        if candles:
            self.rows = self._to_rows(candles).sort_values("time").reset_index(drop=True)

    def _to_rows(self, candles: list[Candle]) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "time": int(c.time),
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "vwap": float(c.vwap) if c.vwap else float("nan"),
                    "volume": float(c.volume) if c.volume else 0.0,
                    "count": int(c.count),
                }
                for c in candles
            ],
            columns=self._COLUMNS,
        )

    def upsert(self, pair: str, interval: int, candles: list[Candle]) -> int:
        self.upserted.extend(candles)
        incoming = self._to_rows(candles)
        merged = pd.concat([self.rows, incoming], ignore_index=True)
        merged = merged.drop_duplicates(subset="time", keep="last").sort_values("time")
        self.rows = merged.reset_index(drop=True)
        return len(incoming)

    def read(
        self,
        pair: str,
        interval: int,
        since: int | None = None,
        until: int | None = None,
    ) -> pd.DataFrame:
        df = self.rows
        if since is not None:
            df = df[df["time"] >= int(since)]
        if until is not None:
            df = df[df["time"] < int(until)]
        df = df.sort_values("time").reset_index(drop=True)
        df["time"] = df["time"].astype("int64")
        df["count"] = df["count"].astype("int64")
        for col in ("open", "high", "low", "close", "vwap", "volume"):
            df[col] = df[col].astype("float64")
        df.index = pd.to_datetime(df["time"], unit="s", utc=True)
        df.index.name = "time"
        return df[self._COLUMNS]


def _assert_shape(df: pd.DataFrame) -> None:
    """The pipeline's exact read contract (UTC DatetimeIndex named time)."""
    assert list(df.columns) == ["time", "open", "high", "low", "close", "vwap", "volume", "count"]
    assert df.index.name == "time"
    assert pd.api.types.is_datetime64_any_dtype(df.index) and df.index.tz is not None
    assert df["time"].dtype.kind in "iu"
    assert df["count"].dtype.kind in "iu"
    assert df["open"].dtype.kind == "f"


def test_read_ohlc_dataframe_null_store_falls_back_to_live_fetch() -> None:
    """market_data_store=None must be byte-identical to fetch_ohlc_dataframe."""
    candles = [_candle(0), _candle(1), _candle(2)]
    source = FakeSource(candles, last=0)

    via_adapter = read_ohlc_dataframe("ETH/USD", 60, pages=2, manager=source)
    via_fetch = fetch_ohlc_dataframe("ETH/USD", 60, pages=2, manager=source)

    pd.testing.assert_frame_equal(via_adapter, via_fetch)
    _assert_shape(via_adapter)


def test_read_ohlc_dataframe_reads_through_store_and_appends_window() -> None:
    """fetch -> upsert -> read: store history + the fetched window both come back."""
    # Two bars already in the store (deeper history than a fresh fetch).
    store = FakeStore([_candle(0), _candle(1)])
    # The source returns three *newer* bars (the "window" for this run).
    fresh = [_candle(2), _candle(3), _candle(4)]
    source = FakeSource(fresh, last=0)

    df = read_ohlc_dataframe(
        "ETH/USD", 60, pages=2, manager=source, market_data_store=store
    )

    _assert_shape(df)
    # 5 bars: 2 pre-existing + 3 appended. Sorted oldest first.
    assert list(df["time"]) == [1785585600 + i * _HOUR for i in range(5)]
    # The window landed in the store too (the "also appended" property).
    assert store.upserted == fresh
    assert len(store.read("ETH/USD", 60)) == 5
    # Index lines up with time (UTC).
    assert df.index[0] == pd.Timestamp("2026-08-01 12:00:00+00:00")


def test_read_ohlc_dataframe_honors_since_until_window() -> None:
    """since inclusive / until exclusive bounds the read window."""
    store = FakeStore([_candle(0), _candle(1), _candle(2), _candle(3), _candle(4)])
    source = FakeSource([], last=0)

    df = read_ohlc_dataframe(
        "ETH/USD",
        60,
        pages=1,
        manager=source,
        since=_BASE + _HOUR,      # bar 1 inclusive
        until=_BASE + 3 * _HOUR,  # bar 3 exclusive
        market_data_store=store,
    )
    assert list(df["time"]) == [_BASE + _HOUR, _BASE + 2 * _HOUR]


def test_read_ohlc_dataframe_empty_window_raises() -> None:
    """An empty store + a source that yields nothing keeps the old contract."""
    store = FakeStore([])
    source = FakeSource([], last=0)

    with pytest.raises(Exception, match="Not enough OHLC candles"):
        read_ohlc_dataframe(
            "ETH/USD", 60, pages=1, manager=source, market_data_store=store
        )


def test_read_ohlc_dataframe_path_branch_requires_sibling_package(monkeypatch) -> None:
    """A store *path* with the sibling package absent fails loudly, not silently."""
    monkeypatch.setitem(__import__("sys").modules, "market_data", None)
    with pytest.raises(ValueError, match="kraken-market-data"):
        read_ohlc_dataframe("ETH/USD", 60, market_data_store="/tmp/no-such-store")


def test_read_ohlc_dataframe_rejects_unknown_store_object() -> None:
    """Not-a-store values get a guiding TypeError instead of an AttributeError."""
    with pytest.raises(TypeError, match="market_data_store"):
        read_ohlc_dataframe("ETH/USD", 60, market_data_store=object())


def test_read_ohlc_dataframe_extra_features_still_merge(tmp_path) -> None:
    """Signals still merge onto the store-window frame (fetch-merge parity)."""
    store = FakeStore([_candle(0), _candle(1)])
    source = FakeSource([_candle(2)], last=0)

    signals = tmp_path / "signals.jsonl"
    signals.write_text(
        '{"timestamp": "2026-08-01T13:00:00Z", "sentiment_score": 0.5, '
        '"article_count": 3, "novelty_flag": true}\n',
        encoding="utf-8",
    )

    df = read_ohlc_dataframe(
        "ETH/USD",
        60,
        pages=1,
        manager=source,
        extra_features_file=str(signals),
        market_data_store=store,
    )
    assert "sentiment_score" in df.columns
    assert "article_count" in df.columns
    assert df["sentiment_score"].iloc[-1] == 0.5
    assert df["article_count"].iloc[-1] == 3


def test_read_ohlc_dataframe_funding_features_merge(tmp_path) -> None:
    """Funding-rate JSONL merges alongside news signals."""
    store = FakeStore([_candle(0), _candle(1)])
    source = FakeSource([_candle(2)], last=0)

    funding = tmp_path / "funding.jsonl"
    funding.write_text(
        '{"timestamp": "2026-08-01T14:00:00Z", "funding_rate": 0.0001, '
        '"basis": 0.0002, "open_interest": 5000.0}\n',
        encoding="utf-8",
    )

    df = read_ohlc_dataframe(
        "ETH/USD",
        60,
        pages=1,
        manager=source,
        funding_features_file=str(funding),
        market_data_store=store,
    )
    assert "funding_rate" in df.columns
    assert "basis" in df.columns
    assert "open_interest" in df.columns
    assert df["funding_rate"].iloc[-1] == pytest.approx(0.0001)
    assert df["basis"].iloc[-1] == pytest.approx(0.0002)


def test_read_ohlc_dataframe_both_signal_files_merge(tmp_path) -> None:
    """News + funding signals merge independently onto the OHLCV frame."""
    store = FakeStore([_candle(0), _candle(1)])
    source = FakeSource([_candle(2)], last=0)

    signals = tmp_path / "news.jsonl"
    signals.write_text(
        '{"timestamp": "2026-08-01T14:00:00Z", "sentiment_score": 0.7, '
        '"article_count": 5, "novelty_flag": false}\n',
        encoding="utf-8",
    )
    funding = tmp_path / "funding.jsonl"
    funding.write_text(
        '{"timestamp": "2026-08-01T14:00:00Z", "funding_rate": 0.0003, '
        '"basis": 0.0004, "open_interest": 8000.0}\n',
        encoding="utf-8",
    )

    df = read_ohlc_dataframe(
        "ETH/USD",
        60,
        pages=1,
        manager=source,
        extra_features_file=str(signals),
        funding_features_file=str(funding),
        market_data_store=store,
    )
    # Both signal sets present
    assert "sentiment_score" in df.columns
    assert "funding_rate" in df.columns
    assert df["sentiment_score"].iloc[-1] == pytest.approx(0.7)
    assert df["funding_rate"].iloc[-1] == pytest.approx(0.0003)