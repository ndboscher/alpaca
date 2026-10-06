"""The main trading loop."""

from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError

from .broker import Broker, OptionPosition
from .config import Config
from .contracts import select_contract
from .journal import DayState, Journal
from .models import parse_occ
from .risk import DayStats, entry_block_reason, exit_reason, position_size
from .strategy import breakout_signal

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
CLOSE_BUFFER = timedelta(minutes=15)  # on early-close days, flatten this long before the bell


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

    # ---- helpers ---------------------------------------------------------

    def load_day(self, today: date) -> DayState:
        if self.state is None or self.state.day != today.isoformat():
            self.state = DayState.load(self.state_dir, today)
            self._flattened_today = False
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
        session_open = datetime.combine(today, SESSION_OPEN, tzinfo=ET)
        flatten_at = min(datetime.combine(today, self.cfg.schedule.flatten_at, tzinfo=ET),
                         clock.next_close.astimezone(ET) - CLOSE_BUFFER)
        last_entry = min(datetime.combine(today, self.cfg.schedule.no_new_entries_after, tzinfo=ET),
                         flatten_at)

        positions = self._managed_positions(today)
        account = self.broker.account()
        stats = DayStats(trades=state.trades, open_positions=len(positions), pnl=account.day_pnl)

        # 1. Hard stops: end-of-day flatten and daily loss limit.
        if now >= flatten_at:
            if positions:
                self.flatten(positions, "end-of-day flatten")
            elif not self._flattened_today:
                log.info("Past flatten time (%s). No new trades today.", flatten_at.strftime("%H:%M"))
            self._flattened_today = True
            state.save(self.state_dir)
            return self.cfg.poll_seconds
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
            reason = exit_reason(p.avg_entry, mark, hw, self.cfg.risk)
            log.info("HOLD %s x%d entry %.2f mark %.2f (%+.1f%%)", p.symbol, p.qty, p.avg_entry,
                     mark, (mark - p.avg_entry) / p.avg_entry * 100)
            if reason:
                self._close(p, reason, bid)

        # 3. Look for new entries.
        if now < last_entry:
            self._scan_entries(session_open, today, stats, account.options_buying_power)

        state.save(self.state_dir)
        return self.cfg.poll_seconds

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
        log.info("zdte-bot starting [%s] symbols=%s", mode, ",".join(self.cfg.symbols))
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
