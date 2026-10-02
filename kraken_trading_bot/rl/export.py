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
    forward-filled and zero-filled. **Pre-transform features**: the
    environment applies the ticker's fitted ``NormalizationStats`` to
    this matrix before handing a row to the policy (see
    ``TradingEnvironment._raw_feature_array``) and
    ``PaperTrader._build_observation`` re-derives the same scaled row
    live.  These columns are the input to z-scoring, not the
    observation itself.

normalized (opt-in, ``z_``-prefixed)
    ``FeaturePipeline.transform``, i.e. the ticker's fitted stats applied
    to the same ffilled matrix — **this block is the agent's observation
    vector**: ``compute`` then ``ffill`` then ``fillna(0)`` then
    ``(x - mean) / std`` per feature, exactly what
    ``TradingEnvironment._observe`` returns (and what the policy was
    trained on).  Off by default so a plain export stays lossless and
    human-readable; ``--normalized`` turns the render of
    ``normalization.npz`` into the live observation.

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

from .data import _OHLCV_COLUMNS, _resolve_config_path, prepare_episode, read_ohlc_dataframe
from .features import FeaturePipeline, first_tradable_index, normalize_ticker_id
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

    Delegates to the environment's own boundary function
    (:func:`~kraken_trading_bot.rl.features.first_tradable_index`) rather
    than restating the rule, so the exported ``warmup`` flag cannot claim
    a different tradable region than
    :class:`~kraken_trading_bot.rl.environment.TradingEnvironment` uses.
    That is the indicator look-back region only: absence in a point-in-time
    exogenous column is not a warm-up bar.
    """
    first = first_tradable_index(features)
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
            ``FeaturePipeline.transform`` — the agent's z-scored
            observation, identical in construction to what the
            environment hands the policy.
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
        signal_max_age_hours=cfg.get("signal_max_age_hours"),
        signal_require_ticker=cfg.get("signal_require_ticker", True),
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

    # Drop columns the feature pipeline re-emits (the seam-derived
    # OHLCV scalars and any merged signal column): they are already in
    # `features`, and keeping the raw copy too would duplicate the column
    # in the CSV and misreport the stage decomposition.
    raw_only = [
        c for c in episode.columns if c not in set(computed.columns)
    ]
    out = episode[raw_only].copy()
    out.insert(0, "timestamp", _timestamp_strings(out))
    # Same fill the environment applies before handing rows to the policy.
    out[list(observed.columns)] = observed
    out.insert(len(out.columns), "warmup", _warmup_mask(computed))

    ohlcv = [c for c in _OHLCV_COLUMNS if c in out.columns]
    # "Everything the read seam added that is not one of the eight raw
    # OHLCV columns", minus anything the feature pipeline also emits.
    # The subtraction matters: the seam-derived scalars
    # (vwap_dev/trade_count_zscore_20/volume_per_trade) and every merged
    # signal column are passed through by the `signals` feature group, so
    # they are already in the `features` stage.  Listing them twice would
    # put a duplicate-named column in the CSV and make the stage
    # decomposition (which is the point of `attrs["stages"]`) a lie.
    computed_names = set(computed.columns)
    signals = [
        c
        for c in episode.columns
        if c not in set(_OHLCV_COLUMNS) and c not in computed_names
    ]
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
    """Default CSV destination: ``{root}/{TICKER_ID}.csv``.

    ``root`` is resolved with the same shared helper the signal and store
    seams use, so a ``~``-spelled ``--output`` root lands in the user's home
    rather than in a directory literally named ``~`` under the CWD.
    """
    resolved = _resolve_config_path(root)
    return (resolved if resolved is not None else Path("exports")) / (
        f"{normalize_ticker_id(ticker_id)}.csv"
    )


def write_export_csv(frame: pd.DataFrame, path: str | Path) -> Path:
    """Write ``frame`` to ``path``, creating parent directories.

    Args:
        frame: Frame from :func:`build_export_frame`.
        path: Destination CSV path; a ``~``-spelled value is expanded
            against ``$HOME``.

    Returns:
        The path written to.
    """
    resolved = _resolve_config_path(path)
    destination = resolved if resolved is not None else Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination, index=False)
    return destination


__all__ = [
    "build_export_frame",
    "default_export_path",
    "write_export_csv",
]
