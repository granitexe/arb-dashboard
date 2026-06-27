#!/usr/bin/env python3
"""Monitor upcoming token unlocks from DeFiLlama.

Alerts when a tracked token has a large unlock within 30 days,
especially if the unlock represents a significant % of circulating supply.
Designed to run as a cron job.

Alert levels:
  - WARNING   : unlock within 7 days
  - CRITICAL  : unlock within 24 hours  OR  unlock > 5% of circulating supply

CRITICAL alerts cause the coin to be written to unlock_suppress.json,
which the signal pipeline uses to exclude that coin from signals.

Exit codes: 0 = OK, 1 = alert(s), 2 = error
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
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from basis_arb.config import BasisArbConfig
from basis_arb.sources.defillama import DefiLlamaClient
from basis_arb.sources.coingecko import CoinGeckoClient
from basis_arb.sources.cache import JsonCache

# --- Output paths ---
ROOT = Path(__file__).parent.parent
ALERT_FILE = ROOT / ".cron_output" / "unlock_alerts.json"
SUPPRESS_FILE = ROOT / ".cron_output" / "unlock_suppress.json"
STATE_FILE = ROOT / ".cron_output" / "unlock_state.json"
LOG_FILE = ROOT / ".cron_output" / "unlock_monitor.log"
AGENDA_FILE = ROOT / ".cron_output" / "improvement_agenda.json"
ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)

# --- Alert thresholds (can be overridden via env) ---
THRESHOLD_DAYS_WARNING = 7
THRESHOLD_DAYS_CRITICAL = 1          # 24 hours
THRESHOLD_PCT_CIRC_CRITICAL = 0.05   # 5% of circulating supply

# Coins to track — expanded universe beyond just the top DeFiLlama coins.
# Lowercase symbol -> canonical uppercase coin.  Entries without an explicit
# uppercase name default to symbol.upper().
TRACKED_COINS: dict[str, str] = {
    # majors
    "btc":   "BTC",
    "eth":   "ETH",
    "sol":   "SOL",
    # L2 / layer-2 tokens
    "arb":   "ARB",
    "op":    "OP",
    "base":  "BASE",
    "mática": "MATIC",
    "avax":  "AVAX",
    # oracle / infra
    "link":  "LINK",
    "uniswap": "UNI",
    "aave":  "AAVE",
    # newer narrations
    "sui":   "SUI",
    "sei":   "SEI",
    "injective": "INJ",
    "wormhole": "W",
    "jito":  "JTO",
    "jupiter": "JUP",
    "ena":   "ENA",
    "wld":   "WLD",
    "berachain": "BERN",
    "not":   "NOT",
    "goat":  "GOAT",
    "ai16z": "AI16Z",
    "degen": "DEGEN",
    "fartcoin": "FARTCOIN",
    # fallbacks (symbol same as canonical)
    "apt":   "APT",
    "tia":   "TIA",
    "strk":  "STRK",
    "pixel": "PIXEL",
    "grass": "GRASS",
    " список": "LIST",
}


def log(msg: str) -> None:
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))


def _gecko_ids_from_targets(targets: dict[str, str]) -> list[str]:
    """Return list of lowercase CoinGecko IDs to look up (same as lowercase symbol)."""
    return list(targets.keys())


def _build_circulating_map(
    gecko: CoinGeckoClient,
) -> dict[str, float]:
    """Fetch top coins from CoinGecko and return {symbol_upper: circulating_supply}."""
    snap, _ = gecko.fetch_markets(pages=2, per_page=250)
    out: dict[str, float] = {}
    for coin, mkt in snap.by_coin.items():
        if mkt.circulating_supply and mkt.circulating_supply > 0:
            out[coin] = mkt.circulating_supply
    return out


def _compute_pct_circ(
    ev_tokens: float | None,
    circ_supply: float | None,
) -> float | None:
    """Compute unlock as fraction of circulating supply."""
    if ev_tokens is None or circ_supply is None or circ_supply <= 0:
        return None
    return ev_tokens / circ_supply


def _check_and_suggest_agenda_update(
    alerts: list[dict],
    state: dict,
) -> None:
    """Write pattern-based trap-model improvement suggestions to the agenda.

    Patterns that suggest the trap model needs updating:
    1. Many CRITICAL alerts in a short window → TGE concentration risk not captured.
    2. High-pct-circ alerts (>> 5%) → overhang threshold may be too lenient.
    3. Coins flagged but no clear reason in the trap subsignal → model gap.
    """
    if not alerts:
        return

    critical_count = sum(1 for a in alerts if a["level"] == "CRITICAL")
    warning_count = sum(1 for a in alerts if a["level"] == "WARNING")
    high_pct_count = sum(
        1 for a in alerts
        if a["pct_circ"] is not None and a["pct_circ"] >= 0.10
    )

    suggestions: list[dict] = []

    # Pattern 1: multiple TGEs clustering in the same week
    if critical_count + warning_count >= 3:
        suggestions.append({
            "id": f"trap-tge-concentration-{datetime.utcnow().strftime('%Y%m%d')}",
            "category": "signal",
            "priority": 1,
            "status": "pending",
            "description": (
                f"{critical_count + warning_count} unlock alerts this cycle "
                f"(CRITICAL={critical_count}, WARNING={warning_count}). "
                "TGE trap model does not account for simultaneous multi-token unlocks "
                "amplifying market-wide sell pressure."
            ),
            "suggested_action": (
                "Add a 'concentration factor' to the unlock subsignal: if >2 coins "
                "have CRITICAL unlocks within 7 days of each other, increase the "
                "composite trap score by 0.15."
            ),
            "pattern": "tge_concentration",
            "detected_at": datetime.utcnow().isoformat() + "Z",
        })

    # Pattern 2: very large individual unlocks (>10% circ)
    if high_pct_count >= 1:
        big_alerts = [
            a for a in alerts
            if a["pct_circ"] is not None and a["pct_circ"] >= 0.10
        ]
        coins_str = ", ".join(a["coin"] for a in big_alerts)
        suggestions.append({
            "id": f"trap-large-unlock-{datetime.utcnow().strftime('%Y%m%d')}",
            "category": "signal",
            "priority": 1,
            "status": "pending",
            "description": (
                f"Unlock(s) > 10% of circulating supply detected: {coins_str}. "
                "Current unlock_hard_pct_circ=0.05 (5%) may be too low for some "
                "tokens, or conversely some tokens with 5-10% unlocks should be "
                "excluded automatically."
            ),
            "suggested_action": (
                "Audit tokens in the 5-10% range vs their actual market impact. "
                "Consider adding a second hard tier: >10% circ → always exclude "
                "regardless of composite score."
            ),
            "pattern": "large_unlock",
            "affected_coins": coins_str,
            "detected_at": datetime.utcnow().isoformat() + "Z",
        })

    if not suggestions:
        return

    agenda = load_json(AGENDA_FILE, {"version": 1, "items": []})
    existing_ids = {item["id"] for item in agenda.get("items", [])}
    for sg in suggestions:
        if sg["id"] not in existing_ids:
            agenda["items"].append(sg)
            log(f"AGENDA UPDATE: added suggestion [{sg['id']}] — {sg['pattern']}")
    save_json(AGENDA_FILE, agenda)


def run() -> int:
    log("Starting unlock monitor")
    now = datetime.now(timezone.utc)
    alerts: list[dict] = []
    suppress: list[str] = []   # coins to suppress signals for
    events_logged: list[str] = []

    cfg = BasisArbConfig(unlock_horizon_days=90)
    cache = JsonCache(cfg.cache_dir, enabled=False)

    # --- DeFiLlama: fetch unlock events ---
    dl = DefiLlamaClient(cfg, cache)
    # DeFiLlama slugs are lowercase; we pass lowercase symbol keys.
    # It stores events back under the same lowercase key we pass in.
    targets: dict[str, str | None] = {sym.lower(): None for sym in TRACKED_COINS}
    snap, dl_meta = dl.fetch_unlocks(targets)
    log(
        f"DeFiLlama: {len(snap.resolved_coins)} coins resolved, "
        f"{sum(len(v) for v in snap.events_by_coin.values())} total events"
    )

    # --- CoinGecko: fetch circulating supply for %-of-circulating calc ---
    try:
        gecko = CoinGeckoClient(cfg, cache)
        circ_map = _build_circulating_map(gecko)
        log(f"CoinGecko: {len(circ_map)} coins with circulating supply")
    except Exception as e:
        log(f"WARN: CoinGecko unavailable ({e}); circulating supply not used")
        circ_map = {}

    # Reverse map: lowercase slug -> canonical uppercase symbol
    slug_to_coin: dict[str, str] = {sym.lower(): canonical for sym, canonical in TRACKED_COINS.items()}

    # --- Evaluate each event against alert thresholds ---
    for dl_key, events in snap.events_by_coin.items():
        coin_upper = slug_to_coin.get(dl_key, dl_key.upper())  # canonical name
        for ev in events:
            if ev.timestamp is None:
                continue

            days_until = (ev.timestamp - now).total_seconds() / 86400.0
            if days_until < 0 or days_until > cfg.unlock_horizon_days:
                continue

            # Resolve circulating supply
            circ_sym = coin_upper  # symbol same as canonical for most
            circ = circ_map.get(circ_sym)
            pct_circ = _compute_pct_circ(ev.tokens, circ)

            size_str = (
                f"{pct_circ * 100:.1f}% circ"
                if pct_circ is not None
                else f"{ev.tokens:.0f} tokens"
            )
            msg = (
                f"{coin_upper}: {size_str} unlock in {days_until:.1f}d "
                f"({ev.timestamp.strftime('%Y-%m-%d')}) [{ev.unlock_type or 'unknown'}]"
            )

            # Determine alert level
            alert_level: str | None = None
            reason: str | None = None

            # CRITICAL: within 24 hours OR > 5% circ
            if days_until <= THRESHOLD_DAYS_CRITICAL:
                alert_level = "CRITICAL"
                reason = f"unlock in <24h ({days_until:.1f}d)"
            elif pct_circ is not None and pct_circ >= THRESHOLD_PCT_CIRC_CRITICAL:
                alert_level = "CRITICAL"
                reason = f"unlock = {pct_circ * 100:.1f}% of circulating supply"
            # WARNING: within 7 days
            elif days_until <= THRESHOLD_DAYS_WARNING:
                alert_level = "WARNING"
                reason = f"unlock in {days_until:.1f}d"

            if alert_level:
                entry = {
                    "coin": coin_upper,
                    "days_until": round(days_until, 1),
                    "pct_circ": pct_circ,
                    "level": alert_level,
                    "date": ev.timestamp.strftime("%Y-%m-%d"),
                    "unlock_type": ev.unlock_type,
                    "usd_value": ev.usd_value,
                    "tokens": ev.tokens,
                    "reason": reason,
                    "detected_at": datetime.utcnow().isoformat() + "Z",
                }
                alerts.append(entry)
                if alert_level == "CRITICAL":
                    suppress.append(coin_upper)
                log(f"ALERT [{alert_level}]: {msg}")

    # Deduplicate suppress list (same coin may have multiple events)
    suppress = list(dict.fromkeys(suppress))

    # --- Write outputs ---
    ALERT_FILE.write_text(json.dumps(alerts, indent=2))
    SUPPRESS_FILE.write_text(json.dumps(suppress, indent=2))
    save_json(
        STATE_FILE,
        {
            "ts": datetime.utcnow().isoformat() + "Z",
            "coins_tracked": len(TRACKED_COINS),
            "coins_resolved": len(snap.resolved_coins),
            "alerts_count": len(alerts),
            "critical_coins": suppress,
            "warning_coins": [
                a["coin"] for a in alerts if a["level"] == "WARNING"
            ],
            "defillama_ok": dl_meta.ok,
            "defillama_error": dl_meta.error,
        },
    )

    # --- Trap-model improvement check ---
    _check_and_suggest_agenda_update(alerts, {})

    if alerts:
        log(f"ALERT: {len(alerts)} unlock alert(s) — {len(suppress)} suppressed")
        return 1
    else:
        log(f"OK: No upcoming unlock alerts ({len(TRACKED_COINS)} coins tracked)")
        return 0


if __name__ == "__main__":
    sys.exit(run())
