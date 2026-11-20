"""Unit tests for every risk check."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from ibkr_options_algo.errors import LiveAccountError
from ibkr_options_algo.models import Holding, LoggedOrder, OpenOrder, Strategy
from ibkr_options_algo.risk import (
    account_value,
    at_position_limit,
    busy_symbols,
    capital_in_use,
    capital_reasons,
    earnings_reasons,
    is_paper_account,
    is_regular_trading_hours,
    is_stale_proposals_file,
    large_cap_reasons,
    missing_margin_reasons,
    needs_large_cap_check,
    require_paper_account,
    required_capital,
    spread_reasons,
    within_earnings_buffer,
)
from tests.conftest import quote

TODAY = date(2026, 11, 20)
EXPIRY = "20261229"


# ----------------------------------------------------------------------------- account type


def test_paper_account_accepted() -> None:
    assert is_paper_account("DU1234567")
    require_paper_account("DU1234567")


@pytest.mark.parametrize("account", ["U1234567", "DF123", "du123", ""])
def test_live_account_detected(account: str) -> None:
    assert not is_paper_account(account)
    with pytest.raises(LiveAccountError, match="not a paper account"):
        require_paper_account(account)


# ----------------------------------------------------------------------------- earnings


def test_earnings_inside_buffer_rejected() -> None:
    reasons = earnings_reasons(TODAY + timedelta(days=7), EXPIRY, TODAY, 7, True)
    assert reasons == ["earnings in 7 days"]


def test_earnings_today_rejected() -> None:
    assert earnings_reasons(TODAY, EXPIRY, TODAY, 7, True) == ["earnings in 0 days"]


def test_earnings_after_buffer_but_before_expiry_rejected() -> None:
    day = TODAY + timedelta(days=8)
    assert earnings_reasons(day, EXPIRY, TODAY, 7, True) == [
        f"earnings {day} before expiration {EXPIRY}"
    ]


def test_earnings_before_expiry_allowed_when_flag_off() -> None:
    assert earnings_reasons(TODAY + timedelta(days=8), EXPIRY, TODAY, 7, False) == []


def test_earnings_after_expiry_or_unknown_passes() -> None:
    assert earnings_reasons(date(2027, 1, 5), EXPIRY, TODAY, 7, True) == []
    assert earnings_reasons(None, EXPIRY, TODAY, 7, True) == []


def test_within_earnings_buffer() -> None:
    assert within_earnings_buffer(TODAY + timedelta(days=7), TODAY, 7)
    assert not within_earnings_buffer(TODAY + timedelta(days=8), TODAY, 7)
    assert not within_earnings_buffer(None, TODAY, 7)


# ----------------------------------------------------------------------------- large cap


@pytest.mark.parametrize(
    ("strategy", "is_etf", "expected"),
    [
        (Strategy.SHORT_STRANGLE, False, True),
        (Strategy.SHORT_STRADDLE, False, True),
        (Strategy.SHORT_STRANGLE, True, False),
        (Strategy.CASH_SECURED_PUT, False, False),
        (Strategy.COVERED_CALL, False, False),
    ],
)
def test_large_cap_check_only_for_undefined_risk_on_stocks(
    strategy: Strategy, is_etf: bool, expected: bool
) -> None:
    assert needs_large_cap_check(strategy, is_etf) is expected


def test_large_cap_reasons() -> None:
    assert large_cap_reasons(10e9, 10e9) == []
    assert large_cap_reasons(5.04e9, 10e9) == ["not an ETF or large cap (market cap $5.0B)"]
    assert large_cap_reasons(None, 10e9) == ["not an ETF or large cap (market cap unknown)"]
    assert large_cap_reasons(0.0, 10e9) == ["not an ETF or large cap (market cap unknown)"]


# ----------------------------------------------------------------------------- spreads


def test_spread_within_limit_passes() -> None:
    q = quote("P", 50.0, -0.3, bid=0.93, ask=1.07)  # 14%
    assert spread_reasons([q], 15) == []


def test_spread_too_wide_rejected_with_message() -> None:
    put = quote("P", 50.0, -0.3, bid=0.80, ask=1.20)  # 40%
    call = quote("C", 60.0, 0.3, bid=1.0, ask=1.05)
    assert spread_reasons([put, call], 15) == ["P 50.0 spread 40% > 15%"]


# ----------------------------------------------------------------------------- capital


def test_required_capital_by_strategy() -> None:
    assert required_capital(Strategy.CASH_SECURED_PUT, 60.0, 2, None) == 12_000.0
    assert required_capital(Strategy.COVERED_CALL, None, 3, None) == 0.0
    assert required_capital(Strategy.SHORT_STRANGLE, None, 1, 7_500.0) == 7_500.0
    assert required_capital(Strategy.SHORT_STRADDLE, None, 1, None) == 0


def test_cash_secured_put_without_strike_is_a_bug() -> None:
    with pytest.raises(ValueError, match="without a put strike"):
        required_capital(Strategy.CASH_SECURED_PUT, None, 1, None)


def test_missing_margin_only_matters_for_margin_strategies() -> None:
    assert missing_margin_reasons(Strategy.SHORT_STRANGLE, 0) == [
        "IBKR didn't return a margin number"
    ]
    assert missing_margin_reasons(Strategy.SHORT_STRANGLE, 5_000.0) == []
    assert missing_margin_reasons(Strategy.COVERED_CALL, 0.0) == []


def test_capital_limit_per_trade_hit() -> None:
    assert capital_reasons(100_001.0, 0.0, 1_000_000.0, 10, 50) == [
        "uses $100,001 > 10% of account"
    ]


def test_capital_limit_total_hit() -> None:
    assert capital_reasons(50_000.0, 460_000.0, 1_000_000.0, 10, 50) == [
        "total would be $510,000 > 50% of account"
    ]


def test_capital_both_limits_hit_and_exact_limits_pass() -> None:
    assert len(capital_reasons(150_000.0, 400_000.0, 1_000_000.0, 10, 50)) == 2
    assert capital_reasons(100_000.0, 400_000.0, 1_000_000.0, 10, 50) == []


def test_position_limit() -> None:
    assert not at_position_limit({"A", "B", "C", "D"}, 5)
    assert at_position_limit({"A", "B", "C", "D", "E"}, 5)


def _option(symbol: str, right: str, strike: float, qty: float) -> Holding:
    return Holding(
        symbol=symbol,
        sec_type="OPT",
        right=right,
        strike=strike,
        quantity=qty,
        avg_cost=1.0,
    )


def _order(order_id: int, symbol: str) -> OpenOrder:
    return OpenOrder(order_id, symbol, symbol, "SELL", 1, 2.0, "Submitted", 0)


def test_busy_symbols_combines_positions_and_orders() -> None:
    assert busy_symbols([_option("META", "P", 500, -1)], [_order(7, "TSLA")]) == {
        "META",
        "TSLA",
    }


def test_capital_in_use_adds_short_put_collateral_and_open_orders() -> None:
    positions = [
        _option("META", "P", 520.0, -1),  # +52,000 collateral
        _option("AMD", "P", 100.0, 2),  # long put: no collateral
        _option("NVDA", "C", 160.0, -1),  # short call: margin already in InitMarginReq
    ]
    log = [
        LoggedOrder("77", 30_000.0),
        LoggedOrder("", 999.0),
        LoggedOrder("12", 5_000.0),
    ]
    used = capital_in_use(10_000.0, positions, [_order(77, "TSLA")], log)
    assert used == pytest.approx(92_000.0)


def test_account_value() -> None:
    assert account_value({"NetLiquidation": "1005842.79"}) == pytest.approx(1_005_842.79)


# ----------------------------------------------------------------------------- timing


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 11, 20, 9, 29), False),
        (datetime(2026, 11, 20, 9, 30), True),
        (datetime(2026, 11, 20, 15, 59), True),
        (datetime(2026, 11, 20, 16, 0), False),
        (datetime(2026, 11, 21, 11, 0), False),  # Saturday
    ],
)
def test_regular_trading_hours(when: datetime, expected: bool) -> None:
    assert is_regular_trading_hours(when) is expected


def test_stale_proposals_file() -> None:
    assert not is_stale_proposals_file("proposed_trades_20261120_1404.csv", TODAY)
    assert is_stale_proposals_file("proposed_trades_20261119_1404.csv", TODAY)
