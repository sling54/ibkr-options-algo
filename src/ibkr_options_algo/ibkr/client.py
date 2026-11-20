"""Thin adapter over ib_async: converts IBKR objects to domain models and back.

Contracts IBKR hands us are cached by conId so the rest of the code can refer to them by id
and the exact qualified objects are reused when ordering.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from typing import Any

from ib_async import (
    IB,
    ComboLeg,
    Contract,
    LimitOrder,
    Option,
    ScannerSubscription,
    Stock,
)

from ibkr_options_algo.calculations import make_quote
from ibkr_options_algo.config import IbkrConfig
from ibkr_options_algo.errors import LiveAccountError, MissingDataError
from ibkr_options_algo.models import (
    Greeks,
    Holding,
    OpenOrder,
    OptionChainParams,
    OptionQuote,
    OrderSpec,
    PlacedOrder,
    PnLSnapshot,
    PortfolioHolding,
    RawOptionTicker,
    Right,
    ScannerHit,
    StockInfo,
    StockSnapshot,
)
from ibkr_options_algo.risk import is_paper_account

log = logging.getLogger(__name__)

SMART = "SMART"
USD = "USD"
DAY = "DAY"
IV_HISTORY = "OPTION_IMPLIED_VOLATILITY"
PRICE_HISTORY = "TRADES"


def _right(value: str) -> Right:
    return "P" if value == "P" else "C"


def _require(contract: Contract | None) -> Contract:
    if contract is None:
        raise MissingDataError("IBKR returned a result without a contract.")
    return contract


def _greeks(model: Any) -> Greeks | None:
    if not model:
        return None
    return Greeks(
        delta=model.delta,
        gamma=model.gamma,
        theta=model.theta,
        vega=model.vega,
        implied_vol=model.impliedVol,
        und_price=model.undPrice,
    )


class IbkrClient:
    """Every IBKR request the pipeline makes goes through this class."""

    def __init__(self, ib: IB, port: int, cfg: IbkrConfig) -> None:
        self.ib = ib
        self.port = port
        self._cfg = cfg
        self._contracts: dict[int, Contract] = {}

    def _remember(self, contract: Contract) -> Contract:
        if contract.conId:
            self._contracts[contract.conId] = contract
        return contract

    def _contract(self, con_id: int) -> Contract:
        try:
            return self._contracts[con_id]
        except KeyError:
            raise MissingDataError(f"Contract {con_id} was never loaded from IBKR") from None

    # ------------------------------------------------------------------ session

    def disconnect(self) -> None:
        self.ib.disconnect()

    def use_configured_market_data(self) -> None:
        self.ib.reqMarketDataType(self._cfg.market_data_type)

    def managed_accounts(self) -> list[str]:
        return list(self.ib.managedAccounts())

    def primary_account(self) -> str:
        accounts = self.managed_accounts()
        if not accounts:
            raise MissingDataError("IBKR reported no managed accounts for this login.")
        return accounts[0]

    def server_time(self) -> Any:
        return self.ib.reqCurrentTime()

    def account_summary(self, account: str = "") -> dict[str, str]:
        return {row.tag: row.value for row in self.ib.accountSummary(account)}

    def positions(self, account: str = "") -> list[Holding]:
        return [
            Holding(
                symbol=p.contract.symbol,
                sec_type=p.contract.secType,
                right=p.contract.right,
                strike=p.contract.strike,
                quantity=p.position,
                avg_cost=p.avgCost,
            )
            for p in self.ib.positions(account)
        ]

    def portfolio(self, account: str = "") -> list[PortfolioHolding]:
        items = []
        for p in self.ib.portfolio(account):
            c = self._remember(p.contract)
            items.append(
                PortfolioHolding(
                    con_id=c.conId,
                    symbol=c.symbol,
                    sec_type=c.secType,
                    right=c.right,
                    strike=c.strike,
                    expiration=c.lastTradeDateOrContractMonth,
                    quantity=p.position,
                    avg_cost=p.averageCost,
                    market_price=p.marketPrice,
                    market_value=p.marketValue,
                    unrealized_pnl=p.unrealizedPNL,
                )
            )
        return items

    def open_orders(self) -> list[OpenOrder]:
        return [
            OpenOrder(
                order_id=t.order.orderId,
                symbol=t.contract.symbol,
                local_symbol=t.contract.localSymbol,
                action=t.order.action,
                quantity=t.order.totalQuantity,
                limit_price=float(t.order.lmtPrice) if t.order.lmtPrice is not None else None,
                status=t.orderStatus.status,
                filled=t.orderStatus.filled,
            )
            for t in self.ib.reqAllOpenOrders()
        ]

    def pnl(self, account: str) -> PnLSnapshot:
        """Today's P&L; IBKR streams it, so wait briefly for the first update."""
        stream = self.ib.reqPnL(account)
        self.ib.sleep(self._cfg.pnl_wait_seconds)
        snapshot = PnLSnapshot(stream.dailyPnL, stream.unrealizedPnL, stream.realizedPnL)
        self.ib.cancelPnL(account)
        return snapshot

    # ------------------------------------------------------------------ screener data

    def most_active(
        self, rows: int, include_etfs: bool, scan_code: str, location_code: str
    ) -> list[ScannerHit]:
        """Stocks with the most option volume today (so every result is optionable)."""
        scan = ScannerSubscription(
            instrument="STK",
            locationCode=location_code,
            scanCode=scan_code,
            numberOfRows=rows,
            stockTypeFilter="ALL" if include_etfs else "CORP",
        )
        hits = []
        for result in self.ib.reqScannerData(scan):
            c = _require(result.contractDetails.contract)
            c.exchange = SMART
            self._remember(c)
            hits.append(ScannerHit(con_id=c.conId, symbol=c.symbol))
        return hits

    def daily_closes(self, con_id: int, what_to_show: str) -> list[float]:
        """One bar per day of IV (as a decimal) or price over the configured duration."""
        bars = self.ib.reqHistoricalData(
            self._contract(con_id),
            endDateTime="",
            durationStr=self._cfg.history_duration,
            barSizeSetting="1 day",
            whatToShow=what_to_show,
            useRTH=True,
            timeout=self._cfg.history_timeout_seconds,
        )
        return [b.close for b in bars]

    # ------------------------------------------------------------------ stocks & chains

    def stock_details(self, symbol: str) -> StockInfo | None:
        details = self.ib.reqContractDetails(Stock(symbol, SMART, USD))
        if not details:
            return None
        c = self._remember(_require(details[0].contract))
        return StockInfo(con_id=c.conId, symbol=c.symbol, is_etf=details[0].stockType == "ETF")

    def qualify_stock(self, symbol: str) -> int:
        stock = Stock(symbol, SMART, USD)
        self.ib.qualifyContracts(stock)
        if not stock.conId:
            raise MissingDataError(f"IBKR doesn't recognise the stock symbol {symbol!r}.")
        return self._remember(stock).conId

    def stock_snapshot(self, con_id: int) -> StockSnapshot:
        ticker = self.ib.reqTickers(self._contract(con_id))[0]
        iv = ticker.impliedVolatility
        return StockSnapshot(market_price=ticker.marketPrice(), close=ticker.close, implied_vol=iv)

    def option_chains(self, con_id: int) -> list[OptionChainParams]:
        stock = self._contract(con_id)
        chains = self.ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
        return [
            OptionChainParams(
                exchange=c.exchange,
                trading_class=c.tradingClass,
                expirations=tuple(c.expirations),
                strikes=tuple(c.strikes),
            )
            for c in chains
        ]

    def listed_strikes(self, symbol: str, expiration: str, trading_class: str) -> list[float]:
        """Strikes that exist for one expiration.

        reqSecDefOptParams mixes strikes from all expirations, so requesting them blindly
        targets strikes that don't exist; contract details for the one expiration are exact.
        """
        details = self.ib.reqContractDetails(
            Option(symbol, expiration, right="C", exchange=SMART, tradingClass=trading_class)
        )
        return [_require(d.contract).strike for d in details]

    def qualify_options(
        self,
        symbol: str,
        expiration: str,
        trading_class: str,
        legs: Sequence[tuple[float, Right]],
    ) -> list[int]:
        """Qualify (strike, right) options and return the conIds IBKR recognised, in order."""
        wanted = [
            Option(symbol, expiration, strike, right, SMART, tradingClass=trading_class)
            for strike, right in legs
        ]
        qualified = [c for c in self.ib.qualifyContracts(*wanted) if c and c.conId]
        return [self._remember(c).conId for c in qualified]

    def requalify_options(self, con_ids: Sequence[int]) -> list[int]:
        """Look option contracts up again by conId (e.g. from a proposals file)."""
        ids = []
        for con_id in con_ids:
            c = Contract(conId=con_id, exchange=SMART)
            self.ib.qualifyContracts(c)
            self._contracts[con_id] = c
            ids.append(con_id)
        return ids

    # ------------------------------------------------------------------ quotes

    def _stream_batches(self, contracts: Sequence[Contract], *, progress: bool) -> list[Any]:
        """Snapshot tickers in batches.

        IBKR limits how many quotes stream at once (~100), so asking for every contract in one
        go leaves many of them blank.
        """
        size = self._cfg.quote_batch_size
        batches = math.ceil(len(contracts) / size)
        tickers: list[Any] = []
        for start in range(0, len(contracts), size):
            batch = contracts[start : start + size]
            batch_tickers = [self.ib.reqMktData(c) for c in batch]
            self.ib.sleep(self._cfg.quote_wait_seconds)
            for c in batch:
                self.ib.cancelMktData(c)
            tickers.extend(batch_tickers)
            if progress:
                log.info("  got batch %d of %d", start // size + 1, batches)
        return tickers

    def option_quotes(self, con_ids: Sequence[int]) -> list[OptionQuote]:
        """Two-sided quotes with a model delta; contracts without one are dropped."""
        tickers = self._stream_batches([self._contract(i) for i in con_ids], progress=False)
        quotes = []
        for t in tickers:
            g = t.modelGreeks
            quote = make_quote(
                con_id=t.contract.conId,
                right=_right(t.contract.right),
                strike=t.contract.strike,
                bid=t.bid,
                ask=t.ask,
                delta=g.delta if g else None,
            )
            if quote:
                quotes.append(quote)
        return quotes

    def raw_option_tickers(self, con_ids: Sequence[int]) -> list[RawOptionTicker]:
        """Everything IBKR sends for each contract, blanks included (for the chain dump)."""
        tickers = self._stream_batches([self._contract(i) for i in con_ids], progress=True)
        return [
            RawOptionTicker(
                expiration=t.contract.lastTradeDateOrContractMonth,
                strike=t.contract.strike,
                right=_right(t.contract.right),
                bid=t.bid,
                ask=t.ask,
                last=t.last,
                greeks=_greeks(t.modelGreeks),
            )
            for t in tickers
        ]

    def position_greeks(self, con_ids: Sequence[int]) -> dict[int, Greeks]:
        """Model Greeks for held options, polling once a second until all arrive."""
        contracts = []
        for con_id in con_ids:
            c = self._contract(con_id)
            c.exchange = SMART  # portfolio contracts come without a routing exchange
            contracts.append(c)
        if not contracts:
            return {}
        tickers = [self.ib.reqMktData(c) for c in contracts]
        for _ in range(self._cfg.greeks_timeout_seconds):
            self.ib.sleep(1)
            if all(t.modelGreeks for t in tickers):
                break
        for c in contracts:
            self.ib.cancelMktData(c)
        result = {}
        for t in tickers:
            greeks = _greeks(t.modelGreeks)
            if greeks:
                result[_require(t.contract).conId] = greeks
        return result

    # ------------------------------------------------------------------ orders

    def _ib_order(self, spec: OrderSpec, account: str) -> tuple[Contract, LimitOrder]:
        if spec.is_combo:
            # Selling every leg of a combo = BUY the combo at a negative price (a credit).
            contract = Contract(
                symbol=spec.symbol,
                secType="BAG",
                currency=USD,
                exchange=SMART,
                comboLegs=[
                    ComboLeg(conId=con_id, ratio=1, action="SELL", exchange=SMART)
                    for con_id in spec.leg_con_ids
                ],
            )
            order = LimitOrder("BUY", spec.quantity, -spec.credit, tif=DAY, account=account)
        else:
            contract = self._contract(spec.leg_con_ids[0])
            order = LimitOrder("SELL", spec.quantity, spec.credit, tif=DAY, account=account)
        if spec.order_ref:
            order.orderRef = spec.order_ref
        return contract, order

    def what_if_margin(self, spec: OrderSpec) -> float | None:
        """Initial margin IBKR would charge, from a what-if order (nothing is placed).

        tif and account must be set explicitly or IBKR answers with an empty list.
        """
        contract, order = self._ib_order(spec, self.primary_account())
        state = self.ib.whatIfOrder(contract, order)
        try:
            return float(state.initMarginChange)
        except (AttributeError, TypeError, ValueError):
            return None

    def place_order(self, spec: OrderSpec, account: str) -> PlacedOrder:
        """Send a limit order. Refuses unless this is a paper account on a paper port."""
        if not is_paper_account(account) or self.port not in self._cfg.paper_ports:
            raise LiveAccountError(
                f"Refusing to place an order: account {account} on port {self.port} "
                "is not a paper session."
            )
        contract, order = self._ib_order(spec, account)
        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(self._cfg.order_ack_wait_seconds)
        messages = tuple(entry.message for entry in trade.log if entry.errorCode)
        return PlacedOrder(trade.order.orderId, trade.orderStatus.status, messages)
