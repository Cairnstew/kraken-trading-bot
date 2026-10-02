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

    def normalize(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Z-score the feature columns using these stats.

        Columns without a registered stat are dropped; zero standard
        deviations are replaced by 1.0 so constant features normalize to
        zero instead of raising.

        Args:
            frame: Un-normalized feature frame produced by a pipeline.

        Returns:
            Frame with the same index and the normalized columns.
        """
        out = pd.DataFrame(index=frame.index)
        for name in self.feature_names:
            if name not in frame.columns:
                continue
            mean, std = self.stats[name]
            safe_std = std if std > 1e-12 else 1.0
            out[name] = (frame[name] - mean) / safe_std
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

        Args:
            df: Raw OHLCV frame (columns: open/high/low/close/volume).
            ticker_id: Ticker these stats belong to.  When omitted the
                stats are stored without a ticker key.

        Returns:
            ``self`` for chaining.
        """
        features = self.compute(df).ffill().fillna(0.0)
        mean = features.mean()
        std = features.std(ddof=0)
        # Non-finite stats are made finite here rather than trusted.
        # `normalize` subtracts this mean from EVERY row, so one
        # non-finite cell anywhere in the frame propagates to the whole
        # episode and the policy is handed inf/NaN on every step -- the
        # documented "transform output is an affine image of the
        # observation" invariant silently inverts.  The builders all emit
        # NaN rather than inf by construction now, but this is the one
        # place where the guarantee is enforced instead of assumed: a
        # column that is still not finite after the observation fill
        # carries no usable scale, so it normalizes to a flat zero and
        # says so here, rather than taking the episode down with it.
        raw_stats: dict[str, tuple[float, float]] = {}
        for name in features.columns:
            m = float(mean[name])
            s = float(std[name])
            if not np.isfinite(m):
                m = 0.0
            if not np.isfinite(s):
                s = 1.0
            raw_stats[name] = (m, s)
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

        Args:
            df: Raw OHLCV frame.
            ticker_id: Ticker whose stored stats should be applied.

        Returns:
            ``float32`` array of shape ``(n_bars, n_features)``.
        """
        features = self.compute(df).ffill().fillna(0.0)
        stats = self._stats.get(ticker_id or "")
        if stats is not None:
            normalized = stats.normalize(features)
        else:
            normalized = (features - features.mean()) / features.std(ddof=0).replace(0, 1.0)
        return normalized.to_numpy(dtype=np.float32)

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

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
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
        safe_close = close.replace(0, np.nan)
        ret = safe_close.pct_change()
        # The NUMERATOR needs the guard too, not just the denominator:
        # `log(0)` is -inf, so a zero *current* close poisons the column
        # just as a zero prior close would.
        log_ret = np.log(safe_close / safe_close.shift(1))
        out["return_1"] = ret
        out["log_return_1"] = log_ret
        out["range_1"] = (high - low) / close.replace(0, np.nan)
        for w in self.windows:
            sma = close.rolling(w).mean()
            out[f"return_{w}"] = safe_close.pct_change(w)
            out[f"log_return_{w}"] = np.log(safe_close / safe_close.shift(w))
            out[f"price_ratio_sma_{w}"] = close / sma.replace(0, np.nan) - 1.0

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
        out["volume_change_1"] = volume.replace(0, np.nan).pct_change()
        vol_mean = volume.rolling(20).mean()
        vol_std = volume.rolling(20).std(ddof=0)
        out["volume_zscore_20"] = (volume - vol_mean) / vol_std.replace(0, 1.0)
        obv = (np.sign(close.diff()) * volume).fillna(0.0).cumsum()
        out["obv"] = obv
        for w in self.windows:
            out[f"obv_slope_{w}"] = obv.diff(w)

    def _add_microstructure_features(
        self, out: pd.DataFrame, df: pd.DataFrame
    ) -> None:
        if "spread" in df.columns:
            out["spread"] = df["spread"].astype(float)
        elif {"bid", "ask"}.issubset(df.columns):
            bid = df["bid"].astype(float)
            ask = df["ask"].astype(float)
            out["spread"] = (ask - bid) / bid.replace(0, np.nan)
        if {"bid_vol", "ask_vol"}.issubset(df.columns):
            bid_vol = df["bid_vol"].astype(float)
            ask_vol = df["ask_vol"].astype(float)
            denom = (bid_vol + ask_vol).replace(0, np.nan)
            out["order_book_imbalance"] = (bid_vol - ask_vol) / denom

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
        """
        for col in _SIGNAL_COLUMNS:
            if col in _SIGNAL_BUILDER_INPUT_COLUMNS:
                continue
            if col in df.columns:
                out[col] = df[col].astype(float)

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
    """Wilder's RSI over ``period`` bars."""
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
    """Bollinger upper, lower, width and %B for ``period``."""
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
    """Average True Range (Wilder) over ``period`` bars."""
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
    "NormalizationStats",
    "check_feature_width",
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