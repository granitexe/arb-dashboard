#!/usr/bin/env bash
# scripts/run_orchestrator.sh
# Single entry point that replaces all 10+ cron jobs.
# The orchestrator.py itself decides what's lazy + stateful.

set -euo pipefail

# ── paths ──────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CRON_OUT="$ROOT/.cron_output"

# ── venv activation ─────────────────────────────────────────────────────────

if [ -z "${VENV_ACTIVATED:-}" ] && [ -f "$ROOT/.venv/bin/python3" ]; then
    export VENV_ACTIVATED=1
    exec "$ROOT/.venv/bin/python3" "$ROOT/basis_arb/orchestrator.py" "$@"
fi

# ── output dirs ─────────────────────────────────────────────────────────────

mkdir -p "$CRON_OUT"

# ── log rotation (keep last 10 runs) ───────────────────────────────────────

if [ -f "$CRON_OUT/orchestrator.log" ]; then
    LINES=$(wc -l < "$CRON_OUT/orchestrator.log")
    if [ "$LINES" -gt 10000 ]; then
        tail -n 5000 "$CRON_OUT/orchestrator.log" > "$CRON_OUT/orchestrator.log.tmp"
        mv "$CRON_OUT/orchestrator.log.tmp" "$CRON_OUT/orchestrator.log"
    fi
fi

# ── run ─────────────────────────────────────────────────────────────────────

exec python3 "$ROOT/basis_arb/orchestrator.py" "$@"