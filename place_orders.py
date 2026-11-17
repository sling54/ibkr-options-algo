"""
Step 5: Check risk limits and place paper orders for the trade ideas from pick_trades.py.

What it does:
  1. Reads today's newest proposed_trades_*.csv.
  2. For each stock (highest IV rank first) it tries the strategies in STRATEGY_PRIORITY
     order and takes the FIRST one that passes every risk check. Max one trade per stock.
  3. Risk checks (change them in the settings below):
       - PAPER account only (refuses to run on a live account)
       - No earnings within 7 days, and none before the option's expiration
       - Strangles/straddles only on ETFs or large companies (market cap >= $10B)
       - Bid-ask spread on every leg <= 15% (re-checked with fresh prices right now)
       - Each trade uses <= 10% of your account value
       - All trades together use <= 50% of your account value
       - At most 5 stocks with option positions or open orders
       - Skips stocks you already have an option position or open order in
  4. Places LIMIT orders at the current mid price (halfway between bid and ask), good
     for today only, and logs each one to orders_log.csv.

SAFE BY DEFAULT: without --live it only PRINTS what it would do (a "dry run").
Run with:
    python place_orders.py           # dry run - nothing is sent
    python place_orders.py --live    # actually sends the orders to your PAPER account

To cancel orders: TWS > Activity panel > right-click an order > Cancel.
"""

import csv
import glob
import json
import logging
import os
import sys
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ib_async import ComboLeg, Contract, IB, LimitOrder

import earnings                                    # our earnings.py helper
from pick_trades import get_quotes, ibkr_margin    # reuse code from pick_trades.py

# ---------------- Risk limits / settings you can change ----------------
MAX_TRADE_PCT = 10           # max % of account value one trade can use
MAX_TOTAL_PCT = 50           # max % of account value all trades together can use
MAX_POSITIONS = 5            # max number of stocks with option positions / open orders
MAX_SPREAD_PCT = 15          # skip a leg if (ask - bid) / mid is wider than this
LARGE_CAP = 10e9             # strangles/straddles only on ETFs or companies this big ($)
EARNINGS_BUFFER_DAYS = 7     # no new trades this close to earnings
BLOCK_EARNINGS_BEFORE_EXPIRY = True  # no trades that would be open during earnings
# Which strategy to try first for each stock (the wheel first, riskier ones last)
STRATEGY_PRIORITY = ["covered_call", "cash_secured_put", "short_strangle", "short_straddle"]
# ------------------------------------------------------------------------

LIVE = "--live" in sys.argv
LOG_FILE = "orders_log.csv"
logging.getLogger("ib_async").setLevel(logging.CRITICAL)
PAPER_PORTS = [7497, 4002]


def connect():
    ib = IB()
    for port in PAPER_PORTS:  # only paper ports - never the live ones (7496/4001)
        try:
            ib.connect("127.0.0.1", port, clientId=5, timeout=5)
            return ib
        except Exception:
            ib.disconnect()
    print("Could not connect. Run check_connection.py first.")
    raise SystemExit(1)


def read_proposals():
    files = sorted(glob.glob("proposed_trades_*.csv"))
    if not files:
        print("No proposed_trades_*.csv found. Run pick_trades.py first.")
        raise SystemExit(1)
    newest = files[-1]
    # Prices go stale fast: only trade from a file made today
    if f"_{date.today():%Y%m%d}_" not in newest:
        print(f"{newest} is not from today. Run volatility_screener.py and pick_trades.py again.")
        raise SystemExit(1)
    with open(newest) as f:
        return newest, list(csv.DictReader(f))


def market_cap(symbol):
    """Company size in $ from Nasdaq's free quote page. None if not available."""
    url = f"https://api.nasdaq.com/api/quote/{symbol}/summary?assetclass=stocks"
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (options-screener)", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
        value = data["data"]["summaryData"]["MarketCap"]["value"]
        return float(value.replace(",", ""))
    except Exception:
        return None


def round_price(price):
    """Options trade in $0.05 steps above $3 (and $0.01 below for most liquid stocks)."""
    return round(round(price / 0.05) * 0.05, 2) if price >= 3 else round(price, 2)


def fresh_quotes(ib, row):
    """Re-price the trade's legs right now. Returns {"P": quote, "C": quote} (only legs that exist)."""
    contracts = []
    for key in ("put_conid", "call_conid"):
        if row[key]:
            c = Contract(conId=int(row[key]), exchange="SMART")
            ib.qualifyContracts(c)
            contracts.append(c)
    return {q["right"]: q for q in get_quotes(ib, contracts)}, len(contracts)


def read_log():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        return list(csv.DictReader(f))


def write_log(entry):
    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=entry.keys())
        if new_file:
            writer.writeheader()
        writer.writerow(entry)


def capital_in_use(ib, account):
    """
    How much of the account is already committed, and which stocks are "busy".
    Capital in use = IBKR's margin requirement
                   + cash set aside for short puts we hold (cash-secured)
                   + capital of our own orders that are still waiting to fill
    Busy = stocks with an option position or an open order.
    (daily_report.py uses this too.)
    """
    summary = {r.tag: r.value for r in ib.accountSummary(account)}
    option_positions = [p for p in ib.positions(account) if p.contract.secType == "OPT"]
    open_trades = ib.reqAllOpenOrders()
    busy = {p.contract.symbol for p in option_positions} | {t.contract.symbol for t in open_trades}

    used = float(summary.get("InitMarginReq", 0))
    used += sum(p.contract.strike * 100 * -p.position for p in option_positions
                if p.contract.right == "P" and p.position < 0)
    open_ids = {str(t.order.orderId) for t in open_trades}
    used += sum(float(e["capital_$"]) for e in read_log() if e["order_id"] in open_ids)
    return used, busy


def check_trade(ib, row, earnings_date, net_liq, used):
    """
    Run every risk check on one trade idea.
    Returns (list of reasons it FAILED, capital it would use, fresh leg quotes).
    An empty reasons list means the trade passed.
    """
    reasons = []
    strategy, symbol = row["strategy"], row["symbol"]

    # 1. Earnings
    if earnings_date:
        days = (earnings_date - date.today()).days
        if days <= EARNINGS_BUFFER_DAYS:
            reasons.append(f"earnings in {days} days")
        elif BLOCK_EARNINGS_BEFORE_EXPIRY and earnings_date.strftime("%Y%m%d") <= row["expiration"]:
            reasons.append(f"earnings {earnings_date} before expiration {row['expiration']}")

    # 2. Strangles/straddles (unlimited risk) only on ETFs or large companies
    if strategy in ("short_strangle", "short_straddle") and row["is_etf"] != "YES":
        cap = market_cap(symbol)
        if cap is None or cap < LARGE_CAP:
            size = f"${cap / 1e9:.1f}B" if cap else "unknown"
            reasons.append(f"not an ETF or large cap (market cap {size})")
    if reasons:
        return reasons, 0, {}  # no need to fetch prices for a trade we won't take

    # 3. Fresh prices + bid-ask spread
    quotes, legs_needed = fresh_quotes(ib, row)
    if len(quotes) < legs_needed:
        return ["no live bid/ask right now"], 0, quotes
    for q in quotes.values():
        if q["spread_pct"] > MAX_SPREAD_PCT:
            reasons.append(f"{q['right']} {q['strike']} spread {q['spread_pct']:.0f}% > {MAX_SPREAD_PCT}%")

    # 4. How much of the account it uses
    contracts = int(row["contracts"])
    if strategy == "cash_secured_put":
        capital = float(row["put_strike"]) * 100 * contracts   # cash to buy the shares if assigned
    elif strategy == "covered_call":
        capital = 0.0                                           # covered by shares you own
    else:
        capital = ibkr_margin(ib, symbol, quotes.get("P"), quotes.get("C")) or 0
        if not capital:
            reasons.append("IBKR didn't return a margin number")
    if capital > net_liq * MAX_TRADE_PCT / 100:
        reasons.append(f"uses ${capital:,.0f} > {MAX_TRADE_PCT}% of account")
    if used + capital > net_liq * MAX_TOTAL_PCT / 100:
        reasons.append(f"total would be ${used + capital:,.0f} > {MAX_TOTAL_PCT}% of account")
    return reasons, capital, quotes


def build_order(ib, row, quotes, account):
    """The contract + limit order for this trade, priced at the current mid."""
    legs = [quotes[r] for r in ("P", "C") if r in quotes]
    mid = round_price(sum(q["mid"] for q in legs))
    contracts = int(row["contracts"])
    if len(legs) == 1:
        contract = legs[0]["contract"]
        order = LimitOrder("SELL", contracts, mid, tif="DAY", account=account)
    else:
        # Combo: BUY the combo at a NEGATIVE price = sell both legs for a credit
        contract = Contract(symbol=row["symbol"], secType="BAG", currency="USD", exchange="SMART",
                            comboLegs=[ComboLeg(conId=q["contract"].conId, ratio=1,
                                                action="SELL", exchange="SMART") for q in legs])
        order = LimitOrder("BUY", contracts, -mid, tif="DAY", account=account)
    order.orderRef = f"algo_{row['strategy']}"  # tag so reports can find our orders
    return contract, order, mid


def main():
    proposals_file, proposals = read_proposals()
    print(f"\n{'LIVE MODE: orders WILL be sent' if LIVE else 'DRY RUN: nothing will be sent (add --live to send)'}")
    print(f"Reading {proposals_file}: {len(proposals)} trade ideas")

    ib = connect()
    ib.reqMarketDataType(4)
    account = ib.managedAccounts()[0]
    if not account.startswith("DU"):  # IBKR paper accounts start with "DU"
        print(f"Account {account} is not a paper account. Stopping.")
        ib.disconnect()
        raise SystemExit(1)

    now_ny = datetime.now(ZoneInfo("America/New_York"))
    if not (now_ny.weekday() < 5 and "09:30" <= now_ny.strftime("%H:%M") < "16:00"):
        print("Note: the market is closed. Orders sent now wait until the next open.")

    # ---- What we already have ----
    summary = {r.tag: r.value for r in ib.accountSummary(account)}
    net_liq = float(summary["NetLiquidation"])

    used, busy = capital_in_use(ib, account)

    print(f"Account {account}: value ${net_liq:,.0f}, capital in use ${used:,.0f} "
          f"({100 * used / net_liq:.1f}%), stocks with positions/orders: {sorted(busy) or 'none'}")
    print(f"Limits: {MAX_TRADE_PCT}% per trade (${net_liq * MAX_TRADE_PCT / 100:,.0f}), "
          f"{MAX_TOTAL_PCT}% total (${net_liq * MAX_TOTAL_PCT / 100:,.0f}), {MAX_POSITIONS} stocks max\n")

    # ---- Fresh earnings dates ----
    symbols = sorted({r["symbol"] for r in proposals})
    earnings_dates, failed = earnings.next_earnings(symbols)
    if failed:
        print(f"WARNING: earnings calendar missing for {', '.join(failed)}")

    # ---- Go through stocks, highest IV rank first ----
    def iv_rank(sym):
        value = next(r["iv_rank"] for r in proposals if r["symbol"] == sym)
        return float(value) if value else 0
    symbols.sort(key=iv_rank, reverse=True)

    placed = 0
    for symbol in symbols:
        if len(busy) >= MAX_POSITIONS:
            print(f"Reached {MAX_POSITIONS} stocks - stopping.")
            break
        if symbol in busy:
            print(f"{symbol}: skipped - already have a position or open order")
            continue

        ideas = sorted((r for r in proposals if r["symbol"] == symbol and r["strategy"] in STRATEGY_PRIORITY),
                       key=lambda r: STRATEGY_PRIORITY.index(r["strategy"]))
        for row in ideas:
            reasons, capital, quotes = check_trade(ib, row, earnings_dates.get(symbol), net_liq, used)
            if reasons:
                print(f"{symbol} {row['strategy']}: REJECTED - {'; '.join(reasons)}")
                continue

            contract, order, mid = build_order(ib, row, quotes, account)
            strikes = "/".join(s for s in (row["put_strike"], row["call_strike"]) if s)
            desc = (f"{symbol} {row['strategy']} {row['contracts']}x {row['expiration']} {strikes} "
                    f"@ ${mid:.2f} credit (${mid * 100 * int(row['contracts']):,.0f}), uses ${capital:,.0f}")

            if not LIVE:
                print(f"{symbol} {row['strategy']}: PASSED - would place: {desc}")
                order_id, status = "", "dry_run"
            else:
                trade = ib.placeOrder(contract, order)
                ib.sleep(3)  # give TWS a moment to accept it
                order_id, status = trade.order.orderId, trade.orderStatus.status
                print(f"{symbol} {row['strategy']}: PLACED order #{order_id} ({status}): {desc}")
                for entry in trade.log:
                    if entry.errorCode:
                        print(f"   IBKR message: {entry.message}")

            write_log({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": "live" if LIVE else "dry_run",
                "order_id": order_id,
                "status": status,
                "symbol": symbol,
                "strategy": row["strategy"],
                "expiration": row["expiration"],
                "put_strike": row["put_strike"],
                "call_strike": row["call_strike"],
                "contracts": row["contracts"],
                "limit_credit": mid,
                "capital_$": round(capital, 2),
            })
            used += capital
            busy.add(symbol)
            placed += 1
            break  # one trade per stock

    ib.disconnect()
    verb = "Placed" if LIVE else "Would place"
    print(f"\n{verb} {placed} order(s). Capital in use would be ${used:,.0f} ({100 * used / net_liq:.1f}% of account).")
    print(f"Logged to {LOG_FILE}.")
    if LIVE and placed:
        print("Orders are limit orders at the mid price for today only. Unfilled ones expire at the close.")


if __name__ == "__main__":
    main()
