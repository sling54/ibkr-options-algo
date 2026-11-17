"""
Step 1: Check that Python can talk to your IBKR paper account.

Before running: TWS or IB Gateway must be open and logged in with your PAPER account.
Run with:  python check_connection.py
"""

from ib_async import IB

# Same API for paper and live. The only difference is which port you connect to.
PORTS = {
    7497: "TWS - PAPER",
    4002: "IB Gateway - PAPER",
    7496: "TWS - LIVE",
    4001: "IB Gateway - LIVE",
}

ib = IB()
connected_label = None

for port, label in PORTS.items():
    try:
        ib.connect("127.0.0.1", port, clientId=1, timeout=5)
        connected_label = label
        break
    except Exception:
        ib.disconnect()

if connected_label is None:
    print("\nCOULD NOT CONNECT.")
    print("1. Is TWS (or IB Gateway) open and logged in?")
    print("2. In TWS: Global Configuration > API > Settings > tick 'Enable ActiveX and Socket Clients'.")
    print("3. Check the 'Socket port' there is 7497 (TWS paper) or 4002 (Gateway paper).")
    raise SystemExit(1)

accounts = ib.managedAccounts()
print(f"\nCONNECTED to {connected_label}")
print(f"Account(s): {accounts}")
print(f"IBKR server time: {ib.reqCurrentTime()}")

if "LIVE" in connected_label or not any(a.startswith("DU") for a in accounts):
    print("\nWARNING: this looks like a LIVE account (paper accounts start with 'DU').")
    print("Log out and log back in to TWS choosing 'Paper Trading' before going further.")
else:
    print("Paper account confirmed (starts with 'DU').")

summary = {row.tag: row.value for row in ib.accountSummary()}
print(f"Net liquidation value: ${float(summary.get('NetLiquidation', 0)):,.2f}")
print(f"Buying power:          ${float(summary.get('BuyingPower', 0)):,.2f}")

ib.disconnect()
print("\nAPI is set up correctly.")
