#!/usr/bin/env python3
"""Risk/Strategy: parameter drift detector — compare current signal quality vs history.

Tracks signal-rank stability, trap-score calibration, and carry-score distribution
over time to detect when the model is drifting (e.g., more coins excluded, lower
carry scores, ranking instability). Designed to run as a cron job.
Exit codes: 0 = OK, 1 = drift detected, 2 = error
"""
from __future__ import annotations

import datetime
import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

STATE_FILE = Path(__file__).parent.parent / ".cron_output" / "param_drift_state.json"
ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "param_drift_alerts.json"
LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "param_drift.log"
ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"history": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def pct(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    return f"{v*100:.1f}%"


def run() -> int:
    log("Starting parameter drift detection")
    prev_state = load_state()
    history = prev_state.get("history", [])

    signals_file = Path(__file__).parent.parent / "signals.json"
    if not signals_file.exists():
        log("signals.json not found, skipping drift detection")
        return 0

    try:
        data = json.loads(signals_file.read_text())
    except Exception as e:
        log(f"ERROR loading signals.json: {e}")
        return 2

    signals = data.get("signals", [])
    now = datetime.utcnow()

    # --- Compute current snapshot ---
    total = len(signals)
    ok_count = sum(1 for s in signals if s.get("status") == "OK")
    excl_count = sum(1 for s in signals if s.get("status") == "EXCLUDED")
    excl_pct = excl_count / total if total > 0 else 0.0

    net_carries = [s.get("carry", {}).get("net_carry_apr")
                   for s in signals if s.get("carry", {}).get("net_carry_apr") is not None]
    avg_net_carry = sum(net_carries) / len(net_carries) if net_carries else 0.0

    trap_scores = [s.get("trap", {}).get("composite_score", 0)
                   for s in signals if s.get("status") == "OK"]
    avg_trap = sum(trap_scores) / len(trap_scores) if trap_scores else 0.0

    risk_adjs = [s.get("risk_adjusted_apr") for s in signals
                 if s.get("risk_adjusted_apr") is not None]
    avg_risk_adj = sum(risk_adjs) / len(risk_adjs) if risk_adjs else 0.0

    # Rank stability: how many coins are in top-5 carry this run vs last run?
    current_top5 = sorted(
        [s for s in signals if s.get("status") == "OK"],
        key=lambda s: s.get("carry", {}).get("net_carry_apr") or -999,
        reverse=True,
    )[:5]
    current_top5_coins = {s.get("coin") for s in current_top5}

    prev_top5_coins = set()
    if len(history) >= 1:
        last = history[-1]
        prev_top5_coins = set(last.get("top5_coins", []))

    rank_churn = len(current_top5_coins - prev_top5_coins) if prev_top5_coins else 0

    snapshot = {
        "ts": now.isoformat(),
        "total": total,
        "ok_count": ok_count,
        "excl_pct": excl_pct,
        "avg_net_carry": avg_net_carry,
        "avg_trap_score": avg_trap,
        "avg_risk_adj": avg_risk_adj,
        "top5_coins": list(current_top5_coins),
        "rank_churn": rank_churn,
    }
    history.append(snapshot)
    history_limited = history[-60:]  # keep 60 data points (~30 days at 2x/day)

    new_state = {"history": history_limited}
    save_state(new_state)

    alerts = []

    # --- Drift detection thresholds ---
    if len(history) >= 7:
        recent = history_limited[-7:]  # last 7 runs
        older = history_limited[-14:-7] if len(history_limited) >= 14 else history_limited[:7]

        def avg_field(hlist, field):
            vals = [h.get(field) for h in hlist if h.get(field) is not None]
            return sum(vals) / len(vals) if vals else 0.0

        recent_excl = avg_field(recent, "excl_pct")
        older_excl = avg_field(older, "excl_pct")

        # Exclusion rate drift: >20% relative increase
        if older_excl > 0.01 and recent_excl > older_excl * 1.2:
            alerts.append({
                "type": "EXCLUSION_RATE_DRIFT",
                "recent_excl_pct": recent_excl,
                "older_excl_pct": older_excl,
                "note": f"Exclusion rate up {((recent_excl/older_excl)-1)*100:.0f}% — check trap thresholds",
            })
            log(f"ALERT: Exclusion rate drift: {older_excl*100:.1f}% → {recent_excl*100:.1f}%")

        recent_carry = avg_field(recent, "avg_net_carry")
        older_carry = avg_field(older, "avg_net_carry")

        # Carry score drift: significant drop
        if older_carry > 0.001 and recent_carry < older_carry * 0.7:
            alerts.append({
                "type": "CARRY_SCORE_DRIFT",
                "recent_avg_net_carry": recent_carry,
                "older_avg_net_carry": older_carry,
                "note": f"Avg carry down {((1 - recent_carry/older_carry))*100:.0f}% — market regime or stale data",
            })
            log(f"ALERT: Carry score drift: {older_carry*100:.2f}% → {recent_carry*100:.2f}%")

        # Rank churn: >3 new coins in top-5
        if rank_churn >= 3:
            alerts.append({
                "type": "RANK_INSTABILITY",
                "rank_churn": rank_churn,
                "new_top5": list(current_top5_coins - prev_top5_coins),
                "note": "Top-5 carry coins changed significantly",
            })
            log(f"ALERT: Rank instability: {rank_churn} new coins in top-5")

    ALERT_FILE.write_text(json.dumps(alerts, indent=2, default=str))

    if alerts:
        log(f"ALERT: {len(alerts)} drift alert(s)")
        return 1
    else:
        log(f"OK: No drift detected ({total} signals, {ok_count} OK, excl_pct={pct(excl_pct)})")
        return 0


if __name__ == "__main__":
    sys.exit(run())
