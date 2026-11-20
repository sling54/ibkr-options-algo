"""Console logging: plain messages normally, timestamps and debug detail with --verbose."""

from __future__ import annotations

import logging
import sys

PACKAGE_LOGGER = "ibkr_options_algo"


class _ConsoleFormatter(logging.Formatter):
    """Prefix warnings and errors with their level; leave normal output untouched."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if record.levelno >= logging.WARNING:
            return f"{record.levelname}: {message}"
        return message


def configure_logging(verbose: bool = False) -> None:
    """Send package logs to stdout. ib_async's routine warnings stay hidden unless verbose."""
    handler = logging.StreamHandler(sys.stdout)
    if verbose:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    else:
        handler.setFormatter(_ConsoleFormatter("%(message)s"))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    logging.getLogger(PACKAGE_LOGGER).setLevel(logging.DEBUG if verbose else logging.INFO)
    # ib_async logs "delayed market data" and similar notices for every request.
    logging.getLogger("ib_async").setLevel(logging.INFO if verbose else logging.CRITICAL)
