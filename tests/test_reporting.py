"""Unit tests for daily-report rows, flags and text."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from ibkr_options_algo.models import Greeks, OpenOrder, PnLSnapshot, PortfolioHolding
from ibkr_options_algo.reporting import (
    ReportInputs,
    actions_for,
    build_position_rows,
    clean,
    clean_pnl,
    position_row,
    render_report,
)

TODAY = date(2026, 11, 20)


def holding(**overrides: object) -> PortfolioHolding:
    fields: dict[str, object] = {
        "con_id": 1,
        "symbol": "HOOD",
        "sec_type": "OPT",
        "right": "P",
        "strike": 60.0,
        "expiration": (TODAY + timedelta(days=30)).strftime("%Y%m%d"),
        "quantity": -1.0,
        "avg_cost": 290.0,
        "market_price": 1.2,
        "market_value": -120.0,
        "unrealized_pnl": 170.0,
    }
    fields.update(overrides)
    return PortfolioHolding(**fields)  # type: ignore[arg-type]


GREEKS = Greeks(
    delta=-0.2, gamma=0.03, theta=-0.04, vega=0.07, implied_vol=0.61, und_price=66.0
)


@pytest.mark.parametrize("value", [None, math.nan, 1.7976931348623157e308])
def test_clean_treats_ibkr_markers_as_missing(value: float | None) -> None:
    assert clean(value) is None


def test_clean_keeps_real_numbers_and_pnl() -> None:
    assert clean(0.0) == 0.0
    assert clean_pnl(PnLSnapshot(5.0, math.nan, None)) == PnLSnapshot(5.0, None, None)


def test_short_put_row_scales_greeks_and_profit() -> None:
    row = position_row(holding(), GREEKS, None, TODAY)
    assert row.type == "short put"
    assert row.dte == 30
    assert row.avg_cost == 2.9
    assert row.profit_pct_of_max == pytest.approx(58.6)
    assert row.delta == 20.0  # -0.2 x -100 shares
    assert row.theta == 4.0
    assert row.vega == -7.0
    assert row.iv_pct == 61.0
    assert row.underlying_price == 66.0


def test_option_without_greeks_has_blank_greeks() -> None:
    row = position_row(holding(), None, None, TODAY)
    assert (row.delta, row.gamma, row.theta, row.vega) == (None, None, None, None)


def test_stock_row_counts_shares_as_delta() -> None:
    row = position_row(
        holding(sec_type="STK", quantity=250.0, avg_cost=201.5, right=""),
        None,
        None,
        TODAY,
    )
    assert row.type == "stock"
    assert row.delta == 250.0
    assert (row.gamma, row.theta, row.vega) == (0, 0, 0)
    assert row.dte is None
    assert row.strike is None


def test_actions_take_profit_dte_itm_and_earnings() -> None:
    earnings = TODAY + timedelta(days=10)
    row = position_row(
        holding(expiration=(TODAY + timedelta(days=20)).strftime("%Y%m%d")),
        GREEKS,
        earnings,
        TODAY,
    )
    flags = actions_for(row, has_short_call=False, take_profit_pct=50, close_at_dte=21)
    assert flags == (
        "TAKE PROFIT: made 59% of max - consider buying back",
        "21 DTE: 20 days left - close or roll",
        "EARNINGS on " + str(earnings) + " before expiration",
    )
    itm = position_row(holding(), Greeks(-0.6, 0.0, 0.0, 0.0, 0.6, 55.0), None, TODAY)
    assert "ITM: likely assignment at expiration" in actions_for(itm, False, 50, 21)


def test_sell_call_flag_only_without_existing_short_call() -> None:
    stock = position_row(
        holding(sec_type="STK", quantity=200.0, right=""), None, None, TODAY
    )
    assert actions_for(
        stock, has_short_call=False, take_profit_pct=50, close_at_dte=21
    )[0].startswith("SELL CALL")
    assert (
        actions_for(stock, has_short_call=True, take_profit_pct=50, close_at_dte=21)
        == ()
    )


def test_build_position_rows_links_short_calls_and_sorts() -> None:
    stock = holding(
        con_id=2, symbol="AAPL", sec_type="STK", quantity=200.0, right="", expiration=""
    )
    call = holding(con_id=3, symbol="AAPL", right="C", strike=240.0)
    rows = build_position_rows([call, stock, holding()], {}, {}, TODAY, 50, 21)
    assert [r.symbol for r in rows] == ["AAPL", "AAPL", "HOOD"]
    assert rows[0].type == "stock"
    assert rows[0].actions == ()


def _inputs(rows: list, orders: list) -> ReportInputs:  # type: ignore[type-arg]
    return ReportInputs(
        generated_at=datetime(2026, 11, 20, 14, 29),
        account="DU1",
        net_liq=1_000_000.0,
        pnl=PnLSnapshot(None, None, None),
        capital_used=64_000.0,
        busy_symbols=frozenset({"HOOD"}),
        rows=rows,
        open_orders=orders,
        max_total_pct=50,
        max_positions=5,
    )


def test_render_report_without_positions() -> None:
    order = OpenOrder(
        32, "HOOD", " HOOD  261224P00060000", "SELL", 1.0, 2.9, "Submitted", 0.0
    )
    text = render_report(_inputs([], [order]))
    assert "Capital in use:     $64,000.00 = 6.4% of account (limit 50%)" in text
    assert "Stocks in use:      1 of 5 (HOOD)" in text
    assert "  No positions yet." in text
    assert "  #32 SELL 1 HOOD  261224P00060000 @ 2.9 (Submitted, filled 0)" in text
    assert "Today's P&L:        n/a" in text


def test_render_report_with_positions_totals_greeks() -> None:
    rows = build_position_rows([holding()], {1: GREEKS}, {}, TODAY, 50, 21)
    text = render_report(_inputs(rows, []))
    assert "Delta: +20.0 shares" in text
    assert "Theta: +4.00 $/day" in text
    assert "       -> TAKE PROFIT" in text
    assert text.endswith("  none")
