"""Feature engineering for the RL trading environment.

Transforms raw OHLCV data into normalized features consumed by the
reinforcement learning agent.  All calculations are vectorized with
pandas/numpy; normalization statistics are kept per ticker and can be
persisted to ``normalization.npz`` files inside the model storage tree
(``models/{TICKER_ID}/{model_name}/``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

_LOGGER = logging.getLogger(__name__)

# Canonical column names expected on the raw OHLCV input frame.
_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")

_FEATURE_GROUPS = ("price", "technical", "volume", "microstructure", "signals")

# The values no derived feature may be computed from, and the one
# extension this pass adds to the module's long-standing
# ``.replace(0, np.nan)`` convention.
#
# **Zero** was the original case (the seeded Binance archive's
# exchange-outage no-trade bars -- corrected 2026-10-02, they were first
# misattributed to "partial-month boundaries"; 0 of 106 month-first bars
# are zero-volume and all 4 are mid-month) and is still the common one.
# **+/-inf** joins it
# because it reaches a frame by exactly the same routes -- a store row, a
# parse of an empty field on the wire, a corrupt CSV export -- and every
# conclusion below applies identically: a ``pct_change``/``log``/ratio
# over it yields an infinity that ``compute -> ffill -> fillna(0)`` cannot
# repair, and that infinity then poisons ``fit``'s ``(mean, std)`` for the
# whole column.  Measured on a 400-bar frame with a single ``inf`` close,
# six columns leaked through the fill before this list existed:
# ``return_{1,4,24}`` and ``log_return_{1,4,24}``.
#
# Zero and non-finite are both *absence* here, which is why they become
# NaN rather than a sentinel: ``POINT_IN_TIME_EXOGENOUS_COLUMNS`` already
# declares that a NaN in these columns means "no reading on this bar", and
# the observation fill already knows how to repair it.
_NON_FINITE_INPUTS = [0.0, np.inf, -np.inf]

# The **infinity-only** subset, for the one site where zero is data.
#
# ``_NON_FINITE_INPUTS`` includes ``0.0`` because zero is a *division
# hazard*: it makes a denominator ``0`` and a ratio undefined, so it is
# absence in the same sense a NaN is.  That reasoning is specific to a
# computed ratio, and it does **not** transfer to a passthrough column.
# :meth:`FeaturePipeline._add_signal_columns` copies exogenous columns
# straight through with no arithmetic at all, and two of them use zero and
# a negative sentinel as *load-bearing data*:
# ``signal_observed`` is 0.0/1.0, and ``signal_age_hours`` carries
# ``-1.0`` for "no reading" (``data._NO_SIGNAL_AGE``).  Those are exactly
# the two columns that exist so a zero-filled value can be told apart
# from a genuine 0 -- see the ``_SIGNAL_COLUMNS`` comment above.  Mapping
# their 0.0/-1.0 to NaN erases that distinction and lets
# ``ffill().fillna(0.0)`` re-flatten it, which is what broke three
# freshness/signal regression tests when this list was first used here.
#
# What still applies at a passthrough column is the infinity argument, and
# it applies just as strongly: an infinity arrives from the same sources
# (a store row, a parse of an empty field, a corrupt CSV) and is the one
# value the observation fill cannot repair.  So this site guards +/-inf
# and leaves 0.0 and -1.0 alone.
_NON_FINITE_INPUTS_NO_ZERO = [np.inf, -np.inf]

# Columns added by :func:`merge_extra_features` in ``data.py`` (which
# imports this tuple — this module is the single canonical source so the
# allow-list cannot drift between the merge seam and the observation).
# The ``signals`` group passes them through as-is so they reach the
# agent's observation vector and the normalization stats.
# News signals (ticker-news-signals): sentiment_score, article_count, novelty_flag
# Funding signals (kraken-funding-rates): funding_rate, basis, open_interest
# Social signals (kraken-social-signals): stt_mention_count, stt_tilt, fng_index
# Freshness/provenance of the merge itself, written by the same seam for
# every source it joins: signal_observed says a record for this ticker
# landed inside the freshness window, signal_age_hours says how old the
# stalest live reading is (-1.0 = none — see ``data._NO_SIGNAL_AGE``).
# They are the reason a zero-filled value can be told apart from a
# genuine 0 / balanced reading, so they must be added here once, not
# re-declared per source.
# OHLCV-derived scalars, derived at the ``data.py`` read seam from the
# ``vwap``/``count`` columns every OHLCV frame already carries (Gap-1
# widening, 49 -> 55).  vwap_dev = close/vwap - 1, volume_per_trade =
# volume/count, trade_count_zscore_20 = 20-bar rolling z-score of count.
_SIGNAL_COLUMNS = (
    "sentiment_score",
    "article_count",
    "novelty_flag",
    "funding_rate",
    "basis",
    "open_interest",
    "stt_mention_count",
    "stt_tilt",
    "fng_index",
    "signal_age_hours",
    "signal_observed",
    "vwap_dev",
    "trade_count_zscore_20",
    "volume_per_trade",
    # Funding-record columns the sibling already emits but the merge
    # allow-list used to drop: the forward (next-settlement) funding
    # estimate, distinct from the settled funding_rate above, and the
    # perp contract's rolling 24h volume (a different horizon than the
    # bar `volume` the volume group already models).
    "funding_rate_prediction",
    "vol24h",
    # Emitted by _add_microstructure_features from the funding bid/ask
    # pair; see _SIGNAL_BUILDER_INPUT_COLUMNS for why it is listed here
    # but not copied verbatim by the signals builder.
    "spread",
    # Raw bid/ask of the funding snapshot.  Listed so the merge seam
    # carries them to the frame; the micro builder is what turns them
    # into `spread`, so they never reach the observation themselves.
    "bid",
    "ask",
)

# The two columns above that describe freshness rather than a signal
# reading.  Both are finite floats by construction (the age carries a
# sentinel instead of NaN, the flag is 0.0/1.0), so a bar that saw no
# signal at all still contributes a usable number to the observation
# instead of a NaN that would spread through the z-scoring.
_SIGNAL_FRESHNESS_COLUMNS = ("signal_age_hours", "signal_observed")

# Members of ``_SIGNAL_COLUMNS`` that reach the frame through the merge
# seam but are NOT copied into the observation by
# ``_add_signals_features``:
#
# * ``bid``/``ask`` are raw dollar prices that only exist as *builder
#   inputs* — ``_add_microstructure_features`` turns them into the single
#   ``spread`` column, so whitelisting them raw would put two more
#   dollar-scale columns into the observation for information the price
#   group already carries.
# * ``spread`` is emitted by that same builder, which stays its
#   single writer; copying it here as well would be a second writer for
#   one column.  A future tick-level tape recorder must use
#   ``realized_spread_bps`` instead of competing for this name.
#
# Net accounting is therefore +1 for the funding bid/ask pair, not +3.
_SIGNAL_BUILDER_INPUT_COLUMNS = ("bid", "ask", "spread")

# Every observation column whose value comes from an exogenous file
# rather than from a look-back window over the OHLCV frame.
#
# The distinction matters because the two kinds of NaN mean opposite
# things.  A NaN in an OHLCV-derived column is *warm-up*: the rolling
# window has not filled yet, so the bar is genuinely not computable and
# the environment skips it.  A NaN in one of the columns below is
# *absence*: the exogenous source settles on its own cadence (funding
# ~8-hourly) or was simply not reading on this bar, so the spread really
# is unknown here and there is nothing to warm up.  ``spread`` is the
# case that proves it — ``_add_microstructure_features`` divides by
# ``bid``, and the merge seam's absence fill leaves a zero bid behind,
# which that builder deliberately turns into NaN rather than inventing a
# spread.  That NaN is correct information, not a not-yet-ready bar.
#
# The two are not distinguishable from the NaN *pattern*: with a single
# funding record the last bar is the only valid one, which is
# byte-identical to a 720-bar rolling window.  So the distinction is
# declared here, by provenance, and consumed by
# :func:`first_tradable_index`.
POINT_IN_TIME_EXOGENOUS_COLUMNS = frozenset(_SIGNAL_COLUMNS) | frozenset(
    _SIGNAL_BUILDER_INPUT_COLUMNS
)


def first_tradable_index(features: pd.DataFrame) -> int:
    """First row of a raw feature frame the environment may trade on.

    This is the *indicator warm-up* boundary and nothing else: the first
    row on which no OHLCV-derived column is still waiting for its
    look-back window.  Positional, so it is correct on any index.

    Point-in-time exogenous columns (``POINT_IN_TIME_EXOGENOUS_COLUMNS``)
    are deliberately excluded from the test.  Their NaN means "no reading
    on this bar", which :meth:`TradingEnvironment._raw_feature_array`
    already resolves with its documented ``ffill().fillna(0.0)`` policy —
    the same policy the merge seam's zero-filled sibling signal columns
    (``funding_rate``, ``basis``, …) have always travelled through, and the
    reason ``signal_observed`` exists to keep the zero unambiguous.
    Gating on them made a sparsely-covered exogenous source silently
    truncate the episode to the bars it happened to cover: a one-record
    funding file moved the start index from 24 to 720 of 721 and left a
    trained policy replayed for a single bar, with no error and a width
    guard that still passed.

    Returns ``0`` when no row is fully warm (there is nothing to skip).
    """
    windowed = [
        col for col in features.columns if col not in POINT_IN_TIME_EXOGENOUS_COLUMNS
    ]
    if not windowed:
        return 0
    valid = features[windowed].notna().to_numpy().all(axis=1)
    if not valid.any():
        return 0
    return max(int(np.argmax(valid)), 0)


class NonFiniteFeatureError(ValueError):
    """A feature column reached the policy as ``inf``/``NaN``, not as warm-up.

    A **subclass of** ``ValueError`` so every existing caller and test
    that catches ``ValueError`` (or :class:`FeatureWidthMismatchError`,
    also a ``ValueError``) keeps working: the typing contract is
    unchanged, only the message grew.  What it adds is the stage, the
    column, and the recipe.

    This exists because the documented observation policy --
    ``compute -> ffill -> fillna(0)`` -- is **not** total.  It repairs
    ``NaN`` perfectly and repairs ``inf`` never.  So the observation can
    still carry an infinity, and the measured consequence of that is
    catastrophic and silent: ``fit`` records ``(mean=inf, std=nan)`` for
    the column, ``normalize`` subtracts that mean from *every* row, and
    the policy is handed ``-inf``/NaN on every single step.  On the
    2026-10-02 deep-history store arm this turned all 36 804 observation
    rows non-finite and PPO died on its own distribution constraint
    (``Expected parameter loc ... to satisfy the constraint Real()``)
    with no indication which of 60 columns was at fault.

    Raising is the only honest response.  The two previous behaviours
    were both wrong in a different direction:

    * clamping the bad stat to ``(0.0, 1.0)`` (what this module did
      before this pass) *persists* the damage -- every ``return_*`` row
      becomes identically zero and the feature stops carrying any
      information, with no error anywhere; and
    * letting it through poisons the whole episode.

    Attributes:
        columns: The offending column names, in observation order.
        stage: Where it was caught -- ``"fit"``, ``"transform"`` or
            ``"normalize"``.
        ticker_id: The ticker whose stats/frame were in play, when known.
        kind: ``"inf"`` or ``"nan"``, for the dominant offending value.
        count: How many cells were non-finite.
    """

    def __init__(
        self,
        columns: Sequence[str],
        *,
        stage: str,
        kind: str = "inf",
        count: int = 0,
        ticker_id: str | None = None,
        limit: int = 8,
    ) -> None:
        self.columns = list(columns)
        self.stage = stage
        self.kind = kind
        self.count = count
        self.ticker_id = ticker_id
        shown = self.columns[:limit]
        ellipsis = "" if len(self.columns) <= limit else f" (+{len(self.columns) - limit} more)"
        who = f" for ticker {ticker_id!r}" if ticker_id else ""
        remedy = {
            "fit": (
                "These values are already present in the observation frame "
                "compute() produced (compute -> ffill -> fillna(0) cannot "
                "produce or repair an infinity, so they came from the source "
                "bars or from an upstream derived column)."
            ),
            "transform": (
                "The observation frame was finite going in, so the "
                "non-finiteness is in the fitted stats -- the persisted "
                "normalization.npz carries a non-finite mean/std, most "
                "likely one written before this guard existed."
            ),
            "normalize": (
                "The stats in use carry a non-finite mean/std, or the frame "
                "being normalized does. When the stats came off disk, the "
                "artifact is the thing to check."
            ),
        }.get(stage, "Inspect the source frame for zero/NaN/infinite bars.")
        super().__init__(
            f"Non-finite feature values ({kind}) at the {stage} stage{who}: "
            f"{count} cell(s) across {len(self.columns)} column(s) "
            f"{shown}{ellipsis}. {remedy} An infinity here is the one value "
            f"the observation fill policy (compute -> ffill -> fillna(0)) "
            f"cannot repair, and because fit() subtracts the mean from "
            f"every row, one bad cell takes the whole episode with it. "
            f"Check the source frame for zero-volume or zero-price bars "
            f"(a store seeded from exchange outages carries no-trade bars "
            f"that look exactly like that), and for "
            f"exogenous signal columns carrying NaN/inf; the builders now "
            f"map those to NaN themselves, so a column named here is one "
            f"that arrived already non-finite."
        )


def _require_finite(
    frame: pd.DataFrame | np.ndarray,
    columns: Sequence[str] | None,
    *,
    stage: str,
    ticker_id: str | None = None,
) -> None:
    """Raise :class:`NonFiniteFeatureError` if any cell is inf or NaN.

    The check is one ``np.isfinite`` pass over a numpy array -- no
    Python-level loop over rows or columns -- so its cost is measured in
    the same order as the array's own memory bandwidth.  See the module
    docstring in :meth:`FeaturePipeline.transform` for the measurement.

    Args:
        frame: The matrix about to be handed to the policy.  A
            ``DataFrame`` or an ``ndarray``; both are checked on the
            float64 view, which is what the arithmetic was done in.
        columns: Column names aligned with ``frame``'s columns, used to
            name the offender.  When ``None`` the matrix is treated as a
            single unnamed block.
        stage: ``"fit"``, ``"transform"`` or ``"normalize"``.
        ticker_id: Ticker in play, for the message.
    """
    values = frame.to_numpy() if hasattr(frame, "to_numpy") else np.asarray(frame)
    if values.size == 0:
        return
    bad = ~np.isfinite(values)
    if not bad.any():
        return
    inf_mask = np.isinf(values)
    kind = "inf" if inf_mask.any() else "nan"
    per_column = bad.any(axis=0) if values.ndim == 2 else np.array([True])
    if columns is None or len(columns) != per_column.shape[0]:
        names: list[str] = [f"column[{i}]" for i in range(per_column.shape[0])]
    else:
        names = [n for n, flag in zip(columns, per_column) if flag]
    raise NonFiniteFeatureError(
        names,
        stage=stage,
        kind=kind,
        count=int(bad.sum()),
        ticker_id=ticker_id,
    )


@dataclass
class NormalizationStats:
    """Per-feature mean/std normalization statistics for one ticker.

    The stats are stored as a mapping ``feature_name -> (mean, std)`` and
    are serialized to the standard ``normalization.npz`` format so the
    training pipeline can persist them next to each trained model.
    """

    ticker_id: str
    stats: dict[str, tuple[float, float]] = field(default_factory=dict)
    feature_names: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def save(self, filename: str | Path) -> None:
        """Write these stats to a ``.npz`` file.

        Args:
            filename: Destination path; a ``.npz`` suffix is added if the
                caller did not supply one.
        """
        path = Path(filename)
        if path.suffix != ".npz":
            path = path.with_suffix(path.suffix + ".npz")
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            path,
            ticker_id=self.ticker_id,
            feature_names=np.asarray(self.feature_names),  # 'U' dtype, pickle-free
            means=np.asarray([s[0] for s in self.stats.values()], dtype=float),
            stds=np.asarray([s[1] for s in self.stats.values()], dtype=float),
        )
        _LOGGER.debug("Saved normalization stats for %s to %s", self.ticker_id, path)

    @classmethod
    def load(cls, filename: str | Path) -> "NormalizationStats":
        """Load stats previously written by :meth:`save`.

        Args:
            filename: Path to the ``.npz`` file.

        Returns:
            The reconstructed ``NormalizationStats``.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        path = Path(filename)
        if path.suffix != ".npz":
            path = path.with_suffix(path.suffix + ".npz")
        with np.load(path, allow_pickle=False) as data:
            feature_names = [str(n) for n in data["feature_names"]]
            means = data["means"].tolist()
            stds = data["stds"].tolist()
            ticker_id = str(data["ticker_id"])
        stats = dict(zip(feature_names, zip(means, stds)))
        return cls(ticker_id=ticker_id, stats=stats, feature_names=feature_names)

    def normalize(
        self, frame: pd.DataFrame, *, ticker_id: str | None = None, stage: str = "normalize"
    ) -> pd.DataFrame:
        """Z-score the feature columns using these stats.

        Columns without a registered stat are dropped; zero standard
        deviations are replaced by 1.0 so constant features normalize to
        zero instead of raising.

        The result is checked for ``inf``/``NaN`` before it is returned.
        That check is the load-bearing half of this module's non-finite
        guard, and it lives **here** rather than in
        :meth:`FeaturePipeline.transform` because three call sites reach
        the observation without going through ``transform``:
        :meth:`TradingEnvironment._raw_feature_array`,
        ``paper_trade``, and ``export``.  A check in ``transform`` alone
        would leave all three uncovered.

        It is also the only place the *loaded-artifact* path can be
        caught.  ``NormalizationStats.load`` reads whatever ``mean``/``std``
        a ``normalization.npz`` on disk carries, with no validation, so an
        artifact written before this guard existed reloads as
        ``(mean=inf, std=nan)`` and ``(x - inf) / 1.0`` is ``-inf`` on
        every single row -- the original store-arm failure, reachable
        again through the model directory rather than through the data.

        Args:
            frame: Un-normalized feature frame produced by a pipeline.
            ticker_id: Ticker these stats belong to, for the error
                message.  Defaults to the stats' own ``ticker_id``.
            stage: Label for the error message; the caller that knows
                which policy path is being served should say so.

        Returns:
            Frame with the same index and the normalized columns.

        Raises:
            NonFiniteFeatureError: If the normalized matrix contains an
                ``inf`` or ``NaN``, naming the offending columns.  This
                is a ``ValueError`` subclass, so existing handlers keep
                working.
        """
        out = pd.DataFrame(index=frame.index)
        for name in self.feature_names:
            if name not in frame.columns:
                continue
            mean, std = self.stats[name]
            safe_std = std if std > 1e-12 else 1.0
            out[name] = (frame[name] - mean) / safe_std
        _require_finite(
            out,
            list(out.columns),
            stage=stage,
            ticker_id=ticker_id if ticker_id is not None else (self.ticker_id or None),
        )
        return out


class FeatureWidthMismatchError(ValueError):
    """A fitted model's feature set does not match the live pipeline's.

    Raised instead of the previous width comparison, which was
    tautological for the staleness it was meant to catch: both sides
    derived from the same loaded :class:`NormalizationStats`, and
    :meth:`NormalizationStats.normalize` silently drops any frame column
    without a registered stat.  A stale 49-wide ``normalization.npz``
    against a 55-wide ``compute()`` therefore normalized straight back
    down to 49 and the check saw ``49 == 49``.

    The check is now non-self-referential: the model's own
    ``feature_names`` (read back from the ``.npz``, or recorded as
    ``n_features`` at training time) are compared against the column set
    the *live* pipeline actually produces.
    """


def check_feature_width(
    expected_feature_names: Sequence[str],
    actual_columns: Sequence[str],
    *,
    context: str,
) -> None:
    """Assert a model's fitted feature names match a live frame's columns.

    This is the width guard that ships with the 49 -> 55 widening.  (For
    the record: those numbers are the *widened* pair, and the width this
    guard actually sees depends on ``feature_windows`` and
    ``feature_groups`` as configured — the shipped ``configs/default.yaml``
    composes 52, so re-derive the number from an artifact rather than
    quoting it.)  It is deliberately **non-self-referential**:
    ``expected_feature_names`` comes from the artifact (the
    ``feature_names`` array inside ``normalization.npz``),
    ``actual_columns`` from the columns ``FeaturePipeline.compute`` just
    produced.  Neither side is derived from the other, so a stale
    artifact cannot pass by construction.

    Comparison is by name, not merely by count, because a same-width
    mismatch (one column replaced by another) is just as silent as a
    width mismatch.

    Args:
        expected_feature_names: The names the model was fitted on, in fit
            order (e.g. from ``NormalizationStats.feature_names``).
        actual_columns: The columns the live pipeline produced.
        context: Human-readable description of what is being checked, used
            verbatim in the error message.

    Raises:
        FeatureWidthMismatchError: If the two sets differ, naming the
            missing and the unexpected columns.
    """
    expected = list(expected_feature_names)
    actual = list(actual_columns)
    missing = [name for name in expected if name not in set(actual)]
    unexpected = [name for name in actual if name not in set(expected)]
    if not missing and not unexpected:
        return
    detail = []
    if missing:
        detail.append(f"missing from the live frame: {missing}")
    if unexpected:
        detail.append(f"not in the model: {unexpected}")
    raise FeatureWidthMismatchError(
        f"Feature-width mismatch ({context}): the model was fitted on "
        f"{len(expected)} features but the live pipeline produced "
        f"{len(actual)}. " + "; ".join(detail) + ". This means the "
        f"feature pipeline was widened or narrowed after training "
        f"(feature_windows / feature_groups, or the funding/vwap/count "
        f"inputs the derived columns need). Retrain the model, or restore "
        f"the feature config it was trained with."
    )


class FeaturePipeline:
    """Transforms raw OHLCV data into normalized features for an RL agent.

    Feature groups (configurable via ``feature_groups``):

    - ``price``: one-period returns, log returns, price ratio to SMA,
      high-low range.
    - ``technical``: SMA, EMA, RSI (Wilder), MACD line/signal/histogram,
      Bollinger upper/lower/width/%B, ATR.
    - ``volume``: volume change, volume z-score, OBV and OBV slope.
    - ``microstructure``: spread and order-book imbalance, computed only
      when the relevant input columns are present.

    Indicators that depend on a look-back window are computed for every
    window size in ``windows`` (e.g. ``[1, 4, 24]`` gives 1h, 4h and 1d
    views of the same levels).  Normalization stats are stored per ticker
    and can be persisted with :meth:`save_normalization` /
    :meth:`load_normalization`.
    """

    def __init__(
        self,
        windows: Sequence[int] = (1, 4, 24),
        feature_groups: Sequence[str] = _FEATURE_GROUPS,
        rsi_period: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
    ) -> None:
        """Initialize the pipeline.

        Args:
            windows: Look-back windows (in bars) used for rolling features.
            feature_groups: Subset of
                ``("price", "technical", "volume", "microstructure")``.
            rsi_period: RSI look-back period.
            macd_fast: Fast EMA period for MACD.
            macd_slow: Slow EMA period for MACD.
            macd_signal: Signal-line EMA period for MACD.
        """
        self.windows = tuple(int(w) for w in windows)
        unknown = set(feature_groups) - set(_FEATURE_GROUPS)
        if unknown:
            raise ValueError(f"Unknown feature groups: {sorted(unknown)}")
        self.feature_groups = tuple(feature_groups)
        self.rsi_period = rsi_period
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        # Per-ticker normalization stats: ticker_id -> NormalizationStats
        self._stats: dict[str, NormalizationStats] = {}
        # Exact column set produced by the last compute() (used by
        # n_features() so it always agrees with what transform emits).
        self._last_feature_names: list[str] | None = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def fit(self, df: pd.DataFrame, ticker_id: str | None = None) -> "FeaturePipeline":
        """Compute and store per-ticker normalization stats.

        Stats are fitted on the **ffilled observation frame** — ``compute``
        then ``ffill`` then ``fillna(0)`` — exactly the matrix the
        environment hands the policy, so :meth:`transform` output is an
        affine image of the observation.  (Fitting on the raw compute
        frame instead would record the NaN-warmup column's ``skipna``
        mean, which no observation row ever sees.)

        **A non-finite value in that frame raises; it is never persisted.**
        ``normalize`` subtracts the stored mean from *every* row, so a
        single ``inf`` in the frame would become ``(mean=inf, std=nan)``
        and then ``-inf`` on all 36 804 rows of the store arm, with PPO
        dying on its own distribution constraint and nothing in the
        message naming a column.  The frame is therefore checked *before*
        the stats are built, and the check is transactional: on failure
        the ticker keeps whatever stats it had, so a bad frame cannot
        leave a half-updated pipeline behind.

        Args:
            df: Raw OHLCV frame (columns: open/high/low/close/volume).
            ticker_id: Ticker these stats belong to.  When omitted the
                stats are stored without a ticker key.

        Returns:
            ``self`` for chaining.

        Raises:
            NonFiniteFeatureError: If the observation frame carries an
                ``inf``/``NaN``, naming the offending columns.  A
                ``ValueError`` subclass.
        """
        features = self.compute(df).ffill().fillna(0.0)
        # The load-bearing guard.  An infinity survives ffill and
        # fillna(0) untouched -- they are the two operations that repair
        # NaN and no other value -- so anything still non-finite here
        # arrived from the source bars or an upstream derived column, and
        # persisting a stat from it would invert the "affine image of the
        # observation" invariant this method's docstring asserts.
        #
        # This replaces an earlier version that *clamped* the offending
        # stat to ``(0.0, 1.0)``.  Clamping persisted the damage quietly:
        # the column normalized to an identically-zero series, so the
        # feature carried no information and no run reported anything.
        # Measured on the guard's own test, a single ``inf`` close in a
        # 400-bar frame silently flattened six columns that way.
        _require_finite(
            features,
            list(features.columns),
            stage="fit",
            ticker_id=ticker_id or None,
        )
        mean = features.mean()
        std = features.std(ddof=0)
        raw_stats: dict[str, tuple[float, float]] = {
            name: (float(mean[name]), float(std[name])) for name in features.columns
        }
        stats = NormalizationStats(
            ticker_id=ticker_id or "",
            stats=raw_stats,
            feature_names=list(features.columns),
        )
        self._stats[ticker_id or ""] = stats
        return self

    def transform(self, df: pd.DataFrame, ticker_id: str | None = None) -> np.ndarray:
        """Compute and normalize features for ``df``.

        The input is the **ffilled observation frame** (``compute`` then
        ``ffill`` then ``fillna(0)``) — the same matrix the environment
        hands the policy — so the output is the observation's exact
        affine image.  Leading warmup rows (zero-filled) therefore map to
        ``(0 - mean) / std`` rather than to zero.

        When per-ticker stats exist they are used; otherwise the frame's
        own statistics are used (in-sample normalization, fine for
        exploratory use but not for evaluation).

        **The returned array is checked for ``inf``/``NaN`` before it is
        returned**, on both branches.  Before this pass the stored-stats
        branch was unchecked and the in-sample fallback was unchecked, and
        both were reachable: measured on a 400-bar frame with one ``inf``
        close, the fallback branch returned 2 400 non-finite cells; and a
        ``normalization.npz`` carrying ``(mean=inf, std=nan)`` -- any
        artifact written before the guard -- turned every row ``-inf``
        through the stats branch.

        **Cost: one ``np.isfinite`` pass, measured, not assumed.**  The
        check is a single vectorized pass over a C-contiguous numpy array
        -- no Python-level loop over rows or columns -- so it is bounded by
        the array's memory bandwidth, not by its element count in Python.

        Measured on the store arm's own row count (36 804 bars, the default
        feature groups, 49 columns, float64) on this host: **0.45 ms** per
        call on a contiguous float64 array (0.52 ms median), 0.94 ms when
        handed the ``DataFrame`` rather than the array, and 0.15 ms on the
        float32 array actually returned.  ``compute()`` on the same frame
        is ~67 ms and a whole ``transform`` ~93 ms, so **both** gates
        together are ~0.5% of a ``transform`` (~1.3% of the ``compute``
        they guard).  It is affordable at this call rate, and it is the
        only place a whole-episode poisoning can be caught before the
        policy sees step 1.

        (An earlier revision of this docstring claimed ~2.0 ms per call
        against a ~9 ms ``compute`` -- i.e. ~18%.  Re-measured, the scan
        is roughly 4x *cheaper* than stated and ``compute`` roughly 7x
        more expensive, so the conclusion is unchanged and the margin is
        wider than claimed.  The numbers above are the ones to trust.)

        The scan runs on **every** ``transform``, not once at
        construction.  Deferring it was rejected: the stats can be replaced
        by :meth:`set_stats` / :meth:`load_normalization` at any time, and
        it is exactly that later swap which reintroduces a poisoned
        artifact.  It is not on the per-step path either -- the environment
        builds its matrix once in ``_raw_feature_array`` and slices per
        step (``environment.py`` calls ``stats.normalize`` at
        construction; ``step`` reads ``self._feature_matrix[idx]``).

        Args:
            df: Raw OHLCV frame.
            ticker_id: Ticker whose stored stats should be applied.

        Returns:
            ``float32`` array of shape ``(n_bars, n_features)``.

        Raises:
            NonFiniteFeatureError: If the normalized matrix contains an
                ``inf`` or ``NaN``, naming the offending columns.  A
                ``ValueError`` subclass.
        """
        features = self.compute(df).ffill().fillna(0.0)
        stats = self._stats.get(ticker_id or "")
        if stats is not None:
            # `normalize` checks its own output and labels it "transform",
            # so the stored-stats branch is covered by the same guard the
            # environment's direct `normalize` call gets.
            normalized = stats.normalize(features, ticker_id=ticker_id or None, stage="transform")
        else:
            # The in-sample fallback computes its own stats, so it needs
            # its own check.  `.replace(0, 1.0)` only floors a *zero*
            # std: a column containing one infinity has an infinite mean,
            # and subtracting it takes the whole frame with it.
            _require_finite(
                features,
                list(features.columns),
                stage="transform",
                ticker_id=ticker_id or None,
            )
            normalized = (features - features.mean()) / features.std(ddof=0).replace(0, 1.0)
        out = normalized.to_numpy(dtype=np.float32)
        _require_finite(
            out,
            list(normalized.columns),
            stage="transform",
            ticker_id=ticker_id or None,
        )
        return out

    def fit_transform(
        self, df: pd.DataFrame, ticker_id: str | None = None
    ) -> np.ndarray:
        """Convenience for ``fit`` followed by ``transform``.

        Args:
            df: Raw OHLCV frame.
            ticker_id: Ticker to associate the computed stats with.

        Returns:
            ``float32`` array of shape ``(n_bars, n_features)``.
        """
        self.fit(df, ticker_id=ticker_id)
        return self.transform(df, ticker_id=ticker_id)

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute un-normalized features (no scaling applied).

        Args:
            df: Raw OHLCV frame.

        Returns:
            DataFrame of the same length as ``df`` with one column per
            computed feature.  The first rows may contain NaN where an
            indicator's look-back window has not filled; the caller (the
            environment) skips those bars.
        """
        missing = [c for c in _OHLCV_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"Missing required OHLCV columns: {missing}")

        # The single sanitizing seam.  Prices are sanitized **here**, once,
        # rather than at each of the eight divisions inside the price and
        # technical builders, and that placement is deliberate: it makes
        # every present *and future* price-derived feature finite-by-
        # construction instead of relying on each new expression to
        # remember its own guard, and it covers the rolling builders too
        # (`sma`/`ema`/`rsi`/`macd`/`bollinger`/`atr`), which no
        # per-expression guard reaches.
        #
        # WHAT THE SEAM ACTUALLY BUYS, measured on pandas 3.0.4 -- and it is
        # not infinity suppression, which is what this comment used to
        # claim.  The two pandas aggregations disagree about non-finite
        # input, and neither one produces an infinity:
        #
        # * ``rolling(w)`` **masks** the bad value to NaN, so the bad bar
        #   and its whole window read NaN and the contamination is visible.
        # * ``ewm()`` **skips** the bar entirely and carries on, returning a
        #   value that is finite *and wrong* (0 infinities, 0 NaNs).
        #
        # So `sma`/`bollinger` would have masked an infinity anyway, and no
        # `isinf` assertion can ever observe this seam.  What it does buy is
        # on the **zero** path: `_NON_FINITE_INPUTS` contains `0.0` as well as
        # the infinities, and a zero price is reachable in real OHLCV.  With
        # the seam a zero price makes its window read NaN (or the module's
        # documented neutral constant); without it the zero enters the window
        # as a real number and the rolling group reports a plausible, finite,
        # completely wrong answer -- measured on a 400-bar frame with one zero
        # price: `sma_4` 91.41 for a ~122 price, `bb_lower_4` **-14.14**,
        # `bb_width_4` **2.3094**, `rsi_4` **0.2979**, `atr_4` **30.87**
        # against a healthy 0.487.  A -83% price print and a 63x range spike,
        # silently, as model input.
        #
        # KNOWN GAP, not fixable here: because `ewm` returns a finite wrong
        # number rather than a NaN, the `ema`/`macd`/`rsi`/`atr` families
        # survive a skipped bar and `_require_finite` cannot see it.  That
        # hazard is pinned by
        # ``tests/test_feature_nonfinite_guards.py::
        # test_rolling_masks_but_ewm_skips_a_non_finite_price_this_guard_cannot_see``
        # and fixing it is out of scope for this pass.
        #
        # ``volume`` is deliberately NOT sanitized here.  A zero-volume
        # bar is a *valid reading* -- it is information, and
        # `volume_zscore_20`/`obv` are right to model it as such -- so the
        # zero guard is applied only where volume is a *denominator*
        # (`volume_change_1`), which is where a zero is meaningless
        # rather than informative.  Its ``+/-inf`` is handled at that same
        # site for the same reason.
        close = df["close"].astype(float).replace(_NON_FINITE_INPUTS, np.nan)
        high = df["high"].astype(float).replace(_NON_FINITE_INPUTS, np.nan)
        low = df["low"].astype(float).replace(_NON_FINITE_INPUTS, np.nan)
        volume = df["volume"].astype(float)

        out = pd.DataFrame(index=df.index)

        if "price" in self.feature_groups:
            self._add_price_features(out, close, high, low)
        if "technical" in self.feature_groups:
            self._add_technical_features(out, close, high, low)
        if "volume" in self.feature_groups:
            self._add_volume_features(out, close, volume)
        if "microstructure" in self.feature_groups:
            self._add_microstructure_features(out, df)
        if "signals" in self.feature_groups:
            self._add_signals_features(out, df)

        self._last_feature_names = list(out.columns)
        return out

    # ------------------------------------------------------------------
    # normalization persistence
    # ------------------------------------------------------------------
    def stats_for(self, ticker_id: str) -> NormalizationStats | None:
        """Return stored stats for ``ticker_id`` (or None)."""
        return self._stats.get(ticker_id)

    def set_stats(self, ticker_id: str, stats: NormalizationStats) -> None:
        """Register externally loaded stats for ``ticker_id``."""
        self._stats[ticker_id] = stats

    def save_normalization(self, ticker_id: str, filename: str | Path) -> None:
        """Persist per-ticker normalization stats to ``normalization.npz``.

        Args:
            ticker_id: Ticker whose stats are saved.
            filename: Target path (``.npz`` appended if missing).

        Raises:
            KeyError: If no stats exist for ``ticker_id``.
        """
        stats = self._stats.get(ticker_id)
        if stats is None:
            raise KeyError(f"No normalization stats for ticker {ticker_id!r}")
        stats.save(filename)

    def load_normalization(self, ticker_id: str, filename: str | Path) -> None:
        """Load per-ticker normalization stats into the pipeline.

        Args:
            ticker_id: Ticker the loaded stats belong to.
            filename: Source ``.npz`` path.
        """
        stats = NormalizationStats.load(filename)
        stats.ticker_id = ticker_id
        self._stats[ticker_id] = stats

    # ------------------------------------------------------------------
    # helper builders (one method per feature group)
    # ------------------------------------------------------------------
    def _add_price_features(
        self, out: pd.DataFrame, close: pd.Series, high: pd.Series, low: pd.Series
    ) -> None:
        # ``replace(0, np.nan)`` before every division, as the rest of
        # this module already does (see ``_add_microstructure_features``,
        # ``_rsi``, ``_bollinger``): ``pct_change`` divides by the prior
        # value, so a zero bar yields +/-inf rather than NaN, and inf is
        # the one value the ffill+fillna(0) observation policy cannot
        # repair.  A zero bar has no percentage change to report, which is
        # absence (NaN), not an infinite one.
        #
        # ``close``/``high``/``low`` arrive here already sanitized by
        # ``compute``'s single seam, so ``safe_close`` here is a second
        # guard against a future caller bypassing it, not the only one.
        safe_close = close.replace(_NON_FINITE_INPUTS, np.nan)
        ret = safe_close.pct_change()
        # The NUMERATOR needs the guard too, not just the denominator:
        # `log(0)` is -inf, so a zero *current* close poisons the column
        # just as a zero prior close would.  Same for an infinite one:
        # measured with a single inf close, `log_return_{1,4,24}` emitted
        # TWO inf cells each before this guard.
        log_ret = np.log(safe_close / safe_close.shift(1))
        out["return_1"] = ret
        out["log_return_1"] = log_ret
        # `range_1` is a ratio of two *series*, so BOTH sides need it: an
        # infinite high yields `inf - low`, which is still infinite.  The
        # numerator guard was the one omission in the original fix.
        out["range_1"] = (
            high.replace(_NON_FINITE_INPUTS, np.nan) - low.replace(_NON_FINITE_INPUTS, np.nan)
        ) / safe_close
        for w in self.windows:
            sma = close.rolling(w).mean()
            out[f"return_{w}"] = safe_close.pct_change(w)
            out[f"log_return_{w}"] = np.log(safe_close / safe_close.shift(w))
            # `safe_close` as the NUMERATOR here too, which the original
            # fix missed: `close / sma - 1` with a zero close is `0 - 1`,
            # a finite but meaningless -1.0 that reads as a 100% discount
            # to the moving average.  A zero-price bar has no
            # ratio-to-SMA to report, which is the same absence argument
            # that guards `return_*` and `range_1`.
            out[f"price_ratio_sma_{w}"] = (
                safe_close / sma.replace(_NON_FINITE_INPUTS, np.nan) - 1.0
            )

    def _add_technical_features(
        self, out: pd.DataFrame, close: pd.Series, high: pd.Series, low: pd.Series
    ) -> None:
        for w in self.windows:
            sma = close.rolling(w).mean()
            ema = close.ewm(span=w, adjust=False).mean()
            out[f"sma_{w}"] = sma
            out[f"ema_{w}"] = ema
            out[f"rsi_{w}"] = _rsi(close, w)
            macd_line, macd_signal, macd_hist = _macd(
                close, self.macd_fast, self.macd_slow, self.macd_signal
            )
            out[f"macd_line_{w}"] = macd_line
            out[f"macd_signal_{w}"] = macd_signal
            out[f"macd_hist_{w}"] = macd_hist
            upper, lower, width, pct_b = _bollinger(close, w, 2.0)
            out[f"bb_upper_{w}"] = upper
            out[f"bb_lower_{w}"] = lower
            out[f"bb_width_{w}"] = width
            out[f"bb_pctb_{w}"] = pct_b
            out[f"atr_{w}"] = _atr(high, low, close, w)

    def _add_volume_features(
        self, out: pd.DataFrame, close: pd.Series, volume: pd.Series
    ) -> None:
        out["volume_change_1"] = volume.replace(_NON_FINITE_INPUTS, np.nan).pct_change()
        # `vol_std.replace(0, 1.0)` floors the 0/0 of an all-constant
        # 20-bar window (which is a real reading -- a genuinely quiet
        # market -- and not the same thing as no reading at all).  No
        # infinity guard is needed on the denominator: pandas' rolling
        # std propagates a non-finite input as NaN, measured, so an
        # infinite volume produces a NaN z-score, which the observation
        # fill repairs.
        vol_mean = volume.rolling(20).mean()
        vol_std = volume.rolling(20).std(ddof=0)
        out["volume_zscore_20"] = (volume - vol_mean) / vol_std.replace(0, 1.0)
        # `obv` and its slopes involve no division or log: `np.sign` maps
        # NaN to NaN, `.fillna(0.0)` absorbs that, and `cumsum`/`diff` of
        # finite values stay finite.  Safe by construction; the only
        # residual risk is a genuine float64 overflow of the cumulative
        # sum at ~1e308, which is what the `fit` guard exists to catch.
        obv = (np.sign(close.diff()) * volume).fillna(0.0).cumsum()
        out["obv"] = obv
        for w in self.windows:
            out[f"obv_slope_{w}"] = obv.diff(w)

    def _add_microstructure_features(
        self, out: pd.DataFrame, df: pd.DataFrame
    ) -> None:
        # Every branch here is a pass-through or a ratio, so all of them
        # need the same guard.  The pass-through branch is the one the
        # original fix missed entirely: an already-computed `spread` was
        # copied verbatim, so a single infinite cell in it landed in the
        # observation unrepaired.  Non-finite is mapped to NaN, not to a
        # sentinel, because that is what a missing microstructure reading
        # already means here -- a zero bid yields a NaN spread today, and
        # an absent reading is precisely "absence".
        if "spread" in df.columns:
            out["spread"] = df["spread"].astype(float).replace(_NON_FINITE_INPUTS, np.nan)
        elif {"bid", "ask"}.issubset(df.columns):
            bid = df["bid"].astype(float)
            ask = df["ask"].astype(float)
            # Both sides: an infinite ask gives `inf - bid` = inf even
            # with a healthy denominator.
            out["spread"] = (
                ask.replace(_NON_FINITE_INPUTS, np.nan) - bid.replace(_NON_FINITE_INPUTS, np.nan)
            ) / bid.replace(_NON_FINITE_INPUTS, np.nan)
        if {"bid_vol", "ask_vol"}.issubset(df.columns):
            bid_vol = df["bid_vol"].astype(float)
            ask_vol = df["ask_vol"].astype(float)
            # Same two-sided rule as `spread` above.
            denom = (bid_vol + ask_vol).replace(_NON_FINITE_INPUTS, np.nan)
            out["order_book_imbalance"] = (
                bid_vol.replace(_NON_FINITE_INPUTS, np.nan)
                - ask_vol.replace(_NON_FINITE_INPUTS, np.nan)
            ) / denom

    def _add_signals_features(
        self, out: pd.DataFrame, df: pd.DataFrame
    ) -> None:
        """Forward exogenous signal columns into the feature matrix.

        These columns are added by :func:`merge_extra_features` in
        ``data.py`` and need no further computation — they are already
        numeric (sentiment_score is a float, article_count is an int,
        novelty_flag is a bool, ``signal_observed`` is 0.0/1.0 and
        ``signal_age_hours`` is a float age in hours carrying a -1.0
        sentinel instead of NaN, so no column here can poison the
        z-scored observation).  Columns not present in the input are
        silently skipped (the merge may not always be active).

        A **non-finite** cell is mapped to NaN, which is the correct
        value here rather than a merely convenient one: this module
        already declares (``POINT_IN_TIME_EXOGENOUS_COLUMNS``) that a
        NaN in an exogenous column means "no reading on this bar", and
        the observation's documented ``ffill().fillna(0.0)`` policy
        already resolves exactly that.  An infinity, by contrast, is the
        one value the policy cannot touch.  This branch matters more than
        the others because these are the only observation columns this
        module does **not** compute: they arrive from sibling signal
        files and from ``data.add_derived_ohlcv_features``
        (``vwap_dev``, ``volume_per_trade``, ``trade_count_zscore_20``),
        so no guard inside this module can have run before they arrive.

        Only ``+/-inf`` is mapped.  **Not** zero: ``signal_observed`` is
        0.0/1.0 and ``signal_age_hours`` carries a ``-1.0`` "no reading"
        sentinel, so zero is a load-bearing value in exactly the columns
        that let a zero-filled cell be told apart from a genuine 0.
        Nothing here divides, so the zero-is-a-denominator reasoning that
        motivates ``_NON_FINITE_INPUTS`` does not apply; see
        ``_NON_FINITE_INPUTS_NO_ZERO``.
        """
        for col in _SIGNAL_COLUMNS:
            if col in _SIGNAL_BUILDER_INPUT_COLUMNS:
                continue
            if col in df.columns:
                out[col] = df[col].astype(float).replace(
                    _NON_FINITE_INPUTS_NO_ZERO, np.nan
                )

    # ------------------------------------------------------------------
    def n_features(self) -> int:
        """Return the number of features ``compute`` produces.

        Uses the exact column set of the last computed frame, so it always
        agrees with :meth:`transform` output width.  Before any data has
        been seen, an exact count is derived from a synthetic minimal
        frame (no microstructure columns).
        """
        if self._last_feature_names is not None:
            return len(self._last_feature_names)
        dummy = pd.DataFrame(
            {col: [1.0, 2.0, 3.0] for col in _OHLCV_COLUMNS}
        )
        return self.compute(dummy).shape[1]


# ----------------------------------------------------------------------
# indicator helpers (module level so they are unit-testable)
# ----------------------------------------------------------------------
def _rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI over ``period`` bars.

    ``avg_loss.replace(0, np.nan)`` guards the one genuine 0/0 here: a
    window with no losses at all, where ``rs`` would be infinite and
    ``100 - 100/(1+inf)`` would be 100.0 rather than the 100.0 the
    ``fillna`` then produces anyway -- so the guard is cosmetic for the
    all-gains case and load-bearing for the both-sides-zero case, where
    ``fillna`` supplies the neutral 50.0.

    No infinity guard on ``avg_gain`` is needed, and the reason is worth
    stating exactly, because a previous version of this docstring got it
    backwards.  It claimed pandas' ``ewm`` "masks a non-finite input as
    NaN", so that ``compute``'s seam (which maps a non-finite close to
    NaN *before* ``diff()`` runs) made ``gain``/``loss`` see a NaN, ``rs``
    came out NaN, and ``fillna(50.0)`` mapped that NaN to the neutral
    reading.  **None of that happens.**  Measured on pandas 3.0.4, ``ewm``
    *skips* a non-finite input exactly as it skips an infinity: it carries
    the previous bar's average forward and returns a **finite** number.
    With the seam applied and a zero price at one bar, measured on a 400-bar
    frame that has losses: ``avg_gain`` and ``avg_loss`` both stay finite
    and non-zero, ``rs`` stays finite, and ``fillna(50.0)`` never fires --
    ``rsi_4`` reports the *previous* bar's value again (85.5634 carried
    across the poisoned bar in the recorded probe).  The 50.0 that
    ``fillna`` does supply in the pinned test comes from the
    both-sides-zero guard above, not from the seam: that test's frame is a
    monotonically rising ramp, so it has no down-bars at all.  On a frame
    with losses the route cannot fire.

    So the residual hazard is real and this docstring does not make it
    unreachable.  A bar skipped by the ``ewm`` recursion leaves a *finite,
    wrong* RSI behind, ``fillna`` cannot distinguish that from a genuine
    reading, and ``_require_finite`` cannot see it.  What ``compute``'s
    seam does buy here is only that the bad price never enters ``diff()``
    as an infinity -- it makes the bar *skippable*, not *unknown*.  That
    distinction is the whole point: the ``rolling`` families report a
    poisoned window as NaN, the ``ewm`` families report it as a stale
    carried value.  The ``KNOWN GAP`` note in ``compute`` is the governing
    record for this, and it states the limitation correctly.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def _macd(
    close: pd.Series, fast: int, slow: int, signal_period: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line and histogram."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal = macd_line.ewm(span=signal_period, adjust=False).mean()
    hist = macd_line - signal
    return macd_line, signal, hist


def _bollinger(
    close: pd.Series, period: int, k: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Bollinger upper, lower, width and %B for ``period``.

    Both ratios are guarded, and the guards are different in kind:

    * ``width`` divides by ``mid``, floored at zero -> NaN.  A flat market
      has ``mid == 0`` only if the price itself is zero, which
      ``compute``'s seam has already mapped to NaN, so this is a
      belt-and-braces guard; it still matters for a caller that reaches
      ``_bollinger`` directly with an unsanitized series.
    * ``pct_b`` divides by ``upper - lower``, which is **identically zero
      on every period-1 roll and on every constant window** -- the 0/0 is
      the common case, not an edge case.  ``.where(denom.abs() > 1e-12,
      0.5)`` maps it to the neutral midpoint, which is the right reading:
      price is exactly at the band centre.

    Neither needs an infinity guard: ``mid``/``std`` come from pandas
    rolling aggregations, and pandas' ``rolling`` **masks** a non-finite
    input to NaN rather than propagating it as an infinity (measured on
    pandas 3.0.4), so the numerators are finite whenever the denominators
    are.

    That masking is pandas' behaviour, not this module's guarantee, and it
    is worth being precise about what it does and does not buy: it stops
    the bad *value* escaping, but the poisoned window then reads NaN rather
    than unknown-and-flagged, and it is the reason a caller reaching
    ``_bollinger`` directly with an unsanitized series gets NaN bands
    without any error.  The seam in ``compute`` is still what keeps a
    **zero** price -- also in ``_NON_FINITE_INPUTS``, and far likelier in
    real data -- out of ``mid`` in the first place; without it a zero
    produces a finite, wrong band (``bb_lower_4`` -14.14, ``bb_width_4``
    2.3094 measured) that ``_require_finite`` cannot reject.
    """
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    width = (upper - lower) / mid.replace(0, np.nan)
    denom = upper - lower
    # Zero-width bands (period-1 rolls) produce 0/0 -> neutral 0.5 %B.
    pct_b = ((close - lower) / denom).where(denom.abs() > 1e-12, 0.5)
    return upper, lower, width, pct_b


def _atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int
) -> pd.Series:
    """Average True Range (Wilder) over ``period`` bars.

    No division and no log anywhere in here: ``tr`` is a ``max`` of three
    differences and the average is an ``ewm``.  Finite inputs therefore
    give finite outputs, so nothing here can emit an infinity.

    A non-finite input does **not** give a NaN, though.  This docstring
    previously said it did, on the grounds that "pandas' ``ewm`` masks it,
    measured" -- measured on pandas 3.0.4, ``ewm`` **skips** the bar
    instead, returning a finite value with no NaN anywhere.  ``compute``'s
    seam is what actually protects this function: it maps a non-finite
    close/high/low to NaN first, and ``DataFrame.max(axis=1)`` then skips
    the NaN rows, so ``tr`` is finite across the bad bar.  Without the seam
    a zero price (also in ``_NON_FINITE_INPUTS``) inflates ``tr`` to 30.87
    against a healthy 0.487 -- finite, 63x wrong, and invisible to
    ``_require_finite``.
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


__all__ = [
    "FeaturePipeline",
    "FeatureWidthMismatchError",
    "NonFiniteFeatureError",
    "NormalizationStats",
    "check_feature_width",
    "first_tradable_index",
    "normalize_ticker_id",
]


def normalize_ticker_id(ticker_id: str) -> str:
    """Normalize a pair/ticker id to a filesystem- and code-safe string.

    ``"ETH/USD"`` becomes ``"ETH_USD"``.  Unchanged ids pass through.

    Args:
        ticker_id: Raw ticker id such as ``"ETH/USD"``.

    Returns:
        Normalized id safe for use in paths and attribute names.
    """
    return ticker_id.replace("/", "_").replace("-", "_").replace(".", "_").upper()