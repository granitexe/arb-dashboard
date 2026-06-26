#!/usr/bin/env bash
# run_autonomous_trading.sh — autonomous basis arbitrage trading loop
#
# This script runs ONE cycle of the autonomous trading executor.
# It reads signals.json, applies risk controls, and executes via Hyperliquid.
#
# ENVIRONMENT VARIABLES (set in ~/.basis_arb/.env):
#   BANKROLL_USD              — your total bankroll in USD (overrides bankroll.txt)
#   BANKROLL_PATH             — path to bankroll file (default ~/.basis_arb/bankroll.txt)
#   HYPERLIQUID_ENABLED       — "true" to enable live trading (default "false")
#   HYPERLIQUID_SECRET_KEY    — your Hyperliquid private key (0x... hex)
#   HYPERLIQUID_ACCOUNT_ADDRESS — optional sub-account
#   HYPERLIQUID_SLIPPAGE_BPS  — slippage tolerance (default 5.0)
#   KELLY_FRACTION            — fractional Kelly (default 0.25)
#   MAX_POSITIONS             — max simultaneous positions (default 5)
#   MAX_DRAWDOWN_FRAC         — kill-switch drawdown (default 0.10)
#   MAX_DAILY_LOSS_FRAC       — daily loss kill-switch (default 0.025)
#   DRY_RUN                   — "false" to execute real trades (default "true")
#   TRAP_SCORE_THRESHOLD      — TGE trap exclusion threshold (default 0.75)
#   LORIS_API_KEY             — Loris.tools API key (recommended for multi-venue)
#   LOG_PATH                  — override log output path
#
# SAFETY DEFAULTS:
#   DRY_RUN=true             — no real trades unless explicitly disabled
#   HYPERLIQUID_ENABLED=false — must be explicitly enabled
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_PATH="${LOG_PATH:-$PROJECT_DIR/.cron_output/trading.log}"
SIGNALS_PATH="${SIGNALS_PATH:-$PROJECT_DIR/signals.json}"
STATE_DIR="$PROJECT_DIR/.cron_output"

mkdir -p "$STATE_DIR"

# Load .env if it exists
if [[ -f "$HOME/.basis_arb/.env" ]]; then
    set -a
    source "$HOME/.basis_arb/.env"
    set +a
fi

log() {
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo "[$ts] $*"
}

# --- Safety check: confirm this is intentional ---
if [[ "${HYPERLIQUID_ENABLED:-false}" == "true" && "${DRY_RUN:-true}" == "true" ]]; then
    log "CONFLICT: HYPERLIQUID_ENABLED=true but DRY_RUN=true — will run in dry-run mode"
fi

if [[ "${HYPERLIQUID_ENABLED:-false}" == "true" && "${DRY_RUN:-true}" == "false" ]]; then
    log "!! LIVE TRADING ENABLED — real funds at risk !!"
    log "!! Bankroll: ${BANKROLL_USD:-$(cat "$HOME/.basis_arb/bankroll.txt" 2>/dev/null || echo 'NOT SET')} USD !!"
fi

# --- Run the signal pipeline first (refresh signals) ---
log "Refreshing signal pipeline..."
if [[ -f "$SCRIPT_DIR/run_signal_pipeline.sh" ]]; then
    bash "$SCRIPT_DIR/run_signal_pipeline.sh" >> "$LOG_PATH" 2>&1 || {
        log "WARNING: signal pipeline failed — using stale signals.json"
    }
else
    log "run_signal_pipeline.sh not found — using existing signals.json"
fi

# --- Run the executor cycle ---
log "Running autonomous trading cycle..."
cd "$PROJECT_DIR"

source .venv/bin/activate

python3 -c "
import sys
sys.path.insert(0, '$PROJECT_DIR')

from basis_arb.executor import run_cycle, ExecutorConfig
from pathlib import Path

cfg = ExecutorConfig.from_env()
print(f'Config: dry_run={cfg.dry_run}, hyperliquid_enabled={cfg.hyperliquid_enabled}')
print(f'  kelly_fraction={cfg.kelly_fraction}, max_positions={cfg.max_positions}')
print(f'  max_drawdown_frac={cfg.max_drawdown_frac}, max_daily_loss_frac={cfg.max_daily_loss_frac}')
print(f'  bankroll_usd={cfg.bankroll_usd}')

result = run_cycle(
    signals_path=Path('$SIGNALS_PATH'),
    cfg=cfg,
)

print(f'Cycle result:')
print(f'  bankroll_usd: {result.get(\"bankroll_usd\")}')
print(f'  kill_switch_triggered: {result.get(\"kill_switch_triggered\")}')
print(f'  kill_switch_reason: {result.get(\"kill_switch_reason\", \"\")}')
print(f'  positions_opened: {len(result.get(\"positions_opened\", []))}')
print(f'  positions_closed: {len(result.get(\"positions_closed\", []))}')
print(f'  errors: {result.get(\"errors\", [])}')
print(f'  dry_run: {result.get(\"dry_run\")}')

for pos in result.get('positions_opened', []):
    print(f'  OPENED: {pos.get(\"coin\")} {pos.get(\"side\")} {pos.get(\"size_requested\", 0):.2f} USD @ {pos.get(\"slippage_bps\", \"?\")} bps [{pos.get(\"status\", \"?\")}]')

for err in result.get('errors', []):
    print(f'  ERROR: {err}')

sys.exit(0)
" >> "$LOG_PATH" 2>&1

log "Trading cycle complete"
