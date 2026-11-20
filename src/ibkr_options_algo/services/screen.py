"""Volatility screener: IV rank, IV percentile and IV vs realized vol for the most active names.

Selling options pays most when IV is high relative to its own history (high IV rank) AND above
the stock's actual movement (positive IV - HV gap).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ibkr_options_algo.calculations import (
    clean_closes,
    has_enough_history,
    rank_screen_results,
    screen_result,
)
from ibkr_options_algo.config import AppConfig
from ibkr_options_algo.errors import NoResultsError
from ibkr_options_algo.ibkr.client import IV_HISTORY, PRICE_HISTORY
from ibkr_options_algo.ibkr.connection import Connector
from ibkr_options_algo.models import ScreenResult
from ibkr_options_algo.storage import OutputStore

log = logging.getLogger(__name__)

PREVIEW_ROWS = 15


def run_screen(cfg: AppConfig, connect: Connector, store: OutputStore) -> Path:
    """Screen the most active optionable stocks and save vol_screen_*.csv."""
    sc = cfg.screen
    results: list[ScreenResult] = []
    skipped: list[str] = []
    with connect(cfg.ibkr.client_ids.screen) as ib:
        log.info("\nScanning for the %d most active optionable stocks...", sc.num_stocks)
        hits = ib.most_active(sc.num_stocks, sc.include_etfs, sc.scan_code, sc.location_code)
        log.info("Scanner returned %d: %s", len(hits), ", ".join(h.symbol for h in hits))

        log.info("\nDownloading 1 year of IV and prices for each (about 1-4 min)...")
        for n, hit in enumerate(hits, start=1):
            try:
                iv_history = clean_closes(ib.daily_closes(hit.con_id, IV_HISTORY))
                prices = clean_closes(ib.daily_closes(hit.con_id, PRICE_HISTORY))
            except Exception as exc:  # one bad symbol shouldn't stop the whole scan
                iv_history, prices = [], []
                log.warning("%s: error %s", hit.symbol, exc)

            if not has_enough_history(iv_history, prices, sc.min_iv_history_days, sc.hv_days):
                skipped.append(hit.symbol)
                log.info(
                    f"  [{n:>2}/{len(hits)}] {hit.symbol:<6} skipped "
                    f"(only {len(iv_history)} days of IV data)"
                )
                continue

            result = screen_result(
                n, hit.symbol, iv_history, prices, sc.hv_days, sc.min_iv_rank, sc.min_iv_hv_gap
            )
            results.append(result)
            log.info(
                f"  [{n:>2}/{len(hits)}] {hit.symbol:<6} IV {result.iv_now_pct:>5}%  "
                f"IV rank {result.iv_rank:>5}  IV-HV {result.iv_minus_hv:>+6}"
            )

    if not results:
        raise NoResultsError("No volatility data came back. Is TWS logged in?")

    ranked = rank_screen_results(results)
    path = store.write_screen(ranked)
    _log_summary(ranked, skipped, path, cfg)
    return path


def _log_summary(
    ranked: list[ScreenResult], skipped: list[str], path: Path, cfg: AppConfig
) -> None:
    log.info(
        f"\n{'rank':<5}{'symbol':<8}{'IV%':>6}{'IVrank':>8}{'IVpct':>7}{'HV%':>7}{'IV-HV':>7}"
        "  candidate"
    )
    for r in ranked[:PREVIEW_ROWS]:
        log.info(
            f"{r.rank:<5}{r.symbol:<8}{r.iv_now_pct:>6}{r.iv_rank:>8}{r.iv_percentile:>7}"
            f"{r.hv_pct:>7}{r.iv_minus_hv:>+7}  {'YES' if r.is_candidate else ''}"
        )
    candidates = [r.symbol for r in ranked if r.is_candidate]
    log.info("\nSaved %d stocks to %s", len(ranked), path)
    log.info(
        "Premium-selling candidates (IV rank >= %g and IV > HV): %s",
        cfg.screen.min_iv_rank,
        ", ".join(candidates) or "none today",
    )
    if skipped:
        log.info("Skipped (not enough history): %s", ", ".join(skipped))
