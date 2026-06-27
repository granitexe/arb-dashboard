#!/usr/bin/env bash
# scripts/run_risk_optimizer.sh
# Wrapper for risk_optimizer.py — runs the Kelly criterion portfolio optimizer.
#
# Usage:
#   bash scripts/run_risk_optimizer.sh
#   bash scripts/run_risk_optimizer.sh --bankroll 10000 --kelly 0.5
#
# Environment variables:
#   RISK_CONFIG   — path to JSON config file (overrides defaults in RiskConfig)
#
# Output:
#   .cron_output/portfolio_recommendation.json
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ROOT"

# ── venv activation ───────────────────────────────────────────────────────────
if [ -z "${VENV_ACTIVATED:-}" ] && [ -f "$ROOT/.venv/bin/python3" ]; then
    export VENV_ACTIVATED=1
    exec "$ROOT/.venv/bin/python3" "$ROOT/basis_arb/risk_optimizer.py" "$@"
fi

# ── Python path ───────────────────────────────────────────────────────────────
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$ROOT"

# ── Config file via env var ───────────────────────────────────────────────────
CONFIG_ARG=()
if [ -n "${RISK_CONFIG:-}" ]; then
    CONFIG_ARG=(--config "$RISK_CONFIG")
fi

python3 "$ROOT/basis_arb/risk_optimizer.py" "${CONFIG_ARG[@]}" "$@"
EXIT_CODE=$?

# ── Log outcome ───────────────────────────────────────────────────────────────
if [ $EXIT_CODE -eq 0 ]; then
    echo "[run_risk_optimizer] completed OK — recommendation written to .cron_output/portfolio_recommendation.json"
elif [ $EXIT_CODE -eq 1 ]; then
    echo "[run_risk_optimizer] KILL SWITCH triggered — review portfolio_recommendation.json"
else
    echo "[run_risk_optimizer] unexpected exit code $EXIT_CODE"
fi

exit $EXIT_CODE