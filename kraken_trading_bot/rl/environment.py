"""Reinforcement learning trading environment.

A gymnasium-compatible environment for training trading agents against
historical OHLCV data.  Each environment instance is specialized for ONE
ticker (e.g. ``ETH/USD``) and maintains its own balance, position, entry
price, equity curve and per-ticker normalization stats.

Action space (continuous): ``Box(3)`` - ``[buy_amount, sell_amount,
hold_confidence]`` with every component in ``[0, 1]``.
Action space (discrete): ``Discrete(3)`` - ``0`` buy, ``1`` hold, ``2``
sell; fractional sizing is applied to the balance/position.

Reward shaping is configurable through :class:`RewardSpec`: PnL-based
reward, risk-adjusted (Sharpe-style) reward, drawdown penalty and a
position-holding penalty, each with its own weight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import gymnasium as gym
import numpy as np
import pandas as pd
from gymnasium import spaces

from .features import (
    FeaturePipeline,
    NormalizationStats,
    normalize_ticker_id,
    observation_frame,
)

_LOGGER = logging.getLogger(__name__)

# Builtin fallback feature set used when no FeaturePipeline is supplied.
# Everything here is computed from raw OHLCV inside the environment.
_BUILTIN_FEATURES = (
    "return_1",
    "log_return_1",
    "return_5",
    "volatility_20",
    "rsi_14",
    "price_to_sma_20",
    "volume_zscore_20",
    "position_ratio",
)

# Minimum bars needed before the first usable observation with the
# builtin fallback set (20-bar rolling window + 5-bar return).
_MIN_BARS_BUILTIN = 24

# Default fraction of balance spent per discrete "buy" action.
_DISCRETE_BUY_FRACTION = 0.2


@dataclass
class RewardSpec:
    """Configuration for the environment's reward function.

    The total reward is a weighted sum of four terms:

    - ``pnl``: change in total equity (realized + unrealized PnL),
      scaled by ``pnl_scale`` so ``1.0`` equals a unit-equity change.
    - ``risk_adjusted``: the same equity change divided by the rolling
      volatility of equity changes (a per-step Sharpe proxy); periods
      with no volatility contribute zero.
    - ``drawdown_penalty``: negative term proportional to the drawdown
      from peak equity, applied when ``penalize_drawdown`` is True.
    - ``holding_penalty``: per-step penalty proportional to the absolute
      position held (discourages sitting in a large position).

    ``from_dict`` accepts plain dicts (e.g. from YAML config).  Passing
    ``{"mode": "pnl"}`` or ``{"mode": "sharpe"}`` selects the two common
    presets; explicit weight keys override ``mode``.
    """

    pnl: float = 1.0
    risk_adjusted: float = 0.0
    drawdown_penalty: float = 0.0
    holding_penalty: float = 0.0
    pnl_scale: float = 1e-4
    risk_window: int = 20
    penalize_drawdown: bool = True
    hold_confidence_threshold: float = 0.5

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RewardSpec":
        """Build a spec from a plain dict (e.g. loaded from YAML)."""
        data = dict(data or {})
        mode = data.pop("mode", None)
        spec = cls()
        if mode == "pnl":
            spec.pnl = 1.0
            spec.risk_adjusted = 0.0
        elif mode == "sharpe":
            spec.pnl = 0.0
            spec.risk_adjusted = 1.0
        elif mode is not None:
            raise ValueError(f"Unknown reward mode: {mode!r}")
        for key in (
            "pnl",
            "risk_adjusted",
            "drawdown_penalty",
            "holding_penalty",
            "pnl_scale",
            "risk_window",
            "penalize_drawdown",
            "hold_confidence_threshold",
        ):
            if key in data:
                setattr(spec, key, data[key])
        return spec


class TradingEnvironment(gym.Env):
    """A single-ticker trading environment for reinforcement learning.

    Parameters:
        ticker_id: Ticker this environment trades, e.g. ``"ETH/USD"``.
        data: OHLCV DataFrame with columns ``open``, ``high``, ``low``,
            ``close``, ``volume`` (plus optional microstructure columns),
            indexed by bar time.  The DataFrame is treated as immutable;
            the environment iterates over it.
        feature_pipeline: Optional :class:`FeaturePipeline`.  When given
            the environment uses its per-ticker normalization stats and
            feature vectors; when omitted a small builtin feature set is
            computed from raw prices.
        action_space: ``"continuous"`` (:class:`gymnasium.spaces.Box`) or
            ``"discrete"`` (:class:`gymnasium.spaces.Discrete`).
        initial_balance: Starting quote-currency balance (e.g. USD).
        fee_rate: Fractional trading fee applied to every executed order.
        slippage: Fractional adverse price move applied on fills.
        reward_spec: Reward weights/configuration, or a dict convertible
            via :meth:`RewardSpec.from_dict`.
        allow_short: When True, ``sell`` actions may open a short position
            beyond the current position; otherwise positions are
            long-only.
    """

    metadata = {"render_modes": ["human", "ansi"], "render_fps": 4}

    def __init__(
        self,
        ticker_id: str,
        data: pd.DataFrame,
        feature_pipeline: FeaturePipeline | None = None,
        action_space: str = "continuous",
        initial_balance: float = 10_000.0,
        fee_rate: float = 0.0,
        slippage: float = 0.0,
        reward_spec: RewardSpec | dict[str, Any] | None = None,
        allow_short: bool = False,
    ) -> None:
        super().__init__()
        if not isinstance(data, pd.DataFrame) or data.empty:
            raise ValueError("`data` must be a non-empty pandas DataFrame")
        missing = [
            c for c in ("open", "high", "low", "close", "volume") if c not in data.columns
        ]
        if missing:
            raise ValueError(f"Missing required OHLCV columns: {missing}")
        if action_space not in ("continuous", "discrete"):
            raise ValueError(f"Unknown action_space: {action_space!r}")

        self.ticker_id = normalize_ticker_id(ticker_id)
        self.ticker_raw = ticker_id
        self.data = data.reset_index(drop=True)
        self.n_bars = len(self.data)
        self.pipeline = feature_pipeline
        self.action_space_name = action_space
        self.initial_balance = float(initial_balance)
        self.fee_rate = float(fee_rate)
        self.slippage = float(slippage)
        self.reward_spec = (
            reward_spec
            if isinstance(reward_spec, RewardSpec)
            else RewardSpec.from_dict(reward_spec)
        )
        self.allow_short = bool(allow_short)

        # Feature matrix and observation space.
        if self.pipeline is not None:
            # Each env instance owns its ticker's normalization stats:
            # fit them from the provided data when they are not already
            # registered (e.g. loaded from a saved model).
            if self.pipeline.stats_for(self.ticker_id) is None:
                self.pipeline.fit(self.data, ticker_id=self.ticker_id)
            self._features = self.pipeline.compute(self.data)
            self._feature_names = list(self._features.columns)
            # Z-scored with the ticker's stats, so train, backtest and
            # paper all condition the policy on the same scale.
            self._feature_matrix = self._raw_feature_array()
            self._start_index = self._first_valid_index()
        else:
            self._feature_matrix, self._feature_names = self._builtin_features()
            self._start_index = _MIN_BARS_BUILTIN

        n_feats = self._feature_matrix.shape[1]
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(n_feats,), dtype=np.float32
        )

        if action_space == "continuous":
            self.action_space = spaces.Box(
                low=0.0, high=1.0, shape=(3,), dtype=np.float32
            )
        else:
            self.action_space = spaces.Discrete(3)

        self.reset(seed=0)

    # ------------------------------------------------------------------
    # gymnasium API
    # ------------------------------------------------------------------
    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        """Reset the environment to its initial state.

        Args:
            seed: Optional RNG seed (gymnasium protocol).
            options: Unused; reserved for future start-bar overrides.

        Returns:
            ``(observation, info)`` at the first valid feature index.
        """
        super().reset(seed=seed)
        self._step_idx = self._start_index
        self._balance = self.initial_balance
        self._position = 0.0
        self._entry_price: float | None = None
        self._equity_curve: list[float] = []
        self._trades = 0
        self._peak_equity = self.initial_balance
        self._last_equity = self.initial_balance
        self._reward_history: list[float] = []
        obs = self._observe()
        return obs, self._info()

    def step(
        self, action: Any
    ) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        """Advance one bar given an action.

        Reward is the change in total equity between this bar's close
        and the previous bar's close, after executing ``action`` at this
        bar's close price.

        Args:
            action: Continuous: array ``[buy_amount, sell_amount,
                hold_confidence]``.  Discrete: ``0`` buy, ``1`` hold,
                ``2`` sell.

        Returns:
            ``(observation, reward, terminated, truncated, info)``.
        """
        if self._step_idx >= self.n_bars:
            raise RuntimeError("step() called after the episode ended; call reset()")

        price = self._price_at(self._step_idx)
        self._step_idx += 1
        self._execute(action, price)
        equity_now = self._equity(price)

        self._equity_curve.append(equity_now)
        if equity_now > self._peak_equity:
            self._peak_equity = equity_now
        reward = self._compute_reward(equity_now)
        self._last_equity = equity_now

        # Long-only fills never drive balance below zero, so termination
        # is structural rather than conditional; keep the check for the
        # allow_short margin case and for future extensions.
        terminated = self._balance <= 0.0 and self._position <= 0.0
        truncated = self._step_idx >= self.n_bars

        obs = self._observe()
        return obs, reward, terminated, truncated, self._info()

    def render(self, mode: str | None = None) -> str | None:
        """Render a one-line summary of the current state.

        Args:
            mode: ``"human"`` prints; ``"ansi"`` returns the string.

        Returns:
            The summary string in ``"ansi"`` mode, else None.
        """
        line = (
            f"[{self.ticker_id}] step={self._step_idx}/{self.n_bars} "
            f"balance={self._balance:,.2f} position={self._position:.6f} "
            f"entry={self._entry_price if self._entry_price is not None else '-'} "
            f"equity={self.current_equity:,.2f} trades={self._trades}"
        )
        if mode == "ansi":
            return line
        print(line)
        return None

    def close(self) -> None:
        """Release resources (no-op for this environment)."""
        pass

    # ------------------------------------------------------------------
    # public state accessors
    # ------------------------------------------------------------------
    @property
    def balance(self) -> float:
        """Available quote-currency balance."""
        return self._balance

    @property
    def position(self) -> float:
        """Current base-currency position (units of the base asset)."""
        return self._position

    @property
    def entry_price(self) -> float | None:
        """Average entry price of the open position, if any."""
        return self._entry_price

    @property
    def unrealized_pnl(self) -> float:
        """Mark-to-market gain/loss of the open position in quote currency."""
        if self._entry_price is None or self._step_idx == 0:
            return 0.0
        price = self._price_at(min(self._step_idx, self.n_bars - 1))
        return self._position * (price - self._entry_price)

    @property
    def current_equity(self) -> float:
        """Balance plus liquidated value of the open position."""
        price = self._price_at(min(self._step_idx, self.n_bars - 1))
        return self._equity(price)

    @property
    def drawdown(self) -> float:
        """Current drawdown from peak equity as a positive fraction in [0, 1]."""
        peak = self._peak_equity
        if peak <= 0.0:
            return 0.0
        return max(0.0, (peak - self.current_equity) / peak)

    @property
    def equity_curve(self) -> np.ndarray:
        """Array of per-bar equity values up to the current step."""
        return np.asarray(self._equity_curve, dtype=float)

    @property
    def num_trades(self) -> int:
        """Number of executed buy/sell orders this episode."""
        return self._trades

    @property
    def feature_names(self) -> list[str]:
        """Names of the columns in the observation vector."""
        return list(self._feature_names)

    def normalization_stats(self) -> NormalizationStats | None:
        """Return the pipeline's stats for this ticker, if any."""
        if self.pipeline is None:
            return None
        return self.pipeline.stats_for(self.ticker_id)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _observe(self) -> np.ndarray:
        idx = min(self._step_idx, self.n_bars - 1)
        obs = self._feature_matrix[idx]
        if self.pipeline is None:
            # Tail feature (position_ratio) is filled live each step so
            # the agent always sees its current exposure.
            obs = obs.copy().astype(np.float32)
            obs[-1] = np.clip(self._position * self._price_at(idx), -1.0, 1.0)
        return np.asarray(obs, dtype=np.float32)

    def _execute(self, action: Any, price: float) -> None:
        buy_frac, sell_frac, hold_conf = self._interpret(action)

        if hold_conf >= self.reward_spec.hold_confidence_threshold:
            return  # agent holds this bar
        if buy_frac <= 0.0 and sell_frac <= 0.0:
            return

        if buy_frac > sell_frac:
            # Buy: budget `buy_frac` of available balance; fee taken in
            # units so the account always has positive cash.
            spend = self._balance * buy_frac
            if spend > 0.0:
                fill = price * (1.0 + self.slippage)
                units = (spend / fill) * (1.0 - self.fee_rate)
                self._balance -= spend
                self._position += units
                self._update_entry(fill, units)
                self._trades += 1
        elif sell_frac > 0.0 and (self._position > 0.0 or self.allow_short):
            # Sell: liquidate `sell_frac` of the position.
            units = self._position * sell_frac
            if units > 0.0:
                fill = price * (1.0 - self.slippage)
                proceeds = units * fill
                self._balance += proceeds * (1.0 - self.fee_rate)
                self._position -= units
                if abs(self._position) < 1e-12:
                    self._position = 0.0
                    self._entry_price = None
                self._trades += 1

    def _interpret(self, action: Any) -> tuple[float, float, float]:
        """Return ``(buy_frac, sell_frac, hold_conf)`` for any action."""
        if self.action_space_name == "continuous":
            arr = np.asarray(action, dtype=float).reshape(-1)
            if arr.size != 3:
                raise ValueError(
                    f"Continuous action must have 3 components, got {arr.size}"
                )
            buy_frac = float(np.clip(arr[0], 0.0, 1.0))
            sell_frac = float(np.clip(arr[1], 0.0, 1.0))
            hold_conf = float(np.clip(arr[2], 0.0, 1.0))
            return buy_frac, sell_frac, hold_conf
        # discrete: 0 = buy (spend 20% of balance), 1 = hold, 2 = sell all
        if int(action) == 0:
            return _DISCRETE_BUY_FRACTION, 0.0, 0.0
        if int(action) == 2:
            return 0.0, 1.0, 0.0
        return 0.0, 0.0, 1.0

    def _update_entry(self, fill: float, units: float) -> None:
        old_qty = self._position - units
        old_cost = old_qty * (self._entry_price if self._entry_price is not None else fill)
        total = self._position
        self._entry_price = (old_cost + units * fill) / total if total > 0 else fill

    def _equity(self, price: float) -> float:
        return self._balance + self._position * price

    def _price_at(self, idx: int) -> float:
        return float(self.data["close"].iloc[idx])

    def _first_valid_index(self) -> int:
        valid = self._features.notna().all(axis=1)
        first = valid.idxmax() if valid.any() else 0
        return max(int(first), 0)

    def _raw_feature_array(self) -> np.ndarray:
        """The observation matrix: ffilled features, z-scored with the stats.

        ``compute`` output is forward-filled then zero-filled (the frame
        the stats were fitted on), then passed through the ticker's
        :class:`NormalizationStats`.  The map is affine and per-feature, so
        the width and column order are exactly the computed feature set —
        only the scale changes, and a policy trained on these stats keeps
        loading unchanged.

        Without a pipeline there are no stats and the built-in features
        are already ratios, so they pass through unscaled.
        """
        observed = observation_frame(self._features)
        if self.pipeline is None:
            return observed.to_numpy(dtype=np.float32)
        stats = self.pipeline.stats_for(self.ticker_id)
        if stats is None:
            return observed.to_numpy(dtype=np.float32)
        return stats.normalize(observed).to_numpy(dtype=np.float32)

    def _builtin_features(self) -> tuple[np.ndarray, list[str]]:
        close = self.data["close"].astype(float)
        high = self.data["high"].astype(float)
        low = self.data["low"].astype(float)
        volume = self.data["volume"].astype(float)

        features = pd.DataFrame(index=self.data.index)
        ret = close.pct_change()
        features["return_1"] = ret
        features["log_return_1"] = np.log(close / close.shift(1))
        features["return_5"] = close.pct_change(5)
        features["volatility_20"] = ret.rolling(20).std(ddof=0)
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.ewm(alpha=1.0 / 14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0 / 14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        features["rsi_14"] = (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)
        sma20 = close.rolling(20).mean()
        features["price_to_sma_20"] = close / sma20 - 1.0
        vol_mean = volume.rolling(20).mean()
        vol_std = volume.rolling(20).std(ddof=0)
        features["volume_zscore_20"] = (volume - vol_mean) / vol_std.replace(0, 1.0)
        features["position_ratio"] = 0.0  # filled per-step in _observe()

        arr = features.to_numpy(dtype=np.float32)
        arr = pd.DataFrame(arr).ffill().fillna(0.0).to_numpy(dtype=np.float32)
        return arr, list(_BUILTIN_FEATURES)

    def _compute_reward(self, equity_now: float) -> float:
        spec = self.reward_spec
        delta = equity_now - self._last_equity
        scaled = delta * spec.pnl_scale

        reward = spec.pnl * scaled

        if spec.risk_adjusted != 0.0:
            hist = np.asarray(self._equity_curve, dtype=float)
            if hist.size >= 2:
                changes = np.diff(hist)
                window = changes[-spec.risk_window :] if spec.risk_window else changes
                std = float(np.std(window)) if window.size else 0.0
                risk = std / spec.pnl_scale if spec.pnl_scale else std
                adj = scaled / risk if risk > 1e-12 else 0.0
            else:
                adj = 0.0
            reward += spec.risk_adjusted * adj

        if spec.drawdown_penalty != 0.0 and spec.penalize_drawdown:
            peak = self._peak_equity
            dd = max(0.0, (peak - equity_now) / peak) if peak > 0.0 else 0.0
            reward -= spec.drawdown_penalty * dd

        if spec.holding_penalty != 0.0:
            reward -= spec.holding_penalty * abs(self._position)

        self._reward_history.append(reward)
        return float(reward)

    def _info(self) -> dict[str, Any]:
        return {
            "ticker_id": self.ticker_id,
            "step": self._step_idx,
            "balance": self._balance,
            "position": self._position,
            "entry_price": self._entry_price,
            "unrealized_pnl": self.unrealized_pnl,
            "equity": self.current_equity,
            "drawdown": self.drawdown,
            "trades": self._trades,
        }


__all__ = ["RewardSpec", "TradingEnvironment"]