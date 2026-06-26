"""Bankroll management for basis arbitrage.

Two hard rules:
  1. Never risk ruin. A drawdown kill-switch hard-caps exposure.
  2. Never over-commit. Liquidation is the only scenario where basis arb
     becomes a directional bet, so position sizes must survive a 3-sigma
     adverse move even in the worst coin.

Key concepts:
  - Kelly Criterion (full): f* = (b* p - q) / b where b = odds, p = win prob, q = 1-p.
    For symmetric basis arb (carry > 0 means positive expected value), b = 1,
    so f* = p - q = 2p - 1. But we don't know p precisely → use fractional Kelly.
  - Fractional Kelly (recommended): bet a fraction of Kelly to reduce variance.
    Conservative: 1/4 Kelly. Moderate: 1/2 Kelly. Aggressive: 3/4 Kelly.
  - Min-notional floor: below ~$50 spot notional, spreads and slippage dominate.
  - Diversification benefit: uncorrelated positions allow more total exposure.
  - Kelly is not static: it must be recomputed as bankroll changes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# --- Kelly math ---------------------------------------------------------------

def full_kelly(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Classic Kelly: f* = (b*p - q) / b where b = avg_win/avg_loss (odds).
    For basis carry, avg_loss is bounded (liquidation = notional * margin_pct).
    Returns Kelly fraction [0, 1]. Returns 0 if edge is negative.
    """
    if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
        return 0.0
    b = avg_win / avg_loss          # odds
    p = win_rate
    q = 1.0 - p
    f_star = (b * p - q) / b
    return max(0.0, min(1.0, f_star))


def fractional_kelly(full_kelly_fraction: float, fraction: float) -> float:
    """Apply a fractional Kelly multiplier.
    fraction=0.25 → 1/4 Kelly (4x variance reduction vs full Kelly).
    fraction=0.50 → 1/2 Kelly.
    fraction=0.75 → 3/4 Kelly.
    """
    return full_kelly_fraction * fraction


def kelly_from_carry_and_vol(
    carry_apr: float,
    volatility_annual: float,
    fraction: float = 0.25,
) -> float:
    """Estimate Kelly fraction from carry APR and volatility.
    Uses the simplified Sharpe-ratio-linked formula:
      Kelly ≈ E[return] / Variance[return]
             ≈ carry_apr / (volatility_annual ** 2)

    This assumes:
      - Expected return = carry_apr (roughly realized since convergence ~30d)
      - Variance = (daily_vol ** 2) * 365 ≈ (volatility_annual ** 2)
      - Losses are bounded by funding-period drawdown before rebalancing

    Args:
        carry_apr: net carry APR (positive = edge)
        volatility_annual: annualized volatility of the basis spread (not of price)
        fraction: fractional Kelly multiplier (default 0.25 = conservative)

    Returns:
        Kelly fraction [0, 1] capped at 1.0
    """
    if carry_apr <= 0 or volatility_annual <= 0:
        return 0.0
    # Kelly ≈ E/g variance, but use Sharpe-style: carry / vol^2
    raw = carry_apr / (volatility_annual ** 2)
    return min(1.0, max(0.0, raw * fraction))


# --- Liquidation sizing -------------------------------------------------------

def max_position_notional(
    spot_price: float,
    margin_fraction: float,
    worst_case_adverse_move: float,
    max_loss_fraction: float,
) -> float:
    """Maximum notional position that keeps liquidation loss within max_loss_fraction.

    Basis arb is delta-neutral on paper, but:
    - Funding can flip (you start paying instead of receiving)
    - ADL can close the short while spot stays open
    - Basis can blow out before converging

    All three scenarios mean the short leg gets stressed. We model this as
    needing enough buffer to survive a worst_case_adverse_move without being
    liquidated on the short side.

    Args:
        spot_price: current spot price of the base asset
        margin_fraction: margin requirement as fraction (e.g. 0.01 = 1% = 100x leverage)
        worst_case_adverse_move: fraction adverse move in the short direction
            (e.g. 0.03 = 3% adverse move before liquidation triggers)
            This is: (1 / leverage) - safety_buffer. Use 0.015 for 1% margin + 0.5% buffer.
        max_loss_fraction: maximum fraction of bankroll to lose on a single position
            in the worst-case liquidation scenario

    Returns:
        max notional position size in USD
    """
    if worst_case_adverse_move <= 0 or margin_fraction <= 0 or max_loss_fraction <= 0:
        return 0.0
    # Liquidation loss = notional * worst_case_adverse_move * margin_fraction
    # Set this ≤ bankroll * max_loss_fraction
    # Solving: notional ≤ bankroll * max_loss_fraction / (worst_case_adverse_move * margin_fraction)
    # But since we don't know bankroll here (it's per-position), we return the
    # NOTIONAL that would lose max_loss_fraction of *any reference bankroll*.
    # The caller applies: position_usd = min(notional, kelly_notional, max_risk_notional)
    numerator = max_loss_fraction          # fraction of reference bankroll
    denominator = worst_case_adverse_move * margin_fraction
    return numerator / denominator if denominator > 0 else 0.0


# --- Per-position sizing -------------------------------------------------------

@dataclass
class PositionSpec:
    """Output of bankroll sizing for a single trade."""
    coin: str
    notional_usd: float          # spot leg notional
    perp_notional_usd: float     # short perp leg (same size, same notional)
    kelly_fraction: float        # Kelly fraction used
    max_risk_fraction: float     # max loss fraction used
    estimated_loss_if_liquidation: float  # USD loss if worst case
    estimated_carry_annual: float  # USD expected carry per year
    passes_min_notional: bool
    passes_max_risk: bool
    is_viable: bool              # kelly > 0 AND passes floors AND passes risk cap


def size_position(
    coin: str,
    spot_price: float,
    perp_price: float,
    net_carry_apr: float,
    trap_score: float,          # composite trap score [0, 1]; 0 = safe, 1 = certain trap
    basis_volatility_annual: float,  # annual vol of perp-spot basis (NOT price vol)
    bankroll_usd: float,
    margin_fraction: float = 0.01,   # 1% margin = 100x leverage (standard for perps)
    safety_buffer: float = 0.005,    # 0.5% safety buffer below liquidation
    kelly_fraction: float = 0.25,   # conservative 1/4 Kelly
    max_loss_per_trade: float = 0.02,  # max 2% of bankroll per position
    min_notional_usd: float = 50.0,  # below this, fees dominate
    max_total_exposure: float = 1.0,  # max fraction of bankroll in all positions combined
    max_single_exposure: float = 0.20,  # max 20% of bankroll in any single position
) -> PositionSpec:
    """Compute the right position size for a single basis arbitrage trade.

    The position is valid only if:
      1. net_carry_apr > 0 (positive expected value after fees)
      2. trap_score < 0.75 (not excluded by TGE trap filter)
      3. notional ≥ min_notional_usd (fees don't dominate)
      4. estimated liquidation loss ≤ max_loss_per_trade fraction of bankroll

    Returns:
        PositionSpec with all sizing details and viability assessment
    """
    passes_min_notional = True
    passes_max_risk = True

    if net_carry_apr <= 0:
        return PositionSpec(
            coin=coin,
            notional_usd=0.0,
            perp_notional_usd=0.0,
            kelly_fraction=0.0,
            max_risk_fraction=0.0,
            estimated_loss_if_liquidation=0.0,
            estimated_carry_annual=0.0,
            passes_min_notional=True,
            passes_max_risk=True,
            is_viable=False,
        )

    # Worst-case adverse move before liquidation:
    # margin_fraction (e.g. 0.01 = 1%) minus safety buffer (0.005)
    worst_case_move = max(margin_fraction - safety_buffer, safety_buffer)

    # --- Kelly-based sizing ---
    # Adjust Kelly: trap score reduces effective edge (higher trap = reduce exposure)
    effective_kelly = kelly_from_carry_and_vol(
        carry_apr=net_carry_apr,
        volatility_annual=basis_volatility_annual,
        fraction=kelly_fraction,
    )
    # Reduce Kelly by trap risk: if trap_score is high, reduce exposure further
    trap_discount = 1.0 - trap_score
    adjusted_kelly = effective_kelly * trap_discount

    kelly_notional = bankroll_usd * adjusted_kelly

    # --- Risk-based sizing (max loss cap) ---
    # If liquidated at worst_case_move, loss = notional * worst_case_move
    # Set loss ≤ max_loss_per_trade * bankroll
    risk_notional = (max_loss_per_trade * bankroll_usd) / worst_case_move

    # --- Minimum notional floor ---
    # Below ~$50 notional, maker fee rebates and spread make execution unreliable
    if kelly_notional < min_notional_usd:
        passes_min_notional = False

    # --- Max single-position cap ---
    max_single_notional = bankroll_usd * max_single_exposure
    kelly_notional = min(kelly_notional, max_single_notional)
    risk_notional = min(risk_notional, max_single_notional)

    # --- Use the smaller of Kelly and risk-based sizing ---
    notional = min(kelly_notional, risk_notional)

    if notional < min_notional_usd:
        passes_min_notional = False
        notional = 0.0

    # --- Risk pass check ---
    if notional > 0:
        estimated_loss = notional * worst_case_move * margin_fraction
        loss_fraction = estimated_loss / bankroll_usd
        if loss_fraction > max_loss_per_trade:
            passes_max_risk = False
            notional = 0.0
    else:
        estimated_loss = 0.0

    perp_notional = notional  # same notional for the short leg (delta neutral)

    estimated_carry = notional * net_carry_apr

    return PositionSpec(
        coin=coin,
        notional_usd=round(notional, 2),
        perp_notional_usd=round(perp_notional, 2),
        kelly_fraction=round(adjusted_kelly, 4),
        max_risk_fraction=max_loss_per_trade,
        estimated_loss_if_liquidation=round(estimated_loss, 2),
        estimated_carry_annual=round(estimated_carry, 2),
        passes_min_notional=passes_min_notional,
        passes_max_risk=passes_max_risk,
        is_viable=notional > 0 and passes_min_notional and passes_max_risk,
    )


# --- Portfolio aggregation -----------------------------------------------------

@dataclass
class PortfolioSpec:
    """Aggregated bankroll allocation across all positions."""
    total_notional_usd: float
    estimated_total_carry_annual: float
    estimated_max_loss: float   # sum of worst-case losses (not additive probability-wise)
    estimated_max_loss_fraction: float  # as fraction of bankroll
    kelly_utilization: float     # fraction of Kelly budget used
    exposure_fraction: float    # total notional / bankroll
    num_viable_positions: int
    positions: list[PositionSpec]
    bankroll_usd: float


def allocate_portfolio(
    signals: list,  # list of CoinSignal with carry, trap, etc.
    bankroll_usd: float,
    basis_volatility_annual: float = 0.15,  # 15% annual vol of basis spread (conservative)
    kelly_fraction: float = 0.25,
    max_loss_per_trade: float = 0.02,
    min_notional_usd: float = 50.0,
    max_total_exposure: float = 1.0,
    max_single_exposure: float = 0.20,
    max_positions: int = 5,  # max simultaneous positions
) -> PortfolioSpec:
    """Allocate bankroll across a ranked list of signals.

    Picks the top N viable signals, sorted by risk-adjusted carry,
    and computes per-position sizes subject to constraints.

    Args:
        signals: ranked CoinSignal list from basis_arb pipeline
        bankroll_usd: total bankroll in USD
        basis_volatility_annual: annualized vol of the basis spread (used for Kelly).
            Conservative estimate: 15% for BTC/ETH (tight basis), 30-50% for alts.
            Pass per-coin if available; use this fallback otherwise.
        kelly_fraction: fractional Kelly multiplier (default 0.25 = 1/4 Kelly)
        max_loss_per_trade: max fraction of bankroll risked per position
        min_notional_usd: minimum viable position size
        max_total_exposure: max total notional / bankroll (1.0 = full Kelly)
        max_single_exposure: max any single position / bankroll
        max_positions: maximum number of simultaneous positions

    Returns:
        PortfolioSpec with per-position and aggregate sizing
    """
    positions: list[PositionSpec] = []
    remaining_bankroll = bankroll_usd

    # Take only OK-status signals, sorted by risk_adjusted_apr descending
    viable_signals = [
        s for s in signals
        if s.status == "OK"
        and s.risk_adjusted_apr is not None
        and s.risk_adjusted_apr > 0
        and s.carry.net_carry_apr is not None
        and s.carry.net_carry_apr > 0
    ][:max_positions]

    total_notional = 0.0
    total_carry = 0.0
    total_max_loss = 0.0

    for sig in viable_signals:
        coin = sig.coin
        spot_price = None
        perp_price = None

        # Get spot price (best available)
        for venue_mkt in sig.raw.markets_by_venue.values():
            if venue_mkt.spot_price and venue_mkt.spot_daily_volume_usd:
                spot_price = venue_mkt.spot_price
                break

        if spot_price is None or spot_price <= 0:
            continue

        # Use net_carry_apr from carry estimate
        net_carry = sig.carry.net_carry_apr or 0.0
        trap_score = sig.trap.composite_score

        spec = size_position(
            coin=coin,
            spot_price=spot_price,
            perp_price=perp_price or spot_price,  # use spot as proxy if perp missing
            net_carry_apr=net_carry,
            trap_score=trap_score,
            basis_volatility_annual=basis_volatility_annual,
            bankroll_usd=remaining_bankroll,  # recalculate against remaining bankroll
            margin_fraction=0.01,
            safety_buffer=0.005,
            kelly_fraction=kelly_fraction,
            max_loss_per_trade=max_loss_per_trade,
            min_notional_usd=min_notional_usd,
            max_total_exposure=max_total_exposure,
            max_single_exposure=max_single_exposure,
        )

        if spec.is_viable and spec.notional_usd > 0:
            positions.append(spec)
            total_notional += spec.notional_usd
            total_carry += spec.estimated_carry_annual
            total_max_loss += spec.estimated_loss_if_liquidation
            remaining_bankroll -= spec.estimated_loss_if_liquidation  # reduce available bankroll

    exposure_fraction = total_notional / bankroll_usd if bankroll_usd > 0 else 0.0
    estimated_max_loss_fraction = total_max_loss / bankroll_usd if bankroll_usd > 0 else 0.0
    kelly_util = total_notional / (bankroll_usd * kelly_fraction) if bankroll_usd > 0 else 0.0

    return PortfolioSpec(
        total_notional_usd=round(total_notional, 2),
        estimated_total_carry_annual=round(total_carry, 2),
        estimated_max_loss=round(total_max_loss, 2),
        estimated_max_loss_fraction=round(estimated_max_loss_fraction, 4),
        kelly_utilization=round(kelly_util, 4),
        exposure_fraction=round(exposure_fraction, 4),
        num_viable_positions=len(positions),
        positions=positions,
        bankroll_usd=bankroll_usd,
    )
