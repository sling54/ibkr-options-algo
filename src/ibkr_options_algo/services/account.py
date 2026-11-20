"""Account state shared by the orders and report commands."""

from __future__ import annotations

from dataclasses import dataclass

from ibkr_options_algo.ibkr.client import IbkrClient
from ibkr_options_algo.risk import busy_symbols, capital_in_use
from ibkr_options_algo.storage import OutputStore

INIT_MARGIN_TAG = "InitMarginReq"


@dataclass(slots=True)
class AccountState:
    capital_used: float
    busy: set[str]


def load_account_state(ib: IbkrClient, account: str, store: OutputStore) -> AccountState:
    """Capital already committed and the stocks that already have a position or order.

    Capital in use = IBKR's margin requirement + cash reserved for short puts we hold
    + capital of our own orders still waiting to fill (from the order log).
    """
    summary = ib.account_summary(account)
    option_positions = [p for p in ib.positions(account) if p.sec_type == "OPT"]
    open_orders = ib.open_orders()
    used = capital_in_use(
        float(summary.get(INIT_MARGIN_TAG, 0)),
        option_positions,
        open_orders,
        store.read_order_log(),
    )
    return AccountState(capital_used=used, busy=busy_symbols(option_positions, open_orders))
