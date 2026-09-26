"""Model registry for the RL training pipeline.

Tracks trained models so multiple per-ticker models can coexist.  The
on-disk layout is ``models/{TICKER_ID}/{model_name}/`` containing, once
a model is fully trained::

    model.zip            trained stable-baselines3 policy
    normalization.npz    per-ticker feature normalization stats
    config.yaml          the exact training config used (same shape as
                         ``configs/default.yaml``)

A model is tied to one ticker: every artifact lives under a
ticker-scoped directory, and listing groups records by ticker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .features import normalize_ticker_id

_LOGGER = logging.getLogger(__name__)

# Canonical artifact filenames inside a model directory.
_CONFIG_FILENAME = "config.yaml"
_MODEL_FILENAME = "model.zip"
_NORMALIZATION_FILENAME = "normalization.npz"


@dataclass
class ModelRecord:
    """Metadata about one trained model on disk.

    Attributes:
        ticker_id: Normalized ticker the model trades (e.g. ``"ETH_USD"``).
        model_name: Name of this model under the ticker.
        config: The model's training config (values reloaded from its
            ``config.yaml``; empty dict if the file is missing).
        created: Filesystem mtime of the model directory (epoch seconds)
            as a best-effort creation timestamp.
        config_path: Path to ``config.yaml`` (None if missing).
        model_path: Path to ``model.zip`` (None if missing/not trained).
        normalization_path: Path to ``normalization.npz`` (None if missing).
        root: Registry root the record was scanned from.
    """

    ticker_id: str
    model_name: str
    config: dict[str, Any] = field(default_factory=dict)
    created: float | None = None
    config_path: Path | None = None
    model_path: Path | None = None
    normalization_path: Path | None = None
    root: Path | None = None

    def config_summary(self) -> dict[str, Any]:
        """A short, serializable summary of the training config.

        Returns:
            Dict with ticker, model_name, action_space, reward mode and
            feature window settings — the keys most useful for
            comparing models.
        """
        return {
            "ticker": self.config.get("ticker"),
            "model_name": self.model_name,
            "action_space": self.config.get("action_space"),
            "reward_mode": (self.config.get("reward") or {}).get("mode")
            if isinstance(self.config.get("reward"), dict)
            else None,
            "feature_windows": self.config.get("feature_windows"),
            "feature_groups": self.config.get("feature_groups"),
        }

    def is_trained(self) -> bool:
        """True when the model policy and normalization are both on disk."""
        return self.model_path is not None and self.normalization_path is not None


def models_root(root: str | Path = "models") -> Path:
    """Resolve the registry root directory.

    Args:
        root: Registry root; defaults to ``models/``.

    Returns:
        Absolute ``Path`` of the root (created if it does not exist).
    """
    path = Path(root).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def model_dir(
    ticker_id: str, model_name: str, root: str | Path = "models"
) -> Path:
    """Return the directory for one ticker/model pair.

    The per-ticker independence invariant: everything for a model lives
    under ``models/{TICKER_ID}/{model_name}/``.

    Args:
        ticker_id: Ticker (e.g. ``"ETH/USD"``; normalized on disk).
        model_name: Model name under the ticker.
        root: Registry root; defaults to ``models/``.

    Returns:
        Path to (and creates) ``root/{ticker_id}/{model_name}/``.
    """
    base = models_root(root)
    path = base / normalize_ticker_id(ticker_id) / model_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def register_model(
    ticker_id: str,
    model_name: str,
    config: dict[str, Any] | None = None,
    root: str | Path = "models",
) -> ModelRecord:
    """Register a model by creating its directory and writing ``config.yaml``.

    This is the metadata half of persistence; the policy (
    ``model.zip``) and normalization stats are written by the training
    pipeline.  The config is written under the exact YAML shape used by
    ``configs/default.yaml`` so per-run overrides round-trip.

    Args:
        ticker_id: Ticker the model trades (e.g. ``"ETH/USD"``).
        model_name: Model name under the ticker.
        config: Training config to persist; an empty dict is written
            when omitted.
        root: Registry root; defaults to ``models/``.

    Returns:
        The :class:`ModelRecord` for the newly registered model.
    """
    cfg = dict(config or {})
    directory = model_dir(ticker_id, model_name, root=root)
    cfg_path = directory / _CONFIG_FILENAME
    cfg_path.write_text(
        yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    _LOGGER.debug("Registered model %s/%s at %s", ticker_id, model_name, directory)
    return scan_model(ticker_id, model_name, root=root)


def list_models(root: str | Path = "models") -> dict[str, list[ModelRecord]]:
    """Scan the registry and return records grouped by ticker.

    Only directories that look like ``{TICKER_ID}/{model_name}`` (i.e.
    contain a non-ticker intermediate level) are considered.  Records
    for a ticker are sorted by model name.

    Args:
        root: Registry root; defaults to ``models/``.

    Returns:
        Dict mapping normalized ``ticker_id`` to its list of
        :class:`ModelRecord` entries.
    """
    base = models_root(root)
    grouped: dict[str, list[ModelRecord]] = {}
    if not base.is_dir():
        return grouped
    for ticker_dir in sorted(base.iterdir()):
        if not ticker_dir.is_dir():
            continue
        records = [
            scan_model(ticker_dir.name, model_name.name, root=base)
            for model_name in sorted(ticker_dir.iterdir())
            if model_name.is_dir()
        ]
        if records:
            grouped[ticker_dir.name] = records
    return grouped


def scan_model(
    ticker_id: str, model_name: str, root: str | Path = "models"
) -> ModelRecord:
    """Build a :class:`ModelRecord` for one ticker/model pair from disk.

    Args:
        ticker_id: Ticker (normalized on disk).
        model_name: Model name under the ticker.
        root: Registry root; defaults to ``models/``.

    Returns:
        The record describing what is currently on disk for the model.
    """
    directory = models_root(root) / normalize_ticker_id(ticker_id) / model_name

    cfg_path = directory / _CONFIG_FILENAME
    config: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            config = loaded if isinstance(loaded, dict) else {}
        except yaml.YAMLError:
            _LOGGER.warning("Ignoring unparseable config.yaml at %s", cfg_path)

    model_path = directory / _MODEL_FILENAME
    normalization_path = directory / _NORMALIZATION_FILENAME

    created = directory.stat().st_mtime if directory.is_dir() else None

    return ModelRecord(
        ticker_id=normalize_ticker_id(ticker_id),
        model_name=model_name,
        config=config,
        created=created,
        config_path=cfg_path if cfg_path.is_file() else None,
        model_path=model_path if model_path.is_file() else None,
        normalization_path=(
            normalization_path if normalization_path.is_file() else None
        ),
        root=models_root(root),
    )


__all__ = [
    "ModelRecord",
    "models_root",
    "model_dir",
    "register_model",
    "list_models",
    "scan_model",
]