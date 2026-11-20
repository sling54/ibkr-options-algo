"""Nasdaq's free public endpoints: earnings calendar and market cap.

IBKR only provides earnings dates with a paid subscription, so they come from Nasdaq. The
endpoint is unofficial and could change; failures are reported, never silently ignored.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from ibkr_options_algo.errors import NetworkError
from ibkr_options_algo.sources.http import HttpClient

log = logging.getLogger(__name__)

EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings?date={day:%Y-%m-%d}"
QUOTE_SUMMARY_URL = "https://api.nasdaq.com/api/quote/{symbol}/summary?assetclass=stocks"


class NasdaqClient:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def earnings_on(self, day: date) -> list[str]:
        """Symbols reporting earnings on ``day``. Raises NetworkError on failure."""
        data = self._http.get_json(EARNINGS_URL.format(day=day))
        try:
            rows = (data.get("data") or {}).get("rows") or []
            return [str(r["symbol"]) for r in rows]
        except (AttributeError, KeyError, TypeError) as exc:
            raise NetworkError(f"Unexpected Nasdaq earnings response for {day}: {exc}") from exc

    def market_cap(self, symbol: str) -> float | None:
        """Company market cap in dollars, or None when Nasdaq can't say."""
        try:
            data: Any = self._http.get_json(QUOTE_SUMMARY_URL.format(symbol=symbol))
            value = data["data"]["summaryData"]["MarketCap"]["value"]
            return float(value.replace(",", ""))
        except NetworkError as exc:
            log.warning("Market cap for %s unavailable: %s", symbol, exc)
            return None
        except (KeyError, TypeError, ValueError, AttributeError):
            log.debug("Nasdaq quote summary for %s has no market cap", symbol)
            return None
