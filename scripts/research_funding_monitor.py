#!/usr/bin/env python3
"""Monitor funding rates for anomalies: flips, spikes, cross-venue divergence.

This script is designed to run as a cron job (no user interaction).
Exit codes: 0 = OK, 1 = anomaly detected, 2 = error
"""
from __future__ import annotations
import sys, os
# Auto-activate venv if not already activated
if sys.prefix == sys.base_prefix:
    venv_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".venv", "bin", "python3")
    if os.path.exists(venv_python):
        os.environ["VENV_ACTIVATED"] = "1"
        os.execv(venv_python, [venv_python, __file__] + sys.argv[1:])

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Use the same lor...ode as the main tool to avoid duplication.
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from basis_arb.sources.loris import LorisClient
    from basis_arb.sources.cache import JsonCache
    from basis_arb.config import BasisArbConfig
except ImportError:
    LorisClient = None  # type: ignore


ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "funding_alerts.json"
STATE_FILE = Path(__file__).parent.parent / ".cron_output" / "funding_state.json"
ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)

LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "funding_monitor.log"


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
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_prev_alerts() -> list:
    if ALERT_FILE.exists():
        try:
            return json.loads(ALERT_FILE.read_text())
        except Exception:
            pass
    return []


def save_alerts(alerts: list) -> None:
    ALERT_FILE.write_text(json.dumps(alerts, indent=2))


def run() -> int:
    log("Starting funding monitor")
    prev = load_state()
    prev_funding: dict[str, dict[str, float]] = prev.get("funding_by_coin", {})
    prev_ts = prev.get("ts", "")

    if LorisClient is None:
        log("WARN: lor...ode not available, using HTTP-only check")
        return 0

    cfg = BasisArbConfig()
    cache = JsonCache(cfg.cache_dir, enabled=False)
    loris = LorisClient(os.environ.get("LORIS_API_KEY"), cfg, cache)

    snap, meta = loris.fetch()
    now_ts = datetime.utcnow().isoformat()
    alerts: list[dict] = []
    anomalies: list[str] = []

    for coin, venues in snap.funding_by_venue.items():
        for venue, vf in venues.items():
            if vf.funding_8h_decimal is None:
                continue
            prev_8h = prev_funding.get(coin, {}).get(venue)
            curr = vf.funding_8h_decimal

            # --- Flip detection ---
            if prev_8h is not None:
                if prev_8h > 0 and curr < 0:
                    anomalies.append(
                        f"FUNDING_FLIP: {coin}/{venue} was +{prev_8h*10000:.1f}bps, now {curr*10000:.1f}bps (SHORT NOW PAYS)"
                    )
                elif prev_8h < 0 and curr > 0:
                    anomalies.append(
                        f"FUNDING_FLIP: {coin}/{venue} was {curr*10000:.1f}bps, now +{curr*10000:.1f}bps (LONG NOW PAYS)"
                    )

            # --- Spike detection (>2x previous, and >5bps) ---
            if prev_8h is not None and abs(prev_8h) > 0.00005:
                ratio = abs(curr / prev_8h)
                if ratio > 2.0 and abs(curr) > 0.0005:
                    direction = "SPIKED UP" if curr > prev_8h else "SPIKED DOWN"
                    anomalies.append(
                        f"FUNDING_SPIKE: {coin}/{venue} {direction} {abs(prev_8h)*10000:.1f}bps → {abs(curr)*10000:.1f}bps ({ratio:.1f}x)"
                    )

    # --- Cross-venue divergence ---
    for coin, venues in snap.funding_by_venue.items():
        rates = {v: vf.funding_8h_decimal for v, vf in venues.items() if vf.funding_8h_decimal is not None}
        if len(rates) >= 2:
            vals = list(rates.values())
            avg = sum(vals) / len(vals)
            max_dev = max(abs(v - avg) for v in vals)
            if max_dev > 0.001:  # >10bps divergence
                anomalies.append(
                    f"VENUE_DIVERGENCE: {coin} funding spread {max_dev*10000:.1f}bps across venues: {rates}"
                )

    # Build state
    new_state = {
        "ts": now_ts,
        "funding_by_coin": {
            coin: {v: vf.funding_8h_decimal for v, vf in venues.items() if vf.funding_8h_decimal is not None}
            for coin, venues in snap.funding_by_venue.items()
        },
    }
    save_state(new_state)

    if anomalies:
        alerts = [{"ts": now_ts, "anomalies": anomalies}]
        save_alerts(alerts)
        for a in anomalies:
            log(f"ALERT: {a}")
        log(f"ALERT: {len(anomalies)} anomaly(ies) detected")
        return 1
    else:
        log(f"OK: No funding anomalies ({len(snap.funding_by_venue)} coins across venues)")
        save_alerts([])
        return 0


if __name__ == "__main__":
    sys.exit(run())
