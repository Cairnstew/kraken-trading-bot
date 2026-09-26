"""Authentication helpers for the Kraken Trading Bot."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import base64
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import requests

from .errors import AuthenticationError, ConfigurationError

if TYPE_CHECKING:
    from .client import KrakenClient


class KrakenAuth:
    """Handle Kraken API authentication (HMAC-SHA512 signing)."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = base64.b64decode(api_secret)

    def sign(self, url_path: str, data: dict[str, Any]) -> dict[str, str]:
        """Sign a request and return headers with API-Key and API-Sign."""
        # Add nonce to the request data
        data["nonce"] = str(int(time.time() * 1000))

        # URL-encode the data
        post_data = urlencode(data)

        # Create the message to sign: url_path + SHA256(post_data)
        encoded = (str(len(post_data)) + post_data).encode()
        message = url_path.encode() + hashlib.sha256(encoded).digest()

        # Sign with HMAC-SHA512
        signature = hmac.new(self.api_secret, message, hashlib.sha512)
        signature_b64 = base64.b64encode(signature.digest()).decode()

        return {
            "API-Key": self.api_key,
            "API-Sign": signature_b64,
        }


def load_credentials_from_env() -> tuple[str, str]:
    """Load Kraken API credentials from environment variables.

    Returns:
        Tuple of (api_key, api_secret).

    Raises:
        ConfigurationError: If credentials are not set.
    """
    api_key = os.environ.get("KRAKEN_API_KEY", "")
    api_secret = os.environ.get("KRAKEN_API_SECRET", "")

    if not api_key or not api_secret:
        raise ConfigurationError(
            "KRAKEN_API_KEY and KRAKEN_API_SECRET environment variables must be set"
        )

    return api_key, api_secret


def client_from_env(**kwargs: Any) -> KrakenClient:
    """Create a KrakenClient from environment variables.

    This is a convenience factory that loads credentials from
    KRAKEN_API_KEY/KRAKEN_API_SECRET environment variables.
    """
    from .client import KrakenClient

    api_key, api_secret = load_credentials_from_env()
    return KrakenClient(api_key=api_key, api_secret=api_secret, **kwargs)


def client_from_credentials(api_key: str, api_secret: str, **kwargs: Any) -> KrakenClient:
    """Create a KrakenClient from explicit credentials.

    This is useful for programmatic access when you have the credentials
    in code (e.g., for testing).
    """
    from .client import KrakenClient

    return KrakenClient(api_key=api_key, api_secret=api_secret, **kwargs)
