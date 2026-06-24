from _helpers import event, make_raw

from basis_arb.config import BasisArbConfig
from basis_arb.signals.trap import unlock_subsignal
from datetime import datetime, timezone

CFG = BasisArbConfig()
NOW = datetime.now(timezone.utc)
CIRC = 1_000_000_000.0


def test_future_unlock_within_horizon_contributes():
    raw = make_raw(circ=CIRC, unlock_missing=False, events=[event(9, 0.05 * CIRC)])
    s = unlock_subsignal(raw, CFG, NOW)
    assert s.available and not raw.unlock_data_missing
    assert s.score > 0.0
    assert s.raw_value > 0.0  # proximity-weighted pressure


def test_events_outside_horizon_ignored():
    raw = make_raw(circ=CIRC, unlock_missing=False, events=[event(9999, 0.5 * CIRC)])
    s = unlock_subsignal(raw, CFG, NOW)
    assert s.score == 0.0  # event beyond horizon does not contribute


def test_hard_flag_on_large_near_unlock():
    raw = make_raw(circ=CIRC, unlock_missing=False, events=[event(2, 0.15 * CIRC)])
    s = unlock_subsignal(raw, CFG, NOW)
    assert s.hard_flag is True
    assert s.score >= CFG.unlock_hard_score


def test_missing_schedule_uses_overhang_fallback():
    # No schedule, but FDV is 5x market cap => overhang 4x.
    raw = make_raw(unlock_missing=True, market_cap=1e9, fdv=5e9)
    s = unlock_subsignal(raw, CFG, NOW)
    assert s.available is True
    assert s.score > 0.0
    assert "overhang" in s.reason.lower()
    assert abs(s.raw_value - 4.0) < 1e-9


def test_no_schedule_no_supply_is_unavailable():
    raw = make_raw(unlock_missing=True)
    s = unlock_subsignal(raw, CFG, NOW)
    assert s.available is False
    assert s.score == 0.0
