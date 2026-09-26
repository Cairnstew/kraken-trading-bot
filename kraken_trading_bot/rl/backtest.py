"""Backtesting evaluation for the RL pipeline.

Replays a trained PPO agent over OHLCV data in a
:class:`TradingEnvironment` and produces standard performance metrics:
total return, per-bar Sharpe, max drawdown, trade count and win rate,
plus the full equity curve.

Reuses the environment's fee/slippage/reward configuration: when the
model directory contains ``normalization.npz`` the pipeline path is
reconstructed exactly as at training time; otherwise the environment's
builtin fallback features are used.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .agent import RLAgent
from .data import fetch_ohlc_dataframe
from .environment import TradingEnvironment
from .features import FeaturePipeline, normalize_ticker_id
from .registry import ModelRecord, scan_model

_LOGGER = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Performance summary of one backtest run.

    Attributes:
        ticker_id: Normalized ticker backtested (e.g. ``"ETH_USD"``).
        model_name: Model name backtested.
        total_return: Final equity / initial balance - 1.
        sharpe: Per-bar Sharpe ratio (mean / std of per-bar equity
            returns scaled by sqrt(n)); 0 when returns are flat.
        max_drawdown: Largest peak-to-trough equity loss as a positive
            fraction of the running peak.
        num_trades: Number of executed orders.
        win_rate: Fraction of fully-closed round trips with positive
            realized profit; 0.0 when no position was fully closed.
        equity_curve: Per-bar equity values, starting at the initial
            balance (length n_steps + 1).
        n_steps: Number of environment steps executed.
        final_equity: Equity after the final step.
        seed: Seed used for the deterministic replay.
    """

    ticker_id: str
    model_name: str
    total_return: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    num_trades: int = 0
    win_rate: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    n_steps: int = 0
    final_equity: float = 0.0
    seed: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable dict view of the result."""
        return {
            "ticker_id": self.ticker_id,
            "model_name": self.model_name,
            "total_return": self.total_return,
            "sharpe": self.sharpe,
            "max_drawdown": self.max_drawdown,
            "num_trades": self.num_trades,
            "win_rate": self.win_rate,
            "equity_curve": list(self.equity_curve),
            "n_steps": self.n_steps,
            "final_equity": self.final_equity,
            "seed": self.seed,
        }


def backtest_model(
    ticker_id: str,
    model_name: str,
    data: pd.DataFrame | None = None,
    agent: RLAgent | None = None,
    models_root: str | Path = "models",
    manager: Any | None = None,
    pages: int = 6,
    env_kwargs: dict[str, Any] | None = None,
    seed: int | None = 42,
    deterministic: bool = True,
) -> BacktestResult:
    """Walk a trained agent forward over OHLC data and compute metrics.

    Args:
        ticker_id: Ticker the model was trained on (e.g. ``"ETH/USD"``).
        model_name: Name of the saved model.
        data: OHLCV DataFrame to replay over.  When None, fresh data is
            fetched via ``manager`` (``KrakenManager.from_env()`` when
            no manager is given).
        agent: Optional pre-loaded :class:`RLAgent`; when omitted the
            model is loaded from ``models/{TICKER}/{NAME}/model.zip``.
        models_root: Registry root; defaults to ``models/``.
        manager: Optional KrakenManager used only when ``data`` is None.
        pages: OHLC pages to fetch when ``data`` is None.
        env_kwargs: Extra kwargs passed to the
            :class:`TradingEnvironment` constructor (overrides any
            per-model config loaded from the registry, e.g.
            ``action_space``).
        seed: Seed for the deterministic replay (0 disables seeding).
        deterministic: Whether the policy acts greedily.

    Returns:
        The :class:`BacktestResult` for the run.

    Raises:
        ValueError: If neither ``data`` nor a way to fetch it exists.
        FileNotFoundError: If ``agent`` is omitted and ``model.zip`` is
            missing.
        NotEnoughDataError: If too few OHLC bars were provided/fetched.
    """
    ticker_key = normalize_ticker_id(ticker_id)
    env_kwargs = dict(env_kwargs or {})

    # 1. Data
    if data is None:
        if manager is None:
            from kraken_api import KrakenManager

            manager = KrakenManager.from_env()
        record = scan_model(ticker_id, model_name, root=models_root)
        interval = int(
            (record.config or {}).get("ohlcv_interval_minutes", 60)
        )
        df = fetch_ohlc_dataframe(
            record.config.get("ticker", ticker_key.replace("_", "/")),
            interval=interval,
            pages=pages,
            manager=manager,
        )
    else:
        df = data

    # 2. Environment: reconstruct the pipeline when normalization exists.
    record = scan_model(ticker_id, model_name, root=models_root)
    config = record.config or {}
    pipeline: FeaturePipeline | None = None
    if record.normalization_path is not None:
        pipeline = FeaturePipeline(
            windows=config.get("feature_windows", [1, 4, 24]),
            feature_groups=config.get("feature_groups", None)
            or ["price", "technical", "volume", "microstructure"],
        )
        pipeline.load_normalization(ticker_key, record.normalization_path)

    env = TradingEnvironment(
        ticker_id=ticker_id,
        data=df,
        feature_pipeline=pipeline,
        action_space=env_kwargs.pop("action_space", config.get("action_space", "continuous")),
        initial_balance=float(
            env_kwargs.pop("initial_balance", config.get("initial_balance", 10_000.0))
        ),
        fee_rate=float(env_kwargs.pop("fee_rate", config.get("fee_rate", 0.0))),
        slippage=float(env_kwargs.pop("slippage", config.get("slippage", 0.0))),
        reward_spec=env_kwargs.pop("reward", config.get("reward")),
        allow_short=bool(
            env_kwargs.pop("allow_short", config.get("allow_short", False))
        ),
        **env_kwargs,
    )

    # 3. Agent (load when not provided).
    if agent is None:
        agent = RLAgent.load(
            ticker_id, model_name, env, models_root=models_root
        )
    else:
        if agent.model is None:
            raise ValueError(
                "`agent` has no policy loaded; call RLAgent.load first."
            )

    # 4. Deterministic replay.
    obs, _ = env.reset(seed=seed)
    closes = df["close"].astype(float).to_numpy()
    initial_balance = float(env.initial_balance)
    equity_curve: list[float] = [initial_balance]
    wins = 0
    closes_completed = 0
    prev_trades = 0

    i = 0
    while True:
        price = float(closes[i])
        entry_before = env.entry_price
        action = agent.predict(obs, deterministic=deterministic)
        obs, _reward, terminated, truncated, info = env.step(action)

        # A full round trip completed this bar when a position that was
        # open before the step is fully closed afterwards (the env clears
        # entry_price only when the whole position liquidates).
        if env.entry_price is None and entry_before is not None:
            fill = price * (1.0 - env.slippage)
            wins += 1 if fill > entry_before else 0
            closes_completed += 1
        prev_trades = info["trades"]

        equity_curve.append(float(info["equity"]))
        i += 1
        if terminated or truncated or i >= len(closes):
            break

    # 5. Metrics.
    eq = np.asarray(equity_curve, dtype=float)
    final_equity = float(eq[-1])
    total_return = final_equity / initial_balance - 1.0

    rets = np.diff(eq) / eq[:-1]
    if rets.size >= 2 and float(np.std(rets)) > 1e-12:
        sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(rets.size))
    else:
        sharpe = 0.0

    peak = np.maximum.accumulate(eq)
    drawdowns = (peak - eq) / peak
    max_drawdown = float(np.max(drawdowns)) if drawdowns.size else 0.0

    num_trades = env.num_trades
    win_rate = wins / closes_completed if closes_completed else 0.0

    result = BacktestResult(
        ticker_id=ticker_key,
        model_name=model_name,
        total_return=total_return,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        num_trades=num_trades,
        win_rate=win_rate,
        equity_curve=equity_curve,
        n_steps=i,
        final_equity=final_equity,
        seed=seed,
    )
    _LOGGER.info(
        "Backtest %s/%s: %d steps, return=%.2f%% max_dd=%.2f%% trades=%d win=%.2f%%",
        ticker_key,
        model_name,
        i,
        total_return * 100.0,
        max_drawdown * 100.0,
        num_trades,
        win_rate * 100.0,
    )
    return result


__all__ = ["BacktestResult", "backtest_model"]