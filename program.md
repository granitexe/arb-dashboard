# Basis Arb Tool — Self-Improvement Program

**IMPROVEMENT GOAL**: Maximize risk-adjusted carry (net carry APR after fees, after trap discount) across the ranked signal list, without introducing new tail risks.

---

## What's Immutable (Do Not Modify)

- `basis_arb/safety.py` — safety rules are hard-coded and cannot be overridden
- `basis_arb/pipeline.py` — pipeline structure is stable
- `basis_arb/models.py` — data model schema is stable
- Any file containing the word `key`, `secret`, `token`, or `password` in its imports

---

## What's Allowed to Change

### Per-venue carry weights
Modify `basis_arb/config.py` — add per-venue fee tables and venue age/competition factors.
Rationale: new venues (Hyperliquid, Paradex) have lower fees and should be weighted higher.

### Trap thresholds
Modify `basis_arb/config.py` — tune trap exclusion scores, unlock horizon, OI ratios.
Rationale: these thresholds determine false positive/negative rates — they need live calibration.

### Carry aggregation
Modify `basis_arb/signals/carry.py` — change how funding rates are aggregated across venues.
Rationale: OI-weighted median vs simple median may perform differently over time.

### Ranking formula
Modify `basis_arb/signals/ranking.py` — change how risk-adjusted APR is computed.
Rationale: the formula (carry * (1 - trap_score)) is a starting point, not gospel.

### Knowledge base
Add/edit files in `knowledge/x/` — new insights about carry mechanics, new venue data.
Rationale: the tool's intelligence grows with its knowledge base.

### Scripts
Add/edit scripts in `scripts/` — new monitors, new analysis tools.
Rationale: scripts are disposable and testable without touching core logic.

---

## What's Forbidden

1. **No new key-handling imports** — web3, eth_account, bitcoinlib, btcrecover
2. **No shell=True subprocess calls**
3. **No eval/exec of dynamic strings**
4. **No hardcoded secrets** — api_key = "..." patterns are rejected
5. **No new URL hosts** — requests must go to the documented data sources only
6. **No modification of safety.py or pipeline.py**

---

## Evaluation Metric

The single metric to optimize:

```
score = mean([s.risk_adjusted_apr for s in ok_signals if s.risk_adjusted_apr > 0])
```

Higher = better. A change that raises this score while not increasing trap exclusion
rate or reducing viable signal count is a genuine improvement.

Secondary metrics (don't sacrifice for the primary):
- Exclusion rate: should stay stable or decrease (fewer false positives)
- Viable signal count: should stay ≥ 3 (diversification floor)
- Minimum viable carry: net_carry_apr of the 5th-ranked coin should be > 0

---

## Improvement Directions (Prioritized)

1. **Per-venue carry weighting** — weight Hyperliquid carry more (lower fees = more net)
2. **Slippage model** — add a size-dependent slippage estimate to net_carry_apr
3. **Basis convergence model** — adapt convergence_days based on coin liquidity
4. **Backtesting module** — historical validation of carry estimates
5. **Per-coin basis vol** — use actual historical basis vol instead of static estimates
6. **Carry stability score** — weight carry by its consistency across time
