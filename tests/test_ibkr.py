"""The IBKR adapter and connection rules, on the fake broker."""

from __future__ import annotations

import pytest

from ibkr_options_algo.config import AppConfig
from ibkr_options_algo.errors import ConnectionFailedError, LiveAccountError
from ibkr_options_algo.ibkr.client import IbkrClient
from ibkr_options_algo.ibkr.connection import connect
from ibkr_options_algo.models import OrderSpec
from tests.fake_ib import FakeIB, FakeWorld

# ----------------------------------------------------------------------------- connection


def test_connection_failure_is_clear(cfg: AppConfig, world: FakeWorld) -> None:
    world.open_ports = ()
    with (
        pytest.raises(ConnectionFailedError, match="Is TWS"),
        connect(cfg.ibkr, 5, FakeIB),
    ):
        pass


def test_trading_connection_never_tries_live_ports(
    cfg: AppConfig, world: FakeWorld
) -> None:
    world.open_ports = (7496, 4001)  # only a LIVE session is running
    with pytest.raises(ConnectionFailedError), connect(cfg.ibkr, 5, FakeIB):
        pass
    assert world.connects == []


def test_connect_uses_first_paper_port_and_client_id(
    cfg: AppConfig, world: FakeWorld
) -> None:
    world.open_ports = (4002,)
    with connect(cfg.ibkr, 5, FakeIB) as ib:
        assert ib.port == 4002
    assert world.connects == [(4002, 5)]


# ----------------------------------------------------------------------------- adapter


def test_place_order_refuses_live_account_or_port(
    cfg: AppConfig, world: FakeWorld
) -> None:
    spec = OrderSpec("HOOD", (next(iter(world.options)),), 1, 1.0)
    with pytest.raises(LiveAccountError):
        IbkrClient(FakeIB(world), 7497, cfg.ibkr).place_order(spec, "U1234567")  # type: ignore[arg-type]
    with pytest.raises(LiveAccountError):
        IbkrClient(FakeIB(world), 7496, cfg.ibkr).place_order(spec, "DU1234567")  # type: ignore[arg-type]
    assert world.placed == []


def test_what_if_sends_day_tif_and_account(cfg: AppConfig, world: FakeWorld) -> None:
    client = IbkrClient(FakeIB(world), 7497, cfg.ibkr)  # type: ignore[arg-type]
    con_ids = [c for c, spec in world.options.items() if spec.stock.symbol == "HOOD"][
        :2
    ]
    client.requalify_options(con_ids)
    margin = client.what_if_margin(OrderSpec("HOOD", tuple(con_ids), 1, 3.2))
    assert margin == pytest.approx(2 * 0.2 * 68.55 * 100)
    contract, order = world.what_if_calls[-1]
    assert (contract.secType, order.action, order.lmtPrice, order.tif) == (
        "BAG",
        "BUY",
        -3.2,
        "DAY",
    )
    assert order.account == "DU1234567"


def test_option_quotes_drop_contracts_without_greeks(
    cfg: AppConfig, world: FakeWorld
) -> None:
    client = IbkrClient(FakeIB(world), 7497, cfg.ibkr)  # type: ignore[arg-type]
    ids = [c for c, s in world.options.items() if s.stock.symbol == "NOGREEK"][:4]
    client.requalify_options(ids)
    assert client.option_quotes(ids) == []
