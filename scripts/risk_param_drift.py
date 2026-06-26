#!/usr/bin/env python3
"""Risk/Strategy: parameter drift detector — compare current signal quality vs history.

Tracks signal-rank stability, trap-score calibration, carry-score distribution,
and exclusion rates over time. Also monitors the health of the live trading version.

Drift detection helps answer: is the model getting worse over time (degradation)
or is the market regime changing (external)? Different responses for each.

Exit codes: 0 = OK, 1 = drift detected, 2 = error
"""
from __future__ import annotations
import sys, os
# Auto-activate venv if not already activated
if sys.prefix == sys.base_prefix:
    venv_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".venv", "bin", "python3")
    if os.path.exists(venv_python):
        os.environ["VENV_ACTIVATED"] = "1"
        os.execv(venv_python, [venv_python, __file__] + sys.argv[1:])

import datetime
import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

STATE_FILE = Path(__file__).parent.parent / ".cron_output" / "param_drift_state.json"
ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "param_drift_alerts.json"
LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "param_drift.log"
PERF_FILE = Path(__file__).parent.parent / ".cron_output" / "performance_health.json"
ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
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
    return {"history": [], "version_tag": "v1.0"}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_perf() -> dict:
    if PERF_FILE.exists():
        try:
            return json.loads(PERF_FILE.read_text())
        except Exception:
            pass
    return {}


def pct(v) -> str:
    if v is None:
        return "N/A"
    return f"{v*100:.1f}%"


def std(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    variance = sum((x - mean) ** 2 for x in vals) / len(vals)
    return math.sqrt(variance)


def run() -> int:
    log("Starting parameter drift detection")
    prev_state = load_state()
    history = prev_state.get("history", [])
    perf = load_perf()

    # Load signals
    signals_file = Path(__file__).parent.parent / "signals.json"
    if not signals_file.exists():
        log("signals.json not found, skipping")
        return 0
    try:
        data = json.loads(signals_file.read_text())
    except Exception as e:
        log(f"ERROR: {e}")
        return 2

    signals = data.get("signals", [])
    now = datetime.datetime.utcnow()

    # --- Current snapshot ---
    total = len(signals)
    ok_signals = [s for s in signals if s.get("status") == "OK"]
    excl_signals = [s for s in signals if s.get("status") == "EXCLUDED"]
    ok_count = len(ok_signals)
    excl_count = len(excl_signals)
    excl_pct = excl_count / total if total > 0 else 0.0

    net_carries = [s.get("carry", {}).get("net_carry_apr") for s in ok_signals
                   if s.get("carry", {}).get("net_carry_apr") is not None]
    avg_net_carry = sum(net_carries) / len(net_carries) if net_carries else 0.0
    carry_std = std(net_carries) if len(net_carries) >= 2 else 0.0

    trap_scores = [s.get("trap", {}).get("composite_score", 0) for s in ok_signals]
    avg_trap = sum(trap_scores) / len(trap_scores) if trap_scores else 0.0

    risk_adjs = [s.get("risk_adjusted_apr") for s in ok_signals
                 if s.get("risk_adjusted_apr") is not None]
    avg_risk_adj = sum(risk_adjs) / len(risk_adjs) if risk_adjs else 0.0

    # Top-5 carry coins
    top5 = sorted(
        ok_signals,
        key=lambda s: s.get("carry", {}).get("net_carry_apr") or -999,
        reverse=True,
    )[:5]
    top5_coins = {s.get("coin") for s in top5}

    prev_top5 = set()
    if len(history) >= 1:
        prev_top5 = set(history[-1].get("top5_coins", []))
    rank_churn = len(top5_coins - prev_top5) if prev_top5 else 0

    # Exclusion rate
    excl_rates = [h.get("excl_pct", 0) for h in history[-14:] if h.get("excl_pct") is not None]
    avg_excl_hist = sum(excl_rates) / len(excl_rates) if excl_rates else 0.0

    snapshot = {
        "ts": now.isoformat(),
        "version_tag": perf.get("version_tag", prev_state.get("version_tag", "v1.0")),
        "total": total,
        "ok_count": ok_count,
        "excl_pct": excl_pct,
        "avg_net_carry": avg_net_carry,
        "carry_std": carry_std,
        "avg_trap_score": avg_trap,
        "avg_risk_adj": avg_risk_adj,
        "top5_coins": list(top5_coins),
        "rank_churn": rank_churn,
    }
    history.append(snapshot)
    history = history[-60:]  # keep 60 snapshots (~30 days at 2x/day)

    save_state({**prev_state, "history": history})
    alerts = []

    # --- Drift detection (only when we have enough history) ---
    if len(history) >= 7:
        recent = history[-7:]
        older = history[-14:-7] if len(history) >= 14 else history[:7]

        def avg(hlist, field):
            vals = [h.get(field) for h in hlist if h.get(field) is not None]
            return sum(vals) / len(vals) if vals else 0.0

        recent_excl = avg(recent, "excl_pct")
        older_excl = avg(older, "excl_pct")
        recent_carry = avg(recent, "avg_net_carry")
        older_carry = avg(older, "avg_net_carry")
        recent_trap = avg(recent, "avg_trap_score")
        older_trap = avg(older, "avg_trap_score")

        # Exclusion rate drift
        if older_excl > 0.01 and recent_excl > older_excl * 1.2:
            alerts.append({
                "type": "EXCLUSION_RATE_DRIFT",
                "direction": "increase",
                "recent_excl_pct": recent_excl,
                "older_excl_pct": older_excl,
                "change_pct": (recent_excl / older_excl - 1) * 100,
                "hypothesis": "market_regime" if recent_carry > older_carry else "model_degradation",
                "note": f"Exclusion rate up {((recent_excl/older_excl)-1)*100:.0f}% — check trap thresholds",
            })
            log(f"DRIFT: Exclusion rate {older_excl*100:.1f}% → {recent_excl*100:.1f}%")

        # Carry score drift
        if older_carry > 0.001 and recent_carry < older_carry * 0.7:
            alerts.append({
                "type": "CARRY_SCORE_DRIFT",
                "direction": "decrease",
                "recent_avg_carry": recent_carry,
                "older_avg_carry": older_carry,
                "change_pct": (1 - recent_carry / older_carry) * 100,
                "hypothesis": "market_regime" if recent_excl > older_excl else "data_staleness",
                "note": f"Carry down {((1 - recent_carry/older_carry))*100:.0f}% — regime change or stale data",
            })
            log(f"DRIFT: Carry {older_carry*100:.2f}% → {recent_carry*100:.2f}%")

        # Trap score drift (if traps are getting higher on OK signals)
        if older_trap > 0.1 and recent_trap > older_trap * 1.3:
            alerts.append({
                "type": "TRAP_SCORE_DRIFT",
                "direction": "increase",
                "recent_avg_trap": recent_trap,
                "older_avg_trap": older_trap,
                "note": "More coins passing filters with higher trap scores — check threshold calibration",
            })
            log(f"DRIFT: Trap avg {older_trap:.3f} → {recent_trap:.3f}")

        # Rank instability
        if rank_churn >= 3:
            new_coins = list(top5_coins - prev_top5)
            alerts.append({
                "type": "RANK_INSTABILITY",
                "rank_churn": rank_churn,
                "new_top5": new_coins,
                "note": "Top-5 carry coins changed significantly — regime shift or data issue",
            })
            log(f"DRIFT: Rank instability — {rank_churn} new coins: {new_coins}")

        # Carry variance change (signals becoming less stable)
        recent_carry_std = std([h.get("avg_net_carry", 0) for h in recent])
        older_carry_std = std([h.get("avg_net_carry", 0) for h in older])
        if older_carry_std > 0 and recent_carry_std > older_carry_std * 1.5:
            alerts.append({
                "type": "CARRY_VARIANCE_INCREASE",
                "recent_std": recent_carry_std,
                "older_std": older_carry_std,
                "note": "Carry scores becoming more volatile — signals less reliable",
            })

    ALERT_FILE.write_text(json.dumps(alerts, indent=2, default=str))

    if alerts:
        log(f"DRIFT: {len(alerts)} alert(s)")
        return 1
    else:
        log(f"OK: {total} signals, {ok_count} OK, excl={pct(excl_pct)}, carry={pct(avg_net_carry)}, rank_churn={rank_churn}")
        return 0


if __name__ == "__main__":
    sys.exit(run())
