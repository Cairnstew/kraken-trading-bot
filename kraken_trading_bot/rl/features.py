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
        stats = NormalizationStats(
            ticker_id=ticker_id or "",
            stats={
                name: (float(mean[name]), float(std[name]))
                for name in features.columns
            },
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
        ret = close.pct_change()
        log_ret = np.log(close / close.shift(1))
        out["return_1"] = ret
        out["log_return_1"] = log_ret
        out["range_1"] = (high - low) / close
        for w in self.windows:
            sma = close.rolling(w).mean()
            out[f"return_{w}"] = close.pct_change(w)
            out[f"log_return_{w}"] = np.log(close / close.shift(w))
            out[f"price_ratio_sma_{w}"] = close / sma - 1.0

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
        out["volume_change_1"] = volume.pct_change()
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
        novelty_flag is a bool).  Columns not present in the input are
        silently skipped (the merge may not always be active).
        """
        for col in _SIGNAL_COLUMNS:
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
    "NormalizationStats",
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