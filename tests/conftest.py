"""Shared fixtures. No test needs TWS or the network."""

from __future__ import annotations

import urllib.request
from datetime import date
from pathlib import Path

import pytest

from ibkr_options_algo.config import AppConfig, load_config
from ibkr_options_algo.earnings import EarningsCalendar
from ibkr_options_algo.models import OptionQuote, Right
from ibkr_options_algo.sources.http import HttpClient
from ibkr_options_algo.sources.nasdaq import NasdaqClient
from ibkr_options_algo.storage import OutputStore
from tests.fake_nasdaq import FakeNasdaq, default_nasdaq

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def base_cfg() -> AppConfig:
    return load_config(REPO_ROOT / "config.toml")


@pytest.fixture
def cfg(base_cfg: AppConfig, tmp_path: Path) -> AppConfig:
    """The repo's config.toml, writing into a temporary output folder."""
    output = base_cfg.output.model_copy(update={"directory": tmp_path / "output"})
    return base_cfg.model_copy(update={"output": output})


@pytest.fixture
def today() -> date:
    return date.today()


@pytest.fixture
def nasdaq(today: date, monkeypatch: pytest.MonkeyPatch) -> FakeNasdaq:
    fake = default_nasdaq(today)
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


@pytest.fixture
def store(cfg: AppConfig) -> OutputStore:
    return OutputStore(cfg.output.directory)


@pytest.fixture
def nasdaq_client(cfg: AppConfig, nasdaq: FakeNasdaq) -> NasdaqClient:
    return NasdaqClient(HttpClient(cfg.network, sleep=lambda _s: None))


@pytest.fixture
def earnings(
    cfg: AppConfig, store: OutputStore, nasdaq_client: NasdaqClient
) -> EarningsCalendar:
    return EarningsCalendar(
        nasdaq_client, cfg.earnings, store.earnings_cache, sleep=lambda _s: None
    )


def quote(
    right: Right,
    strike: float,
    delta: float,
    bid: float = 1.0,
    ask: float = 1.1,
    con_id: int = 0,
) -> OptionQuote:
    """Build an OptionQuote with sensible defaults for tests."""
    return OptionQuote(
        con_id=con_id or int(strike * 100) + (1 if right == "C" else 0),
        right=right,
        strike=strike,
        bid=bid,
        ask=ask,
        delta=delta,
    )
