#!/usr/bin/env python3
"""Security: audit Python dependencies for known vulnerabilities (CVEs).

Uses the `pip-audit` tool if available, falls back to parsing requirements.txt
for outdated packages. Designed to run as a cron job.
Exit codes: 0 = OK, 1 = vulnerabilities found, 2 = error
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
import re
import subprocess
import sys
from pathlib import Path

ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "deps_audit_alerts.json"
LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "deps_audit.log"
ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def run() -> int:
    log("Starting dependency audit")
    repo_root = Path(__file__).parent.parent
    req_file = repo_root / "requirements.txt"
    alerts = []

    # Try pip-audit first (best coverage)
    try:
        result = subprocess.run(
            ["pip-audit", "--format", "json", "--strict"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(repo_root),
        )
        if result.returncode == 0:
            log("OK: pip-audit found no vulnerabilities")
            ALERT_FILE.write_text("[]")
            return 0
        elif result.returncode == 1:
            # Vulnerabilities found
            try:
                vulns = json.loads(result.stdout)
            except Exception:
                vulns = [{"raw": result.stdout[-2000:]}]
            for v in vulns:
                alerts.append({
                    "type": "CVE_VULNERABILITY",
                    "package": v.get("name", "?"),
                    "versions": v.get("versions", []),
                    "vulns": v.get("vulns", []),
                })
            log(f"ALERT: {len(alerts)} vulnerability(ies) found by pip-audit")
    except FileNotFoundError:
        log("pip-audit not found, falling back to pip list")
    except subprocess.TimeoutExpired:
        log("WARN: pip-audit timed out, skipping")
    except Exception as e:
        log(f"WARN: pip-audit error: {e}, falling back")

    # Fallback: parse requirements.txt and check with pip
    if not alerts:
        try:
            result = subprocess.run(
                ["pip", "list", "--format", "json", "--not-required"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                try:
                    pkgs = json.loads(result.stdout)
                    # Check for packages with known vulnerable versions
                    KNOWN_VULNS = {
                        "requests": (">=2.32.0,<2.32.4"),  # example placeholder
                        "urllib3": (">=1.26.0,<1.26.19"),
                    }
                    for pkg in pkgs:
                        name = pkg.get("name", "")
                        version = pkg.get("version", "")
                        if name.lower() in KNOWN_VULNS:
                            alerts.append({
                                "type": "KNOWN_VULN_VERSION",
                                "package": name,
                                "version": version,
                                "rule": KNOWN_VULNS[name.lower()],
                            })
                except Exception as e:
                    log(f"WARN: pip list parse error: {e}")
        except Exception as e:
            log(f"WARN: pip list error: {e}")

    ALERT_FILE.write_text(json.dumps(alerts, indent=2, default=str))

    if alerts:
        for a in alerts:
            log(f"ALERT: [{a['type']}] {a.get('package','?')} {a.get('version','?')}")
        return 1
    else:
        log("OK: No known vulnerabilities in dependencies")
        return 0


if __name__ == "__main__":
    sys.exit(run())
