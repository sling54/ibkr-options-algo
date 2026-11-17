# IBKR Options Premium-Selling Algo

An automated options trading pipeline for an **Interactive Brokers paper account**, written in plain,
heavily commented Python with [`ib_async`](https://github.com/ib-api-reloaded/ib_async).

It finds stocks whose options are expensive, builds premium-selling trades (the wheel,
strangles, straddles), checks them against risk limits, places paper orders, and produces a
daily Greeks & P&L report.

> **Paper trading only.** The order script refuses to run on a live account. This is an
> educational project, not financial advice. Selling options can lose far more than the premium
> collected; short strangles and straddles have unlimited risk.

## How it works

```
volatility_screener.py   Top 50 most-active optionable stocks -> IV rank, IV vs realized vol
        |                (vol_screen_*.csv)
        v
pick_trades.py           Option chains for the candidates -> concrete trade ideas
        |                (proposed_trades_*.csv)      uses earnings.py
        v
place_orders.py          Risk checks -> limit orders on the paper account
        |                (orders_log.csv)             dry run unless --live
        v
daily_report.py          Positions, Greeks, P&L, suggested actions
                         (daily_report_*.txt / .csv)
```

| Script | What it does |
|---|---|
| `check_connection.py` | Confirms Python can reach TWS and that you're on a paper account (`DU...`). |
| `pull_option_chain.py` | Pulls one stock's 20–50 day option chain with Greeks to a CSV. `python pull_option_chain.py AAPL` |
| `volatility_screener.py` | IBKR scanner → 50 stocks with the most option volume. For each: 1 year of daily implied volatility, **IV rank**, **IV percentile**, 20-day **realized volatility**, and the **IV − HV gap**. Flags candidates with IV rank ≥ 50 and IV > HV. |
| `earnings.py` | Next earnings dates from Nasdaq's free public calendar (cached daily). `python earnings.py NVDA AAPL` |
| `pick_trades.py` | For each candidate, picks an expiration 30–45 days out and builds: **cash-secured put** (~0.30Δ), **short strangle** (~0.16Δ each side), **short straddle** (at-the-money) and, for stocks you own 100+ shares of, a **covered call** (~0.30Δ). Margin comes from IBKR's own what-if calculation. Skips stocks with earnings within 7 days. |
| `place_orders.py` | Applies the risk limits below with fresh prices, then places limit orders at the mid price (DAY orders). One trade per stock, wheel strategies tried first. |
| `daily_report.py` | Account value and P&L, capital used vs limits, every position with P&L and Greeks, portfolio Greek totals, waiting orders, and flags: TAKE PROFIT (≥ 50% of max), 21 DTE, ITM, EARNINGS, SELL CALL. |
| `run_daily.sh` | Runs everything above in order. |

## Risk limits (`place_orders.py`)

All are settings at the top of the file.

| Rule | Default |
|---|---|
| Account type | Paper only (account must start with `DU`, paper ports only) |
| Max capital per trade | 10% of account value |
| Max capital across all trades | 50% of account value (includes margin, cash-secured put collateral and pending orders) |
| Max stocks with positions/orders | 5 (one trade per stock) |
| Earnings | None within 7 days, and none before the option's expiration |
| Strangles / straddles | Only on ETFs or companies with market cap ≥ $10B |
| Bid-ask spread | Every leg ≤ 15% of mid, re-checked at order time |
| Stale data | Only trades from a proposals file created today |
| Order type | Limit at mid price, DAY (unfilled orders expire at the close) |

## Setup

1. **TWS**: log in with your **paper** account. In *Global Configuration → API → Settings*:
   tick *Enable ActiveX and Socket Clients*, socket port **7497**, and untick *Read-Only API*
   (needed to place orders).
2. **Python** (3.10+):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   python check_connection.py
   ```
3. **Market data**: without an options data subscription IBKR sends free 15-minute delayed
   quotes, which is fine for paper trading.

## Daily use

```bash
./run_daily.sh            # screen + pick + dry-run orders + report
./run_daily.sh --live     # same, but sends the paper orders
```

Or step by step:

```bash
python volatility_screener.py     # ~1-4 min
python pick_trades.py             # ~1 min
python place_orders.py            # dry run: shows what would be ordered and why others were rejected
python place_orders.py --live     # sends the orders
python daily_report.py            # any time, e.g. after the close
```

Run it during market hours (9:30–16:00 ET) so quotes are live and orders can fill.
To cancel an order: TWS → Activity → right-click the order → Cancel.

## Design notes

Choices made after testing against real TWS data:

- **Realized volatility is computed from closing prices**, not taken from IBKR's
  `HISTORICAL_VOLATILITY` series. In testing that series jumped erratically (e.g. NVDA 47% → 64%
  in one day with no matching price move) and didn't match actual price moves.
- **Margin comes from IBKR's what-if orders**, not a formula. IBKR's house margin on volatile
  stocks was 3–7× the textbook Reg-T figure, which would otherwise make strangles and straddles
  look far more attractive than they are.
- **Strikes are fetched per expiration.** IBKR's chain lists every strike across all
  expirations, so requesting them blindly targets strikes that don't exist.
- **A leg is skipped if no strike is within 0.08 of the target delta**, e.g. when a stock has no
  strikes listed far enough out of the money.
- **Earnings dates come from Nasdaq** because IBKR requires a paid subscription for them. On the
  first test run this caught NVDA reporting the same day and BABA two days later, both of which
  looked like top candidates because of their high IV.

## Limitations

- One contract per trade (no position sizing up to the limits yet).
- Exits are flagged in the report but not automated: you close or roll positions yourself.
- The Nasdaq calendar is an unofficial endpoint and could change; the scripts warn if it can't
  be reached.
- Delayed data can occasionally leave a Greek blank in the report.

## Project layout

```
check_connection.py      step 1  connection test
pull_option_chain.py     step 2  single-stock option chain
volatility_screener.py   step 3  IV rank screener
pick_trades.py           step 4  trade ideas
place_orders.py          step 5  risk checks + orders
daily_report.py          step 6  Greeks & P&L report
earnings.py              helper  earnings calendar
run_daily.sh             runs steps 1, 3-6
```
