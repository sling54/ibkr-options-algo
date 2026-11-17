#!/bin/bash
# Runs the whole pipeline once, in order. Orders are a DRY RUN unless you pass --live:
#   ./run_daily.sh          # screen, pick trades, show what WOULD be ordered, report
#   ./run_daily.sh --live   # same, but actually sends the orders to your PAPER account
set -e                      # stop at the first step that fails
cd "$(dirname "$0")"
PY=.venv/bin/python

$PY check_connection.py
$PY volatility_screener.py
$PY pick_trades.py
$PY place_orders.py "$@"
$PY daily_report.py
