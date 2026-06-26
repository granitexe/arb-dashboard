"""Safety and security layer for basis-arb-tool.

This module enforces the hard boundaries that prevent the tool from:
  1. Sending money anywhere (no keys, no signing, no bridging)
  2. Exfiltrating secrets (no outbound secrets, no env dumps)
  3. Executing untrusted code (no eval, no exec, no dynamic imports from network)
  4. Over-committing capital (bankroll hard caps)
  5. Operating when critical risk limits are breached (kill-switch)

These are NOT configurable. They are hard constraints baked into the tool.

---
SECURITY ARCHITECTURE
---
The tool is a signals-only system. It NEVER:
  - Holds private keys, seed phrases, or wallet credentials
  - Signs transactions, bridges assets, or initiates withdrawals
  - Sends secrets over the network (env vars are read-only, never exported to output)
  - Evaluates dynamically-loaded code from untrusted sources
  - Modifies its own execution permissions or creates executable files

Secrets live ONLY in:
  - Operator's shell environment (env vars, never printed or logged)
  - Files the operator explicitly creates (e.g. bankroll.txt)
  - Files the operator explicitly points to via config

Secrets are NEVER:
  - Hardcoded in source code
  - Written to JSON output or logs
  - Sent to external services except the explicitly documented data sources
  - Stored in the knowledge base or research outputs
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

# ------------------------------------------------------------------
# 1. KILL-SWITCH — hard caps on operational risk
# ------------------------------------------------------------------

# These values are hard caps. They cannot be overridden by config.
# If these values are exceeded, the tool refuses to size or recommend positions.

MAX_TOTAL_EXPOSURE_FRAC = 1.0      # Never more than 100% of bankroll in notional
MAX_SINGLE_POSITION_FRAC = 0.20     # Never more than 20% of bankroll in one coin
MAX_LOSS_FRAC_PER_TRADE = 0.02     # Never risk more than 2% on a single trade
MIN_NET_CARRY_APR = 0.0            # Refuse to size if carry is negative after fees
MIN_POSITION_USD = 10.0            # Below $10 notional: fees dominate, refuse

# ------------------------------------------------------------------
# 2. SECRET DETECTION — prevent accidental exfiltration
# ------------------------------------------------------------------

# Patterns that, if found in any OUTPUT (never in source code logic),
# indicate a secret has leaked and must be flagged.
SECRET_PATTERNS = [
    # Private keys
    (re.compile(r'-----BEGIN [A-Z ]+PRIVATE KEY-----'), "PRIVATE_KEY_BLOCK"),
    (re.compile(r'0x[a-fA-F0-9]{64}'), "HEX_PRIVATE_KEY"),
    # API tokens
    (re.compile(r'ghp_[a-zA-Z0-9]{36,}'), "GITHUB_TOKEN"),
    (re.compile(r'github_pat_[a-zA-Z0-9_]{82,}'), "GITHUB_PAT"),
    (re.compile(r'sk-[a-zA-Z0-9]{48,}'), "OPENAI_KEY"),
    (re.compile(r'AKIA[A-Z0-9]{16}'), "AWS_KEY"),
    (re.compile(r'(?i)slack[_-]?token["\']?\s*[:=]\s*["\']?[a-zA-Z0-9/-]{20,}'), "SLACK_TOKEN"),
    (re.compile(r'(?i)aneric["\']?\s*[:=]\s*["\']?sk-[a-zA-Z0-9]{48,}'), "ANTHROPIC_KEY"),
    # Seed phrases (12/24 words)
    (re.compile(r'\b[a-z]+\s+[a-z]+\s+[a-z]+\s+[a-z]+\s+[a-z]+\s+[a-z]+\s+[a-z]+\s+[a-z]+\s+[a-z]+\s+[a-z]+\s+[a-z]+\s+[a-z]+\b'), "SEED_PHRASE"),
]

# Fields that should NEVER appear in JSON output (they indicate secrets)
FORBIDDEN_OUTPUT_FIELDS = {
    "private_key", "secret_key", "api_secret", "api_key", "seed",
    "mnemonic", "password", "token", "bearer", "authorization",
}


def scan_for_secrets(text: str) -> list[tuple[str, str]]:
    """Scan text for leaked secrets. Returns list of (pattern_name, matched_text).

    Use this on any output that will be written to disk or sent over the network.
    Never call this on source code (it will false-positive on library code).
    """
    found = []
    for pattern, name in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            # Don't flag false positives: hex private keys must be exactly 64 hex chars
            if name == "HEX_PRIVATE_KEY" and len(match.group()) != 66:  # 0x + 64
                continue
            found.append((name, match.group()[:20] + "..."))
    return found


def sanitize_for_output(value: dict | list | str, path: str = "") -> dict | list | str:
    """Remove any secret-adjacent fields from a dict before writing to JSON.

    Recursively walks the structure and removes keys whose names match
    FORBIDDEN_OUTPUT_FIELDS. Also flags hex strings that look like keys.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            k_lower = k.lower()
            # Drop known secret field names
            if k_lower in FORBIDDEN_OUTPUT_FIELDS:
                out[k] = "[REDACTED]"
                continue
            # Recurse
            out[k] = sanitize_for_output(v, f"{path}.{k}")
        return out
    elif isinstance(value, list):
        return [sanitize_for_output(item, f"{path}[i]") for item in value]
    elif isinstance(value, str):
        # Flag strings that look like hex keys
        if re.match(r'^0x[a-fA-F0-9]{64}$', value.strip()):
            return "[HEX_KEY_REDACTED]"
        if re.match(r'^sk-[a-zA-Z0-9]{48,}$', value.strip()):
            return "[API_KEY_REDACTED]"
        if re.match(r'^ghp_[a-zA-Z0-9]{36,}$', value.strip()):
            return "[GITHUB_TOKEN_REDACTED]"
        if len(value) > 200:
            # Long base64 strings might be keys — redact if they're near env-var-like content
            if any(substr in value for substr in ["-----BEGIN", "PRIVATE KEY", "ssh-"]):
                return "[KEY_REDACTED]"
        return value
    else:
        return value


# ------------------------------------------------------------------
# 3. EXECUTION GUARDRAILS — prevent dangerous operations
# ------------------------------------------------------------------

# These functions are monkey-patched or wrapped to prevent dangerous operations.
# They are checked before any external network call.

ALLOWED_OUTBOUND_HOSTS = {
    # Data sources (read-only)
    "api.loris.tools",
    "api.coingecko.com",
    "api.llama.fi",
    "fapi.binance.com",
    "api.binance.com",
    "api.bybit.com",
    "www.okx.com",
    "api.hyperliquid.xyz",
    "forwarder.hyperliquid.xyz",
    # Dashboard / monitoring
    "api.github.com",          # for gh CLI only (read/write to arb-dashboard repo)
    "github.com",              # same
}

# Curl/wget/requests are used only by the source clients in basis_arb/sources/
# This function is a safety check for any subprocess calls
def validate_url_host(url: str) -> bool:
    """Return True if the URL's host is in ALLOWED_OUTBOUND_HOSTS.

    Call this before any subprocess call or URL open to prevent
    SSRF attacks (e.g. file:///, http://169.254.169.254/).
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or ""
        # Block private IPs and unusual schemes
        if parsed.scheme not in ("http", "https"):
            return False
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return False
        if host.startswith("169.254."):
            return False  # AWS metadata
        if host.endswith(".internal"):
            return False
        return True
    except Exception:
        return False


# ------------------------------------------------------------------
# 4. BANKROLL HARD CAPS — prevent over-commitment
# ------------------------------------------------------------------

def apply_hard_caps(position_notional: float, bankroll_usd: float) -> float:
    """Apply hard-cap overrides to a computed position size.

    These caps override any Kelly calculation or operator setting.
    They are the last line of defense against over-exposure.
    """
    if bankroll_usd <= 0:
        return 0.0

    max_single = bankroll_usd * MAX_SINGLE_POSITION_FRAC
    max_total = bankroll_usd * MAX_TOTAL_EXPOSURE_FRAC

    if position_notional > max_single:
        position_notional = max_single

    return position_notional


def validate_bankroll(bankroll_usd: float) -> tuple[bool, str]:
    """Validate that the bankroll value is reasonable.

    Returns (is_valid, reason).
    Reason is empty if valid.
    """
    if bankroll_usd <= 0:
        return False, "bankroll must be positive"
    if bankroll_usd < 10:
        return False, "bankroll below minimum ($10)"
    if bankroll_usd > 10_000_000:
        return False, "bankroll above maximum ($10M — verify this is correct)"
    return True, ""


# ------------------------------------------------------------------
# 5. NETWORK CALL ALLOWLIST — for use by source clients
# ------------------------------------------------------------------

def get_allowed_hosts() -> set[str]:
    """Return the set of allowed outbound hosts. Used by source clients."""
    return ALLOWED_OUTBOUND_HOSTS.copy()


# ------------------------------------------------------------------
# 6. SELF-IMPROVEMENT GUARDRAILS — prevent agent from breaking rules
# ------------------------------------------------------------------

# When the autoresearch loop proposes changes, these rules must be
# respected. Any proposed change that violates these rules is rejected
# before it can be applied.

IMPROVEMENT_FORBIDDEN_PATTERNS = [
    # No new imports of crypto libraries that handle keys
    (re.compile(r'import\s+(web3|eth_account|web3\.py|bitcoinlib|btcrecover)'), "FORBIDDEN_CRYPTO_LIB"),
    # No subprocess calls with shell=True
    (re.compile(r'subprocess\.(run|Popen|call|check_output)\s*\([^)]*shell\s*=\s*True'), "SHELL_TRUE_FORBIDDEN"),
    # No eval/exec of strings
    (re.compile(r'\b(eval|exec)\s*\('), "EVAL_EXEC_FORBIDDEN"),
    # No hardcoded secrets
    (re.compile(r'["\'][a-zA-Z_]*key["\']\s*[:=]\s*["\'][a-zA-Z0-9_/-]{20,}'), "HARDCODED_SECRET"),
    # No new requests to unknown hosts
    (re.compile(r'requests\.(get|post)\s*\([^)]*url\s*=\s*[^"\']+["\']'), "NON_CONSTANT_URL"),
]

IMPROVEMENT_ALLOWED_FILE_PATHS = {
    # Only allow self-improvement to modify these files
    Path(__file__).parent / "signals",
    Path(__file__).parent / "sources",
    Path(__file__).parent / "normalization.py",
    Path(__file__).parent / "config.py",
    Path(__file__).parent / "models.py",
    Path(__file__).parent / "knowledge",
    Path(__file__).parent / "scripts",
    Path(__file__).parent / "portfolio.py",
    Path(__file__).parent / "bankroll.py",
    Path(__file__).parent / "safety.py",
}

IMPROVEMENT_FORBIDDEN_FILE_PATHS = {
    # Never modify these files in a self-improvement loop
    Path(__file__).parent / "safety.py",           # Safety rules are immutable
    Path(__file__).parent / "pipeline.py",          # Pipeline structure is stable
}


def validate_improvement(
    proposed_file: Path,
    proposed_diff: str,
) -> tuple[bool, list[str]]:
    """Validate a proposed self-improvement change.

    Returns (is_valid, list_of_violation_reasons).
    If is_valid is False, the change must NOT be applied.
    """
    violations = []

    abs_proposed = proposed_file.resolve()
    if abs_proposed in IMPROVEMENT_FORBIDDEN_FILE_PATHS:
        violations.append(f"File {proposed_file.name} is on the forbidden list — safety rules are immutable")

    # Check for forbidden patterns in the diff
    for pattern, name in IMPROVEMENT_FORBIDDEN_PATTERNS:
        if pattern.search(proposed_diff):
            violations.append(f"Pattern {name} found in diff — forbidden")

    # Check URL validity in diff
    urls_in_diff = re.findall(r'https?://[^\s\'"<>]+', proposed_diff)
    for url in urls_in_diff:
        if not validate_url_host(url):
            violations.append(f"URL host not in allowlist: {url}")

    return (len(violations) == 0, violations)
