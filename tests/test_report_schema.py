import json

from _helpers import make_raw, vfunding, vmarket

from basis_arb.config import BasisArbConfig
from basis_arb.models import RunReport, SourceRunMetadata, utcnow
from basis_arb.report import build_json_report, render_table, write_json_report
from basis_arb.signals.carry import estimate_carry
from basis_arb.signals.ranking import build_signal, rank_signals
from basis_arb.signals.trap import compute_trap

CFG = BasisArbConfig()


def _report():
    raw = make_raw(coin="BTC",
                   funding=[vfunding("binance", 0.0001), vfunding("bybit", 0.00012)],
                   markets=[vmarket("binance", mark=100, index=100, spot=99.8, oi_usd=2e9, spot_vol=5e8),
                            vmarket("bybit", mark=100.1, spot=99.9, oi_usd=1e9, spot_vol=3e8)],
                   market_cap=1e12, circ=1.9e7, total=2.1e7, unlock_missing=False, events=[])
    carry = estimate_carry(raw, CFG)
    trap = compute_trap(raw, CFG)
    sig = build_signal(raw, carry, trap, CFG)
    ranked = rank_signals([sig])
    return RunReport(
        generated_at=utcnow(), config_snapshot=CFG.snapshot(),
        key_present={"LORIS_API_KEY": False},
        sources={"loris": SourceRunMetadata("loris", ok=False, error="missing LORIS_API_KEY")},
        signals=ranked,
    )


def test_json_report_has_required_keys_and_serializes():
    payload = build_json_report(_report())
    for key in ("tool", "schema_version", "generated_at", "key_present", "config", "sources", "signals"):
        assert key in payload
    sig = payload["signals"][0]
    assert sig["coin"] == "BTC"
    assert set(sig["trap"]["subsignals"]) == {
        "upcoming_unlocks", "spot_illiquidity_to_perp_oi",
        "spot_leading_perp", "oi_market_cap_distortion",
    }
    # Round-trips through JSON without error.
    text = json.dumps(payload)
    assert json.loads(text)["schema_version"] == 1


def test_table_renders_with_header_and_no_throw():
    table = render_table(_report(), CFG)
    assert "Rank" in table and "TopReason" in table and "Trap" in table
    assert "BTC" in table


def test_write_json_report_round_trip(tmp_path):
    path = str(tmp_path / "out.json")
    write_json_report(_report(), path)
    with open(path) as fh:
        loaded = json.load(fh)
    assert loaded["tool"] == "basis_arb"
    assert loaded["signals"][0]["coin"] == "BTC"
