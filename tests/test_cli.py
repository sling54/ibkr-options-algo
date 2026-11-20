"""The ibkr-algo command line, on the fake broker."""

from __future__ import annotations

from pathlib import Path

import pytest

from ibkr_options_algo import cli
from ibkr_options_algo.config import AppConfig
from tests.conftest import REPO_ROOT
from tests.fake_ib import FakeIB, FakeWorld

# ----------------------------------------------------------------------------- CLI


@pytest.fixture
def cli_config(cfg: AppConfig, tmp_path: Path) -> Path:
    text = (REPO_ROOT / "config.toml").read_text()
    text = text.replace('directory = "output"', f'directory = "{cfg.output.directory}"')
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_cli_full_run_is_dry_by_default(
    cli_config: Path,
    world: FakeWorld,
    nasdaq: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)
    assert cli.main(["run-all", "--config", str(cli_config)], ib_factory=FakeIB) == 0
    out = capsys.readouterr().out
    assert "DRY RUN: nothing will be sent" in out
    assert "DAILY REPORT" in out
    assert world.placed == []


def test_cli_reports_missing_tws(
    cli_config: Path, world: FakeWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    world.open_ports = ()
    assert cli.main(["report", "--config", str(cli_config)], ib_factory=FakeIB) == 1
    assert "ERROR: Could not connect to TWS" in capsys.readouterr().out


def test_cli_reports_bad_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["screen", "--config", str(tmp_path / "missing.toml")]) == 1
    assert "not found" in capsys.readouterr().out


def test_cli_verbose_adds_timestamps(
    cli_config: Path, world: FakeWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    world.open_ports = ()
    cli.main(["-v", "report", "--config", str(cli_config)], ib_factory=FakeIB)
    out = capsys.readouterr().out
    assert " DEBUG " in out and "No TWS/Gateway on 127.0.0.1:7497" in out


def test_cli_live_flag_only_on_order_commands() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["orders", "--live"]).live is True
    assert parser.parse_args(["orders"]).live is False
    with pytest.raises(SystemExit):
        parser.parse_args(["report", "--live"])
