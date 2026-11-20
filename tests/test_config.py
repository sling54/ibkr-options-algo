"""Config loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ibkr_options_algo.config import AppConfig, load_config, parse_config
from ibkr_options_algo.errors import ConfigError
from ibkr_options_algo.models import Strategy
from tests.conftest import REPO_ROOT

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


def raw_config() -> dict[str, Any]:
    return tomllib.loads((REPO_ROOT / "config.toml").read_text())


# ----------------------------------------------------------------------------- config


def test_repo_config_loads(base_cfg: AppConfig) -> None:
    assert base_cfg.risk.max_trade_pct == 10
    assert base_cfg.pick.delta_tolerance == 0.08
    assert base_cfg.risk.strategy_priority[0] is Strategy.COVERED_CALL
    assert base_cfg.ibkr.paper_ports == (7497, 4002)


@pytest.mark.parametrize("port", [7496, 4001])
def test_live_ports_rejected(port: int) -> None:
    data = raw_config()
    data["ibkr"]["paper_ports"] = [7497, port]
    with pytest.raises(ConfigError, match="live trading port"):
        parse_config(data)


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("risk", "max_trade_pct", 0, "greater than 0"),
        ("risk", "max_trade_pct", 60, "cannot exceed max_total_pct"),
        ("risk", "strategy_priority", ["covered_call", "covered_call"], "twice"),
        ("risk", "strategy_priority", ["iron_condor"], "strategy_priority"),
        ("pick", "target_dte", 60, "min_dte <= target_dte <= max_dte"),
        ("pick", "put_delta", 1.5, "less than 1"),
        ("screen", "num_stocks", 80, "less than or equal to 50"),
    ],
)
def test_invalid_values_rejected(
    section: str, key: str, value: object, message: str
) -> None:
    data = raw_config()
    data[section][key] = value
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_unknown_and_missing_keys_rejected() -> None:
    data = raw_config()
    data["risk"]["max_trade_percent"] = 10
    with pytest.raises(ConfigError, match="max_trade_percent"):
        parse_config(data)
    data = raw_config()
    del data["risk"]["max_spread_pct"]
    with pytest.raises(ConfigError, match=r"risk\.max_spread_pct: Field required"):
        parse_config(data)


def test_missing_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("[risk\nmax = ")
    with pytest.raises(ConfigError, match="Could not parse"):
        load_config(bad)
