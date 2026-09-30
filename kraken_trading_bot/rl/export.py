"""Export the RL data pipeline's staged frame to CSV.

:func:`build_export_frame` composes the very stages
:func:`kraken_trading_bot.rl.train.train_ticker` runs — resolve the
config, read OHLCV (live Kraken fetch or the ``kraken-market-data``
store), left-join the exogenous signals, fit the feature pipeline and
slice the trailing episode — then stops one step short of the
:class:`~kraken_trading_bot.rl.environment.TradingEnvironment`. The
frame it returns keeps the stage boundaries visible as column blocks,
in pipeline order:

``timestamp``
    ISO-8601 UTC bar time, the readable key for a CSV. The frame's own
    ``time`` epoch column travels with it, unrenamed.

OHLCV (:data:`kraken_trading_bot.rl.data._OHLCV_COLUMNS`)
    ``time, open, high, low, close, vwap, volume, count`` — untouched.

signals
    Whatever ``extra_features_file`` / ``funding_features_file`` /
    ``social_features_file`` merged in. All three are ``None`` in
    ``configs/default.yaml``, so this block is usually empty.

features
    ``FeaturePipeline.compute`` output under its native names, already
    forward-filled and zero-filled. **These columns are the agent's
    observation vector**, not a normalized stand-in:
    ``TradingEnvironment`` feeds ``_raw_feature_array()`` — ``compute``
    then ``ffill`` then ``fillna(0)`` — straight to the policy, and
    ``PaperTrader._build_observation`` rebuilds the same raw row for
    live inference. ``FeaturePipeline.transform`` sits on no
    observation path in this repo.

normalized (opt-in, ``z_``-prefixed)
    ``FeaturePipeline.transform``, i.e. what ``normalization.npz``
    *would* emit. Off by default so the export never implies a scaling
    stage the agent does not have.

``warmup``
    ``True`` for rows before the environment's ``_start_index``
    (``_first_valid_index``): the indicator look-back region, present
    in the observation matrix but never traded on.

The stage breakdown travels on ``frame.attrs["stages"]`` so the CLI can
summarise a frame without re-deriving it. Contract: ``tests/test_rl_export.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .data import _OHLCV_COLUMNS, prepare_episode, read_ohlc_dataframe
from .features import FeaturePipeline, normalize_ticker_id
from .train import build_train_config

_LOGGER = logging.getLogger(__name__)

# ``build_train_config`` stamps a model name onto every config it builds;
# an export has no model, and nothing downstream of this module reads the
# key, so the placeholder is deliberately inert.
_EXPORT_MODEL_NAME = "export"

_DEFAULT_FEATURE_GROUPS = [
    "price",
    "technical",
    "volume",
    "microstructure",
    "signals",
]


def _timestamp_strings(df: pd.DataFrame) -> np.ndarray:
    """ISO-8601 UTC bar timestamps as strings, whatever ``time`` holds.

    Reads the frame's ``time`` column when it is present (epoch seconds
    from Kraken, or a datetime from the store) and falls back to the
    DatetimeIndex, so the export never depends on which of the two the
    loading stage happened to populate.
    """
    if "time" in df.columns:
        col = df["time"]
        if pd.api.types.is_datetime64_any_dtype(col):
            ts = pd.Series(col)
            ts = ts.dt.tz_localize("UTC") if ts.dt.tz is None else ts.dt.tz_convert("UTC")
            return ts.dt.strftime("%Y-%m-%dT%H:%M:%SZ").to_numpy()
        return pd.to_datetime(col, unit="s", utc=True).dt.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ).to_numpy()

    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        idx = pd.to_datetime(idx, unit="s", utc=True)
    elif idx.tz is None:
        idx = idx.tz_localize("UTC")
    return idx.strftime("%Y-%m-%dT%H:%M:%SZ").to_numpy()


def _warmup_mask(features: pd.DataFrame) -> np.ndarray:
    """Rows the environment will not trade on, positionally.

    Mirrors ``TradingEnvironment._first_valid_index``: the first row
    whose features are *all* non-NaN, i.e. the indicator look-back
    region. Positional (rather than ``idxmax``) so it is correct on a
    DatetimeIndex too, which is what this module exports.
    """
    valid = features.notna().to_numpy().all(axis=1)
    if not valid.any():
        first = 0
    else:
        first = int(np.argmax(valid))
    return np.arange(len(features)) < first


def build_export_frame(
    ticker_id: str,
    *,
    config_path: str | Path | None = None,
    pages: int = 6,
    episode_bars: int | None = None,
    manager: Any | None = None,
    include_normalized: bool = False,
    **overrides: Any,
) -> pd.DataFrame:
    """Run the training data pipeline and return the staged frame.

    Stages, in order, matching :func:`train_ticker`: config -> OHLC read
    -> signal merges -> feature fit -> episode slice -> compute.

    Args:
        ticker_id: Ticker to export (e.g. ``"SOL_USD"``).
        config_path: Optional YAML config to base the run on.
        pages: OHLC pages to fetch (each ~720 candles).
        episode_bars: Keep only the trailing N bars (None = all).
        manager: Optional KrakenManager for data fetching; defaults to
            whatever ``read_ohlc_dataframe`` builds.
        include_normalized: Also append ``z_``-prefixed columns from
            ``FeaturePipeline.transform`` (not used by the agent).
        overrides: Config overrides, e.g.
            ``ohlcv_interval_minutes=30``.

    Returns:
        One row per bar; column blocks documented in the module
        docstring, stage split in ``frame.attrs["stages"]``.

    Raises:
        NotEnoughDataError: If too few OHLC bars are available for the
            pipeline's look-back window.
    """
    cfg = build_train_config(ticker_id, _EXPORT_MODEL_NAME, config_path, **overrides)
    pair = cfg["ticker"]
    interval = int(cfg.get("ohlcv_interval_minutes", 60))

    _LOGGER.info("Exporting %d pages of %s %d-minute OHLC", pages, pair, interval)
    df = read_ohlc_dataframe(
        pair,
        interval=interval,
        pages=pages,
        manager=manager,
        extra_features_file=cfg.get("extra_features_file"),
        funding_features_file=cfg.get("funding_features_file"),
        social_features_file=cfg.get("social_features_file"),
        market_data_store=cfg.get("market_data_store"),
    )

    features = FeaturePipeline(
        windows=cfg.get("feature_windows", [1, 4, 24]),
        feature_groups=cfg.get("feature_groups", None) or _DEFAULT_FEATURE_GROUPS,
    )

    # Fits the per-ticker stats on the full frame, then keeps the
    # trailing episode — exactly what train_ticker hands the environment,
    # so this frame is the training frame.
    episode = prepare_episode(
        df, features, ticker_id=ticker_id, episode_bars=episode_bars
    )

    computed = features.compute(episode)
    observed = computed.ffill().fillna(0.0)

    out = episode.copy()
    out.insert(0, "timestamp", _timestamp_strings(out))
    # Same fill the environment applies before handing rows to the policy.
    out[list(observed.columns)] = observed
    out.insert(len(out.columns), "warmup", _warmup_mask(computed))

    ohlcv = [c for c in _OHLCV_COLUMNS if c in out.columns]
    signals = [c for c in episode.columns if c not in set(_OHLCV_COLUMNS)]
    normalized: list[str] = []
    if include_normalized:
        z = features.transform(episode, ticker_id=normalize_ticker_id(ticker_id))
        zframe = pd.DataFrame(
            z, index=episode.index, columns=[f"z_{name}" for name in computed.columns]
        )
        normalized = list(zframe.columns)
        out = pd.concat([out, zframe], axis=1)

    out.attrs["stages"] = {
        "timestamp": ["timestamp"],
        "ohlcv": ohlcv,
        "signals": signals,
        "features": list(computed.columns),
        "normalized": normalized,
        "warmup": ["warmup"],
    }
    _LOGGER.info(
        "Built export frame: %d bars, %d columns (%d features, %d signal)",
        len(out),
        len(out.columns),
        len(computed.columns),
        len(signals),
    )
    return out


def default_export_path(ticker_id: str, root: str | Path = "exports") -> Path:
    """Default CSV destination: ``{root}/{TICKER_ID}.csv``."""
    return Path(root) / f"{normalize_ticker_id(ticker_id)}.csv"


def write_export_csv(frame: pd.DataFrame, path: str | Path) -> Path:
    """Write ``frame`` to ``path``, creating parent directories.

    Args:
        frame: Frame from :func:`build_export_frame`.
        path: Destination CSV path.

    Returns:
        The path written to.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination, index=False)
    return destination


__all__ = [
    "build_export_frame",
    "default_export_path",
    "write_export_csv",
]
