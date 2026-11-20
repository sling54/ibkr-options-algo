"""HTTP retries, the Nasdaq client and the earnings calendar cache."""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any

import pytest

from ibkr_options_algo.config import AppConfig
from ibkr_options_algo.earnings import (
    EarningsCalendar,
    next_earnings_dates,
    weekdays_ahead,
)
from ibkr_options_algo.errors import NetworkError
from ibkr_options_algo.sources.http import HttpClient
from ibkr_options_algo.sources.nasdaq import NasdaqClient
from ibkr_options_algo.storage import OutputStore
from tests.fake_nasdaq import FakeNasdaq


class Responses:
    """urlopen replacement that replays a list of results (exceptions are raised)."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls = 0
        self.timeouts: list[float | None] = []

    def __call__(self, request: Any, timeout: float | None = None) -> io.BytesIO:
        self.calls += 1
        self.timeouts.append(timeout)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(
            result if isinstance(result, bytes) else json.dumps(result).encode()
        )


def http(cfg: AppConfig, sleeps: list[float] | None = None) -> HttpClient:
    record = sleeps if sleeps is not None else []
    return HttpClient(cfg.network, sleep=record.append)


# ----------------------------------------------------------------------------- HTTP


def test_retries_transient_failures_with_backoff(
    cfg: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Responses(urllib.error.URLError("reset"), TimeoutError("slow"), {"ok": 1})
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    sleeps: list[float] = []
    assert http(cfg, sleeps).get_json("https://x") == {"ok": 1}
    assert fake.calls == 3
    assert sleeps == [1.0, 2.0]
    assert fake.timeouts == [cfg.network.timeout_seconds] * 3


def test_gives_up_after_retries(
    cfg: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Responses(*[urllib.error.URLError("down")] * 3)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(NetworkError, match="failed after 3 attempts"):
        http(cfg).get_json("https://x")


def test_client_errors_are_not_retried(
    cfg: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Responses(urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None))  # type: ignore[arg-type]
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    with pytest.raises(NetworkError, match="HTTP 403"):
        http(cfg).get_json("https://x")
    assert fake.calls == 1


def test_invalid_json_is_reported(
    cfg: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", Responses(b"<html>blocked</html>"))
    with pytest.raises(NetworkError, match="invalid JSON"):
        http(cfg).get_json("https://x")


# ----------------------------------------------------------------------------- Nasdaq


def test_market_cap_parsing(nasdaq_client: NasdaqClient) -> None:
    assert nasdaq_client.market_cap("NVDA") == pytest.approx(3.7e12)
    assert nasdaq_client.market_cap("UNLISTED") is None


def test_market_cap_none_and_warning_when_endpoint_down(
    nasdaq_client: NasdaqClient, nasdaq: FakeNasdaq, caplog: pytest.LogCaptureFixture
) -> None:
    nasdaq.down = True
    with caplog.at_level(logging.WARNING):
        assert nasdaq_client.market_cap("NVDA") is None
    assert "Market cap for NVDA unavailable" in caplog.text


def test_earnings_on_handles_empty_days(
    nasdaq_client: NasdaqClient, nasdaq: FakeNasdaq
) -> None:
    assert nasdaq_client.earnings_on(nasdaq.earnings_date("BABA")) == ["BABA"]
    assert nasdaq_client.earnings_on(nasdaq.today - timedelta(days=400)) == []


# ----------------------------------------------------------------------------- calendar


def test_weekdays_ahead_skips_weekends() -> None:
    friday = date(2026, 11, 20)
    assert weekdays_ahead(friday, 3) == [friday, date(2026, 11, 23)]


def test_next_earnings_dates_keeps_soonest() -> None:
    cal = {"2026-12-01": ["AAA"], "2026-11-25": ["AAA", "BBB"]}
    assert next_earnings_dates(cal, ["AAA", "BBB", "ETF"]) == {
        "AAA": date(2026, 11, 25),
        "BBB": date(2026, 11, 25),
        "ETF": None,
    }


def test_calendar_downloads_once_and_caches(
    earnings: EarningsCalendar, nasdaq: FakeNasdaq, store: OutputStore
) -> None:
    first = earnings.next_earnings(["BABA", "HOOD", "SPY"])
    assert first.dates["BABA"] == nasdaq.earnings_date("BABA")
    assert first.dates["SPY"] is None
    assert len(first.failed_days) == 1  # the fake fails one day
    requests = len(nasdaq.requests)
    assert store.earnings_cache.exists()

    second = earnings.next_earnings(["HOOD"])
    assert second.dates["HOOD"] == nasdaq.earnings_date("HOOD")
    assert len(nasdaq.requests) == requests  # served from the cache


def test_calendar_not_cached_when_too_many_days_fail(
    cfg: AppConfig, nasdaq: FakeNasdaq, nasdaq_client: NasdaqClient, store: OutputStore
) -> None:
    nasdaq.fail_dates = {nasdaq.today + timedelta(days=d) for d in range(0, 60, 3)}
    calendar = EarningsCalendar(
        nasdaq_client, cfg.earnings, store.earnings_cache, sleep=lambda _s: None
    )
    lookup = calendar.next_earnings(["BABA"])
    assert len(lookup.failed_days) > cfg.earnings.max_failed_days_to_cache
    assert not store.earnings_cache.exists()


def test_calendar_stops_calling_a_dead_endpoint(
    cfg: AppConfig,
    nasdaq: FakeNasdaq,
    nasdaq_client: NasdaqClient,
    store: OutputStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    nasdaq.down = True
    calendar = EarningsCalendar(
        nasdaq_client, cfg.earnings, store.earnings_cache, sleep=lambda _s: None
    )
    with caplog.at_level(logging.ERROR):
        lookup = calendar.next_earnings(["BABA"])
    attempts_per_day = cfg.network.retries + 1
    assert (
        len(nasdaq.requests)
        == cfg.earnings.abort_after_consecutive_failures * attempts_per_day
    )
    assert lookup.dates == {"BABA": None}
    assert len(lookup.failed_days) == len(
        weekdays_ahead(nasdaq.today, cfg.earnings.days_ahead)
    )
    assert "Check earnings dates manually" in caplog.text


def test_corrupt_cache_is_ignored(
    earnings: EarningsCalendar, store: OutputStore, nasdaq: FakeNasdaq
) -> None:
    store.earnings_cache.parent.mkdir(parents=True, exist_ok=True)
    store.earnings_cache.write_text("{not json")
    assert earnings.next_earnings(["BABA"]).dates["BABA"] == nasdaq.earnings_date(
        "BABA"
    )
