"""Risk-check today's trade ideas and place limit orders on the paper account.

Dry run by default: without ``live=True`` nothing is sent, the decisions are only logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ibkr_options_algo.calculations import CONTRACT_MULTIPLIER
from ibkr_options_algo.config import AppConfig
from ibkr_options_algo.earnings import EarningsCalendar
from ibkr_options_algo.errors import MissingDataError
from ibkr_options_algo.ibkr.client import IbkrClient
from ibkr_options_algo.ibkr.connection import Connector
from ibkr_options_algo.models import OrderLogEntry, Strategy, TradeDecision, TradeIdea
from ibkr_options_algo.risk import (
    account_value,
    at_position_limit,
    capital_reasons,
    earnings_reasons,
    is_regular_trading_hours,
    is_stale_proposals_file,
    large_cap_reasons,
    missing_margin_reasons,
    needs_large_cap_check,
    require_paper_account,
    required_capital,
    spread_reasons,
)
from ibkr_options_algo.services.account import load_account_state
from ibkr_options_algo.storage import OutputStore
from ibkr_options_algo.strategy import (
    ideas_by_priority,
    margin_order_spec,
    symbols_by_iv_rank,
    trade_order_spec,
)

log = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MarketCapLookup = Callable[[str], float | None]


def check_trade(
    ib: IbkrClient,
    cfg: AppConfig,
    idea: TradeIdea,
    earnings: date | None,
    net_liq: float,
    used: float,
    market_cap: MarketCapLookup,
    today: date,
) -> TradeDecision:
    """Run every risk check on one idea, re-pricing its legs right now."""
    rc = cfg.risk
    reasons = earnings_reasons(
        earnings, idea.expiration, today, rc.earnings_buffer_days, rc.block_earnings_before_expiry
    )
    if needs_large_cap_check(idea.strategy, idea.is_etf):
        reasons += large_cap_reasons(market_cap(idea.symbol), rc.large_cap_usd)
    if reasons:
        return TradeDecision(tuple(reasons))  # no need to price a trade we won't take

    legs_needed = ib.requalify_options(idea.leg_con_ids)
    quotes = {q.right: q for q in ib.option_quotes(legs_needed)}
    if len(quotes) < len(legs_needed):
        return TradeDecision(("no live bid/ask right now",), quotes=quotes)
    reasons += spread_reasons(quotes.values(), rc.max_spread_pct)

    margin = None
    if idea.strategy not in (Strategy.CASH_SECURED_PUT, Strategy.COVERED_CALL):
        legs = [q for q in (quotes.get("P"), quotes.get("C")) if q]
        margin = ib.what_if_margin(margin_order_spec(idea.symbol, legs))
    capital = required_capital(idea.strategy, idea.put_strike, idea.contracts, margin)
    reasons += missing_margin_reasons(idea.strategy, capital)
    reasons += capital_reasons(capital, used, net_liq, rc.max_trade_pct, rc.max_total_pct)
    return TradeDecision(tuple(reasons), capital, quotes)


def _execute(
    ib: IbkrClient,
    store: OutputStore,
    idea: TradeIdea,
    decision: TradeDecision,
    account: str,
    *,
    live: bool,
    now: Callable[[], datetime],
) -> None:
    """Place the order (live) or describe it (dry run), then record it in the order log."""
    name = f"{idea.symbol} {idea.strategy.value}"
    spec, mid = trade_order_spec(idea, decision.quotes)
    capital = decision.capital
    desc = (
        f"{idea.symbol} {idea.strategy.value} {idea.contracts}x {idea.expiration} "
        f"{idea.strikes_label} @ ${mid:.2f} credit "
        f"(${mid * CONTRACT_MULTIPLIER * idea.contracts:,.0f}), uses ${capital:,.0f}"
    )
    order_id: int | None
    if live:
        result = ib.place_order(spec, account)
        order_id, status = result.order_id, result.status
        log.info(f"{name}: PLACED order #{order_id} ({status}): {desc}")
        for message in result.messages:
            log.info(f"   IBKR message: {message}")
    else:
        log.info(f"{name}: PASSED - would place: {desc}")
        order_id, status = None, "dry_run"

    store.append_order_log(
        OrderLogEntry(
            time=now().strftime(LOG_TIME_FORMAT),
            mode="live" if live else "dry_run",
            order_id=order_id,
            status=status,
            symbol=idea.symbol,
            strategy=idea.strategy.value,
            expiration=idea.expiration,
            put_strike=idea.put_strike,
            call_strike=idea.call_strike,
            contracts=idea.contracts,
            limit_credit=mid,
            capital=round(capital, 2),
        )
    )


def run_orders(
    cfg: AppConfig,
    connect: Connector,
    store: OutputStore,
    earnings: EarningsCalendar,
    market_cap: MarketCapLookup,
    *,
    live: bool = False,
    now: Callable[[], datetime] = datetime.now,
) -> int:
    """Place (or, in a dry run, describe) at most one order per stock. Returns the count."""
    today = now().date()
    proposals_file, proposals = store.latest_proposals()
    if is_stale_proposals_file(proposals_file.name, today):
        raise MissingDataError(
            f"{proposals_file} is not from today. "
            "Run `ibkr-algo screen` and `ibkr-algo pick` again."
        )
    log.info(
        "\nLIVE MODE: orders WILL be sent"
        if live
        else "\nDRY RUN: nothing will be sent (add --live to send)"
    )
    log.info(f"Reading {proposals_file}: {len(proposals)} trade ideas")

    rc = cfg.risk
    placed = 0
    with connect(cfg.ibkr.client_ids.orders) as ib:
        ib.use_configured_market_data()
        account = ib.primary_account()
        require_paper_account(account)

        if not is_regular_trading_hours(datetime.now(NEW_YORK)):
            log.info("Note: the market is closed. Orders sent now wait until the next open.")

        net_liq = account_value(ib.account_summary(account))
        state = load_account_state(ib, account, store)
        used, busy = state.capital_used, state.busy

        log.info(
            f"Account {account}: value ${net_liq:,.0f}, capital in use ${used:,.0f} "
            f"({100 * used / net_liq:.1f}%), stocks with positions/orders: {sorted(busy) or 'none'}"
        )
        log.info(
            f"Limits: {rc.max_trade_pct:g}% per trade (${net_liq * rc.max_trade_pct / 100:,.0f}), "
            f"{rc.max_total_pct:g}% total (${net_liq * rc.max_total_pct / 100:,.0f}), "
            f"{rc.max_positions} stocks max\n"
        )

        symbols = sorted({i.symbol for i in proposals})
        lookup = earnings.next_earnings(symbols)
        if lookup.failed_days:
            log.warning(f"earnings calendar missing for {', '.join(lookup.failed_days)}")

        for symbol in symbols_by_iv_rank(proposals):
            if at_position_limit(busy, rc.max_positions):
                log.info(f"Reached {rc.max_positions} stocks - stopping.")
                break
            if symbol in busy:
                log.info(f"{symbol}: skipped - already have a position or open order")
                continue

            for idea in ideas_by_priority(proposals, symbol, rc.strategy_priority):
                name = f"{symbol} {idea.strategy.value}"
                decision = check_trade(
                    ib, cfg, idea, lookup.dates.get(symbol), net_liq, used, market_cap, today
                )
                if not decision.passed:
                    log.info(f"{name}: REJECTED - {'; '.join(decision.reasons)}")
                    continue

                _execute(ib, store, idea, decision, account, live=live, now=now)
                capital = decision.capital
                used += capital
                busy.add(symbol)
                placed += 1
                break  # one trade per stock

    verb = "Placed" if live else "Would place"
    log.info(
        f"\n{verb} {placed} order(s). Capital in use would be ${used:,.0f} "
        f"({100 * used / net_liq:.1f}% of account)."
    )
    log.info(f"Logged to {store.order_log}.")
    if live and placed:
        log.info(
            "Orders are limit orders at the mid price for today only. "
            "Unfilled ones expire at the close."
        )
    return placed
