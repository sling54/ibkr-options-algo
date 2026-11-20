"""Connection test: can Python reach TWS, and is it logged in to a paper account?"""

from __future__ import annotations

import logging

from ibkr_options_algo.config import AppConfig
from ibkr_options_algo.ibkr.connection import IBFactory, default_ib_factory, probe
from ibkr_options_algo.risk import is_paper_account

log = logging.getLogger(__name__)


def run_check(cfg: AppConfig, ib_factory: IBFactory = default_ib_factory) -> bool:
    """Report the connection and account type. Returns True for a paper session.

    A live login is reported loudly but doesn't fail the command; the orders command is what
    refuses to trade on it.
    """
    result = probe(cfg.ibkr, cfg.ibkr.client_ids.check, ib_factory)
    ib = result.client
    try:
        accounts = ib.managed_accounts()
        log.info(f"\nCONNECTED to {result.label}")
        log.info(f"Account(s): {accounts}")
        log.info(f"IBKR server time: {ib.server_time()}")

        paper = not result.is_live_port and any(is_paper_account(a) for a in accounts)
        if paper:
            log.info("Paper account confirmed (starts with 'DU').")
        else:
            log.warning("this looks like a LIVE account (paper accounts start with 'DU').")
            log.warning(
                "Log out and log back in to TWS choosing 'Paper Trading' before going further."
            )

        summary = ib.account_summary()
        log.info(f"Net liquidation value: ${float(summary.get('NetLiquidation', 0)):,.2f}")
        log.info(f"Buying power:          ${float(summary.get('BuyingPower', 0)):,.2f}")
    finally:
        ib.disconnect()
    log.info("\nAPI is set up correctly.")
    return paper
