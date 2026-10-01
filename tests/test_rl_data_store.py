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

import logging

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
