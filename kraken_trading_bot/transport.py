"""HTTP transport layer for the Kraken API."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .auth import KrakenAuth
from .errors import APIError, AuthenticationError, RateLimitError

_LOGGER = logging.getLogger(__name__)


class KrakenTransport:
    """Low-level HTTP transport for the Kraken REST API.

    Handles request signing, rate limiting, and error parsing.
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "https://api.kraken.com",
        min_interval: float = 0.1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self._last_request_time = 0.0
        self._session = requests.Session()

        if api_key and api_secret:
            self._auth = KrakenAuth(api_key, api_secret)
        else:
            self._auth = None

    def _throttle(self) -> None:
        """Enforce minimum interval between requests."""
        if self.min_interval <= 0:
            return
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_time = time.time()

    def public(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        """Make a public (unauthenticated) API request.

        Args:
            endpoint: The Kraken API endpoint (e.g., "Ticker", "OHLC").
            params: Optional query parameters.

        Returns:
            The "result" portion of the API response.

        Raises:
            APIError: If the API returns an error.
        """
        self._throttle()
        url = f"{self.base_url}/0/public/{endpoint}"

        try:
            response = self._session.get(url, params=params, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            raise APIError([f"HTTP error: {e}"], endpoint) from e

        data = response.json()
        errors = data.get("error", [])
        if errors:
            raise APIError(errors, endpoint)

        return data.get("result", {})

    def private(self, endpoint: str, data: dict[str, Any] | None = None) -> Any:
        """Make a private (authenticated) API request.

        Args:
            endpoint: The Kraken API endpoint (e.g., "Balance", "AddOrder").
            data: Optional form data to send.

        Returns:
            The "result" portion of the API response.

        Raises:
            AuthenticationError: If no credentials are configured.
            APIError: If the API returns an error.
        """
        if self._auth is None:
            raise AuthenticationError("No API credentials configured for private requests")

        self._throttle()
        url = f"{self.base_url}/0/private/{endpoint}"
        request_data = data or {}

        try:
            headers = self._auth.sign(f"/0/private/{endpoint}", request_data)
            response = self._session.post(url, data=request_data, headers=headers, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            raise APIError([f"HTTP error: {e}"], endpoint) from e

        resp_data = response.json()
        errors = resp_data.get("error", [])
        if errors:
            # Check for rate limit errors
            if any("Rate limit" in err for err in errors):
                raise RateLimitError(errors, endpoint)
            raise APIError(errors, endpoint)

        return resp_data.get("result", {})
