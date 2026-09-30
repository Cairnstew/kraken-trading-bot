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
from .data import prepare_episode, read_ohlc_dataframe
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

    Args:
        config_path: Path to a YAML config with the shape of
            ``configs/default.yaml``; defaults to the resolved shipped
            config (see :func:`resolve_default_config_path`).

    Returns:
        Config dict (empty when the file is absent).  Raises on
        unparseable YAML.
    """
    path = (
        Path(config_path)
        if config_path is not None
        else resolve_default_config_path()
    )
    if not path.is_file():
        return {}
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
    4. Fit a :class:`FeaturePipeline` from the config on the fetched
       data.
    5. Build a :class:`TradingEnvironment` from the config (fee,
       slippage, reward spec, action space).
    6. Train the :class:`RLAgent` and save ``model.zip``.
    7. Save ``normalization.npz`` and ``config.yaml`` next to the
       policy and register the model.
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
    """
    cfg = build_train_config(ticker_id, model_name, config_path, **overrides)

    pair = cfg["ticker"]
    interval = int(cfg.get("ohlcv_interval_minutes", 60))
    action_space = cfg.get("action_space", "continuous")

    _LOGGER.info("Fetching %d pages of %s %d-minute OHLC", pages, pair, interval)
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
        feature_groups=cfg.get("feature_groups", None)
        or ["price", "technical", "volume", "microstructure", "signals"],
    )

    episode_df = prepare_episode(
        df,
        features,
        ticker_id=ticker_id,
        episode_bars=episode_bars,
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
        allow_short=bool(cfg.get("allow_short", False)),
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