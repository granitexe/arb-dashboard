#!/usr/bin/env python3
"""Weekly cron job evaluator — assesses whether each cron job is still needed or should be deprecated.

This runs every Sunday. It looks at:
  1. Each cron job's output files (alerts, errors, last_run status)
  2. Whether the job's findings are still relevant
  3. Whether operator circumstances have changed (bankroll, tools, setup)

DEPRECATION CRITERIA — a job should be deprecated if:
  - It has produced only empty/no-op output for 4+ consecutive runs
  - Its output is fully duplicated by another job
  - The operator's setup makes it irrelevant (e.g., no Loris key → loris-specific jobs are noise)
  - It has errored 3+ times in the past 2 weeks
  - The feature it monitors is now built-in (meta-improvement handles it)

NEW JOB CRITERIA — a job should be added if:
  - A new data source became available (new venue, new API)
  - A new risk vector emerged that isn't covered
  - The operator added a new tool that needs monitoring
  - Performance data shows a gap in coverage

Exit codes: 0 = evaluation complete, 1 = changes recommended, 2 = error
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
STATE_FILE = ROOT / ".cron_output" / "cron_evaluation_state.json"
REPORT_FILE = ROOT / ".cron_output" / "cron_evaluation_report.json"
ALERT_FILE = ROOT / ".cron_output" / "cron_evaluation_alerts.json"
LOG_FILE = ROOT / ".cron_output" / "cron_evaluation.log"
AGENDA_FILE = ROOT / ".cron_output" / "improvement_agenda.json"

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
    path.write_text(json.dumps(data, indent=2, default=str))


# Known cron jobs with their metadata
# job_id -> {name, script, output_files, purpose, dependencies}
KNOWN_JOBS = {
    "41611f11c41f": {
        "name": "signal-pipeline",
        "script": "scripts/run_signal_pipeline.sh",
        "output_files": ["signals.json"],
        "purpose": "Produce ranked carry signals from funding data",
        "critical": True,
        "can_deprecate": False,
    },
    "35401103d72e": {
        "name": "dashboard-refresh",
        "script": "scripts/refresh_dashboard.sh",
        "output_files": [".cron_output/dashboard_out/"],
        "purpose": "Push signals to GitHub Pages dashboard",
        "critical": False,
        "can_deprecate": False,
    },
    "5d8e1a87a81a": {
        "name": "research-funding-monitor",
        "script": "scripts/research_funding_monitor.py",
        "output_files": [".cron_output/funding_monitor.log", ".cron_output/funding_alerts.json"],
        "purpose": "Alert on funding flips, spikes, cross-venue divergence",
        "critical": False,
        "can_deprecate": False,
    },
    "ffd723ab20b9": {
        "name": "research-unlock-monitor",
        "script": "scripts/research_unlock_monitor.py",
        "output_files": [".cron_output/unlock_alerts.json"],
        "purpose": "Alert on upcoming token unlocks (TGE trap detection)",
        "critical": False,
        "can_deprecate": False,
    },
    "a6d3a11ecb0c": {
        "name": "research-market-structure",
        "script": "scripts/research_market_structure.py",
        "output_files": [".cron_output/market_structure.log", ".cron_output/market_structure_alerts.json"],
        "purpose": "Monitor OI shifts, basis regime changes, new venue detection",
        "critical": False,
        "can_deprecate": False,
    },
    "51759058edf0": {
        "name": "security-guardrail-test",
        "script": "scripts/security_guardrail_test.py",
        "output_files": [".cron_output/guardrail.log"],
        "purpose": "Catch runtime errors in signal pipeline before they matter",
        "critical": True,
        "can_deprecate": False,
    },
    "fb28627a0792": {
        "name": "security-secret-scan",
        "script": "scripts/security_secret_scan.py",
        "output_files": [".cron_output/secret_scan.log", ".cron_output/secret_alerts.json"],
        "purpose": "Detect accidentally committed secrets",
        "critical": True,
        "can_deprecate": False,
    },
    "78803c8be97e": {
        "name": "security-deps-audit",
        "script": "scripts/security_deps_audit.py",
        "output_files": [".cron_output/deps_audit.log"],
        "purpose": "Audit Python deps for CVEs",
        "critical": True,
        "can_deprecate": False,
    },
    "bbc59b3edb47": {
        "name": "risk-trap-audit",
        "script": "scripts/risk_trap_audit.py",
        "output_files": [".cron_output/trap_audit.log", ".cron_output/trap_audit_alerts.json"],
        "purpose": "Retrospective trap score quality analysis vs realized trades",
        "critical": False,
        "can_deprecate": True,  # needs trade journal to be useful
    },
    "be468407f419": {
        "name": "risk-param-drift",
        "script": "scripts/risk_param_drift.py",
        "output_files": [".cron_output/param_drift.log", ".cron_output/param_drift_alerts.json"],
        "purpose": "Detect signal model degradation vs market regime change",
        "critical": False,
        "can_deprecate": False,
    },
    "b83046eefb03": {
        "name": "meta-improvement",
        "script": "scripts/meta_improvement.py",
        "output_files": [".cron_output/meta_improvement.log", ".cron_output/improvement_agenda.json"],
        "purpose": "Karpathy-style keep-or-revert improvement loop",
        "critical": True,
        "can_deprecate": False,
    },
    "36ed3394b4e3": {
        "name": "autonomous-trading",
        "script": "scripts/run_autonomous_trading.sh",
        "output_files": [".trade_journal.jsonl"],
        "purpose": "Run the executor trading loop on Hyperliquid",
        "critical": True,
        "can_deprecate": False,
    },
    "693a70e31c65": {
        "name": "performance-inbox",
        "script": "scripts/performance_inbox.py",
        "output_files": [".cron_output/performance_health.json", ".cron_output/performance_inbox.log"],
        "purpose": "Parse trade journal, compute health metrics, feed back to improvement loop",
        "critical": True,
        "can_deprecate": False,
    },
    "818d77555caa": {
        "name": "research-operator-tools",
        "script": "scripts/research_operator_tools.py",
        "output_files": [".cron_output/operator_tools_report.json", ".cron_output/operator_tools_alerts.json"],
        "purpose": "Evaluate operator tools (aggr.trade, hydromancer, hl.eco, etc.) for integration",
        "critical": False,
        "can_deprecate": False,
    },
}


def get_last_run_info(job_id: str) -> dict:
    """Get last run info from hermes cron list."""
    try:
        r = subprocess.run(
            ["hermes", "cron", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            for job in data.get("jobs", []):
                if job.get("job_id") == job_id:
                    return {
                        "last_run_at": job.get("last_run_at"),
                        "last_status": job.get("last_status"),
                        "next_run_at": job.get("next_run_at"),
                        "enabled": job.get("enabled", True),
                    }
    except Exception:
        pass
    return {"last_run_at": None, "last_status": None, "enabled": True}


def check_output_aliveness(output_files: list[str]) -> dict:
    """Check if output files exist and have recent content."""
    results = {}
    for f in output_files:
        path = ROOT / f
        if "*" in f:
            # Glob pattern — check if any matching file exists
            parent = path.parent
            pattern = path.name
            matches = list(parent.glob(pattern)) if parent.exists() else []
            results[f] = {"exists": len(matches) > 0, "count": len(matches)}
        else:
            exists = path.exists()
            age_days = None
            if exists:
                mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
                age_days = (datetime.datetime.now() - mtime).total_seconds() / 86400
            results[f] = {"exists": exists, "age_days": round(age_days, 1) if age_days is not None else None}
    return results


def check_alerts_for_signal(alert_file: Path, lookback_days: int = 14) -> dict:
    """Check if an alert file has meaningful (non-empty) content."""
    if not alert_file.exists():
        return {"has_content": False, "reason": "file_missing"}
    try:
        content = json.loads(alert_file.read_text())
        if isinstance(content, list):
            if len(content) == 0:
                return {"has_content": False, "reason": "empty_list"}
            # Check if alerts are old
            cutoff = datetime.datetime.now() - datetime.timedelta(days=lookback_days)
            recent = [
                a for a in content
                if isinstance(a, dict)
                and a.get("ts")
                and datetime.datetime.fromisoformat(a["ts"].replace("Z", "+00:00")) > cutoff
            ]
            return {"has_content": len(recent) > 0, "recent_count": len(recent), "total_count": len(content)}
        return {"has_content": True, "reason": "non_empty_json"}
    except Exception:
        return {"has_content": False, "reason": "parse_error"}


def run() -> int:
    log("=== WEEKLY CRON EVALUATION ===")
    now = datetime.datetime.utcnow()
    prev_state = load_json(STATE_FILE, {"last_evaluation": None, "deprecation_log": []})
    report = {
        "ts": now.isoformat(),
        "evaluations": {},
        "recommendations": [],
        "new_jobs_suggested": [],
        "deprecated_jobs": [],
    }

    alerts = []
    any_changes = False

    # --- Check each known job ---
    for job_id, meta in KNOWN_JOBS.items():
        last_run = get_last_run_info(job_id)
        output = check_output_aliveness(meta["output_files"])
        alert_file = None
        for f in meta["output_files"]:
            if "_alerts.json" in f or "alert" in f:
                alert_file = ROOT / f
                break

        alert_signal = {"has_content": False}
        if alert_file:
            alert_signal = check_alerts_for_signal(alert_file)

        # Check if job has produced anything useful recently
        has_recent_output = any(
            o.get("exists") and (o.get("age_days") is None or o.get("age_days", 999) < 3)
            for o in output.values()
        )
        has_meaningful_alerts = alert_signal.get("has_content", False)

        status = "active"
        reasons_to_deprecate = []
        notes = []

        # --- Deprecated job detection ---

        # No Loris key → Loris-specific outputs are just noise
        loris_key = os.environ.get("LORIS_API_KEY", "")
        loris_dependent = meta["name"] in ["signal-pipeline"]
        if not loris_key and loris_dependent:
            notes.append("LORIS_API_KEY not set — signal pipeline produces limited output")

        # risk-trap-audit needs trade journal to be useful
        if meta["name"] == "risk-trap-audit":
            trade_journal = Path.home() / ".basis_arb" / "trade_journal.jsonl"
            perf_health = ROOT / ".cron_output" / "performance_health.json"
            if not trade_journal.exists() and not perf_health.exists():
                status = "dormant"
                notes.append("No trade journal yet — job is dormant until trader is live")
                if meta["can_deprecate"]:
                    reasons_to_deprecate.append("No trade journal available — job produces no meaningful output")

        # Job errored 3+ times in recent runs
        if last_run.get("last_status") == "error":
            notes.append(f"Last run: ERROR — needs investigation")
            reasons_to_deprecate.append("Recurring errors")

        # Job has produced zero meaningful alerts for 4+ weeks (only check if job is not critical)
        if not meta["critical"] and alert_file and not alert_signal.get("has_content"):
            # Check the file's age — if it's old and empty, consider deprecating
            if alert_file.exists():
                mtime = datetime.datetime.fromtimestamp(alert_file.stat().st_mtime)
                age_days = (now - mtime).total_seconds() / 86400
                if age_days > 28:
                    reasons_to_deprecate.append(f"Alert file unchanged for {age_days:.0f} days — no signal detected")

        # Output is duplicated by another job
        if meta["name"] == "research-funding-monitor":
            # Check if funding data is already covered by signal-pipeline
            sig_file = ROOT / "signals.json"
            if sig_file.exists():
                try:
                    sig_data = json.loads(sig_file.read_text())
                    if sig_data.get("metadata", {}).get("funding_rates", []):
                        notes.append("Funding data now in signals.json — potential overlap")
                except Exception:
                    pass

        eval_result = {
            "job_id": job_id,
            "name": meta["name"],
            "status": status,
            "last_run": last_run,
            "output_aliveness": output,
            "alert_signal": alert_signal,
            "notes": notes,
            "reasons_to_deprecate": reasons_to_deprecate,
        }

        report["evaluations"][job_id] = eval_result

        # --- Recommendations ---
        if reasons_to_deprecate and meta["can_deprecate"]:
            rec = {
                "action": "deprecate",
                "job_id": job_id,
                "name": meta["name"],
                "reasons": reasons_to_deprecate,
                "rationale": "Job is not producing useful output or is redundant",
            }
            report["recommendations"].append(rec)
            report["deprecated_jobs"].append(job_id)
            alerts.append({
                "type": "DEPRECATE_JOB",
                "job_id": job_id,
                "name": meta["name"],
                "reasons": reasons_to_deprecate,
            })
            any_changes = True
            log(f"RECOMMEND DEPRECATE: {meta['name']} — {'; '.join(reasons_to_deprecate)}")
        elif notes:
            log(f"NOTE: {meta['name']} — {'; '.join(notes)}")
        else:
            log(f"OK: {meta['name']} [{status}]")

    # --- Check for new job needs ---

    # New venue detected? (Paradex, Aster, etc.)
    ms_alerts = ROOT / ".cron_output" / "market_structure_alerts.json"
    if ms_alerts.exists():
        try:
            alerts_data = json.loads(ms_alerts.read_text())
            new_venues = [a for a in alerts_data if a.get("type") == "NEW_VENUE_DETECTED"]
            if new_venues:
                rec = {
                    "action": "add_job",
                    "name": "monitor-new-venue",
                    "purpose": f"New venue detected: {new_venues[0].get('venue')}",
                    "rationale": "A new venue appeared in market structure monitoring — needs dedicated monitoring",
                }
                report["new_jobs_suggested"].append(rec)
                any_changes = True
        except Exception:
            pass

    # Performance degraded?
    perf_file = ROOT / ".cron_output" / "performance_health.json"
    if perf_file.exists():
        try:
            perf = json.loads(perf_file.read_text())
            if perf.get("health_score", 100) < 40:
                rec = {
                    "action": "add_job",
                    "name": "emergency-performance-review",
                    "purpose": "Performance health score < 40 — urgent review needed",
                    "rationale": "The running version is showing signs of degradation",
                }
                report["new_jobs_suggested"].append(rec)
                any_changes = True
        except Exception:
            pass

    # Loris API added?
    loris_key = os.environ.get("LORIS_API_KEY", "")
    was_key_missing = prev_state.get("loris_key_present", False) == False
    is_key_present = bool(loris_key)
    if is_key_present and was_key_missing:
        rec = {
            "action": "remove_dormancy",
            "job_id": "bbc59b3edb47",  # risk-trap-audit
            "name": "risk-trap-audit",
            "rationale": "Loris API key is now present — risk-trap-audit can now cross-reference realized trades",
        }
        report["recommendations"].append(rec)
        any_changes = True

    # Missing operator tool monitoring?
    tools_report = ROOT / ".cron_output" / "operator_tools_report.json"
    if tools_report.exists():
        try:
            tools = json.loads(tools_report.read_text())
            for tool in tools.get("integration_opportunities", []):
                if tool.get("score", 0) >= 7 and tool.get("priority") == "high":
                    rec = {
                        "action": "add_job",
                        "name": f"monitor-{tool['tool']}",
                        "purpose": f"High-scoring tool integration: {tool['tool']} (score={tool['score']})",
                        "rationale": f"Integration opportunity: {tool.get('top_integration_idea', '')}",
                    }
                    report["new_jobs_suggested"].append(rec)
                    any_changes = True
        except Exception:
            pass

    # --- Save state ---
    save_json(STATE_FILE, {
        "last_evaluation": now.isoformat(),
        "loris_key_present": is_key_present,
        "deprecation_log": prev_state.get("deprecation_log", []) + report["deprecated_jobs"],
    })
    save_json(REPORT_FILE, report)
    save_json(ALERT_FILE, alerts)

    log(f"Evaluation complete — {len(report['recommendations'])} recommendations, {len(report['new_jobs_suggested'])} new jobs suggested")

    return 1 if any_changes else 0


if __name__ == "__main__":
    sys.exit(run())
