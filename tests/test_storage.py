"""CSV models and the output store."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from ibkr_options_algo.errors import MissingDataError
from ibkr_options_algo.models import (
    OrderLogEntry,
    Strategy,
    TradeIdea,
    UnderlyingContext,
)
from ibkr_options_algo.storage import OutputStore
from ibkr_options_algo.strategy import build_trade_idea
from tests.conftest import quote

# ----------------------------------------------------------------------------- CSV models


def _idea() -> TradeIdea:
    ctx = UnderlyingContext("HOOD", 68.55, "20261224", 37, False, 69.0, date(2026, 12, 17))
    return build_trade_idea(
        ctx,
        Strategy.CASH_SECURED_PUT,
        put=quote("P", 60.0, -0.248, 2.85, 2.95, 942725253),
        capital=6000.0,
    )


def test_trade_idea_csv_row_matches_original_format() -> None:
    row = _idea().to_csv_row()
    assert list(row)[:5] == ["symbol", "strategy", "is_etf", "iv_rank", "stock_price"]
    assert row["is_etf"] == ""
    assert row["call_strike"] == ""
    assert row["next_earnings"] == "2026-12-17"
    assert row["earnings_before_expiry"] == "YES"
    assert row["put_conid"] == 942725253


def test_trade_idea_round_trips_through_csv(tmp_path: Path) -> None:
    store = OutputStore(tmp_path)
    idea = _idea()
    store.write_proposals([idea])
    _, loaded = store.latest_proposals()
    assert loaded == [idea]


def test_newest_file_wins_and_candidates_filter(tmp_path: Path) -> None:
    store = OutputStore(tmp_path)
    header = "rank,symbol,iv_rank,sell_premium_candidate\n"
    (tmp_path / "vol_screen_20261117_1000.csv").write_text(header + "1,OLD,90.0,YES\n")
    (tmp_path / "vol_screen_20261118_0900.csv").write_text(
        header + "1,NVDA,99.0,YES\n2,AAPL,10.0,\n"
    )
    path, candidates = store.latest_candidates()
    assert path.name == "vol_screen_20261118_0900.csv"
    assert [(c.symbol, c.iv_rank) for c in candidates] == [("NVDA", 99.0)]


def test_missing_inputs_give_clear_errors(tmp_path: Path) -> None:
    store = OutputStore(tmp_path)
    with pytest.raises(MissingDataError, match="ibkr-algo screen"):
        store.latest_candidates()
    with pytest.raises(MissingDataError, match="ibkr-algo pick"):
        store.latest_proposals()


def test_order_log_appends_with_single_header(tmp_path: Path) -> None:
    store = OutputStore(tmp_path / "out", now=lambda: datetime(2026, 11, 20, 10, 0))
    entry = OrderLogEntry(
        "2026-11-20 10:00:00",
        "dry_run",
        None,
        "dry_run",
        "HOOD",
        "cash_secured_put",
        "20261224",
        60.0,
        None,
        1,
        2.9,
        6000.0,
    )
    store.append_order_log(entry)
    store.append_order_log(entry)
    lines = store.order_log.read_text().splitlines()
    assert lines[0].startswith("time,mode,order_id")
    assert (
        lines[1]
        == "2026-11-20 10:00:00,dry_run,,dry_run,HOOD,cash_secured_put,20261224,60.0,,1,2.9,6000.0"
    )
    assert len(lines) == 3
    assert [e.order_id for e in store.read_order_log()] == ["", ""]


def test_outputs_go_into_output_directory(tmp_path: Path) -> None:
    store = OutputStore(tmp_path / "output", now=lambda: datetime(2026, 11, 20, 14, 4))
    path = store.write_proposals([_idea()])
    assert path == tmp_path / "output" / "proposed_trades_20261120_1404.csv"
    paths = store.write_report("hello", [], date(2026, 11, 20))
    assert paths == [tmp_path / "output" / "daily_report_20261120.txt"]
