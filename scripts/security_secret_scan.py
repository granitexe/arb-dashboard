#!/usr/bin/env python3
"""Security: scan the codebase for accidentally committed secrets / credentials.

Checks for private key patterns, API key patterns, AWS/cloud credentials,
and high-entropy strings that may be secrets. Designed to run as a cron job.
Exit codes: 0 = OK (no secrets found), 1 = secrets found, 2 = error
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Optional

ALERT_FILE = Path(__file__).parent.parent / ".cron_output" / "secret_alerts.json"
LOG_FILE = Path(__file__).parent.parent / ".cron_output" / "secret_scan.log"
ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)

SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"), "PRIVATE_KEY_BLOCK"),
    (re.compile(r"api[_-]?key[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_\-]{20,}[\"']", re.I), "API_KEY_ASSIGNMENT"),
    (re.compile(r"secret[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']", re.I), "SECRET_ASSIGNMENT"),
    (re.compile(r"ghp_[A-Za-z0-9]{36,}"), "GITHUB_TOKEN"),
    (re.compile(r"github_pat_[A-Za-z0-9_\-]{83,}"), "GITHUB_PAT"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9]{10,}"), "SLACK_TOKEN"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "AWS_ACCESS_KEY"),
    (re.compile(r"bearer\s+[A-Za-z0-9_\-\.]{20,}", re.I), "BEARER_TOKEN"),
    (re.compile(r"password[\"']?\s*[:=]\s*[\"'][^\"']{8,}[\"']", re.I), "PASSWORD_ASSIGNMENT"),
    (re.compile(r"sk-[A-Za-z0-9]{48,}"), "OPENAI_SK"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{80,}"), "ANTHROPIC_SK"),
]

SKIP_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".zip", ".tar", ".gz", ".pdf",
             ".db", ".sqlite", ".pyc", ".whl", ".pem", ".key", ".p12", ".p8", ".ttf", ".woff", ".woff2"}

SKIP_NAMES = {"basis_arb_signals.json", "config.env.example", ".env.example",
              ".env.template", "requirements.txt", "package-lock.json"}

MIN_SECRET_LEN = 24
SHANNON_MIN = 4.3
MAX_LINE_LEN = 5000


def shannon_entropy(s: str) -> float:
    if len(s) < 4:
        return 0.0
    freq = [0.0] * 256
    for c in s.encode():
        freq[c] += 1
    entropy = 0.0
    for f in freq:
        if f > 0:
            p = f / len(s)
            entropy -= p * math.log2(p)
    return entropy


def scan_file(path: Path) -> list[dict]:
    findings = []
    if path.suffix.lower() in SKIP_EXTS or path.name in SKIP_NAMES:
        return findings
    try:
        content = path.read_bytes()
    except (OSError, IOError, UnicodeDecodeError):
        return findings

    text = content.decode("utf-8", errors="ignore")
    lines = text.split("\n")

    for lineno, line in enumerate(lines, 1):
        if len(line) > MAX_LINE_LEN:
            continue
        for pat, label in SECRET_PATTERNS:
            if pat.search(line):
                snippet = line[:120].strip()
                findings.append({
                    "file": str(path),
                    "line": lineno,
                    "type": label,
                    "snippet": snippet,
                })

    for lineno, line in enumerate(lines, 1):
        for m in re.finditer(r'["\']([A-Za-z0-9_\-=]{' + str(MIN_SECRET_LEN) + r',})["\']', line):
            val = m.group(1)
            if val.lower() in {"true", "false", "null", "none", "undefined", "localhost"}:
                continue
            if shannon_entropy(val) >= SHANNON_MIN:
                findings.append({
                    "file": str(path),
                    "line": lineno,
                    "type": "HIGH_ENTROPY_STRING",
                    "snippet": line[:120].strip(),
                })
                break

    return findings


def scan_directory(root: Path) -> list[dict]:
    all_findings = []
    for item in root.rglob("*"):
        if not item.is_file():
            continue
        if item.name in SKIP_NAMES:
            continue
        skip = any(p in item.parts for p in {".git", "node_modules", "__pycache__",
                                               ".venv", "venv", "dist", "build",
                                               ".pytest_cache", ".mypy_cache", ".tox",
                                               ".eggs", "*.egg-info", ".cache",
                                               "site-packages", "node_modules"})
        if skip:
            continue
        findings = scan_file(item)
        all_findings.extend(findings)
    return all_findings


def log(msg: str) -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    log("Starting secret scan")
    repo_root = Path(__file__).parent.parent
    findings = scan_directory(repo_root)

    if findings:
        log(f"ALERT: {len(findings)} secret finding(s) detected")
        for f in findings[:20]:
            log(f"  [{f['type']}] {f['file']}:{f['line']} -> {f['snippet'][:80]}")
        ALERT_FILE.write_text(json.dumps(findings, indent=2, default=str))
        sys.exit(1)
    else:
        log("OK: No secrets detected in codebase")
        ALERT_FILE.write_text("[]")
        sys.exit(0)
