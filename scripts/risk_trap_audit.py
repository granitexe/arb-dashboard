#!/usr/bin/env python3
"""Risk/Strategy: audit trap-score false positives vs realized outcomes.

Compares historical trap scores against subsequent price/OI moves from the trade
journal to detect systematic bias. Reads:
  - signals.json (current and historical snapshots)
  - ~/.basis_arb/trade_journal.jsonl (realized trades from live trader)

Exit codes: 0 = OK, 1 = anomaly detected, 2 = error
"""
from __future__ import annotations

import datetime
import json
import math
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

STATE_FILE = Path(__file__).parent.parent / ".cron_output" / "trap_audit_state.json"
ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "trap_audit_alerts.json"
LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "trap_audit.log"
PERF_FILE = Path(__file__).parent.parent / ".cron_output" / "performance_health.json"
TRADER_INBOX = Path.home() / ".basis_arb" / "trade_journal.jsonl"
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
    return {"history": [], "trap_predictions": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_trades() -> list[dict]:
    """Load trade journal entries from local file or HTTP feed."""
    entries = []
    if TRADER_INBOX.exists():
        try:
            for line in TRADER_INBOX.read_text().splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
            return entries
        except Exception as e:
            log(f"Error reading {TRADER_INBOX}: {e}")

    feed_url = os.environ.get("TRADEFEED_URL", "")
    if feed_url:
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": "basis-arb-tool/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                entries = data if isinstance(data, list) else data.get("trades", [])
            return entries
        except Exception as e:
            log(f"Error fetching trades: {e}")
    return entries


def run() -> int:
    log("Starting trap-score false-positive audit")
    prev_state = load_state()
    history = prev_state.get("history", [])
    trap_predictions = prev_state.get("trap_predictions", {})
    alerts = []

    # Load current signals
    signals_file = Path(__file__).parent.parent / "signals.json"
    if not signals_file.exists():
        log("signals.json not found, skipping audit")
        return 0
    try:
        signals_data = json.loads(signals_file.read_text())
    except Exception as e:
        log(f"ERROR loading signals.json: {e}")
        return 2

    signals = signals_data.get("signals", [])
    now = datetime.datetime.utcnow()

    # --- Load realized trades for cross-reference ---
    trades = load_trades()
    closed_trades = [t for t in trades if t.get("status") == "closed"]
    coin_outcomes = {}  # coin -> list of realized outcomes
    for t in closed_trades:
        coin = t.get("coin")
        if coin:
            coin_outcomes.setdefault(coin, []).append(t)

    # --- Current snapshot ---
    current_snapshot = {
        "ts": now.isoformat(),
        "total": len(signals),
        "ok": sum(1 for s in signals if s.get("status") == "OK"),
        "excluded": sum(1 for s in signals if s.get("status") == "EXCLUDED"),
    }

    # --- Trap score analysis ---
    false_negatives = []
    false_positives = []
    broken_signals = []

    for s in signals:
        coin = s.get("coin")
        trap = s.get("trap", {})
        carry = s.get("carry", {})
        status = s.get("status")
        composite = trap.get("composite_score", 0)
        funding = carry.get("funding_apr")
        excluded = trap.get("excluded", False)
        net_carry = carry.get("net_carry_apr")

        # Record prediction for future cross-check
        if status == "OK" and coin:
            trap_predictions[coin] = {
                "ts": now.isoformat(),
                "trap_score": composite,
                "net_carry_apr": net_carry,
                "funding_apr": funding,
            }

        # Flag: high trap score but NOT excluded (shouldn't happen — possible bug)
        if composite >= 0.6 and not excluded:
            false_negatives.append({
                "coin": coin,
                "composite_score": composite,
                "funding_apr": funding,
            })

        # Flag: moderate trap but very high carry
        if 0.4 <= composite < 0.6 and funding is not None and funding >= 0.5:
            alerts.append({
                "type": "POTENTIAL_FALSE_NEGATIVE",
                "coin": coin,
                "composite_score": composite,
                "funding_apr": funding,
                "note": "Moderate trap but high carry — verify exclusion logic",
            })

        # Flag: net carry negative after fees (broken signal)
        if net_carry is not None and net_carry < 0:
            broken_signals.append({"coin": coin, "net_carry_apr": net_carry})
            alerts.append({
                "type": "NEGATIVE_NET_CARRY",
                "coin": coin,
                "net_carry_apr": net_carry,
                "note": "Net carry negative — unprofitable after fees",
            })

        # Flag: zero funding but OK status (stale signal)
        if funding == 0 and status == "OK":
            alerts.append({
                "type": "ZERO_FUNDING_OK",
                "coin": coin,
                "note": "Zero funding but OK status — possible stale signal",
            })

        # Cross-reference: excluded coin that later had good outcomes
        if excluded and coin in coin_outcomes:
            realized_pnl = sum(t.get("pnl_usd", 0) for t in coin_outcomes[coin])
            if realized_pnl > 0:
                trap_breakdown = trap.get("exclusion_reasons", [])
                alerts.append({
                    "type": "POSSIBLE_FALSE_POSITIVE_EXCLUSION",
                    "coin": coin,
                    "trap_score": composite,
                    "realized_pnl_usd": realized_pnl,
                    "exclusion_reasons": trap_breakdown,
                    "note": "Excluded coin had positive realized P&L — review exclusion logic",
                })

    # Cross-reference: OK coin that later had bad outcomes
    for s in signals:
        coin = s.get("coin")
        trap = s.get("trap", {})
        if s.get("status") == "OK" and coin in coin_outcomes:
            realized_pnl = sum(t.get("pnl_usd", 0) for t in coin_outcomes[coin])
            composite = trap.get("composite_score", 0)
            if realized_pnl < -50:  # significant loss
                alerts.append({
                    "type": "REALIZED_LOSS_ON_OK_SIGNAL",
                    "coin": coin,
                    "trap_score": composite,
                    "realized_pnl_usd": realized_pnl,
                    "note": "Signal passed filters but lost money — review trap model calibration",
                })

    # --- Update history ---
    history.append(current_snapshot)
    history = history[-30:]  # keep 30 snapshots

    # Compact trap_predictions (keep last 30 days per coin)
    cutoff = (now - datetime.timedelta(days=30)).isoformat()
    trap_predictions = {
        c: p for c, p in trap_predictions.items()
        if p.get("ts", "") > cutoff
    }

    save_state({
        "history": history,
        "trap_predictions": trap_predictions,
    })

    ALERT_FILE.write_text(json.dumps(alerts, indent=2, default=str))

    if alerts:
        for a in alerts:
            log(f"ALERT: [{a['type']}] {a.get('coin','?')} — {a.get('note','')}")
        log(f"ALERT: {len(alerts)} trap audit alert(s)")
        return 1
    else:
        log(f"OK: Trap audit clean ({len(signals)} signals, {len(closed_trades)} closed trades reviewed)")
        return 0


if __name__ == "__main__":
    sys.exit(run())
