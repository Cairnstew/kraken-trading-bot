"""Reinforcement learning components for the Kraken Trading Bot.

This package provides the building blocks for training trading agents
with reinforcement learning:

- :class:`~kraken_trading_bot.rl.environment.TradingEnvironment` — a
  gymnasium-compatible, single-ticker trading environment.
- :class:`~kraken_trading_bot.rl.features.FeaturePipeline` — transforms
  raw OHLCV data into normalized features for the agent, with per-ticker
  normalization stats that can be persisted alongside trained models.
- :class:`~kraken_trading_bot.rl.features.NormalizationStats` — the
  per-ticker mean/std statistics saved/loaded as ``normalization.npz``.
- :class:`~kraken_trading_bot.rl.environment.RewardSpec` — configurable
  reward-shaping weights (PnL, risk-adjusted, drawdown, holding).
- :class:`~kraken_trading_bot.rl.registry.ModelRecord` and friends — a
  per-ticker model registry under ``models/{TICKER_ID}/{model_name}/``.
- :class:`~kraken_trading_bot.rl.agent.RLAgent` — a thin stable-baselines3
  PPO wrapper for train/load/predict.
- :func:`~kraken_trading_bot.rl.data.fetch_ohlc_dataframe` and
  :func:`~kraken_trading_bot.rl.data.prepare_episode` — OHLC data loading
  and episode slicing.
- :func:`~kraken_trading_bot.rl.train.train_ticker` — end-to-end
  training orchestrator.
- :func:`~kraken_trading_bot.rl.export.build_export_frame` — the same
  data pipeline stages composed into a CSV-ready frame.
- :func:`~kraken_trading_bot.rl.backtest.backtest_model` — walk-forward
  backtesting with standard performance metrics.
- :class:`~kraken_trading_bot.rl.paper_trade.PaperTrader` and
  :func:`~kraken_trading_bot.rl.paper_trade.run_paper_trader` — live
  paper-trading runner that steps a trained model against live market
  data through ``KrakenManager.paper()`` (no real orders).
"""

from __future__ import annotations

from .agent import RLAgent
from .backtest import ActionSpaceMismatchError, BacktestResult, backtest_model
from .data import (
    NotEnoughDataError,
    SignalTickerMismatchError,
    candles_to_dataframe,
    fetch_ohlc_dataframe,
    prepare_episode,
    read_ohlc_dataframe,
)
from .data_window import (
    DataWindow,
    clip_to_window,
    evaluation_frame,
    resolve_data_window,
    training_frame,
)
from .environment import RewardSpec, TradingEnvironment
from .export import build_export_frame, default_export_path, write_export_csv
from .features import FeaturePipeline, NormalizationStats, normalize_ticker_id
from .paper_trade import PaperSignal, PaperTrader, run_paper_trader
from .registry import ModelRecord, list_models, register_model
from .train import (
    build_train_config,
    load_train_config,
    pair_from_ticker_id,
    resolve_default_config_path,
    train_ticker,
)

__all__ = [
    "TradingEnvironment",
    "FeaturePipeline",
    "NormalizationStats",
    "RewardSpec",
    "normalize_ticker_id",
    "RLAgent",
    "ModelRecord",
    "list_models",
    "register_model",
    "NotEnoughDataError",
    "SignalTickerMismatchError",
    "candles_to_dataframe",
    "fetch_ohlc_dataframe",
    "read_ohlc_dataframe",
    "prepare_episode",
    "DataWindow",
    "resolve_data_window",
    "clip_to_window",
    "training_frame",
    "evaluation_frame",
    "load_train_config",
    "resolve_default_config_path",
    "build_train_config",
    "pair_from_ticker_id",
    "train_ticker",
    "build_export_frame",
    "default_export_path",
    "write_export_csv",
    "ActionSpaceMismatchError",
    "BacktestResult",
    "backtest_model",
    "PaperSignal",
    "PaperTrader",
    "run_paper_trader",
]