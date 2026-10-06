"""Plain data types shared across the bot, independent of the Alpaca SDK."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class Side(str, Enum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OptionCandidate:
    symbol: str
    underlying: str
    expiration: date
    side: Side
    strike: float
    bid: float
    ask: float
    delta: float | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100 if self.mid > 0 else float("inf")


_OCC_RE = re.compile(r"^([A-Z.]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


@dataclass(frozen=True)
class OccSymbol:
    underlying: str
    expiration: date
    side: Side
    strike: float


def parse_occ(symbol: str) -> OccSymbol | None:
    """Parse an OCC option symbol like SPY251006C00570000. Returns None if it isn't one."""
    m = _OCC_RE.match(symbol.replace(" ", ""))
    if not m:
        return None
    root, yy, mm, dd, cp, strike = m.groups()
    return OccSymbol(
        underlying=root,
        expiration=date(2000 + int(yy), int(mm), int(dd)),
        side=Side.CALL if cp == "C" else Side.PUT,
        strike=int(strike) / 1000,
    )
