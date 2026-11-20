"""Unit tests for strategy construction."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from ibkr_options_algo.models import Strategy, TradeIdea, UnderlyingContext
from ibkr_options_algo.strategy import (
    build_trade_idea,
    cash_secured_put_capital,
    cash_secured_put_leg,
    covered_call_contracts,
    covered_call_leg,
    ideas_by_priority,
    margin_order_spec,
    sort_trade_ideas,
    straddle_legs,
    strangle_legs,
    symbols_by_iv_rank,
    trade_order_spec,
)
from tests.conftest import quote

CTX = UnderlyingContext(
    symbol="HOOD",
    price=68.553,
    expiration="20261224",
    dte=37,
    is_etf=False,
    iv_rank=69.0,
    earnings=date(2026, 12, 17),
)

QUOTES = [
    quote("P", 55.0, -0.15, bid=0.95, ask=1.05),
    quote("P", 60.0, -0.25, bid=2.85, ask=2.95),
    quote("P", 69.0, -0.48, bid=4.90, ask=5.10),
    quote("C", 69.0, 0.52, bid=5.00, ask=5.20),
    quote("C", 80.0, 0.29, bid=1.90, ask=2.00),
    quote("C", 88.0, 0.17, bid=0.75, ask=0.85),
]


def test_leg_selection_by_delta() -> None:
    put = cash_secured_put_leg(QUOTES, 0.30, 0.08)
    call = covered_call_leg(QUOTES, 0.30, 0.08)
    assert put is not None
    assert call is not None
    assert (put.strike, call.strike) == (60.0, 80.0)


def test_strangle_legs() -> None:
    legs = strangle_legs(QUOTES, 0.16, 0.08)
    assert legs is not None
    assert (legs[0].strike, legs[1].strike) == (55.0, 88.0)


def test_strangle_rejected_when_put_strike_not_below_call() -> None:
    crossed = [quote("P", 70.0, -0.16), quote("C", 70.0, 0.16)]
    assert strangle_legs(crossed, 0.16, 0.08) is None


def test_strangle_none_when_a_side_has_no_strike_within_tolerance() -> None:
    only_near = [q for q in QUOTES if abs(q.delta) > 0.2]
    assert strangle_legs(only_near, 0.16, 0.08) is None


def test_straddle_legs_need_both_sides_at_atm() -> None:
    legs = straddle_legs(QUOTES, 69.0)
    assert legs is not None
    assert {legs[0].right, legs[1].right} == {"P", "C"}
    assert straddle_legs(QUOTES, 60.0) is None


def test_covered_call_contracts_and_csp_capital() -> None:
    assert covered_call_contracts(250.0) == 2
    assert covered_call_contracts(99.0) == 0
    assert cash_secured_put_capital(60.0) == 6000.0


def test_cash_secured_put_idea_numbers() -> None:
    put = QUOTES[1]
    idea = build_trade_idea(CTX, Strategy.CASH_SECURED_PUT, put=put, capital=6000.0)
    assert idea.credit == 290.0
    assert idea.return_pct == 4.83
    assert idea.annualized_pct == 47.7
    assert idea.breakeven_low == 57.1
    assert idea.breakeven_high is None
    assert idea.chance_all_otm_pct == 75.0
    assert idea.worst_spread_pct == round(put.spread_pct, 1)
    assert idea.stock_price == 68.55
    assert idea.earnings_before_expiry
    assert idea.put_conid == put.con_id
    assert idea.call_conid is None


def test_covered_call_breakeven_uses_cost_basis() -> None:
    call = QUOTES[4]
    idea = build_trade_idea(
        CTX,
        Strategy.COVERED_CALL,
        call=call,
        contracts=2,
        capital=2 * 100 * CTX.price,
        breakeven_cost=61.0,
    )
    assert idea.contracts == 2
    assert idea.credit == pytest.approx(390.0)
    assert idea.breakeven_low == pytest.approx(61.0 - 1.95)
    assert idea.breakeven_high is None


def test_chance_otm_floors_at_zero_and_zero_capital_gives_blank_returns() -> None:
    legs = straddle_legs(QUOTES, 69.0)
    assert legs is not None
    idea = build_trade_idea(
        CTX, Strategy.SHORT_STRADDLE, put=legs[0], call=legs[1], capital=0.0
    )
    assert idea.chance_all_otm_pct == 0
    assert idea.return_pct is None
    assert idea.annualized_pct is None


def _idea(
    symbol: str, strategy: Strategy, annualized: float | None, iv_rank: float | None
) -> TradeIdea:
    base = build_trade_idea(CTX, strategy, put=QUOTES[1], capital=6000.0)
    return replace(base, symbol=symbol, annualized_pct=annualized, iv_rank=iv_rank)


def test_sort_trade_ideas_groups_by_strategy_then_best_return() -> None:
    ideas = [
        _idea("A", Strategy.SHORT_STRANGLE, 50.0, 60),
        _idea("B", Strategy.CASH_SECURED_PUT, 20.0, 60),
        _idea("C", Strategy.CASH_SECURED_PUT, 40.0, 60),
        _idea("D", Strategy.CASH_SECURED_PUT, None, 60),
    ]
    assert [i.symbol for i in sort_trade_ideas(ideas)] == ["C", "B", "D", "A"]


def test_symbols_by_iv_rank_uses_first_row_and_alphabetical_ties() -> None:
    ideas = [
        _idea("ZZZ", Strategy.CASH_SECURED_PUT, 1, 70.0),
        _idea("AAA", Strategy.CASH_SECURED_PUT, 1, 70.0),
        _idea("MMM", Strategy.CASH_SECURED_PUT, 1, None),
        _idea("BBB", Strategy.CASH_SECURED_PUT, 1, 90.0),
    ]
    assert symbols_by_iv_rank(ideas) == ["BBB", "AAA", "ZZZ", "MMM"]


def test_ideas_by_priority_filters_and_orders() -> None:
    priority = (
        Strategy.COVERED_CALL,
        Strategy.CASH_SECURED_PUT,
        Strategy.SHORT_STRANGLE,
    )
    ideas = [
        _idea("X", Strategy.SHORT_STRADDLE, 1, 50),
        _idea("X", Strategy.SHORT_STRANGLE, 1, 50),
        _idea("X", Strategy.CASH_SECURED_PUT, 1, 50),
        _idea("Y", Strategy.CASH_SECURED_PUT, 1, 50),
    ]
    ordered = ideas_by_priority(ideas, "X", priority)
    assert [i.strategy for i in ordered] == [
        Strategy.CASH_SECURED_PUT,
        Strategy.SHORT_STRANGLE,
    ]


def test_margin_order_spec_is_one_lot_at_unrounded_mid() -> None:
    spec = margin_order_spec("HOOD", [QUOTES[0], QUOTES[5]])
    assert spec.is_combo
    assert spec.quantity == 1
    assert spec.credit == round(1.0 + 0.8, 2)
    assert spec.order_ref is None


def test_trade_order_spec_rounds_to_tick_and_tags_order() -> None:
    idea = build_trade_idea(
        CTX, Strategy.CASH_SECURED_PUT, put=QUOTES[2], capital=6900.0
    )
    spec, mid = trade_order_spec(
        idea, {"P": quote("P", 69.0, -0.48, bid=4.91, ask=5.12)}
    )
    assert mid == 5.0  # 5.015 rounds to the $0.05 tick
    assert spec.credit == mid
    assert not spec.is_combo
    assert spec.order_ref == "algo_cash_secured_put"


def test_trade_order_spec_combo_puts_put_leg_first() -> None:
    idea = build_trade_idea(
        CTX, Strategy.SHORT_STRANGLE, put=QUOTES[0], call=QUOTES[5], capital=1.0
    )
    call, put = quote("C", 88.0, 0.17, con_id=2), quote("P", 55.0, -0.15, con_id=1)
    spec, _ = trade_order_spec(idea, {"C": call, "P": put})
    assert spec.leg_con_ids == (1, 2)
