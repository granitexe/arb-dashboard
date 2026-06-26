#!/usr/bin/env python3
"""Meta-improvement orchestrator for basis-arb-tool.

This is the "thinking about improving itself" cron job. It reads a shared
agenda file and coordinates improvement tasks across other cron runs.

HOW IT WORKS:
  - Writes a structured agenda to .cron_output/improvement_agenda.json
  - Each cron job reads this agenda and picks ONE relevant task to work on
  - Results are written to .cron_output/improvement_results.json
  - This job reads results and updates the agenda

The agenda has categories:
  - signal: improve signal quality
  - risk: improve risk controls
  - data: improve data sources
  - docs: improve documentation
  - ops: improve operational reliability

Each agenda item has:
  - priority (1-5, 1=highest)
  - status: pending | in_progress | done | abandoned
  - description
  - suggested_action
  - last_updated

Exit codes: 0 = OK, 1 = items need attention, 2 = error
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

AGENDA_FILE = Path(__file__).parent.parent / ".cron_output" / "improvement_agenda.json"
RESULTS_FILE = Path(__file__).parent.parent / ".cron_output" / "improvement_results.json"
LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "meta_improvement.log"
STATE_FILE = Path(__file__).parent.parent / ".cron_output" / "meta_improvement_state.json"
ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "meta_improvement_alerts.json"
ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
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


# --- Default agenda: things that should always be on the improvement list ---
DEFAULT_AGENDA_ITEMS = [
    {
        "id": "loris-api-key-paid",
        "category": "data",
        "priority": 1,
        "status": "pending",
        "description": "Loris.tools is PAID for multi-venue coverage. Free tier only gives BTC/ETH. Sign up at https://api.loris.tools",
        "suggested_action": "Get an API key and set LORIS_API_KEY env var. Without it, only BTC/ETH signals are reliable.",
        "last_updated": None,
    },
    {
        "id": "hyperliquid-live-trading",
        "category": "ops",
        "priority": 1,
        "status": "pending",
        "description": "Execution layer (basis_arb/execution/hyperliquid.py) is built but not yet tested with real keys. Need to verify the signing flow works.",
        "suggested_action": "Test HYPERLIQUID_ENABLED=true with a small position. Verify market_open() returns a valid order_id. Then enable kelly sizing.",
        "last_updated": None,
    },
    {
        "id": "hyperliquid-venue-weight",
        "category": "signal",
        "priority": 2,
        "status": "pending",
        "description": "Hyperliquid/Paradex/Aster are new venues — should get higher carry weight since thinner competition and lower fees",
        "suggested_action": "Add venue_age_factor to carry.py to down-weight mature venues (Binance, Bybit) vs new venues (HL, Paradex, Aster)",
        "last_updated": None,
    },
    {
        "id": "basis-volatility-calibration",
        "category": "risk",
        "priority": 2,
        "status": "pending",
        "description": "executor.py uses a static 0.15 (15%) basis_volatility_annual. Real basis volatility varies by coin and regime.",
        "suggested_action": "Pull historical HL funding rates, compute actual basis std-dev per coin. Store in a per-coin config or compute rolling.",
        "last_updated": None,
    },
    {
        "id": "backtest-module",
        "category": "risk",
        "priority": 2,
        "status": "pending",
        "description": "Tool has no backtesting — cannot validate carry estimates historically",
        "suggested_action": "Implement a simple backtest using cached Loris data: for each historical snapshot, calculate carry, apply TGE filter, track hypothetical P&L",
        "last_updated": None,
    },
    {
        "id": "trap-score-calibration",
        "category": "signal",
        "priority": 2,
        "status": "pending",
        "description": "TGE trap scores are based on heuristics — need calibration against real unlock events",
        "suggested_action": "Add a 'known unlock events' JSON file: coins + dates of past TGE unlocks; backtest trap score at those dates",
        "last_updated": None,
    },
    {
        "id": "per-coin-basis-vol",
        "category": "signal",
        "priority": 3,
        "status": "pending",
        "description": "Kelly sizing uses a flat 15% vol. Per-coin vol would give better sizing — high-vol coins should get smaller positions.",
        "suggested_action": "Use HL funding history to compute rolling 30d basis vol per coin. Replace the static 0.15 with per-coin values.",
        "last_updated": None,
    },
    {
        "id": "dashboard-github-token",
        "category": "ops",
        "priority": 2,
        "status": "pending",
        "description": "refresh_dashboard.sh needs GITHUB_TOKEN set — without it, dashboard can't auto-refresh on GitHub Pages",
        "suggested_action": "Create a GitHub PAT (needs repo scope), store in ~/.basis_arb/.env as GITHUB_TOKEN=ghp_...",
        "last_updated": None,
    },
    {
        "id": "autonomous-trading-loop",
        "category": "ops",
        "priority": 1,
        "status": "pending",
        "description": "executor.py is built but the actual autonomous trading cron job is not yet registered",
        "suggested_action": "Register run_autonomous_trading.sh as a cron job (every 4h). Test with DRY_RUN=true first. Then enable with HYPERLIQUID_ENABLED=true.",
        "last_updated": None,
    },
    {
        "id": "rabby-jumper-manual-bridging",
        "category": "ops",
        "priority": 3,
        "status": "pending",
        "description": "Spot leg of basis arb (going long spot) requires bridging via Jumper/Rabby. This is manual today.",
        "suggested_action": "Research Jumper.xyz API for programmatic routing. Until then, spot leg remains manual.",
        "last_updated": None,
    },
    {
        "id": "knowledge-base",
        "category": "docs",
        "priority": 3,
        "status": "pending",
        "description": "knowledge/x/ has 4 articles covering carry mechanics, execution friction, edge identification, fee math",
        "suggested_action": "Add articles on: delta-neutral mechanics, ADL risk, funding rate regimes, new-venue edge",
        "last_updated": None,
    },
]


def run() -> int:
    log("Starting meta-improvement orchestrator")
    now_ts = datetime.datetime.utcnow().isoformat()

    # Load existing agenda or initialize
    agenda = load_json(AGENDA_FILE, {"items": [], "version": 1})
    if not agenda.get("items"):
        agenda = {"version": 1, "items": DEFAULT_AGENDA_ITEMS, "last_full_refresh": now_ts}

    # Load results from other jobs
    results = load_json(RESULTS_FILE, {})

    # Process any completed results
    for item_id, result_data in results.items():
        for item in agenda["items"]:
            if item["id"] == item_id:
                item["status"] = result_data.get("status", "done")
                item["last_updated"] = now_ts
                item["result"] = result_data
                log(f"Result for [{item_id}]: {result_data.get('summary', 'no summary')}")

    # Pick the highest-priority pending item and update it
    pending = [i for i in agenda["items"] if i["status"] == "pending"]
    pending.sort(key=lambda x: (x["priority"], x["id"]))

    alerts = []
    if pending:
        top = pending[0]
        top["status"] = "in_progress"
        top["last_updated"] = now_ts
        alerts.append({
            "focus_item": top["id"],
            "category": top["category"],
            "description": top["description"],
            "suggested_action": top["suggested_action"],
            "all_pending": [(i["id"], i["priority"], i["category"]) for i in pending[:10]],
        })
        log(f"FOCUS: [{top['id']}] (priority={top['priority']}) — {top['description']}")
        log(f"SUGGESTED ACTION: {top['suggested_action']}")
    else:
        log("All agenda items resolved — refreshing agenda")
        agenda["items"] = DEFAULT_AGENDA_ITEMS
        agenda["last_full_refresh"] = now_ts

    # Clean old results
    for item_id in list(results.keys()):
        if agenda["items"] and not any(i["id"] == item_id for i in agenda["items"]):
            del results[item_id]

    save_json(AGENDA_FILE, agenda)
    save_json(RESULTS_FILE, results)
    ALERT_FILE.write_text(json.dumps(alerts, indent=2, default=str))

    pending_count = sum(1 for i in agenda["items"] if i["status"] == "pending")
    in_progress_count = sum(1 for i in agenda["items"] if i["status"] == "in_progress")

    if pending_count > 0 or in_progress_count > 0:
        log(f"Agenda: {pending_count} pending, {in_progress_count} in progress")
        return 1
    else:
        log("Agenda: all items done")
        return 0


if __name__ == "__main__":
    sys.exit(run())
