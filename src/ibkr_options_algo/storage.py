"""Reading and writing the pipeline's files, all inside the output directory."""

from __future__ import annotations

import csv
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path

from ibkr_options_algo.errors import MissingDataError
from ibkr_options_algo.models import (
    YES,
    CsvRow,
    LoggedOrder,
    OrderLogEntry,
    PositionReportRow,
    ScreenCandidate,
    ScreenResult,
    TradeIdea,
)

SCREEN_PREFIX = "vol_screen_"
PROPOSALS_PREFIX = "proposed_trades_"
REPORT_PREFIX = "daily_report_"
ORDER_LOG_NAME = "orders_log.csv"
EARNINGS_CACHE_NAME = "earnings_cache.json"
STAMP_FORMAT = "%Y%m%d_%H%M"
DAY_FORMAT = "%Y%m%d"


def write_csv(path: Path, rows: Sequence[CsvRow]) -> None:
    """Write rows with the first row's keys as the header (no-op for an empty list)."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


class OutputStore:
    """Knows where every output file lives and how it is formatted."""

    def __init__(self, directory: Path, now: Callable[[], datetime] = datetime.now) -> None:
        self.directory = directory
        self._now = now

    def _stamped(self, prefix: str) -> Path:
        return self.directory / f"{prefix}{self._now():{STAMP_FORMAT}}.csv"

    def _newest(self, prefix: str) -> Path | None:
        files = sorted(self.directory.glob(f"{prefix}*.csv"))
        return files[-1] if files else None

    @property
    def earnings_cache(self) -> Path:
        return self.directory / EARNINGS_CACHE_NAME

    @property
    def order_log(self) -> Path:
        return self.directory / ORDER_LOG_NAME

    # ------------------------------------------------------------------ screener

    def write_screen(self, results: Sequence[ScreenResult]) -> Path:
        path = self._stamped(SCREEN_PREFIX)
        write_csv(path, [r.to_csv_row() for r in results])
        return path

    def latest_candidates(self) -> tuple[Path, list[ScreenCandidate]]:
        """Newest screener file and its rows flagged as premium-selling candidates."""
        path = self._newest(SCREEN_PREFIX)
        if path is None:
            raise MissingDataError(
                f"No {SCREEN_PREFIX}*.csv in {self.directory}/. Run `ibkr-algo screen` first."
            )
        rows = read_csv(path)
        return path, [
            ScreenCandidate.from_csv_row(r) for r in rows if r["sell_premium_candidate"] == YES
        ]

    # ------------------------------------------------------------------ proposals

    def write_proposals(self, ideas: Sequence[TradeIdea]) -> Path:
        path = self._stamped(PROPOSALS_PREFIX)
        write_csv(path, [i.to_csv_row() for i in ideas])
        return path

    def latest_proposals(self) -> tuple[Path, list[TradeIdea]]:
        path = self._newest(PROPOSALS_PREFIX)
        if path is None:
            raise MissingDataError(
                f"No {PROPOSALS_PREFIX}*.csv in {self.directory}/. Run `ibkr-algo pick` first."
            )
        return path, [TradeIdea.from_csv_row(r) for r in read_csv(path)]

    # ------------------------------------------------------------------ order log

    def read_order_log(self) -> list[LoggedOrder]:
        if not self.order_log.exists():
            return []
        return [LoggedOrder.from_csv_row(r) for r in read_csv(self.order_log)]

    def append_order_log(self, entry: OrderLogEntry) -> None:
        row = entry.to_csv_row()
        new_file = not self.order_log.exists()
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.order_log.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    # ------------------------------------------------------------------ report

    def write_report(self, text: str, rows: Sequence[PositionReportRow], day: date) -> list[Path]:
        """Save the text summary and, if there are positions, the per-position CSV."""
        stamp = f"{day:{DAY_FORMAT}}"
        self.directory.mkdir(parents=True, exist_ok=True)
        text_path = self.directory / f"{REPORT_PREFIX}{stamp}.txt"
        text_path.write_text(text + "\n")
        written = [text_path]
        if rows:
            csv_path = self.directory / f"{REPORT_PREFIX}{stamp}.csv"
            write_csv(csv_path, [r.to_csv_row() for r in rows])
            written.append(csv_path)
        return written

    # ------------------------------------------------------------------ chain dump

    def write_chain(self, symbol: str, rows: Sequence[CsvRow]) -> Path:
        path = self.directory / f"{symbol}_options_{self._now():{STAMP_FORMAT}}.csv"
        write_csv(path, rows)
        return path
