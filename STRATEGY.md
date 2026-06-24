# Basis / Funding-Rate Arbitrage Tool — Spec

## Job
Emit delta-neutral basis/funding signals (long spot, short perp) for review.
This tool only PRODUCES SIGNALS AND CODE. It does NOT execute trades, hold keys,
or touch a wallet. Execution happens on a separate machine, gated by a human.

## Data sources
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

## Output
A ranked signal list with a "trap score" per token, plus reasoning. No order placement.
