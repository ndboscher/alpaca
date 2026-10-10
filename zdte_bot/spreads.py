"""0DTE credit spreads placed on the far side of the opening range.

Modelled on the widely published "60-minute opening range breakout" 0DTE bot: when price
breaks out ABOVE the range, sell a put spread with the short strike at the range LOW; when it
breaks out BELOW, sell a call spread with the short strike at the range HIGH. The trade wins
as long as price doesn't fully reverse through the whole opening range by the close.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import ContractsCfg, CreditSpreadCfg
from .models import OptionCandidate, Side
from .risk import CONTRACT_MULTIPLIER
from .strategy import Signal


@dataclass(frozen=True)
class CreditSpread:
    short: OptionCandidate
    long: OptionCandidate

    @property
    def side(self) -> Side:
        """PUT for a bull put spread, CALL for a bear call spread."""
        return self.short.side

    @property
    def width(self) -> float:
        return abs(self.short.strike - self.long.strike)

    @property
    def mid_credit(self) -> float:
        return self.short.mid - self.long.mid

    @property
    def natural_credit(self) -> float:
        """Credit if we hit both bids/asks: the worst reasonable fill."""
        return self.short.bid - self.long.ask


def spread_side_for(signal: Signal) -> Side:
    """Bullish breakout -> sell puts below; bearish breakout -> sell calls above."""
    return Side.PUT if signal.side == Side.CALL else Side.CALL


def select_credit_spread(chain: list[OptionCandidate], signal: Signal, cfg: CreditSpreadCfg,
                         contracts: ContractsCfg) -> CreditSpread | None:
    side = spread_side_for(signal)
    opts = sorted((c for c in chain if c.side == side and c.bid > 0 and c.ask >= c.bid),
                  key=lambda c: c.strike)
    if not opts:
        return None

    if side == Side.PUT:
        # Short put at or just below the range low, long put `width` further down.
        shorts = [c for c in opts if c.strike <= signal.range.low]
        if not shorts:
            return None
        short = shorts[-1]
        longs = [c for c in opts if c.strike <= short.strike - cfg.width + 1e-9]
        long = longs[-1] if longs else None
    else:
        shorts = [c for c in opts if c.strike >= signal.range.high]
        if not shorts:
            return None
        short = shorts[0]
        longs = [c for c in opts if c.strike >= short.strike + cfg.width - 1e-9]
        long = longs[0] if longs else None

    if long is None or short.spread_pct > contracts.max_spread_pct:
        return None
    spread = CreditSpread(short, long)
    if spread.mid_credit < cfg.min_credit or spread.mid_credit >= spread.width:
        return None
    return spread


def spread_size(credit: float, width: float, cfg: CreditSpreadCfg, max_contracts: int,
                buying_power: float) -> int:
    """Contracts such that the worst-case loss (width - credit) stays within max_risk_usd."""
    max_loss = (width - credit) * CONTRACT_MULTIPLIER
    if max_loss <= 0:
        return 0
    # Alpaca holds the full spread width as collateral (minus credit).
    budget = min(cfg.max_risk_usd, buying_power)
    return max(0, min(max_contracts, math.floor(budget / max_loss)))


def spread_exit_reason(credit: float, cost_to_close: float, spot: float | None, side: Side,
                       short_strike: float, cfg: CreditSpreadCfg) -> str | None:
    """Exit rules for a short vertical. Prices are per-share; `credit` is what we received."""
    if credit <= 0:
        return None
    captured = (credit - cost_to_close) / credit * 100
    if captured >= cfg.profit_target_pct:
        return f"take profit ({captured:.0f}% of credit kept)"
    if -captured >= cfg.stop_loss_pct:
        return f"stop loss (loss = {-captured:.0f}% of credit)"
    if cfg.exit_on_short_strike_breach and spot is not None:
        if (side == Side.PUT and spot < short_strike) or (side == Side.CALL and spot > short_strike):
            return f"underlying {spot:.2f} breached short strike {short_strike:g}"
    return None
