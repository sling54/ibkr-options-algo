"""Trade picker: turn screener candidates into concrete premium-selling trade ideas.

For each candidate: an expiration 30-45 days out, then a cash-secured put (~0.30 delta), a
short strangle (~0.16 delta each side), a short straddle (ATM) and, for stocks with 100+
shares held, a covered call (~0.30 delta). Nothing is ordered here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ibkr_options_algo.calculations import (
    CONTRACT_MULTIPLIER,
    choose_stock_price,
    days_until,
    expected_move,
    is_positive_number,
    pick_expiration,
    select_strikes,
)
from ibkr_options_algo.config import AppConfig
from ibkr_options_algo.earnings import EarningsCalendar
from ibkr_options_algo.errors import NoResultsError
from ibkr_options_algo.ibkr.client import SMART, IbkrClient
from ibkr_options_algo.ibkr.connection import Connector
from ibkr_options_algo.models import Holding, Right, Strategy, TradeIdea, UnderlyingContext
from ibkr_options_algo.risk import within_earnings_buffer
from ibkr_options_algo.storage import OutputStore
from ibkr_options_algo.strategy import (
    build_trade_idea,
    cash_secured_put_capital,
    cash_secured_put_leg,
    covered_call_contracts,
    covered_call_leg,
    margin_order_spec,
    sort_trade_ideas,
    straddle_legs,
    strangle_legs,
)

log = logging.getLogger(__name__)

MIN_SHARES_FOR_COVERED_CALL = 100


@dataclass(frozen=True, slots=True)
class SymbolRequest:
    symbol: str
    iv_rank: float | None
    shares_owned: float = 0
    avg_cost: float | None = None
    sell_premium: bool = True
    earnings: date | None = None


def analyze_symbol(
    ib: IbkrClient, cfg: AppConfig, req: SymbolRequest, today: date
) -> list[TradeIdea]:
    """Price one stock's option chain and build its trade ideas."""
    pc = cfg.pick
    symbol = req.symbol
    info = ib.stock_details(symbol)
    if info is None:
        log.info(f"  {symbol}: not found, skipped")
        return []

    snapshot = ib.stock_snapshot(info.con_id)
    price = choose_stock_price(snapshot.market_price, snapshot.close)
    if price is None:
        log.info(f"  {symbol}: no stock price, skipped")
        return []

    smart = [c for c in ib.option_chains(info.con_id) if c.exchange == SMART]
    chain = next((c for c in smart if c.trading_class == symbol), smart[0] if smart else None)
    expiration = (
        pick_expiration(
            sorted(chain.expirations),
            today,
            pc.min_dte,
            pc.max_dte,
            pc.target_dte,
            pc.fallback_min_dte,
        )
        if chain
        else None
    )
    if chain is None or expiration is None:
        log.info(f"  {symbol}: no usable expiration, skipped")
        return []
    dte = days_until(expiration, today)

    raw_iv = snapshot.implied_vol
    iv = raw_iv if raw_iv is not None and is_positive_number(raw_iv) else pc.default_iv
    move = expected_move(price, iv, dte)
    listed = ib.listed_strikes(symbol, expiration, chain.trading_class)
    all_strikes = sorted(set(listed)) or sorted(chain.strikes)
    strikes = select_strikes(
        all_strikes, price, move, pc.strike_range_expected_moves, pc.max_strikes_per_side
    )

    # OTM puts below the price, OTM calls above, and both at the ATM strike.
    legs: list[tuple[float, Right]] = [(s, "P") for s in [*strikes.below, strikes.atm]]
    legs += [(s, "C") for s in [strikes.atm, *strikes.above]]
    con_ids = ib.qualify_options(symbol, expiration, chain.trading_class, legs)
    quotes = ib.option_quotes(con_ids)
    log.info(
        f"  {symbol:<6} ${price:<8.2f} exp {expiration} ({dte}d)  "
        f"{len(quotes)}/{len(con_ids)} options priced"
    )
    if not quotes:
        return []

    ctx = UnderlyingContext(
        symbol=symbol,
        price=price,
        expiration=expiration,
        dte=dte,
        is_etf=info.is_etf,
        iv_rank=req.iv_rank,
        earnings=req.earnings,
    )
    ideas: list[TradeIdea] = []

    if req.sell_premium:
        put = cash_secured_put_leg(quotes, pc.put_delta, pc.delta_tolerance)
        if put:
            ideas.append(
                build_trade_idea(
                    ctx,
                    Strategy.CASH_SECURED_PUT,
                    put=put,
                    capital=cash_secured_put_capital(put.strike),
                )
            )

        strangle = strangle_legs(quotes, pc.strangle_delta, pc.delta_tolerance)
        margin = ib.what_if_margin(margin_order_spec(symbol, strangle)) if strangle else None
        if strangle and margin:
            ideas.append(
                build_trade_idea(
                    ctx, Strategy.SHORT_STRANGLE, put=strangle[0], call=strangle[1], capital=margin
                )
            )
        else:
            log.info(
                f"         {symbol}: no strangle (no strikes near {pc.strangle_delta} delta, "
                "or no margin answer)"
            )

        straddle = straddle_legs(quotes, strikes.atm)
        margin = ib.what_if_margin(margin_order_spec(symbol, straddle)) if straddle else None
        if straddle and margin:
            ideas.append(
                build_trade_idea(
                    ctx, Strategy.SHORT_STRADDLE, put=straddle[0], call=straddle[1], capital=margin
                )
            )

    if req.shares_owned >= MIN_SHARES_FOR_COVERED_CALL:
        call = covered_call_leg(quotes, pc.call_delta, pc.delta_tolerance)
        if call:
            n = covered_call_contracts(req.shares_owned)
            ideas.append(
                build_trade_idea(
                    ctx,
                    Strategy.COVERED_CALL,
                    call=call,
                    contracts=n,
                    capital=n * CONTRACT_MULTIPLIER * price,
                    breakeven_cost=req.avg_cost,
                )
            )
    return ideas


def run_pick(
    cfg: AppConfig,
    connect: Connector,
    store: OutputStore,
    earnings: EarningsCalendar,
    today: date | None = None,
) -> Path:
    """Build trade ideas for today's candidates and save proposed_trades_*.csv."""
    today = today or date.today()
    screen_file, candidates = store.latest_candidates()
    log.info(
        f"\nUsing {screen_file}: {len(candidates)} candidates "
        f"({', '.join(c.symbol for c in candidates) or 'none'})"
    )

    ideas: list[TradeIdea] = []
    with connect(cfg.ibkr.client_ids.pick) as ib:
        ib.use_configured_market_data()

        owned: dict[str, Holding] = {
            p.symbol: p
            for p in ib.positions()
            if p.sec_type == "STK" and p.quantity >= MIN_SHARES_FOR_COVERED_CALL
        }
        if owned:
            log.info(f"You own 100+ shares of: {', '.join(owned)} (covered calls will be added)")
        else:
            log.info("No stock positions of 100+ shares, so no covered calls yet.")

        # Screener candidates plus any stock owned (covered calls), even if not a candidate.
        to_check: dict[str, float | None] = {c.symbol: c.iv_rank for c in candidates}
        for symbol in owned:
            to_check.setdefault(symbol, None)

        # Covered calls stay allowed near earnings: the shares are already owned, and the call
        # adds no downside risk, it only caps the upside.
        lookup = earnings.next_earnings(list(to_check))
        if lookup.failed_days:
            log.warning(
                f"couldn't download earnings for {', '.join(lookup.failed_days)} "
                "- check those manually."
            )
        sellable: set[str] = set()
        for c in candidates:
            d = lookup.dates.get(c.symbol)
            if d and within_earnings_buffer(d, today, cfg.pick.earnings_buffer_days):
                log.info(f"  {c.symbol}: SKIPPED - earnings on {d} ({(d - today).days} days away)")
            else:
                sellable.add(c.symbol)

        log.info("\nPricing options (about 10-20 sec per stock)...")
        for symbol, iv_rank in to_check.items():
            if symbol not in sellable and symbol not in owned:
                continue  # skipped for earnings, and no shares to write calls on
            pos = owned.get(symbol)
            ideas += analyze_symbol(
                ib,
                cfg,
                SymbolRequest(
                    symbol=symbol,
                    iv_rank=iv_rank,
                    shares_owned=pos.quantity if pos else 0,
                    avg_cost=pos.avg_cost if pos else None,
                    sell_premium=symbol in sellable,
                    earnings=lookup.dates.get(symbol),
                ),
                today,
            )

    if not ideas:
        raise NoResultsError("No trade ideas built (no candidates, or no option prices came back).")

    ideas = sort_trade_ideas(ideas)
    path = store.write_proposals(ideas)
    _log_ideas(ideas, path)
    return path


def _log_ideas(ideas: list[TradeIdea], path: Path) -> None:
    log.info(
        f"\n{'symbol':<7}{'strategy':<18}{'exp':<10}{'strikes':>15}{'credit$':>9}"
        f"{'capital$':>11}{'ann%':>7}{'otm%':>6}"
    )
    for i in ideas:
        annualized = i.annualized_pct if i.annualized_pct is not None else ""
        log.info(
            f"{i.symbol:<7}{i.strategy.value:<18}{i.expiration:<10}{i.strikes_label:>15}"
            f"{i.credit:>9}{i.capital:>11}{annualized:>7}{i.chance_all_otm_pct:>6}"
        )
    log.info(f"\nSaved {len(ideas)} trade ideas to {path}")
    log.info(
        "Nothing has been ordered. Next: `ibkr-algo orders` checks risk limits and places them."
    )
