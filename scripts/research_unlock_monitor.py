#!/usr/bin/env python3
"""Monitor upcoming token unlocks from DeFiLlama.

Alerts when a tracked token has a large unlock within 30 days,
especially if the unlock represents >2% of circulating supply.
Designed to run as a cron job.
Exit codes: 0 = OK, 1 = alert, 2 = error
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from basis_arb.sources.defillama import DefiLlamaClient
    from basis_arb.sources.cache import JsonCache
    from basis_arb.config import BasisArbConfig
except ImportError:
    DefiLlamaClient = None  # type: ignore

ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "unlock_alerts.json"
STATE_FILE = Path(__file__).parent.parent / ".cron_output" / "unlock_state.json"
LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "unlock_monitor.log"
ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)

# Coins to track (add/remove as needed)
TRACKED_COINS = [
    "BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "MATIC", "APT",
    "SUI", "SEI", "W", "JTO", "JUP", "ENA", "BERN", "WLD",
]


def log(msg: str) -> None:
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def run() -> int:
    log("Starting unlock monitor")
    now = datetime.now(timezone.utc)
    alerts: list[dict] = []
    events_logged: list[str] = []

    if DefiLlamaClient is None:
        log("WARN: defillama module not available")
        return 0

    cfg = BasisArbConfig(unlock_horizon_days=90)
    cache = JsonCache(cfg.cache_dir, enabled=False)
    dl = DefiLlamaClient(cfg, cache)

    targets = {c: c.lower() for c in TRACKED_COINS}
    snap, meta = dl.fetch_unlocks(targets)

    for coin in TRACKED_COINS:
        events = snap.events_by_coin.get(coin, [])
        for ev in events:
            if ev.timestamp is None:
                continue
            days_until = (ev.timestamp - now).total_seconds() / 86400.0
            if days_until < 0 or days_until > cfg.unlock_horizon_days:
                continue

            pct_circ = ev.pct_circulating_supply
            size_str = f"{pct_circ*100:.1f}% circ" if pct_circ is not None else f"{ev.tokens:.0f} tokens"
            msg = f"{coin}: {size_str} unlock in {days_until:.0f}d ({ev.timestamp.strftime('%Y-%m-%d')}) [{ev.unlock_type or 'unknown'}]"

            # Alert thresholds
            alert_level = None
            if days_until <= 7:
                alert_level = "CRITICAL"
            elif days_until <= 14:
                alert_level = "HIGH"
            elif days_until <= 30 and (pct_circ is None or pct_circ >= 0.02):
                alert_level = "MEDIUM"

            if alert_level:
                alerts.append({
                    "coin": coin,
                    "days_until": round(days_until, 1),
                    "pct_circ": pct_circ,
                    "level": alert_level,
                    "date": ev.timestamp.strftime("%Y-%m-%d"),
                    "unlock_type": ev.unlock_type,
                    "usd_value": ev.usd_value,
                })
                log(f"ALERT [{alert_level}]: {msg}")

    ALERT_FILE.write_text(json.dumps(alerts, indent=2))

    if alerts:
        log(f"ALERT: {len(alerts)} unlock alert(s)")
        return 1
    else:
        log(f"OK: No upcoming unlock alerts ({len(TRACKED_COINS)} coins tracked)")
        return 0


if __name__ == "__main__":
    sys.exit(run())
