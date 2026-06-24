"""TGE-trap score.

Four sub-signals, each normalized to [0, 1] (higher = more trap-like):
  (a) upcoming token unlocks (DeFiLlama; fallback to FDV/MC + supply overhang)
  (b) spot-illiquidity-to-perp-OI ratio
  (c) spot leading perp on the way up (lead/lag of returns)
  (d) OI / market-cap distortion

A weighted composite plus hard flags drive EXCLUSION. The composite only
averages the sub-signals that have data, so a coin is never penalized for a
source being down -- but exclusion still fires on any hard flag.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from ..config import BasisArbConfig
from ..models import CoinRawInput, TrapBreakdown, TrapSubSignal
from .stats import clip01, linear_score, log_score, pearson


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


# --- (a) upcoming unlocks -----------------------------------------------------

def unlock_subsignal(raw: CoinRawInput, cfg: BasisArbConfig, now: datetime) -> TrapSubSignal:
    name = "upcoming_unlocks"
    circ = raw.circulating_supply
    resolved = not raw.unlock_data_missing

    if resolved and circ:
        pressure = 0.0
        nearest_days: Optional[float] = None
        hard = False
        for ev in raw.unlock_events:
            if ev.tokens is None or ev.tokens <= 0:
                continue
            days_until = max((ev.timestamp - now).total_seconds() / 86400.0, 0.0)
            if days_until > cfg.unlock_horizon_days:
                continue
            size_ratio = ev.pct_circulating_supply if ev.pct_circulating_supply is not None else ev.tokens / circ
            weight = math.exp(-days_until / cfg.unlock_proximity_half_life_days)
            pressure += size_ratio * weight
            nearest_days = days_until if nearest_days is None else min(nearest_days, days_until)
            if days_until <= cfg.unlock_hard_days and size_ratio >= cfg.unlock_hard_pct_circ:
                hard = True
        score = linear_score(pressure, cfg.unlock_pressure_low, cfg.unlock_pressure_high)
        hard_flag = hard and score >= cfg.unlock_hard_score
        if not raw.unlock_events:
            reason = "no unlock events within horizon"
        else:
            near = f"; nearest event in {nearest_days:.0f}d" if nearest_days is not None else ""
            reason = f"{_pct(pressure)} circ-equivalent unlock pressure over {cfg.unlock_horizon_days}d{near}"
        return TrapSubSignal(name, score, pressure, True, reason, hard_flag)

    # Fallback: supply overhang (FDV/MC and total/circulating).
    overhangs: list[float] = []
    if raw.fully_diluted_valuation_usd and raw.market_cap_usd:
        overhangs.append(raw.fully_diluted_valuation_usd / raw.market_cap_usd - 1.0)
    if raw.total_supply and circ:
        overhangs.append(raw.total_supply / circ - 1.0)
    overhangs = [o for o in overhangs if o > 0]
    if overhangs:
        overhang = max(overhangs)
        score = cfg.fallback_unlock_weight * linear_score(overhang, cfg.overhang_low, cfg.overhang_high)
        hard_flag = overhang >= cfg.overhang_hard
        reason = f"unlock schedule unavailable; supply overhang {overhang + 1:.1f}x used as proxy"
        return TrapSubSignal(name, clip01(score), overhang, True, reason, hard_flag)

    return TrapSubSignal(name, 0.0, None, False, "unlock schedule and supply overhang unavailable")


# --- (b) spot illiquidity to perp OI ------------------------------------------

def illiquidity_subsignal(raw: CoinRawInput, cfg: BasisArbConfig) -> TrapSubSignal:
    name = "spot_illiquidity_to_perp_oi"
    perp_oi = raw.perp_oi_usd_total()
    spot_vol = raw.spot_volume_usd_total()
    if perp_oi is None or spot_vol is None:
        return TrapSubSignal(name, 0.0, None, False, "perp OI or spot volume unavailable")
    ratio = perp_oi / max(spot_vol, cfg.min_volume_floor_usd)
    score = log_score(ratio, cfg.oi_spot_vol_ratio_low, cfg.oi_spot_vol_ratio_high)
    hard_flag = ratio >= cfg.oi_spot_vol_hard_ratio
    reason = f"perp OI is {ratio:.1f}x spot 24h volume; thin exit liquidity vs the short leg"
    return TrapSubSignal(name, score, ratio, True, reason, hard_flag)


# --- (c) spot leading perp on the way up --------------------------------------

def _aligned_returns(raw: CoinRawInput) -> tuple[list[float], list[float]]:
    spot = {b.timestamp: b.log_return for b in raw.spot_returns if b.log_return is not None}
    perp = {b.timestamp: b.log_return for b in raw.perp_returns if b.log_return is not None}
    common = sorted(set(spot) & set(perp))
    return [spot[t] for t in common], [perp[t] for t in common]


def _corr_lag(a: list[float], b: list[float], k: int) -> Optional[float]:
    """Correlation of a[t] with b[t+k] (a leads b by k bars)."""
    if k == 0:
        return pearson(a, b)
    if len(a) <= k:
        return None
    return pearson(a[:-k], b[k:])


def spot_leads_subsignal(raw: CoinRawInput, cfg: BasisArbConfig) -> TrapSubSignal:
    name = "spot_leading_perp"
    r_s, r_p = _aligned_returns(raw)
    if len(r_s) < cfg.min_return_bars:
        return TrapSubSignal(name, 0.0, None, False, f"insufficient aligned bars ({len(r_s)} < {cfg.min_return_bars})")

    corr0 = _corr_lag(r_s, r_p, 0)
    spot_leads = [c for c in (_corr_lag(r_s, r_p, k) for k in cfg.lead_lag_lags_bars) if c is not None]
    perp_leads = [c for c in (_corr_lag(r_p, r_s, k) for k in cfg.lead_lag_lags_bars) if c is not None]
    if not spot_leads:
        return TrapSubSignal(name, 0.0, None, False, "lead/lag correlation undefined (flat returns)")

    max_spot_leads = max(spot_leads)
    baseline = max([c for c in [corr0] if c is not None] + perp_leads + [0.0])
    lead_advantage = max_spot_leads - baseline
    cum_spot_return = math.exp(math.fsum(r_s)) - 1.0

    on_way_up = cum_spot_return >= cfg.spot_up_return_low and max_spot_leads >= cfg.min_spot_lead_corr
    if not on_way_up:
        return TrapSubSignal(name, 0.0, lead_advantage, True,
                             f"no spot-leads-up signature (spot move {_pct(cum_spot_return)}, lead corr {max_spot_leads:.2f})")
    score = (linear_score(lead_advantage, cfg.lead_corr_low, cfg.lead_corr_high)
             * linear_score(cum_spot_return, cfg.spot_up_return_low, cfg.spot_up_return_high))
    hard_flag = score >= cfg.spot_lead_hard_score and cum_spot_return >= cfg.spot_lead_hard_up_return
    reason = f"spot led perp (corr advantage {lead_advantage:.2f}) during a {_pct(cum_spot_return)} spot move"
    return TrapSubSignal(name, clip01(score), lead_advantage, True, reason, hard_flag)


# --- (d) OI / market-cap distortion -------------------------------------------

def oi_market_cap_subsignal(raw: CoinRawInput, cfg: BasisArbConfig) -> TrapSubSignal:
    name = "oi_market_cap_distortion"
    perp_oi = raw.perp_oi_usd_total()
    mcap = raw.market_cap_usd
    if perp_oi is None or mcap is None or mcap <= 0:
        return TrapSubSignal(name, 0.0, None, False, "perp OI or market cap unavailable")
    ratio = perp_oi / mcap
    score = log_score(ratio, cfg.oi_market_cap_ratio_low, cfg.oi_market_cap_ratio_high)
    hard_flag = ratio >= cfg.oi_market_cap_hard_ratio
    reason = f"perp OI is {_pct(ratio)} of market cap; derivatives may dominate the float"
    return TrapSubSignal(name, score, ratio, True, reason, hard_flag)


# --- composite ----------------------------------------------------------------

def compute_trap(raw: CoinRawInput, cfg: BasisArbConfig, now: Optional[datetime] = None) -> TrapBreakdown:
    now = now or datetime.now(timezone.utc)
    unlock = unlock_subsignal(raw, cfg, now)
    illiq = illiquidity_subsignal(raw, cfg)
    leads = spot_leads_subsignal(raw, cfg)
    oimc = oi_market_cap_subsignal(raw, cfg)

    weights = {
        "upcoming_unlocks": cfg.unlock_weight,
        "spot_illiquidity_to_perp_oi": cfg.spot_illiquidity_weight,
        "spot_leading_perp": cfg.spot_leading_weight,
        "oi_market_cap_distortion": cfg.oi_market_cap_weight,
    }
    subs = [unlock, illiq, leads, oimc]
    available = [s for s in subs if s.available]
    wsum = math.fsum(weights[s.name] for s in available)
    composite = math.fsum(s.score * weights[s.name] for s in available) / wsum if wsum > 0 else 0.0

    insufficient = len(available) < cfg.min_available_trap_subsignals
    exclusion_reasons: list[str] = []
    for s in subs:
        if s.hard_flag:
            exclusion_reasons.append(f"hard flag [{s.name}]: {s.reason}")
    if composite >= cfg.trap_exclusion_score:
        exclusion_reasons.append(f"composite trap score {composite:.2f} >= {cfg.trap_exclusion_score:.2f}")
    excluded = bool(exclusion_reasons)

    return TrapBreakdown(
        upcoming_unlocks=unlock,
        spot_illiquidity_to_perp_oi=illiq,
        spot_leading_perp=leads,
        oi_market_cap_distortion=oimc,
        composite_score=clip01(composite),
        weights_used=weights,
        excluded=excluded,
        exclusion_reasons=exclusion_reasons,
        unlock_data_missing=raw.unlock_data_missing,
        insufficient_trap_data=insufficient,
    )
