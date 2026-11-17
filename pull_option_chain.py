"""
Step 2: Pull an options chain from IBKR and save it to a CSV file.

Before running: TWS or IB Gateway must be open and logged in with your PAPER account.
Run with:  python pull_option_chain.py AAPL
(Change AAPL to any US stock ticker.)
"""

import csv
import logging
import math
import sys
from datetime import date, datetime

from ib_async import IB, Option, Stock

SYMBOL = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"
MIN_DAYS = 20            # skip expirations closer than this (too short for the wheel/strangles)
MAX_DAYS = 50            # skip expirations further out than this
STRIKE_RANGE = 0.10      # only strikes within +/-10% of the stock price

# Hide IBKR's routine warnings (e.g. "delayed market data", "open orders timed out")
logging.getLogger("ib_async").setLevel(logging.CRITICAL)

PAPER_PORTS = [7497, 4002]  # TWS paper, IB Gateway paper


def connect():
    ib = IB()
    for port in PAPER_PORTS:
        try:
            ib.connect("127.0.0.1", port, clientId=2, timeout=5)
            return ib
        except Exception:
            ib.disconnect()
    print("Could not connect. Run check_connection.py first.")
    raise SystemExit(1)


def num(x):
    """Turn missing values (None / NaN / -1) into blanks for the CSV."""
    if x is None or (isinstance(x, float) and math.isnan(x)) or x == -1:
        return ""
    return round(x, 4)


ib = connect()

# 4 = use live data if you have a subscription, otherwise free delayed data
ib.reqMarketDataType(4)

# 1. Find the stock and its current price
stock = Stock(SYMBOL, "SMART", "USD")
ib.qualifyContracts(stock)
stock_ticker = ib.reqTickers(stock)[0]
price = stock_ticker.marketPrice()
if math.isnan(price):
    price = stock_ticker.close
print(f"\n{SYMBOL} price: ${price:.2f}")

# 2. Ask IBKR which expirations and strikes exist for this stock
chains = ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
chain = next(c for c in chains if c.exchange == "SMART" and c.tradingClass == SYMBOL)

def days_until(exp):
    return (datetime.strptime(exp, "%Y%m%d").date() - date.today()).days

expirations = [e for e in sorted(chain.expirations) if MIN_DAYS <= days_until(e) <= MAX_DAYS]
if not expirations:  # fallback: nearest 2 expirations
    expirations = sorted(chain.expirations)[:2]
strikes = [
    s for s in sorted(chain.strikes)
    if price * (1 - STRIKE_RANGE) <= s <= price * (1 + STRIKE_RANGE)
]
print(f"Expirations ({MIN_DAYS}-{MAX_DAYS} days out): {expirations}")
print(f"Strikes: {len(strikes)} between ${strikes[0]} and ${strikes[-1]}")

# 3. Build every call and put for those expirations/strikes
contracts = [
    Option(SYMBOL, exp, strike, right, "SMART", tradingClass=chain.tradingClass)
    for exp in expirations
    for strike in strikes
    for right in ["C", "P"]
]
contracts = [c for c in ib.qualifyContracts(*contracts) if c and c.conId]
print(f"Requesting prices for {len(contracts)} option contracts (takes ~30-60 sec)...")

# 4. Get prices and Greeks in small batches.
# IBKR limits how many quotes you can stream at once (~100), so asking for all
# contracts in one go leaves many of them blank.
BATCH_SIZE = 40
WAIT_SECONDS = 6
tickers = []
for i in range(0, len(contracts), BATCH_SIZE):
    batch = contracts[i:i + BATCH_SIZE]
    batch_tickers = [ib.reqMktData(c) for c in batch]
    ib.sleep(WAIT_SECONDS)  # give IBKR time to send bid/ask and Greeks
    for c in batch:
        ib.cancelMktData(c)
    tickers.extend(batch_tickers)
    print(f"  got batch {i // BATCH_SIZE + 1} of {math.ceil(len(contracts) / BATCH_SIZE)}")

rows = []
for t in tickers:
    c = t.contract
    g = t.modelGreeks
    rows.append({
        "expiration": c.lastTradeDateOrContractMonth,
        "strike": c.strike,
        "type": "Call" if c.right == "C" else "Put",
        "bid": num(t.bid),
        "ask": num(t.ask),
        "last": num(t.last),
        "implied_vol": num(g.impliedVol) if g else "",
        "delta": num(g.delta) if g else "",
        "gamma": num(g.gamma) if g else "",
        "theta": num(g.theta) if g else "",
        "vega": num(g.vega) if g else "",
    })

rows.sort(key=lambda r: (r["expiration"], r["type"], r["strike"]))

# 5. Save to CSV
filename = f"{SYMBOL}_options_{datetime.now():%Y%m%d_%H%M}.csv"
with open(filename, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

# 6. Quick preview in the terminal
print(f"\n{'exp':<10}{'strike':>8}{'type':>6}{'bid':>8}{'ask':>8}{'IV':>8}{'delta':>8}")
for r in rows[:10]:
    print(f"{r['expiration']:<10}{r['strike']:>8}{r['type']:>6}{str(r['bid']):>8}"
          f"{str(r['ask']):>8}{str(r['implied_vol']):>8}{str(r['delta']):>8}")

filled = sum(1 for r in rows if r["bid"] != "")
print(f"\nSaved {len(rows)} contracts to {filename}")
print(f"{filled} of {len(rows)} have bid prices.")
if filled == 0:
    print("No prices came back: see the note about options market data.")

ib.disconnect()
