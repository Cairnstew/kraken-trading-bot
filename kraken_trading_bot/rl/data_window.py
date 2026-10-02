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

**A pin with nothing behind it is a named error, and a result says which
kind of measurement it is.**  Two additions close the loop this module
opened:

:func:`evaluate_scope`
    Derives the in-sample/out-of-sample verdict from the **actual**
    halves, not from a flag a caller set -- so every artifact that
    reports a return has to carry the verdict it earned.  With the
    shipped default (``since``/``until`` null) the two halves are the
    same object and the verdict is ``IN-SAMPLE``, which is the honest
    reading of a backtest over the bars the model was fitted on.

:func:`training_frame` / :func:`evaluation_frame`
    Refuse a pin whose range misses every bar that exists, raising
    :class:`~kraken_trading_bot.rl.data.PinnedWindowUnavailableError`
    (a ``NotEnoughDataError``) that names the cause, the overlap that
    failed and the fix, instead of letting an empty slice reach
    ``prepare_episode`` and surface as a bare bar count.
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

# The two words every artifact carries.  Matched verbatim on
# ``IN-SAMPLE`` by the matrix harness (``tools/model_matrix.py``), so a
# cell summarised here and a cell summarised there read the same.
IN_SAMPLE_LABEL = "IN-SAMPLE"
OUT_OF_SAMPLE_LABEL = "OUT-OF-SAMPLE"


def _pinned_window_error() -> type[Exception]:
    """The pin-coverage error class, imported lazily.

    Deferred because ``data_window`` is deliberately dependency-free --
    pure pandas slicing, nothing fetched, nothing required -- and
    ``kraken_trading_bot.rl.data`` pulls in the whole feature package.
    ``data`` does not import this module, so there is no cycle to avoid;
    only the import cost, and only on the one error path.
    """
    from .data import PinnedWindowUnavailableError

    return PinnedWindowUnavailableError


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


@dataclass(frozen=True)
class EvaluationScope:
    """What kind of measurement a run's evaluation bars actually are.

    Every artifact that reports a return carries one of these, because
    "a backtest" says nothing about whether the bars it replayed were
    bars the model was fitted on.  With the shipped default
    (``since``/``until`` null) :func:`training_frame` and
    :func:`evaluation_frame` return the *same object*, so the return
    describes the fit and not a prediction -- and the only honest label
    for it is ``IN-SAMPLE``.

    A plain bool plus strings rather than an enum: it has to survive
    ``json.dumps`` in ``--json`` output and land as a CSV column in
    ``export-data``, and both want a scalar.

    Attributes:
        is_out_of_sample: True only when the window is pinned, the split
            applies, and the two halves came back **non-empty and
            genuinely disjoint**.  Every clause is load-bearing: a
            nominally pinned window that yields 0/0 bars has not earned
            an out-of-sample label, it has failed.
        label: ``"OUT-OF-SAMPLE"`` or ``"IN-SAMPLE"`` -- the word to
            print.
        reason: One sentence naming which clause decided it, so a reader
            can tell "you never pinned a window" from "you pinned one and
            it covered nothing".
        window_is_pinned: The window's own :attr:`DataWindow.is_pinned`.
        window_has_split: The window's own :attr:`DataWindow.has_split`.
        n_train_bars: Bars the training half holds.
        n_eval_bars: Bars the evaluation half holds.
        n_overlapping_bars: Bars the two halves share; must be ``0`` for
            an out-of-sample verdict.
    """

    is_out_of_sample: bool
    label: str
    reason: str
    window_is_pinned: bool
    window_has_split: bool
    n_train_bars: int
    n_eval_bars: int
    n_overlapping_bars: int = 0

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable view, for ``--json`` output."""
        return {
            "evaluation_is_out_of_sample": self.is_out_of_sample,
            "evaluation_scope_label": self.label,
            "evaluation_scope_reason": self.reason,
            "data_window_is_pinned": self.window_is_pinned,
            "data_window_has_split": self.window_has_split,
            "n_train_bars": self.n_train_bars,
            "n_eval_bars": self.n_eval_bars,
            "n_overlapping_bars": self.n_overlapping_bars,
        }


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

    Raises:
        PinnedWindowUnavailableError: If the pin leaves no bar at all --
            a subclass of ``NotEnoughDataError``, so existing handlers
            still catch it.
    """
    clipped = clip_to_window(df, window)
    if not window.has_split or len(clipped) < 2:
        _guard_pinned_coverage(df, clipped, window, "training")
        return clipped
    out = clipped.iloc[: split_index(len(clipped), window)]
    _guard_pinned_coverage(df, out, window, "training")
    return out


def evaluation_frame(df: pd.DataFrame, window: DataWindow) -> pd.DataFrame:
    """The bars a *backtest* replays: everything after the training split.

    Args:
        df: Frame indexed by bar time.
        window: Resolved window.

    Returns:
        The out-of-sample slice; the whole (clipped) frame when no split
        applies, so an unpinned backtest replays exactly what it always
        did.

    Raises:
        PinnedWindowUnavailableError: If the pin leaves no bar at all --
            a subclass of ``NotEnoughDataError``, so existing handlers
            still catch it.
    """
    clipped = clip_to_window(df, window)
    if not window.has_split or len(clipped) < 2:
        _guard_pinned_coverage(df, clipped, window, "evaluation")
        return clipped
    out = clipped.iloc[split_index(len(clipped), window) :]
    _guard_pinned_coverage(df, out, window, "evaluation")
    return out


def _bar_span(df: pd.DataFrame) -> str | None:
    """Human ``first .. last (n bars)`` for a frame, or ``None`` if empty.

    The available span an error needs to quote: a reader cannot tell why
    a pin missed without seeing what was there instead.
    """
    if df is None or len(df) == 0:
        return None
    index = df.index
    if not isinstance(index, pd.DatetimeIndex):
        try:
            index = pd.to_datetime(index, utc=True)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return f"<{len(df)} bars on a {type(df.index).__name__} index>"
        if index.tz is None:  # pragma: no cover - to_datetime(utc) sets tz
            index = index.tz_localize("UTC")
    return (
        f"{index[0].isoformat()} .. {index[-1].isoformat()} ({len(df)} bars)"
    )


def _guard_pinned_coverage(
    source: pd.DataFrame,
    sliced: pd.DataFrame,
    window: DataWindow,
    half: str,
) -> None:
    """Refuse a pin that overlaps nothing, naming the cause and the fix.

    Fires on exactly one condition -- **the window is pinned** *and* the
    resulting ``{half}`` slice is empty -- because that is the one
    "not enough data" state whose remedy is not "raise ``--pages``": the
    bars are not behind the requested window at all, so paging further
    into the trailing 721 can never reach them.

    It deliberately does **not** fire for an ordinary short frame.  A
    frame that overlaps the window but holds fewer bars than the warm-up
    needs keeps returning its (short) slice and keeps raising plain
    ``NotEnoughDataError`` further down, where "widen --pages" is the
    right advice; ``tests/test_market_data_store_seeding.py`` pins both
    behaviours.

    An **unpinned** window never fires here either, even on an empty
    frame: that is the pre-existing path and it already has its own named
    error one layer down.
    """
    if not window.is_pinned:
        return
    if sliced is not None and len(sliced) > 0:
        return

    n_available = 0 if source is None else len(source)
    if n_available == 0:
        reason = (
            "the pinned window is satisfiable in principle, but the read "
            "returned no bars at all"
        )
    else:
        reason = (
            "the pinned window is entirely outside the available bars -- "
            f"no {half} bar survives the clip"
        )
    raise _pinned_window_error()(
        reason,
        window=window.describe(),
        requested_range=_requested_range(window),
        available_span=_bar_span(source),
        n_available=n_available,
    )


def _requested_range(window: DataWindow) -> str:
    """The ``since``/``until`` a pin asked for, as one quotable phrase."""
    lower = "unbounded" if window.since is None else window.since.isoformat()
    upper = "unbounded" if window.until is None else window.until.isoformat()
    return f"[{lower}, {upper})"


def evaluate_scope(df: pd.DataFrame, window: DataWindow) -> EvaluationScope:
    """Is the evaluation slice genuinely out-of-sample?  Derive, don't ask.

    The verdict is computed from the two halves :func:`training_frame`
    and :func:`evaluation_frame` actually produce, so a caller cannot get
    an out-of-sample label by setting a flag -- and cannot get one by
    pinning a window that covers nothing.  Three clauses, all required:

    1. the window is **pinned** (``since``/``until`` set) -- otherwise
       the split is inert and both halves are the same object;
    2. the split **applies** (``eval_split`` below 1.0);
    3. both halves come back **non-empty and disjoint**.

    Clause 3 is the one that matters for honesty rather than mechanics: a
    pinned window over data that does not reach back that far yields
    0/0 bars, and that is a *failed* measurement, not an out-of-sample
    one.  :func:`training_frame` refuses that case outright, so this
    function sees it only if a caller handed it a short frame directly --
    and it still refuses to call it out-of-sample.

    Args:
        df: The full (pre-clip) frame the run read.
        window: Resolved window.

    Returns:
        The verdict, with the reason a reader needs to trust it.
    """
    train = training_frame(df, window)
    evaluation = evaluation_frame(df, window)

    n_train = 0 if train is None else len(train)
    n_eval = 0 if evaluation is None else len(evaluation)

    # `is` first: the unpinned default hands back one object, and
    # building two sets for 76k bars to rediscover that is not free.
    if train is not None and evaluation is not None and train is evaluation:
        n_overlap = n_eval
    else:
        try:
            n_overlap = len(set(train.index) & set(evaluation.index))
        except TypeError:  # pragma: no cover - unhashable index values
            n_overlap = 0

    scope = dict(
        window_is_pinned=window.is_pinned,
        window_has_split=window.has_split,
        n_train_bars=n_train,
        n_eval_bars=n_eval,
        n_overlapping_bars=n_overlap,
    )

    if not window.is_pinned:
        return EvaluationScope(
            is_out_of_sample=False,
            label=IN_SAMPLE_LABEL,
            reason=(
                "data_window.since and until are both null (the shipped "
                f"default), so the training and evaluation slices are the "
                f"SAME {n_eval} bar(s) -- this return describes the fit, "
                "not a prediction. Pin data_window.since/until to hold bars "
                "out."
            ),
            **scope,
        )

    if not window.has_split:
        return EvaluationScope(
            is_out_of_sample=False,
            label=IN_SAMPLE_LABEL,
            reason=(
                f"data_window is pinned to {window.describe()} but "
                f"eval_split={window.eval_split:g} disables the train/eval "
                f"split, so evaluation replayed the same {n_eval} bar(s) "
                "the model was fitted on. Set data_window.eval_split below "
                "1.0 to hold bars out."
            ),
            **scope,
        )

    if n_train == 0 or n_eval == 0:
        return EvaluationScope(
            is_out_of_sample=False,
            label=IN_SAMPLE_LABEL,
            reason=(
                f"data_window is pinned to {window.describe()} but clipped "
                f"to {n_train} training / {n_eval} evaluation bar(s) -- "
                "there is nothing held out, so this supports no "
                "out-of-sample claim."
            ),
            **scope,
        )

    if n_overlap > 0:
        return EvaluationScope(
            is_out_of_sample=False,
            label=IN_SAMPLE_LABEL,
            reason=(
                f"data_window is pinned to {window.describe()} but the "
                f"training and evaluation slices share {n_overlap} bar(s), "
                "so the return is measured partly over bars the model was "
                "fitted on."
            ),
            **scope,
        )

    return EvaluationScope(
        is_out_of_sample=True,
        label=OUT_OF_SAMPLE_LABEL,
        reason=(
            f"data_window pinned to {window.describe()} with "
            f"eval_split={window.eval_split:g}: evaluation replayed "
            f"{n_eval} bar(s) disjoint from the {n_train} training bar(s) "
            "the model was fitted on."
        ),
        **scope,
    )


__all__ = [
    "DataWindow",
    "DEFAULT_EVAL_SPLIT",
    "IN_SAMPLE_LABEL",
    "OUT_OF_SAMPLE_LABEL",
    "EvaluationScope",
    "clip_to_window",
    "evaluate_scope",
    "evaluation_frame",
    "resolve_data_window",
    "split_index",
    "training_frame",
]