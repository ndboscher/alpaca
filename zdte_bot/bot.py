"""The main trading loop."""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError

from .broker import Broker, OptionPosition
from .config import EXPIRY_ORDER_CUTOFF, Config
from .contracts import select_contract
from .journal import DayState, Journal
from .models import Side, parse_occ
from .risk import DayStats, entry_block_reason, exit_reason, position_size, window_exit_reason
from .spreads import select_credit_spread, spread_exit_reason, spread_side_for, spread_size
from .strategy import breakout_signal

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
CLOSE_BUFFER = timedelta(minutes=15)  # on early-close days, flatten this long before the bell


@dataclass(frozen=True)
class DaySchedule:
    """Today's key times (ET), shared by the trading loop and the dashboard."""
    session_open: datetime
    range_end: datetime          # opening range complete; breakouts can trigger from here
    last_entry: datetime         # no new trades at or after this
    flatten_at: datetime         # safety flatten (earlier on half-days)
    close: datetime
    window_start: datetime | None = None  # midday profit window, if enabled
    window_end: datetime | None = None


def day_schedule(cfg: Config, today: date, next_close: datetime) -> DaySchedule:
    spread_mode = cfg.mode == "credit_spread"
    session_open = datetime.combine(today, SESSION_OPEN, tzinfo=ET)
    close = next_close.astimezone(ET)
    flatten_at = min(datetime.combine(today, cfg.schedule.flatten_at, tzinfo=ET),
                     close - CLOSE_BUFFER)
    entry_cutoff = (cfg.credit_spread.no_new_entries_after if spread_mode
                    else cfg.schedule.no_new_entries_after)
    pw = cfg.profit_window
    if pw.enabled:  # trades need time to work before the window
        entry_cutoff = min(entry_cutoff, pw.start)
    minutes = (cfg.credit_spread.opening_range_minutes if spread_mode
               else cfg.schedule.opening_range_minutes)
    return DaySchedule(
        session_open=session_open,
        range_end=session_open + timedelta(minutes=minutes),
        last_entry=min(datetime.combine(today, entry_cutoff, tzinfo=ET), flatten_at),
        flatten_at=flatten_at,
        close=close,
        window_start=datetime.combine(today, pw.start, tzinfo=ET) if pw.enabled else None,
        window_end=datetime.combine(today, pw.end, tzinfo=ET) if pw.enabled else None,
    )


class Bot:
    def __init__(self, cfg: Config, broker: Broker, data_dir: Path = Path("data"),
                 log_dir: Path = Path("logs")):
        self.cfg = cfg
        self.broker = broker
        self.dry_run = broker.dry_run
        self.state_dir = data_dir / ("dry-run" if self.dry_run else "live" if not cfg.paper
                                     else "paper")
        self.journal = Journal(log_dir / ("trades-dry-run.csv" if self.dry_run else "trades.csv"))
        self.state: DayState | None = None
        self._flattened_today = False
        self._session_seen = False
        self._market_open = False
        self._skip_logged = False
        self.spread_mode = cfg.mode == "credit_spread"

    # ---- helpers ---------------------------------------------------------

    def load_day(self, today: date) -> DayState:
        if self.state is None or self.state.day != today.isoformat():
            self.state = DayState.load(self.state_dir, today)
            self._flattened_today = False
            self._skip_logged = False
        return self.state

    def _managed_positions(self, today: date) -> list[OptionPosition]:
        """0DTE option positions on our symbols. In dry-run these are simulated."""
        if self.dry_run:
            out = []
            for sym, p in self.state.paper_positions.items():
                occ = parse_occ(sym)
                out.append(OptionPosition(sym, occ.underlying, occ.expiration, occ.side,
                                          p["qty"], p["entry"], None))
            return out
        return [p for p in self.broker.option_positions()
                if p.underlying in self.cfg.symbols and p.expiration == today and p.qty > 0]

    def _close(self, pos: OptionPosition, reason: str, bid: float | None) -> None:
        use_market = self.cfg.execution.use_market_orders_for_exits or not bid
        limit = None if use_market else bid
        log.info("EXIT %s x%d: %s", pos.symbol, pos.qty, reason)
        res = self.broker.sell(pos.symbol, pos.qty, limit)
        price = res.avg_price if res.avg_price is not None else bid
        self.journal.record("SELL", pos.symbol, res.filled_qty, price, reason, self.dry_run)
        if res.filled_qty < pos.qty:
            log.warning("Only %d/%d of %s sold; will retry next loop",
                        res.filled_qty, pos.qty, pos.symbol)
        if self.dry_run:
            remaining = pos.qty - res.filled_qty
            if remaining > 0:
                self.state.paper_positions[pos.symbol]["qty"] = remaining
            else:
                self.state.paper_positions.pop(pos.symbol, None)
        if res.filled_qty >= pos.qty:
            self.state.high_water.pop(pos.symbol, None)

    def flatten(self, positions: list[OptionPosition], reason: str) -> None:
        if not positions:
            return
        self.broker.cancel_orders_for({p.symbol for p in positions})
        quotes = self._quotes([p.symbol for p in positions])
        for p in positions:
            self._close(p, reason, quotes.get(p.symbol, (None, None))[0])

    def _quotes(self, symbols: list[str]) -> dict[str, tuple[float, float]]:
        try:
            return self.broker.option_quotes(symbols)
        except APIError as e:
            log.warning("Could not fetch option quotes: %s", e)
            return {}

    # ---- one iteration ---------------------------------------------------

    def tick(self) -> float:
        """Run one loop iteration. Returns seconds to sleep before the next one."""
        clock = self.broker.clock()
        now = clock.now.astimezone(ET)
        today = now.date()

        self._market_open = clock.is_open
        if clock.is_open:
            self._session_seen = True
        else:
            wait = (clock.next_open - clock.now).total_seconds()
            log.info("Market closed. Next open %s (in %.1f h).",
                     clock.next_open.astimezone(ET).strftime("%a %Y-%m-%d %H:%M ET"), wait / 3600)
            return max(60.0, min(wait, 1800.0))

        state = self.load_day(today)
        sched = day_schedule(self.cfg, today, clock.next_close)
        session_open, flatten_at, last_entry = sched.session_open, sched.flatten_at, sched.last_entry
        if today.isoformat() in self.cfg.skip_dates:
            if not self._skip_logged:
                log.info("%s is in skip_dates: managing open positions only, no new trades.", today)
                self._skip_logged = True
            last_entry = session_open

        account = self.broker.account()
        if self.spread_mode:
            self._tick_spreads(now, today, session_open, flatten_at, last_entry, account)
        else:
            self._tick_long(now, today, session_open, flatten_at, last_entry, account)
        state.save(self.state_dir)
        return self.cfg.poll_seconds

    def _past_flatten(self, now: datetime, flatten_at: datetime, has_positions: bool) -> bool:
        if now < flatten_at:
            return False
        if now.time() >= EXPIRY_ORDER_CUTOFF and has_positions:
            log.error("Past Alpaca's %s ET order cutoff with positions still open; Alpaca will "
                      "auto-liquidate them. Check the account.", EXPIRY_ORDER_CUTOFF.strftime("%H:%M"))
        elif not has_positions and not self._flattened_today:
            log.info("Past flatten time (%s). No new trades today.", flatten_at.strftime("%H:%M"))
        self._flattened_today = True
        return True

    # ---- long calls/puts -------------------------------------------------

    def _tick_long(self, now, today, session_open, flatten_at, last_entry, account) -> None:
        state = self.state
        positions = self._managed_positions(today)
        stats = DayStats(trades=state.trades, open_positions=len(positions), pnl=account.day_pnl)

        # 1. Hard stops: end-of-day flatten and daily loss limit.
        if self._past_flatten(now, flatten_at, bool(positions)):
            self.flatten(positions, "end-of-day flatten")
            return
        if stats.pnl <= -abs(self.cfg.risk.max_daily_loss_usd) and positions:
            self.flatten(positions, f"daily loss limit ({stats.pnl:.2f})")
            positions = []

        # 2. Manage open positions.
        quotes = self._quotes([p.symbol for p in positions])
        for p in positions:
            bid, ask = quotes.get(p.symbol, (0.0, 0.0))
            mark = (bid + ask) / 2 if bid > 0 and ask > 0 else (p.current_price or 0)
            if mark <= 0:
                continue
            hw = max(state.high_water.get(p.symbol, p.avg_entry), mark)
            state.high_water[p.symbol] = hw
            pnl_pct = (mark - p.avg_entry) / p.avg_entry * 100
            reason = (exit_reason(p.avg_entry, mark, hw, self.cfg.risk)
                      or window_exit_reason(pnl_pct, now.time(), self.cfg.profit_window))
            log.info("HOLD %s x%d entry %.2f mark %.2f (%+.1f%%)", p.symbol, p.qty, p.avg_entry,
                     mark, pnl_pct)
            if reason:
                self._close(p, reason, bid)

        # 3. Look for new entries.
        if now < last_entry:
            self._scan_entries(session_open, today, stats, account.options_buying_power)

    # ---- credit spreads --------------------------------------------------

    def _tick_spreads(self, now, today, session_open, flatten_at, last_entry, account) -> None:
        state = self.state
        spreads = self._reconcile_spreads(today)
        stats = DayStats(trades=state.trades, open_positions=len(spreads), pnl=account.day_pnl)

        if self._past_flatten(now, flatten_at, bool(spreads)):
            self.flatten_spreads(today, "end-of-day flatten")
            return
        if stats.pnl <= -abs(self.cfg.risk.max_daily_loss_usd) and spreads:
            self.flatten_spreads(today, f"daily loss limit ({stats.pnl:.2f})")
            stats.open_positions = 0

        if state.spreads:
            legs = [s for sp in state.spreads.values() for s in (sp["short"], sp["long"])]
            quotes = self._quotes(legs)
            spots = self._spots({sp["underlying"] for sp in state.spreads.values()})
            for key, sp in list(state.spreads.items()):
                if sp["short"] not in quotes or sp["long"] not in quotes:
                    continue
                (sb, sa), (lb, la) = quotes[sp["short"]], quotes[sp["long"]]
                cost = max(0.0, (sb + sa) / 2 - (lb + la) / 2)
                spot = spots.get(sp["underlying"])
                log.info("HOLD %s x%d credit %.2f cost-to-close %.2f spot %s", key, sp["qty"],
                         sp["credit"], cost, f"{spot:.2f}" if spot else "?")
                captured_pct = (sp["credit"] - cost) / sp["credit"] * 100
                reason = (spread_exit_reason(sp["credit"], cost, spot, Side(sp["side"]),
                                             sp["short_strike"], self.cfg.credit_spread)
                          or window_exit_reason(captured_pct, now.time(), self.cfg.profit_window))
                if reason:
                    self._close_spread(key, reason, natural_debit=max(0.01, sa - lb))

        if now < last_entry:
            self._scan_spread_entries(session_open, today, stats, account.options_buying_power)

    def _spots(self, symbols: set[str]) -> dict[str, float]:
        out = {}
        for s in symbols:
            try:
                out[s] = self.broker.last_price(s)
            except APIError as e:
                log.warning("Could not fetch %s price: %s", s, e)
        return out

    def _reconcile_spreads(self, today: date) -> dict[str, dict]:
        """Drop spreads that are no longer held (closed by hand, or by Alpaca)."""
        if self.dry_run or not self.state.spreads:
            return self.state.spreads
        held = {p.symbol: p.qty for p in self.broker.option_positions()}
        for key, sp in list(self.state.spreads.items()):
            short_q, long_q = -held.get(sp["short"], 0), held.get(sp["long"], 0)
            if short_q <= 0 and long_q <= 0:
                log.info("Spread %s is no longer held; forgetting it.", key)
                del self.state.spreads[key]
            elif short_q > 0 and long_q > 0:
                sp["qty"] = min(short_q, long_q)
            else:  # one leg gone: a spread order can't close this, so close the leftover leg
                leftover = sp["short"] if short_q > 0 else sp["long"]
                log.warning("Spread %s has only one leg left; closing %s at market.", key, leftover)
                self.broker.close_position(leftover)
                self.journal.record("CLOSE", leftover, max(short_q, long_q), None,
                                    "orphaned spread leg", self.dry_run)
                del self.state.spreads[key]
        return self.state.spreads

    def _close_spread(self, key: str, reason: str, natural_debit: float | None) -> None:
        sp = self.state.spreads[key]
        use_market = self.cfg.execution.use_market_orders_for_exits or natural_debit is None
        log.info("EXIT %s x%d: %s", key, sp["qty"], reason)
        res = self.broker.close_credit_spread(sp["short"], sp["long"], sp["qty"],
                                              None if use_market else natural_debit)
        price = res.avg_price if res.avg_price is not None else natural_debit
        self.journal.record("CLOSE_SPREAD", key, res.filled_qty, price, reason, self.dry_run)
        if res.filled_qty >= sp["qty"]:
            del self.state.spreads[key]
        else:
            sp["qty"] -= res.filled_qty
            log.warning("Only %d of %s closed; will retry next loop", res.filled_qty, key)

    def flatten_spreads(self, today: date, reason: str) -> None:
        state = self.state
        if state.spreads:
            self.broker.cancel_orders_for({s for sp in state.spreads.values()
                                           for s in (sp["short"], sp["long"])})
        for key in list(state.spreads):
            self._close_spread(key, reason, natural_debit=None)
        if self.dry_run:
            return
        # Anything left that isn't a tracked spread (e.g. a leg orphaned by a partial fill):
        # close shorts first so we never leave a naked short behind.
        tracked = {s for sp in state.spreads.values() for s in (sp["short"], sp["long"])}
        strays = sorted((p for p in self.broker.option_positions()
                         if p.underlying in self.cfg.symbols and p.expiration == today
                         and p.symbol not in tracked), key=lambda p: p.qty)
        for p in strays:
            log.warning("Closing stray leg %s x%d at market", p.symbol, p.qty)
            self.broker.close_position(p.symbol)
            self.journal.record("CLOSE", p.symbol, abs(p.qty), None, reason, self.dry_run)

    def _scan_spread_entries(self, session_open: datetime, today: date, stats: DayStats,
                             buying_power: float) -> None:
        state, cs = self.state, self.cfg.credit_spread
        if (block := entry_block_reason(stats, self.cfg.risk)) is not None:
            log.debug("No new entries: %s", block)
            return
        held = {sp["underlying"] for sp in state.spreads.values()}
        pending = set() if self.dry_run else {
            occ.underlying for s in self.broker.open_order_symbols() if (occ := parse_occ(s))}
        candidates = [s for s in self.cfg.symbols
                      if s not in held and s not in pending
                      and state.entries_per_symbol.get(s, 0) < self.cfg.strategy.max_entries_per_symbol]
        if not candidates:
            return

        bars = self.broker.minute_bars(candidates, session_open)
        for sym in candidates:
            signal = breakout_signal(bars.get(sym, []), session_open, cs.opening_range_minutes,
                                     self.cfg.strategy, cs.min_range_pct, cs.max_range_pct)
            if signal is None:
                continue
            side = spread_side_for(signal)
            log.info("SIGNAL %s %s breakout -> sell %s spread: %s", sym,
                     "bullish" if signal.side == Side.CALL else "bearish", side.value, signal.reason)

            far_edge = signal.range.low if side == Side.PUT else signal.range.high
            width_pct = (abs(signal.price - far_edge) + cs.width + 1) / signal.price * 100
            chain = self.broker.option_chain(sym, today, side, signal.price, width_pct)
            spread = select_credit_spread(chain, signal, cs, self.cfg.contracts)
            if spread is None:
                log.info("  no spread passed filters (%d contracts in chain)", len(chain))
                continue

            slip = self.cfg.execution.entry_slippage
            credit = round(spread.mid_credit - (spread.mid_credit - spread.natural_credit) * slip, 2)
            if credit < cs.min_credit:
                log.info("  credit %.2f below min_credit %.2f", credit, cs.min_credit)
                continue
            qty = spread_size(credit, spread.width, cs, self.cfg.risk.max_contracts, buying_power)
            if qty == 0:
                log.info("  max loss per spread exceeds max_risk_usd / buying power")
                continue

            key = f"{spread.short.symbol}/{spread.long.symbol}"
            log.info("ENTRY sell %s x%d for %.2f credit (width %.2f, max loss $%.0f)", key, qty,
                     credit, spread.width, (spread.width - credit) * 100 * qty)
            res = self.broker.open_credit_spread(spread.short.symbol, spread.long.symbol, qty, credit)
            state.entries_per_symbol[sym] = state.entries_per_symbol.get(sym, 0) + 1
            if res.filled_qty == 0:
                log.info("  not filled, cancelled")
                continue
            fill = res.avg_price or credit
            state.trades += 1
            stats.trades += 1
            stats.open_positions += 1
            buying_power -= (spread.width - fill) * 100 * res.filled_qty
            state.spreads[key] = {
                "underlying": sym, "side": side.value, "short": spread.short.symbol,
                "long": spread.long.symbol, "short_strike": spread.short.strike,
                "qty": res.filled_qty, "credit": fill,
            }
            self.journal.record("OPEN_SPREAD", key, res.filled_qty, fill,
                                f"{side.value} credit spread: {signal.reason}", self.dry_run)
            if entry_block_reason(stats, self.cfg.risk) is not None:
                return

    # ---- long calls/puts entries -----------------------------------------

    def _scan_entries(self, session_open: datetime, today: date, stats: DayStats,
                      buying_power: float) -> None:
        state = self.state
        if (block := entry_block_reason(stats, self.cfg.risk)) is not None:
            log.debug("No new entries: %s", block)
            return

        held = {p.underlying for p in self._managed_positions(today)}
        pending = set() if self.dry_run else {
            occ.underlying for s in self.broker.open_order_symbols() if (occ := parse_occ(s))}
        candidates = [s for s in self.cfg.symbols
                      if s not in held and s not in pending
                      and state.entries_per_symbol.get(s, 0) < self.cfg.strategy.max_entries_per_symbol]
        if not candidates:
            return

        bars = self.broker.minute_bars(candidates, session_open)
        for sym in candidates:
            signal = breakout_signal(bars.get(sym, []), session_open,
                                     self.cfg.schedule.opening_range_minutes, self.cfg.strategy)
            if signal is None:
                continue
            log.info("SIGNAL %s %s: %s", sym, signal.side.value.upper(), signal.reason)

            chain = self.broker.option_chain(sym, today, signal.side, signal.price,
                                             self.cfg.contracts.max_strike_distance_pct + 0.5)
            contract = select_contract(chain, signal.side, signal.price, self.cfg.contracts)
            if contract is None:
                log.info("  no contract passed filters (%d in chain)", len(chain))
                continue

            limit = contract.mid + (contract.ask - contract.mid) * self.cfg.execution.entry_slippage
            qty = position_size(limit, self.cfg.risk, buying_power)
            if qty == 0:
                log.info("  %s @ %.2f is too expensive for max_position_usd", contract.symbol, limit)
                continue

            delta = f"{contract.delta:+.2f}" if contract.delta is not None else "n/a"
            log.info("ENTRY %s x%d limit %.2f (bid %.2f ask %.2f delta %s)", contract.symbol, qty,
                     limit, contract.bid, contract.ask, delta)
            res = self.broker.buy_limit(contract.symbol, qty, limit)

            # Count the attempt even if unfilled so we don't chase the same breakout all day.
            state.entries_per_symbol[sym] = state.entries_per_symbol.get(sym, 0) + 1
            if res.filled_qty == 0:
                log.info("  not filled, cancelled")
                continue
            state.trades += 1
            stats.trades += 1
            stats.open_positions += 1
            fill = res.avg_price or limit
            buying_power -= fill * 100 * res.filled_qty
            state.high_water[contract.symbol] = fill
            if self.dry_run:
                state.paper_positions[contract.symbol] = {"qty": res.filled_qty, "entry": fill}
            self.journal.record("BUY", contract.symbol, res.filled_qty, fill,
                                f"{signal.side.value} breakout: {signal.reason}", self.dry_run)
            if entry_block_reason(stats, self.cfg.risk) is not None:
                return

    # ---- run forever -----------------------------------------------------

    def run(self, exit_after_close: bool = False) -> None:
        mode = "DRY RUN" if self.dry_run else ("PAPER" if self.cfg.paper else "LIVE MONEY")
        log.info("zdte-bot starting [%s] mode=%s symbols=%s", mode, self.cfg.mode,
                 ",".join(self.cfg.symbols))
        errors = 0
        while True:
            try:
                wait = self.tick()
                errors = 0
                if exit_after_close and self._session_seen and not self._market_open:
                    log.info("Market closed for the day; exiting.")
                    return
            except APIError as e:
                errors += 1
                wait = min(300, 10 * 2 ** min(errors, 5))
                log.error("Alpaca API error: %s (retrying in %ds)", e, wait)
            except (ConnectionError, TimeoutError, OSError) as e:
                errors += 1
                wait = min(300, 10 * 2 ** min(errors, 5))
                log.error("Network error: %s (retrying in %ds)", e, wait)
            except Exception:  # keep running so open positions still get managed/flattened
                errors += 1
                wait = min(300, 10 * 2 ** min(errors, 5))
                log.exception("Unexpected error (retrying in %ds)", wait)
            _time.sleep(wait)
