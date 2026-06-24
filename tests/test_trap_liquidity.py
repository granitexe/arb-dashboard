from _helpers import make_raw, vmarket

from basis_arb.config import BasisArbConfig
from basis_arb.signals.trap import illiquidity_subsignal, oi_market_cap_subsignal

CFG = BasisArbConfig()


def test_illiquidity_ratio_normalizes_high():
    raw = make_raw(markets=[vmarket("binance", oi_usd=1e9, spot_vol=1e8)])
    s = illiquidity_subsignal(raw, CFG)
    assert s.available is True
    assert abs(s.raw_value - 10.0) < 1e-6
    assert s.score > 0.99  # ratio at the configured high => ~1.0


def test_illiquidity_ratio_low_scores_zero():
    raw = make_raw(markets=[vmarket("binance", oi_usd=1e8, spot_vol=1e8)])
    s = illiquidity_subsignal(raw, CFG)
    assert s.raw_value == 1.0
    assert s.score == 0.0


def test_illiquidity_unavailable_without_volume():
    raw = make_raw(markets=[vmarket("binance", oi_usd=1e9)])  # no spot volume
    s = illiquidity_subsignal(raw, CFG)
    assert s.available is False


def test_illiquidity_tiny_volume_floor_triggers_hard_flag():
    raw = make_raw(markets=[vmarket("binance", oi_usd=1e9, spot_vol=1000.0)])
    s = illiquidity_subsignal(raw, CFG)
    # ratio = 1e9 / max(1000, 1e5 floor) = 1e4 >> hard ratio 25
    assert s.hard_flag is True


def test_oi_market_cap_hard_flag():
    raw = make_raw(markets=[vmarket("binance", oi_usd=5e8)], market_cap=1e9)
    s = oi_market_cap_subsignal(raw, CFG)
    assert abs(s.raw_value - 0.5) < 1e-9
    assert s.hard_flag is True


def test_oi_market_cap_unavailable_without_mcap():
    raw = make_raw(markets=[vmarket("binance", oi_usd=5e8)])
    s = oi_market_cap_subsignal(raw, CFG)
    assert s.available is False
