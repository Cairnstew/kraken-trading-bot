"""PPO agent wrapper for the RL training pipeline.

A thin wrapper around `stable-baselines3`'s :class:`PPO` so the RL
backend stays swappable and model artifacts follow the per-ticker
registry layout (``models/{TICKER_ID}/{model_name}/model.zip``).

This intentionally re-implements nothing: PPO is trained/loaded via
stable-baselines3 and the wrapper only owns per-ticker naming and
predictable save/load paths.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from .features import normalize_ticker_id
from .registry import _MODEL_FILENAME, models_root

_LOGGER = logging.getLogger(__name__)


class RLAgent:
    """Train/load/predict wrapper around stable-baselines3 PPO.

    Args:
        ticker_id: Ticker the agent trades (e.g. ``"ETH/USD"``).
        model_name: Name for the trained model under the ticker.
        models_root: Registry root; defaults to ``models/``.
        seed: Training seed passed to PPO for reproducibility.
        device: Torch device for PPO (``"auto"``, ``"cpu"``, ``"cuda"``).
        model_kwargs: Extra kwargs forwarded to the ``PPO`` constructor
            (e.g. ``learning_rate``, ``n_steps``).
    """

    def __init__(
        self,
        ticker_id: str,
        model_name: str,
        models_root: str | Path = "models",
        seed: int | None = None,
        device: str = "auto",
        **model_kwargs: Any,
    ) -> None:
        self.ticker_id = normalize_ticker_id(ticker_id)
        self.model_name = model_name
        self.models_root = Path(models_root).expanduser()
        self.seed = seed
        self.device = device
        self.model_kwargs = dict(model_kwargs)
        self._model: Any | None = None

    # ------------------------------------------------------------------
    # paths
    # ------------------------------------------------------------------
    @property
    def save_dir(self) -> Path:
        """Per-ticker model directory ``models/{TICKER}/{NAME}/``."""
        return models_root(self.models_root) / self.ticker_id / self.model_name

    @property
    def model_path(self) -> Path:
        """Path of the saved policy artifact (``model.zip``)."""
        return self.save_dir / _MODEL_FILENAME

    # ------------------------------------------------------------------
    # training / persistence
    # ------------------------------------------------------------------
    def train(
        self,
        env: Any,
        total_timesteps: int,
        **learn_kwargs: Any,
    ) -> Path:
        """Train a PPO policy on ``env`` and save it under the model dir.

        Args:
            env: A gymnasium environment (typically a
                :class:`TradingEnvironment`).
            total_timesteps: Number of environment steps to train for.
            learn_kwargs: Extra kwargs forwarded to ``PPO.learn`` (e.g.
                ``progress_bar``, ``reset_num_timesteps``).

        Returns:
            Path of the saved ``model.zip``.

        Raises:
            RuntimeError: If stable-baselines3 is not installed.
        """
        try:
            from stable_baselines3 import PPO
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "stable-baselines3 is not installed; add it to the project "
                "dependencies (pyproject.toml) to train PPO agents."
            ) from exc

        kwargs = {
            "seed": self.seed,
            "device": self.device,
            **self.model_kwargs,
        }
        self._model = PPO("MlpPolicy", env, **kwargs)
        self._model.learn(total_timesteps=total_timesteps, **learn_kwargs)

        self.save_dir.mkdir(parents=True, exist_ok=True)
        self._model.save(self.model_path)
        _LOGGER.info(
            "Trained PPO for %s/%s (%d timesteps) -> %s",
            self.ticker_id,
            self.model_name,
            total_timesteps,
            self.model_path,
        )
        return self.model_path

    @classmethod
    def load(
        cls,
        ticker_id: str,
        model_name: str,
        env: Any,
        models_root: str | Path = "models",
        device: str = "auto",
    ) -> "RLAgent":
        """Load a saved model for a ticker into a new agent.

        Args:
            ticker_id: Ticker the saved model was trained for.
            model_name: Name of the saved model.
            env: An environment with the same observation/action spaces
                the model was trained with.
            models_root: Registry root; defaults to ``models/``.
            device: Torch device for PPO.

        Returns:
            A :class:`RLAgent` with the policy loaded; call
            :meth:`predict` to act.

        Raises:
            FileNotFoundError: If ``model.zip`` does not exist for the
                ticker/model pair.
        """
        from stable_baselines3 import PPO

        agent = cls(ticker_id=ticker_id, model_name=model_name, models_root=models_root)
        if not agent.model_path.is_file():
            raise FileNotFoundError(
                f"No trained model at {agent.model_path}; train it first "
                f"(RLAgent.train or rl.train_ticker)."
            )
        agent._model = PPO.load(agent.model_path, env=env, device=device)
        return agent

    # ------------------------------------------------------------------
    # inference
    # ------------------------------------------------------------------
    def predict(self, observation: Any, deterministic: bool = True) -> Any:
        """Return an action for ``observation`` using the trained policy.

        Args:
            observation: Observation vector (or batch) matching the
                environment's observation space.
            deterministic: Whether to sample greedily (True) or from
                the policy distribution (False).

        Returns:
            The action (numpy array for box spaces, scalar for discrete
            spaces).

        Raises:
            RuntimeError: If no policy is loaded (train/load first).
        """
        if self._model is None:
            raise RuntimeError("No policy loaded; call train() or inject via PPO.")
        action, _states = self._model.predict(
            observation, deterministic=deterministic
        )
        return action

    @property
    def model(self) -> Any:
        """The underlying stable-baselines3 model (None until trained/loaded)."""
        return self._model


__all__ = ["RLAgent"]