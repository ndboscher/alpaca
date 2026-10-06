"""Thin wrapper around alpaca-py so the rest of the bot deals in plain types."""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from datetime import date, datetime

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    OptionLatestQuoteRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    ContractType,
    OrderSide,
    OrderStatus,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)

from .config import Config
from .models import Bar, OptionCandidate, Side, parse_occ

log = logging.getLogger(__name__)

_DONE = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED,
         OrderStatus.DONE_FOR_DAY}


@dataclass
class Clock:
    now: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass
class Account:
    equity: float
    last_equity: float
    options_buying_power: float
    options_level: int
    status: str
    trading_blocked: bool

    @property
    def day_pnl(self) -> float:
        return self.equity - self.last_equity


@dataclass
class OptionPosition:
    symbol: str
    underlying: str
    expiration: date
    side: Side
    qty: int
    avg_entry: float
    current_price: float | None


@dataclass
class FillResult:
    filled_qty: int
    avg_price: float | None
    order_id: str | None


class Broker:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.trading = TradingClient(cfg.api_key, cfg.secret_key, paper=cfg.paper)
        self.stocks = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
        self.options = OptionHistoricalDataClient(cfg.api_key, cfg.secret_key)
        self._stock_feed = DataFeed(cfg.stock_feed)
        self._options_feed = OptionsFeed(cfg.options_feed)

    # ---- account / clock -------------------------------------------------

    def clock(self) -> Clock:
        c = self.trading.get_clock()
        return Clock(now=c.timestamp, is_open=c.is_open, next_open=c.next_open,
                     next_close=c.next_close)

    def account(self) -> Account:
        a = self.trading.get_account()
        return Account(
            equity=float(a.equity or 0),
            last_equity=float(a.last_equity or 0),
            options_buying_power=float(a.options_buying_power or a.buying_power or 0),
            options_level=int(a.options_trading_level or 0),
            status=str(a.status.value if hasattr(a.status, "value") else a.status),
            trading_blocked=bool(a.trading_blocked),
        )

    # ---- market data -----------------------------------------------------

    def minute_bars(self, symbols: list[str], start: datetime) -> dict[str, list[Bar]]:
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Minute,
                               start=start, feed=self._stock_feed)
        data = self.stocks.get_stock_bars(req).data
        out: dict[str, list[Bar]] = {}
        for sym in symbols:
            out[sym] = sorted(
                (Bar(b.timestamp, b.open, b.high, b.low, b.close, b.volume or 0)
                 for b in data.get(sym, [])),
                key=lambda b: b.ts,
            )
        return out

    def last_price(self, symbol: str) -> float:
        trades = self.stocks.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self._stock_feed))
        return float(trades[symbol].price)

    def option_chain(self, underlying: str, expiration: date, side: Side | None,
                     spot: float, width_pct: float) -> list[OptionCandidate]:
        lo, hi = spot * (1 - width_pct / 100), spot * (1 + width_pct / 100)
        req = OptionChainRequest(
            underlying_symbol=underlying,
            feed=self._options_feed,
            expiration_date=expiration,
            type=None if side is None else (ContractType.CALL if side == Side.CALL
                                            else ContractType.PUT),
            strike_price_gte=round(lo, 2),
            strike_price_lte=round(hi, 2),
        )
        snaps = self.options.get_option_chain(req)
        out = []
        for sym, snap in snaps.items():
            occ = parse_occ(sym)
            q = snap.latest_quote
            if occ is None or q is None:
                continue
            delta = snap.greeks.delta if snap.greeks is not None else None
            out.append(OptionCandidate(
                symbol=sym, underlying=occ.underlying, expiration=occ.expiration,
                side=occ.side, strike=occ.strike, bid=float(q.bid_price or 0),
                ask=float(q.ask_price or 0), delta=delta,
            ))
        return sorted(out, key=lambda c: (c.side.value, c.strike))

    def option_quotes(self, symbols: list[str]) -> dict[str, tuple[float, float]]:
        if not symbols:
            return {}
        quotes = self.options.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=symbols, feed=self._options_feed))
        return {s: (float(q.bid_price or 0), float(q.ask_price or 0)) for s, q in quotes.items()}

    # ---- positions / orders ---------------------------------------------

    def option_positions(self) -> list[OptionPosition]:
        out = []
        for p in self.trading.get_all_positions():
            occ = parse_occ(p.symbol)
            if occ is None:
                continue
            out.append(OptionPosition(
                symbol=p.symbol, underlying=occ.underlying, expiration=occ.expiration,
                side=occ.side, qty=int(float(p.qty)), avg_entry=float(p.avg_entry_price),
                current_price=float(p.current_price) if p.current_price else None,
            ))
        return out

    def open_order_symbols(self) -> set[str]:
        orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return {o.symbol for o in orders}

    def buy_limit(self, symbol: str, qty: int, limit: float) -> FillResult:
        """Buy-to-open with a limit order; wait for a fill, cancel whatever is left."""
        if self.dry_run:
            log.info("[DRY RUN] BUY %d %s @ %.2f", qty, symbol, limit)
            return FillResult(qty, limit, None)
        order = self.trading.submit_order(LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            limit_price=round(limit, 2),
        ))
        return self._await_fill(str(order.id), self.cfg.execution.fill_timeout_seconds,
                                cancel_on_timeout=True)

    def sell(self, symbol: str, qty: int, limit: float | None) -> FillResult:
        """Sell-to-close. Market order when `limit` is None."""
        if self.dry_run:
            log.info("[DRY RUN] SELL %d %s @ %s", qty, symbol,
                     "MKT" if limit is None else f"{limit:.2f}")
            return FillResult(qty, limit, None)
        if limit is None:
            req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL,
                                     time_in_force=TimeInForce.DAY)
        else:
            req = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL,
                                    time_in_force=TimeInForce.DAY, limit_price=round(limit, 2))
        order = self.trading.submit_order(req)
        return self._await_fill(str(order.id), self.cfg.execution.fill_timeout_seconds,
                                cancel_on_timeout=limit is not None)

    def cancel_orders_for(self, symbols: set[str]) -> None:
        if self.dry_run:
            return
        for o in self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)):
            if o.symbol in symbols:
                try:
                    self.trading.cancel_order_by_id(o.id)
                except APIError as e:
                    log.warning("Could not cancel order %s: %s", o.id, e)

    def _await_fill(self, order_id: str, timeout: float, cancel_on_timeout: bool) -> FillResult:
        deadline = _time.monotonic() + timeout
        order = self.trading.get_order_by_id(order_id)
        while order.status not in _DONE and _time.monotonic() < deadline:
            _time.sleep(1)
            order = self.trading.get_order_by_id(order_id)
        if order.status not in _DONE and cancel_on_timeout:
            try:
                self.trading.cancel_order_by_id(order_id)
            except APIError as e:
                log.warning("Cancel failed for %s: %s", order_id, e)
            # Re-read: it may have filled (fully or partly) while we were cancelling.
            for _ in range(5):
                order = self.trading.get_order_by_id(order_id)
                if order.status in _DONE:
                    break
                _time.sleep(1)
        filled = int(float(order.filled_qty or 0))
        price = float(order.filled_avg_price) if order.filled_avg_price else None
        return FillResult(filled, price, order_id)
