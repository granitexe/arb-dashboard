#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_data_gatherer.sh
# Launch script for the continuous market data gatherer.
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Virtual environment path (adjust if you use a different venv name/location)
VENV_PATH="${VENV_PATH:-"$PROJECT_ROOT/.venv"}"
PYTHON="${PYTHON:-python3}"

# Database path (absolute or relative to project root)
DB_PATH="${DB_PATH:-"$PROJECT_ROOT/market_data.db"}"

# Fetch interval in seconds
INTERVAL="${INTERVAL:-15}"

# Log file
LOG_DIR="${LOG_DIR:-"$PROJECT_ROOT/logs"}"
LOG_FILE="${LOG_FILE:-"$LOG_DIR/data_gatherer.log"}"

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

# Create log directory
mkdir -p "$LOG_DIR"

# Activate virtual environment if it exists
if [[ -d "$VENV_PATH" ]]; then
    source "$VENV_PATH/bin/activate"
fi

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

exec "$PYTHON" -m basis_arb.data_gatherer \
    --db "$DB_PATH" \
    --interval "$INTERVAL" \
    2>&1 | tee -a "$LOG_FILE"