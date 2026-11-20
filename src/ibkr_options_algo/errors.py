"""Exceptions with messages written for the person running the CLI."""

from __future__ import annotations


class AlgoError(Exception):
    """Base class: the CLI prints the message and exits with status 1."""


class ConfigError(AlgoError):
    """config.toml is missing, unreadable or fails validation."""


class ConnectionFailedError(AlgoError):
    """TWS / IB Gateway could not be reached on any paper port."""

    def __init__(self, ports: tuple[int, ...]) -> None:
        port_list = ", ".join(str(p) for p in ports)
        super().__init__(
            f"Could not connect to TWS / IB Gateway on paper port(s) {port_list}.\n"
            "  1. Is TWS (or IB Gateway) open and logged in with your PAPER account?\n"
            "  2. TWS: Global Configuration > API > Settings > tick "
            "'Enable ActiveX and Socket Clients'.\n"
            "  3. Check the 'Socket port' there is 7497 (TWS paper) or 4002 (Gateway paper)."
        )


class LiveAccountError(AlgoError):
    """The connected account is not an IBKR paper account."""


class MissingDataError(AlgoError):
    """An input file or a piece of market data the step needs is not available."""


class NoResultsError(AlgoError):
    """A step finished but produced nothing to pass on to the next step."""


class NetworkError(AlgoError):
    """An HTTP request failed after all retries."""
