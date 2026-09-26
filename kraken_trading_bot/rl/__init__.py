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
"""

from __future__ import annotations

from .environment import RewardSpec, TradingEnvironment
from .features import FeaturePipeline, NormalizationStats, normalize_ticker_id

__all__ = [
    "TradingEnvironment",
    "FeaturePipeline",
    "NormalizationStats",
    "RewardSpec",
    "normalize_ticker_id",
]