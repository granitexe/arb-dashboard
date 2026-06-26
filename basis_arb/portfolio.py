"""Portfolio view: combine signals with live bankroll sizing.

This module brings together the ranked signals from the pipeline with the
bankroll manager to produce an actionable portfolio view.

Key design principle: this module NEVER holds keys, NEVER executes trades,
and NEVER writes secrets anywhere. It is a read-only analysis module.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .bankroll import (
    PortfolioSpec,
    PositionSpec,
    allocate_portfolio,
    kelly_from_carry_and_vol,
    size_position,
)
from .models import CoinSignal, to_jsonable, utcnow


# Default basis volatility per coin type (annualized).
# These are estimates of the volatility of the PERP-SPOT BASIS, not of price.
# Basis vol is typically MUCH lower than price vol for liquid coins.
# Source: empirical observation; recalibrate from live data.
BASIS_VOL_ESTIMATES = {
    # BTC/ETH: very tight basis, <5% annual basis vol
    "BTC": 0.03,
    "ETH": 0.04,
    # Large-cap alts: 5-10%
    "SOL": 0.08,
    "ARB": 0.10,
    "OP": 0.10,
    "AVAX": 0.08,
    "MATIC": 0.12,
    "APT": 0.10,
    "SUI": 0.12,
    "SEI": 0.12,
    # Mid-cap: 15-25%
    "W": 0.15,
    "JTO": 0.15,
    "JUP": 0.18,
    "ENA": 0.20,
    "WLD": 0.25,
    "BERN": 0.30,
    # Unknown: conservative fallback
    "DEFAULT": 0.15,
}


def get_basis_vol(coin: str) -> float:
    """Return estimated annual basis volatility for a coin.
    Falls back to DEFAULT (15%) for unknown coins.
    """
    return BASIS_VOL_ESTIMATES.get(coin.upper(), BASIS_VOL_ESTIMATES["DEFAULT"])


@dataclass
class PortfolioSignal:
    """A CoinSignal enriched with position sizing from the bankroll manager."""
    coin: str
    status: str
    net_carry_apr: float
    risk_adjusted_apr: float
    trap_score: float
    position_notional: float
    kelly_fraction: float
    estimated_annual_carry: float
    estimated_liquidation_loss: float
    is_viable: bool
    rank: Optional[int]
    caveats: list[str]
    excluded_reason: Optional[str]

    @classmethod
    def from_signal(
        cls,
        sig: CoinSignal,
        bankroll_usd: float,
        kelly_fraction: float = 0.25,
        max_loss_per_trade: float = 0.02,
        min_notional_usd: float = 50.0,
    ) -> "PortfolioSignal":
        """Convert a CoinSignal to a PortfolioSignal with sizing computed."""
        basis_vol = get_basis_vol(sig.coin)
        net_carry = sig.carry.net_carry_apr or 0.0
        trap_score = sig.trap.composite_score

        # Get spot price
        spot_price = None
        for mkt in sig.raw.markets_by_venue.values():
            if mkt.spot_price and mkt.spot_daily_volume_usd:
                spot_price = mkt.spot_price
                break

        if sig.status == "EXCLUDED":
            return cls(
                coin=sig.coin,
                status="EXCLUDED",
                net_carry_apr=net_carry,
                risk_adjusted_apr=sig.risk_adjusted_apr or 0.0,
                trap_score=trap_score,
                position_notional=0.0,
                kelly_fraction=0.0,
                estimated_annual_carry=0.0,
                estimated_liquidation_loss=0.0,
                is_viable=False,
                rank=sig.rank,
                caveats=sig.carry.caveats,
                excluded_reason=sig.trap.exclusion_reasons[0] if sig.trap.exclusion_reasons else "trap",
            )

        if sig.status != "OK" or spot_price is None or spot_price <= 0:
            return cls(
                coin=sig.coin,
                status=sig.status,
                net_carry_apr=net_carry,
                risk_adjusted_apr=sig.risk_adjusted_apr or 0.0,
                trap_score=trap_score,
                position_notional=0.0,
                kelly_fraction=0.0,
                estimated_annual_carry=0.0,
                estimated_liquidation_loss=0.0,
                is_viable=False,
                rank=sig.rank,
                caveats=sig.carry.caveats,
                excluded_reason=sig.carry.unavailable_reason,
            )

        spec = size_position(
            coin=sig.coin,
            spot_price=spot_price,
            perp_price=spot_price,  # use spot as proxy (basis is small)
            net_carry_apr=net_carry,
            trap_score=trap_score,
            basis_volatility_annual=basis_vol,
            bankroll_usd=bankroll_usd,
            margin_fraction=0.01,
            safety_buffer=0.005,
            kelly_fraction=kelly_fraction,
            max_loss_per_trade=max_loss_per_trade,
            min_notional_usd=min_notional_usd,
            max_total_exposure=1.0,
            max_single_exposure=0.20,
        )

        return cls(
            coin=sig.coin,
            status=sig.status,
            net_carry_apr=net_carry,
            risk_adjusted_apr=sig.risk_adjusted_apr or 0.0,
            trap_score=trap_score,
            position_notional=spec.notional_usd,
            kelly_fraction=spec.kelly_fraction,
            estimated_annual_carry=spec.estimated_carry_annual,
            estimated_liquidation_loss=spec.estimated_loss_if_liquidation,
            is_viable=spec.is_viable,
            rank=sig.rank,
            caveats=sig.carry.caveats,
            excluded_reason=None,
        )


@dataclass
class PortfolioReport:
    """Full portfolio analysis: signals + sizing + aggregate risk."""
    generated_at: str
    bankroll_usd: float
    kelly_fraction: float
    max_loss_per_trade: float
    total_notional_usd: float
    estimated_total_carry_annual: float
    estimated_max_loss: float
    estimated_max_loss_fraction: float
    kelly_utilization: float
    num_viable_positions: int
    max_positions: int
    signals: list  # dicts from PortfolioSignal


def build_portfolio_report(
    signals: list[CoinSignal],
    bankroll_usd: float,
    kelly_fraction: float = 0.25,
    max_loss_per_trade: float = 0.02,
    min_notional_usd: float = 50.0,
    max_positions: int = 5,
) -> PortfolioReport:
    """Build a full portfolio report from ranked signals.

    Args:
        signals: ranked list of CoinSignal from basis_arb pipeline
        bankroll_usd: operator's available bankroll
        kelly_fraction: fractional Kelly multiplier (0.25 = conservative 1/4 Kelly)
        max_loss_per_trade: max fraction of bankroll at risk per position
        min_notional_usd: minimum position size for execution viability
        max_positions: max simultaneous positions to allocate

    Returns:
        PortfolioReport ready for display / JSON export
    """
    # Per-signal sizing
    portfolio_signals = [
        PortfolioSignal.from_signal(
            sig=sig,
            bankroll_usd=bankroll_usd,
            kelly_fraction=kelly_fraction,
            max_loss_per_trade=max_loss_per_trade,
            min_notional_usd=min_notional_usd,
        )
        for sig in signals
    ]

    # Aggregate allocation
    portfolio_spec = allocate_portfolio(
        signals=signals,
        bankroll_usd=bankroll_usd,
        basis_volatility_annual=0.15,  # conservative global default
        kelly_fraction=kelly_fraction,
        max_loss_per_trade=max_loss_per_trade,
        min_notional_usd=min_notional_usd,
        max_positions=max_positions,
    )

    return PortfolioReport(
        generated_at=utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        bankroll_usd=bankroll_usd,
        kelly_fraction=kelly_fraction,
        max_loss_per_trade=max_loss_per_trade,
        total_notional_usd=portfolio_spec.total_notional_usd,
        estimated_total_carry_annual=portfolio_spec.estimated_total_carry_annual,
        estimated_max_loss=portfolio_spec.estimated_max_loss,
        estimated_max_loss_fraction=portfolio_spec.estimated_max_loss_fraction,
        kelly_utilization=portfolio_spec.kelly_utilization,
        num_viable_positions=portfolio_spec.num_viable_positions,
        max_positions=max_positions,
        signals=[asdict(ps) for ps in portfolio_signals],
    )


def load_bankroll(path: str | Path) -> float:
    """Load bankroll from a file. File contains a single number (USD).

    This is the ONLY place bankroll is read from disk. The file should be
    owned and created by the operator, not by this tool. The tool reads
    it to compute sizing but never writes a value the operator hasn't set.

    File format: plain text, single line, numeric value in USD.
    Example: "1500.00\n"

    Returns:
        bankroll in USD, or 0.0 if file doesn't exist or is invalid.
    """
    p = Path(path)
    if not p.exists():
        return 0.0
    try:
        return float(p.read_text().strip())
    except (ValueError, OSError):
        return 0.0
