"""Append-only CSV record of every order the bot places, plus small per-day state."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

FIELDS = ["time", "action", "symbol", "qty", "price", "reason", "dry_run"]


class Journal:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, action: str, symbol: str, qty: int, price: float | None, reason: str,
               dry_run: bool) -> None:
        new = not self.path.exists()
        with self.path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow({
                "time": datetime.now().isoformat(timespec="seconds"),
                "action": action, "symbol": symbol, "qty": qty,
                "price": "" if price is None else f"{price:.2f}",
                "reason": reason, "dry_run": dry_run,
            })


@dataclass
class DayState:
    """What the bot has done today. Persisted so a restart doesn't re-trade the same setup."""
    day: str
    trades: int = 0
    entries_per_symbol: dict[str, int] = field(default_factory=dict)
    high_water: dict[str, float] = field(default_factory=dict)
    # Dry-run only: simulated positions {option_symbol: {"qty": n, "entry": price}}
    paper_positions: dict[str, dict] = field(default_factory=dict)
    # Credit spreads the bot opened: {"SHORT/LONG": {underlying, side, short, long,
    # short_strike, qty, credit}}. Alpaca reports legs separately, so we pair them here.
    spreads: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, folder: Path, today: date) -> "DayState":
        path = folder / f"state-{today.isoformat()}.json"
        if path.exists():
            return cls(**json.loads(path.read_text()))
        return cls(day=today.isoformat())

    def save(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"state-{self.day}.json").write_text(json.dumps(asdict(self), indent=2))
