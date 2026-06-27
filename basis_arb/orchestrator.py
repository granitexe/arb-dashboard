#!/usr/bin/env python3
"""
basis_arb/orchestrator.py — Single smart orchestration brain.

Replaces 10+ separate cron jobs with ONE decision-based coordinator that is:
  LAZY      — only runs what actually needs to run, based on current state
  STATEFUL  — remembers what it did, what changed, and what still needs attention

Consolidated jobs:
  signal-pipeline          → produce ranked carry signals
  research-funding-monitor → funding flips, spikes, cross-venue divergence
  research-unlock-monitor  → TGE trap alerts
  research-market-structure→ OI shifts, basis regime changes, new venue detection
  risk-trap-audit          → trap score quality vs realized trades
  risk-param-drift         → signal model degradation detection
  performance-inbox        → trade journal → health metrics
  meta-improvement         → Karpathy-style keep-or-revert loop
  autonomous-trading       → executor trading loop on Hyperliquid
  security-guardrail-test  → catch runtime errors before they matter
  security-secret-scan     → detect accidentally committed secrets
  security-deps-audit      → audit Python deps for CVEs

Gap-filling: when the orchestrator encounters a problem without a tool to solve it,
it creates a gap-filling tool on the fly and logs it to the improvement agenda.

Exit codes:
  0  — nothing needed to run (all healthy / cooldowns active)
  1  — work was done
  2  — error
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Optional

# ── paths ──────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
SCRIPTS = ROOT / "scripts"
CRON_OUT = ROOT / ".cron_output"
STATE_FILE = CRON_OUT / "orchestrator_state.json"
LOG_FILE = CRON_OUT / "orchestrator.log"
ALERT_FILE = CRON_OUT / "orchestrator_alerts.json"
HEALTH_FILE = CRON_OUT / "performance_health.json"
AGENDA_FILE = CRON_OUT / "improvement_agenda.json"
GAP_LOG = CRON_OUT / "gap_filling_log.json"

CRON_OUT.mkdir(parents=True, exist_ok=True)

# ── venv activation ─────────────────────────────────────────────────────────

if sys.prefix == sys.base_prefix:
    venv_python = ROOT / ".venv" / "bin" / "python3"
    if venv_python.exists():
        os.environ["VENV_ACTIVATED"] = "1"
        os.execv(str(venv_python), [str(venv_python), __file__] + sys.argv[1:])

# ── helpers ─────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    LOG_FILE.write_text(LOG_FILE.read_text() + line + "\n" if LOG_FILE.exists() else line + "\n")


def load_json(path: Path, default: Any = None) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default if default is not None else {}


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))


def run_cmd(cmd: list[str], cwd: Path = ROOT, timeout: int = 300,
            env_extra: Optional[dict] = None) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    if env_extra:
        env.update(env_extra)
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"
    except Exception as e:
        return -1, "", str(e)


# ── gap-filling tool factory ─────────────────────────────────────────────────

GAP_TOOL_TEMPLATE = '''\
#!/usr/bin/env python3
"""Auto-generated gap-filling tool: {name}

Created by orchestrator on {ts}.
Problem: {problem}
Suggested fix: {suggestion}
"""

from __future__ import annotations
import sys, os, datetime

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

LOG_FILE = ROOT / ".cron_output" / "{log_name}"
ALERT_FILE = ROOT / ".cron_output" / "{alert_name}"

def log(msg: str) -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] [GAP-FILL] {{msg}}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(LOG_FILE.read_text() + line + "\\n" if LOG_FILE.exists() else line + "\\n")

def run() -> int:
    log("Gap-fill tool `{name}` started")
    # TODO: implement {suggestion}
    # Put your implementation here
    log("Gap-fill tool `{name}` finished — implement the TODO above")
    return 0

if __name__ == "__main__":
    sys.exit(run())
'''


def create_gap_tool(problem: str, suggestion: str, name: str) -> Path:
    """Write a new gap-filling tool script and register it."""
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    script_path = SCRIPTS / f"gap_fill_{safe_name}.py"
    if script_path.exists():
        log(f"Gap-fill tool {script_path.name} already exists, skipping creation")
        return script_path

    ts = datetime.datetime.utcnow().isoformat()
    content = GAP_TOOL_TEMPLATE.format(
        name=safe_name,
        ts=ts,
        problem=problem,
        suggestion=suggestion,
        log_name=f"gap_fill_{safe_name}.log",
        alert_name=f"gap_fill_{safe_name}_alerts.json",
    )
    script_path.write_text(textwrap.dedent(content))
    script_path.chmod(0o755)
    log(f"CREATED GAP-FILL TOOL: {script_path.name}")
    return script_path


def register_gap_in_agenda(problem: str, suggestion: str, name: str) -> None:
    """Add gap-fill item to the improvement agenda so meta-improvement can pick it up."""
    agenda = load_json(AGENDA_FILE, {"version": 1, "items": []})
    item_id = f"gap-fill-{hashlib.md5(name.encode()).hexdigest()[:8]}"
    # Avoid duplicates
    existing = [i for i in agenda.get("items", []) if i["id"] == item_id]
    if existing:
        return
    agenda.setdefault("items", []).append({
        "id": item_id,
        "category": "gap-fill",
        "priority": 1,
        "status": "pending",
        "description": f"Gap-fill: {problem}",
        "suggested_action": suggestion,
        "version_tag": "gap-fill",
        "created_by": "orchestrator",
        "created_at": datetime.datetime.utcnow().isoformat(),
    })
    save_json(AGENDA_FILE, agenda)
    log(f"Registered gap-fill [{item_id}] in improvement agenda")


def handle_gap(problem: str, suggestion: str, name: str) -> Optional[Path]:
    """Detect a gap, create a tool for it, register it in agenda."""
    log(f"GAP DETECTED: {problem}", level="WARNING")
    path = create_gap_tool(problem, suggestion, name)
    register_gap_in_agenda(problem, suggestion, name)
    # Log to gap log
    gap_log = load_json(GAP_LOG, [])
    gap_log.append({
        "ts": datetime.datetime.utcnow().isoformat(),
        "problem": problem,
        "suggestion": suggestion,
        "name": name,
        "script": str(path),
    })
    save_json(GAP_LOG, gap_log)
    return path


# ── condition evaluators ─────────────────────────────────────────────────────
# Each returns (should_run: bool, reason: str)

def _file_aged(path: Path, hours: int) -> bool:
    if not path.exists():
        return True
    age_s = datetime.datetime.now().timestamp() - path.stat().st_mtime
    return age_s > hours * 3600


def _check_signal_stale() -> tuple[bool, str]:
    """Signal pipeline should run if signals.json is >1h old."""
    path = ROOT / "signals.json"
    if _file_aged(path, hours=1):
        return True, "signals.json missing or >1h old"
    return False, "signals.json fresh"


def _check_performance_stale() -> tuple[bool, str]:
    """Performance inbox should run if health file >30 min old."""
    if _file_aged(HEALTH_FILE, hours=0.5):
        return True, "performance_health.json missing or >30min old"
    return False, "performance_health.json fresh"


def _check_meta_improvement_needed() -> tuple[bool, str]:
    """Meta-improvement should run if no recent activity on feature branch."""
    state = load_json(STATE_FILE, {})
    last_meta = state.get("last_meta_improvement")
    if not last_meta:
        return True, "never run meta-improvement"
    last_dt = datetime.datetime.fromisoformat(last_meta)
    age_h = (datetime.datetime.now() - last_dt).total_seconds() / 3600
    if age_h > 2:
        return True, f"meta-improvement last ran {age_h:.1f}h ago"
    return False, f"meta-improvement ran {age_h:.1f}h ago"


def _check_funding_alerts_needed() -> tuple[bool, str]:
    """Research funding monitor if alert file >1h old."""
    alert = CRON_OUT / "funding_alerts.json"
    if _file_aged(alert, hours=1):
        return True, "funding_alerts.json missing or >1h old"
    return False, "funding_alerts fresh"


def _check_unlock_alerts_needed() -> tuple[bool, str]:
    """Research unlock monitor if >2h since last check."""
    alert = CRON_OUT / "unlock_alerts.json"
    if _file_aged(alert, hours=2):
        return True, "unlock_alerts.json missing or >2h old"
    return False, "unlock_alerts fresh"


def _check_market_structure_needed() -> tuple[bool, str]:
    """Market structure if >1h old."""
    alert = CRON_OUT / "market_structure_alerts.json"
    if _file_aged(alert, hours=1):
        return True, "market_structure_alerts.json missing or >1h old"
    return False, "market_structure fresh"


def _check_risk_trap_audit_needed() -> tuple[bool, str]:
    """Risk trap audit needs trade journal to be useful."""
    journal = Path.home() / ".basis_arb" / "trade_journal.jsonl"
    perf_health = HEALTH_FILE
    has_data = journal.exists() or perf_health.exists()
    if not has_data:
        return False, "no trade journal yet — dormant until trader is live"
    if _file_aged(CRON_OUT / "trap_audit_alerts.json", hours=6):
        return True, "trap_audit stale or missing"
    return False, "trap_audit fresh"


def _check_param_drift_needed() -> tuple[bool, str]:
    """Param drift if >6h old."""
    if _file_aged(CRON_OUT / "param_drift_alerts.json", hours=6):
        return True, "param_drift stale or missing"
    return False, "param_drift fresh"


def _check_guardrail_needed() -> tuple[bool, str]:
    """Security guardrail test if >12h old."""
    if _file_aged(CRON_OUT / "guardrail.log", hours=12):
        return True, "guardrail check stale or missing"
    return False, "guardrail fresh"


def _check_secret_scan_needed() -> tuple[bool, str]:
    """Secret scan if >24h old."""
    if _file_aged(CRON_OUT / "secret_scan.log", hours=24):
        return True, "secret_scan stale or missing"
    return False, "secret_scan fresh"


def _check_deps_audit_needed() -> tuple[bool, str]:
    """Deps audit if >24h old."""
    if _file_aged(CRON_OUT / "deps_audit.log", hours=24):
        return True, "deps_audit stale or missing"
    return False, "deps_audit fresh"


def _check_autonomous_trading_needed() -> tuple[bool, str]:
    """Autonomous trading if Hyperliquid is enabled and not paused."""
    env = os.environ.get("HYPERLIQUID_ENABLED", "").lower()
    if env != "true":
        return False, "HYPERLIQUID_ENABLED != true"
    state = load_json(STATE_FILE, {})
    paused = state.get("trading_paused", False)
    if paused:
        return False, "trading paused via orchestrator state"
    # Check if executor is already running
    rc, out, _ = run_cmd(["pgrep", "-f", "autonomous_trading"], timeout=5)
    if rc == 0 and out.strip():
        return False, "autonomous trading already running"
    return True, "Hyperliquid enabled and trading not paused"


def _check_health_critical() -> tuple[bool, str]:
    """If health score is very low, we need emergency meta-review."""
    health = load_json(HEALTH_FILE, {})
    score = health.get("health_score")
    if score is not None and score < 30:
        return True, f"health_score={score} — critical"
    return False, f"health_score={score} — ok"


def _check_cron_evaluation_needed() -> tuple[bool, str]:
    """Weekly cron evaluation — Sunday or if never run."""
    state = load_json(STATE_FILE, {})
    last_eval = state.get("last_cron_evaluation")
    if not last_eval:
        return True, "never run cron evaluation"
    last_dt = datetime.datetime.fromisoformat(last_eval)
    days_since = (datetime.datetime.now() - last_dt).days
    if days_since >= 7:
        return True, f"last cron evaluation was {days_since} days ago"
    return False, f"cron evaluation ran {days_since} days ago"


# ── job runners ─────────────────────────────────────────────────────────────

def run_signal_pipeline() -> tuple[bool, str]:
    """Run the signal pipeline shell script."""
    script = SCRIPTS / "run_signal_pipeline.sh"
    if not script.exists():
        # Try running the pipeline module directly
        try:
            from basis_arb import signal_pipeline
            log("run_signal_pipeline.sh not found — running pipeline module directly")
            # Minimal run
            cfg = signal_pipeline.PipelineConfig()
            report = signal_pipeline.run_pipeline(cfg)
            signal_pipeline.write_json_report(report, str(ROOT / "signals.json"))
            return True, "pipeline module ran ok"
        except Exception as e:
            return False, f"signal pipeline failed: {e}"
    rc, out, err = run_cmd(["bash", str(script)], timeout=180)
    ok = rc == 0
    return ok, f"signal-pipeline rc={rc}: {err[:200] if err else out[:200]}"


def run_performance_inbox() -> tuple[bool, str]:
    """Run performance_inbox.py."""
    script = SCRIPTS / "performance_inbox.py"
    rc, out, err = run_cmd(["python3", str(script)], timeout=60)
    return rc == 0, f"performance_inbox rc={rc}: {err[:200] if err else out[:200]}"


def run_meta_improvement() -> tuple[bool, str]:
    """Run meta_improvement.py."""
    script = SCRIPTS / "meta_improvement.py"
    rc, out, err = run_cmd(["python3", str(script)], timeout=600)
    return rc == 0, f"meta_improvement rc={rc}: {err[:200] if err else out[:200]}"


def run_research_funding_monitor() -> tuple[bool, str]:
    """Run research_funding_monitor.py."""
    script = SCRIPTS / "research_funding_monitor.py"
    rc, out, err = run_cmd(["python3", str(script)], timeout=60)
    return rc == 0, f"funding_monitor rc={rc}: {err[:200] if err else out[:200]}"


def run_research_unlock_monitor() -> tuple[bool, str]:
    """Run research_unlock_monitor.py."""
    script = SCRIPTS / "research_unlock_monitor.py"
    rc, out, err = run_cmd(["python3", str(script)], timeout=60)
    return rc == 0, f"unlock_monitor rc={rc}: {err[:200] if err else out[:200]}"


def run_research_market_structure() -> tuple[bool, str]:
    """Run research_market_structure.py."""
    script = SCRIPTS / "research_market_structure.py"
    rc, out, err = run_cmd(["python3", str(script)], timeout=60)
    return rc == 0, f"market_structure rc={rc}: {err[:200] if err else out[:200]}"


def run_risk_trap_audit() -> tuple[bool, str]:
    """Run risk_trap_audit.py."""
    script = SCRIPTS / "risk_trap_audit.py"
    rc, out, err = run_cmd(["python3", str(script)], timeout=120)
    return rc == 0, f"trap_audit rc={rc}: {err[:200] if err else out[:200]}"


def run_risk_param_drift() -> tuple[bool, str]:
    """Run risk_param_drift.py."""
    script = SCRIPTS / "risk_param_drift.py"
    rc, out, err = run_cmd(["python3", str(script)], timeout=120)
    return rc == 0, f"param_drift rc={rc}: {err[:200] if err else out[:200]}"


def run_security_guardrail() -> tuple[bool, str]:
    """Run security_guardrail_test.py."""
    script = SCRIPTS / "security_guardrail_test.py"
    rc, out, err = run_cmd(["python3", str(script)], timeout=60)
    return rc == 0, f"guardrail rc={rc}: {err[:200] if err else out[:200]}"


def run_security_secret_scan() -> tuple[bool, str]:
    """Run security_secret_scan.py."""
    script = SCRIPTS / "security_secret_scan.py"
    rc, out, err = run_cmd(["python3", str(script)], timeout=30)
    return rc == 0, f"secret_scan rc={rc}: {err[:200] if err else out[:200]}"


def run_security_deps_audit() -> tuple[bool, str]:
    """Run security_deps_audit.py."""
    script = SCRIPTS / "security_deps_audit.py"
    rc, out, err = run_cmd(["python3", str(script)], timeout=60)
    return rc == 0, f"deps_audit rc={rc}: {err[:200] if err else out[:200]}"


def run_autonomous_trading() -> tuple[bool, str]:
    """Run run_autonomous_trading.sh."""
    script = SCRIPTS / "run_autonomous_trading.sh"
    if not script.exists():
        return False, "run_autonomous_trading.sh not found"
    # Run in background — don't block
    try:
        subprocess.Popen(
            ["bash", str(script)],
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        return True, "autonomous trading started"
    except Exception as e:
        return False, f"failed to start autonomous trading: {e}"


def run_cron_evaluation() -> tuple[bool, str]:
    """Run cron_evaluation.py."""
    script = SCRIPTS / "cron_evaluation.py"
    rc, out, err = run_cmd(["python3", str(script)], timeout=120)
    return rc == 0, f"cron_evaluation rc={rc}: {err[:200] if err else out[:200]}"


def run_refresh_dashboard() -> tuple[bool, str]:
    """Run refresh_dashboard.sh."""
    script = SCRIPTS / "refresh_dashboard.sh"
    rc, out, err = run_cmd(["bash", str(script)], timeout=60)
    return rc == 0, f"dashboard_refresh rc={rc}: {err[:200] if err else out[:200]}"


# ── gap detection ────────────────────────────────────────────────────────────

def detect_gaps() -> list:
    """Look for problems that have no tool to solve them."""
    gaps = []

    # Check: market structure alerts mention new venue but no monitoring tool
    ms_alerts = CRON_OUT / "market_structure_alerts.json"
    if ms_alerts.exists():
        try:
            alerts = json.loads(ms_alerts.read_text())
            new_venues = [a for a in alerts if a.get("type") == "NEW_VENUE_DETECTED"]
            if new_venues:
                venue = new_venues[0].get("venue", "unknown")
                gaps.append({
                    "problem": f"New venue detected: {venue}",
                    "suggestion": f"Build monitoring for new venue: {venue}",
                    "name": f"monitor-{venue}",
                })
        except Exception:
            pass

    # Check: performance degraded but no emergency review scheduled
    health = load_json(HEALTH_FILE, {})
    if health.get("health_score", 100) < 40:
        gaps.append({
            "problem": f"Performance health score is {health.get('health_score')}",
            "suggestion": "Create emergency performance review tool",
            "name": "emergency-performance-review",
        })

    # Check: signal pipeline errors
    sig_file = ROOT / "signals.json"
    if sig_file.exists():
        try:
            sig_data = json.loads(sig_file.read_text())
            errors = sig_data.get("errors", [])
            if errors:
                gap_name = f"fix-signal-errors-{len(errors)}"
                gaps.append({
                    "problem": f"Signal pipeline has {len(errors)} errors",
                    "suggestion": f"Fix errors: {'; '.join(errors[:3])}",
                    "name": gap_name,
                })
        except Exception:
            pass

    return gaps


# ── main decision loop ───────────────────────────────────────────────────────

def decide_and_run() -> tuple[int, list]:
    """
    Evaluate all conditions and run whatever needs to run.
    Returns (work_done_count, list_of_descriptions).
    """
    state = load_json(STATE_FILE, {})
    now = datetime.datetime.utcnow()
    actions_taken = []
    alerts = []

    # ── Critical health check ──────────────────────────────────────────────
    needs_urgent, reason = _check_health_critical()
    if needs_urgent:
        log(f"CRITICAL: {reason}", level="WARNING")
        alerts.append({"type": "HEALTH_CRITICAL", "reason": reason})
        # Run performance inbox + meta-improvement immediately
        ok, msg = run_performance_inbox()
        actions_taken.append(f"performance_inbox: {msg}")
        ok2, msg2 = run_meta_improvement()
        actions_taken.append(f"meta_improvement: {msg2}")

    # ── Signal pipeline (critical — feeds everything else) ─────────────────
    needs_sig, reason = _check_signal_stale()
    if needs_sig:
        log(f"Running signal-pipeline: {reason}")
        ok, msg = run_signal_pipeline()
        actions_taken.append(f"signal-pipeline: {msg}")
        if not ok:
            alerts.append({"type": "SIGNAL_PIPELINE_ERROR", "msg": msg})
            # Create gap-fill tool for signal pipeline failure
            handle_gap(
                problem=f"Signal pipeline failed: {msg}",
                suggestion="Debug signal pipeline errors and fix data sources",
                name="fix-signal-pipeline",
            )
        # After signal pipeline, refresh dashboard
        ok3, msg3 = run_refresh_dashboard()
        actions_taken.append(f"refresh_dashboard: {msg3}")

    # ── Performance inbox ──────────────────────────────────────────────────
    needs_perf, reason = _check_performance_stale()
    if needs_perf:
        log(f"Running performance_inbox: {reason}")
        ok, msg = run_performance_inbox()
        actions_taken.append(f"performance_inbox: {msg}")
        if not ok:
            alerts.append({"type": "PERFORMANCE_INBOX_ERROR", "msg": msg})

    # ── Meta-improvement ───────────────────────────────────────────────────
    needs_meta, reason = _check_meta_improvement_needed()
    if needs_meta:
        log(f"Running meta_improvement: {reason}")
        ok, msg = run_meta_improvement()
        actions_taken.append(f"meta_improvement: {msg}")
        state["last_meta_improvement"] = now.isoformat()

    # ── Research monitors ──────────────────────────────────────────────────
    needs_funding, reason = _check_funding_alerts_needed()
    if needs_funding:
        log(f"Running research_funding_monitor: {reason}")
        ok, msg = run_research_funding_monitor()
        actions_taken.append(f"research_funding_monitor: {msg}")

    needs_unlock, reason = _check_unlock_alerts_needed()
    if needs_unlock:
        log(f"Running research_unlock_monitor: {reason}")
        ok, msg = run_research_unlock_monitor()
        actions_taken.append(f"research_unlock_monitor: {msg}")

    needs_ms, reason = _check_market_structure_needed()
    if needs_ms:
        log(f"Running research_market_structure: {reason}")
        ok, msg = run_research_market_structure()
        actions_taken.append(f"research_market_structure: {msg}")

    # ── Risk jobs ──────────────────────────────────────────────────────────
    needs_trap, reason = _check_risk_trap_audit_needed()
    if needs_trap:
        log(f"Running risk_trap_audit: {reason}")
        ok, msg = run_risk_trap_audit()
        actions_taken.append(f"risk_trap_audit: {msg}")

    needs_drift, reason = _check_param_drift_needed()
    if needs_drift:
        log(f"Running risk_param_drift: {reason}")
        ok, msg = run_risk_param_drift()
        actions_taken.append(f"risk_param_drift: {msg}")

    # ── Security jobs ──────────────────────────────────────────────────────
    needs_guard, reason = _check_guardrail_needed()
    if needs_guard:
        log(f"Running security_guardrail: {reason}")
        ok, msg = run_security_guardrail()
        actions_taken.append(f"security_guardrail: {msg}")

    needs_secrets, reason = _check_secret_scan_needed()
    if needs_secrets:
        log(f"Running security_secret_scan: {reason}")
        ok, msg = run_security_secret_scan()
        actions_taken.append(f"security_secret_scan: {msg}")

    needs_deps, reason = _check_deps_audit_needed()
    if needs_deps:
        log(f"Running security_deps_audit: {reason}")
        ok, msg = run_security_deps_audit()
        actions_taken.append(f"security_deps_audit: {msg}")

    # ── Autonomous trading ─────────────────────────────────────────────────
    needs_trade, reason = _check_autonomous_trading_needed()
    if needs_trade:
        log(f"Starting autonomous_trading: {reason}")
        ok, msg = run_autonomous_trading()
        actions_taken.append(f"autonomous_trading: {msg}")

    # ── Cron evaluation (weekly) ───────────────────────────────────────────
    needs_eval, reason = _check_cron_evaluation_needed()
    if needs_eval:
        log(f"Running cron_evaluation: {reason}")
        ok, msg = run_cron_evaluation()
        actions_taken.append(f"cron_evaluation: {msg}")
        state["last_cron_evaluation"] = now.isoformat()

    # ── Gap detection ──────────────────────────────────────────────────────
    gaps = detect_gaps()
    for gap in gaps:
        path = handle_gap(gap["problem"], gap["suggestion"], gap["name"])
        actions_taken.append(f"gap-fill-created: {path.name if path else 'unknown'}")
        alerts.append({
            "type": "GAP_DETECTED",
            "problem": gap["problem"],
            "script": str(path) if path else None,
        })

    # Save alerts
    if alerts:
        save_json(ALERT_FILE, alerts)

    # Update state
    state["last_run"] = now.isoformat()
    state["actions_last_run"] = actions_taken
    save_json(STATE_FILE, state)

    return len(actions_taken), actions_taken


# ── orchestrator ─────────────────────────────────────────────────────────────

def run() -> int:
    log("=== ORCHESTRATOR START ===")
    now = datetime.datetime.utcnow()

    # Dedupe: if another instance ran in last 5 min, skip
    state = load_json(STATE_FILE, {})
    last_run = state.get("last_run")
    if last_run:
        try:
            last_dt = datetime.datetime.fromisoformat(last_run)
            age_s = (now - last_dt).total_seconds()
            if age_s < 300:
                log(f"Skipping — last run was {age_s:.0f}s ago")
                return 0
        except Exception:
            pass

    # Mark running
    (CRON_OUT / "orchestrator.running").write_text(now.isoformat())

    try:
        count, actions = decide_and_run()
        log(f"=== ORCHESTRATOR DONE: {count} actions ===")
        for a in actions:
            log(f"  → {a}")
        return 1 if count > 0 else 0
    except Exception as e:
        log(f"ERROR: {e}", level="ERROR")
        return 2
    finally:
        # Cleanup running marker
        try:
            (CRON_OUT / "orchestrator.running").unlink(missing_ok=True)
            (CRON_OUT / "orchestrator.finished").write_text(
                datetime.datetime.utcnow().isoformat()
            )
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(run())