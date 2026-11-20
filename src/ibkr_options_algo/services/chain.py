"""Dump one stock's near-term option chain, with Greeks, to CSV."""

from __future__ import annotations

import logging
import math
from datetime import date
from pathlib import Path

from ibkr_options_algo.calculations import expirations_in_window, strikes_near_price
from ibkr_options_algo.config import AppConfig
from ibkr_options_algo.errors import MissingDataError
from ibkr_options_algo.ibkr.client import SMART
from ibkr_options_algo.ibkr.connection import Connector
from ibkr_options_algo.models import CsvRow, CsvValue, RawOptionTicker, Right
from ibkr_options_algo.storage import OutputStore

log = logging.getLogger(__name__)

NO_DATA = -1  # IBKR's marker for a missing price or size
CELL_DIGITS = 4
RIGHTS: tuple[Right, ...] = ("C", "P")


def cell(value: float | None) -> CsvValue:
    """Missing values (None / NaN / -1) become blanks; numbers are rounded."""
    if value is None or (isinstance(value, float) and math.isnan(value)) or value == NO_DATA:
        return ""
    return round(value, CELL_DIGITS)


def chain_row(t: RawOptionTicker) -> CsvRow:
    g = t.greeks
    return {
        "expiration": t.expiration,
        "strike": t.strike,
        "type": "Call" if t.right == "C" else "Put",
        "bid": cell(t.bid),
        "ask": cell(t.ask),
        "last": cell(t.last),
        "implied_vol": cell(g.implied_vol) if g else "",
        "delta": cell(g.delta) if g else "",
        "gamma": cell(g.gamma) if g else "",
        "theta": cell(g.theta) if g else "",
        "vega": cell(g.vega) if g else "",
    }


def run_chain(cfg: AppConfig, connect: Connector, store: OutputStore, symbol: str) -> Path:
    """Pull calls and puts near the money for the configured expiration window."""
    cc = cfg.chain
    symbol = symbol.upper()
    with connect(cfg.ibkr.client_ids.chain) as ib:
        ib.use_configured_market_data()
        con_id = ib.qualify_stock(symbol)
        snapshot = ib.stock_snapshot(con_id)
        price = snapshot.market_price
        if math.isnan(price):
            price = snapshot.close
        if math.isnan(price):
            raise MissingDataError(f"No price for {symbol} (market data not available).")
        log.info(f"\n{symbol} price: ${price:.2f}")

        chain = next(
            (
                c
                for c in ib.option_chains(con_id)
                if c.exchange == SMART and c.trading_class == symbol
            ),
            None,
        )
        if chain is None:
            raise MissingDataError(f"IBKR lists no SMART option chain for {symbol}.")
        expirations = expirations_in_window(
            chain.expirations, date.today(), cc.min_dte, cc.max_dte, cc.fallback_expirations
        )
        strikes = strikes_near_price(chain.strikes, price, cc.strike_range_pct)
        if not strikes:
            raise MissingDataError(f"No {symbol} strikes within {cc.strike_range_pct:g}% of price.")
        log.info(f"Expirations ({cc.min_dte}-{cc.max_dte} days out): {expirations}")
        log.info(f"Strikes: {len(strikes)} between ${strikes[0]} and ${strikes[-1]}")

        con_ids: list[int] = []
        for exp in expirations:
            legs = [(strike, right) for strike in strikes for right in RIGHTS]
            con_ids += ib.qualify_options(symbol, exp, chain.trading_class, legs)
        log.info(f"Requesting prices for {len(con_ids)} option contracts (takes ~30-60 sec)...")
        tickers = ib.raw_option_tickers(con_ids)

    rows = sorted(
        (chain_row(t) for t in tickers),
        key=lambda r: (str(r["expiration"]), str(r["type"]), float(r["strike"])),
    )
    if not rows:
        raise MissingDataError(f"IBKR returned no option contracts for {symbol}.")
    path = store.write_chain(symbol, rows)

    log.info(f"\n{'exp':<10}{'strike':>8}{'type':>6}{'bid':>8}{'ask':>8}{'IV':>8}{'delta':>8}")
    for r in rows[: cc.preview_rows]:
        log.info(
            f"{r['expiration']!s:<10}{r['strike']!s:>8}{r['type']!s:>6}{r['bid']!s:>8}"
            f"{r['ask']!s:>8}{r['implied_vol']!s:>8}{r['delta']!s:>8}"
        )
    filled = sum(1 for r in rows if r["bid"] != "")
    log.info(f"\nSaved {len(rows)} contracts to {path}")
    log.info(f"{filled} of {len(rows)} have bid prices.")
    if filled == 0:
        log.info("No prices came back: check your options market data permissions.")
    return path
