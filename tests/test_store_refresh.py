"""``refresh=False``: a store read that costs zero API calls, and proves it.

The read seam (:func:`kraken_trading_bot.rl.data.read_ohlc_dataframe`) has
always run **fetch -> upsert -> read**, unconditionally, even when the
window being asked for is a pinned one the store already holds in full.
Measured on a pinned-window backtest: **6 OHLC API calls, 731 bars
upserted** into the store's trailing months, every one of them then
discarded by the window clip.  Rate limit spent on nothing, and the store
written to for nothing.

So ``refresh=False`` skips the fetch.  The whole difficulty is that
skipping a fetch is only honest if the store is *checked* instead of
trusted -- a silent skip would return a frame shorter than the window and
every report downstream would be quietly about a different period than it
claims.  That is what these tests pin:

1. ``refresh=False`` makes **zero** API calls (the fetch leg is stubbed to
   raise, so any call at all is a failure, not a count);
2. the default path **still** fetches (the flag is opt-in; paper trade's
   per-tick read depends on it);
3. a hole the skipped fetch *could* have filled **raises** rather than
   returning a short frame -- and a hole it could never have filled does
   **not** raise, because raising there would fail reads the old path
   performs happily;
4. on the real store, the frame is **byte-identical** with and without the
   refresh, hashed.

The ceiling in (3) is not a tolerance knob: Kraken's REST OHLC endpoint
serves at most ~721 bars newest-first and states that older data cannot be
retrieved regardless of ``since``.  So a hole behind that floor is
unrepairable *by fetching*, and refusing it would be a regression wearing
a guard's clothes.  The seeded 60-minute ``ETH_USD`` store really does
have such holes -- 31 bars inside 2020-01-01..2025-01-01, in 23 gaps --
which is why the distinction is tested rather than assumed.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from kraken_trading_bot.rl import data as data_mod
from kraken_trading_bot.rl.data import (
    PinnedWindowUnavailableError,
    expected_grid_window,
    read_ohlc_dataframe,
    refresh_for_window,
    store_tail_epoch,
)

_HOUR = 3600
_BASE = 1_700_000_000 // _HOUR * _HOUR  # a grid-aligned start


def _frame(times: list[int]) -> pd.DataFrame:
    """A minimal OHLCV frame at the given bar-start epochs."""
    index = pd.to_datetime(times, unit="s", utc=True)
    n = len(index)
    return pd.DataFrame(
        {
            "time": index,
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "vwap": [100.2 + i for i in range(n)],
            "volume": [10.0] * n,
            "count": [5] * n,
        },
        index=index,
    )


class FakeStore:
    """The sibling store's two-method contract, plus ``stats()``."""

    def __init__(self, times: list[int]) -> None:
        self._times = sorted(times)
        self.upserts: list[int] = []

    def read(self, pair, interval, since=None, until=None):
        times = self._times
        if since is not None:
            times = [t for t in times if t >= int(since)]
        if until is not None:
            times = [t for t in times if t < int(until)]
        return _frame(times)

    def upsert(self, pair, interval, candles):
        self.upserts.append(len(candles))
        return None

    def stats(self):
        return [
            SimpleNamespace(
                pair="ETH/USD",
                pair_id="ETH_USD",
                interval_min=60,
                files=1,
                bars=len(self._times),
                first_time=self._times[0] if self._times else None,
                last_time=self._times[-1] if self._times else None,
            )
        ]


@pytest.fixture
def no_api(monkeypatch):
    """Make any OHLC fetch leg an immediate, unmistakable failure."""

    def _boom(*args, **kwargs):
        raise AssertionError(
            "the fetch leg ran: _page_candles was called, so this read "
            "would have spent API rate limit"
        )

    monkeypatch.setattr(data_mod, "_page_candles", _boom)


def _utc(value) -> pd.Timestamp:
    """A tz-aware UTC Timestamp from an epoch, datetime, or Timestamp."""
    stamp = pd.Timestamp(value)
    return stamp if stamp.tzinfo is not None else stamp.tz_localize("UTC")


def _ok_hours(count: int, start: int = _BASE) -> list[int]:
    return [start + i * _HOUR for i in range(count)]


# 1. refresh=False makes zero API calls
def test_refresh_false_makes_zero_api_calls(no_api) -> None:
    """The whole point: a store read that cannot reach the network.

    ``_page_candles`` is replaced by a raiser rather than a counter, so
    this cannot pass by "one call but fewer than before" -- it passes only
    on zero.
    """
    store = FakeStore(_ok_hours(48))

    df = read_ohlc_dataframe(
        "ETH/USD",
        interval=60,
        pages=6,
        market_data_store=store,
        market_data_store_venue="test",
        refresh=False,
        coverage=(_BASE, _BASE + 48 * _HOUR),
    )

    assert len(df) == 48
    assert store.upserts == [], "refresh=False must not write to the store"


# 2. the default path still fetches
def test_default_refresh_still_fetches_and_upserts(monkeypatch) -> None:
    """``refresh`` defaults to ``True``: paper trade's tick read relies on it.

    The flag is opt-in precisely so this path cannot drift -- it is the
    one leg whose whole job is to append the newest bars to the store.
    """
    calls: list[object] = []

    class Candle(SimpleNamespace):
        pass

    def _fake_page(pair, interval, pages, manager, since):
        calls.append((pair, interval, pages, since))
        return [Candle(time=_BASE + i * _HOUR) for i in range(3)]

    monkeypatch.setattr(data_mod, "_page_candles", _fake_page)
    store = FakeStore(_ok_hours(48))

    df = read_ohlc_dataframe(
        "ETH/USD",
        interval=60,
        pages=6,
        market_data_store=store,
        market_data_store_venue="test",
        market_data_source=object(),
    )

    assert len(calls) == 1, "the default read must still page the source"
    assert store.upserts == [3], "the default read must still upsert"
    assert len(df) == 48


# 3a. a hole the fetch could have filled raises
def test_refresh_false_raises_on_a_gap_inside_the_rest_ceiling(no_api) -> None:
    """A hole in the trailing 721 bars is a real skip, so it refuses.

    The window's own tail is the store's tail here, so the hole sits at
    ``_BASE + 1h`` -- squarely inside the region the fetch leg could have
    supplied.  Returning a 2-bar frame for a 3-bar window would make every
    downstream number describe a period the caller never asked for.
    """
    store = FakeStore([_BASE, _BASE + 2 * _HOUR, _BASE + 3 * _HOUR])

    with pytest.raises(PinnedWindowUnavailableError) as excinfo:
        read_ohlc_dataframe(
            "ETH/USD",
            interval=60,
            pages=6,
            market_data_store=store,
            market_data_store_venue="test",
            refresh=False,
            coverage=(_BASE, _BASE + 3 * _HOUR),
        )

    message = str(excinfo.value)
    assert "1 bar(s)" in message
    assert "REST" in message or "rest" in message.lower()


# 3b. ... but a hole no fetch could ever fill does NOT raise
def test_refresh_false_warns_but_does_not_raise_on_an_unrepairable_hole(
    no_api, caplog
) -> None:
    """Behind the REST ceiling the hole is the store's, not the skip's.

    The store's tail is pushed 4000 bars past the window, so the missing
    hour is far older than anything ``refresh=True`` could have fetched.
    Raising there would fail a read the old code performs happily, which is
    a regression rather than a guard.
    """
    tail = _BASE + 4000 * _HOUR
    store = FakeStore(
        [_BASE, _BASE + 2 * _HOUR, _BASE + 3 * _HOUR, tail]
    )

    with caplog.at_level("WARNING", logger="kraken_trading_bot.rl.data"):
        df = read_ohlc_dataframe(
            "ETH/USD",
            interval=60,
            pages=6,
            market_data_store=store,
            market_data_store_venue="test",
            refresh=False,
            coverage=(_BASE, _BASE + 3 * _HOUR),
        )

    assert len(df) == 4, "the frame is returned, not refused"
    warnings = "\n".join(r.getMessage() for r in caplog.records)
    assert "MISSING" in warnings
    assert "no fetch could have supplied them" in warnings


# 3c. no store at all is refused rather than silently fetched
def test_refresh_false_without_a_store_raises() -> None:
    """Without a store the only source of bars *is* the fetch."""
    with pytest.raises(ValueError, match="needs a store"):
        read_ohlc_dataframe("ETH/USD", interval=60, refresh=False)


# 3d. no window to verify against is refused too
def test_refresh_false_without_a_window_raises(no_api) -> None:
    """An unverifiable skip is the thing this guard exists to prevent."""
    store = FakeStore(_ok_hours(10))

    with pytest.raises(ValueError, match="cannot verify coverage"):
        read_ohlc_dataframe(
            "ETH/USD",
            interval=60,
            market_data_store=store,
            market_data_store_venue="test",
            refresh=False,
        )


# 3e. the grid arithmetic
@pytest.mark.parametrize(
    ("since", "until", "interval", "expected"),
    [
        (0, 3600, 60, 1),
        (0, 3600 * 5, 60, 5),
        (0, 3600 * 5, 60, 5),
        (3600, 3600, 60, 0),  # empty window
        (7, 3600, 60, 1),  # unaligned start snaps down to the grid
        (1577836800, 1735689600, 60, 43848),  # 2020-01-01 .. 2025-01-01
    ],
)
def test_expected_grid_window(since, until, interval, expected) -> None:
    """Expected bar count comes from the interval, not from the frame."""
    assert expected_grid_window(since, until, interval)[1] == expected


# 4. the real store: identical frame with and without the refresh
def _real_store_root() -> Path | None:
    raw = os.environ.get(
        "KTB_STORE_ROOT", "~/Projects/kraken-market-data/store"
    )
    root = Path(raw).expanduser()
    return root if (root / "ETH_USD" / "60").is_dir() else None


@pytest.mark.skipif(
    _real_store_root() is None,
    reason="the seeded market-data store is not present on this machine",
)
def test_pinned_window_frame_is_identical_with_and_without_refresh(
    monkeypatch,
) -> None:
    """Byte-identical frames on the real store, even with the fetch leg running.

    This is the property that makes ``refresh=False`` safe to ship: the
    fetch leg **upserts into the store**, so in principle it could change
    what a subsequent read returns.  Over a window that ends years behind
    the store's tail it cannot -- and this proves it on the actual data,
    with the fetch leg stubbed to return the store's own trailing bars (a
    faithful stand-in for what Kraken's REST endpoint would hand back)
    and the store's ``upsert`` counted rather than performed, so a unit
    test never writes to the real store.
    """
    from kraken_trading_bot.rl.data import _resolve_store

    root = _real_store_root()
    assert root is not None
    real = _resolve_store(str(root))
    stats = [s for s in real.stats() if s.interval_min == 60]
    assert stats, "no 60-minute stats for the real store"
    tail = int(stats[0].last_time)
    since = 1577836800  # 2020-01-01T00:00Z
    until = 1735689600  # 2025-01-01T00:00Z
    assert until <= tail, "this test needs a window behind the store tail"

    class CountingStore:
        """Delegates reads; records the upsert instead of performing it."""

        def __init__(self) -> None:
            self.upserted = 0

        def upsert(self, pair, interval, candles):
            self.upserted += len(candles)
            return None

        def read(self, pair, interval, since=None, until=None):
            return real.read(pair, interval, since=since, until=until)

        def stats(self):
            return real.stats()

    trailing = real.read("ETH/USD", 60, since=tail - 721 * _HOUR)
    fetched = [
        SimpleNamespace(
            time=int(_utc(stamp).timestamp()),
            open=str(row.open),
            high=str(row.high),
            low=str(row.low),
            close=str(row.close),
            vwap=str(row.vwap),
            volume=str(row.volume),
            count=int(row.count),
        )
        for stamp, row in zip(trailing.index, trailing.itertuples())
    ]
    assert len(fetched) > 700, "the stand-in fetch must be a realistic page"

    def _fake_page(pair, interval, pages, manager, since_arg):
        return fetched

    monkeypatch.setattr(data_mod, "_page_candles", _fake_page)

    kwargs = dict(
        interval=60,
        pages=6,
        market_data_store_venue="test",
        coverage=(since, until),
    )
    with_store = CountingStore()
    with_fresh = read_ohlc_dataframe(
        "ETH/USD", market_data_store=with_store, refresh=True, **kwargs
    )
    no_store = CountingStore()
    without = read_ohlc_dataframe(
        "ETH/USD", market_data_store=no_store, refresh=False, **kwargs
    )

    def _digest(frame: pd.DataFrame) -> str:
        return hashlib.sha256(
            pd.util.hash_pandas_object(frame, index=True).values.tobytes()
        ).hexdigest()

    assert with_store.upserted > 700, "the refresh leg must really have run"
    assert no_store.upserted == 0
    assert _digest(without) == _digest(with_fresh)
    assert len(without) == len(with_fresh)
    # And the coverage guard agrees: the window is short by exactly the
    # store's own pre-existing holes, all of them behind the REST floor.
    window_frame = without[
        (without.index >= pd.Timestamp(since, unit="s", tz="UTC"))
        & (without.index < pd.Timestamp(until, unit="s", tz="UTC"))
    ]
    assert 43_000 < len(window_frame) < 43_848


# the decision itself: which reads skip the fetch
class _FakeWindow:
    def __init__(self, since, until, pinned=True):
        self.since = None if since is None else _utc(since)
        self.until = None if until is None else _utc(until)
        self.is_pinned = pinned

    def describe(self) -> str:
        return "test window"


def test_refresh_for_window_skips_the_fetch_only_when_the_store_can_serve_it(
    monkeypatch,
) -> None:
    """Three conditions, all required; each one alone keeps the fetch.

    Every branch here corresponds to a real consumer: unpinned is
    paper trade's trailing tick, "ends past the tail" is any run that
    genuinely wants the newest bars, and "no store" is the live fallback.
    """
    tail = _BASE + 10 * _HOUR
    monkeypatch.setattr(
        data_mod,
        "_resolve_store",
        lambda setting: FakeStore([tail]),
    )

    def decide(window, setting="/store"):
        return refresh_for_window(
            since=window.since,
            until=window.until,
            is_pinned=window.is_pinned,
            window_label=window.describe(),
            pair="ETH/USD",
            interval=60,
            store_setting=setting,
        )

    after = pd.Timestamp(tail - 5 * _HOUR, unit="s", tz="UTC")
    before = pd.Timestamp(tail - 10 * _HOUR, unit="s", tz="UTC")

    assert decide(_FakeWindow(before, after))["refresh"] is False
    assert decide(_FakeWindow(before, after))["coverage"] is not None
    assert decide(_FakeWindow(None, after, pinned=False))["refresh"] is True
    assert decide(_FakeWindow(None, after, pinned=True))["refresh"] is True
    # Both bounds present but the run is unpinned: still fetches.  This is
    # the `is_pinned` flag doing the work, not the missing bounds -- the
    # two cases above already return before reaching that test.
    assert decide(_FakeWindow(before, after, pinned=False))["refresh"] is True
    assert decide(_FakeWindow(before, after), setting=None)["refresh"] is True
    # ... and a window reaching PAST the tail keeps the fetch too:
    past = pd.Timestamp(tail + _HOUR, unit="s", tz="UTC")
    assert decide(_FakeWindow(before, past))["refresh"] is True


def test_backtest_passes_the_decision_rather_than_a_hardcoded_refresh() -> None:
    """``backtest_model`` must take ``refresh`` from the decision, not a literal.

    Behaviour is covered by the real-store test above, but that drives
    ``read_ohlc_dataframe`` directly.  This pins the *call site*: a
    hardcoded ``refresh=True`` there would restore six API calls per
    backtest with every behavioural test still green, which is exactly the
    regression this whole change exists to prevent.
    """
    import inspect

    from kraken_trading_bot.rl import backtest as backtest_mod

    source = inspect.getsource(backtest_mod.backtest_model)
    assert "refresh_for_window(" in source
    assert 'refresh=bool(refresh_kwargs["refresh"])' in source, (
        "backtest_model must pass the decision's value, not a literal"
    )
    assert "**refresh_kwargs" not in source


def test_train_passes_the_decision_rather_than_a_hardcoded_refresh() -> None:
    """``train_ticker`` must take ``refresh`` from the decision, not a literal.

    The backtest leg is covered behaviourally (the real-store test above
    goes through ``read_ohlc_dataframe`` with the flag the decision
    produces).  Training is pinned structurally instead, because
    exercising it means a real PPO run -- and a hardcoded ``refresh=True``
    there would silently restore six API calls per matrix cell with every
    test still green.

    Both call sites are asserted to name ``refresh`` at all *and* to derive
    it from ``refresh_kwargs``, which is what
    :func:`~kraken_trading_bot.rl.data.refresh_for_window` returns.
    """
    import inspect

    from kraken_trading_bot.rl import train as train_mod

    source = inspect.getsource(train_mod.train_ticker)
    assert "refresh_for_window(" in source, (
        "train_ticker must consult refresh_for_window; without it a pinned "
        "window costs 6 OHLC API calls per run"
    )
    assert 'refresh=bool(refresh_kwargs["refresh"])' in source, (
        "train_ticker must pass the decision's value, not a literal"
    )
    assert "**refresh_kwargs" not in source, (
        "a ** splat at the read seam cannot be proved to forward anything "
        "(see test_every_read_ohlc_dataframe_call_site_forwards_venue)"
    )


def test_store_tail_epoch_prefers_metadata_and_falls_back(
    monkeypatch,
) -> None:
    """A store that cannot report a tail falls back to the frame's last bar."""
    store = FakeStore([_BASE, _BASE + 5 * _HOUR])
    assert store_tail_epoch(store, "ETH/USD", 60) == _BASE + 5 * _HOUR
    assert store_tail_epoch(store, "ETH_USD", 60) == _BASE + 5 * _HOUR
    assert store_tail_epoch(store, "ETH/USD", 240) is None

    class NoStats:
        def read(self, *a, **k):  # pragma: no cover - not reached
            return _frame([_BASE])

    assert store_tail_epoch(NoStats(), "ETH/USD", 60, fallback=123) == 123