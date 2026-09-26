"""Logging configuration for the Kraken Trading Bot."""

from __future__ import annotations

import logging
from typing import Any


def setup_logging(level: int = logging.INFO) -> None:
    """Configure logging for the trading bot.

    Sets up a clean, readable log format with timestamps.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Silence noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, **kwargs: Any) -> None:
    """Log a structured event with key-value pairs.

    Args:
        logger: The logger to use.
        event: The event name.
        **kwargs: Additional context as key-value pairs.
    """
    parts = [f"event={event}"]
    for k, v in kwargs.items():
        parts.append(f"{k}={v}")
    logger.info(" ".join(parts))
