#!/usr/bin/env python3
"""Research: operator tools integration scanner.

Scans and evaluates the operator-provided tools for integration opportunities:
  - aggr.trade       (HL ecosystem aggregator)
  - velo.xyz         (€200/month — skip unless bankroll justifies it)
  - chart.kiyotaka.ai (charting)
  - hydromancer.xyz  (HL ecosystem)
  - hl.eco           (HL ecosystem)
  - github.com/hyperliquid-dex (official SDK)

Outputs:
  - integration_opportunities.json — scored list of what to integrate
  - venue_coverage.json — which venues are covered by which tools

Exit codes: 0 = OK, 1 = findings, 2 = error
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
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

REPORT_FILE = Path(__file__).parent.parent / ".cron_output" / "operator_tools_report.json"
LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "operator_tools.log"
ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "operator_tools_alerts.json"
REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)

# Operator-configured cost threshold (from env or default)
VELO_COST_MONTHLY_EUR = float(os.environ.get("VELO_COST_THRESHOLD_EUR", "200"))


def log(msg: str) -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def fetch_url(url: str, timeout: int = 10) -> Optional[str]:
    """Fetch a URL, return content or None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "basis-arb-tool/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log(f"fetch error for {url}: {e}")
        return None


def fetch_github_api(url: str) -> Optional[dict]:
    """Fetch GitHub API endpoint, return parsed JSON or None."""
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN_RO", "")
    try:
        headers = {"Accept": "application/vnd.github.v3+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"GitHub API error for {url}: {e}")
        return None


def score_integration(tool: str, features: list[str], existing_features: set[str]) -> float:
    """Score 0-10 for how much this tool adds to the existing system."""
    score = 0.0
    new_features = [f for f in features if f.lower() not in existing_features]
    score += len(new_features) * 2.0  # 2 pts per new capability
    if tool in ("aggr.trade", "hydromancer.xyz", "hl.eco"):
        score += 1.5  # HL ecosystem bonus
    if "charting" in features or "visualization" in features:
        score += 1.0  # useful for analysis
    if "funding" in " ".join(features).lower():
        score += 2.0  # high value for carry detection
    if "api" in " ".join(features).lower() or "rest" in " ".join(features).lower():
        score += 1.5  # programmable access
    return min(score, 10.0)


# --- Tool profiles ---

TOOLS = {
    "aggr.trade": {
        "url": "https://aggr.trade",
        "description": "Hyperliquid ecosystem order-flow and liquidity aggregator",
        "known_features": [
            "aggregated orderbook data across HL venues",
            "large trade alerts / orderflow tracking",
            "liquidity heatmaps",
            "perp basis data",
            "open interest tracking",
        ],
        "api_available": "unknown",
        "cost": 0,
        "priority": "high",
        "integration_ideas": [
            "pull large trade alerts → early funding signal",
            "use liquidity data for slippage estimation in executor",
            "OI heatmap → detect OI accumulation before funding spikes",
        ],
    },
    "velo.xyz": {
        "url": "https://velo.xyz",
        "description": "Hyperliquid analytics and trading tool",
        "known_features": [
            "advanced charting for HL perps",
            "funding rate analytics",
            "orderflow tools",
            "strategy builder",
        ],
        "api_available": "unknown",
        "cost": 200,  # EUR/month
        "cost_justified": False,  # update based on bankroll
        "priority": "skip_until_bankroll",
        "integration_ideas": [
            "SKIP unless monthly carry profit exceeds €200",
        ],
    },
    "chart.kiyotaka.ai": {
        "url": "https://chart.kiyotaka.ai",
        "description": "Community-driven charting tool for Hyperliquid",
        "known_features": [
            "public charts for HL pairs",
            "technical analysis overlays",
            "community indicators",
        ],
        "api_available": "no (web only)",
        "cost": 0,
        "priority": "low",
        "integration_ideas": [
            "use public chart data for technical confirmation of basis moves",
            "low priority — no API access",
        ],
    },
    "hydromancer.xyz": {
        "url": "https://hydromancer.xyz",
        "description": "Hyperliquid ecosystem developer tools and data",
        "known_features": [
            "HL data APIs",
            "historical funding rate data",
            "OI tracking",
            "perpetual analytics",
        ],
        "api_available": "likely",
        "cost": 0,
        "priority": "high",
        "integration_ideas": [
            "pull historical funding rates for backtesting",
            "OI data for trap detection",
            "replace Loris free tier for HL-specific data",
        ],
    },
    "hl.eco": {
        "url": "https://hl.eco",
        "description": "Hyperliquid ecosystem link hub and data portal",
        "known_features": [
            "ecosystem tool directory",
            "HL protocol data",
            "developer resources",
        ],
        "api_available": "unknown",
        "cost": 0,
        "priority": "medium",
        "integration_ideas": [
            "use as a discovery portal for new HL ecosystem tools",
            "scan for new venues (Paradex, Aster) listed in ecosystem",
        ],
    },
    "github.com/hyperliquid-dex": {
        "url": "https://github.com/hyperliquid-dex/hyperliquid",
        "description": "Official Hyperliquid SDK and API documentation",
        "known_features": [
            "Python SDK for info and exchange APIs",
            "TypeScript SDK",
            "WebSocket support",
            "EIP-712 signing",
            "perp trading",
            "spot trading",
        ],
        "api_available": "yes (official)",
        "cost": 0,
        "priority": "critical",
        "integration_ideas": [
            "ALREADY INTEGRATED in basis_arb/execution/hyperliquid.py",
            "monitor for SDK updates / breaking changes",
            "check for new endpoint additions (new order types, new venues)",
        ],
    },
}


def run() -> int:
    log("Starting operator tools research")
    alerts = []

    # --- Bankroll check: is velo.xyz justified? ---
    bankroll_str = os.environ.get("BANKROLL_USD", "0")
    try:
        bankroll_usd = float(bankroll_str)
    except Exception:
        bankroll_usd = 0.0

    velo_monthly_eur = 200.0
    velo_cost_pct = (velo_monthly_eur / (bankroll_usd or 1)) * 100 if bankroll_usd else float("inf")
    velo_justified = bankroll_usd > 50_000  # very rough heuristic: need >$50k bankroll to justify €200/mo

    TOOLS["velo.xyz"]["cost_justified"] = velo_justified
    TOOLS["velo.xyz"]["bankroll_usd"] = bankroll_usd

    if not velo_justified:
        alerts.append({
            "tool": "velo.xyz",
            "type": "COST_SKIPPED",
            "cost_eur": velo_monthly_eur,
            "bankroll_usd": bankroll_usd,
            "note": f"Velo costs {velo_cost_pct:.1f}% of bankroll/mo — skip until bankroll > ~$50k",
        })

    # --- Check GitHub SDK for updates ---
    repo_data = fetch_github_api("https://api.github.com/repos/hyperliquid-dex/hyperliquid")
    sdk_version = None
    if repo_data:
        sdk_version = repo_data.get("default_branch", "main")
        TOOLS["github.com/hyperliquid-dex"]["repo_stars"] = repo_data.get("stargazers_count")
        TOOLS["github.com/hyperliquid-dex"]["repo_updated"] = repo_data.get("pushed_at")

    # --- Check hydromancer.xyz for API access ---
    hm_content = fetch_url("https://hydromancer.xyz")
    hydromancer_api_found = False
    if hm_content:
        if "api" in hm_content.lower() or "/v1" in hm_content or "/v2" in hm_content:
            hydromancer_api_found = True
        TOOLS["hydromancer.xyz"]["api_available"] = "confirmed" if hydromancer_api_found else "web_only"

    # --- Check aggr.trade ---
    aggr_content = fetch_url("https://aggr.trade")
    aggr_api_found = False
    if aggr_content:
        if "api" in aggr_content.lower() or "/rest" in aggr_content.lower():
            aggr_api_found = True
        TOOLS["aggr.trade"]["api_available"] = "confirmed" if aggr_api_found else "unknown"

    # --- Build integration scores ---
    # Features already in the tool (don't double-count)
    existing_features = {
        "funding rates", "perp funding", "open interest", "spot price",
        "orderbook", "Kelly sizing", "TGE trap detection", "drawdown tracking",
        "hyperliquid api", "signal pipeline",
    }

    integration_opportunities = []
    for tool_id, tool in TOOLS.items():
        score = score_integration(tool_id, tool["known_features"], existing_features)
        opp = {
            "tool": tool_id,
            "url": tool["url"],
            "score": score,
            "priority": tool["priority"],
            "cost": tool["cost"],
            "cost_justified": tool.get("cost_justified", True),
            "api_available": tool.get("api_available", "unknown"),
            "new_capabilities": [f for f in tool["known_features"]
                                 if f.lower() not in existing_features],
            "top_integration_idea": tool["integration_ideas"][0] if tool["integration_ideas"] else None,
        }
        integration_opportunities.append(opp)

    integration_opportunities.sort(key=lambda x: x["score"], reverse=True)

    report = {
        "ts": datetime.datetime.utcnow().isoformat(),
        "bankroll_usd": bankroll_usd,
        "velo_cost_justified": velo_justified,
        "sdk_version": sdk_version,
        "tools": TOOLS,
        "integration_opportunities": integration_opportunities,
        "alerts": alerts,
    }

    REPORT_FILE.write_text(json.dumps(report, indent=2, default=str))
    ALERT_FILE.write_text(json.dumps(alerts, indent=2, default=str))

    log(f"Research complete — {len(integration_opportunities)} tools evaluated")
    top = integration_opportunities[0]
    log(f"TOP OPPORTUNITY: {top['tool']} (score={top['score']:.1f}) — {top['top_integration_idea']}")

    if not velo_justified:
        log(f"VELO SKIPPED: costs {velo_cost_pct:.1f}% of bankroll/mo")

    return 0 if not alerts else 1


if __name__ == "__main__":
    sys.exit(run())
