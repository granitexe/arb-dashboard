"""Output rendering: a hand-rolled fixed-width stdout table and a JSON report.

No third-party table/serialization deps. The JSON contains full per-coin
breakdowns plus run metadata so the output is self-describing and auditable.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional

from .config import BasisArbConfig
from .models import CoinSignal, RunReport, to_jsonable

SCHEMA_VERSION = 1


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _ratiox(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.1f}x"


def _score(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _trunc(s: str, width: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= width else s[: width - 1] + "\u2026"


# (header, width, getter)
_Column = tuple[str, int, Callable[[CoinSignal], str]]


def _columns(reason_width: int) -> list[_Column]:
    return [
        ("Rank", 4, lambda s: str(s.rank or "")),
        ("Coin", 10, lambda s: _trunc(s.coin, 10)),
        ("Status", 17, lambda s: s.status),
        ("NetCarryAPR", 12, lambda s: _pct(s.carry.net_carry_apr)),
        ("RiskAdjAPR", 10, lambda s: _pct(s.risk_adjusted_apr)),
        ("CarryAPR", 9, lambda s: _pct(s.carry.total_carry_apr)),
        ("FundAPR", 9, lambda s: _pct(s.carry.funding_apr)),
        ("BasisAPR", 9, lambda s: _pct(s.carry.basis_apr)),
        ("Trap", 5, lambda s: _score(s.trap.composite_score)),
        ("Unlk", 5, lambda s: _score(s.trap.upcoming_unlocks.score)),
        ("OI/Vol", 7, lambda s: _ratiox(s.trap.spot_illiquidity_to_perp_oi.raw_value)),
        ("OI/MC", 7, lambda s: _pct(s.trap.oi_market_cap_distortion.raw_value)),
        ("Lead", 5, lambda s: _score(s.trap.spot_leading_perp.score)),
        ("Short", 9, lambda s: _trunc(s.carry.selected_short_venue or "-", 9)),
        ("TopReason", reason_width, lambda s: _trunc(s.top_reason, reason_width)),
    ]


def render_table(report: RunReport, cfg: BasisArbConfig) -> str:
    cols = _columns(cfg.reason_width)
    rows = [s for s in report.signals if cfg.show_excluded or s.status != "EXCLUDED"]
    rows = rows[: cfg.max_table_rows]

    header = "  ".join(h.ljust(w) for h, w, _ in cols)
    sep = "  ".join("-" * w for _, w, _ in cols)
    lines = [header, sep]
    for s in rows:
        lines.append("  ".join(_trunc(get(s), w).ljust(w) for _, w, get in cols))

    ok = sum(1 for s in report.signals if s.status == "OK")
    excl = sum(1 for s in report.signals if s.status == "EXCLUDED")
    footer = (
        f"\n{len(report.signals)} coins | {ok} tradable | {excl} excluded (trap) | "
        f"LORIS_API_KEY={'set' if report.key_present.get('LORIS_API_KEY') else 'absent'}"
    )
    src = "  ".join(
        f"{name}:{'ok' if m.ok else 'FAIL'}{'(stale)' if m.stale else ''}"
        for name, m in report.sources.items()
    )
    return "\n".join(lines) + footer + "\n" + "sources: " + src


def build_json_report(report: RunReport) -> dict:
    return {
        "tool": "basis_arb",
        "schema_version": SCHEMA_VERSION,
        "generated_at": to_jsonable(report.generated_at),
        "disclaimer": "Signals only. This tool does not place orders or touch a wallet.",
        "key_present": report.key_present,
        "config": to_jsonable(report.config_snapshot),
        "sources": to_jsonable(report.sources),
        "signals": [_signal_json(s) for s in report.signals],
    }


def _signal_json(s: CoinSignal) -> dict:
    trap = s.trap
    return {
        "rank": s.rank,
        "coin": s.coin,
        "carry": to_jsonable(s.carry),
        "risk_adjusted_apr": to_jsonable(s.risk_adjusted_apr),
        "top_reason": s.top_reason,
        "trap": {
            "composite_score": trap.composite_score,
            "excluded": trap.excluded,
            "exclusion_reasons": trap.exclusion_reasons,
            "unlock_data_missing": trap.unlock_data_missing,
            "insufficient_trap_data": trap.insufficient_trap_data,
            "weights_used": trap.weights_used,
            "subsignals": {sub.name: to_jsonable(sub) for sub in trap.subsignals()},
        },
        "raw": to_jsonable(s.raw),
    }


def write_json_report(report: RunReport, path: str) -> str:
    payload = build_json_report(report)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False)
    return path
