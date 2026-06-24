"""Combine carry + trap into a ranked CoinSignal.

risk_adjusted_apr = total_carry_apr * (1 - trap.composite_score), defined only
for tradable ("OK") coins. Excluded and carry-unavailable coins still carry a
full breakdown but sort below tradable ones.
"""
from __future__ import annotations

import math
from typing import Optional

from ..config import BasisArbConfig
from ..models import CarryEstimate, CoinRawInput, CoinSignal, TrapBreakdown


def build_signal(raw: CoinRawInput, carry: CarryEstimate, trap: TrapBreakdown, cfg: BasisArbConfig) -> CoinSignal:
    if trap.excluded:
        status = "EXCLUDED"
        risk_adjusted: Optional[float] = None
        top_reason = trap.exclusion_reasons[0] if trap.exclusion_reasons else "excluded by trap score"
    elif carry.total_carry_apr is None:
        status = "DATA_INSUFFICIENT" if trap.insufficient_trap_data else "CARRY_UNAVAILABLE"
        risk_adjusted = None
        top_reason = carry.unavailable_reason or "carry unavailable"
    else:
        status = "OK"
        risk_adjusted = carry.total_carry_apr * (1.0 - trap.composite_score)
        top_reason = _top_reason(carry, trap)

    return CoinSignal(
        coin=raw.coin, status=status, raw=raw, carry=carry, trap=trap,
        risk_adjusted_apr=risk_adjusted, top_reason=top_reason,
    )


def _top_reason(carry: CarryEstimate, trap: TrapBreakdown) -> str:
    available = [s for s in trap.subsignals() if s.available]
    top = max(available, key=lambda s: s.score, default=None)
    carry_txt = f"carry {carry.total_carry_apr * 100:.1f}% APR" if carry.total_carry_apr is not None else "carry n/a"
    if top is not None and top.score >= 0.4:
        return f"{carry_txt}; watch: {top.reason}"
    return f"{carry_txt}; low trap signature"


def _sort_key(s: CoinSignal):
    if s.status == "OK":
        return (0, -(s.risk_adjusted_apr if s.risk_adjusted_apr is not None else -math.inf))
    if s.status == "EXCLUDED":
        return (2, -s.trap.composite_score)
    rank = s.raw.loris_oi_rank if s.raw.loris_oi_rank is not None else 10 ** 9
    return (1, rank, -s.trap.composite_score)


def rank_signals(signals: list[CoinSignal]) -> list[CoinSignal]:
    ordered = sorted(signals, key=_sort_key)
    for i, sig in enumerate(ordered, start=1):
        sig.rank = i
    return ordered
