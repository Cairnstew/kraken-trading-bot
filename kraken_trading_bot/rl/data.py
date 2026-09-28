"""Data loading for the RL training pipeline.

Turns Kraken OHLCV market data into the pandas DataFrames consumed by
the :class:`~kraken_trading_bot.rl.environment.TradingEnvironment`.

Pagination is implemented client-side here: :meth:`KrakenManager.ohlc`
already returns a ``(candles, last)`` cursor pair, so we loop on the
``since`` cursor until we have ``pages`` pages or the exchange says the
history is exhausted (``last == 0``).  This intentionally does not add
pagination to kraken-python itself; it lives in this package.

Exogenous signal merging (:func:`merge_extra_features`) left-joins
per-(ticker, hour) signal vectors produced by the sibling
``ticker-news-signals`` project onto the OHLCV frame by floored
bar timestamp.

Local market-data store integration (:func:`read_ohlc_dataframe`): when the
``market_data_store`` config key points at the sibling ``kraken-market-data``
store root, loading becomes fetch -> ``upsert`` -> ``read`` — every window is
also appended to the store, and later reads can return deeper history than
Kraken's ~720-bar REST ceiling.  The store is imported lazily and only when
configured, so ``kraken-market-data`` stays an optional, duck-typed accent of
this package.  See ``kraken-market-data/INTEGRATION.md`` for the full
consumer contract.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

import pandas as pd

from .features import FeaturePipeline, normalize_ticker_id

if TYPE_CHECKING:
    from kraken_api import KrakenManager

_LOGGER = logging.getLogger(__name__)

_OHLCV_COLUMNS = ("time", "open", "high", "low", "close", "vwap", "volume", "count")

# Columns expected in signal JSONL records from sibling projects.
# News signals (ticker-news-signals): sentiment_score, article_count, novelty_flag
# Funding signals (kraken-funding-rates): funding_rate, basis, open_interest
_SIGNAL_COLUMNS = (
    "sentiment_score",
    "article_count",
    "novelty_flag",
    "funding_rate",
    "basis",
    "open_interest",
)

# Bars a 24-window feature pipeline needs before any indicator fills its
# look-back window (max window + a small return/rolling cushion).
_WARMUP_PAD = 6


class NotEnoughDataError(ValueError):
    """Raised when the available OHLCV bars are too few to train/backtest.

    Attributes:
        needed: Minimum number of bars required.
        available: Number of bars actually available.
    """

    def __init__(self, needed: int, available: int, what: str = "data") -> None:
        self.needed = needed
        self.available = available
        super().__init__(
            f"Not enough {what}: need at least {needed} bars "
            f"(max feature window + warmup), got {available}"
        )


def merge_extra_features(
    df: pd.DataFrame,
    extra_features_file: str | None = None,
) -> pd.DataFrame:
    """Merge exogenous per-(ticker, hour) signal vectors onto an OHLCV frame.

    Reads a JSONL file produced by the sibling ``ticker-news-signals``
    project — each record has a ``timestamp`` (ISO 8601, UTC) and the
    three signal columns: ``sentiment_score`` (float in [-1, 1]),
    ``article_count`` (int) and ``novelty_flag`` (bool).

    Both the OHLCV bar index and the signal timestamps are floor-truncated
    to the hour before joining, so the merge is robust to minor timestamp
    offsets (e.g. 1-minute candles with non-zero minutes).  Missing hours
    in the signal file are forward-filled and then zero-filled (neutral
    sentiment, 0 articles, no novelty) so every OHLCV row always has the
    three signal columns.

    Args:
        df: OHLCV DataFrame indexed by UTC ``DatetimeIndex`` (``time``).
        extra_features_file: Path to the signal JSONL; ``None`` or empty
            returns ``df`` unchanged.

    Returns:
        The input DataFrame with ``sentiment_score``, ``article_count``
        and ``novelty_flag`` columns added (when a file is provided and
        readable).
    """
    if not extra_features_file:
        return df

    path = Path(extra_features_file)
    if not path.is_file():
        _LOGGER.warning(
            "Extra features file not found: %s — skipping signal merge", path
        )
        return df

    # ── read JSONL ────────────────────────────────────────────────────
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                _LOGGER.warning("Skipping malformed JSONL line %d: %s", lineno, exc)

    if not records:
        _LOGGER.debug("Extra features file %s is empty — skipping merge", path)
        return df

    # ── build a small DataFrame from the signals ──────────────────────
    signal_df = pd.DataFrame(records)
    if "timestamp" not in signal_df.columns:
        _LOGGER.warning("No 'timestamp' column in %s — skipping merge", path)
        return df

    signal_df["timestamp"] = pd.to_datetime(signal_df["timestamp"], utc=True)
    signal_df = signal_df.set_index("timestamp")

    # Floor both indices to the hour so 60-min candles align cleanly
    # even if bar timestamps land at e.g. 14:01 due to exchange quirks.
    signal_df.index = signal_df.index.floor("h")

    # Keep only the three signal columns; ignore extras (ticker, etc.).
    available_cols = [c for c in _SIGNAL_COLUMNS if c in signal_df.columns]
    if not available_cols:
        _LOGGER.warning(
            "No signal columns found in %s — expected %s",
            path,
            _SIGNAL_COLUMNS,
        )
        return df

    signal_df = signal_df[available_cols].sort_index()

    # ── left-join onto the OHLCV frame ────────────────────────────────
    ohlc_index = df.index.floor("h")

    merged = signal_df.reindex(ohlc_index)

    # Forward-fill gaps (hours without new signals inherit the last known),
    # then zero-fill the leading NaNs (first bar onward with no signal yet).
    for col in available_cols:
        df[col] = merged[col].ffill().fillna(0.0).values

    _LOGGER.debug(
        "Merged %d signal records from %s onto %d OHLCV bars "
        "(columns: %s)",
        len(records),
        path,
        len(df),
        available_cols,
    )
    return df


def candles_to_dataframe(candles: Sequence[Any]) -> pd.DataFrame:
    """Convert a list of :class:`kraken_api.models.Candle` to a DataFrame.

    Args:
        candles: Candle objects with ``time`` (epoch seconds),
            ``open/high/low/close/vwap/volume`` (string prices) and
            ``count`` fields.

    Returns:
        DataFrame with columns ``time, open, high, low, close, vwap,
        volume, count`` (prices as float, ``time``/``count`` as int),
        indexed by ``pd.to_datetime(time, unit="s")`` and sorted oldest
        first.

    Raises:
        NotEnoughDataError: If ``candles`` is empty.
    """
    if not candles:
        raise NotEnoughDataError(1, 0, what="OHLC candles")
    rows = [
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
    ]
    df = pd.DataFrame(rows, columns=list(_OHLCV_COLUMNS))
    df = df.sort_values("time").reset_index(drop=True)
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    df.index.name = "time"
    return df


def _page_candles(
    pair: str,
    interval: int,
    pages: int,
    manager: Any,
    since: int | None,
) -> list[Any]:
    """Page through a Kraken-compatible source ``ohlc`` and collect candles.

    Follows the exchange's ``last`` cursor exactly as the loop in
    :func:`fetch_ohlc_dataframe` always has: each call to
    ``manager.ohlc(pair, interval=..., since=cursor)`` returns
    ``(candles, last)``, the next call passes ``since=last``, and the loop
    stops after ``pages`` pages, when the source reports ``last == 0``
    (history exhausted), or when a page returns no candles.  This is shared
    by the live fetch and the ``market-data`` store read-through adapter so
    the two paths can never drift apart on pagination semantics.

    Args:
        pair: Kraken pair notation, e.g. ``"ETH/USD"``.
        interval: Candle interval in minutes.
        pages: Maximum number of OHLC pages to fetch.
        manager: Anything exposing ``ohlc(pair, interval, since) ->
            (candles, last)`` (a ``KrakenManager`` or a duck-typed source).
        since: Optional start cursor for the first page (epoch seconds).

    Returns:
        The collected Candle-like objects (may be empty).
    """
    if pages < 1:
        raise ValueError(f"`pages` must be >= 1, got {pages}")
    collected: list[Any] = []
    cursor: int | None = since
    for _ in range(pages):
        batch, last = manager.ohlc(pair, interval=interval, since=cursor)
        if batch:
            collected.extend(batch)
        if last == 0 or not batch:
            _LOGGER.debug("OHLC history exhausted at cursor %s", cursor)
            break
        cursor = last
    return collected


def fetch_ohlc_dataframe(
    pair: str,
    interval: int,
    pages: int = 6,
    manager: "KrakenManager | None" = None,
    since: int | None = None,
    extra_features_file: str | None = None,
    funding_features_file: str | None = None,
) -> pd.DataFrame:
    """Page through Kraken OHLCV history into a training DataFrame.

    The loop follows the exchange's ``last`` cursor: each call to
    :meth:`KrakenManager.ohlc` returns ``(candles, last)``, and the next
    call passes ``since=last``.  The loop stops after ``pages`` pages,
    when the exchange reports ``last == 0`` (history exhausted), or when
    a page returns no candles.

    Args:
        pair: Kraken pair notation, e.g. ``"ETH/USD"``.
        interval: Candle interval in minutes (see
            :data:`kraken_api.manager.OHLC_INTERVALS`).
        pages: Maximum number of OHLC pages to fetch.
        manager: A :class:`kraken_api.KrakenManager`; defaults to
            ``KrakenManager.from_env()`` when omitted.
        since: Optional start cursor for the first page (epoch seconds).
        extra_features_file: Optional path to a JSONL of per-(ticker,
            hour) signal vectors (see :func:`merge_extra_features`); a
            feature-engineering seam joins them onto the OHLCV frame by
            floored bar timestamp. ``None`` (config default) disables it.
        funding_features_file: Optional path to a JSONL of per-(ticker,
            hour) funding-rate/basis vectors from the sibling
            ``kraken-funding-rates`` project.  Merged after news signals
            via the same timestamp-floor left-join.

    Returns:
        DataFrame as produced by :func:`candles_to_dataframe` with
        ``open/high/low/close/volume`` columns suitable for
        :class:`~kraken_trading_bot.rl.environment.TradingEnvironment`.

    Raises:
        NotEnoughDataError: If no candles could be fetched at all.
    """
    if manager is None:  # deferred import: only needed for live fetching
        from kraken_api import KrakenManager

        manager = KrakenManager.from_env()

    collected = _page_candles(pair, interval, pages, manager, since)

    if not collected:
        raise NotEnoughDataError(1, 0, what="OHLC candles")

    df = candles_to_dataframe(collected)
    # The exchange may return overlapping boundary candles across pages;
    # keep the first occurrence per timestamp.
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df = merge_extra_features(df, extra_features_file)
    df = merge_extra_features(df, funding_features_file)
    return df


def read_ohlc_dataframe(
    pair: str,
    interval: int,
    *,
    pages: int = 6,
    manager: "KrakenManager | None" = None,
    since: int | None = None,
    until: int | None = None,
    extra_features_file: str | None = None,
    funding_features_file: str | None = None,
    market_data_store: Any = None,
    market_data_source: Any = None,
) -> pd.DataFrame:
    """Read OHLCV through the local market-data store, or fall back to live.

    The integration seam for the sibling ``kraken-market-data`` project.
    When ``market_data_store`` is set, this performs **fetch -> upsert ->
    read**: the window is paged from Kraken exactly as
    :func:`fetch_ohlc_dataframe` does, appended into the store, and then
    read back — so the requested window is also persisted and later reads
    can return deeper history than Kraken's ~720-bar REST ceiling.  When
    ``market_data_store`` is unset (the config default, ``null``), it is a
    pass-through to :func:`fetch_ohlc_dataframe` and behaviour is
    byte-identical, which keeps the existing train/backtest/paper call
    sites working unchanged.

    Args:
        pair: Kraken pair notation, e.g. ``"ETH/USD"``.
        interval: Candle interval in minutes.
        pages: Maximum number of OHLC pages to fetch before the read.
        manager: A :class:`kraken_api.KrakenManager` for the fetch leg;
            defaults to ``KrakenManager.from_env()`` when omitted.
        since: Inclusive lower bound on the returned window (epoch seconds
            or ``None`` for the whole store).
        until: Exclusive upper bound on the returned window (epoch seconds
            or ``None`` for no bound).
        extra_features_file: Passed to :func:`merge_extra_features` on the
            read frame, exactly as in :func:`fetch_ohlc_dataframe`.
        market_data_store: ``null`` (live fetch), a store root path, or a
            store-like object exposing ``upsert``/``read`` (see
            :func:`_resolve_store`).
        market_data_source: Fetch source for the upsert leg; defaults to
            ``manager``.  Anything exposing ``ohlc(pair, interval, since)
            -> (candles, last)`` works (a live ``KrakenManager``, the
            store's own thin client, or a fake in tests).

    Returns:
        The same DataFrame shape :func:`fetch_ohlc_dataframe` returns
        (UTC ``DatetimeIndex``, columns ``time, open, high, low, close,
        vwap, volume, count``).

    Raises:
        ValueError: If a store *path* is configured but the sibling
            package is not importable.
        TypeError: If ``market_data_store`` is neither null, a path, nor a
            store-like object.
        NotEnoughDataError: If the read window is empty.

    .. todo:: Follow-up integration pass (see ``kraken-market-data/
       INTEGRATION.md``): honour ``since``/``until`` from config; drive
       train/eval split and walk-forward through the currently-unused
       ``TradingEnvironment.reset(options=...)``; let backtest skip the
       re-fetch and paper trade collapse to append + tail read.
    """
    if market_data_store is None:
        return fetch_ohlc_dataframe(
            pair,
            interval=interval,
            pages=pages,
            manager=manager,
            since=since,
            extra_features_file=extra_features_file,
            funding_features_file=funding_features_file,
        )

    store = _resolve_store(market_data_store)

    source = market_data_source if market_data_source is not None else manager
    if source is None:  # deferred import: only needed for live fetching
        from kraken_api import KrakenManager

        source = KrakenManager.from_env()

    # fetch -> upsert: page the window from the source and append it to the
    # store, so the read below covers both pre-existing history and the
    # just-fetched bars.
    candles = _page_candles(pair, interval, pages, source, since)
    if candles:
        store.upsert(pair, interval, candles)
        _LOGGER.info(
            "Upserted %d %d-minute %s bars into market-data store",
            len(candles),
            interval,
            pair,
        )
    else:
        _LOGGER.warning(
            "market-data store fetch returned no candles for %s/%d — "
            "reading whatever the store already has",
            pair,
            interval,
        )

    df = store.read(pair, interval, since=since, until=until)
    if isinstance(df, pd.DataFrame) and df.empty:
        raise NotEnoughDataError(1, 0, what="OHLC candles")
    df = merge_extra_features(df, extra_features_file)
    df = merge_extra_features(df, funding_features_file)
    return df


def _resolve_store(market_data_store: Any) -> Any:
    """Resolve the ``market_data_store`` config value to a usable store.

    Two shapes are accepted, both duck-typed against the sibling
    ``kraken-market-data`` contract (``upsert(pair, interval, candles)``
    plus ``read(pair, interval, since, until) -> DataFrame``):

    * a **path** (``str``/``os.PathLike``) to a store root — the sibling
      package is imported lazily and ``MarketDataStore(path)`` is built
      (raises a clear error if it is not installed);
    * a **store-like object** — used directly, which is what lets tests
      (and embedded callers) drive the seam without the sibling package.

    Returns:
        An object exposing the store's ``upsert``/``read`` contract.

    Raises:
        ValueError: If the sibling package is missing for a path config.
        TypeError: If the value is neither a path nor a store-like object.
    """
    upsert = getattr(market_data_store, "upsert", None)
    read = getattr(market_data_store, "read", None)
    if callable(upsert) and callable(read):
        return market_data_store
    if isinstance(market_data_store, (str, os.PathLike)):
        try:  # deferred import: optional accent, not a hard dependency
            from market_data.store import MarketDataStore
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ValueError(
                "market_data_store is configured but the sibling "
                "`kraken-market-data` package is not importable; install it "
                "or set market_data_store: null in config to use the live "
                "paginated fetch."
            ) from exc
        return MarketDataStore(Path(market_data_store))
    raise TypeError(
        "market_data_store must be a store root path, a MarketDataStore-like "
        "object with upsert()/read(), or None (live fetch); got "
        f"{type(market_data_store).__name__}"
    )


def prepare_episode(
    df: pd.DataFrame,
    features: FeaturePipeline,
    ticker_id: str | None = None,
    episode_bars: int | None = None,
) -> pd.DataFrame:
    """Fit features on OHLCV data and slice an episode-sized window.

    The :class:`TradingEnvironment` treats the DataFrame it is given as
    one episode (its ``reset(options=...)`` protocol is currently
    unused), so bounding episode length means bounding the DataFrame.
    This helper fits the pipeline's per-ticker normalization stats on
    the full frame, validates that enough data is present, then returns
    the trailing ``episode_bars`` bars (or the whole frame when
    ``episode_bars`` is None).

    Args:
        df: Raw OHLCV DataFrame (``open/high/low/close/volume``).
        features: Feature pipeline to fit; its per-ticker stats are
            registered under ``ticker_id``.
        ticker_id: Ticker the stats belong to (e.g. ``"ETH/USD"``).
        episode_bars: Number of trailing bars to slice into one episode;
            None (default) keeps the full frame.

    Returns:
        The episode window DataFrame to pass as ``data`` to a
        :class:`TradingEnvironment`.

    Raises:
        NotEnoughDataError: If the frame is empty/shorter than the
            pipeline's minimum length, or ``episode_bars`` would slice
            below that minimum.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise NotEnoughDataError(1, 0, what="OHLC bars")

    ticker_key = normalize_ticker_id(ticker_id) if ticker_id else ""
    features.fit(df, ticker_id=ticker_key)

    needed = _minimum_bars(features)
    available = len(df)
    if available < needed:
        raise NotEnoughDataError(needed, available, what="OHLC bars")

    if episode_bars is not None:
        if int(episode_bars) < needed:
            raise NotEnoughDataError(int(episode_bars), available, what="episode bars")
        window = df.tail(int(episode_bars))
    else:
        window = df

    _LOGGER.debug(
        "Prepared %s-bar episode for %s (full frame %d bars)",
        len(window),
        ticker_key or "unknown",
        available,
    )
    return window


def _minimum_bars(features: FeaturePipeline) -> int:
    """Minimum OHLC bars a pipeline can produce a useful episode from."""
    max_window = max(features.windows) if features.windows else 1
    return int(max_window) + _WARMUP_PAD


__all__ = [
    "NotEnoughDataError",
    "candles_to_dataframe",
    "merge_extra_features",
    "fetch_ohlc_dataframe",
    "read_ohlc_dataframe",
    "prepare_episode",
    "_minimum_bars",
    "_page_candles",
    "_resolve_store",
]