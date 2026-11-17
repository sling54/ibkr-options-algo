"""
Step 6: Daily Greeks + P&L report for your paper account.

What it shows:
  - Account: value, today's P&L, unrealized and realized P&L, capital in use vs your limits
  - Every position: P&L, Greeks, days to expiration, and a suggested ACTION, e.g.
      TAKE PROFIT  - a short option has made >= 50% of its max profit (common rule: buy it back)
      21 DTE       - 21 days or less left: gamma risk grows, consider closing or rolling
      ITM          - option is in-the-money: assignment is likely at expiration
      EARNINGS     - earnings land before expiration
      SELL CALL    - you own 100+ shares with no call sold against them (wheel step 2)
  - Portfolio totals of the Greeks:
      delta = how many $ you make/lose per $1 move in the stocks (in "shares" terms)
      theta = $ you earn per day from time decay (positive is good for option sellers)
      vega  = $ you make/lose per 1-point rise in implied volatility (negative for sellers)
      gamma = how fast delta changes as the stock moves
  - Orders still waiting to fill

Saves daily_report_YYYYMMDD.csv (one row per position) and daily_report_YYYYMMDD.txt (summary).

Before running: TWS open on your PAPER account.
Run with:  python daily_report.py
"""

import csv
import logging
import math
from datetime import date, datetime

import earnings
from place_orders import MAX_POSITIONS, MAX_TOTAL_PCT, capital_in_use

# ---------------- Settings you can change ----------------
TAKE_PROFIT_PCT = 50   # flag short options that have made this % of the max profit
CLOSE_AT_DTE = 21      # flag positions with this many days (or fewer) to expiration
# ----------------------------------------------------------

logging.getLogger("ib_async").setLevel(logging.CRITICAL)
from ib_async import IB  # noqa: E402

PAPER_PORTS = [7497, 4002]


def connect():
    ib = IB()
    for port in PAPER_PORTS:
        try:
            ib.connect("127.0.0.1", port, clientId=6, timeout=5)
            return ib
        except Exception:
            ib.disconnect()
    print("Could not connect. Run check_connection.py first.")
    raise SystemExit(1)


def clean(x):
    """Turn IBKR's 'no value' markers (None, NaN, huge numbers) into None."""
    if x is None or (isinstance(x, float) and (math.isnan(x) or abs(x) > 1e300)):
        return None
    return x


def option_greeks(ib, items):
    """Live Greeks + underlying price for every option position. Returns {conId: Greeks}."""
    contracts = []
    for item in items:
        c = item.contract
        c.exchange = "SMART"  # portfolio contracts come without a routing exchange
        contracts.append(c)
    if not contracts:
        return {}
    tickers = [ib.reqMktData(c) for c in contracts]
    # Wait until every option has Greeks (checking once a second, up to 15 seconds)
    for _ in range(15):
        ib.sleep(1)
        if all(t.modelGreeks for t in tickers):
            break
    for c in contracts:
        ib.cancelMktData(c)
    return {t.contract.conId: t.modelGreeks for t in tickers if t.modelGreeks}


def actions_for(row, has_short_call):
    """Suggested actions for one position (plain-English flags, nothing is automated)."""
    flags = []
    if row["type"] == "stock":
        if row["quantity"] >= 100 and not has_short_call:
            flags.append("SELL CALL: own 100+ shares, run pick_trades.py for a covered call")
        return flags
    if row["quantity"] < 0 and row["profit_%_of_max"] is not None and row["profit_%_of_max"] >= TAKE_PROFIT_PCT:
        flags.append(f"TAKE PROFIT: made {row['profit_%_of_max']:.0f}% of max - consider buying back")
    if row["dte"] <= CLOSE_AT_DTE:
        flags.append(f"{CLOSE_AT_DTE} DTE: {row['dte']} days left - close or roll")
    und = row["underlying_price"]
    if und:
        itm = (row["right"] == "P" and und < row["strike"]) or (row["right"] == "C" and und > row["strike"])
        if itm and row["quantity"] < 0:
            flags.append("ITM: likely assignment at expiration")
    if row["earnings_before_expiry"]:
        flags.append(f"EARNINGS on {row['next_earnings']} before expiration")
    return flags


def main():
    ib = connect()
    ib.reqMarketDataType(4)  # live data if subscribed, otherwise delayed
    account = ib.managedAccounts()[0]

    # ---- Account numbers ----
    summary = {r.tag: r.value for r in ib.accountSummary(account)}
    net_liq = float(summary["NetLiquidation"])
    pnl = ib.reqPnL(account)  # IBKR streams today's P&L
    ib.sleep(2)
    daily_pnl, unrealized, realized = clean(pnl.dailyPnL), clean(pnl.unrealizedPnL), clean(pnl.realizedPnL)
    ib.cancelPnL(account)
    used, busy = capital_in_use(ib, account)

    # ---- Positions ----
    items = [p for p in ib.portfolio(account) if p.position != 0]
    option_items = [p for p in items if p.contract.secType == "OPT"]
    greeks = option_greeks(ib, option_items)
    symbols = sorted({p.contract.symbol for p in items})
    earnings_dates, _ = earnings.next_earnings(symbols) if symbols else ({}, [])

    rows = []
    for p in items:
        c = p.contract
        is_option = c.secType == "OPT"
        g = greeks.get(c.conId)
        size = p.position * (100 if is_option else 1)  # shares controlled (negative = short)
        exp = c.lastTradeDateOrContractMonth if is_option else ""
        dte = (datetime.strptime(exp, "%Y%m%d").date() - date.today()).days if exp else ""
        earn = earnings_dates.get(c.symbol)

        # For options we SOLD: max profit = the premium we collected
        credit = -p.averageCost * p.position if is_option and p.position < 0 else None
        profit_pct = 100 * p.unrealizedPNL / credit if credit else None

        delta = clean(g.delta) if g else None
        rows.append({
            "symbol": c.symbol,
            "type": ("short " if p.position < 0 else "long ") + ("put" if c.right == "P" else "call") if is_option else "stock",
            "right": c.right if is_option else "",
            "strike": c.strike if is_option else "",
            "expiration": exp,
            "dte": dte,
            "quantity": p.position,
            "avg_cost": round(p.averageCost / (100 if is_option else 1), 2),   # per share
            "market_price": round(p.marketPrice, 2),
            "market_value": round(p.marketValue, 2),
            "unrealized_pnl": round(p.unrealizedPNL, 2),
            "profit_%_of_max": round(profit_pct, 1) if profit_pct is not None else None,
            "underlying_price": round(clean(g.undPrice), 2) if g and clean(g.undPrice) else None,
            # Position Greeks = per-share Greek x number of shares controlled
            "delta": round(delta * size, 1) if delta is not None else (p.position if not is_option else None),
            "gamma": round(clean(g.gamma) * size, 2) if g and clean(g.gamma) is not None else (0 if not is_option else None),
            "theta_$/day": round(clean(g.theta) * size, 2) if g and clean(g.theta) is not None else (0 if not is_option else None),
            "vega_$": round(clean(g.vega) * size, 2) if g and clean(g.vega) is not None else (0 if not is_option else None),
            "iv_%": round(clean(g.impliedVol) * 100, 1) if g and clean(g.impliedVol) else None,
            "next_earnings": earn or "",
            "earnings_before_expiry": "YES" if earn and exp and earn.strftime("%Y%m%d") <= exp else "",
        })

    short_calls = {r["symbol"] for r in rows if r["type"] == "short call"}
    for r in rows:
        r["actions"] = " | ".join(actions_for(r, r["symbol"] in short_calls))
    rows.sort(key=lambda r: (r["symbol"], r["expiration"]))

    open_trades = ib.reqAllOpenOrders()
    ib.disconnect()

    # ---- Build the text summary ----
    def money(x):
        return f"${x:,.2f}" if x is not None else "n/a"

    def total(key):
        values = [r[key] for r in rows if r[key] is not None]
        return sum(values) if values else 0

    lines = [
        f"DAILY REPORT  {datetime.now():%Y-%m-%d %H:%M}   account {account}",
        "=" * 72,
        f"Account value:      {money(net_liq)}",
        f"Today's P&L:        {money(daily_pnl)}",
        f"Unrealized P&L:     {money(unrealized)}",
        f"Realized P&L:       {money(realized)}",
        f"Capital in use:     {money(used)} = {100 * used / net_liq:.1f}% of account (limit {MAX_TOTAL_PCT}%)",
        f"Stocks in use:      {len(busy)} of {MAX_POSITIONS} ({', '.join(sorted(busy)) or 'none'})",
        "",
        f"POSITIONS ({len(rows)})",
    ]
    if rows:
        lines.append(f"{'symbol':<7}{'type':<12}{'strike':>8}{'exp':>10}{'dte':>5}{'qty':>6}"
                     f"{'P&L':>10}{'%max':>7}{'delta':>8}{'theta':>8}{'vega':>8}")
        for r in rows:
            lines.append(
                f"{r['symbol']:<7}{r['type']:<12}{str(r['strike']):>8}{r['expiration']:>10}{str(r['dte']):>5}"
                f"{r['quantity']:>6.0f}{r['unrealized_pnl']:>10,.0f}"
                f"{(str(r['profit_%_of_max']) if r['profit_%_of_max'] is not None else ''):>7}"
                f"{(str(r['delta']) if r['delta'] is not None else ''):>8}"
                f"{(str(r['theta_$/day']) if r['theta_$/day'] is not None else ''):>8}"
                f"{(str(r['vega_$']) if r['vega_$'] is not None else ''):>8}")
            if r["actions"]:
                lines.append(f"       -> {r['actions']}")
        lines += [
            "",
            "PORTFOLIO GREEKS",
            f"Delta: {total('delta'):+,.1f} shares   (like owning that many shares across your stocks)",
            f"Theta: {total('theta_$/day'):+,.2f} $/day (time decay: what you earn per day if nothing moves)",
            f"Vega:  {total('vega_$'):+,.2f} $ per 1-point rise in implied volatility",
            f"Gamma: {total('gamma'):+,.2f}",
        ]
    else:
        lines.append("  No positions yet.")

    lines += ["", f"ORDERS WAITING TO FILL ({len(open_trades)})"]
    for t in open_trades:
        lines.append(f"  #{t.order.orderId} {t.order.action} {t.order.totalQuantity:.0f} {t.contract.localSymbol.strip() or t.contract.symbol} "
                     f"@ {t.order.lmtPrice} ({t.orderStatus.status}, filled {t.orderStatus.filled:.0f})")
    if not open_trades:
        lines.append("  none")
    report = "\n".join(lines)

    # ---- Save ----
    stamp = f"{date.today():%Y%m%d}"
    with open(f"daily_report_{stamp}.txt", "w") as f:
        f.write(report + "\n")
    if rows:
        with open(f"daily_report_{stamp}.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    print(report)
    print(f"\nSaved daily_report_{stamp}.txt" + (f" and daily_report_{stamp}.csv" if rows else ""))


if __name__ == "__main__":
    main()
