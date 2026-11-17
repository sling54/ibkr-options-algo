"""
Helper: find upcoming earnings dates (free, no API key, no IBKR subscription needed).

IBKR only provides earnings dates with a paid data subscription, so this downloads
Nasdaq's public earnings calendar instead: one web request per weekday, each listing every
company reporting that day. Results are saved to earnings_cache.json so running the
scripts several times a day doesn't re-download.

Why we care: a stock often jumps or drops 5-15% on earnings day. If you've SOLD options
(puts, strangles, straddles) and earnings land before expiration, that jump can wipe out
many months of premium. So we avoid opening new trades close to earnings.

Used by pick_trades.py (and later place_orders.py). You can also run it directly:
    python earnings.py NVDA BABA META
"""

import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta

CACHE_FILE = "earnings_cache.json"
DAYS_AHEAD = 60   # how far ahead to look (covers our 30-45 day expirations)


def download_day(day):
    """List of stock symbols reporting earnings on one date (from Nasdaq's website)."""
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={day:%Y-%m-%d}"
    # Nasdaq refuses requests that don't look like they come from a browser
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (options-screener)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    rows = (data.get("data") or {}).get("rows") or []
    return [r["symbol"] for r in rows]


def load_calendar():
    """
    Returns {"YYYY-MM-DD": [symbols reporting that day], ...} for the next DAYS_AHEAD days,
    plus a list of days that couldn't be downloaded.
    """
    today = date.today().isoformat()

    # Reuse today's download if we already have it
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            cache = json.load(f)
        if cache.get("downloaded_on") == today:
            return cache["calendar"], cache["failed_days"]

    print(f"Downloading earnings calendar for the next {DAYS_AHEAD} days from Nasdaq (~20 sec)...")
    calendar, failed = {}, []
    for offset in range(DAYS_AHEAD + 1):
        day = date.today() + timedelta(days=offset)
        if day.weekday() >= 5:  # Saturday / Sunday: no earnings
            continue
        try:
            calendar[day.isoformat()] = download_day(day)
        except Exception:
            failed.append(day.isoformat())
        time.sleep(0.2)  # be polite to Nasdaq's server

    # Only save the cache if most of it worked (otherwise try again next run)
    if len(failed) < 5:
        with open(CACHE_FILE, "w") as f:
            json.dump({"downloaded_on": today, "calendar": calendar, "failed_days": failed}, f)
    return calendar, failed


def next_earnings(symbols):
    """
    Returns ({symbol: next earnings date (a date) or None}, list of days we couldn't check).
    None means no earnings in the next DAYS_AHEAD days (ETFs never have earnings).
    """
    calendar, failed = load_calendar()
    found = {s: None for s in symbols}
    for day in sorted(calendar):                 # walk forward from today
        for s in calendar[day]:
            if s in found and found[s] is None:  # keep the FIRST (soonest) date
                found[s] = datetime.strptime(day, "%Y-%m-%d").date()
    return found, failed


if __name__ == "__main__":
    symbols = [s.upper() for s in sys.argv[1:]] or ["NVDA", "BABA", "META"]
    dates, failed = next_earnings(symbols)
    for s, d in dates.items():
        print(f"{s:<6} {d} ({(d - date.today()).days} days away)" if d else f"{s:<6} none in next {DAYS_AHEAD} days")
    if failed:
        print(f"WARNING: couldn't download these days: {', '.join(failed)}")
