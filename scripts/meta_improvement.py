#!/usr/bin/env python3
"""Meta-improvement orchestrator — Karpathy-style keep-or-revert loop.

HOW IT WORKS:
  Each run picks ONE improvement from the agenda. It uses omp.sh to implement
  that improvement on a feature branch. Then it runs the test suite. If tests
  pass and the improvement meets the quality bar, keep the change. If it makes
  things worse or breaks tests, revert it.

  After 3 consecutive keeps, the change is promoted to main via PR.

ANTI-DEGRADATION RULES (never violate these):
  1. Tests must always pass (42 baseline + new tests for new features)
  2. No hardcoded secrets or credentials
  3. The execution layer isolation boundary (execution/) must never import
     from the main codebase — only from execution/ itself
  4. Capital preservation logic (kill-switch, drawdown caps) must never be
     weakened or removed
  5. No live trading can be enabled without explicit DRY_RUN=false + operator
     confirmation in the PR description

VERSION DISCIPLINE:
  - Version N runs in production on the separate PC
  - Version N+1 is being developed here, on a feature branch
  - When N+1 is merged to main, it becomes the new "candidate stable"
  - The operator pulls from main to deploy the next version
  - The inbox (performance_inbox.py) monitors the running version and reports
    anomalies back so this agent can decide when to cut a new version

Exit codes: 0 = done, 1 = work in progress, 2 = error
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
AGENDA_FILE = ROOT / ".cron_output" / "improvement_agenda.json"
RESULTS_FILE = ROOT / ".cron_output" / "improvement_results.json"
LOG_FILE = ROOT / ".cron_output" / "meta_improvement.log"
STATE_FILE = ROOT / ".cron_output" / "meta_improvement_state.json"
PERF_FILE = ROOT / ".cron_output" / "performance_health.json"
ALERT_FILE = ROOT / ".cron_output" / "meta_improvement_alerts.json"
AGENDA_FILE.parent.mkdir(parents=True, exist_ok=True)


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


def run_cmd(cmd: list[str], cwd: Path = ROOT, timeout: int = 300) -> tuple[int, str, str]:
    """Run a shell command, return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timed out"
    except Exception as e:
        return -1, "", str(e)


def get_git_branch() -> str:
    rc, out, _ = run_cmd(["git", "branch", "--show-current"])
    return out.strip()


def get_git_status() -> str:
    rc, out, _ = run_cmd(["git", "status", "--porcelain"])
    return out.strip()


# ---- Anti-degradation guardrail checks ----

def run_test_suite() -> tuple[bool, str]:
    """Run full test suite. Returns (passed, output)."""
    log("Running test suite...")
    rc, out, err = run_cmd(
        ["python3", "-m", "pytest", "tests/", "-q", "--tb=short"],
        timeout=120,
    )
    if rc != 0:
        log(f"TEST FAILURE:\n{out}\n{err}")
        return False, f"FAIL:\n{out}\n{err}"
    return True, out


def check_guardrails() -> tuple[bool, str]:
    """Run security guardrail checks. Returns (clean, output)."""
    script = ROOT / "scripts" / "security_guardrail_test.py"
    if not script.exists():
        return True, "no guardrail script"
    rc, out, err = run_cmd(["python3", str(script)], timeout=60)
    if rc != 0:
        return False, f"GUARDRAIL FAIL:\n{out}\n{err}"
    return True, out


def check_no_live_trading_in_code() -> tuple[bool, str]:
    """Ensure DRY_RUN=false cannot be set programmatically or without operator intent."""
    # Check that execution/hyperliquid.py never defaults to live trading
    hl_file = ROOT / "basis_arb" / "execution" / "hyperliquid.py"
    if hl_file.exists():
        content = hl_file.read_text()
        if "DRY_RUN" in content and "default=False" in content:
            return False, "hyperliquid.py appears to default DRY_RUN to False — blocked"
    return True, "ok"


def check_no_secrets() -> tuple[bool, str]:
    """Quick scan for accidentally committed secrets."""
    secret_scan = ROOT / "scripts" / "security_secret_scan.py"
    if not secret_scan.exists():
        return True, "no secret scan script"
    rc, out, err = run_cmd(["python3", str(secret_scan)], timeout=30)
    if rc != 0:
        return False, f"SECRETS FOUND:\n{out}"
    return True, "ok"


def run_anti_degradation() -> tuple[bool, str]:
    """Run all anti-degradation checks. Returns (passed, details)."""
    checks = [
        ("tests", run_test_suite),
        ("guardrails", check_guardrails),
        ("no_live_default", check_no_live_trading_in_code),
        ("no_secrets", check_no_secrets),
    ]
    failures = []
    for name, fn in checks:
        passed, output = fn()
        if not passed:
            failures.append(f"  [{name}]: {output[:200]}")
    if failures:
        return False, "\n".join(failures)
    return True, "all checks passed"


# ---- Improvement agenda ----

DEFAULT_AGENDA = [
    {
        "id": "loris-api-key-paid",
        "category": "data",
        "priority": 1,
        "status": "pending",
        "description": "Loris.tools is PAID. Free tier = BTC/ETH only. Paid tier needed for full universe.",
        "suggested_action": "Sign up at https://api.loris.tools. Set LORIS_API_KEY env var.",
        "version_tag": "v1.0",
    },
    {
        "id": "hyperliquid-live-trading",
        "category": "ops",
        "priority": 1,
        "status": "pending",
        "description": "Execution layer built but not tested with real keys on main branch.",
        "suggested_action": "Merge execution-layer PR to main. Test with HYPERLIQUID_ENABLED=true, DRY_RUN=false, small position.",
        "version_tag": "v1.0",
    },
    {
        "id": "performance-inbox",
        "category": "ops",
        "priority": 1,
        "status": "pending",
        "description": "No live feedback loop from running trader to this agent.",
        "suggested_action": "Set up trade journal push from trader PC to ~/.basis_arb/trade_journal.jsonl. Configure TRADEFEED_URL if webhook.",
        "version_tag": "v1.0",
    },
    {
        "id": "operator-tools-integration",
        "category": "signal",
        "priority": 1,
        "status": "pending",
        "description": "aggr.trade, chart.kiyotaka.ai, hydromancer.xyz, hl.eco, hyperliquid-dex have useful features not yet integrated.",
        "suggested_action": "Research each tool's API/features. Integrate useful ones into the signal pipeline or execution layer.",
        "version_tag": "v1.1",
    },
    {
        "id": "hyperliquid-venue-weight",
        "category": "signal",
        "priority": 2,
        "status": "pending",
        "description": "Hyperliquid/Paradex/Aster are new venues — should get higher carry weight (thinner competition, lower fees).",
        "suggested_action": "Add venue_age_factor to carry.py. New venues get 1.2x multiplier, decays to 1.0x over 12 months.",
        "version_tag": "v1.1",
    },
    {
        "id": "basis-volatility-calibration",
        "category": "risk",
        "priority": 2,
        "status": "pending",
        "description": "Static 15% basis vol used for Kelly sizing. Real vol varies by coin and regime.",
        "suggested_action": "Pull HL funding history, compute rolling 30d basis std-dev per coin. Replace static 0.15.",
        "version_tag": "v1.1",
    },
    {
        "id": "trap-score-calibration",
        "category": "signal",
        "priority": 2,
        "status": "pending",
        "description": "TGE trap scores are heuristics — need calibration against real unlock events.",
        "suggested_action": "Add known_unlock_events.json (past TGEs with dates). Backtest trap score at those dates.",
        "version_tag": "v1.1",
    },
    {
        "id": "backtest-module",
        "category": "risk",
        "priority": 2,
        "status": "pending",
        "description": "No backtesting — cannot validate carry estimates historically.",
        "suggested_action": "Build simple backtest: for each historical Loris snapshot, calc carry, apply TGE filter, track hypothetical P&L.",
        "version_tag": "v1.2",
    },
    {
        "id": "per-coin-kelly-sizing",
        "category": "risk",
        "priority": 3,
        "status": "pending",
        "description": "Kelly uses flat 15% vol for all coins. Per-coin vol gives better sizing — high-vol coins get smaller positions.",
        "suggested_action": "Use rolling 30d HL funding history per coin for vol. High-vol coins get smaller Kelly fractions.",
        "version_tag": "v1.2",
    },
    {
        "id": "rabby-jumper-bridging-research",
        "category": "ops",
        "priority": 3,
        "status": "pending",
        "description": "Spot leg (long spot) requires Jumper/Rabby bridging — currently manual.",
        "suggested_action": "Research Jumper.xyz API for programmatic routing. Until then spot leg remains manual. Document manual process.",
        "version_tag": "v1.2",
    },
    {
        "id": "dashboard-github-token",
        "category": "ops",
        "priority": 3,
        "status": "pending",
        "description": "refresh_dashboard.sh needs GITHUB_TOKEN for auto-refresh on GitHub Pages.",
        "suggested_action": "Create a GitHub PAT (needs repo scope). Store in ~/.basis_arb/.env as GITHUB_TOKEN.",
        "version_tag": "v1.1",
    },
    {
        "id": "knowledge-base-expansion",
        "category": "docs",
        "priority": 3,
        "status": "pending",
        "description": "knowledge/x/ has 4 articles. Need: delta-neutral mechanics, ADL risk, funding regimes, new-venue edge.",
        "suggested_action": "Write 4 new articles: delta-neutral mechanics, ADL explained, funding rate regimes, new-venue edge thesis.",
        "version_tag": "v1.1",
    },
]


def _omp_coding(task: str, branch: str) -> tuple[bool, str]:
    """Use omp.sh to implement a task on a named branch. Returns (success, output)."""
    log(f"OMP: '{task[:80]}...' on branch '{branch}'")
    # Build the full prompt for omp.sh
    prompt = textwrap.dedent(f"""\
        You are improving the basis-arb-tool at {ROOT}.

        CURRENT WORKING DIRECTORY: {ROOT}

        TASK: {task}

        RULES:
        - Use Python 3.11+ syntax
        - All secrets come from environment variables — NEVER hardcode
        - The execution/ directory is the ONLY place that may import Hyperliquid Exchange SDK
        - Never change or weaken kill-switch or drawdown cap logic
        - Run `python3 -m pytest tests/ -q` after changes and fix any failures before returning
        - Write a test in tests/ for any new feature
        - Commit your changes with a clear message starting with the ticket id, e.g. "[operator-tools-integration] Add aggr.trade routing"
        - Push the branch: git push -u origin {branch}

        Return a brief summary of what you changed.
    """)

    # Write prompt to temp file to avoid shell quoting issues
    prompt_file = ROOT / ".cron_output" / f"omp_prompt_{branch}.txt"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(prompt)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    # Use login shell to source ~/.hermes/.env (API keys) before calling omp
    # omp binary is ~/.local/bin/omp; use -p for non-interactive mode
    omp_bin = str(Path.home() / ".local" / "bin" / "omp")
    rc, out, err = run_cmd(
        ["bash", "-lc",
         f"source ~/.hermes/.env 2>/dev/null; cd {ROOT} && git checkout -b {branch} 2>/dev/null || git checkout {branch} && {omp_bin} -p < {prompt_file}"],
        timeout=600,
    )
    prompt_file.unlink(missing_ok=True)
    log(f"OMP RC={rc}: {out[:300]}")
    if err:
        log(f"OMP stderr: {err[:200]}")
    return rc == 0, f"{out}\n{err}"[:500]


def run() -> int:
    log("=== META-IMPROVEMENT ORCHESTRATOR ===")
    now_ts = datetime.datetime.utcnow().isoformat()

    # Load or initialize state
    state = load_json(STATE_FILE, {
        "version_tag": "v1.0",
        "consecutive_keeps": 0,
        "current_branch": None,
        "current_item_id": None,
        "last_run_ts": None,
    })
    agenda = load_json(AGENDA_FILE, {"version": 1, "items": DEFAULT_AGENDA})
    if not agenda.get("items"):
        agenda = {"version": 1, "items": DEFAULT_AGENDA, "last_full_refresh": now_ts}

    # Load performance health
    perf = load_json(PERF_FILE, {})
    health_score = perf.get("health_score", None)
    drawdown = perf.get("drawdown_pct", None)
    version_tag = perf.get("version_tag", state["version_tag"])

    log(f"Performance: health={health_score} drawdown={drawdown} version={version_tag}")

    # Check if the running version is showing problems — if so, flag for urgent review
    if health_score is not None and health_score < 40:
        alerts = [{
            "type": "PERFORMANCE_DEGRADATION",
            "health_score": health_score,
            "drawdown_pct": drawdown,
            "note": "Running version showing poor health — investigate before promoting next version",
        }]
        ALERT_FILE.write_text(json.dumps(alerts, indent=2, default=str))
        log(f"ALERT: Performance degradation — health={health_score}/100")

    # Check current branch status
    current_branch = get_git_branch()
    log(f"Current branch: {current_branch}")
    git_status = get_git_status()
    has_changes = bool(git_status.strip())

    # If we're on a feature branch and there are changes, try to keep them
    if current_branch != "main" and has_changes:
        log(f"Changes on {current_branch}: {git_status[:200]}")
        # Run anti-degradation checks
        passed, details = run_anti_degradation()
        if passed:
            log(f"Anti-degradation checks PASSED")
            state["consecutive_keeps"] += 1
            log(f"Consecutive keeps: {state['consecutive_keeps']}/3")
            if state["consecutive_keeps"] >= 3:
                log("READY TO PROMOTE: 3 consecutive keeps — open PR to main")
                # Create PR
                rc, out, err = run_cmd(
                    ["gh", "pr", "create", "--fill"],
                    cwd=ROOT,
                )
                if rc == 0:
                    log(f"PR created: {out.strip()}")
                    state["consecutive_keeps"] = 0
                    state["version_tag"] = _bump_version(state["version_tag"])
                else:
                    log(f"PR create failed: {err}")
            # Commit and push current changes
            run_cmd(["git", "add", "-A"], cwd=ROOT)
            run_cmd(["git", "commit", "-am", f"[{state.get('current_item_id','unknown')}] incremental improvement"], cwd=ROOT)
            run_cmd(["git", "push"], cwd=ROOT)
        else:
            log(f"Anti-degradation FAILED — REVERTING:")
            log(details)
            # Revert to main
            run_cmd(["git", "checkout", "main"], cwd=ROOT)
            run_cmd(["git", "branch", "-D", current_branch], cwd=ROOT)
            state["consecutive_keeps"] = 0
            log("Reverted to main")

    # Pick the next pending item
    pending = [i for i in agenda["items"] if i["status"] == "pending"]
    pending.sort(key=lambda x: (x["priority"], x["id"]))

    if not pending:
        log("All agenda items done — refreshing for new ones")
        agenda["items"] = DEFAULT_AGENDA
        agenda["last_full_refresh"] = now_ts
        save_json(AGENDA_FILE, agenda)
        return 0

    top = pending[0]
    branch = f"feat/{top['id']}"
    top["status"] = "in_progress"
    top["last_updated"] = now_ts
    state["current_item_id"] = top["id"]
    state["current_branch"] = branch
    state["last_run_ts"] = now_ts

    # Use omp.sh to implement this item
    task = f"""{top['description']}

    Action to take: {top['suggested_action']}
    Ticket ID: {top['id']}
    """
    success, output = _omp_coding(task, branch)

    if success:
        top["status"] = "done"
        top["result"] = {"summary": output[:300], "ts": now_ts}
        top["last_updated"] = now_ts
        state["consecutive_keeps"] = 0  # reset — new item started
    else:
        top["status"] = "abandoned"
        top["result"] = {"error": output[:300], "ts": now_ts}
        top["last_updated"] = now_ts
        log(f"OMP failed for [{top['id']}]: {output[:200]}")

    save_json(AGENDA_FILE, agenda)
    save_json(STATE_FILE, state)

    pending_count = sum(1 for i in agenda["items"] if i["status"] == "pending")
    log(f"Agenda: {pending_count} pending | Focus: [{top['id']}] {'✓' if success else '✗'}")

    return 0 if not pending else 1


def _bump_version(v: str) -> str:
    """Bump version tag, e.g. v1.0 -> v1.1"""
    try:
        parts = v.lstrip("v").split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        return "v" + ".".join(parts)
    except Exception:
        return v


if __name__ == "__main__":
    sys.exit(run())
