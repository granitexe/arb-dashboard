#!/usr/bin/env python3
"""Performance inbox — parses trade journal and produces a health snapshot.

This is the LIVE TRADER FEEDBACK loop. The running trader (on the separate PC)
writes its .trade_journal.jsonl to a shared location this script can read.
Options (in order of priority):
  1. Local path: ~/.basis_arb/trade_journal.jsonl (same machine)
  2. HTTP GET URL: TRADEFEED_URL env var (e.g. a webhook endpoint the trader pushes to)
  3. GitHub API: read from arb-dashboard repo if GITHUB_TOKEN is set

The script computes:
  - Total P&L vs bankroll
  - Funding collected vs expectations
  - Win rate (basis converged before ADL?)
  - Current open position count and aggregate carry
  - Drawdown from peak

Exit codes: 0 = OK (no issues), 1 = warnings, 2 = critical (kill-switch near)
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

STATE_FILE = Path(__file__).parent.parent / ".cron_output" / "performance_state.json"
HEALTH_FILE = Path(__file__).parent.parent / ".cron_output" / "performance_health.json"
ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "performance_alerts.json"
LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "performance_inbox.log"
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
    return {
        "history": [],
        "peak_equity": 0.0,
        "peak_equity_ts": None,
        "version_tag": "unknown",
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def pct(v: float) -> str:
    return f"{v*100:.2f}%"


def load_journal() -> list[dict]:
    """Load trade journal from one of the configured sources."""
    entries = []

    # Source 1: local file
    if TRADER_INBOX.exists():
        try:
            for line in TRADER_INBOX.read_text().splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
            log(f"Loaded {len(entries)} entries from {TRADER_INBOX}")
            return entries
        except Exception as e:
            log(f"ERROR reading {TRADER_INBOX}: {e}")

    # Source 2: HTTP endpoint
    feed_url = os.environ.get("TRADEFEED_URL", "")
    if feed_url:
        try:
            req = urllib.request.Request(
                feed_url,
                headers={"User-Agent": "basis-arb-tool/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if isinstance(data, list):
                    entries = data
                elif isinstance(data, dict) and "trades" in data:
                    entries = data["trades"]
            log(f"Loaded {len(entries)} entries from {feed_url}")
            return entries
        except Exception as e:
            log(f"ERROR fetching {feed_url}: {e}")

    # Source 3: GitHub API (arb-dashboard repo)
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN_RO", "")
    if token:
        try:
            from urllib.parse import urlparse
            # Construct raw URL for trade_journal.jsonl in arb-dashboard repo
            api_url = (
                "https://api.github.com/repos/granitexe/arb-dashboard/"
                "contents/trade_journal.jsonl"
            )
            req = urllib.request.Request(api_url, headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3.raw",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read().decode()
                for line in content.splitlines():
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
            log(f"Loaded {len(entries)} entries from GitHub")
            return entries
        except Exception as e:
            log(f"ERROR fetching from GitHub: {e}")

    return entries


def compute_health(entries: list[dict], prev_state: dict) -> tuple[dict, list[dict]]:
    """Compute health metrics from trade journal entries."""
    if not entries:
        return {"score": None, "note": "No trade data available"}, []

    alerts = []
    total_pnl = 0.0
    total_funding_collected = 0.0
    wins = 0
    losses = 0
    adl_forced = 0
    open_positions = []
    peak_equity = prev_state.get("peak_equity", 0.0)
    peak_ts = prev_state.get("peak_equity_ts")

    for e in entries:
        pnl = e.get("pnl_usd", 0.0) or 0.0
        funding = e.get("funding_collected_usd", 0.0) or 0.0
        status = e.get("status", "")
        exit_reason = e.get("exit_reason", "")

        total_pnl += pnl
        total_funding_collected += funding

        if status == "closed":
            if pnl >= 0:
                wins += 1
            else:
                losses += 1
            if exit_reason == "ADL":
                adl_forced += 1
        elif status == "open":
            open_positions.append(e)

    total_trades = wins + losses
    win_rate = wins / total_trades if total_trades > 0 else None

    # Drawdown detection
    current_equity = prev_state.get("bankroll_usd", 0.0) + total_pnl
    if current_equity > peak_equity:
        peak_equity = current_equity
        peak_ts = datetime.datetime.utcnow().isoformat()

    drawdown = (peak_equity - current_equity) / peak_equity if peak_equity > 0 else 0.0

    # Health score (0-100)
    score = 50  # baseline
    if win_rate is not None:
        score += (win_rate - 0.5) * 40  # ±20 pts based on win rate
    if drawdown < 0.05:
        score += 20
    elif drawdown < 0.10:
        score += 10
    elif drawdown >= 0.15:
        score -= 30
        alerts.append({
            "type": "HIGH_DRAWDOWN",
            "drawdown_pct": pct(drawdown),
            "peak_equity": peak_equity,
            "current_equity": current_equity,
            "note": "Drawdown >15% — review strategy",
        })
    if adl_forced > 0:
        score -= adl_forced * 5
        alerts.append({
            "type": "ADL_FORCED",
            "count": adl_forced,
            "note": "Positions forced-closed by ADL — check over-leveraging",
        })
    score = max(0, min(100, score))

    # Aggregate carry on open positions
    open_carry = sum(e.get("est_annual_carry", 0.0) for e in open_positions)
    open_notional = sum(e.get("notional_usd", 0.0) for e in open_positions)

    snapshot = {
        "ts": datetime.datetime.utcnow().isoformat(),
        "total_pnl_usd": round(total_pnl, 2),
        "total_funding_collected_usd": round(total_funding_collected, 2),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "adl_forced": adl_forced,
        "open_positions": len(open_positions),
        "open_carry_annual_usd": round(open_carry, 2),
        "open_notional_usd": round(open_notional, 2),
        "current_equity": round(current_equity, 2),
        "peak_equity": round(peak_equity, 2),
        "drawdown_pct": round(drawdown, 4),
        "health_score": score,
        "version_tag": prev_state.get("version_tag", "unknown"),
    }

    # Check kill-switch threshold
    kill_switch_frac = float(os.environ.get("KILL_SWITCH_DRAWDOWN_FRAC", "0.15"))
    if drawdown >= kill_switch_frac:
        alerts.append({
            "type": "KILL_SWITCH_NEAR",
            "drawdown_pct": pct(drawdown),
            "kill_switch_frac": pct(kill_switch_frac),
            "note": f"Drawdown within 1% of kill-switch threshold",
        })

    return snapshot, alerts


def run() -> int:
    log("Starting performance inbox parser")
    prev_state = load_state()
    entries = load_journal()
    snapshot, alerts = compute_health(entries, prev_state)

    # Update history
    history = prev_state.get("history", [])
    history.append(snapshot)
    history = history[-60:]  # keep 60 data points

    new_state = {
        **prev_state,
        "history": history,
        "peak_equity": snapshot.get("peak_equity", prev_state.get("peak_equity", 0.0)),
        "peak_equity_ts": peak_ts if (peak_ts := snapshot.get("peak_equity_ts")) else prev_state.get("peak_equity_ts"),
    }
    save_state(new_state)

    HEALTH_FILE.write_text(json.dumps(snapshot, indent=2, default=str))
    ALERT_FILE.write_text(json.dumps(alerts, indent=2, default=str))

    if alerts:
        for a in alerts:
            log(f"ALERT: [{a['type']}] {a.get('note', '')}")
        log(f"Health score: {snapshot['health_score']}/100")
        return 1
    else:
        log(f"OK: score={snapshot['health_score']}/100 | pnl=${snapshot['total_pnl_usd']} | "
            f"win_rate={pct(snapshot['win_rate']) if snapshot['win_rate'] else 'N/A'} | "
            f"open={snapshot['open_positions']} | carry=${snapshot['total_funding_collected_usd']}")
        return 0


if __name__ == "__main__":
    sys.exit(run())
