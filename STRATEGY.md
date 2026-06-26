# Basis / Funding-Rate Arbitrage Tool — Spec

## Job
Emit delta-neutral basis/funding signals (long spot, short perp) for review.
This tool only PRODUCES SIGNALS AND CODE. It does NOT execute trades, hold keys,
or touch a wallet. Execution happens on a separate machine, gated by a human.

## Architecture: Signal-Only Tool
This is a SIGNALS tool, not an execution agent. It:
- Reads public market data (no keys needed for data)
- Produces ranked signal lists with carry estimates and trap scores
- Does NOT hold private keys, seed phrases, or wallet credentials
- Does NOT sign transactions, bridge assets, or initiate withdrawals
- Does NOT execute trades automatically

**If an agent is used to operate this tool**, it must:
- NEVER hold exchange API keys — they must live in the operator's environment only
- NEVER export keys to output, logs, or JSON reports
- Be a "read-only analyst" that produces signals, not an execution engine
- Have explicit allowlist of allowed URL hosts (see safety.py)

## Execution Layer
Execution is the operator's job. This tool's outputs are signals.
If you connect it to an execution layer (e.g. tread.fi, a bot, a CEX API):
- API keys must live on the operator's machine, NOT in this codebase
- The tool's output is a signal list, not a trade instruction
- All execution is at the operator's discretion after reviewing the signal

## Data Sources
- Loris Tools API (api.loris.tools/funding): cross-venue funding_rate (8h-normalized bps) and
  open-interest rankings. Auth via `X-Api-Key` read from the LORIS_API_KEY env var. Free tier
  returns BTC/ETH only; full multi-venue coverage needs a paid key. Poll <= once per 60s.
- Public exchange REST (Binance, Bybit, OKX): spot + perp prices for basis and spot-vs-perp
  lead/lag. Keyless.
- Hyperliquid SDK (github.com/hyperliquid-dex): venue + on-chain funding, notional open
  interest, premium, and spot circulating/total supply. Keyless.
- CoinGecko API: market cap, circulating + total supply (OI / market-cap distortion). Keyless.
- DeFiLlama API: token unlock / emissions schedules (upcoming-unlock signal). Keyless via the
  emissions datasets CDN.
- aggr.trade: spot-vs-perp flow (dislocation spotting).
- Evaluate and report on: hl.eco, kiyotaka.ai, hydromancer.xyz (treat as unvetted).

## HARD GUARDRAIL — the TGE trap (must be implemented, not optional)
Distinguish ORGANIC funding/basis carry from MANUFACTURED carry. Some altcoins show fat
positive funding because foundations buy spot to squeeze TGE hedgers, then unlocks dump
the token for 6-12 months. A naive long-spot/short-perp harvester walks into this.

The tool MUST down-weight or EXCLUDE tokens showing the TGE-deal signature:
- large upcoming token unlocks,
- abnormal spot-illiquidity-to-perp-OI ratio,
- spot LEADING perp on the way up,
- OI / market-cap distortions.

Treat "delta-neutral" as breakable:
- funding can flip so you start PAYING,
- basis can blow out before it converges and pressure the short leg,
- auto-deleveraging (ADL) can force-close a profitable short while the spot leg bleeds.

The single most valuable feature is telling real carry from a trap.

## Carry and Net Carry
The tool computes two carry estimates:
- `total_carry_apr`: gross carry before execution costs (funding APR + basis APR)
- `net_carry_apr`: carry minus execution fee floor (8 bps round-trip default, annualized)

The execution fee floor is: `bps/10000 * 3 * 365 = bps * 0.1095`% APR per bps.
At 8 bps default: ~8.76% APR just in execution costs. BTC/ETH carry (~15% gross) leaves
~6% net — barely worth execution risk. New venues (Hyperliquid: ~2 bps) leave ~11% net.

## Output
A ranked signal list with:
- trap score per token + reasoning
- net carry APR after fees
- position sizing via bankroll.py (Kelly-based, with hard caps)
- risk-adjusted carry rank

No order placement. Execution is the operator's decision.

## Bankroll Management (bankroll.py)
The bankroll manager computes position sizes for small-capital operators.
Key principles:
- Never risk ruin: hard cap of 2% of bankroll per position at liquidation
- Kelly Criterion (fractional, 1/4 Kelly default) as starting point
- Min-notional floor: $50 — below this, fees dominate
- Max single position: 20% of bankroll
- Max total exposure: 100% of bankroll
- Negative net carry: position is flagged but not auto-sized

## Autoresearch Loop (autoresearch.py)
Inspired by Karpathy's AutoResearch, the tool has a self-improvement loop:
- `program.md` defines the goal and immutable rules
- Experiments are proposed → evaluated against live signals → kept or reverted
- Ratchet principle: score only goes up; bad changes are auto-reverted
- Safety validation before any experiment: no new secret imports, no shell=True, no eval
- Results logged to `.cron_output/autoresearch_results.tsv`

## Safety Layer (safety.py)
Hard constraints that cannot be overridden:
1. KILL-SWITCH hard caps: MAX_TOTAL_EXPOSURE=100%, MAX_SINGLE=20%, MAX_LOSS_PER_TRADE=2%
2. SECRET DETECTION: scans all output for leaked keys/tokens
3. EXECUTION GUARDRAILS: validate_url_host() blocks SSRF and private IPs
4. BANKROLL HARD CAPS: apply_hard_caps() overrides any bad sizing
5. SELF-IMPROVEMENT GUARDRAILS: no new crypto key libraries, no shell=True, no eval/exec
