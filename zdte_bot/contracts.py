"""Choosing which 0DTE contract to buy from the chain."""

from __future__ import annotations

from .config import ContractsCfg
from .models import OptionCandidate, Side


def select_contract(candidates: list[OptionCandidate], side: Side, spot: float,
                    cfg: ContractsCfg) -> OptionCandidate | None:
    """Pick the liquid contract closest to the target delta.

    Contracts are filtered by strike distance, premium range and bid/ask spread. If the
    feed has no greeks, we fall back to the slightly out-of-the-money strike nearest spot.
    """
    max_dist = spot * cfg.max_strike_distance_pct / 100
    usable = [
        c for c in candidates
        if c.side == side
        and abs(c.strike - spot) <= max_dist
        and c.bid > 0 and c.ask > c.bid
        and cfg.min_premium <= c.ask <= cfg.max_premium
        and c.spread_pct <= cfg.max_spread_pct
    ]
    if not usable:
        return None

    with_delta = [c for c in usable if c.delta is not None]
    if with_delta:
        return min(with_delta, key=lambda c: (abs(abs(c.delta) - cfg.target_delta), c.spread_pct))

    def otm_distance(c: OptionCandidate) -> float:
        d = c.strike - spot if side == Side.CALL else spot - c.strike
        # Prefer the first strike at/just beyond spot; penalise in-the-money strikes.
        return d if d >= 0 else abs(d) + max_dist

    return min(usable, key=otm_distance)
