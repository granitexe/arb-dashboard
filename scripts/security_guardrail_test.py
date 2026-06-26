#!/usr/bin/env python3
"""Security: guardrail test — dry-run the signal pipeline to catch runtime errors.

Runs the full basis_arb pipeline in dry-run mode (no execution, no keys required)
to catch import errors, config issues, API failures, and runtime exceptions
before they matter. Designed to run as a cron job.
Exit codes: 0 = OK, 1 = guardrail alert, 2 = error
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
from pathlib import Path

ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "guardrail_alerts.json"
LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "guardrail_test.log"
ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def run() -> int:
    log("Starting guardrail test")
    repo_root = Path(__file__).parent.parent
    script = repo_root / "scripts" / "run_signal_pipeline.sh"

    env = os.environ.copy()
    # Use a test output path so we don't overwrite real signals.json
    env["OUTPUT_PATH"] = str(repo_root / ".cron_output" / "guardrail_signals.json")
    # Use anonymous Loris (no key) — if this fails, we still get pipeline init errors
    env.pop("LORIS_API_KEY", None)
    env["EXECUTION_FEE_BPS"] = "8"

    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(repo_root),
        env=env,
    )

    alerts = []
    stdout = result.stdout
    stderr = result.stderr

    # Categorize failures
    if result.returncode != 0:
        # Runtime errors (pipeline ran but crashed)
        alerts.append({
            "type": "PIPELINE_ERROR",
            "returncode": result.returncode,
            "stderr": stderr[-2000:],
            "stdout_tail": stdout[-1000:],
        })
        log(f"ALERT: Pipeline failed with exit code {result.returncode}")
        for line in stderr.split("\n")[-10:]:
            if line.strip():
                log(f"  stderr: {line}")
    else:
        # Pipeline succeeded — check for warnings in output
        warning_lines = [l for l in stderr.split("\n") + stdout.split("\n")
                        if "WARNING" in l or "ERROR" in l or "Exception" in l]
        if warning_lines:
            alerts.append({
                "type": "PIPELINE_WARNINGS",
                "warnings": warning_lines[-10:],
            })
            for w in warning_lines[-5:]:
                log(f"WARN: {w}")

    # Check Python import errors separately
    try:
        import_check = subprocess.run(
            [sys.executable, "-c",
             "import basis_arb; from basis_arb.config import BasisArbConfig; "
             "from basis_arb.pipeline import run_pipeline; print('imports OK')"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(repo_root),
        )
        if import_check.returncode != 0:
            alerts.append({
                "type": "IMPORT_ERROR",
                "stderr": import_check.stderr[-1000:],
            })
            log(f"ALERT: Import check failed: {import_check.stderr[-200:]}")
    except Exception as e:
        alerts.append({"type": "IMPORT_CHECK_EXCEPTION", "detail": str(e)})

    ALERT_FILE.write_text(json.dumps(alerts, indent=2, default=str))

    if alerts:
        log(f"ALERT: {len(alerts)} guardrail alert(s)")
        return 1
    else:
        log("OK: Guardrail test passed — pipeline runs cleanly")
        return 0


if __name__ == "__main__":
    sys.exit(run())
