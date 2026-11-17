"""
Step 4: Pick specific option trades for the stocks the screener liked.

What it does:
  1. Reads the newest vol_screen_*.csv and keeps the rows marked sell_premium_candidate = YES.
  2. For each candidate, finds an expiration 30-45 days out and gets option prices + Greeks.
  3. Builds these trade ideas (all of them SELL options to collect premium):
       - Cash-secured put (wheel step 1): sell a ~0.30 delta put. If the stock falls below the
         strike you buy 100 shares per contract at the strike price.
       - Short strangle: sell a ~0.16 delta put AND a ~0.16 delta call. Profits if the stock
         stays between the two strikes. Losses are UNLIMITED if it moves a lot.
       - Short straddle: sell the at-the-money put AND call. More premium, more risk.
       - Covered call (wheel step 2): for any stock you already own 100+ shares of,
         sell a ~0.30 delta call against those shares.
  Stocks reporting earnings within 7 days are skipped (see earnings.py), and every idea
  shows the next earnings date so you can see if earnings fall before expiration.
  4. Saves everything to proposed_trades_YYYYMMDD_HHMM.csv. NOTHING is ordered here -
     place_orders.py (next step) checks risk limits and places the orders.

Delta in plain English: roughly the chance an option ends up "in the money".
A 0.30 delta put has about a 30% chance of being assigned; 0.16 is about 1 standard deviation away.

Before running: run volatility_screener.py first, and have TWS open on your PAPER account.
Run with:  python pick_trades.py
Takes about 1-3 minutes.
"""

import csv
import glob
import logging
import math
from datetime import date, datetime

from ib_async import IB, ComboLeg, Contract, LimitOrder, Option, Stock

import earnings  # our earnings.py helper (free Nasdaq earnings calendar)

# ---------------- Settings you can change ----------------
MIN_DAYS, MAX_DAYS = 30, 45   # expiration window (days to expiration)
TARGET_DAYS = 38              # pick the expiration closest to this
PUT_DELTA = 0.30              # cash-secured put
CALL_DELTA = 0.30             # covered call
STRANGLE_DELTA = 0.16         # each side of the strangle
DELTA_TOLERANCE = 0.08        # skip a leg if no strike is within this of the target delta
EARNINGS_BUFFER_DAYS = 7      # don't SELL premium on a stock reporting earnings within this many days
MAX_STRIKES_PER_SIDE = 20     # how many strikes to price on each side of the stock price
# ----------------------------------------------------------

logging.getLogger("ib_async").setLevel(logging.CRITICAL)
PAPER_PORTS = [7497, 4002]
BATCH_SIZE = 40   # IBKR limits how many quotes stream at once
WAIT_SECONDS = 6  # time for bid/ask/Greeks to arrive


def connect():
    ib = IB()
    for port in PAPER_PORTS:
        try:
            ib.connect("127.0.0.1", port, clientId=4, timeout=5)
            return ib
        except Exception:
            ib.disconnect()
    print("Could not connect. Run check_connection.py first.")
    raise SystemExit(1)


def is_number(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x)) and x > 0


def read_candidates():
    """Load the newest screener CSV. Returns (filename, list of candidate rows)."""
    files = sorted(glob.glob("vol_screen_*.csv"))
    if not files:
        print("No vol_screen_*.csv found. Run volatility_screener.py first.")
        raise SystemExit(1)
    with open(files[-1]) as f:
        rows = list(csv.DictReader(f))
    return files[-1], [r for r in rows if r["sell_premium_candidate"] == "YES"]


def days_until(expiration):
    return (datetime.strptime(expiration, "%Y%m%d").date() - date.today()).days


def pick_expiration(expirations):
    """Expiration closest to TARGET_DAYS, preferring ones inside MIN_DAYS..MAX_DAYS."""
    inside = [e for e in expirations if MIN_DAYS <= days_until(e) <= MAX_DAYS]
    pool = inside or [e for e in expirations if days_until(e) >= 7]
    return min(pool, key=lambda e: abs(days_until(e) - TARGET_DAYS)) if pool else None


def thin_out(strikes, keep):
    """If there are too many strikes, keep every Nth one so we price at most `keep`."""
    if len(strikes) <= keep:
        return strikes
    step = math.ceil(len(strikes) / keep)
    return strikes[::step]


def get_quotes(ib, contracts):
    """Get bid/ask/delta for many option contracts, in batches. Returns list of dicts."""
    quotes = []
    for i in range(0, len(contracts), BATCH_SIZE):
        batch = contracts[i:i + BATCH_SIZE]
        tickers = [ib.reqMktData(c) for c in batch]
        ib.sleep(WAIT_SECONDS)
        for c in batch:
            ib.cancelMktData(c)
        for t in tickers:
            g = t.modelGreeks
            # Only keep options with a real two-sided market and a delta
            if is_number(t.bid) and is_number(t.ask) and g and g.delta is not None:
                quotes.append({
                    "contract": t.contract,
                    "right": t.contract.right,
                    "strike": t.contract.strike,
                    "bid": t.bid,
                    "ask": t.ask,
                    "mid": (t.bid + t.ask) / 2,
                    "delta": g.delta,
                    "spread_pct": 100 * (t.ask - t.bid) / ((t.bid + t.ask) / 2),
                })
    return quotes


def closest(quotes, right, target_abs_delta):
    """
    The put ("P") or call ("C") whose delta is closest to the target (ignoring the sign).
    Returns None if even the closest one is too far off (e.g. no strikes listed that low).
    """
    options = [q for q in quotes if q["right"] == right]
    if not options:
        return None
    best = min(options, key=lambda q: abs(abs(q["delta"]) - target_abs_delta))
    return best if abs(abs(best["delta"]) - target_abs_delta) <= DELTA_TOLERANCE else None


def ibkr_margin(ib, symbol, put=None, call=None):
    """
    Ask IBKR how much margin selling this trade would use, WITHOUT placing an order
    (a "what-if" order). This uses IBKR's real house rules, which for volatile stocks
    can be several times higher than the textbook Reg-T formula.
    Returns dollars, or None if IBKR didn't answer.
    """
    legs = [q for q in (put, call) if q]
    limit = round(sum(q["mid"] for q in legs), 2)
    if len(legs) == 1:
        # One option: a plain SELL order
        contract = legs[0]["contract"]
        order = LimitOrder("SELL", 1, limit, tif="DAY", account=ib.managedAccounts()[0])
    else:
        # Two options in one order (a "combo"). IBKR's convention for a combo where you
        # SELL both legs: BUY the combo at a NEGATIVE price (= collecting a credit).
        contract = Contract(symbol=symbol, secType="BAG", currency="USD", exchange="SMART",
                            comboLegs=[ComboLeg(conId=q["contract"].conId, ratio=1,
                                                action="SELL", exchange="SMART") for q in legs])
        order = LimitOrder("BUY", 1, -limit, tif="DAY", account=ib.managedAccounts()[0])
    state = ib.whatIfOrder(contract, order)
    try:
        return float(state.initMarginChange)
    except (AttributeError, TypeError, ValueError):
        return None


def make_row(symbol, strategy, info, put=None, call=None, capital=0.0, contracts=1, breakeven_cost=None):
    """Put one trade idea into a single CSV row."""
    price, dte = info["price"], info["dte"]
    credit = 100 * ((put["mid"] if put else 0) + (call["mid"] if call else 0)) * contracts
    legs = [q for q in (put, call) if q]

    # Breakevens: where the trade starts losing money at expiration
    per_share = credit / (100 * contracts)
    low = round(put["strike"] - per_share, 2) if put else ""
    high = round(call["strike"] + per_share, 2) if call else ""
    if breakeven_cost is not None:  # covered call: you're protected down to cost - premium
        low, high = round(breakeven_cost - per_share, 2), ""

    return {
        "symbol": symbol,
        "strategy": strategy,
        "is_etf": info["is_etf"],
        "iv_rank": info["iv_rank"],
        "stock_price": round(price, 2),
        "expiration": info["expiration"],
        "dte": dte,
        "next_earnings": info["earnings"] or "",
        # YES = earnings happen while this trade is open (a big overnight move is possible)
        "earnings_before_expiry": "YES" if info["earnings"] and
            info["earnings"].strftime("%Y%m%d") <= info["expiration"] else "",
        "contracts": contracts,
        "put_strike": put["strike"] if put else "",
        "call_strike": call["strike"] if call else "",
        "put_delta": round(put["delta"], 3) if put else "",
        "call_delta": round(call["delta"], 3) if call else "",
        "put_mid": round(put["mid"], 2) if put else "",
        "call_mid": round(call["mid"], 2) if call else "",
        "credit_$": round(credit, 2),
        "capital_or_margin_$": round(capital, 2),
        "return_%": round(100 * credit / capital, 2) if capital else "",
        "annualized_%": round(100 * credit / capital * 365 / dte, 1) if capital else "",
        "breakeven_low": low,
        "breakeven_high": high,
        # Rough chance every leg expires worthless (you keep all the premium)
        "chance_all_otm_%": round(max(0, 100 * (1 - sum(abs(q["delta"]) for q in legs))), 1),
        "worst_spread_%": round(max(q["spread_pct"] for q in legs), 1),
        "put_conid": put["contract"].conId if put else "",
        "call_conid": call["contract"].conId if call else "",
    }


def analyze_symbol(ib, symbol, iv_rank, shares_owned=0, avg_cost=None, sell_premium=True, earnings_date=None):
    """Get the option chain for one stock and build its trade ideas. Returns a list of rows."""
    stock = Stock(symbol, "SMART", "USD")
    details = ib.reqContractDetails(stock)
    if not details:
        print(f"  {symbol}: not found, skipped")
        return []
    stock = details[0].contract
    is_etf = "YES" if details[0].stockType == "ETF" else ""

    # Current stock price
    ticker = ib.reqTickers(stock)[0]
    price = ticker.marketPrice()
    if not is_number(price):
        price = ticker.close
    if not is_number(price):
        print(f"  {symbol}: no stock price, skipped")
        return []

    # Which expirations and strikes exist
    chains = ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
    smart = [c for c in chains if c.exchange == "SMART"]
    chain = next((c for c in smart if c.tradingClass == symbol), smart[0] if smart else None)
    expiration = pick_expiration(sorted(chain.expirations)) if chain else None
    if not expiration:
        print(f"  {symbol}: no usable expiration, skipped")
        return []
    dte = days_until(expiration)

    # Price strikes out to ~2 "expected moves" each side (covers the 0.16 delta easily).
    # Expected move = stock price x IV x sqrt(years to expiration)
    iv = (ticker.impliedVolatility if is_number(ticker.impliedVolatility) else 0.5)
    move = price * iv * math.sqrt(dte / 365)
    # chain.strikes mixes strikes from ALL expirations, so ask IBKR which strikes
    # actually exist for the one expiration we picked.
    listed = ib.reqContractDetails(Option(symbol, expiration, right="C", exchange="SMART",
                                          tradingClass=chain.tradingClass))
    all_strikes = sorted({d.contract.strike for d in listed}) or sorted(chain.strikes)
    atm = min(all_strikes, key=lambda s: abs(s - price))  # at-the-money strike
    below = thin_out([s for s in all_strikes if price - 2 * move <= s < atm], MAX_STRIKES_PER_SIDE)
    above = thin_out([s for s in all_strikes if atm < s <= price + 2 * move], MAX_STRIKES_PER_SIDE)

    # Out-of-the-money puts below the price, calls above, and both at the ATM strike
    wanted = [Option(symbol, expiration, s, "P", "SMART", tradingClass=chain.tradingClass) for s in below + [atm]]
    wanted += [Option(symbol, expiration, s, "C", "SMART", tradingClass=chain.tradingClass) for s in [atm] + above]
    contracts = [c for c in ib.qualifyContracts(*wanted) if c and c.conId]
    quotes = get_quotes(ib, contracts)
    print(f"  {symbol:<6} ${price:<8.2f} exp {expiration} ({dte}d)  {len(quotes)}/{len(contracts)} options priced")
    if not quotes:
        return []

    info = {"price": price, "dte": dte, "expiration": expiration, "is_etf": is_etf,
            "iv_rank": iv_rank, "earnings": earnings_date}
    rows = []

    if sell_premium:
        # Cash-secured put: you must hold strike x 100 in cash in case you're assigned
        put = closest(quotes, "P", PUT_DELTA)
        if put:
            rows.append(make_row(symbol, "cash_secured_put", info, put=put, capital=100 * put["strike"]))

        # Strangle: ~0.16 delta on each side. Margin comes from IBKR (see ibkr_margin)
        sp, sc = closest(quotes, "P", STRANGLE_DELTA), closest(quotes, "C", STRANGLE_DELTA)
        margin = ibkr_margin(ib, symbol, sp, sc) if sp and sc and sp["strike"] < sc["strike"] else None
        if margin:
            rows.append(make_row(symbol, "short_strangle", info, put=sp, call=sc, capital=margin))
        else:
            print(f"         {symbol}: no strangle (no strikes near {STRANGLE_DELTA} delta, or no margin answer)")

        # Straddle: both legs at the at-the-money strike
        ap = next((q for q in quotes if q["right"] == "P" and q["strike"] == atm), None)
        ac = next((q for q in quotes if q["right"] == "C" and q["strike"] == atm), None)
        margin = ibkr_margin(ib, symbol, ap, ac) if ap and ac else None
        if margin:
            rows.append(make_row(symbol, "short_straddle", info, put=ap, call=ac, capital=margin))

    # Covered call: only if you own at least 100 shares (1 contract per 100 shares)
    if shares_owned >= 100:
        call = closest(quotes, "C", CALL_DELTA)
        if call:
            n = int(shares_owned // 100)
            rows.append(make_row(symbol, "covered_call", info, call=call, contracts=n,
                                 capital=n * 100 * price, breakeven_cost=avg_cost))
    return rows


def main():
    screen_file, candidates = read_candidates()
    print(f"\nUsing {screen_file}: {len(candidates)} candidates "
          f"({', '.join(r['symbol'] for r in candidates) or 'none'})")

    ib = connect()
    ib.reqMarketDataType(4)  # live data if subscribed, otherwise free delayed data

    # Stocks you already own 100+ shares of -> covered call ideas (wheel step 2)
    owned = {p.contract.symbol: p for p in ib.positions()
             if p.contract.secType == "STK" and p.position >= 100}
    if owned:
        print(f"You own 100+ shares of: {', '.join(owned)} (covered calls will be added)")
    else:
        print("No stock positions of 100+ shares, so no covered calls yet.")

    # Analyze screener candidates plus any stock you own (even if not a candidate today)
    to_check = {r["symbol"]: r["iv_rank"] for r in candidates}
    for sym in owned:
        to_check.setdefault(sym, "")

    # Earnings check: skip selling premium on anything reporting in the next few days.
    # (Covered calls are still allowed: you already own the shares, and the call doesn't
    # add downside risk - it just caps your upside if the stock jumps.)
    earnings_dates, failed_days = earnings.next_earnings(list(to_check))
    if failed_days:
        print(f"WARNING: couldn't download earnings for {', '.join(failed_days)} - check those manually.")
    sellable = set()
    for r in candidates:
        d = earnings_dates.get(r["symbol"])
        if d and (d - date.today()).days <= EARNINGS_BUFFER_DAYS:
            print(f"  {r['symbol']}: SKIPPED - earnings on {d} ({(d - date.today()).days} days away)")
        else:
            sellable.add(r["symbol"])

    print("\nPricing options (about 10-20 sec per stock)...")
    rows = []
    for symbol, iv_rank in to_check.items():
        if symbol not in sellable and symbol not in owned:
            continue  # skipped for earnings, and no shares to write covered calls on
        pos = owned.get(symbol)
        rows += analyze_symbol(
            ib, symbol, iv_rank,
            shares_owned=pos.position if pos else 0,
            avg_cost=pos.avgCost if pos else None,
            sell_premium=symbol in sellable,
            earnings_date=earnings_dates.get(symbol),
        )
    ib.disconnect()

    if not rows:
        print("No trade ideas built (no candidates, or no option prices came back).")
        raise SystemExit(1)

    # Best annualized return first within each strategy
    rows.sort(key=lambda r: (r["strategy"], -(r["annualized_%"] or 0)))

    filename = f"proposed_trades_{datetime.now():%Y%m%d_%H%M}.csv"
    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{'symbol':<7}{'strategy':<18}{'exp':<10}{'strikes':>15}{'credit$':>9}"
          f"{'capital$':>11}{'ann%':>7}{'otm%':>6}")
    for r in rows:
        strikes = "/".join(str(s) for s in (r["put_strike"], r["call_strike"]) if s != "")
        print(f"{r['symbol']:<7}{r['strategy']:<18}{r['expiration']:<10}{strikes:>15}"
              f"{r['credit_$']:>9}{r['capital_or_margin_$']:>11}{r['annualized_%']:>7}{r['chance_all_otm_%']:>6}")
    print(f"\nSaved {len(rows)} trade ideas to {filename}")
    print("Nothing has been ordered. Next: place_orders.py checks risk limits and places them.")


# Only run main() when this file is run directly (lets other scripts reuse the functions above)
if __name__ == "__main__":
    main()
