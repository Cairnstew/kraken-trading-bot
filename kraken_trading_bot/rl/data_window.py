"""Data-window pinning and the train/eval split.

A single run of this pipeline cannot say whether a model is useful in the
real world, and the reason is not the model: ``train`` and ``backtest``
each fetch their own **fresh trailing** window.  Two fetches of the same
pair have been measured to disagree by 16% on the fitted std of
``rsi_24``, so a comparison across runs was comparing *data*, not
configs.

This module is the one place a window is pinned.  ``configs/default.yaml``
gains a ``data_window`` block::

    data_window:
      since: null    # ISO-8601 inclusive lower bound, or null
      until: null    # ISO-8601 exclusive upper bound, or null
      eval_split: 0.7

and both :func:`kraken_trading_bot.rl.train.train_ticker` and
:func:`kraken_trading_bot.rl.backtest.backtest_model` resolve it through
:func:`resolve_data_window` and slice through the helpers here.

Two properties are load-bearing and both are about *not changing the
default path*:

**Unpinned is inert.**  With ``since`` and ``until`` null — the shipped
default — :func:`clip_to_window` returns *the very same object it was
given*, and :func:`training_frame` / :func:`evaluation_frame` return the
whole frame.  Every pre-existing caller therefore takes the byte-identical
trailing-fetch path it took before this module existed, and ``eval_split``
is not applied at all: honouring a 0.7 split against an open-ended window
would silently train on 70% of the bars of every existing run, which is
the opposite of "do not change the default path".

**Pinned is shared.**  Once either bound is set, ``train`` takes the
leading ``eval_split`` fraction of the window and ``backtest`` takes the
remainder, so the two halves are disjoint and the backtest is genuinely
out-of-sample.  A backtest over the *training* bars is in-sample and is
the exact mistake this exists to make hard to commit by accident.

Nothing here is required and nothing is fetched: the bounds are applied as
a clip on the frame a caller already has, which is why the same helper
works for a fetched frame, a store-backed frame and a test fixture alike.
A pinned window therefore still needs ``--pages``/``eval_split`` to be
large enough to cover it; the callers raise a named error rather than
silently replaying a handful of bars.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

_LOGGER = logging.getLogger(__name__)

# Train fraction used when the config names ``data_window`` without one.
# Matches the value shipped in ``configs/default.yaml``; it is inert while
# the window is unpinned (see the module docstring).
DEFAULT_EVAL_SPLIT = 0.7


@dataclass(frozen=True)
class DataWindow:
    """One resolved ``data_window`` block.

    Attributes:
        since: Inclusive lower bound as a tz-aware UTC ``Timestamp``, or
            ``None`` for an open lower bound.
        until: Exclusive upper bound as a tz-aware UTC ``Timestamp``, or
            ``None`` for an open upper bound.
        eval_split: Fraction of the pinned window used for *training*;
            ``1.0`` disables the split.  Clamped into ``(0, 1]``.
    """

    since: pd.Timestamp | None = None
    until: pd.Timestamp | None = None
    eval_split: float = DEFAULT_EVAL_SPLIT

    @property
    def is_pinned(self) -> bool:
        """True when at least one bound is set, i.e. a window is pinned.

        This is the switch for the whole block: while it is False the
        frame is returned untouched and ``eval_split`` is not applied.
        """
        return self.since is not None or self.until is not None

    @property
    def has_split(self) -> bool:
        """True when a pinned window is actually divided train/eval.

        Requires :attr:`is_pinned` — an ``eval_split`` below 1.0 on an
        *unpinned* window is deliberately ignored, because applying it
        would change every existing run.
        """
        return self.is_pinned and self.eval_split < 1.0

    def describe(self) -> str:
        """One-line human summary for logs and error messages."""
        lower = "unbounded" if self.since is None else self.since.isoformat()
        upper = "unbounded" if self.until is None else self.until.isoformat()
        return f"[{lower}, {upper}) eval_split={self.eval_split:g}"


def _parse_bound(value: Any, key: str) -> pd.Timestamp | None:
    """Parse one ISO-8601 bound into a tz-aware UTC ``Timestamp``.

    Args:
        value: ``None``, an ISO-8601 string, a ``datetime``/``Timestamp``
            (naive values are read as UTC), or epoch seconds.
        key: Config key name, for the error message.

    Returns:
        The parsed timestamp, or ``None`` when ``value`` is ``None`` or
        empty.

    Raises:
        ValueError: If the value cannot be read as a timestamp.  Raised
            rather than warned: a mis-parsed bound would silently widen
            the window, which is precisely the "matrix compares data, not
            configs" failure this module exists to stop.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"data_window.{key}={value!r} is not an ISO-8601 timestamp "
            f"({exc}). Use e.g. '2026-01-01' or "
            f"'2026-01-01T00:00:00Z', or null for an open bound."
        ) from exc
    if pd.isna(ts):
        raise ValueError(f"data_window.{key}={value!r} parsed to NaT.")
    if ts.tzinfo is None:
        # The frames are UTC (the read seam builds a tz-aware index), so a
        # naive bound is read as UTC rather than as local time.
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


def _parse_split(value: Any) -> float:
    """Parse ``data_window.eval_split`` into a fraction in ``(0, 1]``.

    Raises:
        ValueError: If the value is not a number in ``(0, 1]``.
    """
    if value is None:
        return DEFAULT_EVAL_SPLIT
    if isinstance(value, bool):
        raise ValueError(f"data_window.eval_split={value!r} is not a fraction.")
    try:
        split = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"data_window.eval_split={value!r} is not a number; it is the "
            f"fraction of the pinned window used for training, in (0, 1]."
        ) from exc
    if not (split > 0.0) or split > 1.0:
        raise ValueError(
            f"data_window.eval_split={split!r} is out of range; it is the "
            f"fraction of the pinned window used for training, so it must "
            f"be in (0, 1] (1.0 means no split)."
        )
    return split


def resolve_data_window(config: Mapping[str, Any] | None) -> DataWindow:
    """Resolve the ``data_window`` block of a config into a
    :class:`DataWindow`.

    Args:
        config: A config dict shaped like ``configs/default.yaml``.  A
            missing key, a null value, or an absent config all resolve to
            the inert unpinned window.

    Returns:
        The resolved window.

    Raises:
        ValueError: If the block is not a mapping, a bound is unparseable,
            ``until`` is not after ``since``, or ``eval_split`` is out of
            range.
    """
    raw = (config or {}).get("data_window") or {}
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"data_window must be a mapping with since/until/eval_split, "
            f"got {type(raw).__name__}."
        )

    since = _parse_bound(raw.get("since"), "since")
    until = _parse_bound(raw.get("until"), "until")
    eval_split = _parse_split(raw.get("eval_split"))

    if since is not None and until is not None and until <= since:
        raise ValueError(
            f"data_window until ({until.isoformat()}) is not after since "
            f"({since.isoformat()}); `since` is inclusive and `until` is "
            f"exclusive, so an empty window pins nothing."
        )

    return DataWindow(since=since, until=until, eval_split=eval_split)


def _window_mask(index: pd.Index, window: DataWindow) -> pd.Series:
    """Boolean mask of the rows inside ``window`` (bounds applied inclusively).

    A non-datetime index is coerced to UTC-aware datetimes for the
    comparison only; the mask is positional, so the frame keeps whatever
    index it arrived with.
    """
    if not isinstance(index, pd.DatetimeIndex):
        try:
            comparable = pd.to_datetime(index, utc=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"A pinned data_window needs a datetime bar index, got "
                f"{type(index).__name__}: {exc}. Pin the window on a frame "
                f"read by data.read_ohlc_dataframe, or leave since/until "
                f"null."
            ) from exc
    else:
        comparable = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")

    mask = pd.Series(True, index=range(len(index)))
    if window.since is not None:
        mask &= comparable >= window.since
    if window.until is not None:
        mask &= comparable < window.until
    return mask


def clip_to_window(df: pd.DataFrame, window: DataWindow) -> pd.DataFrame:
    """Clip ``df`` to the pinned window; return it untouched when unpinned.

    ``since`` is **inclusive** and ``until`` is **exclusive**, which is
    what makes a half-open window join cleanly end-to-end without
    double-counting the bar that bounds both sides.

    Args:
        df: Frame indexed by bar time.
        window: Resolved window.

    Returns:
        ``df`` itself when :attr:`DataWindow.is_pinned` is False — the
        identity, not a copy, so an unpinned run is byte-identical to one
        made before this module existed — otherwise the masked rows.
    """
    if not window.is_pinned:
        return df
    if df is None or len(df) == 0:
        return df

    mask = _window_mask(df.index, window)
    clipped = df[mask.to_numpy()]
    if len(clipped) != len(df):
        _LOGGER.info(
            "data_window %s kept %d of %d bars (%s -> %s)",
            window.describe(),
            len(clipped),
            len(df),
            df.index[0] if len(df) else "-",
            clipped.index[-1] if len(clipped) else "-",
        )
    return clipped


def split_index(n_bars: int, window: DataWindow) -> int:
    """First eval-slice bar, i.e. the number of leading training bars.

    Args:
        n_bars: Length of the clipped window.
        window: Resolved window.

    Returns:
        ``n_bars`` when no split applies (unpinned window, ``eval_split``
        at 1.0, or fewer than two bars), otherwise ``round(n_bars *
        eval_split)`` clamped so at least one bar lands on each side.
    """
    if not window.has_split or n_bars < 2:
        return max(n_bars, 0)
    cut = int(round(n_bars * window.eval_split))
    return min(max(cut, 1), n_bars - 1)


def training_frame(df: pd.DataFrame, window: DataWindow) -> pd.DataFrame:
    """The bars a run *trains* on: the window's leading ``eval_split``.

    Fitting the pipeline on this slice rather than the whole pinned window
    is what makes the eval slice out-of-sample — otherwise the tail bars
    the normalization stats were fitted on are exactly the bars the
    backtest reports a return over.

    Args:
        df: Frame indexed by bar time.
        window: Resolved window.

    Returns:
        The training slice; the whole (clipped) frame when no split
        applies.
    """
    clipped = clip_to_window(df, window)
    if not window.has_split or len(clipped) < 2:
        return clipped
    return clipped.iloc[: split_index(len(clipped), window)]


def evaluation_frame(df: pd.DataFrame, window: DataWindow) -> pd.DataFrame:
    """The bars a *backtest* replays: everything after the training split.

    Args:
        df: Frame indexed by bar time.
        window: Resolved window.

    Returns:
        The out-of-sample slice; the whole (clipped) frame when no split
        applies, so an unpinned backtest replays exactly what it always
        did.
    """
    clipped = clip_to_window(df, window)
    if not window.has_split or len(clipped) < 2:
        return clipped
    return clipped.iloc[split_index(len(clipped), window) :]


__all__ = [
    "DataWindow",
    "DEFAULT_EVAL_SPLIT",
    "clip_to_window",
    "evaluation_frame",
    "resolve_data_window",
    "split_index",
    "training_frame",
]