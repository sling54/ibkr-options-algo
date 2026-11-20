"""A deterministic stand-in for ``ib_async.IB`` so the pipeline runs without TWS.

It returns real ib_async objects (Ticker, ContractDetails, OptionChain, ...), so the production
adapter in ``ibkr_options_algo.ibkr.client`` is exercised unchanged. Option prices and Greeks
come from Black-Scholes on a per-stock IV; history is a seeded random walk.

Every ``placeOrder`` call is recorded in ``FakeWorld.placed`` so tests can prove a dry run
never sends anything.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ib_async import (
    AccountValue,
    BarData,
    Contract,
    ContractDetails,
    OptionChain,
    OptionComputation,
    OrderState,
    OrderStatus,
    PnL,
    PortfolioItem,
    Position,
    ScanData,
    Stock,
    Ticker,
    Trade,
    TradeLogEntry,
)

NAN = float("nan")
#: Days from "today" to each listed expiration.
EXPIRY_OFFSETS = (4, 11, 18, 25, 32, 39, 46, 53, 81)


@dataclass
class FakeStock:
    symbol: str
    price: float
    con_id: int
    is_etf: bool = False
    iv: float = 0.40  # today's 30-day IV
    iv_low: float = 0.20
    iv_high: float = 0.45
    iv_days: int = 251
    daily_vol: float = 0.015  # drives realized vol of the price path
    spread_frac: float = 0.06  # bid-ask spread as a fraction of the option's value
    whatif_ok: bool = True
    margin_per_leg_pct: float = 0.20  # what-if margin = pct x price x 100 per leg
    stock_iv: float | None = None  # ticker.impliedVolatility (None -> NaN)
    no_greeks: bool = False
    history_error: bool = False


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def black_scholes(
    spot: float, strike: float, years: float, vol: float, right: str
) -> tuple[float, float, float, float, float]:
    """(price, delta, gamma, vega per vol point, theta per day) with zero rates."""
    years = max(years, 1 / 365)
    sd = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if right == "C":
        price = spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
        delta = _norm_cdf(d1)
    else:
        price = strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1
    gamma = _norm_pdf(d1) / (spot * sd)
    vega = spot * _norm_pdf(d1) * math.sqrt(years) / 100
    theta = -(spot * _norm_pdf(d1) * vol) / (2 * math.sqrt(years)) / 365
    return price, delta, gamma, vega, theta


def strike_step(price: float) -> float:
    if price < 25:
        return 0.5
    if price < 100:
        return 1.0
    if price < 300:
        return 2.5
    return 5.0


@dataclass
class OptionSpec:
    con_id: int
    stock: FakeStock
    expiration: str
    strike: float
    right: str


@dataclass
class FakeWorld:
    """Everything the fake broker knows. Shared by every FakeIB instance."""

    stocks: list[FakeStock]
    today: date = field(default_factory=date.today)
    account: str = "DU1234567"
    open_ports: tuple[int, ...] = (7497,)
    net_liq: float = 1_000_000.0
    init_margin: float = 50_000.0
    positions: list[Position] = field(default_factory=list)
    portfolio: list[PortfolioItem] = field(default_factory=list)
    open_trades: list[Trade] = field(default_factory=list)
    pnl: tuple[float, float, float] = (1234.5, -250.0, 99.0)
    placed: list[tuple[Contract, Any]] = field(default_factory=list)
    what_if_calls: list[tuple[Contract, Any]] = field(default_factory=list)
    connects: list[tuple[int, int]] = field(default_factory=list)
    next_order_id: int = 100

    def __post_init__(self) -> None:
        self.by_symbol = {s.symbol: s for s in self.stocks}
        self.options: dict[int, OptionSpec] = {}
        self.option_ids: dict[tuple[str, str, float, str], int] = {}
        for s in self.stocks:
            for ei, exp in enumerate(self.expirations()):
                for si, k in enumerate(self.listed_strikes(s, exp)):
                    for right in ("P", "C"):
                        con_id = (
                            s.con_id * 100_000 + ei * 2_000 + si * 2 + (right == "C")
                        )
                        self.options[con_id] = OptionSpec(con_id, s, exp, k, right)
                        self.option_ids[(s.symbol, exp, k, right)] = con_id

    def expirations(self) -> list[str]:
        return [
            (self.today + timedelta(days=d)).strftime("%Y%m%d") for d in EXPIRY_OFFSETS
        ]

    def listed_strikes(self, stock: FakeStock, expiration: str) -> list[float]:
        """Strikes listed for one expiration: nearer expiries list a narrower range."""
        step = strike_step(stock.price)
        days = (datetime.strptime(expiration, "%Y%m%d").date() - self.today).days
        width = 0.25 if days < 20 else 0.55
        lo = math.ceil(stock.price * (1 - width) / step) * step
        hi = math.floor(stock.price * (1 + width) / step) * step
        n = round((hi - lo) / step)
        return [round(lo + i * step, 2) for i in range(n + 1)]

    def chain_strikes(self, stock: FakeStock) -> list[float]:
        """Union across expirations plus odd strikes that exist for none of them."""
        union = {k for e in self.expirations() for k in self.listed_strikes(stock, e)}
        step = strike_step(stock.price)
        union |= {
            round(stock.price * 0.4 // step * step, 2),
            round(stock.price * 1.6 // step * step, 2),
        }
        return sorted(union)

    # ------------------------------------------------------------------ pricing

    def option_values(self, spec: OptionSpec) -> dict[str, Any]:
        s = spec.stock
        days = (datetime.strptime(spec.expiration, "%Y%m%d").date() - self.today).days
        moneyness = math.log(spec.strike / s.price)
        vol = s.iv * (1 - 0.6 * moneyness)  # mild put skew
        price, delta, gamma, vega, theta = black_scholes(
            s.price, spec.strike, days / 365, vol, spec.right
        )
        half = max(price * s.spread_frac / 2, 0.01)
        bid = round(price - half, 2)
        ask = round(price + half, 2)
        greeks = None
        if not s.no_greeks:
            greeks = OptionComputation(
                0, vol, delta, price, 0.0, gamma, vega, theta, s.price
            )
        return {
            "bid": bid if bid > 0 else 0.0,
            "ask": ask,
            "last": round(price, 2),
            "greeks": greeks,
        }

    def iv_history(self, s: FakeStock) -> list[float]:
        rng = random.Random(f"iv-{s.symbol}")
        values = [rng.uniform(s.iv_low, s.iv_high) for _ in range(s.iv_days - 1)]
        values[0], values[1] = s.iv_low, s.iv_high
        return [*values, s.iv]

    def price_history(self, s: FakeStock) -> list[float]:
        rng = random.Random(f"px-{s.symbol}")
        path = [s.price]
        for _ in range(250):
            path.append(path[-1] / math.exp(rng.gauss(0, s.daily_vol)))
        return list(reversed(path))


def make_ticker(contract: Contract, **fields: Any) -> Ticker:
    """Ticker.__post_init__ resets market fields, so set them after construction."""
    ticker = Ticker(contract=contract)
    for name, value in fields.items():
        setattr(ticker, name, value)
    return ticker


class FakeIB:
    """Implements the subset of ``ib_async.IB`` the pipeline (old and new) uses."""

    world: FakeWorld  # set by the test, shared by instances created with IB()

    def __init__(self, world: FakeWorld | None = None) -> None:
        if world is not None:
            self.world = world
        self.connected = False

    # ------------------------------------------------------------------ session

    def connect(
        self, host: str, port: int, clientId: int = 0, timeout: float = 0
    ) -> FakeIB:
        if port not in self.world.open_ports:
            raise ConnectionRefusedError(f"nothing listening on {host}:{port}")
        self.world.connects.append((port, clientId))
        self.connected = True
        return self

    def disconnect(self) -> None:
        self.connected = False

    def sleep(self, _seconds: float = 0) -> bool:
        return True

    def reqMarketDataType(self, _kind: int) -> None:
        pass

    def managedAccounts(self) -> list[str]:
        return [self.world.account]

    def reqCurrentTime(self) -> datetime:
        return datetime(2026, 11, 20, 15, 0, 0)

    def accountSummary(self, account: str = "") -> list[AccountValue]:
        a = self.world.account
        return [
            AccountValue(a, "NetLiquidation", str(self.world.net_liq), "USD", ""),
            AccountValue(a, "InitMarginReq", str(self.world.init_margin), "USD", ""),
            AccountValue(a, "BuyingPower", str(self.world.net_liq * 4), "USD", ""),
        ]

    def positions(self, account: str = "") -> list[Position]:
        return list(self.world.positions)

    def portfolio(self, account: str = "") -> list[PortfolioItem]:
        return list(self.world.portfolio)

    def reqAllOpenOrders(self) -> list[Trade]:
        return list(self.world.open_trades)

    def reqPnL(self, account: str, modelCode: str = "") -> PnL:
        daily, unrealized, realized = self.world.pnl
        return PnL(account, modelCode, daily, unrealized, realized)

    def cancelPnL(self, account: str, modelCode: str = "") -> None:
        pass

    # ------------------------------------------------------------------ contracts

    def _stock_contract(self, s: FakeStock) -> Stock:
        c = Stock(s.symbol, "SMART", "USD")
        c.conId = s.con_id
        c.primaryExchange = "NASDAQ"
        c.localSymbol = s.symbol
        c.tradingClass = s.symbol
        return c

    def _fill_option(self, c: Contract, spec: OptionSpec) -> None:
        c.secType = "OPT"
        c.conId = spec.con_id
        c.symbol = spec.stock.symbol
        c.lastTradeDateOrContractMonth = spec.expiration
        c.strike = spec.strike
        c.right = spec.right
        c.multiplier = "100"
        c.currency = "USD"
        c.tradingClass = spec.stock.symbol
        c.localSymbol = f"{spec.stock.symbol:<6}{spec.expiration[2:]}{spec.right}{int(spec.strike * 1000):08d}"
        if not c.exchange:
            c.exchange = "SMART"

    def qualifyContracts(self, *contracts: Contract) -> list[Contract]:
        qualified = []
        for c in contracts:
            if c.secType == "STK" and c.symbol in self.world.by_symbol:
                s = self.world.by_symbol[c.symbol]
                c.conId = s.con_id
                c.primaryExchange = "NASDAQ"
                qualified.append(c)
            elif c.conId and c.conId in self.world.options:
                self._fill_option(c, self.world.options[c.conId])
                qualified.append(c)
            elif c.secType == "OPT":
                key = (
                    c.symbol,
                    c.lastTradeDateOrContractMonth,
                    float(c.strike),
                    c.right,
                )
                con_id = self.world.option_ids.get(key)
                if con_id:
                    self._fill_option(c, self.world.options[con_id])
                    qualified.append(c)
        return qualified

    def reqContractDetails(self, contract: Contract) -> list[ContractDetails]:
        if contract.secType == "STK":
            s = self.world.by_symbol.get(contract.symbol)
            if s is None:
                return []
            return [
                ContractDetails(
                    contract=self._stock_contract(s),
                    stockType="ETF" if s.is_etf else "COMMON",
                )
            ]
        if contract.secType == "OPT":
            s = self.world.by_symbol.get(contract.symbol)
            exp = contract.lastTradeDateOrContractMonth
            if s is None or exp not in self.world.expirations():
                return []
            details = []
            for k in self.world.listed_strikes(s, exp):
                spec = self.world.options[
                    self.world.option_ids[(s.symbol, exp, k, contract.right)]
                ]
                c = Contract()
                self._fill_option(c, spec)
                details.append(ContractDetails(contract=c))
            return details
        return []

    def reqSecDefOptParams(
        self, symbol: str, _fut_exchange: str, _sec_type: str, con_id: int
    ) -> list[OptionChain]:
        s = self.world.by_symbol[symbol]
        exps = self.world.expirations()
        strikes = self.world.chain_strikes(s)
        return [
            OptionChain("CBOE", con_id, symbol, "100", exps, strikes),
            OptionChain("SMART", con_id, symbol, "100", exps, strikes),
        ]

    # ------------------------------------------------------------------ data

    def reqScannerData(self, subscription: Any, *_args: Any) -> list[ScanData]:
        stocks = [
            s
            for s in self.world.stocks
            if subscription.stockTypeFilter == "ALL" or not s.is_etf
        ]
        stocks = stocks[: subscription.numberOfRows]
        return [
            ScanData(
                rank=i,
                contractDetails=ContractDetails(contract=self._stock_contract(s)),
                distance="",
                benchmark="",
                projection="",
                legsStr="",
            )
            for i, s in enumerate(stocks)
        ]

    def reqHistoricalData(self, contract: Contract, **kwargs: Any) -> list[BarData]:
        s = self.world.by_symbol[contract.symbol]
        if s.history_error:
            raise TimeoutError("historical data request timed out")
        what = kwargs["whatToShow"]
        values = (
            self.world.iv_history(s)
            if what == "OPTION_IMPLIED_VOLATILITY"
            else self.world.price_history(s)
        )
        start = self.world.today - timedelta(days=len(values))
        return [
            BarData(date=start + timedelta(days=i), open=v, high=v, low=v, close=v)
            for i, v in enumerate(values)
        ]

    def reqTickers(self, *contracts: Contract) -> list[Ticker]:
        tickers = []
        for c in contracts:
            s = self.world.by_symbol[c.symbol]
            iv = s.stock_iv if s.stock_iv is not None else NAN
            tickers.append(
                make_ticker(
                    c,
                    bid=s.price - 0.01,
                    ask=s.price + 0.01,
                    last=s.price,
                    bidSize=100,
                    askSize=100,
                    close=s.price * 0.99,
                    impliedVolatility=iv,
                )
            )
        return tickers

    def reqMktData(self, contract: Contract, *_args: Any, **_kwargs: Any) -> Ticker:
        spec = self.world.options.get(contract.conId)
        if spec is None:
            return make_ticker(contract)
        v = self.world.option_values(spec)
        return make_ticker(
            contract,
            bid=v["bid"],
            ask=v["ask"],
            last=v["last"],
            bidSize=10,
            askSize=10,
            modelGreeks=v["greeks"],
        )

    def cancelMktData(self, _contract: Contract) -> None:
        pass

    # ------------------------------------------------------------------ orders

    def _legs(self, contract: Contract) -> list[OptionSpec]:
        if contract.secType == "BAG":
            return [self.world.options[leg.conId] for leg in contract.comboLegs]
        return [self.world.options[contract.conId]]

    def whatIfOrder(self, contract: Contract, order: Any) -> OrderState | list[Any]:
        self.world.what_if_calls.append((contract, order))
        if order.tif != "DAY" or not order.account:
            return []  # what real TWS does without an explicit tif + account
        legs = self._legs(contract)
        s = legs[0].stock
        if not s.whatif_ok:
            return []
        margin = (
            sum(s.margin_per_leg_pct * s.price * 100 for _ in legs)
            * order.totalQuantity
        )
        return OrderState(initMarginChange=f"{margin:.2f}")

    def placeOrder(self, contract: Contract, order: Any) -> Trade:
        self.world.placed.append((contract, order))
        order.orderId = self.world.next_order_id
        self.world.next_order_id += 1
        status = OrderStatus(orderId=order.orderId, status="Submitted")
        log = [
            TradeLogEntry(
                time=datetime.now(), status="Submitted", message="", errorCode=0
            )
        ]
        return Trade(contract=contract, order=order, orderStatus=status, log=log)


# ---------------------------------------------------------------------- scenario


def default_world(today: date | None = None) -> FakeWorld:
    """A market that exercises every branch of the pipeline.

    - NVDA/BABA/HOOD/SMALL/SPY/WIDE/BIG/NOMARGIN: high IV rank candidates
    - LOWIV: IV rank low -> not a candidate; NEW: too little history; BROKE: history errors
    - Earnings (see fake_nasdaq): BABA inside 7 days, HOOD before expiry
    - SMALL is under $10B (strangles blocked); SPY is an ETF; WIDE has 20% spreads
    - BIG is a $1,500 stock: its cash-secured put exceeds 10% of the account
    - AAPL: 250 shares owned (covered calls), not a candidate
    - META: short put held (busy), TSLA: open order (busy)
    """
    stocks = [
        FakeStock(
            "NVDA", 154.19, 1001, iv=0.62, iv_low=0.28, iv_high=0.64, stock_iv=0.62
        ),
        FakeStock("BABA", 86.97, 1002, iv=0.54, iv_low=0.30, iv_high=0.55),
        FakeStock(
            "HOOD", 68.55, 1003, iv=0.80, iv_low=0.45, iv_high=0.82, stock_iv=0.80
        ),
        FakeStock("SMALL", 18.40, 1004, iv=0.90, iv_low=0.40, iv_high=0.95),
        FakeStock(
            "SPY",
            590.10,
            1005,
            is_etf=True,
            iv=0.22,
            iv_low=0.10,
            iv_high=0.23,
            daily_vol=0.006,
        ),
        FakeStock(
            "WIDE", 42.00, 1006, iv=0.70, iv_low=0.30, iv_high=0.72, spread_frac=0.40
        ),
        FakeStock(
            "BIG", 1500.0, 1007, iv=0.45, iv_low=0.20, iv_high=0.46, stock_iv=0.45
        ),
        FakeStock(
            "NOMARGIN", 55.0, 1008, iv=0.65, iv_low=0.25, iv_high=0.66, whatif_ok=False
        ),
        FakeStock("LOWIV", 120.0, 1009, iv=0.21, iv_low=0.20, iv_high=0.60),
        FakeStock("NEW", 30.0, 1010, iv_days=40),
        FakeStock("BROKE", 75.0, 1011, history_error=True),
        FakeStock("AAPL", 228.0, 1012, iv=0.25, iv_low=0.18, iv_high=0.40),
        FakeStock("META", 590.0, 1013, iv=0.42, iv_low=0.25, iv_high=0.43),
        FakeStock("TSLA", 330.0, 1014, iv=0.70, iv_low=0.40, iv_high=0.71),
        FakeStock(
            "NOGREEK", 64.0, 1015, iv=0.70, iv_low=0.30, iv_high=0.71, no_greeks=True
        ),
    ]
    world = FakeWorld(stocks=stocks, today=today or date.today())
    add_holdings(world)
    return world


def add_holdings(world: FakeWorld) -> None:
    """AAPL shares, a short META put and an open TSLA order (plus a long NVDA call)."""
    acct = world.account
    aapl = world.by_symbol["AAPL"]
    stock = Stock("AAPL", "", "USD")
    stock.conId = aapl.con_id
    world.positions.append(Position(acct, stock, 250.0, 201.5))

    meta_exp = world.expirations()[5]
    meta_put = _option_contract(world, "META", meta_exp, 520.0, "P")
    world.positions.append(Position(acct, meta_put, -1.0, 1250.0))

    nvda_exp = world.expirations()[2]
    nvda_call = _option_contract(world, "NVDA", nvda_exp, 160.0, "C")
    world.positions.append(Position(acct, nvda_call, 2.0, 410.0))

    world.portfolio += [
        PortfolioItem(stock, 250.0, 228.0, 57000.0, 201.5, 6625.0, 0.0, acct),
        PortfolioItem(meta_put, -1.0, 5.1, -510.0, 1250.0, 740.0, 0.0, acct),
        PortfolioItem(nvda_call, 2.0, 3.2, 640.0, 410.0, -180.0, 0.0, acct),
    ]

    tsla = world.by_symbol["TSLA"]
    order_contract = _option_contract(world, "TSLA", world.expirations()[5], 300.0, "P")
    from ib_async import LimitOrder

    order = LimitOrder("SELL", 1, 9.85, tif="DAY", account=acct)
    order.orderId = 77
    world.open_trades.append(
        Trade(
            contract=order_contract,
            order=order,
            orderStatus=OrderStatus(orderId=77, status="Submitted"),
        )
    )
    _ = tsla


def _option_contract(
    world: FakeWorld, symbol: str, exp: str, strike: float, right: str
) -> Contract:
    con_id = world.option_ids[(symbol, exp, strike, right)]
    c = Contract()
    FakeIB(world)._fill_option(c, world.options[con_id])
    c.exchange = ""  # portfolio contracts come without a routing exchange
    return c


def risk_world(today: date | None = None) -> FakeWorld:
    """Like default_world but with room for more trades, so every risk check gets reached.

    No long NVDA call (fewer busy stocks) and a large existing margin, so the 50% total-capital
    limit is hit part-way through.
    """
    world = default_world(today)
    world.positions = [p for p in world.positions if p.contract.symbol != "NVDA"]
    world.portfolio = [p for p in world.portfolio if p.contract.symbol != "NVDA"]
    world.init_margin = 380_000.0
    return world
