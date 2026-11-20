"""Daily Greeks and P&L report for the paper account."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ibkr_options_algo.config import AppConfig
from ibkr_options_algo.earnings import EarningsCalendar
from ibkr_options_algo.ibkr.connection import Connector
from ibkr_options_algo.reporting import ReportInputs, build_position_rows, clean_pnl, render_report
from ibkr_options_algo.risk import account_value
from ibkr_options_algo.services.account import load_account_state
from ibkr_options_algo.storage import OutputStore

log = logging.getLogger(__name__)


def run_report(
    cfg: AppConfig,
    connect: Connector,
    store: OutputStore,
    earnings: EarningsCalendar,
    now: Callable[[], datetime] = datetime.now,
) -> list[Path]:
    """Build, print and save the daily report. Read-only: never places orders."""
    with connect(cfg.ibkr.client_ids.report) as ib:
        ib.use_configured_market_data()
        account = ib.primary_account()

        net_liq = account_value(ib.account_summary(account))
        pnl = clean_pnl(ib.pnl(account))
        state = load_account_state(ib, account, store)

        items = [p for p in ib.portfolio(account) if p.quantity != 0]
        greeks = ib.position_greeks([p.con_id for p in items if p.is_option])
        symbols = sorted({p.symbol for p in items})
        dates = earnings.next_earnings(symbols).dates if symbols else {}

        today = now().date()
        rows = build_position_rows(
            items, greeks, dates, today, cfg.report.take_profit_pct, cfg.report.close_at_dte
        )
        open_orders = ib.open_orders()

    text = render_report(
        ReportInputs(
            generated_at=now(),
            account=account,
            net_liq=net_liq,
            pnl=pnl,
            capital_used=state.capital_used,
            busy_symbols=frozenset(state.busy),
            rows=rows,
            open_orders=open_orders,
            max_total_pct=cfg.risk.max_total_pct,
            max_positions=cfg.risk.max_positions,
        )
    )
    paths = store.write_report(text, rows, today)
    log.info(text)
    log.info(f"\nSaved {' and '.join(str(p) for p in paths)}")
    return paths
