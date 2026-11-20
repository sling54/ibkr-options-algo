"""Pure risk checks. Each returns the reasons a trade fails (empty list = passes)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time

from ibkr_options_algo.calculations import CONTRACT_MULTIPLIER, EXPIRATION_FORMAT
from ibkr_options_algo.errors import LiveAccountError
from ibkr_options_algo.models import Holding, LoggedOrder, OpenOrder, OptionQuote, Strategy

#: IBKR paper accounts always start with these letters. A safeguard, so not configurable.
PAPER_ACCOUNT_PREFIX = "DU"

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
BILLION = 1e9


def is_paper_account(account: str) -> bool:
    return account.startswith(PAPER_ACCOUNT_PREFIX)


def require_paper_account(account: str) -> None:
    """Raise LiveAccountError unless ``account`` is an IBKR paper account."""
    if not is_paper_account(account):
        raise LiveAccountError(f"Account {account} is not a paper account. Stopping.")


def is_regular_trading_hours(now_new_york: datetime) -> bool:
    """Weekday between 09:30 and 16:00 New York time (holidays not considered)."""
    hhmm = now_new_york.strftime("%H:%M")
    return now_new_york.weekday() < 5 and MARKET_OPEN.strftime(
        "%H:%M"
    ) <= hhmm < MARKET_CLOSE.strftime("%H:%M")


def is_stale_proposals_file(filename: str, today: date) -> bool:
    """Prices go stale fast, so only trade from a proposals file stamped today."""
    return f"_{today:%Y%m%d}_" not in filename


def earnings_reasons(
    earnings: date | None,
    expiration: str,
    today: date,
    buffer_days: int,
    block_before_expiry: bool,
) -> list[str]:
    """No trades within the earnings buffer, nor (optionally) open over an earnings date."""
    if earnings is None:
        return []
    days = (earnings - today).days
    if days <= buffer_days:
        return [f"earnings in {days} days"]
    if block_before_expiry and earnings.strftime(EXPIRATION_FORMAT) <= expiration:
        return [f"earnings {earnings} before expiration {expiration}"]
    return []


def within_earnings_buffer(earnings: date | None, today: date, buffer_days: int) -> bool:
    """True when earnings are ``buffer_days`` or fewer away (the picker's skip rule)."""
    return earnings is not None and (earnings - today).days <= buffer_days


def needs_large_cap_check(strategy: Strategy, is_etf: bool) -> bool:
    """Undefined-risk strategies are only allowed on ETFs or large companies."""
    return strategy.has_undefined_risk and not is_etf


def large_cap_reasons(market_cap: float | None, threshold: float) -> list[str]:
    """Fails when the market cap is unknown or below ``threshold`` dollars."""
    if market_cap is None or market_cap < threshold:
        size = f"${market_cap / BILLION:.1f}B" if market_cap else "unknown"
        return [f"not an ETF or large cap (market cap {size})"]
    return []


def spread_reasons(quotes: Iterable[OptionQuote], max_spread_pct: float) -> list[str]:
    """Every leg's bid-ask spread must be at most ``max_spread_pct`` of its mid."""
    return [
        f"{q.right} {q.strike} spread {q.spread_pct:.0f}% > {max_spread_pct:g}%"
        for q in quotes
        if q.spread_pct > max_spread_pct
    ]


def required_capital(
    strategy: Strategy, put_strike: float | None, contracts: int, margin: float | None
) -> float:
    """Capital a trade ties up.

    Cash-secured puts reserve the assignment cost, covered calls are covered by the shares
    already owned, and everything else uses IBKR's what-if margin (0 if IBKR gave none).
    """
    if strategy is Strategy.CASH_SECURED_PUT:
        if put_strike is None:
            raise ValueError("cash-secured put without a put strike")
        return put_strike * CONTRACT_MULTIPLIER * contracts
    if strategy is Strategy.COVERED_CALL:
        return 0.0
    return margin or 0


def missing_margin_reasons(strategy: Strategy, capital: float) -> list[str]:
    if strategy in (Strategy.CASH_SECURED_PUT, Strategy.COVERED_CALL) or capital:
        return []
    return ["IBKR didn't return a margin number"]


def capital_reasons(
    capital: float, used: float, net_liq: float, max_trade_pct: float, max_total_pct: float
) -> list[str]:
    """Per-trade and whole-account capital limits, as percentages of net liquidation."""
    reasons = []
    if capital > net_liq * max_trade_pct / 100:
        reasons.append(f"uses ${capital:,.0f} > {max_trade_pct:g}% of account")
    if used + capital > net_liq * max_total_pct / 100:
        reasons.append(f"total would be ${used + capital:,.0f} > {max_total_pct:g}% of account")
    return reasons


def at_position_limit(busy_symbols: set[str], max_positions: int) -> bool:
    return len(busy_symbols) >= max_positions


def busy_symbols(option_positions: Iterable[Holding], open_orders: Iterable[OpenOrder]) -> set[str]:
    """Stocks with an option position or an open order: at most one trade per stock."""
    return {p.symbol for p in option_positions} | {o.symbol for o in open_orders}


def capital_in_use(
    init_margin: float,
    option_positions: Sequence[Holding],
    open_orders: Sequence[OpenOrder],
    order_log: Sequence[LoggedOrder],
) -> float:
    """Capital already committed.

    IBKR's initial margin, plus cash reserved for short puts we hold (cash-secured), plus the
    capital of our own orders still waiting to fill (looked up in the order log).
    """
    used = init_margin
    used += sum(
        p.strike * CONTRACT_MULTIPLIER * -p.quantity
        for p in option_positions
        if p.right == "P" and p.quantity < 0
    )
    open_ids = {str(o.order_id) for o in open_orders}
    used += sum(e.capital for e in order_log if e.order_id in open_ids)
    return used


def account_value(summary: Mapping[str, str], tag: str = "NetLiquidation") -> float:
    """A numeric value from IBKR's account summary."""
    return float(summary[tag])
