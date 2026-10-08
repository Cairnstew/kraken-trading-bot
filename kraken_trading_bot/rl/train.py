"""Training orchestrator for the RL pipeline.

``train_ticker`` wires the registry, data loading, feature pipeline,
environment and PPO agent together: fetch OHLC history, fit features,
build the environment, train, and persist every artifact under the
per-ticker model directory (
``models/{TICKER_ID}/{model_name}/{model.zip,normalization.npz,config.yaml}``
).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .agent import RLAgent
from .data import (
    _resolve_config_path,
    prepare_episode,
    read_ohlc_dataframe,
    refresh_for_window,
)
from .data_window import resolve_data_window, training_frame
from .environment import TradingEnvironment
from .features import FeaturePipeline, normalize_ticker_id
from .registry import ModelRecord, register_model

_LOGGER = logging.getLogger(__name__)

# Repo-root config shipped with the package (parents: rl -> kraken_trading_bot -> root).
_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "configs" / "default.yaml"
)


def resolve_default_config_path() -> Path:
    """Locate the shipped default training config.

    Resolution order:

    1. Module-relative (``configs/default.yaml`` above the ``rl`` package)
       — correct for a source checkout or editable install.
    2. ``./configs/default.yaml`` under the current working directory —
       the packaged console script imports this module out of the Nix
       store, where ``configs/`` is not installed, so the module-relative
       path resolves to a non-existent store path.  The justfile and
       ``nix develop`` both run from the repo root, so honouring the CWD
       recovers the config users actually edit.

    Returns:
        The first candidate that exists, else the CWD candidate (which
        ``load_train_config`` reports as absent when it does not exist).
    """
    if _DEFAULT_CONFIG_PATH.is_file():
        return _DEFAULT_CONFIG_PATH
    return Path.cwd() / "configs" / "default.yaml"


def pair_from_ticker_id(ticker_id: str) -> str:
    """Map a ticker id back to Kraken pair notation.

    Args:
        ticker_id: e.g. ``"ETH_USD"``.

    Returns:
        e.g. ``"ETH/USD"``.
    """
    return normalize_ticker_id(ticker_id).replace("_", "/")


def load_train_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load a training config from YAML.

    An **explicitly passed** path is a declared intent to train from that
    file, so a path that does not resolve raises instead of returning an
    empty dict: silently training on CLI defaults because of a typo is the
    failure the ``fee_rate``/``slippage`` echo on a backtest result exists
    to make visible, and ``backtest._load_run_config`` already refuses the
    same way.  The *implicit* default (``config_path=None``) stays silent
    when absent, because the packaged console script imports this module out
    of the Nix store where ``configs/`` is not installed — see
    :func:`resolve_default_config_path`.

    Args:
        config_path: Path to a YAML config with the shape of
            ``configs/default.yaml``; defaults to the resolved shipped
            config (see :func:`resolve_default_config_path`).  A
            ``~``-spelled value is expanded against ``$HOME``.

    Returns:
        Config dict (empty when the *default* config is absent).  Raises on
        an explicitly passed path that does not exist, and on unparseable
        YAML.

    Raises:
        FileNotFoundError: If ``config_path`` was given but does not exist.
    """
    explicit = config_path is not None
    path = (
        _resolve_config_path(config_path)
        if explicit
        else resolve_default_config_path()
    )
    if not path.is_file():
        if not explicit:
            return {}
        raise FileNotFoundError(
            f"Train config not found: {path}. Pass --config with an existing "
            f"YAML file (e.g. configs/default.yaml), or omit it to load the "
            f"shipped default."
        )
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def build_train_config(
    ticker_id: str,
    model_name: str,
    config_path: str | Path | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Build the config used for one training run.

    Starts from the YAML config (defaults to ``configs/default.yaml``),
    pins ``ticker``/``model_name`` to this run, then applies
    ``overrides``.  Mapping overrides (e.g. ``reward``) shallow-merge
    into their config key rather than replacing wholesale.

    Args:
        ticker_id: Ticker being trained (e.g. ``"ETH/USD"``).
        model_name: Model name for this run.
        config_path: Optional config file to base the run on.
        overrides: Top-level config keys to override.

    Returns:
        Merged config dict.
    """
    cfg = load_train_config(config_path)
    cfg["ticker"] = pair_from_ticker_id(ticker_id)
    cfg["model_name"] = model_name
    for key, value in overrides.items():
        if (
            isinstance(value, dict)
            and isinstance(cfg.get(key), dict)
        ):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    return cfg


def train_ticker(
    ticker_id: str,
    model_name: str,
    config_path: str | Path | None = None,
    manager: Any | None = None,
    pages: int = 6,
    episode_bars: int | None = None,
    total_timesteps: int = 10_000,
    seed: int | None = 42,
    models_root: str | Path = "models",
    **overrides: Any,
) -> ModelRecord:
    """Train a PPO model for one ticker and persist it in the registry.

    Steps:
    1. Build the config (defaults to ``configs/default.yaml``; CLI args
       arrive as ``overrides``).
    2. Determine the Kraken pair from ``ticker_id`` (``ETH_USD`` ->
       ``ETH/USD``).
    3. Fetch OHLC history with client-side pagination (read through the
       local ``market_data_store`` when ``configs/default.yaml`` sets one;
       otherwise the live fetch, unchanged).
    3b. Clip to the pinned ``data_window`` (when the config sets one) and
       keep the leading ``eval_split`` fraction — the bars a matching
       ``backtest`` will replay out-of-sample.  With an unpinned window
       this is a no-op and the whole frame is used, exactly as before.
    4. Fit a :class:`FeaturePipeline` from the config on the fetched
       data.
    5. Build a :class:`TradingEnvironment` from the config (fee,
       slippage, reward spec, action space).
    6. Train the :class:`RLAgent` and save ``model.zip``.
    7. Save ``normalization.npz`` and ``config.yaml`` next to the
       policy and register the model.  ``config.yaml`` also carries the
       ``data_window`` it was trained on and the resulting ``n_bars``, so
       a later backtest (or a matrix report) can read the window back
       instead of re-deriving it.
    8. Return the :class:`ModelRecord`.

    Args:
        ticker_id: Ticker to train (e.g. ``"ETH/USD"``).
        model_name: Name under the ticker for the trained model.
        config_path: Optional YAML config to base the run on.
        manager: Optional KrakenManager for data fetching; defaults to
            ``KrakenManager.from_env()``.
        pages: OHLC pages to fetch (each ~720 candles).
        episode_bars: Optional cap on the episode length (trailing bars).
        total_timesteps: PPO training budget.
        seed: Training seed for reproducibility.
        models_root: Registry root; defaults to ``models/``.
        overrides: Config overrides (e.g. ``action_space="discrete"``).

    Returns:
        The registry record for the trained model.

    Raises:
        NotEnoughDataError: If too few OHLC bars are available.
        ValueError: If the config sets ``allow_short: true``.  Shorting is
            not implemented, so such a run would train LONG-ONLY while its
            artifact recorded a flag claiming otherwise.  Only training
            refuses: ``backtest_model``/``paper_trade`` read the flag off
            saved artifacts and keep warning instead, so every existing
            model still loads.
    """
    cfg = build_train_config(ticker_id, model_name, config_path, **overrides)

    pair = cfg["ticker"]
    interval = int(cfg.get("ohlcv_interval_minutes", 60))
    action_space = cfg.get("action_space", "continuous")

    _LOGGER.info("Fetching %d pages of %s %d-minute OHLC", pages, pair, interval)
    # The window is resolved *before* the read now, so the read can be
    # told to skip its fetch leg when the store already holds the whole
    # pinned window -- otherwise every training run spent 6 OHLC API
    # calls upserting 731 trailing bars that `training_frame` was about
    # to clip away.  Unpinned (the default) keeps the fetch: that IS the
    # trailing live window the fetch exists for.
    window = resolve_data_window(cfg)
    # Kept as named keywords rather than a `**` splat: the package's own
    # `test_every_read_ohlc_dataframe_call_site_forwards_venue` refuses to
    # prove a splatted call forwards anything ("cannot be proved is not
    # is fine"), and that rule is the right one at this seam.
    refresh_kwargs = refresh_for_window(
        since=window.since,
        until=window.until,
        is_pinned=window.is_pinned,
        window_label=window.describe(),
        pair=pair,
        interval=interval,
        store_setting=cfg.get("market_data_store"),
    )
    df = read_ohlc_dataframe(
        pair,
        interval=interval,
        pages=pages,
        manager=manager,
        extra_features_file=cfg.get("extra_features_file"),
        funding_features_file=cfg.get("funding_features_file"),
        social_features_file=cfg.get("social_features_file"),
        orderbook_features_file=cfg.get("orderbook_features_file"),
        signal_max_age_hours=cfg.get("signal_max_age_hours"),
        signal_require_ticker=cfg.get("signal_require_ticker", True),
        market_data_store=cfg.get("market_data_store"),
        market_data_store_venue=cfg.get("market_data_store_venue"),
        refresh=bool(refresh_kwargs["refresh"]),
        coverage=refresh_kwargs.get("coverage"),
    )

    # Pinned window + train/eval split.  `training_frame` returns the very
    # same object when the window is unpinned, so the default path reads
    # the whole freshly-fetched frame exactly as it always has.
    df = training_frame(df, window)
    if window.is_pinned:
        _LOGGER.info(
            "Training window %s -> %d bars (the remaining %d%% is held out "
            "for the out-of-sample backtest)",
            window.describe(),
            len(df),
            int(round((1.0 - window.eval_split) * 100)),
        )

    features = FeaturePipeline(
        windows=cfg.get("feature_windows", [1, 4, 24]),
        feature_groups=cfg.get("feature_groups", None)
        or ["price", "technical", "volume", "microstructure", "signals"],
    )

    episode_df = prepare_episode(
        df,
        features,
        ticker_id=ticker_id,
        episode_bars=episode_bars,
    )

    if cfg.get("allow_short", False):
        # RAISE, here and only here.  `allow_short` is a documented no-op:
        # `TradingEnvironment._execute`'s sell branch computes
        # `units = self._position * sell_frac`, which is 0.0 on a flat book,
        # so the `if units > 0.0` guard skips the trade and no short is ever
        # opened (measured 2026-10-05 -- an `allow_short=True` run from a
        # flat start hammering sell ends at position 0.0, trades 0, byte for
        # byte identical to `allow_short=False`).  So a training run
        # configured to short is silently LONG-ONLY, and its artifact
        # records `allow_short: true`, which is a lie about what was
        # traded.
        #
        # Training is the one place a *new* artifact is created, so it is
        # the one place refusing costs nothing.  `backtest_model` and
        # `paper_trade` keep only a WARNING: both resolve `allow_short`
        # from the artifact's own `config.yaml`, so raising there would
        # make every model ever trained with the flag unloadable -- and all
        # 90 saved configs across every matrix carry `allow_short: false`,
        # so nothing recorded is affected either way.
        raise ValueError(
            f"`allow_short: true` is set in the config for {ticker_id}, but "
            "shorting is NOT implemented, so this run would be LONG-ONLY "
            "while its artifact records a flag that says otherwise. "
            "TradingEnvironment._execute's sell branch computes "
            "units = position * sell_frac, which is 0 on a flat book, so "
            "the trade is skipped and no short is ever opened. Set "
            "`allow_short: false` (the default, and what every shipped "
            "config and all 90 saved model configs carry), or implement "
            "the short branch in TradingEnvironment._execute first."
        )

    env = TradingEnvironment(
        ticker_id=ticker_id,
        data=episode_df,
        feature_pipeline=features,
        action_space=action_space,
        initial_balance=float(cfg.get("initial_balance", 10_000.0)),
        fee_rate=float(cfg.get("fee_rate", 0.0)),
        slippage=float(cfg.get("slippage", 0.0)),
        reward_spec=cfg.get("reward"),
        allow_short=False,
    )

    agent = RLAgent(
        ticker_id=ticker_id,
        model_name=model_name,
        models_root=models_root,
        seed=seed,
    )
    _LOGGER.info(
        "Training PPO on %d bars of %s (%d obs features, %d timesteps)",
        len(episode_df),
        ticker_id,
        env.observation_space.shape[0],
        total_timesteps,
    )
    agent.train(env, total_timesteps=total_timesteps)

    # Width provenance, recorded BEFORE the config is written so it lands
    # in config.yaml.  `n_features` is what the registry surfaces and
    # what a stale-width artifact is detected by: a model trained before
    # the 49 -> 55 widening has no such key, which is exactly the signal
    # `registry.ModelRecord.is_stale_width` reports on.
    n_features = int(env.observation_space.shape[0])
    cfg["n_features"] = n_features

    # Window provenance, recorded alongside the width.  `n_bars` is the
    # magnitude guard for a report: a model trained on 12 bars and a model
    # trained on 3 000 produce returns of the same shape and none of the
    # same meaning, and the number that says which one this is was not
    # recoverable from the artifact.  `data_window` is already in `cfg`
    # (it came from the YAML), so it lands in config.yaml by itself and
    # a matching `backtest --config` can read back what it was trained
    # on rather than assuming.
    cfg["n_bars"] = int(len(episode_df))
    cfg.setdefault("train_timesteps", int(total_timesteps))

    # Persist the per-ticker normalization next to the policy.
    ticker_key = normalize_ticker_id(ticker_id)
    features.save_normalization(ticker_key, agent.save_dir / "normalization.npz")

    record = register_model(ticker_id, model_name, cfg, root=models_root)
    _LOGGER.info("Trained and registered %s/%s", ticker_key, model_name)
    return record


__all__ = [
    "pair_from_ticker_id",
    "resolve_default_config_path",
    "load_train_config",
    "build_train_config",
    "train_ticker",
]