"""Opening-range-breakout signal on the underlying ETF.

Pure functions over a list of 1-minute bars for the current session, so the logic
can be unit tested and reasoned about without a broker connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import StrategyCfg
from .models import Bar, Side


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2

    @property
    def width_pct(self) -> float:
        return (self.high - self.low) / self.mid * 100


@dataclass(frozen=True)
class Signal:
    side: Side          # CALL = bullish breakout, PUT = bearish breakout
    price: float
    reason: str
    range: OpeningRange


def opening_range(bars: list[Bar], session_open: datetime, minutes: int) -> OpeningRange | None:
    """High/low of bars in [open, open+minutes). None until that window has fully elapsed."""
    end = session_open + timedelta(minutes=minutes)
    window = [b for b in bars if session_open <= b.ts < end]
    if not window or not any(b.ts >= end for b in bars):
        return None
    return OpeningRange(high=max(b.high for b in window), low=min(b.low for b in window))


def vwap(bars: list[Bar]) -> float | None:
    vol = sum(b.volume for b in bars)
    if vol <= 0:
        return None
    return sum((b.high + b.low + b.close) / 3 * b.volume for b in bars) / vol


def closed_bars(bars: list[Bar], now: datetime) -> list[Bar]:
    """Drop the bar for the minute still in progress, whose close isn't final yet."""
    return [b for b in bars if b.ts + timedelta(minutes=1) <= now]


def stop_level(signal: Signal, mode: str) -> float | None:
    """ETF price that proves the breakout failed: back to the range midpoint or far edge."""
    if mode == "range_mid":
        return signal.range.mid
    if mode == "range_far_edge":
        return signal.range.low if signal.side == Side.CALL else signal.range.high
    return None


def breakout_signal(bars: list[Bar], session_open: datetime, range_minutes: int,
                    cfg: StrategyCfg, min_range_pct: float = 0.0,
                    max_range_pct: float | None = None) -> Signal | None:
    """Return a CALL/PUT signal if the latest completed bar closes outside the opening range.

    `min_range_pct`/`max_range_pct` skip days whose range is too tight (no real move to
    break out of) or too wide (choppy, gappy open). `max_range_pct` defaults to cfg's.
    """
    session = [b for b in bars if b.ts >= session_open]
    rng = opening_range(session, session_open, range_minutes)
    if rng is None:
        return None
    max_pct = cfg.max_range_pct if max_range_pct is None else max_range_pct
    if not min_range_pct <= rng.width_pct <= max_pct:
        return None

    last = session[-1]
    buffer = rng.mid * cfg.breakout_buffer_pct / 100
    vw = vwap(session)
    # The last `confirm_bars` closes must all be beyond the level, and all after the range.
    recent = session[-cfg.confirm_bars:]
    range_end = session_open + timedelta(minutes=range_minutes)
    if len(recent) < cfg.confirm_bars or recent[0].ts < range_end:
        return None

    if all(b.close > rng.high + buffer for b in recent):
        if cfg.require_vwap_confirm and vw is not None and last.close <= vw:
            return None
        return Signal(Side.CALL, last.close,
                      f"close {last.close:.2f} > OR high {rng.high:.2f} (vwap {vw or 0:.2f})", rng)
    if all(b.close < rng.low - buffer for b in recent):
        if cfg.require_vwap_confirm and vw is not None and last.close >= vw:
            return None
        return Signal(Side.PUT, last.close,
                      f"close {last.close:.2f} < OR low {rng.low:.2f} (vwap {vw or 0:.2f})", rng)
    return None
