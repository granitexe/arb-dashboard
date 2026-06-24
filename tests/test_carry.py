from _helpers import make_raw, vfunding, vmarket

from basis_arb.config import BasisArbConfig
from basis_arb.signals.carry import estimate_carry

CFG = BasisArbConfig()


def test_funding_apr_annualization():
    # 10 bps 8h => 0.001 decimal => APR 0.001 * 3 * 365 = 1.095
    raw = make_raw(funding=[vfunding("binance", 0.001)],
                   markets=[vmarket("binance", mark=100.0, spot=100.0, oi_usd=1e9)])
    c = estimate_carry(raw, CFG)
    assert c.aggregation_method != "unavailable"
    assert abs(c.funding_apr - 1.095) < 1e-9


def test_negative_funding_stays_negative():
    raw = make_raw(funding=[vfunding("binance", -0.0005)],
                   markets=[vmarket("binance", mark=100.0, spot=100.0, oi_usd=1e8)])
    c = estimate_carry(raw, CFG)
    assert c.funding_apr < 0
    assert c.total_carry_apr < 0.0 or c.total_carry_apr <= c.funding_apr + 1e-9


def test_positive_basis_annualizes():
    # perp 2% rich to spot, convergence 30d => basis_apr = 0.02 * 365/30
    raw = make_raw(funding=[vfunding("binance", 0.0)],
                   markets=[vmarket("binance", mark=102.0, spot=100.0, oi_usd=1e8)])
    c = estimate_carry(raw, CFG)
    assert abs(c.basis_apr - 0.02 * 365 / 30) < 1e-9
    assert abs(c.total_carry_apr - c.basis_apr) < 1e-9  # funding zero


def test_missing_funding_unavailable_no_crash():
    raw = make_raw(funding=[], markets=[vmarket("binance", mark=100.0, spot=100.0)])
    c = estimate_carry(raw, CFG)
    assert c.aggregation_method == "unavailable"
    assert c.total_carry_apr is None
    assert c.unavailable_reason


def test_context_only_funding_is_excluded():
    # Only Hyperliquid context funding present -> not rankable -> unavailable.
    raw = make_raw(funding=[vfunding("hyperliquid", 0.001, context_only=True)],
                   markets=[vmarket("hyperliquid", mark=100.0, spot=100.0, oi_usd=1e9)])
    c = estimate_carry(raw, CFG)
    assert c.aggregation_method == "unavailable"


def test_oi_weighted_median_uses_weights():
    # Bybit funding (APR 1.095) carries far more OI than binance (APR ~ -1.6),
    # so OI-weighted median should land on the heavy venue's value.
    raw = make_raw(
        funding=[vfunding("binance", -0.0015), vfunding("bybit", 0.001)],
        markets=[vmarket("binance", mark=100, spot=100, oi_usd=1e6),
                 vmarket("bybit", mark=100, spot=100, oi_usd=1e10)],
    )
    c = estimate_carry(raw, CFG)
    assert c.aggregation_method == "oi_weighted_median"
    assert c.funding_apr > 0  # heavy (bybit) positive venue wins the weighted median


def test_funding_flip_caveat_near_zero():
    raw = make_raw(funding=[vfunding("binance", 0.000001)],
                   markets=[vmarket("binance", mark=100, spot=100, oi_usd=1e8)])
    c = estimate_carry(raw, CFG)
    assert any("funding_flip_risk" in cav for cav in c.caveats)
    assert any("adl_risk" in cav for cav in c.caveats)
