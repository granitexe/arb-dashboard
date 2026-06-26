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


def _is_schema_a(entry: dict) -> bool:
    """Return True if entry has the enriched Schema A fields (pnl_usd etc.).

    Schema A is produced by a live-trader that writes enriched entries with
    realized P&L, funding collected, and explicit open/closed status.
    Schema B (TradeJournalEntry.to_dict()) has action/size_executed but no pnl_usd.
    """
    return "pnl_usd" in entry or "funding_collected_usd" in entry or entry.get("status") in ("open", "closed")


def _is_real_trade(entry: dict) -> bool:
    """Return True if this entry represents a real executed trade, not a dry_run or skip."""
    status = entry.get("status", "")
    if status == "dry_run":
        return False
    # Schema B: real trades have action=OPEN and status=ok with non-zero size
    if status == "ok" and entry.get("action") == "OPEN" and (entry.get("size_executed") or 0) > 0:
        return True
    # Schema A: any entry with status=ok and size_executed>0
    if status == "ok" and (entry.get("size_executed") or 0) > 0:
        return True
    return False


def _schema_a_compute(entries: list[dict]) -> tuple[dict, list[dict], bool]:
    """Compute health for Schema A (enriched P&L entries with open/closed status)."""
    alerts = []
    total_pnl = 0.0
    total_funding_collected = 0.0
    wins = 0
    losses = 0
    adl_forced = 0
    open_positions = []
    no_data = True

    for e in entries:
        pnl = e.get("pnl_usd", 0.0) or 0.0
        funding = e.get("funding_collected_usd", 0.0) or 0.0
        status = e.get("status", "")
        exit_reason = e.get("exit_reason", "")

        if status in ("open", "closed"):
            no_data = False

        if status == "open":
            open_positions.append(e)
            total_pnl += pnl
            total_funding_collected += funding
        elif status == "closed":
            total_pnl += pnl
            total_funding_collected += funding
            if pnl >= 0:
                wins += 1
            else:
                losses += 1
            if exit_reason == "ADL":
                adl_forced += 1

    return _build_snapshot(
        entries=entries, wins=wins, losses=losses, adl_forced=adl_forced,
        total_pnl=total_pnl, total_funding_collected=total_funding_collected,
        open_positions=open_positions, no_data=no_data,
    )


def _schema_b_compute(entries: list[dict]) -> tuple[dict, list[dict], bool]:
    """Compute health for Schema B (TradeJournalEntry.to_dict() action-based journal).

    Schema B has no P&L or exit_reason fields. We infer health from:
    - Win/loss: matched OPEN→CLOSE pairs via coin+direction
    - ADL: entries where reason contains "ADL"
    - Open carry: live fetch from Hyperliquid API
    - P&L: inferred from bankroll_usd delta between cycles
    """
    alerts = []
    no_data = True

    # Separate dry_run from real trades
    real_entries = [e for e in entries if _is_real_trade(e)]
    dry_run_count = sum(1 for e in entries if e.get("status") == "dry_run")

    if not real_entries:
        # Only dry runs — informational only
        return _build_snapshot(
            entries=entries, wins=0, losses=0, adl_forced=0,
            total_pnl=0.0, total_funding_collected=0.0,
            open_positions=[], no_data=True,
        )

    no_data = False

    # Match OPEN→CLOSE pairs by coin to determine wins/losses
    # We track in-flight opens and look for corresponding CLOSE actions
    open_by_coin: dict[str, dict] = {}
    closed_trades = []
    adl_forced = 0

    for e in entries:
        action = e.get("action", "")
        coin = e.get("coin", "")
        reason = e.get("reason", "")

        if action == "OPEN" and _is_real_trade(e):
            open_by_coin[coin] = e
        elif action == "CLOSE" and _is_real_trade(e):
            if coin in open_by_coin:
                open_entry = open_by_coin.pop(coin)
                closed_trades.append({"open": open_entry, "close": e})
                if "ADL" in reason.upper():
                    adl_forced += 1

    wins = 0
    losses = 0
    total_pnl = 0.0

    # Estimate P&L from bankroll_usd delta recorded in entries
    # The executor records bankroll_usd at each action. Use last-seen bankroll
    # as equity proxy; compare to initial bankroll in state.
    bankroll_values = [e.get("bankroll_usd", 0) for e in entries if e.get("bankroll_usd")]
    if bankroll_values:
        latest_bankroll = bankroll_values[-1]
        initial_bankroll = bankroll_values[0] if bankroll_values else 10000.0
        total_pnl = latest_bankroll - initial_bankroll

    # Funding collected: sum net_carry_apr * notional * duration
    # Since we don't have duration, report as "estimated carry earned"
    total_funding_collected = 0.0

    # Open positions: live fetch from Hyperliquid (only if enabled)
    open_positions = _fetch_live_positions()

    # Classify wins/losses from closed trades
    # P&L is bundled in bankroll delta; for per-trade classification use reason
    for pair in closed_trades:
        close_entry = pair["close"]
        reason = close_entry.get("reason", "")
        if "profit" in reason.lower() or "gain" in reason.lower() or "close" in reason.lower():
            # Assume basis converged (good) unless ADL
            if "ADL" not in reason.upper():
                wins += 1
            else:
                losses += 1  # ADL is a forced loss
        else:
            losses += 1

    # If we can't classify, use total_pnl sign
    if closed_trades and wins + losses == 0:
        if total_pnl >= 0:
            wins = len(closed_trades)
        else:
            losses = len(closed_trades)

    return _build_snapshot(
        entries=entries, wins=wins, losses=losses, adl_forced=adl_forced,
        total_pnl=total_pnl, total_funding_collected=total_funding_collected,
        open_positions=open_positions, no_data=no_data,
    )


def _fetch_live_positions() -> list[dict]:
    """Fetch current open positions from Hyperliquid if enabled.

    Returns a list of position dicts with est_annual_carry and notional_usd.
    """
    try:
        cfg_env = os.environ.get("HYPERLIQUID_ENABLED", "").lower()
        if cfg_env != "true":
            return []

        # Import here to avoid hard dependency when HL is not configured
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from basis_arb.executor import get_open_positions
        positions = get_open_positions()
        enriched = []
        for p in positions:
            notional = abs(float(p.get("szi", 0) or 0) * float(p.get("lastPrice", 0) or 0))
            # Carry estimated from funding rate stored in entry metadata if available
            carry_apr = float(p.get("funding_apr", 0) or 0)
            enriched.append({
                "coin": p.get("coin", ""),
                "notional_usd": notional,
                "est_annual_carry": notional * carry_apr if carry_apr else 0.0,
                "entry": p,
            })
        return enriched
    except Exception as e:
        log(f"Could not fetch live positions from Hyperliquid: {e}")
        return []


def _build_snapshot(
    entries: list[dict],
    wins: int,
    losses: int,
    adl_forced: int,
    total_pnl: float,
    total_funding_collected: float,
    open_positions: list[dict],
    no_data: bool,
) -> tuple[dict, list[dict], bool]:
    """Build the health snapshot dict and alerts from computed metrics."""
    alerts = []
    total_trades = wins + losses
    win_rate = wins / total_trades if total_trades > 0 else None

    # Load previous state for peak tracking
    prev_state = load_state()
    peak_equity = prev_state.get("peak_equity", 0.0)
    peak_ts = prev_state.get("peak_equity_ts")
    bankroll = prev_state.get("bankroll_usd", 10000.0)

    # Drawdown detection
    current_equity = bankroll + total_pnl
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
            "peak_equity": round(peak_equity, 2),
            "current_equity": round(current_equity, 2),
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
    open_carry = sum(e.get("est_annual_carry", 0.0) if isinstance(e, dict) else 0.0 for e in open_positions)
    open_notional = sum(e.get("notional_usd", 0.0) if isinstance(e, dict) else 0.0 for e in open_positions)

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
        "peak_equity_ts": peak_ts,
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

    return snapshot, alerts, no_data


def load_journal() -> list[dict]:
    """Load trade journal from one of the configured sources.

    Priority (highest to lowest):
      0. Project-local .trade_journal.jsonl (same repo, written by local trader)
      1. ~/.basis_arb/trade_journal.jsonl  (shared volume mount)
      2. TRADEFEED_URL env var             (webhook / REST endpoint)
      3. GitHub API (granitexe/arb-dashboard) if GITHUB_TOKEN is set
    """
    entries = []

    # Source 0: project-local file (written by trader on the same machine)
    project_journal = Path(__file__).parent.parent / ".trade_journal.jsonl"
    if project_journal.exists():
        try:
            for line in project_journal.read_text().splitlines():
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
            log(f"Loaded {len(entries)} entries from {project_journal}")
            return entries
        except Exception as e:
            log(f"ERROR reading {project_journal}: {e}")

    # Source 1: shared-volume local file
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


def compute_health(entries: list[dict], prev_state: dict) -> tuple[dict, list[dict], bool]:
    """Compute health metrics from trade journal entries.

    Returns (snapshot, alerts, no_data_flag).
    no_data_flag is True when there are no closed trades — health score is based
    on dry-run-only history and is informational, not actionable.

    Handles two schemas:
    - Schema A (enriched): entries with pnl_usd / funding_collected_usd / status in (open, closed)
    - Schema B (TradeJournalEntry): action-based entries with no P&L fields
    """
    if not entries:
        return {"note": "No trade data available"}, [], True

    # Detect schema from first non-empty entry
    schema_a = _is_schema_a(entries[0]) if entries else False

    if schema_a:
        log("Detected Schema A (enriched P&L entries)")
        return _schema_a_compute(entries)
    else:
        log("Detected Schema B (TradeJournalEntry action-based journal)")
        return _schema_b_compute(entries)


def run() -> int:
    log("Starting performance inbox parser")
    prev_state = load_state()
    entries = load_journal()
    snapshot, alerts, no_data = compute_health(entries, prev_state)

    # Update history
    history = prev_state.get("history", [])
    history.append(snapshot)
    history = history[-60:]  # keep 60 data points

    new_state = {
        **prev_state,
        "history": history,
        "peak_equity": snapshot.get("peak_equity", prev_state.get("peak_equity", 0.0)),
        "peak_equity_ts": snapshot.get("peak_equity_ts") or prev_state.get("peak_equity_ts"),
        "bankroll_usd": snapshot.get("current_equity", prev_state.get("bankroll_usd", 10000.0)),
    }
    save_state(new_state)

    HEALTH_FILE.write_text(json.dumps(snapshot, indent=2, default=str))
    ALERT_FILE.write_text(json.dumps(alerts, indent=2, default=str))

    if no_data:
        log("NO DATA YET — no live trades recorded; dry_run signals do not affect health")
        print("=== HEALTH SNAPSHOT ===")
        print(json.dumps(snapshot, indent=2, default=str))
        return 0

    if alerts:
        for a in alerts:
            log(f"ALERT: [{a['type']}] {a.get('note', '')}")
        score = snapshot.get("health_score", "N/A")
        log(f"Health score: {score}/100")
        return 1
    else:
        score = snapshot.get("health_score", 0)
        pnl = snapshot.get("total_pnl_usd", 0)
        wr = snapshot.get("win_rate")
        open_pos = snapshot.get("open_positions", 0)
        carry = snapshot.get("total_funding_collected_usd", 0)
        log(f"OK: score={score}/100 | pnl=${pnl} | win_rate={pct(wr) if wr else 'N/A'} | "
            f"open={open_pos} | carry=${carry}")
        print("=== HEALTH SNAPSHOT ===")
        print(json.dumps(snapshot, indent=2, default=str))
        return 0


if __name__ == "__main__":
    sys.exit(run())
