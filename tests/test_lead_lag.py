from _helpers import lead_lag_series, make_raw, rbars

from basis_arb.config import BasisArbConfig
from basis_arb.signals.trap import spot_leads_subsignal

CFG = BasisArbConfig()


def test_spot_leads_up_yields_positive_score():
    spot, perp = lead_lag_series(90, drift=0.005, lag=2)
    raw = make_raw(spot_returns=spot, perp_returns=perp)
    s = spot_leads_subsignal(raw, CFG)
    assert s.available is True
    assert s.score > 0.0


def test_spot_leads_but_not_up_scores_zero():
    # Same lead structure, but the move is DOWN -> not the squeeze signature.
    spot, perp = lead_lag_series(90, drift=-0.005, lag=2)
    raw = make_raw(spot_returns=spot, perp_returns=perp)
    s = spot_leads_subsignal(raw, CFG)
    assert s.available is True
    assert s.score == 0.0


def test_perp_leading_scores_zero():
    # Swap roles: now perp leads spot.
    spot, perp = lead_lag_series(90, drift=0.005, lag=2)
    raw = make_raw(spot_returns=perp, perp_returns=spot)
    s = spot_leads_subsignal(raw, CFG)
    assert s.score == 0.0


def test_insufficient_bars_unavailable():
    raw = make_raw(spot_returns=rbars([0.01] * 10), perp_returns=rbars([0.01] * 10))
    s = spot_leads_subsignal(raw, CFG)
    assert s.available is False
    assert s.score == 0.0
