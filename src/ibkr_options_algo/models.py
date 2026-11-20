"""Domain types passed between layers.

The CSV column names and value formats match the original scripts exactly, so output files
from before and after the refactor can be compared line by line.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Literal

Right = Literal["P", "C"]
CsvValue = str | int | float
CsvRow = dict[str, CsvValue]

YES = "YES"


class Strategy(str, Enum):
    """Premium-selling strategies the picker can propose."""

    COVERED_CALL = "covered_call"
    CASH_SECURED_PUT = "cash_secured_put"
    SHORT_STRANGLE = "short_strangle"
    SHORT_STRADDLE = "short_straddle"

    @property
    def has_undefined_risk(self) -> bool:
        """Strangles and straddles: losses are unlimited on a big move."""
        return self in (Strategy.SHORT_STRANGLE, Strategy.SHORT_STRADDLE)


def _blank(value: CsvValue | date | None) -> CsvValue:
    """CSV cell for an optional value: None becomes an empty string."""
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return value


def _yes(flag: bool) -> str:
    return YES if flag else ""


def _opt_float(text: str) -> float | None:
    return float(text) if text != "" else None


def _opt_int(text: str) -> int | None:
    return int(text) if text != "" else None


# ----------------------------------------------------------------------------- market data


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """A two-sided option quote with a model delta."""

    con_id: int
    right: Right
    strike: float
    bid: float
    ask: float
    delta: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        """Bid-ask spread as a percentage of the mid price."""
        return 100 * (self.ask - self.bid) / ((self.bid + self.ask) / 2)


@dataclass(frozen=True, slots=True)
class OptionChainParams:
    """Expirations and strikes IBKR lists for one exchange / trading class."""

    exchange: str
    trading_class: str
    expirations: tuple[str, ...]
    strikes: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class StockInfo:
    con_id: int
    symbol: str
    is_etf: bool


@dataclass(frozen=True, slots=True)
class StockSnapshot:
    """Raw stock ticker values; any of them may be NaN when IBKR has no data."""

    market_price: float
    close: float
    implied_vol: float | None


@dataclass(frozen=True, slots=True)
class ScannerHit:
    con_id: int
    symbol: str


@dataclass(frozen=True, slots=True)
class Greeks:
    """IBKR model Greeks, uncleaned (values may be None, NaN or IBKR's 'unset' sentinel)."""

    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    implied_vol: float | None
    und_price: float | None


@dataclass(frozen=True, slots=True)
class RawOptionTicker:
    """Everything the single-stock chain dump shows for one contract."""

    expiration: str
    strike: float
    right: Right
    bid: float | None
    ask: float | None
    last: float | None
    greeks: Greeks | None


# ----------------------------------------------------------------------------- account


@dataclass(frozen=True, slots=True)
class Holding:
    """One position from ib.positions()."""

    symbol: str
    sec_type: str
    right: str
    strike: float
    quantity: float
    avg_cost: float


@dataclass(frozen=True, slots=True)
class PortfolioHolding:
    """One position from ib.portfolio(), with market value and P&L."""

    con_id: int
    symbol: str
    sec_type: str
    right: str
    strike: float
    expiration: str
    quantity: float
    avg_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float

    @property
    def is_option(self) -> bool:
        return self.sec_type == "OPT"


@dataclass(frozen=True, slots=True)
class OpenOrder:
    order_id: int
    symbol: str
    local_symbol: str
    action: str
    quantity: float
    limit_price: float | None
    status: str
    filled: float


@dataclass(frozen=True, slots=True)
class PnLSnapshot:
    daily: float | None
    unrealized: float | None
    realized: float | None


@dataclass(frozen=True, slots=True)
class OrderSpec:
    """A limit order to sell one option, or a combo that sells two, for a credit.

    For a combo where every leg is sold, IBKR's convention is to BUY the combo at a negative
    price; the IBKR adapter applies that sign. ``credit`` here is always positive.
    """

    symbol: str
    leg_con_ids: tuple[int, ...]
    quantity: int
    credit: float
    order_ref: str | None = None

    @property
    def is_combo(self) -> bool:
        return len(self.leg_con_ids) > 1


@dataclass(frozen=True, slots=True)
class PlacedOrder:
    order_id: int
    status: str
    messages: tuple[str, ...] = ()


# ----------------------------------------------------------------------------- screener


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """One stock's volatility statistics. Percent fields are already rounded for display."""

    activity_rank: int
    symbol: str
    price: float
    iv_now_pct: float
    iv_low_pct: float
    iv_high_pct: float
    iv_rank: float
    iv_percentile: float
    hv_pct: float
    iv_minus_hv: float
    iv_hv_ratio: float | None
    days_of_data: int
    is_candidate: bool
    hv_days: int
    rank: int = 0

    def to_csv_row(self) -> CsvRow:
        return {
            "rank": self.rank,
            "activity_rank": self.activity_rank,
            "symbol": self.symbol,
            "price": self.price,
            "iv_now_%": self.iv_now_pct,
            "iv_1y_low_%": self.iv_low_pct,
            "iv_1y_high_%": self.iv_high_pct,
            "iv_rank": self.iv_rank,
            "iv_percentile": self.iv_percentile,
            f"hv_{self.hv_days}d_%": self.hv_pct,
            "iv_minus_hv": self.iv_minus_hv,
            "iv_hv_ratio": _blank(self.iv_hv_ratio),
            "days_of_data": self.days_of_data,
            "sell_premium_candidate": _yes(self.is_candidate),
        }


@dataclass(frozen=True, slots=True)
class ScreenCandidate:
    """The two screener columns the picker needs."""

    symbol: str
    iv_rank: float | None

    @classmethod
    def from_csv_row(cls, row: Mapping[str, str]) -> ScreenCandidate:
        return cls(symbol=row["symbol"], iv_rank=_opt_float(row["iv_rank"]))


# ----------------------------------------------------------------------------- trade ideas


@dataclass(frozen=True, slots=True)
class TradeIdea:
    """One proposed trade, as written to proposed_trades_*.csv."""

    symbol: str
    strategy: Strategy
    is_etf: bool
    iv_rank: float | None
    stock_price: float
    expiration: str
    dte: int
    next_earnings: date | None
    earnings_before_expiry: bool
    contracts: int
    put_strike: float | None
    call_strike: float | None
    put_delta: float | None
    call_delta: float | None
    put_mid: float | None
    call_mid: float | None
    credit: float
    capital: float
    return_pct: float | None
    annualized_pct: float | None
    breakeven_low: float | None
    breakeven_high: float | None
    chance_all_otm_pct: float
    worst_spread_pct: float
    put_conid: int | None
    call_conid: int | None

    @property
    def leg_con_ids(self) -> tuple[int, ...]:
        return tuple(c for c in (self.put_conid, self.call_conid) if c is not None)

    @property
    def strikes_label(self) -> str:
        """'60.0' or '55.0/70.0' (put/call) for log lines."""
        return "/".join(str(s) for s in (self.put_strike, self.call_strike) if s is not None)

    def to_csv_row(self) -> CsvRow:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy.value,
            "is_etf": _yes(self.is_etf),
            "iv_rank": _blank(self.iv_rank),
            "stock_price": self.stock_price,
            "expiration": self.expiration,
            "dte": self.dte,
            "next_earnings": _blank(self.next_earnings),
            "earnings_before_expiry": _yes(self.earnings_before_expiry),
            "contracts": self.contracts,
            "put_strike": _blank(self.put_strike),
            "call_strike": _blank(self.call_strike),
            "put_delta": _blank(self.put_delta),
            "call_delta": _blank(self.call_delta),
            "put_mid": _blank(self.put_mid),
            "call_mid": _blank(self.call_mid),
            "credit_$": self.credit,
            "capital_or_margin_$": self.capital,
            "return_%": _blank(self.return_pct),
            "annualized_%": _blank(self.annualized_pct),
            "breakeven_low": _blank(self.breakeven_low),
            "breakeven_high": _blank(self.breakeven_high),
            "chance_all_otm_%": self.chance_all_otm_pct,
            "worst_spread_%": self.worst_spread_pct,
            "put_conid": _blank(self.put_conid),
            "call_conid": _blank(self.call_conid),
        }

    @classmethod
    def from_csv_row(cls, row: Mapping[str, str]) -> TradeIdea:
        earnings = row["next_earnings"]
        return cls(
            symbol=row["symbol"],
            strategy=Strategy(row["strategy"]),
            is_etf=row["is_etf"] == YES,
            iv_rank=_opt_float(row["iv_rank"]),
            stock_price=float(row["stock_price"]),
            expiration=row["expiration"],
            dte=int(row["dte"]),
            next_earnings=date.fromisoformat(earnings) if earnings else None,
            earnings_before_expiry=row["earnings_before_expiry"] == YES,
            contracts=int(row["contracts"]),
            put_strike=_opt_float(row["put_strike"]),
            call_strike=_opt_float(row["call_strike"]),
            put_delta=_opt_float(row["put_delta"]),
            call_delta=_opt_float(row["call_delta"]),
            put_mid=_opt_float(row["put_mid"]),
            call_mid=_opt_float(row["call_mid"]),
            credit=float(row["credit_$"]),
            capital=float(row["capital_or_margin_$"]),
            return_pct=_opt_float(row["return_%"]),
            annualized_pct=_opt_float(row["annualized_%"]),
            breakeven_low=_opt_float(row["breakeven_low"]),
            breakeven_high=_opt_float(row["breakeven_high"]),
            chance_all_otm_pct=float(row["chance_all_otm_%"]),
            worst_spread_pct=float(row["worst_spread_%"]),
            put_conid=_opt_int(row["put_conid"]),
            call_conid=_opt_int(row["call_conid"]),
        )


@dataclass(frozen=True, slots=True)
class UnderlyingContext:
    """Per-stock facts shared by every trade idea on that stock."""

    symbol: str
    price: float
    expiration: str
    dte: int
    is_etf: bool
    iv_rank: float | None
    earnings: date | None


# ----------------------------------------------------------------------------- orders


@dataclass(frozen=True, slots=True)
class OrderLogEntry:
    """One line of orders_log.csv (written for dry runs too)."""

    time: str
    mode: Literal["live", "dry_run"]
    order_id: int | None
    status: str
    symbol: str
    strategy: str
    expiration: str
    put_strike: float | None
    call_strike: float | None
    contracts: int
    limit_credit: float
    capital: float

    def to_csv_row(self) -> CsvRow:
        return {
            "time": self.time,
            "mode": self.mode,
            "order_id": _blank(self.order_id),
            "status": self.status,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "expiration": self.expiration,
            "put_strike": _blank(self.put_strike),
            "call_strike": _blank(self.call_strike),
            "contracts": self.contracts,
            "limit_credit": self.limit_credit,
            "capital_$": self.capital,
        }


@dataclass(frozen=True, slots=True)
class LoggedOrder:
    """The two order-log columns needed to count capital tied up in open orders."""

    order_id: str
    capital: float

    @classmethod
    def from_csv_row(cls, row: Mapping[str, str]) -> LoggedOrder:
        return cls(order_id=row["order_id"], capital=float(row["capital_$"]))


@dataclass(frozen=True, slots=True)
class TradeDecision:
    """Outcome of the risk checks for one trade idea."""

    reasons: tuple[str, ...]
    capital: float = 0.0
    quotes: Mapping[Right, OptionQuote] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.reasons


# ----------------------------------------------------------------------------- report


@dataclass(frozen=True, slots=True)
class PositionReportRow:
    """One position in the daily report. Stock rows leave option-only fields as None."""

    symbol: str
    type: str
    right: str
    strike: float | None
    expiration: str
    dte: int | None
    quantity: float
    avg_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float
    profit_pct_of_max: float | None
    underlying_price: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    iv_pct: float | None
    next_earnings: date | None
    earnings_before_expiry: bool
    actions: tuple[str, ...] = ()

    @property
    def is_stock(self) -> bool:
        return self.type == "stock"

    def to_csv_row(self) -> CsvRow:
        return {
            "symbol": self.symbol,
            "type": self.type,
            "right": self.right,
            "strike": _blank(self.strike),
            "expiration": self.expiration,
            "dte": _blank(self.dte),
            "quantity": self.quantity,
            "avg_cost": self.avg_cost,
            "market_price": self.market_price,
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
            "profit_%_of_max": _blank(self.profit_pct_of_max),
            "underlying_price": _blank(self.underlying_price),
            "delta": _blank(self.delta),
            "gamma": _blank(self.gamma),
            "theta_$/day": _blank(self.theta),
            "vega_$": _blank(self.vega),
            "iv_%": _blank(self.iv_pct),
            "next_earnings": _blank(self.next_earnings),
            "earnings_before_expiry": _yes(self.earnings_before_expiry),
            "actions": " | ".join(self.actions),
        }
