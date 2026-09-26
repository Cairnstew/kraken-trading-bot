"""Data loading for the RL training pipeline.

Turns Kraken OHLCV market data into the pandas DataFrames consumed by
the :class:`~kraken_trading_bot.rl.environment.TradingEnvironment`.

Pagination is implemented client-side here: :meth:`KrakenManager.ohlc`
already returns a ``(candles, last)`` cursor pair, so we loop on the
``since`` cursor until we have ``pages`` pages or the exchange says the
history is exhausted (``last == 0``).  This intentionally does not add
pagination to kraken-python itself; it lives in this package.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Sequence

import pandas as pd

from .features import FeaturePipeline, normalize_ticker_id

if TYPE_CHECKING:
    from kraken_api import KrakenManager

_LOGGER = logging.getLogger(__name__)

_OHLCV_COLUMNS = ("time", "open", "high", "low", "close", "vwap", "volume", "count")

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


def fetch_ohlc_dataframe(
    pair: str,
    interval: int,
    pages: int = 6,
    manager: "KrakenManager | None" = None,
    since: int | None = None,
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

    Returns:
        DataFrame as produced by :func:`candles_to_dataframe` with
        ``open/high/low/close/volume`` columns suitable for
        :class:`~kraken_trading_bot.rl.environment.TradingEnvironment`.

    Raises:
        NotEnoughDataError: If no candles could be fetched at all.
    """
    if pages < 1:
        raise ValueError(f"`pages` must be >= 1, got {pages}")
    if manager is None:  # deferred import: only needed for live fetching
        from kraken_api import KrakenManager

        manager = KrakenManager.from_env()

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

    if not collected:
        raise NotEnoughDataError(1, 0, what="OHLC candles")

    df = candles_to_dataframe(collected)
    # The exchange may return overlapping boundary candles across pages;
    # keep the first occurrence per timestamp.
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df


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
    "fetch_ohlc_dataframe",
    "prepare_episode",
    "_minimum_bars",
]