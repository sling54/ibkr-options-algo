"""Every service end to end on the fake broker and fake Nasdaq."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ibkr_options_algo.config import AppConfig
from ibkr_options_algo.earnings import EarningsCalendar
from ibkr_options_algo.errors import LiveAccountError, MissingDataError
from ibkr_options_algo.ibkr.connection import Connector, probe
from ibkr_options_algo.models import OrderLogEntry, Strategy
from ibkr_options_algo.services.chain import run_chain
from ibkr_options_algo.services.check import run_check
from ibkr_options_algo.services.orders import run_orders
from ibkr_options_algo.services.pick import run_pick
from ibkr_options_algo.services.report import run_report
from ibkr_options_algo.services.screen import run_screen
from ibkr_options_algo.sources.nasdaq import NasdaqClient
from ibkr_options_algo.storage import OutputStore
from tests.fake_ib import FakeIB, FakeWorld, risk_world


def read(path: Path) -> list[dict[str, str]]:
    with path.open() as f:
        return list(csv.DictReader(f))


@pytest.fixture
def pipeline(
    cfg: AppConfig, connect: Connector, store: OutputStore, earnings: EarningsCalendar
) -> tuple[Path, Path]:
    """Run screen + pick; return both output files."""
    return run_screen(cfg, connect, store), run_pick(cfg, connect, store, earnings)


# ----------------------------------------------------------------------------- screen / pick


def test_screen_ranks_and_skips_short_history(
    cfg: AppConfig,
    connect: Connector,
    store: OutputStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = read(run_screen(cfg, connect, store))
    symbols = [r["symbol"] for r in rows]
    assert "NEW" not in symbols and "BROKE" not in symbols
    assert [int(r["rank"]) for r in rows] == list(range(1, len(rows) + 1))
    ranks = [float(r["iv_rank"]) for r in rows]
    assert ranks == sorted(ranks, reverse=True)
    assert {r["symbol"] for r in rows if r["sell_premium_candidate"] == "YES"} >= {
        "NVDA",
        "SPY",
    }
    assert "LOWIV" in symbols and "hv_20d_%" in rows[0]
    assert "BROKE: error" in caplog.text


def test_pick_builds_every_strategy_and_skips_earnings(
    pipeline: tuple[Path, Path],
) -> None:
    ideas = read(pipeline[1])
    by_symbol = {(r["symbol"], r["strategy"]) for r in ideas}
    assert ("AAPL", "covered_call") in by_symbol  # 250 shares owned, not a candidate
    assert ("AAPL", "cash_secured_put") not in by_symbol
    assert not any(s == "BABA" for s, _ in by_symbol)  # earnings in 2 days
    assert ("NOMARGIN", "short_strangle") not in by_symbol  # IBKR gave no margin
    assert ("NOMARGIN", "cash_secured_put") in by_symbol
    assert not any(s == "NOGREEK" for s, _ in by_symbol)  # no deltas, no ideas
    assert {r["strategy"] for r in ideas} == {
        "cash_secured_put",
        "covered_call",
        "short_strangle",
        "short_straddle",
    }
    hood = next(r for r in ideas if r["symbol"] == "HOOD")
    assert hood["earnings_before_expiry"] == "YES"
    cc = next(r for r in ideas if r["strategy"] == "covered_call")
    assert cc["contracts"] == "2"


def test_pick_without_screen_file(
    cfg: AppConfig, connect: Connector, store: OutputStore, earnings: EarningsCalendar
) -> None:
    with pytest.raises(MissingDataError, match="Run `ibkr-algo screen` first"):
        run_pick(cfg, connect, store, earnings)


# ----------------------------------------------------------------------------- orders


def run(
    cfg: AppConfig,
    connect: Connector,
    store: OutputStore,
    earnings: EarningsCalendar,
    nasdaq_client: NasdaqClient,
    live: bool = False,
) -> int:
    return run_orders(
        cfg, connect, store, earnings, nasdaq_client.market_cap, live=live
    )


def test_dry_run_never_places_orders(
    pipeline: tuple[Path, Path],
    cfg: AppConfig,
    connect: Connector,
    store: OutputStore,
    earnings: EarningsCalendar,
    nasdaq_client: NasdaqClient,
    world: FakeWorld,
) -> None:
    placed = run(cfg, connect, store, earnings, nasdaq_client)
    assert placed > 0
    assert world.placed == []
    log = read(store.order_log)
    assert {r["mode"] for r in log} == {"dry_run"}
    assert all(r["order_id"] == "" for r in log)


def test_live_account_detected_stops_before_any_order(
    pipeline: tuple[Path, Path],
    cfg: AppConfig,
    connect: Connector,
    store: OutputStore,
    earnings: EarningsCalendar,
    nasdaq_client: NasdaqClient,
    world: FakeWorld,
) -> None:
    world.account = "U1234567"
    with pytest.raises(LiveAccountError, match="not a paper account"):
        run(cfg, connect, store, earnings, nasdaq_client, live=True)
    assert world.placed == []


def test_stale_proposals_refused(
    pipeline: tuple[Path, Path],
    cfg: AppConfig,
    connect: Connector,
    store: OutputStore,
    earnings: EarningsCalendar,
    nasdaq_client: NasdaqClient,
) -> None:
    tomorrow = datetime.now() + timedelta(days=1)
    with pytest.raises(MissingDataError, match="is not from today"):
        run_orders(
            cfg,
            connect,
            store,
            earnings,
            nasdaq_client.market_cap,
            now=lambda: tomorrow,
        )


def test_live_run_applies_every_risk_check(
    cfg: AppConfig,
    connect: Connector,
    store: OutputStore,
    earnings: EarningsCalendar,
    nasdaq_client: NasdaqClient,
    world: FakeWorld,
    caplog: pytest.LogCaptureFixture,
    today,
) -> None:
    fresh = risk_world(today)
    world.__dict__.update(fresh.__dict__)  # keep the same object the fixtures hold
    seed_open_order_log(store)
    run_screen(cfg, connect, store)
    run_pick(cfg, connect, store, earnings)
    placed = run(cfg, connect, store, earnings, nasdaq_client, live=True)

    text = caplog.text
    assert "TSLA: skipped - already have a position or open order" in text
    assert "BIG cash_secured_put: REJECTED - uses $141,000 > 10% of account" in text
    assert (
        "BIG short_strangle: REJECTED - total would be $527,000 > 50% of account"
        in text
    )
    assert "WIDE cash_secured_put: REJECTED - P 38.0 spread 40% > 15%" in text
    assert (
        "HOOD cash_secured_put: REJECTED - earnings" in text
        and "before expiration" in text
    )
    assert "Reached 5 stocks - stopping." in text

    assert placed == len(world.placed) == 3
    for contract, order in world.placed:
        assert order.tif == "DAY"
        assert order.account.startswith("DU")
        assert order.orderRef == "algo_cash_secured_put"
        assert (contract.secType, order.action) == ("OPT", "SELL")
    assert {r["mode"] for r in read(store.order_log)} == {"live"}


def seed_open_order_log(store: OutputStore) -> None:
    """The fake's open TSLA order #77, as an earlier live run would have logged it."""
    store.append_order_log(
        OrderLogEntry(
            "2026-11-19 10:00:00",
            "live",
            77,
            "Submitted",
            "TSLA",
            "cash_secured_put",
            "20261229",
            300.0,
            None,
            1,
            9.85,
            30_000.0,
        )
    )


def test_small_cap_strangle_rejected(
    cfg: AppConfig,
    connect: Connector,
    store: OutputStore,
    earnings: EarningsCalendar,
    nasdaq_client: NasdaqClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    risk = cfg.risk.model_copy(
        update={"strategy_priority": (Strategy.SHORT_STRANGLE,), "max_positions": 20}
    )
    cfg = cfg.model_copy(update={"risk": risk})
    run_screen(cfg, connect, store)
    run_pick(cfg, connect, store, earnings)
    run(cfg, connect, store, earnings, nasdaq_client)
    assert (
        "SMALL short_strangle: REJECTED - not an ETF or large cap (market cap $2.0B)"
        in caplog.text
    )
    assert "SPY short_strangle: PASSED" in caplog.text  # ETFs skip the market-cap check


# ----------------------------------------------------------------------------- report / chain


def test_report_lists_positions_flags_and_orders(
    cfg: AppConfig, connect: Connector, store: OutputStore, earnings: EarningsCalendar
) -> None:
    paths = run_report(cfg, connect, store, earnings)
    text = paths[0].read_text()
    assert "account DU1234567" in text
    assert "POSITIONS (3)" in text
    assert "SELL CALL" in text
    assert "#77 SELL 1" in text
    assert (
        "Capital in use:     $102,000.00" in text
    )  # 50k margin + 52k META put collateral
    rows = read(paths[1])
    assert {r["type"] for r in rows} == {"stock", "short put", "long call"}


def test_chain_dump(cfg: AppConfig, connect: Connector, store: OutputStore) -> None:
    rows = read(run_chain(cfg, connect, store, "hood"))
    assert rows[0].keys() >= {"expiration", "strike", "type", "bid", "delta"}
    assert {r["type"] for r in rows} == {"Call", "Put"}
    with pytest.raises(MissingDataError, match="doesn't recognise"):
        run_chain(cfg, connect, store, "ZZZZ")


# ----------------------------------------------------------------------------- check


def test_check_warns_on_live_session(
    cfg: AppConfig, world: FakeWorld, caplog: pytest.LogCaptureFixture
) -> None:
    world.open_ports = (7496,)
    world.account = "U7654321"
    assert run_check(cfg, FakeIB) is False
    assert "looks like a LIVE account" in caplog.text
    world.open_ports, world.account = (7497,), "DU1234567"
    assert run_check(cfg, FakeIB) is True
    assert probe(cfg.ibkr, 1, FakeIB).label == "TWS - PAPER"
