"""Small JSON-over-HTTP helper with timeouts and retries (standard library only)."""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ibkr_options_algo.config import NetworkConfig
from ibkr_options_algo.errors import NetworkError

log = logging.getLogger(__name__)

#: HTTP status codes worth retrying: rate limiting and server-side failures.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

Sleeper = Callable[[float], None]


class HttpClient:
    """GET JSON with a timeout, retrying transient failures with exponential backoff."""

    def __init__(self, cfg: NetworkConfig, sleep: Sleeper | None = None) -> None:
        self._cfg = cfg
        self._sleep = sleep or time.sleep

    def get_json(self, url: str) -> Any:
        """Return the decoded JSON body or raise NetworkError after the last attempt."""
        attempts = self._cfg.retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._fetch(url)
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRYABLE_STATUS:
                    raise NetworkError(f"GET {url} failed: HTTP {exc.code}") from exc
                last_error = exc
            except (OSError, http.client.HTTPException) as exc:  # URLError, timeouts, resets
                last_error = exc
            except json.JSONDecodeError as exc:
                raise NetworkError(f"GET {url} returned invalid JSON: {exc}") from exc
            if attempt < attempts:
                delay = self._cfg.backoff_seconds * 2 ** (attempt - 1)
                log.debug("GET %s failed (%s); retry %d in %.1fs", url, last_error, attempt, delay)
                self._sleep(delay)
        raise NetworkError(f"GET {url} failed after {attempts} attempts: {last_error}")

    def _fetch(self, url: str) -> Any:
        request = urllib.request.Request(
            url, headers={"User-Agent": self._cfg.user_agent, "Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self._cfg.timeout_seconds) as response:
            return json.load(response)
