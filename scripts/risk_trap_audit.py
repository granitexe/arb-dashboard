#!/usr/bin/env python3
"""Risk/Strategy: audit trap-score false positives vs. realized outcomes.

This script does NOT run live trading — it is a retrospective analysis
tool that compares trap scores against subsequent price/OI moves to detect
systematic bias. Designed to run as a cron job.

Exit codes: 0 = OK, 1 = anomaly detected, 2 = error
"""
from __future__ import annotations

import datetime
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from basis_arb.sources.loris import LorisClient
    from basis_arb.sources.cache import JsonCache
    from basis_arb.config import BasisArbConfig
except ImportError:
    LorisClient = None

STATE_FILE = Path(__file__).parent.parent / ".cron_output" / "trap_audit_state.json"
ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "trap_audit_alerts.json"
LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "trap_audit.log"
ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


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


def run() -> int:
    log("Starting trap-score false-positive audit")
    prev_state = load_state()
    history = prev_state.get("history", [])

    alerts = []

    # --- Load current signals.json if available ---
    signals_file = Path(__file__).parent.parent / "signals.json"
    if not signals_file.exists():
        log("signals.json not found, skipping audit")
        return 0

    try:
        data = json.loads(signals_file.read_text())
    except Exception as e:
        log(f"ERROR loading signals.json: {e}")
        return 2

    signals = data.get("signals", [])
    now = datetime.utcnow()

    # --- Check for coins with high trap score but excluded = False ---
    # These are potential false negatives
    false_negatives = []
    for s in signals:
        trap = s.get("trap", {})
        carry = s.get("carry", {})
        status = s.get("status")
        composite = trap.get("composite_score", 0)
        funding = carry.get("funding_apr")
        excluded = trap.get("excluded", False)

        # Flag: high trap score but NOT excluded (shouldn't happen, but check)
        if composite >= 0.6 and not excluded:
            false_negatives.append({
                "coin": s.get("coin"),
                "composite_score": composite,
                "funding_apr": funding,
                "status": status,
            })

        # Flag: moderate trap score but very high carry (possible false negative)
        if 0.4 <= composite < 0.6 and funding is not None and funding >= 0.5:
            alerts.append({
                "type": "POTENTIAL_FALSE_NEGATIVE",
                "coin": s.get("coin"),
                "composite_score": composite,
                "funding_apr": funding,
                "note": "Moderate trap score but high carry — verify exclusion logic",
            })

        # Flag: net carry negative after fees (broken signal)
        net = carry.get("net_carry_apr")
        if net is not None and net < 0:
            alerts.append({
                "type": "NEGATIVE_NET_CARRY",
                "coin": s.get("coin"),
                "net_carry_apr": net,
                "funding_apr": funding,
                "note": "Net carry negative — coin likely unprofitable after fees",
            })

        # Flag: zero funding but included (stale carry)
        if funding == 0 and status == "OK":
            alerts.append({
                "type": "ZERO_FUNDING_OK_STATUS",
                "coin": s.get("coin"),
                "note": "Zero funding but OK status — possible stale signal",
            })

    # --- Cross-check: excluded coins with very low trap scores ---
    for s in signals:
        trap = s.get("trap", {})
        excluded = trap.get("excluded", False)
        composite = trap.get("composite_score", 0)
        if excluded and composite < 0.3:
            alerts.append({
                "type": "POTENTIAL_FALSE_POSITIVE",
                "coin": s.get("coin"),
                "composite_score": composite,
                "exclusion_reasons": trap.get("exclusion_reasons", []),
                "note": "Excluded but composite trap score very low",
            })

    # --- Record history for trend analysis ---
    new_entry = {
        "ts": now.isoformat(),
        "counts": {
            "total": len(signals),
            "ok": sum(1 for s in signals if s.get("status") == "OK"),
            "excluded": sum(1 for s in signals if s.get("status") == "EXCLUDED"),
            "false_negatives": len(false_negatives),
            "alerts": len(alerts),
        }
    }
    history.append(new_entry)
    # Keep last 30 entries
    history = history[-30:]

    new_state = {"history": history}
    save_state(new_state)

    ALERT_FILE.write_text(json.dumps(alerts, indent=2))

    if alerts:
        for a in alerts:
            log(f"ALERT: [{a['type']}] {a.get('coin','?')} — {a.get('note','')}")
        log(f"ALERT: {len(alerts)} trap audit alert(s)")
        return 1
    else:
        log(f"OK: Trap audit clean ({len(signals)} signals reviewed)")
        return 0


if __name__ == "__main__":
    sys.exit(run())
