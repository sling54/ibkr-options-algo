# IBKR Options Premium-Selling Algo

[![CI](https://github.com/sling54/ibkr-options-algo/actions/workflows/ci.yml/badge.svg)](https://github.com/sling54/ibkr-options-algo/actions/workflows/ci.yml)

An automated options pipeline for an **Interactive Brokers paper account**, built on
[`ib_async`](https://github.com/ib-api-reloaded/ib_async).

It finds stocks whose options are expensive, builds premium-selling trades (the wheel,
strangles, straddles), checks them against risk limits, places paper orders, and produces a
daily Greeks & P&L report.

> **Paper trading only.** Orders are refused on anything but a paper account (`DU...`) on a
> paper port, and every order run is a dry run unless you pass `--live`. This is an
> educational project, not financial advice. Selling options can lose far more than the
> premium collected; short strangles and straddles have unlimited risk.

## How it works

```
ibkr-algo screen    Top 50 most-active optionable stocks -> IV rank, IV vs realized vol
        |           output/vol_screen_*.csv
        v
ibkr-algo pick      Option chains for the candidates -> concrete trade ideas
        |           output/proposed_trades_*.csv            (earnings from Nasdaq)
        v
ibkr-algo orders    Risk checks with fresh prices -> limit orders on the paper account
        |           output/orders_log.csv                   dry run unless --live
        v
ibkr-algo report    Positions, Greeks, P&L, suggested actions
                    output/daily_report_*.txt / .csv
```

- **screen**: IBKR scanner for the 50 stocks with the most option volume. For each: 1 year of
  daily implied volatility, **IV rank**, **IV percentile**, 20-day **realized volatility** and the
  **IV − HV gap**. Candidates have IV rank ≥ 50 and IV > HV.
- **pick**: for each candidate, an expiration 30–45 days out and a **cash-secured put** (~0.30Δ),
  **short strangle** (~0.16Δ each side), **short straddle** (ATM) and, for stocks you own 100+
  shares of, a **covered call** (~0.30Δ). Margin comes from IBKR's what-if calculation. Stocks
  with earnings within 7 days are skipped.
- **orders**: re-prices every leg, applies the risk limits below, and places limit orders at the
  mid (DAY orders). One trade per stock, wheel strategies first.
- **report**: account value and P&L, capital used vs limits, every position with P&L and Greeks,
  portfolio Greek totals, waiting orders, and flags: TAKE PROFIT (≥ 50% of max), 21 DTE, ITM,
  EARNINGS, SELL CALL.

## Install

Requires Python 3.10+ and TWS or IB Gateway.

1. **TWS**: log in with your **paper** account. In *Global Configuration → API → Settings*:
   tick *Enable ActiveX and Socket Clients*, socket port **7497**, and untick *Read-Only API*
   (needed to place orders).
2. **Python**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"     # or `pip install -e .` without the dev tools
   ibkr-algo check
   ```
3. **Market data**: without an options data subscription IBKR sends free 15-minute delayed
   quotes, which is fine for paper trading.

## Usage

Run from the repo folder (it reads `config.toml` there; pass `--config PATH` otherwise).

| Command | What it does |
|---|---|
| `ibkr-algo check` | Confirms Python can reach TWS and that you're on a paper account. |
| `ibkr-algo screen` | Volatility screener (~1–4 min). |
| `ibkr-algo pick` | Trade ideas for today's candidates (~1–3 min). Nothing is ordered. |
| `ibkr-algo orders` | **Dry run**: shows what would be ordered and why other ideas were rejected. |
| `ibkr-algo orders --live` | Same checks, then sends the orders to the paper account. |
| `ibkr-algo report` | Daily Greeks & P&L report. Read-only. |
| `ibkr-algo run-all [--live]` | `check`, `screen`, `pick`, `orders`, `report` in order; stops at the first failure. |
| `ibkr-algo chain AAPL` | Dumps one stock's 20–50 day option chain with Greeks to CSV. |
| `ibkr-algo earnings NVDA AAPL` | Next earnings dates from Nasdaq. |

Global options: `-v/--verbose` (debug logging with timestamps, including `ib_async`'s own
messages) and `--config PATH`. `python -m ibkr_options_algo ...` works too.

Run during market hours (9:30–16:00 ET) so quotes are live and orders can fill.
To cancel an order: TWS → Activity → right-click the order → Cancel.

## Risk limits

All live in `config.toml` (see [Configuration](#configuration)).

| Rule | Default |
|---|---|
| Account type | Paper only: account must start with `DU`, paper ports only |
| Max capital per trade | 10% of account value |
| Max capital across all trades | 50% of account value (margin + cash-secured put collateral + pending orders) |
| Max stocks with positions/orders | 5 (one trade per stock) |
| Earnings | None within 7 days, and none before the option's expiration |
| Strangles / straddles | Only on ETFs or companies with market cap ≥ $10B |
| Bid-ask spread | Every leg ≤ 15% of mid, re-checked at order time |
| Stale data | Only trades from a proposals file created today |
| Order type | Limit at mid price, DAY (unfilled orders expire at the close) |

### Paper-only safeguards

These are code, not configuration:

- Trading commands connect only to `ibkr.paper_ports`; the config loader rejects the live ports
  7496 and 4001. (`check` also probes the live ports, read-only, purely to warn you.)
- `orders` stops before doing anything if the account doesn't start with `DU`.
- The IBKR adapter's `place_order` independently refuses a non-`DU` account or a non-paper port.
- `orders` and `run-all` are dry runs unless you pass `--live`.

## Architecture

```
                         ┌──────────────────────────────┐
                         │  cli.py        ibkr-algo ... │  argparse, logging, exit codes
                         └──────────────┬───────────────┘
                                        │
                         ┌──────────────▼───────────────┐
                         │  services/                   │  one module per command:
                         │  screen pick orders report   │  fetch -> pure logic -> save
                         │  chain check account         │
                         └──┬──────────┬─────────────┬──┘
              I/O           │          │             │          I/O
     ┌──────────────────────▼──┐  ┌────▼──────────┐  ┌▼──────────────────────────┐
     │ ibkr/                   │  │ storage.py    │  │ sources/ + earnings.py    │
     │ client.py  connection.py│  │ output/ files │  │ Nasdaq earnings, mkt cap  │
     │ (only ib_async importer)│  │               │  │ HTTP retries + timeouts   │
     └─────────────────────────┘  └───────────────┘  └───────────────────────────┘
                                        │
             pure (no IBKR, no network, no files) — unit tested directly, mypy --strict
     ┌──────────────────┬───────────────┬────────────────┬──────────────────────────┐
     │ calculations.py  │ strategy.py   │ risk.py        │ reporting.py             │
     │ IV rank, HV,     │ legs, trade   │ every risk     │ position rows, flags,    │
     │ expiry, strikes, │ ideas, order  │ check and      │ text report              │
     │ delta, ticks     │ specs         │ capital usage  │                          │
     └──────────────────┴───────────────┴────────────────┴──────────────────────────┘
                     models.py (dataclasses)   config.py (pydantic)   errors.py
```

- **Pure layers** (`calculations`, `strategy`, `risk`, `reporting`) take plain values and
  dataclasses and return new ones. They never import `ib_async`, open files or use the network.
- **`ibkr/client.py`** is the only module that imports `ib_async`. It wraps every IBKR request
  and converts IBKR objects into the dataclasses in `models.py`.
- **Services** wire the layers together, one per command. They receive a `Connector` (a function
  that opens an IBKR session), so tests can hand them a fake broker.
- **`models.py`** dataclasses replace loose dicts and CSV rows. Their `to_csv_row()` methods
  produce exactly the columns and formats the original scripts wrote.

## Configuration

Every setting is in [`config.toml`](config.toml), loaded into frozen, validated pydantic models
(`config.py`). All keys are required; unknown keys, wrong types and inconsistent values (e.g. a
live port, `min_dte > max_dte`, `max_trade_pct > max_total_pct`) stop the program with a
message naming the key. Percentages are percent (`10` = 10%); deltas and IV are decimals.

| Section | Key | Default | Meaning |
|---|---|---|---|
| `ibkr` | `host` | `127.0.0.1` | TWS / Gateway host |
| | `paper_ports` | `[7497, 4002]` | Ports to try, in order. Live ports are rejected. |
| | `connect_timeout_seconds` | `5` | Per-port connection timeout |
| | `market_data_type` | `4` | 4 = live if subscribed, else delayed |
| | `quote_batch_size` / `quote_wait_seconds` | `40` / `6` | Option quotes per batch and wait per batch |
| | `greeks_timeout_seconds` | `15` | Report: max wait for position Greeks |
| | `pnl_wait_seconds` / `order_ack_wait_seconds` | `2` / `3` | Waits for P&L stream and order acknowledgement |
| | `history_duration` / `history_timeout_seconds` | `"1 Y"` / `30` | Historical IV and price request |
| `ibkr.client_ids` | `check` … `report` | `1`…`6` | One client id per command |
| `screen` | `num_stocks` | `50` | Scanner rows (IBKR max 50) |
| | `include_etfs` | `true` | Include SPY, QQQ, GLD, ... |
| | `scan_code` / `location_code` | `OPT_VOLUME_MOST_ACTIVE` / `STK.US.MAJOR` | IBKR scanner |
| | `min_iv_rank` / `min_iv_hv_gap` | `50` / `0.0` | Candidate thresholds |
| | `hv_days` | `20` | Realized-vol window (trading days) |
| | `min_iv_history_days` | `60` | Minimum IV history for IV rank |
| `pick` | `min_dte` / `max_dte` / `target_dte` | `30` / `45` / `38` | Expiration window and target |
| | `fallback_min_dte` | `7` | Nearest allowed expiry if none is in the window |
| | `put_delta` / `call_delta` / `strangle_delta` | `0.30` / `0.30` / `0.16` | Target deltas |
| | `delta_tolerance` | `0.08` | Skip a leg if no strike is this close to the target |
| | `earnings_buffer_days` | `7` | Skip selling premium this close to earnings |
| | `max_strikes_per_side` | `20` | Strikes priced on each side of the money |
| | `strike_range_expected_moves` | `2.0` | How far out to price strikes |
| | `default_iv` | `0.5` | IV for the expected move when IBKR has none |
| `risk` | `max_trade_pct` / `max_total_pct` | `10` / `50` | Capital limits (% of net liquidation) |
| | `max_positions` | `5` | Max stocks with positions / open orders |
| | `max_spread_pct` | `15` | Max bid-ask spread per leg |
| | `large_cap_usd` | `10_000_000_000` | Market cap needed for strangles/straddles |
| | `earnings_buffer_days` / `block_earnings_before_expiry` | `7` / `true` | Earnings rules at order time |
| | `strategy_priority` | wheel first | Order strategies are tried per stock |
| `report` | `take_profit_pct` / `close_at_dte` | `50` / `21` | Report flags |
| `earnings` | `days_ahead` | `60` | Calendar window |
| | `request_delay_seconds` | `0.2` | Pause between Nasdaq requests |
| | `max_failed_days_to_cache` | `4` | Don't cache a download with more failed days |
| | `abort_after_consecutive_failures` | `3` | Stop calling a dead endpoint |
| `network` | `timeout_seconds` / `retries` / `backoff_seconds` | `15` / `2` / `1.0` | HTTP timeout and retry policy |
| | `user_agent` | browser-like | Nasdaq rejects requests without one |
| `chain` | `min_dte` / `max_dte` / `strike_range_pct` | `20` / `50` / `10` | `chain` command window |
| | `fallback_expirations` / `preview_rows` | `2` / `10` | |
| `output` | `directory` | `output` | Where every file is written |

## Testing

```bash
pytest              # ~160 tests, < 1 s, no TWS or network needed
ruff check .        # lint
ruff format .       # format
mypy                # types (strict on the pure modules)
pre-commit install  # run ruff + mypy on every commit
```

- Every pure function and every risk check has unit tests, including the edge cases: no strike
  within 0.08 delta, earnings inside the buffer, spread too wide, capital limits hit, a live
  account detected.
- `tests/fake_ib.py` is a deterministic stand-in for `ib_async.IB` built from real `ib_async`
  objects (Black-Scholes prices and Greeks, seeded history, what-if margins). The services and
  the CLI run end to end against it, and it records every `placeOrder` so tests prove a dry run
  sends nothing and a live account is refused. `tests/fake_nasdaq.py` does the same for Nasdaq.
- CI runs ruff, mypy and pytest on Python 3.10 and 3.13 on every push.

## Design notes

Choices made after testing against real TWS data:

- **Realized volatility is computed from closing prices**, not taken from IBKR's
  `HISTORICAL_VOLATILITY` series. In testing that series jumped erratically (e.g. NVDA 47% → 64%
  in one day with no matching price move) and didn't match actual price moves.
- **Margin comes from IBKR's what-if orders**, not a formula. IBKR's house margin on volatile
  stocks was 3–7× the textbook Reg-T figure, which would otherwise make strangles and straddles
  look far more attractive than they are. The what-if order must set `tif="DAY"` and the account
  explicitly, or IBKR returns nothing.
- **Strikes are fetched per expiration.** IBKR's chain lists every strike across all
  expirations, so requesting them blindly targets strikes that don't exist.
- **A leg is skipped if no strike is within 0.08 of the target delta**, e.g. when a stock has no
  strikes listed far enough out of the money.
- **Earnings dates come from Nasdaq** because IBKR requires a paid subscription for them. On the
  first test run this caught NVDA reporting the same day and BABA two days later, both of which
  looked like top candidates because of their high IV.
- **Output formats are stable.** The refactor kept every CSV column and value format, so files
  from before and after can be compared directly.

## Limitations

- One contract per trade (no position sizing up to the limits yet).
- Exits are flagged in the report but not automated: you close or roll positions yourself.
- The Nasdaq calendar is an unofficial endpoint and could change; failures are reported and the
  affected days listed so you can check them manually.
- Delayed data can occasionally leave a Greek blank in the report.
- Market-hours detection ignores exchange holidays.

## Project layout

```
config.toml                      all settings and risk limits
src/ibkr_options_algo/
  cli.py                         ibkr-algo entry point
  config.py  models.py  errors.py
  calculations.py  strategy.py  risk.py  reporting.py     pure logic
  storage.py                     output/ files
  earnings.py  sources/          Nasdaq earnings + market cap, HTTP retries
  ibkr/                          ib_async adapter and connection rules
  services/                      one module per command
tests/                           pytest suite + fake IBKR and Nasdaq
```

## License

MIT - see [LICENSE](LICENSE).
