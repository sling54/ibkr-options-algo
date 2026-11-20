"""Upcoming earnings dates, downloaded from Nasdaq once a day and cached to disk.

A stock often moves 5-15% on earnings day. A premium seller with earnings before expiration
can lose months of premium overnight, so new trades near earnings are avoided.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ibkr_options_algo.config import EarningsConfig
from ibkr_options_algo.errors import NetworkError
from ibkr_options_algo.sources.nasdaq import NasdaqClient

log = logging.getLogger(__name__)

SATURDAY = 5
Calendar = dict[str, list[str]]


@dataclass(frozen=True, slots=True)
class EarningsLookup:
    """Next earnings date per symbol (None = none in the window), plus unchecked days."""

    dates: dict[str, date | None]
    failed_days: list[str]


def weekdays_ahead(today: date, days_ahead: int) -> list[date]:
    """Today and the next ``days_ahead`` days, weekends removed (no earnings on weekends)."""
    days = (today + timedelta(days=offset) for offset in range(days_ahead + 1))
    return [d for d in days if d.weekday() < SATURDAY]


def next_earnings_dates(
    calendar: Mapping[str, Sequence[str]], symbols: Iterable[str]
) -> dict[str, date | None]:
    """The soonest calendar date each symbol reports on."""
    found: dict[str, date | None] = dict.fromkeys(symbols)
    for day in sorted(calendar):
        for symbol in calendar[day]:
            if symbol in found and found[symbol] is None:
                found[symbol] = date.fromisoformat(day)
    return found


class EarningsCalendar:
    """Loads the calendar (from today's cache when present) and answers lookups."""

    def __init__(
        self,
        client: NasdaqClient,
        cfg: EarningsConfig,
        cache_path: Path,
        today: Callable[[], date] = date.today,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._cache_path = cache_path
        self._today = today
        self._sleep = sleep or time.sleep

    def next_earnings(self, symbols: Iterable[str]) -> EarningsLookup:
        """Next earnings date for each symbol over the configured window."""
        calendar, failed = self._load()
        return EarningsLookup(next_earnings_dates(calendar, symbols), failed)

    def _load(self) -> tuple[Calendar, list[str]]:
        today = self._today().isoformat()
        cached = self._read_cache()
        if cached is not None and cached.get("downloaded_on") == today:
            return cached["calendar"], cached["failed_days"]

        log.info(
            "Downloading earnings calendar for the next %d days from Nasdaq (~20 sec)...",
            self._cfg.days_ahead,
        )
        calendar: Calendar = {}
        failed: list[str] = []
        consecutive_failures = 0
        days = weekdays_ahead(self._today(), self._cfg.days_ahead)
        for index, day in enumerate(days):
            if consecutive_failures >= self._cfg.abort_after_consecutive_failures:
                remaining = [d.isoformat() for d in days[index:]]
                log.error(
                    "Nasdaq earnings endpoint failed %d days in a row; skipping the remaining "
                    "%d days. Check earnings dates manually before trading.",
                    consecutive_failures,
                    len(remaining),
                )
                failed.extend(remaining)
                break
            try:
                calendar[day.isoformat()] = self._client.earnings_on(day)
                consecutive_failures = 0
            except NetworkError as exc:
                log.debug("Earnings download failed for %s: %s", day, exc)
                failed.append(day.isoformat())
                consecutive_failures += 1
            self._sleep(self._cfg.request_delay_seconds)

        # Only cache a mostly complete download, so a bad run is retried next time.
        if len(failed) <= self._cfg.max_failed_days_to_cache:
            self._write_cache({"downloaded_on": today, "calendar": calendar, "failed_days": failed})
        return calendar, failed

    def _read_cache(self) -> dict[str, Any] | None:
        if not self._cache_path.exists():
            return None
        try:
            data = json.loads(self._cache_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Ignoring unreadable earnings cache %s: %s", self._cache_path, exc)
            return None
        if (
            not isinstance(data, dict)
            or not {"downloaded_on", "calendar", "failed_days"} <= data.keys()
        ):
            log.warning("Ignoring earnings cache %s with an unexpected format", self._cache_path)
            return None
        return data

    def _write_cache(self, data: dict[str, object]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(data))
