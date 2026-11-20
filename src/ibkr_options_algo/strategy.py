"""Pure strategy construction: choose legs, build trade ideas and order specs.

Margin for strangles and straddles comes from IBKR what-if orders (see the services layer),
not from a formula: IBKR's house margin on volatile stocks was 3-7x the textbook Reg-T figure,
which would make undefined-risk trades look far more attractive than they are.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from ibkr_options_algo.calculations import (
    CALENDAR_DAYS_PER_YEAR,
    CONTRACT_MULTIPLIER,
    closest_by_delta,
    earnings_before_expiry,
    find_quote,
    round_to_tick,
)
from ibkr_options_algo.models import (
    OptionQuote,
    OrderSpec,
    Right,
    Strategy,
    TradeIdea,
    UnderlyingContext,
)

ORDER_REF_PREFIX = "algo_"
LEG_ORDER: tuple[Right, ...] = ("P", "C")  # put first, then call, everywhere


def cash_secured_put_leg(
    quotes: Sequence[OptionQuote], target_delta: float, tolerance: float
) -> OptionQuote | None:
    """Wheel step 1: the put nearest the target delta."""
    return closest_by_delta(quotes, "P", target_delta, tolerance)


def covered_call_leg(
    quotes: Sequence[OptionQuote], target_delta: float, tolerance: float
) -> OptionQuote | None:
    """Wheel step 2: the call nearest the target delta."""
    return closest_by_delta(quotes, "C", target_delta, tolerance)


def strangle_legs(
    quotes: Sequence[OptionQuote], target_delta: float, tolerance: float
) -> tuple[OptionQuote, OptionQuote] | None:
    """Put and call nearest the target delta, only if the put strike is below the call's."""
    put = closest_by_delta(quotes, "P", target_delta, tolerance)
    call = closest_by_delta(quotes, "C", target_delta, tolerance)
    if put and call and put.strike < call.strike:
        return put, call
    return None


def straddle_legs(
    quotes: Sequence[OptionQuote], atm_strike: float
) -> tuple[OptionQuote, OptionQuote] | None:
    """Put and call at the at-the-money strike, if both were priced."""
    put = find_quote(quotes, "P", atm_strike)
    call = find_quote(quotes, "C", atm_strike)
    return (put, call) if put and call else None


def covered_call_contracts(shares_owned: float) -> int:
    """One call per 100 shares owned."""
    return int(shares_owned // CONTRACT_MULTIPLIER)


def cash_secured_put_capital(strike: float, contracts: int = 1) -> float:
    """Cash set aside to buy the shares if assigned."""
    return strike * CONTRACT_MULTIPLIER * contracts


def build_trade_idea(
    ctx: UnderlyingContext,
    strategy: Strategy,
    *,
    put: OptionQuote | None = None,
    call: OptionQuote | None = None,
    capital: float = 0.0,
    contracts: int = 1,
    breakeven_cost: float | None = None,
) -> TradeIdea:
    """Describe one trade: credit, return on capital, breakevens and rough odds.

    ``breakeven_cost`` is the share cost basis for a covered call, which is protected down to
    cost minus premium.
    """
    credit = CONTRACT_MULTIPLIER * ((put.mid if put else 0) + (call.mid if call else 0)) * contracts
    legs = [q for q in (put, call) if q]

    per_share = credit / (CONTRACT_MULTIPLIER * contracts)
    low = round(put.strike - per_share, 2) if put else None
    high = round(call.strike + per_share, 2) if call else None
    if breakeven_cost is not None:
        low, high = round(breakeven_cost - per_share, 2), None

    return TradeIdea(
        symbol=ctx.symbol,
        strategy=strategy,
        is_etf=ctx.is_etf,
        iv_rank=ctx.iv_rank,
        stock_price=round(ctx.price, 2),
        expiration=ctx.expiration,
        dte=ctx.dte,
        next_earnings=ctx.earnings,
        earnings_before_expiry=earnings_before_expiry(ctx.earnings, ctx.expiration),
        contracts=contracts,
        put_strike=put.strike if put else None,
        call_strike=call.strike if call else None,
        put_delta=round(put.delta, 3) if put else None,
        call_delta=round(call.delta, 3) if call else None,
        put_mid=round(put.mid, 2) if put else None,
        call_mid=round(call.mid, 2) if call else None,
        credit=round(credit, 2),
        capital=round(capital, 2),
        return_pct=round(100 * credit / capital, 2) if capital else None,
        annualized_pct=(
            round(100 * credit / capital * CALENDAR_DAYS_PER_YEAR / ctx.dte, 1) if capital else None
        ),
        breakeven_low=low,
        breakeven_high=high,
        # Rough chance every leg expires worthless, using |delta| as P(ITM).
        chance_all_otm_pct=round(max(0, 100 * (1 - sum(abs(q.delta) for q in legs))), 1),
        worst_spread_pct=round(max(q.spread_pct for q in legs), 1),
        put_conid=put.con_id if put else None,
        call_conid=call.con_id if call else None,
    )


def sort_trade_ideas(ideas: Iterable[TradeIdea]) -> list[TradeIdea]:
    """Group by strategy, best annualized return first within each."""
    return sorted(ideas, key=lambda i: (i.strategy.value, -(i.annualized_pct or 0)))


def symbols_by_iv_rank(ideas: Sequence[TradeIdea]) -> list[str]:
    """Distinct symbols, highest IV rank first (alphabetical among equals)."""

    def first_iv_rank(symbol: str) -> float:
        value = next(i.iv_rank for i in ideas if i.symbol == symbol)
        return value if value else 0

    return sorted(sorted({i.symbol for i in ideas}), key=first_iv_rank, reverse=True)


def ideas_by_priority(
    ideas: Iterable[TradeIdea], symbol: str, priority: Sequence[Strategy]
) -> list[TradeIdea]:
    """This symbol's ideas in strategy-priority order; strategies not listed are dropped."""
    mine = [i for i in ideas if i.symbol == symbol and i.strategy in priority]
    return sorted(mine, key=lambda i: priority.index(i.strategy))


def margin_order_spec(symbol: str, legs: Sequence[OptionQuote]) -> OrderSpec:
    """One-lot what-if order at the combined mid, used to ask IBKR for the margin."""
    return OrderSpec(
        symbol=symbol,
        leg_con_ids=tuple(q.con_id for q in legs),
        quantity=1,
        credit=round(sum(q.mid for q in legs), 2),
    )


def trade_order_spec(
    idea: TradeIdea, quotes: Mapping[Right, OptionQuote]
) -> tuple[OrderSpec, float]:
    """The limit order for a trade at the current mid, rounded to a valid tick."""
    legs = [quotes[r] for r in LEG_ORDER if r in quotes]
    mid = round_to_tick(sum(q.mid for q in legs))
    spec = OrderSpec(
        symbol=idea.symbol,
        leg_con_ids=tuple(q.con_id for q in legs),
        quantity=idea.contracts,
        credit=mid,
        order_ref=f"{ORDER_REF_PREFIX}{idea.strategy.value}",
    )
    return spec, mid
