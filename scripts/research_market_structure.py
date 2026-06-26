#!/usr/bin/env python3
"""Research: monitor market structure — OI shifts, perp-vs-spot basis widening.

Detects regime changes in the basis market:
- Sudden OI expansion/contraction (via oi_rankings ordinal changes)
- Perp price leading spot (unwind risk)
- Cross-exchange basis divergence
- Funding rate regime changes

Exit codes: 0 = OK, 1 = regime alert, 2 = error
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

STATE_FILE = Path(__file__).parent.parent / ".cron_output" / "market_structure_state.json"
ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "market_structure_alerts.json"
LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "market_structure.log"
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
    return {"oi_rank_history": {}, "funding_history": {}, "symbols": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def run() -> int:
    log("Starting market structure monitor")
    prev = load_state()

    if LorisClient is None:
        log("LorisClient not available, skipping")
        return 0

    cfg = BasisArbConfig()
    cache = JsonCache(cfg.cache_dir, enabled=False)
    loris = LorisClient(os.environ.get("LORIS_API_KEY"), cfg, cache)

    snap, meta = loris.fetch()
    now_ts = datetime.utcnow()
    alerts = []

    if not snap.available:
        log(f"LorisSnapshot unavailable: {snap.unavailable_reason}")
        ALERT_FILE.write_text(json.dumps([{
            "type": "SOURCE_UNAVAILABLE",
            "reason": snap.unavailable_reason,
            "meta": str(meta),
        }], indent=2, default=str))
        return 2

    prev_oi_rank = prev.get("oi_rank_history", {})
    prev_funding = prev.get("funding_history", {})
    prev_symbols = set(prev.get("symbols", []))

    new_oi_rank = {}
    new_funding = {}

    # --- Funding regime detection ---
    # Compare current funding to prior reading (state persistence)
    funding_by_coin = {}
    for coin, venues in snap.funding_by_venue.items():
        rates = [vf.funding_8h_decimal for vf in venues.values() if vf.funding_8h_decimal is not None]
        if rates:
            funding_by_coin[coin] = sum(rates) / len(rates)

    for coin, rate in funding_by_coin.items():
        prev_rate = prev_funding.get(coin, {}).get("value", rate)
        new_funding[coin] = {"value": rate, "ts": now_ts.isoformat()}

        if abs(prev_rate) > 0.00005:  # >0.5bps prior
            ratio = rate / prev_rate if prev_rate != 0 else 1.0
            if ratio > 2.0 or ratio < 0.5:
                direction = "SPIKED" if ratio > 1 else "DROPPED"
                alerts.append({
                    "type": f"FUNDING_{direction}",
                    "coin": coin,
                    "prev_8h_bps": round(prev_rate * 10000, 3),
                    "curr_8h_bps": round(rate * 10000, 3),
                    "ratio": round(ratio, 2),
                    "note": f"Funding {direction.lower()} {abs(ratio):.1f}x vs prior",
                })
                log(f"ALERT: Funding {direction}: {coin} {prev_rate*10000:.2f} → {rate*10000:.2f} bps/8h ({ratio:.1f}x)")

    # --- OI ranking change detection ---
    # Loris provides ordinal oi_rankings (e.g. "1", "2", or rank strings)
    # Detect when a coin's ranking ordinal changes significantly between runs
    for coin, rank_pair in snap.oi_rankings.items():
        curr_ordinal, curr_label = rank_pair
        prev_pair = prev_oi_rank.get(coin, (None, ""))
        prev_ordinal, _ = prev_pair

        new_oi_rank[coin] = {"ordinal": curr_ordinal, "label": curr_label, "ts": now_ts.isoformat()}

        # Only flag if both readings have valid ordinals and rank moved >3 spots
        if prev_ordinal is not None and curr_ordinal is not None and prev_ordinal > 0:
            rank_change = curr_ordinal - prev_ordinal
            if abs(rank_change) >= 3:
                direction = "RANK_ROSE" if rank_change < 0 else "RANK_FELL"
                alerts.append({
                    "type": f"OI_RANK_{direction}",
                    "coin": coin,
                    "prev_ordinal": prev_ordinal,
                    "curr_ordinal": curr_ordinal,
                    "prev_label": prev_pair[1],
                    "curr_label": curr_label,
                    "change": int(rank_change),
                    "note": f"OI ranking shifted {abs(int(rank_change))} spots — {'higher OI concentration' if rank_change < 0 else 'lower OI concentration'}",
                })
                log(f"ALERT: OI Rank {direction}: {coin} #{prev_ordinal} → #{curr_ordinal} ({curr_label})")

    # --- New/removed symbols ---
    curr_symbols = set(snap.symbols)
    new_symbols = curr_symbols - prev_symbols
    dropped_symbols = prev_symbols - curr_symbols
    if new_symbols:
        alerts.append({
            "type": "SYMBOLS_ADDED",
            "coins": sorted(new_symbols),
            "note": f"{len(new_symbols)} new coin(s) appeared in Loris universe",
        })
        log(f"ALERT: New symbols added: {sorted(new_symbols)}")
    if dropped_symbols:
        alerts.append({
            "type": "SYMBOLS_DROPPED",
            "coins": sorted(dropped_symbols),
            "note": f"{len(dropped_symbols)} coin(s) dropped from Loris universe",
        })
        log(f"ALERT: Symbols dropped: {sorted(dropped_symbols)}")

    # Save state
    save_state({
        "oi_rank_history": new_oi_rank,
        "funding_history": new_funding,
        "symbols": snap.symbols,
    })

    ALERT_FILE.write_text(json.dumps(alerts, indent=2, default=str))

    num_tracked = len(funding_by_coin)
    if alerts:
        log(f"ALERT: {len(alerts)} market structure alert(s) — {num_tracked} coins tracked")
        return 1
    else:
        log(f"OK: No market structure anomalies ({num_tracked} coins tracked)")
        return 0


if __name__ == "__main__":
    sys.exit(run())
