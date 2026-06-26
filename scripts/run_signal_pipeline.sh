#!/usr/bin/env bash
# run_signal_pipeline.sh
# Runs the basis_arb signal pipeline and writes results to signals.json.
# Designed to run as a cron job (no user interaction).
#
# Usage:
#   bash scripts/run_signal_pipeline.sh
#
# Environment:
#   LORIS_API_KEY   — optional; unset means anonymous (rate-limited) mode
#   EXECUTION_FEE_BPS — override the default execution_fee_bps_roundtrip (8)
#                     e.g. EXECUTION_FEE_BPS=15 for tread.fi estimates
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="$REPO_DIR/.cron_output"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Find the venv Python so cron jobs work regardless of environment
if [ -f "$REPO_DIR/.venv/bin/python3" ]; then
    PYTHON="$REPO_DIR/.venv/bin/python3"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
else
    PYTHON="/usr/bin/python3"
fi

mkdir -p "$OUTPUT_DIR"

# Determine execution fee bps
FEE_BPS="${EXECUTION_FEE_BPS:-8}"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting signal pipeline (fee floor: ${FEE_BPS} bps)"

cd "$REPO_DIR"

# Run the pipeline with JSON output and custom fee floor
# --output-json goes to OUTPUT_DIR/signals.json
# We pass execution_fee via a temp config override using Python
$PYTHON - <<'PYEOF'
import os, sys, json, importlib, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from basis_arb.config import BasisArbConfig
from basis_arb.pipeline import run_pipeline
from basis_arb.report import build_json_report, write_json_report

fee_bps = float(os.environ.get("EXECUTION_FEE_BPS", "8"))
output_path = os.environ.get("OUTPUT_PATH", "signals.json")

cfg = BasisArbConfig(
    execution_fee_bps_roundtrip=fee_bps,
    output_json_path=output_path,
)
LORIS_KEY = os.environ.get("LORIS_API_KEY") or None

print(f"[{datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}] Configured: fee={fee_bps}bps, output={output_path}")

try:
    report = run_pipeline(cfg, LORIS_KEY, progress=lambda m: print(f"  {m}", flush=True))
    write_json_report(report, output_path)
    print(f"[{datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}] Pipeline complete: {len(report.signals)} coins, {sum(1 for s in report.signals if s.status=='OK')} tradable")
    # Write a compact status line for the cron caller
    with open(".cron_output/last_status.txt", "w") as f:
        f.write(f"OK|{len(report.signals)}|{sum(1 for s in report.signals if s.status=='OK')}|{report.generated_at.isoformat()}\n")
except Exception as e:
    import traceback
    traceback.print_exc()
    with open(".cron_output/last_status.txt", "w") as f:
        f.write(f"ERROR|0|0|\n")
    with open(".cron_output/last_error.txt", "w") as f:
        f.write(str(e) + "\n")
    sys.exit(1)
PYEOF

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Done. Output: $REPO_DIR/signals.json"
