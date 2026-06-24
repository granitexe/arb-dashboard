"""Delta-neutral carry estimate (long spot / short perp).

Funding is sourced ONLY from Loris venue funding (context-only Hyperliquid
funding is excluded from the ranked estimate). The estimate is intentionally
conservative about realizability: it surfaces the ways delta-neutral breaks
(funding flip, basis blowout, ADL) as caveats.
"""
from __future__ import annotations

from typing import Optional

from ..config import BasisArbConfig
from ..models import CarryEstimate, CoinRawInput
from .stats import median, weighted_median


def estimate_carry(raw: CoinRawInput, cfg: BasisArbConfig) -> CarryEstimate:
    # 1) Funding APR per venue (Loris only; never context-only HL funding).
    venue_funding: dict[str, float] = {}
    venue_f8: dict[str, float] = {}
    for venue, vf in raw.funding_by_venue.items():
        if vf.context_only or vf.funding_apr is None or vf.funding_8h_decimal is None:
            continue
        venue_funding[venue] = vf.funding_apr
        venue_f8[venue] = vf.funding_8h_decimal

    if not venue_funding:
        return CarryEstimate(
            coin=raw.coin, aggregation_method="unavailable",
            unavailable_reason="no Loris funding for coin (set LORIS_API_KEY / upgrade tier)",
            caveats=_base_caveats(raw, cfg),
        )

    # 2) Aggregate funding: OI-weighted median when enough venues have OI.
    weights = {v: (raw.markets_by_venue[v].perp_open_interest_usd or 0.0)
               for v in venue_funding if v in raw.markets_by_venue}
    positive_oi = [v for v, w in weights.items() if w > 0]
    if cfg.funding_aggregation == "oi_weighted_median" and len(positive_oi) >= cfg.min_oi_weighted_venues:
        vals = [venue_funding[v] for v in positive_oi]
        wts = [weights[v] for v in positive_oi]
        funding_apr = weighted_median(vals, wts)
        method = "oi_weighted_median"
    else:
        funding_apr = median(list(venue_funding.values()))
        method = "median" if len(venue_funding) > 1 else "single_venue"

    f8 = funding_apr / (cfg.funding_periods_per_day * cfg.days_per_year) if funding_apr is not None else None

    # 3) Basis APR per venue: (mark - spot)/spot annualized over convergence window.
    venue_basis: dict[str, float] = {}
    for venue, mkt in raw.markets_by_venue.items():
        if mkt.perp_mark_price and mkt.spot_price:
            basis_pct = (mkt.perp_mark_price - mkt.spot_price) / mkt.spot_price
            venue_basis[venue] = basis_pct * cfg.days_per_year / cfg.basis_convergence_days
    basis_apr = median(list(venue_basis.values())) if venue_basis else None
    basis_pct_agg = (basis_apr * cfg.basis_convergence_days / cfg.days_per_year) if basis_apr is not None else None

    total = (funding_apr or 0.0) + (basis_apr or 0.0)

    # 4) Diagnostic best venue (highest complete funding+basis), not used for ranking.
    selected_short = None
    best = None
    for v in venue_funding:
        combined = venue_funding[v] + venue_basis.get(v, 0.0)
        if best is None or combined > best:
            best, selected_short = combined, v
    selected_spot = max(
        (v for v, m in raw.markets_by_venue.items() if m.spot_price and m.spot_daily_volume_usd),
        key=lambda v: raw.markets_by_venue[v].spot_daily_volume_usd or 0.0,
        default=None,
    )

    caveats = _base_caveats(raw, cfg)
    if f8 is not None and abs(f8) < cfg.funding_flip_near_zero_8h:
        caveats.append("funding_flip_risk: funding near zero; sign can flip and you start paying")
    hl_ctx = next((vf for vf in raw.funding_by_venue.values() if vf.context_only and vf.funding_8h_decimal is not None), None)
    if hl_ctx is not None and f8 is not None and (hl_ctx.funding_8h_decimal or 0.0) * f8 < 0:
        caveats.append("funding_flip_risk: Hyperliquid funding sign disagrees with cross-venue funding")
    if any(abs(b) >= cfg.basis_blowout_pct * cfg.days_per_year / cfg.basis_convergence_days for b in venue_basis.values()):
        caveats.append("basis_blowout_risk: wide perp/spot basis can pressure the short leg before it converges")

    return CarryEstimate(
        coin=raw.coin,
        aggregation_method=method,
        selected_short_venue=selected_short,
        selected_spot_venue=selected_spot,
        funding_8h_decimal=f8,
        funding_apr=funding_apr,
        basis_pct=basis_pct_agg,
        basis_apr=basis_apr,
        total_carry_apr=total,
        venue_funding_aprs=venue_funding,
        venue_basis_aprs=venue_basis,
        caveats=caveats,
    )


def _base_caveats(raw: CoinRawInput, cfg: BasisArbConfig) -> list[str]:
    return [
        "adl_risk: a profitable short can be auto-deleveraged while the spot leg stays exposed",
        "costs_excluded: ignores spot borrow, execution slippage, transfer/withdrawal and margin friction",
    ]
