"""Command-line interface: ``ibkr-algo <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ibkr_options_algo import __version__
from ibkr_options_algo.config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from ibkr_options_algo.earnings import EarningsCalendar
from ibkr_options_algo.errors import AlgoError
from ibkr_options_algo.ibkr.connection import (
    Connector,
    IBFactory,
    connector,
    default_ib_factory,
)
from ibkr_options_algo.logging_setup import configure_logging
from ibkr_options_algo.services.chain import run_chain
from ibkr_options_algo.services.check import run_check
from ibkr_options_algo.services.orders import run_orders
from ibkr_options_algo.services.pick import run_pick
from ibkr_options_algo.services.report import run_report
from ibkr_options_algo.services.screen import run_screen
from ibkr_options_algo.sources.http import HttpClient
from ibkr_options_algo.sources.nasdaq import NasdaqClient
from ibkr_options_algo.storage import OutputStore

log = logging.getLogger(__name__)

EXIT_ERROR = 1
EXIT_INTERRUPTED = 130


@dataclass(frozen=True, slots=True)
class App:
    """Everything a command needs, built once from the config."""

    cfg: AppConfig
    connect: Connector
    ib_factory: IBFactory
    store: OutputStore
    nasdaq: NasdaqClient
    earnings: EarningsCalendar


def build_app(cfg: AppConfig, ib_factory: IBFactory = default_ib_factory) -> App:
    store = OutputStore(cfg.output.directory)
    nasdaq = NasdaqClient(HttpClient(cfg.network))
    return App(
        cfg=cfg,
        connect=connector(cfg.ibkr, ib_factory),
        ib_factory=ib_factory,
        store=store,
        nasdaq=nasdaq,
        earnings=EarningsCalendar(nasdaq, cfg.earnings, store.earnings_cache),
    )


# ----------------------------------------------------------------------------- commands


def cmd_check(app: App, _args: argparse.Namespace) -> None:
    run_check(app.cfg, app.ib_factory)


def cmd_chain(app: App, args: argparse.Namespace) -> None:
    run_chain(app.cfg, app.connect, app.store, args.symbol)


def cmd_screen(app: App, _args: argparse.Namespace) -> None:
    run_screen(app.cfg, app.connect, app.store)


def cmd_pick(app: App, _args: argparse.Namespace) -> None:
    run_pick(app.cfg, app.connect, app.store, app.earnings)


def cmd_orders(app: App, args: argparse.Namespace) -> None:
    run_orders(app.cfg, app.connect, app.store, app.earnings, app.nasdaq.market_cap, live=args.live)


def cmd_report(app: App, _args: argparse.Namespace) -> None:
    run_report(app.cfg, app.connect, app.store, app.earnings)


def cmd_earnings(app: App, args: argparse.Namespace) -> None:
    symbols = [s.upper() for s in args.symbols]
    lookup = app.earnings.next_earnings(symbols)
    today = date.today()
    for symbol, day in lookup.dates.items():
        if day:
            log.info(f"{symbol:<6} {day} ({(day - today).days} days away)")
        else:
            log.info(f"{symbol:<6} none in next {app.cfg.earnings.days_ahead} days")
    if lookup.failed_days:
        log.warning(f"couldn't download these days: {', '.join(lookup.failed_days)}")


def cmd_run_all(app: App, args: argparse.Namespace) -> None:
    """check -> screen -> pick -> orders -> report, stopping at the first failure."""
    cmd_check(app, args)
    cmd_screen(app, args)
    cmd_pick(app, args)
    cmd_orders(app, args)
    cmd_report(app, args)


Command = Callable[[App, argparse.Namespace], None]


# ----------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="debug logging with timestamps (also shows ib_async messages)",
    )
    common.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help=f"settings file (default: {DEFAULT_CONFIG_PATH})",
    )

    parser = argparse.ArgumentParser(
        prog="ibkr-algo",
        description="Options premium-selling pipeline for an IBKR paper account.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, func: Command, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text, parents=[common])
        p.set_defaults(func=func)
        return p

    add("check", cmd_check, "test the TWS connection and confirm a paper account")
    chain = add("chain", cmd_chain, "dump one stock's near-term option chain with Greeks")
    chain.add_argument("symbol", help="stock ticker, e.g. AAPL")
    add("screen", cmd_screen, "rank the most active optionable stocks by IV rank")
    add("pick", cmd_pick, "build trade ideas for today's screener candidates")
    orders = add("orders", cmd_orders, "risk-check trade ideas and place paper orders")
    orders.add_argument("--live", action="store_true", help="send the orders (default: dry run)")
    add("report", cmd_report, "daily positions, Greeks and P&L report")
    earnings = add("earnings", cmd_earnings, "next earnings dates from Nasdaq")
    earnings.add_argument("symbols", nargs="+", metavar="SYMBOL")
    run_all = add("run-all", cmd_run_all, "check, screen, pick, orders (dry run) and report")
    run_all.add_argument("--live", action="store_true", help="send the orders (default: dry run)")
    return parser


def main(argv: Sequence[str] | None = None, ib_factory: IBFactory = default_ib_factory) -> int:
    """Console entry point. Returns the process exit code."""
    args = build_parser().parse_args(argv)
    configure_logging(getattr(args, "verbose", False))
    try:
        cfg = load_config(getattr(args, "config", DEFAULT_CONFIG_PATH))
        args.func(build_app(cfg, ib_factory), args)
    except AlgoError as exc:
        log.error(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return EXIT_INTERRUPTED
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
