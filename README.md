# Basis Arb Tool — Status Dashboard

**Live status page:** https://granitexe.github.io/arb-dashboard/

This repository serves the public GitHub Pages dashboard for the Basis Arb Tool — a delta-neutral funding-rate arbitrage engine.

## What is this?

The **Basis Arb Tool** watches cross-exchange funding rates and basis spreads to identify organic carry opportunities (long spot / short perpetual futures), while explicitly excluding tokens showing TGE/manufactured carry signatures.

## Dashboard Sections

| Panel | Description |
|---|---|
| **Signal Feed** | Current basis/funding opportunities with trap scores |
| **TGE Trap Monitor** | Tokens flagged or cleared based on unlock calendars, OI/mcap ratios |
| **Risk Controls** | Kill-switches, leverage caps, drawdown limits |
| **Data Sources** | Live API connections: Loris, Binance, Bybit, OKX, Hyperliquid, CoinGecko, DeFiLlama |
| **Architecture** | System flow from data ingestion → filter → signal → operator → execution |
| **Key Features** | TGE detection, ADL modeling, bankroll sizing, cross-venue basis |
| **Cron Jobs** | Self-improvement loop: signal audit (4h), research scan (6h), code review (daily) |
| **Changelog** | Version history |

## Important

- **Signals require human operator review** — nothing is executed automatically
- The tool does NOT hold private keys or move funds
- All credentials are operator-configured, never shared

## Related

- **Private tool repo:** `basis-arb-tool` (on the operator's machine)
- **Agent:** This tool is built and maintained by an autonomous coding agent (Hermes) per the SOUL.md mandate

## Enabling GitHub Pages

If Pages is ever disabled:
1. Repo Settings → Pages → Source: `main` branch, `/ (root)`
2. Dashboard available at `https://granitexe.github.io/arb-dashboard/`
