from _helpers import make_raw

from basis_arb.config import BasisArbConfig
from basis_arb.models import CarryEstimate, TrapBreakdown, TrapSubSignal
from basis_arb.signals.ranking import build_signal, rank_signals

CFG = BasisArbConfig()
WEIGHTS = {"upcoming_unlocks": 0.35, "spot_illiquidity_to_perp_oi": 0.25,
           "spot_leading_perp": 0.20, "oi_market_cap_distortion": 0.20}


def _sub(name, score=0.0, hard=False, available=True):
    return TrapSubSignal(name, score, score, available, "", hard)


def _trap(composite, *, excluded=False, insufficient=False):
    reasons = ["excluded"] if excluded else []
    return TrapBreakdown(
        _sub("upcoming_unlocks"), _sub("spot_illiquidity_to_perp_oi"),
        _sub("spot_leading_perp"), _sub("oi_market_cap_distortion"),
        composite, WEIGHTS, excluded, reasons, False, insufficient,
    )


def _carry(total):
    return CarryEstimate(coin="X", aggregation_method="median", total_carry_apr=total,
                         funding_apr=total, basis_apr=0.0)


def test_risk_adjusted_formula():
    raw = make_raw(coin="AAA")
    sig = build_signal(raw, _carry(0.10), _trap(0.25), CFG)
    assert sig.status == "OK"
    assert abs(sig.risk_adjusted_apr - 0.10 * (1 - 0.25)) < 1e-12


def test_excluded_has_no_score():
    raw = make_raw(coin="BBB")
    sig = build_signal(raw, _carry(0.20), _trap(0.9, excluded=True), CFG)
    assert sig.status == "EXCLUDED"
    assert sig.risk_adjusted_apr is None


def test_carry_unavailable_status():
    raw = make_raw(coin="CCC")
    unavailable = CarryEstimate(coin="CCC", aggregation_method="unavailable", unavailable_reason="no funding")
    sig = build_signal(raw, unavailable, _trap(0.3), CFG)
    assert sig.status == "CARRY_UNAVAILABLE"
    sig2 = build_signal(raw, unavailable, _trap(0.3, insufficient=True), CFG)
    assert sig2.status == "DATA_INSUFFICIENT"


def test_ranking_orders_ok_first_then_excluded_last():
    ok_lo = build_signal(make_raw(coin="LO"), _carry(0.05), _trap(0.1), CFG)
    ok_hi = build_signal(make_raw(coin="HI"), _carry(0.30), _trap(0.1), CFG)
    excl = build_signal(make_raw(coin="EX"), _carry(0.50), _trap(0.9, excluded=True), CFG)
    unavail = build_signal(make_raw(coin="UN", loris_rank=2),
                           CarryEstimate(coin="UN", aggregation_method="unavailable"), _trap(0.4), CFG)
    ranked = rank_signals([ok_lo, excl, unavail, ok_hi])
    coins = [s.coin for s in ranked]
    assert coins[0] == "HI" and coins[1] == "LO"   # OK by risk-adjusted desc
    assert coins[-1] == "EX"                        # excluded last
    assert [s.rank for s in ranked] == [1, 2, 3, 4]
