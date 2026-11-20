"""Pure daily-report logic: position rows, suggested actions and the text summary."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

from ibkr_options_algo.calculations import (
    CONTRACT_MULTIPLIER,
    days_until,
    earnings_before_expiry,
)
from ibkr_options_algo.models import (
    Greeks,
    OpenOrder,
    PnLSnapshot,
    PortfolioHolding,
    PositionReportRow,
)

#: IBKR fills unset doubles with huge sentinels (about 1.8e308); treat those as missing.
IBKR_UNSET_THRESHOLD = 1e300
REPORT_WIDTH = 72


def clean(value: float | None) -> float | None:
    """Turn IBKR's 'no value' markers (None, NaN, huge sentinels) into None."""
    if value is None or (
        isinstance(value, float) and (math.isnan(value) or abs(value) > IBKR_UNSET_THRESHOLD)
    ):
        return None
    return value


def clean_pnl(pnl: PnLSnapshot) -> PnLSnapshot:
    return PnLSnapshot(clean(pnl.daily), clean(pnl.unrealized), clean(pnl.realized))


def _scaled(value: float | None, size: float, digits: int) -> float | None:
    cleaned = clean(value)
    return round(cleaned * size, digits) if cleaned is not None else None


def position_row(
    item: PortfolioHolding, greeks: Greeks | None, earnings: date | None, today: date
) -> PositionReportRow:
    """One report row; position Greeks are per-share Greeks x shares controlled."""
    is_option = item.is_option
    g = greeks
    size = item.quantity * (CONTRACT_MULTIPLIER if is_option else 1)
    expiration = item.expiration if is_option else ""
    dte = days_until(expiration, today) if expiration else None

    # For options we sold, max profit is the premium collected.
    credit = -item.avg_cost * item.quantity if is_option and item.quantity < 0 else None
    profit_pct = 100 * item.unrealized_pnl / credit if credit else None

    if is_option:
        side = "short " if item.quantity < 0 else "long "
        kind = side + ("put" if item.right == "P" else "call")
    else:
        kind = "stock"

    delta = clean(g.delta) if g else None
    und_price = clean(g.und_price) if g else None
    implied_vol = clean(g.implied_vol) if g else None
    stock_zero: float | None = None if is_option else 0
    return PositionReportRow(
        symbol=item.symbol,
        type=kind,
        right=item.right if is_option else "",
        strike=item.strike if is_option else None,
        expiration=expiration,
        dte=dte,
        quantity=item.quantity,
        avg_cost=round(item.avg_cost / (CONTRACT_MULTIPLIER if is_option else 1), 2),
        market_price=round(item.market_price, 2),
        market_value=round(item.market_value, 2),
        unrealized_pnl=round(item.unrealized_pnl, 2),
        profit_pct_of_max=round(profit_pct, 1) if profit_pct is not None else None,
        underlying_price=round(und_price, 2) if und_price else None,
        delta=(
            round(delta * size, 1)
            if delta is not None
            else (item.quantity if not is_option else None)
        ),
        gamma=_scaled(g.gamma, size, 2) if g else stock_zero,
        theta=_scaled(g.theta, size, 2) if g else stock_zero,
        vega=_scaled(g.vega, size, 2) if g else stock_zero,
        iv_pct=round(implied_vol * 100, 1) if implied_vol else None,
        next_earnings=earnings,
        earnings_before_expiry=bool(expiration) and earnings_before_expiry(earnings, expiration),
    )


def actions_for(
    row: PositionReportRow, has_short_call: bool, take_profit_pct: float, close_at_dte: int
) -> tuple[str, ...]:
    """Suggested actions for one position. Flags only: nothing is automated."""
    flags: list[str] = []
    if row.is_stock:
        if row.quantity >= CONTRACT_MULTIPLIER and not has_short_call:
            flags.append("SELL CALL: own 100+ shares, run `ibkr-algo pick` for a covered call")
        return tuple(flags)
    if (
        row.quantity < 0
        and row.profit_pct_of_max is not None
        and row.profit_pct_of_max >= take_profit_pct
    ):
        flags.append(
            f"TAKE PROFIT: made {row.profit_pct_of_max:.0f}% of max - consider buying back"
        )
    if row.dte is not None and row.dte <= close_at_dte:
        flags.append(f"{close_at_dte} DTE: {row.dte} days left - close or roll")
    und = row.underlying_price
    if und and row.strike is not None:
        itm = (row.right == "P" and und < row.strike) or (row.right == "C" and und > row.strike)
        if itm and row.quantity < 0:
            flags.append("ITM: likely assignment at expiration")
    if row.earnings_before_expiry:
        flags.append(f"EARNINGS on {row.next_earnings} before expiration")
    return tuple(flags)


def build_position_rows(
    items: Sequence[PortfolioHolding],
    greeks: Mapping[int, Greeks],
    earnings: Mapping[str, date | None],
    today: date,
    take_profit_pct: float,
    close_at_dte: int,
) -> list[PositionReportRow]:
    """Rows for every non-zero position, with action flags, sorted by symbol and expiry."""
    rows = [
        position_row(item, greeks.get(item.con_id), earnings.get(item.symbol), today)
        for item in items
    ]
    short_calls = {r.symbol for r in rows if r.type == "short call"}
    rows = [
        replace(r, actions=actions_for(r, r.symbol in short_calls, take_profit_pct, close_at_dte))
        for r in rows
    ]
    return sorted(rows, key=lambda r: (r.symbol, r.expiration))


@dataclass(frozen=True, slots=True)
class ReportInputs:
    generated_at: datetime
    account: str
    net_liq: float
    pnl: PnLSnapshot
    capital_used: float
    busy_symbols: frozenset[str]
    rows: Sequence[PositionReportRow]
    open_orders: Sequence[OpenOrder]
    max_total_pct: float
    max_positions: int


def _money(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "n/a"


def _cell(value: float | int | None) -> str:
    return str(value) if value is not None else ""


def _total(values: Sequence[float | None]) -> float:
    present = [v for v in values if v is not None]
    return sum(present) if present else 0


def render_report(data: ReportInputs) -> str:
    """The plain-text daily report."""
    rows = data.rows
    lines = [
        f"DAILY REPORT  {data.generated_at:%Y-%m-%d %H:%M}   account {data.account}",
        "=" * REPORT_WIDTH,
        f"Account value:      {_money(data.net_liq)}",
        f"Today's P&L:        {_money(data.pnl.daily)}",
        f"Unrealized P&L:     {_money(data.pnl.unrealized)}",
        f"Realized P&L:       {_money(data.pnl.realized)}",
        f"Capital in use:     {_money(data.capital_used)} = "
        f"{100 * data.capital_used / data.net_liq:.1f}% of account "
        f"(limit {data.max_total_pct:g}%)",
        f"Stocks in use:      {len(data.busy_symbols)} of {data.max_positions} "
        f"({', '.join(sorted(data.busy_symbols)) or 'none'})",
        "",
        f"POSITIONS ({len(rows)})",
    ]
    if rows:
        lines.append(
            f"{'symbol':<7}{'type':<12}{'strike':>8}{'exp':>10}{'dte':>5}{'qty':>6}"
            f"{'P&L':>10}{'%max':>7}{'delta':>8}{'theta':>8}{'vega':>8}"
        )
        for r in rows:
            lines.append(
                f"{r.symbol:<7}{r.type:<12}{_cell(r.strike):>8}{r.expiration:>10}"
                f"{_cell(r.dte):>5}{r.quantity:>6.0f}{r.unrealized_pnl:>10,.0f}"
                f"{_cell(r.profit_pct_of_max):>7}{_cell(r.delta):>8}"
                f"{_cell(r.theta):>8}{_cell(r.vega):>8}"
            )
            if r.actions:
                lines.append(f"       -> {' | '.join(r.actions)}")
        lines += [
            "",
            "PORTFOLIO GREEKS",
            f"Delta: {_total([r.delta for r in rows]):+,.1f} shares   "
            "(like owning that many shares across your stocks)",
            f"Theta: {_total([r.theta for r in rows]):+,.2f} $/day "
            "(time decay: what you earn per day if nothing moves)",
            f"Vega:  {_total([r.vega for r in rows]):+,.2f} $ "
            "per 1-point rise in implied volatility",
            f"Gamma: {_total([r.gamma for r in rows]):+,.2f}",
        ]
    else:
        lines.append("  No positions yet.")

    lines += ["", f"ORDERS WAITING TO FILL ({len(data.open_orders)})"]
    for o in data.open_orders:
        name = o.local_symbol.strip() or o.symbol
        lines.append(
            f"  #{o.order_id} {o.action} {o.quantity:.0f} {name} "
            f"@ {o.limit_price} ({o.status}, filled {o.filled:.0f})"
        )
    if not data.open_orders:
        lines.append("  none")
    return "\n".join(lines)
