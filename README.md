# basis-arb-tool

Read-only **delta-neutral basis / funding-rate signal** scanner with a
**TGE-trap score**. For each tradable coin it estimates the carry of a
long-spot / short-perp position **and** scores how likely that carry is a
*manufactured* "TGE trap" rather than organic carry. It prints a ranked table
to stdout and writes a full JSON report.

> **This tool produces signals only.** It never places orders, never signs a
> transaction, never constructs a wallet, and never reads a private key.
> Execution is out of scope and is assumed to happen elsewhere, gated by a
> human. See [Guardrails](#guardrails).

See [`STRATEGY.md`](STRATEGY.md) for the underlying strategy spec.

## What it computes

### Carry estimate (long spot / short perp)
- **Funding APR** — from [Loris Tools](https://loris.tools) cross-venue funding
  (8h-normalized), aggregated across venues by OI-weighted median.
  `funding_apr = funding_8h_decimal * 3 * 365`.
- **Basis APR** — `(perp_mark - spot) / spot`, annualized over a configurable
  convergence window (`--basis-convergence-days`, default 30).
- **Total carry APR** = funding + basis. Every estimate carries caveats about
  how delta-neutral *breaks*: funding can flip (you start paying), basis can
  blow out before it converges, and ADL can force-close the short while the
  spot leg stays exposed. Execution/borrow/transfer costs are **not** modeled.

### Trap score (0 = clean, 1 = trap-like)
A weighted composite of four sub-signals from the TGE-trap signature in
`STRATEGY.md`. Any single hard flag, or a composite ≥ `trap_exclusion_score`
(default 0.75), **excludes** a coin from the tradable ranking.

| Sub-signal | What it measures | Source |
|---|---|---|
| `upcoming_unlocks` | Proximity- & size-weighted token unlocks over the horizon as a fraction of circulating supply. Falls back to FDV/MC and total/circulating **supply overhang** when no schedule is found. | DeFiLlama emissions |
| `spot_illiquidity_to_perp_oi` | `perp_OI_usd / spot_24h_volume_usd` — crowded short leg vs thin exit liquidity. | Exchanges + Hyperliquid |
| `spot_leading_perp` | Lead/lag cross-correlation of spot vs perp returns; only fires when **spot leads perp on the way up** (the squeeze signature). | Exchange klines |
| `oi_market_cap_distortion` | `perp_OI_usd / market_cap` — derivatives positioning dominating the float. | Exchanges/HL + CoinGecko |

`risk_adjusted_apr = total_carry_apr * (1 - trap_score)` for tradable coins.

## Data sources (all keyless except Loris)

| Source | Used for | Key |
|---|---|---|
| Loris Tools (`api.loris.tools/funding`) | cross-venue funding rates + OI rankings | `LORIS_API_KEY` (optional) |
| Hyperliquid SDK (`Info` only) | notional OI, premium, mark/oracle, spot supply/market cap | none |
| Binance / Bybit / OKX public REST | spot+perp prices (basis), notional OI, spot volume, lead/lag klines | none |
| CoinGecko | market cap, circulating + total supply, FDV | none |
| DeFiLlama (emissions datasets CDN) | token unlock / emissions schedules | none |

**Loris requires an API key.** Without `LORIS_API_KEY` the tool still runs and
emits the full **trap structure** for every coin, but carry is reported as
`CARRY_UNAVAILABLE`. On the Loris free tier only BTC and ETH return funding.

## Install

```bash
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# Keyless: structural trap signals for the default universe (carry unavailable).
python -m basis_arb

# Specific coins.
python -m basis_arb --coins BTC,ETH,SOL,TIA --output-json signals.json

# With a Loris key, funding/carry is populated.
LORIS_API_KEY=lk_live_xxx python -m basis_arb --universe-size 40
```

Progress is logged to stderr; the ranked table goes to stdout; the full
breakdown is written to `--output-json` (default `basis_arb_signals.json`).

### Useful flags
`--coins` · `--universe-size` · `--venues binance,bybit,okx,hyperliquid` ·
`--horizon-days` · `--basis-convergence-days` · `--lookback-days` ·
`--bar-interval 1h` · `--timeout` · `--retries` · `--cache-dir` · `--no-cache` ·
`--max-table-rows` · `--output-json` · `--hide-excluded` · `--quiet` ·
`--strict` (exit non-zero only if *every* source fails).

All scoring thresholds/weights live in `basis_arb/config.py`
(`BasisArbConfig`); the active values are snapshotted into the JSON report.

## Output

Stdout is a fixed-width ranked table (rank, coin, status, risk-adjusted/carry/
funding/basis APR, trap composite, sub-scores, short venue, top reason).
Statuses: `OK` (tradable), `CARRY_UNAVAILABLE`, `DATA_INSUFFICIENT`, `EXCLUDED`
(trap). The JSON report contains run metadata (timestamp, source status, config
snapshot, key presence) and a full per-coin breakdown including every
sub-signal's raw value, score, availability, and human-readable reason.

## Guardrails

- The Hyperliquid integration imports **only** `hyperliquid.info.Info` and
  `MAINNET_API_URL`. It never imports `hyperliquid.exchange`, signing utilities,
  or `eth_account`.
- No order placement, no wallet construction, no private-key handling anywhere.
  The only secret read is `LORIS_API_KEY` (an HTTP header), and only its
  presence (boolean) is ever recorded.
- `tests/test_guardrails.py` statically enforces the above.

## Tests

```bash
python -m pytest -q          # offline unit + guardrail tests (no network)
```

Unit tests cover the pure carry/trap/ranking math and the Loris parser against
captured fixtures; no network and no business-logic mocks. A live keyless
smoke test is `python -m basis_arb --coins BTC,ETH --universe-size 2`.

## Caveats

Funding/basis APR overstates realizable returns (ignores fees, slippage,
borrow, margin). Spot volume can be wash-traded. Lead/lag over a few days of
hourly bars is noisy — treat it as one weighted input, never proof. Cross-source
symbol joins can misfire on wrapped/bridged/`1000x` tickers; see
`symbol_overrides` in the config.
