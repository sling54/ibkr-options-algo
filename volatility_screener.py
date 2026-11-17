"""
Step 3: Volatility screener.

What it does:
  1. Asks IBKR's market scanner for the 50 US stocks/ETFs with the most OPTION volume
     (so every result is optionable and has a liquid options market).
  2. For each one, downloads 1 year of daily implied volatility (IV) and daily
     closing prices from IBKR, and calculates realized volatility (HV) from the prices.
  3. Computes:
       - IV rank:        where today's IV sits in its 1-year range (0 = lowest, 100 = highest)
       - IV percentile:  % of days in the past year when IV was LOWER than today
       - IV - HV gap:    how much more (or less) the options market expects the stock to move
                         than it actually has been moving. Positive = options look "expensive".
  4. Ranks them (highest IV rank first) and saves a CSV.

Why this matters for the wheel / covered calls / short strangles:
  Selling options pays the most when IV is high relative to its own history (high IV rank)
  AND higher than the stock's actual movement (positive IV - HV gap).

Before running: TWS or IB Gateway must be open and logged in with your PAPER account.
Run with:  python volatility_screener.py
Takes about 1-4 minutes (depends on how fast IBKR answers).
"""

import csv
import logging
import math
from datetime import datetime

from ib_async import IB, ScannerSubscription

# ---------------- Settings you can change ----------------
NUM_STOCKS = 50          # IBKR's scanner returns at most 50 rows
INCLUDE_ETFS = True      # False = only company stocks (drops SPY, QQQ, GLD, ...)
MIN_IV_RANK = 50         # "candidate" flag: IV rank at least this high...
MIN_GAP = 0.0            # ...and IV at least this much above HV (in vol points)
HV_DAYS = 20             # realized-vol window in trading days (20 = ~1 month,
                         # the same horizon as IBKR's 30-calendar-day IV)
# ----------------------------------------------------------

# Hide IBKR's routine warnings (e.g. "delayed market data")
logging.getLogger("ib_async").setLevel(logging.CRITICAL)

PAPER_PORTS = [7497, 4002]  # TWS paper, IB Gateway paper


def connect():
    ib = IB()
    for port in PAPER_PORTS:
        try:
            ib.connect("127.0.0.1", port, clientId=3, timeout=5)
            return ib
        except Exception:
            ib.disconnect()
    print("Could not connect. Run check_connection.py first.")
    raise SystemExit(1)


def get_most_active(ib):
    """Return a list of stock contracts, most active options first."""
    scan = ScannerSubscription(
        instrument="STK",                   # stocks (IBKR counts ETFs as stocks too)
        locationCode="STK.US.MAJOR",        # NYSE, NASDAQ, AMEX, ARCA ...
        scanCode="OPT_VOLUME_MOST_ACTIVE",  # sorted by how many options traded today
        numberOfRows=NUM_STOCKS,
        stockTypeFilter="ALL" if INCLUDE_ETFS else "CORP",
    )
    results = ib.reqScannerData(scan)
    contracts = []
    for r in results:
        c = r.contractDetails.contract
        c.exchange = "SMART"  # route requests through IBKR's smart router
        contracts.append(c)
    return contracts


def get_daily_closes(ib, contract, what):
    """
    Download 1 year of daily bars and return just the closing values.
    what = "OPTION_IMPLIED_VOLATILITY" -> IV as a decimal (0.25 means 25% a year)
    what = "TRADES"                    -> the stock's daily closing price
    """
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",        # "" = up to now
        durationStr="1 Y",
        barSizeSetting="1 day",
        whatToShow=what,
        useRTH=True,           # regular trading hours only
        timeout=30,
    )
    return [b.close for b in bars if b.close and not math.isnan(b.close) and b.close > 0]


def realized_vol(prices, days):
    """
    Realized (historical) volatility over the last `days` trading days.
    Standard method: take the daily % changes (as log returns), measure how
    spread out they are (standard deviation), then scale from 1 day to 1 year
    by multiplying by sqrt(252 trading days).
    We calculate this ourselves because IBKR's own HISTORICAL_VOLATILITY series
    jumped around in testing and didn't match the actual price moves.
    """
    recent = prices[-(days + 1):]   # days+1 prices give `days` daily returns
    returns = [math.log(today / yesterday) for yesterday, today in zip(recent[:-1], recent[1:])]
    average = sum(returns) / len(returns)
    variance = sum((r - average) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(252)


def iv_stats(iv_history, prices):
    """Work out IV rank, IV percentile and the IV-HV gap. Returns a dict."""
    current_iv = iv_history[-1]
    low, high = min(iv_history), max(iv_history)
    current_hv = realized_vol(prices, HV_DAYS)

    # IV rank: 0 when today's IV is the 1-year low, 100 when it's the 1-year high
    iv_rank = 100 * (current_iv - low) / (high - low) if high > low else 0

    # IV percentile: % of past days with IV below today's
    days_lower = sum(1 for iv in iv_history if iv < current_iv)
    iv_percentile = 100 * days_lower / len(iv_history)

    # Gap in "vol points": IV 30% vs HV 22% -> +8 points
    gap = (current_iv - current_hv) * 100

    return {
        "price": round(prices[-1], 2),
        "iv_now_%": round(current_iv * 100, 1),
        "iv_1y_low_%": round(low * 100, 1),
        "iv_1y_high_%": round(high * 100, 1),
        "iv_rank": round(iv_rank, 1),
        "iv_percentile": round(iv_percentile, 1),
        f"hv_{HV_DAYS}d_%": round(current_hv * 100, 1),
        "iv_minus_hv": round(gap, 1),
        "iv_hv_ratio": round(current_iv / current_hv, 2) if current_hv else "",
        "days_of_data": len(iv_history),
    }


ib = connect()

# 1. Get the list of stocks
print(f"\nScanning for the {NUM_STOCKS} most active optionable stocks...")
contracts = get_most_active(ib)
print(f"Scanner returned {len(contracts)}: {', '.join(c.symbol for c in contracts)}")

# 2. Download volatility history for each one
print("\nDownloading 1 year of IV and prices for each (about 1-4 min)...")
rows = []
skipped = []
for n, contract in enumerate(contracts, start=1):
    symbol = contract.symbol
    try:
        iv_history = get_daily_closes(ib, contract, "OPTION_IMPLIED_VOLATILITY")
        prices = get_daily_closes(ib, contract, "TRADES")
    except Exception as e:
        iv_history, prices = [], []
        print(f"  {symbol}: error {e}")

    # Need a decent amount of history for IV rank to mean anything
    # (a stock that listed 2 months ago only has 2 months of range).
    if len(iv_history) < 60 or len(prices) <= HV_DAYS:
        skipped.append(symbol)
        print(f"  [{n:>2}/{len(contracts)}] {symbol:<6} skipped (only {len(iv_history)} days of IV data)")
        continue

    row = {"activity_rank": n, "symbol": symbol}
    row.update(iv_stats(iv_history, prices))
    # Simple flag for "options look rich here" = worth a look for premium selling
    row["sell_premium_candidate"] = (
        "YES" if row["iv_rank"] >= MIN_IV_RANK and row["iv_minus_hv"] > MIN_GAP else ""
    )
    rows.append(row)
    print(f"  [{n:>2}/{len(contracts)}] {symbol:<6} IV {row['iv_now_%']:>5}%  "
          f"IV rank {row['iv_rank']:>5}  IV-HV {row['iv_minus_hv']:>+6}")

ib.disconnect()

if not rows:
    print("No volatility data came back. Is TWS logged in?")
    raise SystemExit(1)

# 3. Rank: highest IV rank first; ties broken by the bigger IV-HV gap
rows.sort(key=lambda r: (r["iv_rank"], r["iv_minus_hv"]), reverse=True)
for position, row in enumerate(rows, start=1):
    row["rank"] = position

# Put "rank" as the first column
columns = ["rank"] + [k for k in rows[0] if k != "rank"]

# 4. Save to CSV
filename = f"vol_screen_{datetime.now():%Y%m%d_%H%M}.csv"
with open(filename, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)

# 5. Show the top 15 in the terminal
print(f"\n{'rank':<5}{'symbol':<8}{'IV%':>6}{'IVrank':>8}{'IVpct':>7}{'HV%':>7}{'IV-HV':>7}  candidate")
for r in rows[:15]:
    print(f"{r['rank']:<5}{r['symbol']:<8}{r['iv_now_%']:>6}{r['iv_rank']:>8}{r['iv_percentile']:>7}"
          f"{r[f'hv_{HV_DAYS}d_%']:>7}{r['iv_minus_hv']:>+7}  {r['sell_premium_candidate']}")

candidates = [r["symbol"] for r in rows if r["sell_premium_candidate"]]
print(f"\nSaved {len(rows)} stocks to {filename}")
print(f"Premium-selling candidates (IV rank >= {MIN_IV_RANK} and IV > HV): {', '.join(candidates) or 'none today'}")
if skipped:
    print(f"Skipped (not enough history): {', '.join(skipped)}")
