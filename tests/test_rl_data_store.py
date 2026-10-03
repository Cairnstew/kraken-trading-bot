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

import json
import logging
from pathlib import Path

import pandas as pd
import pytest
from kraken_api.models import Candle

import numpy as np

from kraken_trading_bot.rl import (
    FeaturePipeline,
    SignalTickerMismatchError,
    fetch_ohlc_dataframe,
    prepare_episode,
    read_ohlc_dataframe,
)
from kraken_trading_bot.rl.data import (
    add_derived_ohlcv_features,
    candles_to_dataframe,
    merge_extra_features,
)

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


# The exact read contract: the eight OHLCV columns plus the three
# scalars derived from vwap/count at the read seam (Gap 1 widening).  The
# derived names are the last three, in derivation order.
_DERIVED_OHLCV_COLUMNS = ["vwap_dev", "trade_count_zscore_20", "volume_per_trade"]
_OHLCV_READ_CONTRACT = [
    "time", "open", "high", "low", "close", "vwap", "volume", "count"
] + _DERIVED_OHLCV_COLUMNS


def _assert_shape(df: pd.DataFrame) -> None:
    """The pipeline's exact read contract (UTC DatetimeIndex named time)."""
    assert list(df.columns) == _OHLCV_READ_CONTRACT
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
    # Both names must go, not just the package.  ``from market_data.store
    # import MarketDataStore`` resolves ``market_data.store`` straight out
    # of ``sys.modules`` when some earlier test in the same session already
    # imported it for real -- which is exactly what the sibling package
    # being importable in this repo's dev shell makes likely -- and a
    # ``None`` entry for ``market_data`` alone does NOT hide that cached
    # submodule.  Purging only the parent silently turned this assertion
    # into a test of the *root-status* refusal instead of the ImportError
    # one, i.e. an order-dependent test.  The expectation itself is
    # unchanged: the ImportError branch still has to name the package.
    monkeypatch.setitem(__import__("sys").modules, "market_data", None)
    monkeypatch.setitem(__import__("sys").modules, "market_data.store", None)
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


def test_read_ohlc_dataframe_social_features_merge(tmp_path) -> None:
    """Social-signal JSONL merges (hour-floor, ffill, zero-fill) onto OHLCV."""
    store = FakeStore([_candle(0), _candle(1)])
    source = FakeSource([_candle(2)], last=0)

    social = tmp_path / "social.jsonl"
    social.write_text(
        '{"timestamp": "2026-08-01T14:00:00Z", "stt_mention_count": 42, '
        '"stt_tilt": 0.8, "fng_index": 65}\n',
        encoding="utf-8",
    )

    df = read_ohlc_dataframe(
        "ETH/USD",
        60,
        pages=1,
        manager=source,
        social_features_file=str(social),
        market_data_store=store,
    )
    assert "stt_mention_count" in df.columns
    assert "stt_tilt" in df.columns
    assert "fng_index" in df.columns
    # 2026-08-01T14:00Z floors onto bar 2 (hours: 12, 13, 14).
    assert df["stt_mention_count"].iloc[-1] == 42
    assert df["stt_tilt"].iloc[-1] == pytest.approx(0.8)
    assert df["fng_index"].iloc[-1] == 65


def test_read_ohlc_dataframe_all_signal_files_merge(tmp_path) -> None:
    """News + funding + social signals all merge; all 9 signal columns present."""
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
    social = tmp_path / "social.jsonl"
    social.write_text(
        '{"timestamp": "2026-08-01T14:00:00Z", "stt_mention_count": 42, '
        '"stt_tilt": 0.8, "fng_index": 65}\n',
        encoding="utf-8",
    )

    df = read_ohlc_dataframe(
        "ETH/USD",
        60,
        pages=1,
        manager=source,
        extra_features_file=str(signals),
        funding_features_file=str(funding),
        social_features_file=str(social),
        market_data_store=store,
    )
    # All three signal sets present
    assert "sentiment_score" in df.columns
    assert "article_count" in df.columns
    assert "novelty_flag" in df.columns
    assert "funding_rate" in df.columns
    assert "basis" in df.columns
    assert "open_interest" in df.columns
    assert "stt_mention_count" in df.columns
    assert "stt_tilt" in df.columns
    assert "fng_index" in df.columns
    assert df["sentiment_score"].iloc[-1] == pytest.approx(0.7)
    assert df["funding_rate"].iloc[-1] == pytest.approx(0.0003)
    assert df["stt_mention_count"].iloc[-1] == 42
    assert df["stt_tilt"].iloc[-1] == pytest.approx(0.8)


# ----------------------------------------------------------------------
# merge seam: ticker filter, hour de-duplication, bounded carry,
# and absence-is-not-neutral.  All four are regressions for the signal
# join that every exogenous column travels through.
# ----------------------------------------------------------------------
def _ohlc_frame(bars: int = 6, start_hour: int = 12) -> pd.DataFrame:
    """A flat OHLCV frame of ``bars`` hourly bars from 2026-08-01T12:00Z."""
    index = pd.to_datetime(
        [f"2026-08-01T{hour:02d}:00:00Z" for hour in range(start_hour, start_hour + bars)],
        utc=True,
    )
    return pd.DataFrame(
        {
            "open": 1000.0,
            "high": 1000.0,
            "low": 1000.0,
            "close": 1000.0,
            "volume": 10.0,
        },
        index=index,
    )


def test_read_ohlc_dataframe_signal_ticker_mismatch_does_not_merge(tmp_path) -> None:
    """A BTC-only signal file cannot annotate ETH bars, with or without opt-in.

    This is the failure the merge used to commit silently: ``-0.9`` of BTC
    sentiment landing on an ETH frame with no error and no log.  The pair
    is threaded from the call site, so the guard works for every consumer,
    and ``signal_require_ticker: false`` remains a deliberate escape hatch
    for a file that is knowingly mixed.
    """
    store = FakeStore([_candle(0), _candle(1)])
    source = FakeSource([_candle(2)], last=0)

    signals = tmp_path / "btc.jsonl"
    signals.write_text(
        '{"ticker": "BTC/USD", "timestamp": "2026-08-01T14:00:00Z", '
        '"sentiment_score": -0.9}\n',
        encoding="utf-8",
    )

    with pytest.raises(SignalTickerMismatchError) as excinfo:
        read_ohlc_dataframe(
            "ETH/USD",
            60,
            pages=1,
            manager=source,
            extra_features_file=str(signals),
            market_data_store=store,
        )
    assert "ETH/USD" in str(excinfo.value)
    assert "BTCUSD" in str(excinfo.value)

    # Opting out merges (and says so) rather than refusing the read.
    df = read_ohlc_dataframe(
        "ETH/USD",
        60,
        pages=1,
        manager=source,
        extra_features_file=str(signals),
        signal_require_ticker=False,
        market_data_store=store,
    )
    assert df["sentiment_score"].iloc[-1] == pytest.approx(-0.9)


def test_merge_extra_features_filters_to_requested_ticker(tmp_path) -> None:
    """A multi-ticker file contributes only the requested ticker's records."""
    signals = tmp_path / "mixed.jsonl"
    signals.write_text(
        '{"ticker": "BTC/USD", "timestamp": "2026-08-01T14:00:00Z", '
        '"sentiment_score": -0.9}\n'
        '{"ticker": "ETH_USD", "timestamp": "2026-08-01T14:00:00Z", '
        '"sentiment_score": 0.6}\n'
        '{"ticker": "ETH/USD", "timestamp": "2026-08-01T12:00:00Z", '
        '"sentiment_score": 0.4}\n',
        encoding="utf-8",
    )

    df = merge_extra_features(_ohlc_frame(3), str(signals), ticker="ETH/USD")
    # 12:00 is our own record; 14:00 is ETH's (BTC's -0.9 never arrives,
    # and the underscore spelling of the same pair is not a second ticker).
    assert df["sentiment_score"].iloc[0] == pytest.approx(0.4)
    assert df["sentiment_score"].iloc[-1] == pytest.approx(0.6)
    assert df["signal_age_hours"].iloc[-1] == pytest.approx(0.0)
    assert bool(df["signal_observed"].iloc[-1]) is True


def test_merge_extra_features_untagged_file_still_merges(tmp_path, caplog) -> None:
    """A file with no ``ticker`` field is a one-ticker opt-in, not a failure.

    The eight merge tests above and the documented one-ticker cron all
    write untagged records; they must keep working, but loudly, because
    only a tagged file can be filtered per pair.
    """
    signals = tmp_path / "untagged.jsonl"
    signals.write_text(
        '{"timestamp": "2026-08-01T14:00:00Z", "sentiment_score": 0.5}\n',
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="kraken_trading_bot.rl.data"):
        df = merge_extra_features(_ohlc_frame(3), str(signals), ticker="ETH/USD")

    assert df["sentiment_score"].iloc[-1] == pytest.approx(0.5)
    assert any("no 'ticker' field" in rec.message for rec in caplog.records)


def test_merge_extra_features_duplicate_hour_does_not_raise(tmp_path) -> None:
    """The documented hourly-append cron re-emits the current hour.

    Every pull inside the same hour therefore re-appends that hour, which
    used to make the reindex raise ``ValueError: cannot reindex on an axis
    with duplicate labels`` and kill every train/backtest/paper tick.  The
    last write for an hour wins — the most recent pull.
    """
    signals = tmp_path / "hourly_append.jsonl"
    signals.write_text(
        '{"ticker": "ETH/USD", "timestamp": "2026-08-01T13:00:00Z", '
        '"sentiment_score": 0.1}\n'
        '{"ticker": "ETH/USD", "timestamp": "2026-08-01T13:30:00Z", '
        '"sentiment_score": 0.7}\n'
        '{"ticker": "ETH/USD", "timestamp": "2026-08-01T13:59:00Z", '
        '"sentiment_score": 0.9}\n',
        encoding="utf-8",
    )

    df = merge_extra_features(_ohlc_frame(3), str(signals), ticker="ETH/USD")
    assert df["sentiment_score"].iloc[1] == pytest.approx(0.9)  # 13:00 bar
    assert df["signal_age_hours"].iloc[1] == pytest.approx(0.0)
    assert df["signal_age_hours"].iloc[2] == pytest.approx(1.0)  # 14:00 bar
    assert list(df["sentiment_score"]) == [0.0, 0.9, 0.9]


def test_merge_extra_features_ffill_is_bounded_and_age_grows(tmp_path) -> None:
    """A reading is carried for a bounded number of hours, and says how old.

    One record at 12:00 used to propagate unchanged for the whole frame,
    so a 3-day-old funding rate was indistinguishable from a current one.
    The default bound is one bar of carry; ``signal_max_age_hours`` widens
    it and ``signal_age_hours`` reports the age of the reading that is
    being used.
    """
    signals = tmp_path / "one_record.jsonl"
    signals.write_text(
        '{"ticker": "ETH/USD", "timestamp": "2026-08-01T12:00:00Z", '
        '"funding_rate": 0.0001}\n',
        encoding="utf-8",
    )

    # Default: one hour of carry on hourly bars, then the value is gone.
    default = merge_extra_features(_ohlc_frame(6), str(signals), ticker="ETH/USD")
    assert list(default["funding_rate"]) == [0.0001, 0.0001, 0.0, 0.0, 0.0, 0.0]
    assert list(default["signal_age_hours"]) == [0.0, 1.0, -1.0, -1.0, -1.0, -1.0]
    assert list(default["signal_observed"]) == [True, True, False, False, False, False]

    # Configured bound: three hours of carry, and the age grows with it.
    widened = merge_extra_features(
        _ohlc_frame(6), str(signals), ticker="ETH/USD", max_age_hours=3
    )
    assert list(widened["funding_rate"]) == [0.0001, 0.0001, 0.0001, 0.0001, 0.0, 0.0]
    assert list(widened["signal_age_hours"]) == [0.0, 1.0, 2.0, 3.0, -1.0, -1.0]
    assert list(widened["signal_observed"]) == [True, True, True, True, False, False]

    # 0 hours: the reading only applies to the bar whose hour it is in.
    strict = merge_extra_features(
        _ohlc_frame(6), str(signals), ticker="ETH/USD", max_age_hours=0
    )
    assert list(strict["funding_rate"]) == [0.0001, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert list(strict["signal_observed"]) == [True, False, False, False, False, False]


def test_read_ohlc_dataframe_absence_is_distinguishable_from_zero(tmp_path) -> None:
    """``fng_index=0`` ("extreme fear") and "no record at all" must differ.

    The value column keeps its historical zero-fill so no NaN can reach
    the z-scored observation, so the freshness pair is what carries the
    distinction — which is only true if it reaches the frame on every read
    the store branch performs.
    """
    store = FakeStore([_candle(0), _candle(1), _candle(2)])
    source = FakeSource([_candle(3)], last=0)

    social = tmp_path / "social_zero.jsonl"
    social.write_text(
        '{"ticker": "ETH/USD", "timestamp": "2026-08-01T13:00:00Z", '
        '"fng_index": 0}\n',
        encoding="utf-8",
    )

    df = read_ohlc_dataframe(
        "ETH/USD",
        60,
        pages=1,
        manager=source,
        social_features_file=str(social),
        market_data_store=store,
    )
    # 12:00 = no record; 13:00 = a genuine reading of 0; 14:00 = that same
    # reading carried one bar (the default bound); 15:00 = nothing at all.
    # Every value is 0.0 — only the freshness pair tells them apart.
    assert list(df["fng_index"]) == [0.0, 0.0, 0.0, 0.0]
    assert list(df["signal_observed"]) == [False, True, True, False]
    assert list(df["signal_age_hours"]) == [-1.0, 0.0, 1.0, -1.0]


# ---------------------------------------------------------------------------
# Gap-1 widening: the store leg derives the same columns as the live leg
# ---------------------------------------------------------------------------
def _moving_candles(n: int = 120, seed: int = 17) -> list[Candle]:
    """Wandering OHLCV bars with a real vwap and a varying count.

    Both matter: a flat frame makes ``vwap_dev`` identically 0 and a
    constant count makes the trade-count z-score identically 0, which would
    let a broken derivation pass unnoticed.
    """
    rng = np.random.default_rng(seed)
    out: list[Candle] = []
    for i in range(n):
        close = 2000.0 + i * 1.5 + float(rng.normal(0.0, 3.0))
        vwap = close - float(rng.uniform(0.0, 1.5))
        out.append(
            Candle(
                pair="ETH/USD",
                time=_BASE + i * _HOUR,
                open=f"{close:.4f}",
                high=f"{close + 2.0:.4f}",
                low=f"{close - 2.0:.4f}",
                close=f"{close:.4f}",
                vwap=f"{vwap:.4f}",
                volume=f"{10.0 + (i % 7):.2f}",
                count=8 + (i % 11),
            )
        )
    return out


def test_store_read_leg_derives_the_ohlcv_scalars() -> None:
    """The market-data store read carries the derived columns too.

    The store persists ``vwap`` and ``count`` (the sibling's
    ``_OHLCV_COLUMNS`` matches ours), so the store leg must derive the
    same three scalars as the live leg -- otherwise the two paths drift
    and a store-backed model trains at a different width than a
    fetch-backed one.
    """
    candles = _moving_candles()
    df = read_ohlc_dataframe(
        "ETH/USD",
        60,
        pages=1,
        manager=FakeSource(candles),
        market_data_store=FakeStore(candles),
    )

    _assert_shape(df)
    for name in _DERIVED_OHLCV_COLUMNS:
        assert name in df.columns
        # Finite from the 20-bar warmup onward (the first rows are NaN by
        # construction, exactly like every other rolling indicator).
        tail = df[name].iloc[20:].to_numpy(dtype=float)
        assert np.isfinite(tail).all()
        assert np.unique(tail).size > 1, f"{name} is constant, not derived"

    expected = add_derived_ohlcv_features(candles_to_dataframe(candles))
    for name in _DERIVED_OHLCV_COLUMNS:
        np.testing.assert_allclose(
            df[name].to_numpy(dtype=float),
            expected[name].to_numpy(dtype=float),
            rtol=1e-12,
            equal_nan=True,
        )


def test_store_and_live_legs_derive_identical_columns() -> None:
    """Byte-identical derived columns from both read paths.

    ``read_ohlc_dataframe`` with a store and ``fetch_ohlc_dataframe`` must
    not be able to drift: the derivation sits in both return paths, and
    this pins that they agree.
    """
    candles = _moving_candles(60)

    via_store = read_ohlc_dataframe(
        "ETH/USD",
        60,
        pages=1,
        manager=FakeSource(candles),
        market_data_store=FakeStore(candles),
    )
    via_live = fetch_ohlc_dataframe(
        "ETH/USD", 60, pages=1, manager=FakeSource(candles)
    )

    assert list(via_store.columns) == list(via_live.columns)
    for name in _DERIVED_OHLCV_COLUMNS:
        np.testing.assert_allclose(
            via_store[name].to_numpy(dtype=float),
            via_live[name].to_numpy(dtype=float),
            rtol=1e-12,
            equal_nan=True,
        )


def test_derived_columns_survive_prepare_episode() -> None:
    """The derived columns reach the frame the environment is built from."""
    candles = _moving_candles()
    df = read_ohlc_dataframe(
        "ETH/USD",
        60,
        pages=1,
        manager=FakeSource(candles),
        market_data_store=FakeStore(candles),
    )

    pipe = FeaturePipeline(windows=[1, 4, 24])
    episode = prepare_episode(df, pipe, ticker_id="ETH_USD", episode_bars=60)

    for name in _DERIVED_OHLCV_COLUMNS:
        assert name in episode.columns

    stats = pipe.stats_for("ETH_USD")
    assert stats is not None
    for name in _DERIVED_OHLCV_COLUMNS:
        assert name in stats.feature_names


def test_missing_vwap_count_still_reads_at_the_narrower_width() -> None:
    """Presence-gated: a store frame without vwap/count computes narrower.

    Mirrors the pipeline-level width probe, but at the seam: the derived
    columns are simply absent, and nothing raises or warns about it.
    """
    candles = _moving_candles(60)
    for candle in candles:
        candle.vwap = ""

    df = fetch_ohlc_dataframe("ETH/USD", 60, pages=1, manager=FakeSource(candles))
    # vwap is parsed as NaN, so the column is present and vwap_dev is all
    # NaN -- the column set is unchanged, the values are not.
    assert "vwap_dev" in df.columns
    assert df["vwap_dev"].isna().all()
    assert np.isfinite(df["volume_per_trade"].to_numpy(dtype=float)).all()

    # count is always present on a real OHLCV frame, so drop it (and the
    # already-derived columns, which the seam put there) to see the
    # truly-absent path.
    raw = candles_to_dataframe(candles).drop(columns=["count"])
    without = add_derived_ohlcv_features(raw)
    assert "trade_count_zscore_20" not in without.columns
    assert "volume_per_trade" not in without.columns
    assert "vwap_dev" in without.columns


# ── F3: a run config cannot turn the store OFF (resolution order) ──────────
#
# Phase 6 finding F3. `_resolve_env_setting` skips a source whose value is
# `None`, because YAML `key: null` means "unset". So a run config
# (`--config`) saying `market_data_store: null` against a model trained
# with a store resolves to **the model's own store**, not to the live fetch.
# These tests pin that behaviour directly, because the error message now
# tells the reader to edit the MODEL's config.yaml and that advice has to
# be true.


def _resolve(name, env_kwargs, run_cfg, record_cfg, default=None):
    from kraken_trading_bot.rl.backtest import _resolve_env_setting

    return _resolve_env_setting(name, env_kwargs, run_cfg, record_cfg, default)


def test_a_run_config_null_cannot_turn_the_store_off_for_a_store_trained_model():
    """The behaviour the corrected error message now depends on.

    If this ever starts returning ``None`` (live), the message's advice
    would still be correct but this repo would have gained a way to turn
    the store off from a run file -- and, more importantly, the resolution
    order documented in `_resolve_env_setting` would be a lie.
    """
    assert (
        _resolve(
            "market_data_store",
            env_kwargs={},
            run_cfg={"market_data_store": None},
            record_cfg={"market_data_store": "/m/store"},
        )
        == "/m/store"
    )


def test_an_explicit_env_kwarg_does_override_the_models_store():
    """Source 1 is honoured even when it is ``None`` -- the one real escape."""
    assert (
        _resolve(
            "market_data_store",
            env_kwargs={"market_data_store": None},
            run_cfg={"market_data_store": "/run/store"},
            record_cfg={"market_data_store": "/m/store"},
        )
        is None
    )


def test_a_fresh_clone_with_no_model_config_still_goes_live():
    """The regression the F3 remedy could have caused, pinned explicitly.

    A fresh clone has no model config at all, so source 3 is absent and
    the shipped default (`null` -> live fetch) must survive untouched.
    """
    assert (
        _resolve(
            "market_data_store",
            env_kwargs={},
            run_cfg={},
            record_cfg={},
            default=None,
        )
        is None
    )
    # ...and an explicit run-config path still wins over the default.
    assert (
        _resolve(
            "market_data_store",
            env_kwargs={},
            run_cfg={"market_data_store": "/run/store"},
            record_cfg={},
            default=None,
        )
        == "/run/store"
    )


def test_the_full_precedence_order_is_still_what_the_message_claims():
    """env kwargs > run config > model config > default, strongest first."""
    record = {"market_data_store": "/m/store"}
    default = "/default/store"
    assert _resolve("market_data_store", {"market_data_store": "/env/store"}, {"market_data_store": "/run/store"}, record, default) == "/env/store"
    assert _resolve("market_data_store", {}, {"market_data_store": "/run/store"}, record, default) == "/run/store"
    assert _resolve("market_data_store", {}, {}, record, default) == "/m/store"
    assert _resolve("market_data_store", {}, {}, {}, default) == "/default/store"


# ---------------------------------------------------------------------------
# G-C activation: the news + social channels are now non-null in
# configs/default.yaml, so the seam has to carry all six of their columns
# (AC1), the widening has to be a DELTA and the freshness pair must not
# stack (AC2), and a fresh clone with no signal file must refuse loudly
# (AC7).  The merge mechanism itself is already covered above at lines
# 263/322/350/391/460/472/608 — only the ACTIVATION is new.
# ---------------------------------------------------------------------------

# The exact column set each channel contributes, and nothing else.  Kept as
# two literals rather than derived from the producer files so the guard
# states the contract instead of restating whatever the code happens to do.
_NEWS_COLUMNS = {"sentiment_score", "article_count", "novelty_flag"}
_SOCIAL_COLUMNS = {"stt_mention_count", "stt_tilt", "fng_index"}
_FRESHNESS_COLUMNS = {"signal_observed", "signal_age_hours"}


def _signal_jsonl(path, records) -> str:
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
    )
    return str(path)


def _hourly_records(bars: int, start_hour: int = 12) -> list[str]:
    return [f"2026-08-01T{hour:02d}:00:00Z" for hour in range(start_hour, start_hour + bars)]


def _news_file(tmp_path, bars: int = 6, start_hour: int = 12, **overrides) -> str:
    records = [
        {
            "ticker": "ETH/USD",
            "timestamp": ts,
            "sentiment_score": 0.1 * i,
            "article_count": 5 + i,
            "novelty_flag": bool(i % 2),
        }
        for i, ts in enumerate(_hourly_records(bars, start_hour))
    ]
    for record in records:
        record.update(overrides)
    return _signal_jsonl(tmp_path / "news.jsonl", records)


def _social_file(tmp_path, bars: int = 6, start_hour: int = 12, **overrides) -> str:
    records = [
        {
            "ticker": "ETH/USD",
            "timestamp": ts,
            "stt_mention_count": 10 + i,
            "stt_tilt": 0.25,
            "fng_index": 40 + i,
        }
        for i, ts in enumerate(_hourly_records(bars, start_hour))
    ]
    for record in records:
        record.update(overrides)
    return _signal_jsonl(tmp_path / "social.jsonl", records)


def _funding_file(tmp_path, bars: int = 6, start_hour: int = 12) -> str:
    """The real funding record shape, bid/ask included.

    bid/ask matter: they are `_SIGNAL_COLUMNS` members and the microstructure
    builder turns them into `spread`, so a funding file WITHOUT them measures a
    different width than the shipped one does.
    """
    records = [
        {
            "ticker": "ETH/USD",
            "timestamp": ts,
            "funding_rate": 0.0001,
            "basis": 0.0002,
            "open_interest": 5000.0,
            "funding_rate_prediction": 0.0003,
            "vol24h": 42000.5,
            "bid": 1000.0,
            "ask": 1000.4,
        }
        for ts in _hourly_records(bars, start_hour)
    ]
    return _signal_jsonl(tmp_path / "funding.jsonl", records)


# --- AC1: the seam carries all six columns, by name ----------------------
def test_news_merge_adds_exactly_the_three_news_columns(tmp_path) -> None:
    """AC1, by set equality: the columns ADDED are exactly these three.

    Not `len(added) == 3` — a rename that kept the count would pass that, and
    then `sentiment_score` would silently read 0.0 forever at unchanged
    width, which is the whole failure class this gap was found by.
    """
    base = _ohlc_frame(6)
    merged = merge_extra_features(base.copy(), _news_file(tmp_path), ticker="ETH/USD")

    added = set(merged.columns) - set(base.columns)
    missing = _NEWS_COLUMNS - added
    unexpected = added - _NEWS_COLUMNS - _FRESHNESS_COLUMNS
    assert not missing and not unexpected, (
        "merge did not add expected columns; "
        f"missing={missing}, unexpected={unexpected}"
    )
    # ...and the freshness pair rides along, because the same seam writes it.
    assert _FRESHNESS_COLUMNS <= added


def test_social_merge_adds_exactly_the_three_social_columns(tmp_path) -> None:
    """AC1 for the social channel, same set-equality shape."""
    base = _ohlc_frame(6)
    merged = merge_extra_features(base.copy(), _social_file(tmp_path), ticker="ETH/USD")

    added = set(merged.columns) - set(base.columns)
    missing = _SOCIAL_COLUMNS - added
    unexpected = added - _SOCIAL_COLUMNS - _FRESHNESS_COLUMNS
    assert not missing and not unexpected, (
        "merge did not add expected columns; "
        f"missing={missing}, unexpected={unexpected}"
    )


def test_the_news_producer_key_spelling_is_accepted(tmp_path) -> None:
    """The live producer writes `ETH_USD`; the config says `ETH/USD`.

    Measured on a real pull, so this is a pinned fact rather than a guess:
    `_canonical_ticker` is separator-free (`data.py:486-492`), so both
    spellings are the same ticker.  If that ever changes, the news channel
    would start raising `SignalTickerMismatchError` on every read, and this
    is where it should be noticed.
    """
    records = [
        {"ticker": "ETH_USD", "timestamp": ts, "sentiment_score": 0.5}
        for ts in _hourly_records(6)
    ]
    merged = merge_extra_features(
        _ohlc_frame(6), _signal_jsonl(tmp_path / "underscore.jsonl", records),
        ticker="ETH/USD",
    )
    assert merged["sentiment_score"].iloc[-1] == pytest.approx(0.5)


# --- AC2: the widening is a delta, and freshness does not stack -----------
def _width(frame: pd.DataFrame) -> int:
    """Observation width for this repo's shipped feature configuration.

    Mirrors `configs/default.yaml` (feature_windows [1,4,24], all five groups)
    and goes through `add_derived_ohlcv_features` first, because omitting that
    is the F-15 trap: the derived columns are computed at the read seam and
    the pipeline gates on their presence.
    """
    derived = add_derived_ohlcv_features(frame.copy().reset_index())
    derived = derived.set_index(derived.columns[0])
    pipe = FeaturePipeline(windows=[1, 4, 24])
    return int(pipe.compute(derived).shape[1])


def _width_fixture(bars: int = 200) -> pd.DataFrame:
    """200 synthetic hourly bars with a real vwap and a varying count.

    Flat closes make every technical indicator degenerate and a constant
    count makes the trade-count z-score identically 0, either of which lets
    a broken wiring pass unnoticed.
    """
    rng = np.random.default_rng(17)
    closes = 3000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.002, bars)))
    index = pd.date_range("2026-01-01T00:00:00Z", periods=bars, freq="1h")
    return pd.DataFrame(
        {
            "open": closes * 0.999,
            "high": closes * 1.004,
            "low": closes * 0.996,
            "close": closes,
            "vwap": closes * 1.0005,
            "volume": rng.uniform(100.0, 1000.0, bars),
            "count": rng.integers(50, 200, bars).astype(float),
        },
        index=index,
    )


def test_width_delta_equals_the_newly_reachable_signal_columns(tmp_path) -> None:
    """AC2: delta(funding -> funding+news+social) == newly-reachable count.

    The expected number is COMPUTED from the column sets, never hardcoded,
    and that is the whole point of this test.  Two different fixtures give
    two different right answers — 6 with the funding channel already active
    (the shipped default: funding has already introduced the freshness
    pair) and 8 without it (each channel costs 3 signal columns plus the
    freshness pair, and the pair does not stack) — so a literal 6 here would
    be a test that only passes on the configuration it was written against.

    For the same reason this never asserts an absolute width.  The absolute
    number is per-configuration (49–66 across everything measured this
    project) and depends on whether the frame happens to carry native
    bid/ask, so pinning it would pin a coincidence.
    """
    fixture = _width_fixture(200)
    # Built from the index, not from f"{h:02d}": 200 hourly stamps run past
    # midnight on day two, and "2026-01-01T24:00:00Z" is not a timestamp.
    hour = [ts.strftime("%Y-%m-%dT%H:%M:%SZ") for ts in fixture.index]

    def fund_records():
        return [
            {"ticker": "ETH/USD", "timestamp": ts, "funding_rate": 0.0001,
             "basis": 0.0002, "open_interest": 5000.0,
             "funding_rate_prediction": 0.0003, "vol24h": 42000.5,
             "bid": 3000.0, "ask": 3000.4}
            for ts in hour
        ]

    def news_records():
        return [
            {"ticker": "ETH/USD", "timestamp": ts, "sentiment_score": 0.1,
             "article_count": 5, "novelty_flag": False}
            for ts in hour
        ]

    def social_records():
        return [
            {"ticker": "ETH/USD", "timestamp": ts, "stt_mention_count": 10,
             "stt_tilt": 0.25, "fng_index": 40}
            for ts in hour
        ]

    funding = _signal_jsonl(tmp_path / "f.jsonl", fund_records())
    news = _signal_jsonl(tmp_path / "n.jsonl", news_records())
    social = _signal_jsonl(tmp_path / "s.jsonl", social_records())

    # --- the shipped-default axis: funding already active -----------------
    with_funding = merge_extra_features(
        fixture.copy(), funding, ticker="ETH/USD", max_age_hours=12
    )
    w_funding = _width(with_funding)
    all_three = with_funding
    for path in (news, social):
        all_three = merge_extra_features(
            all_three.copy(), path, ticker="ETH/USD", max_age_hours=12
        )
    w_all = _width(all_three)

    # Derive the expectation from the column sets, not from a literal.
    new_signal_columns = _NEWS_COLUMNS | _SOCIAL_COLUMNS
    already_present = set(with_funding.columns)
    expected = len(new_signal_columns - already_present) + len(
        _FRESHNESS_COLUMNS - already_present
    )
    delta = w_all - w_funding
    assert delta == expected, (
        "funding+news+social delta != number of newly reachable signal "
        f"columns; delta={delta}, expected={expected} "
        f"(baseline columns already carrying: "
        f"{sorted((new_signal_columns | _FRESHNESS_COLUMNS) & already_present)})"
    )

    # The freshness pair does not stack: three merges, one of each column.
    assert list(all_three.columns).count("signal_observed") == 1, (
        "signal_observed stacked across merges: "
        f"{list(all_three.columns).count('signal_observed')} copies"
    )
    assert list(all_three.columns).count("signal_age_hours") == 1, (
        "signal_age_hours stacked across merges: "
        f"{list(all_three.columns).count('signal_age_hours')} copies"
    )

    # --- the no-funding axis, to prove the expectation is really derived ---
    # Same news+social merge onto a frame with no funding at all: the
    # freshness pair is new here, so the expected delta is 8, not 6.  If the
    # test were hardcoding 6, this second half would fail; if the *merge* were
    # wrong, the first half would.
    bare = fixture.copy()
    for path in (news, social):
        bare = merge_extra_features(
            bare.copy(), path, ticker="ETH/USD", max_age_hours=12
        )
    # The baseline here is the same frame with no signal file at all.
    bare_delta = _width(bare) - _width(fixture.copy())
    bare_expected = len(_NEWS_COLUMNS | _SOCIAL_COLUMNS | _FRESHNESS_COLUMNS)
    assert bare_delta == bare_expected, (
        "without funding active the two channels cost their own 6 columns "
        f"PLUS the freshness pair; delta={bare_delta}, expected={bare_expected}"
    )


def test_all_six_signal_columns_are_allow_listed(tmp_path) -> None:
    """Both channels' columns survive `_SIGNAL_COLUMNS`, and none is a builder input.

    A name absent from the allow-list is dropped by the seam *before* the
    feature pipeline sees the frame; a name wrongly in
    `_SIGNAL_BUILDER_INPUT_COLUMNS` reaches the frame but never the
    observation.  Both are at unchanged width, so neither is visible to
    `check_feature_width`.
    """
    from kraken_trading_bot.rl.features import (
        _SIGNAL_BUILDER_INPUT_COLUMNS,
        _SIGNAL_COLUMNS,
    )

    assert _NEWS_COLUMNS <= set(_SIGNAL_COLUMNS)
    assert _SOCIAL_COLUMNS <= set(_SIGNAL_COLUMNS)
    assert _FRESHNESS_COLUMNS <= set(_SIGNAL_COLUMNS)
    assert not (_NEWS_COLUMNS | _SOCIAL_COLUMNS) & set(_SIGNAL_BUILDER_INPUT_COLUMNS)


# --- AC7: a non-null key with no file is a refusal, not a silent skip ----
def test_non_null_key_without_a_file_refuses(tmp_path) -> None:
    """AC7.  `signals/` is gitignored (.gitignore:65), so THIS is the fresh clone.

    Asserted as the *specific* class, not "raises": a bare FileNotFoundError
    from an unrelated open() would satisfy a generic `pytest.raises` while
    telling the operator nothing about which config key is at fault.  The
    message has to name the key, the value as written, and the expanded path.
    """
    from kraken_trading_bot.rl import SignalFileNotFoundError

    missing = tmp_path / "signals" / "eth_usd_news.jsonl"
    assert not missing.exists(), "the fixture must start from the fresh-clone state"

    with pytest.raises(SignalFileNotFoundError) as excinfo:
        merge_extra_features(_ohlc_frame(6), str(missing), ticker="ETH/USD",
                             config_key="extra_features_file")
    message = str(excinfo.value)
    assert "extra_features_file" in message
    assert str(missing) in message, "the EXPANDED path must be named, not the raw value"
    assert "--append" in message, (
        "the refusal should point at the producer command that creates the "
        f"file, which now carries --append: {message}"
    )


def test_the_shipped_default_declares_all_three_channels_non_null() -> None:
    """The activation itself: `configs/default.yaml` points at real files.

    Without this, the seam is perfect and nothing feeds it — which is exactly
    the gap: the merge tests above have always passed while both channels sat
    at `null` in the shipped config.
    """
    import yaml

    repo = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((repo / "configs" / "default.yaml").read_text())
    for key in ("extra_features_file", "funding_features_file", "social_features_file"):
        value = config[key]
        assert isinstance(value, str) and value, (
            f"{key} is {value!r}: a null key switches the channel off silently, "
            "which is how this gap stayed invisible"
        )
        assert value.endswith(".jsonl"), f"{key} must name a JSONL file, got {value!r}"


def test_the_shipped_default_channel_files_agree_with_the_timer_units() -> None:
    """The config's paths and the units' generated ExecStart are one spelling.

    Drift here is invisible until the timer fires: the unit would append to a
    file nothing reads, and the read would keep raising SignalFileNotFoundError
    against a file that is being filled perfectly.

    The unit is generated, so this generates it too — same `sed` the
    `news-timer` / `social-timer` recipes run, with the same default the
    recipe declares — rather than string-matching the template, whose
    `--output @OUTPUT@` says nothing about where the bytes land.
    """
    import re

    import yaml

    repo = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((repo / "configs" / "default.yaml").read_text())
    justfile = (repo / "justfile").read_text(encoding="utf-8")

    pairs = (
        ("extra_features_file", "news", "systemd/kraken-trading-bot-news.service.in"),
        (
            "social_features_file",
            "social",
            "systemd/kraken-trading-bot-social.service.in",
        ),
        (
            "funding_features_file",
            "funding",
            "systemd/kraken-trading-bot-funding.service.in",
        ),
    )
    for key, recipe, unit in pairs:
        # The output the *timer* recipe defaults to, i.e. what an operator who
        # ran `just <recipe>-timer` with no arguments actually gets.
        default = re.search(rf'^{recipe}-timer\b.*?output="([^"]+)"', justfile, re.M | re.S)
        assert default, f"justfile has no {recipe}-timer recipe with an output default"

        template = (repo / unit).read_text(encoding="utf-8")
        generated = template.replace("@TICKER@", "ETH/USD").replace(
            "@OUTPUT@", str(repo / default.group(1))
        )
        written = re.search(r"--output ([^\s]+)", generated)
        assert written, f"{unit} generates no --output path"

        configured = Path(config[key]).name
        assert Path(written.group(1)).name == configured, (
            f"{key} is {configured!r} but `just {recipe}-timer` generates a unit "
            f"that writes {Path(written.group(1)).name!r} — the timer would fill "
            "a file nobody reads"
        )
