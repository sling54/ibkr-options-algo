"""A stand-in for Nasdaq's earnings-calendar and quote-summary endpoints.

Install it with ``monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)``.
"""

from __future__ import annotations

import io
import json
import urllib.error
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

SATURDAY = 5


def next_weekday(day: date) -> date:
    while day.weekday() >= SATURDAY:
        day += timedelta(days=1)
    return day


@dataclass
class FakeNasdaq:
    today: date
    #: symbol -> calendar days until earnings (moved forward to a weekday)
    earnings_in: dict[str, int] = field(default_factory=dict)
    market_caps: dict[str, float] = field(default_factory=dict)
    fail_dates: set[date] = field(default_factory=set)
    down: bool = False
    requests: list[str] = field(default_factory=list)

    def earnings_date(self, symbol: str) -> date:
        return next_weekday(self.today + timedelta(days=self.earnings_in[symbol]))

    def urlopen(self, request: Any, timeout: float | None = None) -> io.BytesIO:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.requests.append(url)
        if self.down:
            raise urllib.error.URLError("Temporary failure in name resolution")
        parsed = urlparse(url)
        if parsed.path == "/api/calendar/earnings":
            day = date.fromisoformat(parse_qs(parsed.query)["date"][0])
            if day in self.fail_dates:
                raise urllib.error.URLError("timed out")
            rows = [
                {"symbol": s, "name": f"{s} Inc"}
                for s in self.earnings_in
                if self.earnings_date(s) == day
            ]
            body: Any = {"data": {"rows": rows or None}}
        elif parsed.path.startswith("/api/quote/"):
            symbol = parsed.path.split("/")[3]
            cap = self.market_caps.get(symbol)
            summary = {"MarketCap": {"value": f"{cap:,.0f}"}} if cap else {}
            body = {"data": {"summaryData": summary}}
        else:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        return io.BytesIO(json.dumps(body).encode())


def default_nasdaq(today: date) -> FakeNasdaq:
    """Matches ``fake_ib.default_world``: see that docstring for the scenario."""
    return FakeNasdaq(
        today=today,
        earnings_in={"BABA": 2, "HOOD": 30, "META": 12, "AAPL": 9, "NVDA": 70},
        market_caps={
            "NVDA": 3.7e12,
            "BABA": 2.1e11,
            "HOOD": 6.0e10,
            "SMALL": 2.0e9,
            "WIDE": 1.5e10,
            "BIG": 5.0e10,
            "NOMARGIN": 1.2e10,
            "AAPL": 3.4e12,
            "META": 1.5e12,
            "TSLA": 1.1e12,
            "NOGREEK": 2.5e10,
        },
        fail_dates={next_weekday(today + timedelta(days=50))},
    )
