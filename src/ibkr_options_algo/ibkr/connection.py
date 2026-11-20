"""Connecting to TWS / IB Gateway. Trading commands only ever try paper ports."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

from ib_async import IB

from ibkr_options_algo.config import LIVE_PORTS, IbkrConfig
from ibkr_options_algo.errors import ConnectionFailedError
from ibkr_options_algo.ibkr.client import IbkrClient

log = logging.getLogger(__name__)

IBFactory = Callable[[], IB]


def default_ib_factory() -> IB:
    return IB()


#: Opens a session for a client id; services take one of these so tests can inject a fake.
Connector = Callable[[int], AbstractContextManager[IbkrClient]]

PORT_LABELS = {
    7497: "TWS - PAPER",
    4002: "IB Gateway - PAPER",
    7496: "TWS - LIVE",
    4001: "IB Gateway - LIVE",
}


@dataclass(frozen=True, slots=True)
class ProbeResult:
    client: IbkrClient
    port: int

    @property
    def label(self) -> str:
        default = "LIVE" if self.port in LIVE_PORTS else "PAPER"
        return PORT_LABELS.get(self.port, f"port {self.port} - {default}")

    @property
    def is_live_port(self) -> bool:
        return self.port in LIVE_PORTS


def _try_ports(ib: IB, cfg: IbkrConfig, ports: Sequence[int], client_id: int) -> int | None:
    for port in ports:
        try:
            ib.connect(cfg.host, port, clientId=client_id, timeout=cfg.connect_timeout_seconds)
            return port
        except Exception as exc:  # ib_async raises several unrelated types on failure
            log.debug("No TWS/Gateway on %s:%d (%s)", cfg.host, port, exc)
            ib.disconnect()
    return None


@contextmanager
def connect(
    cfg: IbkrConfig, client_id: int, ib_factory: IBFactory = default_ib_factory
) -> Iterator[IbkrClient]:
    """Connect on the first paper port that answers; always disconnects afterwards."""
    ib = ib_factory()
    port = _try_ports(ib, cfg, cfg.paper_ports, client_id)
    if port is None:
        raise ConnectionFailedError(cfg.paper_ports)
    log.debug("Connected to %s:%d as client %d", cfg.host, port, client_id)
    try:
        yield IbkrClient(ib, port, cfg)
    finally:
        ib.disconnect()


def connector(cfg: IbkrConfig, ib_factory: IBFactory = default_ib_factory) -> Connector:
    """A Connector bound to this config."""

    def open_session(client_id: int) -> AbstractContextManager[IbkrClient]:
        return connect(cfg, client_id, ib_factory)

    return open_session


def probe(
    cfg: IbkrConfig, client_id: int, ib_factory: IBFactory = default_ib_factory
) -> ProbeResult:
    """Connection test: tries paper ports, then live ones, only to warn about a live login.

    The caller must disconnect. Nothing that trades uses this function.
    """
    ib = ib_factory()
    ports = [*cfg.paper_ports, *sorted(LIVE_PORTS, reverse=True)]
    port = _try_ports(ib, cfg, ports, client_id)
    if port is None:
        raise ConnectionFailedError(cfg.paper_ports)
    return ProbeResult(IbkrClient(ib, port, cfg), port)
