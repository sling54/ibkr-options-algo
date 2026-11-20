"""Unit tests for the pure calculation functions."""

from __future__ import annotations

import itertools
import math
import statistics
from datetime import date, timedelta

import pytest

from ibkr_options_algo import calculations as calc
from ibkr_options_algo.calculations import (
    StrikeSelection,
    at_the_money,
    choose_stock_price,
    clean_closes,
    closest_by_delta,
    days_until,
    earnings_before_expiry,
    expected_move,
    expirations_in_window,
    find_quote,
    has_enough_history,
    is_positive_number,
    is_sell_premium_candidate,
    iv_percentile,
    iv_rank,
    make_quote,
    pick_expiration,
    rank_screen_results,
    realized_vol,
    round_to_tick,
    screen_result,
    select_strikes,
    strikes_near_price,
    thin_out,
)
from tests.conftest import quote

TODAY = date(2026, 11, 20)


def exp(days: int) -> str:
    return (TODAY + timedelta(days=days)).strftime("%Y%m%d")


# ----------------------------------------------------------------------------- basics


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.5, True),
        (0, False),
        (0.0, False),
        (-1, False),
        (None, False),
        (math.nan, False),
    ],
)
def test_is_positive_number(value: float | None, expected: bool) -> None:
    assert is_positive_number(value) is expected


def test_clean_closes_drops_missing_nan_and_non_positive() -> None:
    assert clean_closes([1.0, None, math.nan, 0.0, -2.0, 3.0]) == [1.0, 3.0]


# ----------------------------------------------------------------------------- volatility


def test_realized_vol_matches_sample_stdev_of_log_returns() -> None:
    prices = [100.0, 101.0, 99.5, 102.0, 101.0, 103.5]
    returns = [math.log(b / a) for a, b in itertools.pairwise(prices)]
    expected = statistics.stdev(returns) * math.sqrt(252)
    assert realized_vol(prices, 5) == pytest.approx(expected, rel=1e-12)


def test_realized_vol_uses_only_the_last_window() -> None:
    noisy_start = [50.0, 200.0, 50.0]
    steady = [100.0 * 1.01**i for i in range(21)]
    assert realized_vol(noisy_start + steady, 20) == pytest.approx(0.0, abs=1e-9)


def test_realized_vol_needs_enough_prices() -> None:
    with pytest.raises(ValueError, match="need at least 21 prices"):
        realized_vol([100.0] * 20, 20)


def test_iv_rank_and_flat_range() -> None:
    assert iv_rank(0.30, 0.20, 0.40) == pytest.approx(50.0)
    assert iv_rank(0.40, 0.20, 0.40) == pytest.approx(100.0)
    assert iv_rank(0.30, 0.30, 0.30) == 0


def test_iv_percentile_counts_strictly_lower_days() -> None:
    assert iv_percentile([0.1, 0.2, 0.3, 0.3], 0.3) == pytest.approx(50.0)


@pytest.mark.parametrize(
    ("rank", "gap", "expected"),
    [(50.0, 0.1, True), (49.9, 5.0, False), (80.0, 0.0, False), (80.0, -1.0, False)],
)
def test_candidate_needs_rank_at_least_min_and_gap_above_min(
    rank: float, gap: float, expected: bool
) -> None:
    assert is_sell_premium_candidate(rank, gap, min_iv_rank=50, min_gap=0.0) is expected


def test_has_enough_history() -> None:
    assert has_enough_history([0.2] * 60, [1.0] * 21, min_iv_days=60, hv_days=20)
    assert not has_enough_history([0.2] * 59, [1.0] * 21, min_iv_days=60, hv_days=20)
    assert not has_enough_history([0.2] * 60, [1.0] * 20, min_iv_days=60, hv_days=20)


def test_screen_result_rounds_and_flags_on_rounded_values() -> None:
    # IV rank of 49.96 rounds to 50.0, which counts as a candidate (as the old CSV reader saw it).
    history = [0.20] * 10 + [0.30] + [0.2 + 0.1 * 0.4996]
    prices = [100.0 + 0.01 * (i % 2) for i in range(30)]  # almost no realized vol
    r = screen_result(7, "XYZ", history, prices, hv_days=20, min_iv_rank=50, min_gap=0.0)
    assert r.iv_rank == 50.0
    assert r.is_candidate
    assert r.activity_rank == 7
    assert r.iv_now_pct == round(history[-1] * 100, 1)
    assert r.price == round(prices[-1], 2)
    assert r.days_of_data == len(history)


def test_screen_result_ratio_blank_when_hv_is_zero() -> None:
    prices = [100.0] * 25
    r = screen_result(1, "FLAT", [0.2, 0.3], prices, hv_days=20, min_iv_rank=50, min_gap=0.0)
    assert r.hv_pct == 0.0
    assert r.iv_hv_ratio is None


def test_rank_screen_results_orders_by_rank_then_gap_and_numbers_them() -> None:
    base = screen_result(1, "A", [0.2, 0.4], [100.0 + i for i in range(25)], 20, 50, 0.0)
    from dataclasses import replace

    a = replace(base, symbol="A", iv_rank=80.0, iv_minus_hv=5.0)
    b = replace(base, symbol="B", iv_rank=90.0, iv_minus_hv=1.0)
    c = replace(base, symbol="C", iv_rank=80.0, iv_minus_hv=9.0)
    d = replace(base, symbol="D", iv_rank=80.0, iv_minus_hv=5.0)  # exact tie with A keeps order
    ranked = rank_screen_results([a, b, c, d])
    assert [(r.symbol, r.rank) for r in ranked] == [
        ("B", 1),
        ("C", 2),
        ("A", 3),
        ("D", 4),
    ]


# ----------------------------------------------------------------------------- expirations


def test_days_until() -> None:
    assert days_until(exp(38), TODAY) == 38


def test_pick_expiration_prefers_window_and_target() -> None:
    expirations = [exp(d) for d in (4, 11, 32, 39, 46)]
    assert pick_expiration(expirations, TODAY, 30, 45, 38, 7) == exp(39)


def test_pick_expiration_tie_goes_to_earlier() -> None:
    expirations = [exp(36), exp(40)]
    assert pick_expiration(expirations, TODAY, 30, 45, 38, 7) == exp(36)


def test_pick_expiration_falls_back_outside_window() -> None:
    expirations = [exp(4), exp(12), exp(60)]
    assert pick_expiration(expirations, TODAY, 30, 45, 38, 7) == exp(60)  # |60-38| < |12-38|


def test_pick_expiration_none_when_everything_too_close() -> None:
    assert pick_expiration([exp(2), exp(6)], TODAY, 30, 45, 38, 7) is None


def test_expirations_in_window_and_fallback() -> None:
    exps = [exp(50), exp(4), exp(25)]
    assert expirations_in_window(exps, TODAY, 20, 50, 2) == [exp(25), exp(50)]
    assert expirations_in_window(exps, TODAY, 60, 90, 2) == [exp(4), exp(25)]


# ----------------------------------------------------------------------------- strikes


def test_thin_out_keeps_every_nth() -> None:
    assert thin_out([1.0, 2.0, 3.0], 5) == [1.0, 2.0, 3.0]
    assert thin_out([float(i) for i in range(10)], 4) == [0.0, 3.0, 6.0, 9.0]


def test_expected_move() -> None:
    assert expected_move(100.0, 0.365, 365) == pytest.approx(36.5)


def test_at_the_money_first_on_tie() -> None:
    assert at_the_money([95.0, 100.0, 105.0], 102.5) == 100.0


def test_select_strikes_splits_around_atm_within_reach() -> None:
    strikes = [float(s) for s in range(80, 121, 5)]
    sel = select_strikes(strikes, price=101.0, move=5.0, moves_each_side=2.0, max_per_side=20)
    assert sel == StrikeSelection(below=[95.0], atm=100.0, above=[105.0, 110.0])


def test_strikes_near_price() -> None:
    assert strikes_near_price([120.0, 90.0, 100.0, 111.0], 100.0, 10) == [90.0, 100.0]


# ----------------------------------------------------------------------------- quotes


def test_make_quote_requires_two_sided_market_and_delta() -> None:
    assert make_quote(1, "P", 50.0, 1.0, 1.2, -0.3) is not None
    assert make_quote(1, "P", 50.0, 0.0, 1.2, -0.3) is None
    assert make_quote(1, "P", 50.0, math.nan, 1.2, -0.3) is None
    assert make_quote(1, "P", 50.0, 1.0, -1.0, -0.3) is None
    assert make_quote(1, "P", 50.0, 1.0, 1.2, None) is None


def test_option_quote_mid_and_spread() -> None:
    q = quote("C", 100.0, 0.3, bid=1.9, ask=2.1)
    assert q.mid == pytest.approx(2.0)
    assert q.spread_pct == pytest.approx(10.0)


def test_closest_by_delta_picks_nearest_absolute_delta() -> None:
    quotes = [
        quote("P", 90.0, -0.20),
        quote("P", 95.0, -0.28),
        quote("P", 100.0, -0.45),
    ]
    best = closest_by_delta(quotes, "P", 0.30, 0.08)
    assert best is not None
    assert best.strike == 95.0


def test_closest_by_delta_none_when_no_strike_within_tolerance() -> None:
    quotes = [quote("P", 90.0, -0.05), quote("P", 100.0, -0.50)]
    assert closest_by_delta(quotes, "P", 0.30, 0.08) is None


def test_closest_by_delta_tolerance_is_inclusive() -> None:
    quotes = [quote("C", 110.0, 0.375)]
    assert closest_by_delta(quotes, "C", 0.30, 0.08) is not None


def test_closest_by_delta_ignores_other_right() -> None:
    assert closest_by_delta([quote("C", 110.0, 0.30)], "P", 0.30, 0.08) is None


def test_find_quote() -> None:
    quotes = [quote("P", 100.0, -0.5), quote("C", 100.0, 0.5)]
    found = find_quote(quotes, "C", 100.0)
    assert found is not None
    assert found.right == "C"
    assert find_quote(quotes, "C", 105.0) is None


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (3.07, 3.05),
        (3.08, 3.10),
        (17.18, 17.20),
        (2.996, 3.0),
        (2.913, 2.91),
        (0.054, 0.05),
    ],
)
def test_round_to_tick(price: float, expected: float) -> None:
    assert round_to_tick(price) == pytest.approx(expected)


def test_choose_stock_price_falls_back_to_close() -> None:
    assert choose_stock_price(10.0, 9.0) == 10.0
    assert choose_stock_price(math.nan, 9.0) == 9.0
    assert choose_stock_price(math.nan, math.nan) is None


def test_earnings_before_expiry_is_inclusive() -> None:
    assert earnings_before_expiry(TODAY + timedelta(days=38), exp(38))
    assert not earnings_before_expiry(TODAY + timedelta(days=39), exp(38))
    assert not earnings_before_expiry(None, exp(38))


def test_constants_are_named() -> None:
    assert calc.TRADING_DAYS_PER_YEAR == 252
    assert calc.CONTRACT_MULTIPLIER == 100
