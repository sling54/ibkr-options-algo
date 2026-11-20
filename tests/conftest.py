"""Shared fixtures. No test needs TWS or the network."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ibkr_options_algo.config import AppConfig, load_config

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
