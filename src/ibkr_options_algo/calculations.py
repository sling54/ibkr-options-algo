"""Pure calculations: volatility statistics, expirations, strikes, deltas and prices.

Nothing here talks to IBKR or the network, so every function can be unit tested directly.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from itertools import pairwise

from ibkr_options_algo.models import OptionQuote, Right, ScreenResult

TRADING_DAYS_PER_YEAR = 252
CALENDAR_DAYS_PER_YEAR = 365
CONTRACT_MULTIPLIER = 100
EXPIRATION_FORMAT = "%Y%m%d"

# US equity option tick sizes: $0.05 at or above $3.00, $0.01 below (penny-pilot names).
NICKEL_TICK_THRESHOLD = 3.0
NICKEL_TICK = 0.05


def is_positive_number(value: float | None) -> bool:
    """True for a real, positive number (rejects None, NaN, zero and IBKR's -1 'no data')."""
    return value is not None and not (isinstance(value, float) and math.isnan(value)) and value > 0


def clean_closes(values: Iterable[float | None]) -> list[float]:
    """Drop missing, NaN and non-positive closes from a historical-bar series."""
    return [v for v in values if v and not math.isnan(v) and v > 0]


# ----------------------------------------------------------------------------- volatility


def realized_vol(prices: Sequence[float], days: int) -> float:
    """Annualized close-to-close volatility of the last ``days`` log returns.

    Computed from closing prices rather than IBKR's HISTORICAL_VOLATILITY series, which
    jumped erratically in testing and didn't match actual price moves.
    """
    if days < 2 or len(prices) < days + 1:
        raise ValueError(f"need at least {days + 1} prices (and days >= 2), got {len(prices)}")
    recent = prices[-(days + 1) :]
    returns = [math.log(today / yesterday) for yesterday, today in pairwise(recent)]
    average = sum(returns) / len(returns)
    variance = sum((r - average) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR)


def iv_rank(current: float, low: float, high: float) -> float:
    """Where ``current`` sits in the [low, high] range, 0-100 (0 when the range is flat)."""
    return 100 * (current - low) / (high - low) if high > low else 0


def iv_percentile(history: Sequence[float], current: float) -> float:
    """Percentage of days in ``history`` with IV strictly below ``current``."""
    days_lower = sum(1 for iv in history if iv < current)
    return 100 * days_lower / len(history)


def has_enough_history(
    iv_history: Sequence[float], prices: Sequence[float], min_iv_days: int, hv_days: int
) -> bool:
    """IV rank needs a year-ish of IV; realized vol needs more than ``hv_days`` closes."""
    return len(iv_history) >= min_iv_days and len(prices) > hv_days


def is_sell_premium_candidate(
    rank: float, iv_minus_hv: float, min_iv_rank: float, min_gap: float
) -> bool:
    """Options look rich: IV high versus its own year AND above realized vol."""
    return rank >= min_iv_rank and iv_minus_hv > min_gap


def screen_result(
    activity_rank: int,
    symbol: str,
    iv_history: Sequence[float],
    prices: Sequence[float],
    hv_days: int,
    min_iv_rank: float,
    min_gap: float,
) -> ScreenResult:
    """Volatility statistics for one stock (values rounded as shown in the CSV).

    The candidate flag is evaluated on the rounded values, as the CSV reader sees them.
    """
    current_iv = iv_history[-1]
    low, high = min(iv_history), max(iv_history)
    current_hv = realized_vol(prices, hv_days)
    rank = round(iv_rank(current_iv, low, high), 1)
    gap = round((current_iv - current_hv) * 100, 1)  # vol points: IV 30% vs HV 22% -> +8
    return ScreenResult(
        activity_rank=activity_rank,
        symbol=symbol,
        price=round(prices[-1], 2),
        iv_now_pct=round(current_iv * 100, 1),
        iv_low_pct=round(low * 100, 1),
        iv_high_pct=round(high * 100, 1),
        iv_rank=rank,
        iv_percentile=round(iv_percentile(iv_history, current_iv), 1),
        hv_pct=round(current_hv * 100, 1),
        iv_minus_hv=gap,
        iv_hv_ratio=round(current_iv / current_hv, 2) if current_hv else None,
        days_of_data=len(iv_history),
        is_candidate=is_sell_premium_candidate(rank, gap, min_iv_rank, min_gap),
        hv_days=hv_days,
    )


def rank_screen_results(results: Iterable[ScreenResult]) -> list[ScreenResult]:
    """Highest IV rank first, ties broken by the larger IV-HV gap; assigns ``rank``."""
    ordered = sorted(results, key=lambda r: (r.iv_rank, r.iv_minus_hv), reverse=True)
    return [replace(r, rank=position) for position, r in enumerate(ordered, start=1)]


# ----------------------------------------------------------------------------- expirations


def days_until(expiration: str, today: date) -> int:
    """Calendar days from ``today`` to an IBKR expiration string (YYYYMMDD)."""
    return (datetime.strptime(expiration, EXPIRATION_FORMAT).date() - today).days


def pick_expiration(
    expirations: Sequence[str],
    today: date,
    min_dte: int,
    max_dte: int,
    target_dte: int,
    fallback_min_dte: int,
) -> str | None:
    """Expiration closest to ``target_dte``, preferring ones inside [min_dte, max_dte].

    Falls back to any expiration at least ``fallback_min_dte`` away. Ties go to the earlier
    expiration (``expirations`` is expected sorted).
    """
    inside = [e for e in expirations if min_dte <= days_until(e, today) <= max_dte]
    pool = inside or [e for e in expirations if days_until(e, today) >= fallback_min_dte]
    if not pool:
        return None
    return min(pool, key=lambda e: abs(days_until(e, today) - target_dte))


def expirations_in_window(
    expirations: Sequence[str], today: date, min_dte: int, max_dte: int, fallback_count: int
) -> list[str]:
    """Sorted expirations inside the window, or the nearest ``fallback_count`` if none are."""
    ordered = sorted(expirations)
    inside = [e for e in ordered if min_dte <= days_until(e, today) <= max_dte]
    return inside or ordered[:fallback_count]


# ----------------------------------------------------------------------------- strikes


@dataclass(frozen=True, slots=True)
class StrikeSelection:
    """Strikes to price: OTM puts below the money, the ATM strike, OTM calls above."""

    below: list[float]
    atm: float
    above: list[float]


def thin_out(strikes: Sequence[float], keep: int) -> list[float]:
    """Keep every Nth strike so that at most ``keep`` are priced."""
    if len(strikes) <= keep:
        return list(strikes)
    step = math.ceil(len(strikes) / keep)
    return list(strikes[::step])


def expected_move(price: float, implied_vol: float, dte: int) -> float:
    """One-standard-deviation move by expiration: price x IV x sqrt(years)."""
    return price * implied_vol * math.sqrt(dte / CALENDAR_DAYS_PER_YEAR)


def at_the_money(strikes: Sequence[float], price: float) -> float:
    """The strike nearest the stock price (first one on a tie)."""
    return min(strikes, key=lambda s: abs(s - price))


def select_strikes(
    strikes: Sequence[float], price: float, move: float, moves_each_side: float, max_per_side: int
) -> StrikeSelection:
    """ATM strike plus up to ``max_per_side`` strikes within N expected moves each side."""
    atm = at_the_money(strikes, price)
    reach = moves_each_side * move
    below = thin_out([s for s in strikes if price - reach <= s < atm], max_per_side)
    above = thin_out([s for s in strikes if atm < s <= price + reach], max_per_side)
    return StrikeSelection(below=below, atm=atm, above=above)


def strikes_near_price(strikes: Iterable[float], price: float, range_pct: float) -> list[float]:
    """Sorted strikes within +/- ``range_pct`` percent of ``price``."""
    fraction = range_pct / 100
    return [s for s in sorted(strikes) if price * (1 - fraction) <= s <= price * (1 + fraction)]


# ----------------------------------------------------------------------------- quotes


def make_quote(
    con_id: int,
    right: Right,
    strike: float,
    bid: float | None,
    ask: float | None,
    delta: float | None,
) -> OptionQuote | None:
    """A usable quote needs a real two-sided market and a model delta."""
    if bid is None or ask is None or delta is None:
        return None
    if not (is_positive_number(bid) and is_positive_number(ask)):
        return None
    return OptionQuote(con_id=con_id, right=right, strike=strike, bid=bid, ask=ask, delta=delta)


def closest_by_delta(
    quotes: Iterable[OptionQuote], right: Right, target_abs_delta: float, tolerance: float
) -> OptionQuote | None:
    """The put or call whose |delta| is nearest the target.

    Returns None when even the nearest one is more than ``tolerance`` away, e.g. when no
    strikes are listed far enough out of the money.
    """
    options = [q for q in quotes if q.right == right]
    if not options:
        return None
    best = min(options, key=lambda q: abs(abs(q.delta) - target_abs_delta))
    return best if abs(abs(best.delta) - target_abs_delta) <= tolerance else None


def find_quote(quotes: Iterable[OptionQuote], right: Right, strike: float) -> OptionQuote | None:
    """The quote for an exact right and strike, if it was priced."""
    return next((q for q in quotes if q.right == right and q.strike == strike), None)


def round_to_tick(price: float) -> float:
    """Round a limit price to a valid option tick ($0.05 at or above $3, else $0.01)."""
    if price >= NICKEL_TICK_THRESHOLD:
        return round(round(price / NICKEL_TICK) * NICKEL_TICK, 2)
    return round(price, 2)


def choose_stock_price(market_price: float, close: float) -> float | None:
    """Live/delayed market price, else yesterday's close, else None."""
    if is_positive_number(market_price):
        return market_price
    if is_positive_number(close):
        return close
    return None


def earnings_before_expiry(earnings: date | None, expiration: str) -> bool:
    """True when earnings land on or before an expiration (YYYYMMDD)."""
    return earnings is not None and earnings.strftime(EXPIRATION_FORMAT) <= expiration
