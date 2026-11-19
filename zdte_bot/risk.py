"""Position sizing, entry gating and exit rules."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import time

from .config import ProfitWindowCfg, RiskCfg

CONTRACT_MULTIPLIER = 100
STOP_PREFIX = "stop loss"  # exit reasons starting with this are subject to stop_confirm_checks


def position_size(price: float, cfg: RiskCfg, buying_power: float) -> int:
    """Number of contracts to buy at `price` (per-share premium)."""
    if price <= 0:
        return 0
    cost = price * CONTRACT_MULTIPLIER
    budget = min(cfg.max_position_usd, buying_power)
    return max(0, min(cfg.max_contracts, math.floor(budget / cost)))


@dataclass
class DayStats:
    trades: int = 0
    open_positions: int = 0
    pnl: float = 0.0  # today's account P&L (equity - last_equity)


def entry_block_reason(stats: DayStats, cfg: RiskCfg) -> str | None:
    """Why a new entry is not allowed right now, or None if it is."""
    if stats.pnl <= -abs(cfg.max_daily_loss_usd):
        return f"daily loss limit hit ({stats.pnl:.2f})"
    if stats.trades >= cfg.max_trades_per_day:
        return f"max trades per day reached ({stats.trades})"
    if stats.open_positions >= cfg.max_open_positions:
        return f"max open positions reached ({stats.open_positions})"
    return None


def exit_reason(entry: float, current: float, high_water: float, cfg: RiskCfg) -> str | None:
    """Decide whether to exit a long option position. Prices are per-share premiums."""
    if entry <= 0 or current <= 0:
        return None
    change = (current - entry) / entry * 100
    if change >= cfg.take_profit_pct:
        return f"take profit ({change:+.1f}%)"
    if change <= -cfg.stop_loss_pct:
        return f"{STOP_PREFIX} ({change:+.1f}%)"
    peak_gain = (high_water - entry) / entry * 100
    if peak_gain >= cfg.trailing_activate_pct:
        drawdown = (high_water - current) / high_water * 100
        if drawdown >= cfg.trailing_stop_pct:
            return f"trailing stop ({change:+.1f}%, {drawdown:.1f}% off high)"
    return None


def window_exit_reason(pnl_pct: float, now: time, cfg: ProfitWindowCfg) -> str | None:
    """Midday profit-window rule. `pnl_pct` is the position's gain as % of entry (or credit)."""
    if not cfg.enabled or now < cfg.start:
        return None
    if now >= cfg.end and cfg.close_all_at_end:
        return f"profit window closed at {cfg.end:%H:%M} ({pnl_pct:+.1f}%)"
    if pnl_pct >= cfg.min_profit_pct:
        return f"lock in profit after {cfg.start:%H:%M} ({pnl_pct:+.1f}%)"
    return None


def underlying_stop_reason(side: str, level: float, close: float) -> str | None:
    """`side` is the option bought ("call"/"put"); `close` the ETF's last completed 1-min close."""
    if side == "call" and close < level:
        return f"breakout failed: ETF closed {close:.2f} below {level:.2f}"
    if side == "put" and close > level:
        return f"breakout failed: ETF closed {close:.2f} above {level:.2f}"
    return None
