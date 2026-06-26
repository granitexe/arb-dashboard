#!/usr/bin/env bash
# refresh_dashboard.sh
# Reads signals.json, regenerates index.html, commits and pushes to arb-dashboard.
#
# Usage:
#   bash scripts/refresh_dashboard.sh
#
# Required env vars (ideally in ~/.hermes/.env or passed by cron):
#   GITHUB_TOKEN      — GitHub personal access token (needs repo scope)
#   DASHBOARD_REPO    — repo in "owner/name" format (default: granitexe/arb-dashboard)
#   DASHBOARD_BRANCH  — branch to push to (default: main)
#   DASHBOARD_TOKEN   — secret query-param token for operator dashboard access
#                       If set, dashboard requires ?key=DASHBOARD_TOKEN to view.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CRON_OUT="$REPO_DIR/.cron_output"
cd "$REPO_DIR"

GITHUB_TOKEN="${GITHUB_TOKEN:-}"
DASHBOARD_REPO="${DASHBOARD_REPO:-granitexe/arb-dashboard}"
DASHBOARD_BRANCH="${DASHBOARD_BRANCH:-main}"
DASHBOARD_TOKEN="${DASHBOARD_TOKEN:-}"   # optional secret for access control

SIGNALS_FILE="$REPO_DIR/signals.json"
LAST_STATUS="$CRON_OUT/last_status.txt"

# --- sanity checks ---
if [[ ! -f "$SIGNALS_FILE" ]]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: signals.json not found at $SIGNALS_FILE" >&2
    echo "Run run_signal_pipeline.sh first." >&2
    exit 1
fi

if [[ -z "$GITHUB_TOKEN" ]]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: GITHUB_TOKEN not set" >&2
    exit 1
fi

# Read last pipeline status
if [[ -f "$LAST_STATUS" ]]; then
    LAST_RUN=$(head -1 "$LAST_STATUS")
else
    LAST_RUN="unknown"
fi

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "[$TS] Refreshing dashboard from signals.json (repo: $DASHBOARD_REPO)"

# --- clone dashboard repo (shallow) ---
TMPDIR=$(mktemp -d)
git clone --depth=1 --branch "$DASHBOARD_BRANCH" \
    "https://x-access-token:${GITHUB_TOKEN}@github.com/${DASHBOARD_REPO}.git" \
    "$TMPDIR" 2>&1 | tail -3

if [[ ! -d "$TMPDIR" ]] || [[ ! -f "$TMPDIR/index.html" ]]; then
    echo "[$TS] ERROR: failed to clone $DASHBOARD_REPO" >&2
    rm -rf "$TMPDIR"
    exit 1
fi

# --- generate HTML from signals.json using Python ---
python3 - <<PYEOF
import json, sys, datetime, os, textwrap
from pathlib import Path

TS = os.environ.get("TS", "$(date -u +%Y-%m-%dT%H:%M:%SZ)")
LAST_RUN = os.environ.get("LAST_RUN", "$LAST_RUN")
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "$DASHBOARD_TOKEN")
SIGNALS_FILE = os.environ.get("SIGNALS_FILE", "$SIGNALS_FILE")
TMPDIR = os.environ.get("TMPDIR", "$TMPDIR")

try:
    with open(SIGNALS_FILE) as f:
        data = json.load(f)
except Exception as e:
    print(f"ERROR loading signals: {e}", file=sys.stderr)
    sys.exit(1)

signals = data.get("signals", [])
generated = data.get("generated_at", TS)
sources = data.get("sources", {})

ok_signals = [s for s in signals if s.get("status") == "OK"]
excl_signals = [s for s in signals if s.get("status") == "EXCLUDED"]

def pct(v):
    if v is None: return "—"
    return f"{v*100:.1f}%"

def score(v):
    if v is None: return "—"
    return f"{v:.2f}"

def ratiox(v):
    if v is None: return "—"
    return f"{v:.1f}x"

def status_class(s):
    if s.get("status") == "OK": return "status-ok"
    if s.get("status") == "EXCLUDED": return "status-trap"
    return "status-warn"

def trap_class(s):
    t = s.get("trap", {})
    c = t.get("composite_score", 0)
    if c < 0.33: return "trap-low"
    if c < 0.66: return "trap-med"
    return "trap-high"

def trap_label(s):
    t = s.get("trap", {})
    c = t.get("composite_score", 0)
    if c < 0.33: return "LOW"
    if c < 0.66: return "MED"
    return "HIGH"

# Build TGE excluded list
tge_excl = []
for s in excl_signals:
    trap = s.get("trap", {})
    tge_excl.append({
        "coin": s.get("coin", "?"),
        "reasons": trap.get("exclusion_reasons", ["unknown"]),
    })

# Build cleared list
cleared = []
for s in ok_signals[:10]:
    trap = s.get("trap", {})
    cleared.append({
        "coin": s.get("coin", "?"),
        "score": trap.get("composite_score", 0),
    })

# Build signal table rows
signal_rows = ""
for s in (ok_signals + excl_signals):
    c = s.get("carry", {})
    trap = s.get("trap", {})
    net = c.get("net_carry_apr")
    risk = s.get("risk_adjusted_apr")
    rows += f"""            <tr>
              <td><strong>{s.get('coin','?')}</strong></td>
              <td class="num">{pct(c.get('basis_pct'))}</td>
              <td class="num">{pct(c.get('funding_apr'))}</td>
              <td class="num">{pct(c.get('basis_apr'))}</td>
              <td class="num">{pct(net)}</td>
              <td class="num {status_class(s)}">{pct(risk)}</td>
              <td><span class="trap-score trap-{trap_class(s)}">{trap_label(s)}</span></td>
              <td class="{status_class(s)}">{s.get('status','?')}</td>
            </tr>
"""

# Sources
source_rows = ""
for name, m in sources.items():
    icon = "✓" if m.get("ok") else "✗"
    cls = "" if m.get("ok") else "off"
    source_rows += f'          <div class="source-item"><span class="source-name">{name}</span><span class="source-check {cls}">{icon}</span></div>\n'

# TGE excluded
tge_rows = ""
for t in tge_excl[:10]:
    tge_rows += f'        <div class="tge-item"><span class="tge-token">{t["coin"]}</span><span class="tge-reason">{"; ".join(t["reasons"][:2])}</span></div>\n'

# TGE cleared
cleared_rows = ""
for t in cleared[:10]:
    cleared_rows += f'          <div class="tge-cleared"><span class="token">{t["coin"]}</span><span class="check">✓</span> trap={t["score"]:.2f}</div>\n'

# Security gate JS
if DASHBOARD_TOKEN:
    security_js = textwrap.dedent(f"""
    <script>
      var VALID_KEY = "{DASHBOARD_TOKEN}";
      var params = new URLSearchParams(window.location.search);
      var provided = params.get("key");
      if (!provided || provided !== VALID_KEY) {{
        document.head.innerHTML = "<title>Access Restricted</title>";
        document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:monospace;color:#8b949e;background:#0d1117;">\
          <div style="text-align:center;padding:40px;border:1px solid #30363d;border-radius:8px;max-width:400px;">\
            <h2 style="color:#f85149;margin-bottom:16px;">🔒 Access Restricted</h2>\
            <p>This dashboard is operator-only.</p>\
            <p style="font-size:13px;margin-top:12px;color:#6e7681;">Add <code>?key=YOUR_TOKEN</code> to the URL.</p>\
          </div></div>';
      }}
    </script>
    """)
    security_note = '<span style="font-size:11px;color:var(--amber);">🔒 Token-protected — requires ?key=TOKEN</span>'
else:
    security_js = ""
    security_note = '<span style="font-size:11px;color:var(--amber);">⚠ No token set — dashboard is public. Set DASHBOARD_TOKEN to protect it.</span>'

html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BASIS ARB TOOL — Status Dashboard</title>{security_js}
<style>
  :root {{
    --bg: #0d1117; --panel: #161b22; --border: #30363d;
    --green: #3fb950; --amber: #d29922; --red: #f85149;
    --blue: #58a6ff; --text: #e6edf3; --muted: #8b949e;
    --mono: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; line-height: 1.5; font-size: 14px; }}
  a {{ color: var(--blue); text-decoration: none; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
  header {{ display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }}
  .logo {{ font-family: var(--mono); font-size: 22px; font-weight: 700; color: var(--green); }}
  .badge {{ display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; font-family: var(--mono); text-transform: uppercase; }}
  .badge-live {{ background: rgba(63,185,80,0.15); color: var(--green); border: 1px solid var(--green); }}
  .badge-live::before {{ content: ''; width: 8px; height: 8px; border-radius: 50%; background: currentColor; animation: pulse 2s infinite; }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.4}} }}
  .header-meta {{ display: flex; gap: 20px; font-size: 12px; color: var(--muted); font-family: var(--mono); flex-wrap: wrap; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  @media(max-width:900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .panel {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
  .panel-header {{ display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-bottom: 1px solid var(--border); }}
  .panel-title {{ font-family: var(--mono); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); }}
  .panel-body {{ padding: 16px; }}
  table {{ width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 13px; }}
  th {{ text-align: left; font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); padding: 6px 8px; border-bottom: 1px solid var(--border); }}
  td {{ padding: 8px 8px; border-bottom: 1px solid rgba(48,54,61,0.5); }}
  .num {{ text-align: right; }}
  .status-ok {{ color: var(--green); }} .status-warn {{ color: var(--amber); }} .status-trap {{ color: var(--red); }}
  .trap-score {{ display: inline-flex; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
  .trap-low {{ background: rgba(63,185,80,0.1); color: var(--green); }} .trap-med {{ background: rgba(210,153,34,0.1); color: var(--amber); }} .trap-high {{ background: rgba(248,81,73,0.1); color: var(--red); }}
  .risk-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .risk-item {{ background: rgba(63,185,80,0.08); border: 1px solid rgba(63,185,80,0.2); border-radius: 6px; padding: 10px 12px; }}
  .risk-item.warn {{ background: rgba(210,153,34,0.08); border-color: rgba(210,153,34,0.2); }}
  .risk-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); margin-bottom: 4px; }}
  .risk-value {{ font-family: var(--mono); font-size: 15px; font-weight: 600; color: var(--green); }}
  .risk-value.warn {{ color: var(--amber); }}
  .source-list {{ display: flex; flex-direction: column; gap: 6px; }}
  .source-item {{ display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; background: rgba(88,166,255,0.06); border-radius: 4px; font-family: var(--mono); font-size: 12px; }}
  .source-check {{ color: var(--green); font-size: 16px; }} .source-check.off {{ color: var(--red); }}
  .tge-item {{ display: flex; align-items: flex-start; gap: 10px; padding: 6px 0; border-bottom: 1px solid rgba(48,54,61,0.4); }}
  .tge-token {{ font-family: var(--mono); color: var(--red); font-weight: 600; min-width: 60px; }}
  .tge-reason {{ font-size: 12px; color: var(--muted); }}
  .tge-cleared {{ display: flex; align-items: center; gap: 6px; font-family: var(--mono); font-size: 12px; padding: 4px 0; }}
  .tge-cleared .token {{ color: var(--green); font-weight: 600; min-width: 60px; }}
  .tge-cleared .check {{ color: var(--green); }}
  .arch {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-family: var(--mono); font-size: 12px; }}
  .arch-node {{ background: rgba(88,166,255,0.1); border: 1px solid rgba(88,166,255,0.3); border-radius: 6px; padding: 8px 14px; color: var(--blue); text-align: center; }}
  .arch-node.highlight {{ border-color: var(--green); background: rgba(63,185,80,0.1); color: var(--green); }}
  .arch-arrow {{ color: var(--muted); font-size: 18px; }}
  .feature-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .feature {{ display: flex; align-items: flex-start; gap: 10px; padding: 10px; background: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid var(--border); }}
  .feature-icon {{ font-size: 18px; }}
  .feature-name {{ font-family: var(--mono); font-size: 12px; font-weight: 600; }}
  .feature-desc {{ font-size: 11px; color: var(--muted); margin-top: 2px; }}
  footer {{ margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border); display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; font-size: 12px; color: var(--muted); font-family: var(--mono); }}
  .full-width {{ grid-column: 1 / -1; }}
  .stat-bar {{ display: flex; gap: 16px; margin-bottom: 16px; }}
  .stat-chip {{ background: rgba(63,185,80,0.1); border: 1px solid rgba(63,185,80,0.3); border-radius: 6px; padding: 8px 16px; font-family: var(--mono); font-size: 13px; color: var(--green); }}
  .stat-chip.warn {{ background: rgba(210,153,34,0.1); border-color: rgba(210,153,34,0.3); color: var(--amber); }}
  .stat-chip.danger {{ background: rgba(248,81,73,0.1); border-color: rgba(248,81,73,0.3); color: var(--red); }}
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="logo">BASIS ARB <span style="color:var(--muted);font-weight:400;">TOOL</span></div>
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
      <span class="badge badge-live">LIVE</span>
      {security_note}
      <div class="header-meta">
        <span>Repo: <strong>{DASHBOARD_REPO}</strong></span>
        <span>Pipelines: <strong>{LAST_RUN}</strong></span>
        <span>Updated: <strong id="ts">{TS}</strong></span>
      </div>
    </div>
  </header>

  <!-- STAT SUMMARY -->
  <div class="stat-bar">
    <div class="stat-chip">{len(ok_signals)} tradable</div>
    <div class="stat-chip warn">{len(excl_signals)} excluded</div>
    <div class="stat-chip">Generated: {generated[:16]}Z</div>
  </div>

  <div class="grid">

    <!-- SIGNAL TABLE -->
    <div class="panel" style="grid-column:1/-1;">
      <div class="panel-header">
        <span class="panel-title">Signal Feed — Long Spot / Short Perp</span>
        <span style="font-size:11px;color:var(--muted);font-family:var(--mono);">Net carry after execution fees · Trap-adjusted</span>
      </div>
      <div class="panel-body" style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>Coin</th>
              <th class="num">Basis%</th>
              <th class="num">FundAPR</th>
              <th class="num">BasisAPR</th>
              <th class="num">NetCarry</th>
              <th class="num">RiskAdj</th>
              <th>Trap</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
{signal_rows or "            <tr><td colspan=\"8\" style=\"color:var(--muted);text-align:center;\">No signals — run signal pipeline first.</td></tr>"}
          </tbody>
        </table>
        <p style="margin-top:12px;font-size:11px;color:var(--muted);font-family:var(--mono);">
          NetCarry = carry APR minus execution fee floor ({data.get('config',{}).get('execution_fee_bps_roundtrip',8):.0f} bps round-trip). RiskAdj = NetCarry × (1 − TrapScore). Signals require operator review before execution.
        </p>
      </div>
    </div>

    <!-- TGE TRAP MONITOR -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">TGE Trap Monitor</span>
        <span style="font-size:11px;color:var(--red);font-family:var(--mono);">MANUFACTURED CARRY EXCLUDED</span>
      </div>
      <div class="panel-body">
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:var(--muted);margin-bottom:8px;">Flagged — Excluded from carry</div>
{tge_rows or "        <p style=\"color:var(--muted);font-size:12px;\">No exclusions.</p>\n"}
        <div style="margin-top:12px;font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:var(--muted);margin-bottom:8px;">Cleared — Organic carry</div>
{cleared_rows or "        <p style=\"color:var(--muted);font-size:12px;\">No cleared signals yet.</p>\n"}
      </div>
    </div>

    <!-- RISK CONTROLS -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Risk Controls</span>
      </div>
      <div class="panel-body">
        <div class="risk-grid">
          <div class="risk-item">
            <div class="risk-label">Max Notional</div>
            <div class="risk-value" style="color:var(--muted);">$50,000</div>
          </div>
          <div class="risk-item">
            <div class="risk-label">Leverage Cap</div>
            <div class="risk-value" style="color:var(--muted);">3×</div>
          </div>
          <div class="risk-item">
            <div class="risk-label">Per-Position Cap</div>
            <div class="risk-value" style="color:var(--muted);">$10,000</div>
          </div>
          <div class="risk-item warn">
            <div class="risk-label">Daily Drawdown Kill</div>
            <div class="risk-value warn">−2.5%</div>
          </div>
          <div class="risk-item warn">
            <div class="risk-label">Total Drawdown Kill</div>
            <div class="risk-value warn">−10%</div>
          </div>
          <div class="risk-item">
            <div class="risk-label">Address Allowlist</div>
            <div class="risk-value" style="font-size:12px;">Operator-Only</div>
          </div>
        </div>
        <p style="margin-top:12px;font-size:11px;color:var(--amber);font-family:var(--mono);">
          ⚠️ Delta-neutral is breakable: funding flip, basis blowout, and ADL can all force position closure.
        </p>
      </div>
    </div>

    <!-- DATA SOURCES -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Data Sources</span>
        <span style="font-size:11px;color:var(--muted);font-family:var(--mono);">Free APIs</span>
      </div>
      <div class="panel-body">
        <div class="source-list">
{source_rows or "          <p style=\"color:var(--muted);font-size:12px;\">No source data.</p>\n"}
        </div>
      </div>
    </div>

    <!-- ARCHITECTURE -->
    <div class="panel full-width">
      <div class="panel-header">
        <span class="panel-title">Architecture</span>
      </div>
      <div class="panel-body">
        <div class="arch">
          <div class="arch-node">📡 Data Sources<br><small style="opacity:0.6">Loris · Binance · Bybit · OKX · HL</small></div>
          <span class="arch-arrow">→</span>
          <div class="arch-node highlight">🛡️ TGE Trap Filter<br><small style="opacity:0.6">Unlock calendar · OI/mcap · spot-lead</small></div>
          <span class="arch-arrow">→</span>
          <div class="arch-node">📊 Signal Engine<br><small style="opacity:0.6">Basis · funding · net carry</small></div>
          <span class="arch-arrow">→</span>
          <div class="arch-node">👤 Operator Review<br><small style="opacity:0.6">Signal list → approve → execute</small></div>
          <span class="arch-arrow">→</span>
          <div class="arch-node">🔗 Execution Layer<br><small style="opacity:0.6">Rabby · Jumper · bridges</small></div>
        </div>
      </div>
    </div>

  </div><!-- /grid -->

  <footer>
    <span>BASIS ARB TOOL · Delta-Neutral Arbitrage Engine</span>
    <span>
      Last refresh: <span id="ts-footer">{TS}</span> ·
      <a href="https://github.com/{DASHBOARD_REPO}">GitHub</a> ·
      Human review required before any execution
    </span>
  </footer>
</div><!-- /container -->
<script>
  function updateTS() {{
    const now = new Date().toISOString().replace('T',' ').substring(0,19)+' UTC';
    document.getElementById('ts').textContent = now;
    document.getElementById('ts-footer').textContent = now;
  }}
  setInterval(updateTS, 60000);
</script>
</body>
</html>'''

with open(f"{TMPDIR}/index.html", "w") as f:
    f.write(html)

print(f"Dashboard regenerated: {len(signals)} signals, {len(ok_signals)} tradable, {len(excl_signals)} excluded")
PYEOF

# --- commit and push ---
cd "$TMPDIR"
git config user.email "hermes-agent@basis-arb-tool"
git config user.name "Hermes Agent (basis-arb-tool)"
git add index.html
git commit -m "chore: refresh dashboard $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git push origin "$DASHBOARD_BRANCH" 2>&1

rm -rf "$TMPDIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Dashboard pushed to $DASHBOARD_REPO:$DASHBOARD_BRANCH"
