import json
import os

from basis_arb.config import BasisArbConfig
from basis_arb.sources.cache import JsonCache
from basis_arb.sources.loris import LorisClient, _parse_rank

CFG = BasisArbConfig()
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "loris_funding.json")


def _client(key="lk_live_test"):
    return LorisClient(key, CFG, JsonCache("/tmp/nope", enabled=False))


def test_parse_funding_and_apr():
    data = json.load(open(FIXTURE))
    snap = _client()._parse(data)
    # 10 bps 8h => 0.001 decimal => APR 1.095
    btc = snap.funding_by_venue["binance"]["BTC"]
    assert abs(btc.funding_8h_decimal - 0.001) < 1e-12
    assert abs(btc.funding_apr - 1.095) < 1e-9
    assert btc.interval_hours == 8
    # negative funding preserved
    assert snap.funding_by_venue["binance"]["ETH"].funding_8h_decimal < 0


def test_parse_oi_rankings_and_timestamp():
    data = json.load(open(FIXTURE))
    snap = _client()._parse(data)
    assert snap.oi_rankings["BTC"][0] == 1
    assert snap.oi_rankings["XYZ"] == (500, "500+")  # numeric prefix parsed for ordering
    assert snap.timestamp is not None and snap.timestamp.year == 2026


def test_parse_rank_helper():
    assert _parse_rank("12") == (12, "12")
    assert _parse_rank("500+") == (500, "500+")
    assert _parse_rank(3) == (3, "3")


def test_missing_key_is_nonfatal_no_http():
    # api_key None must NOT make a network call and must degrade gracefully.
    client = LorisClient(None, CFG, JsonCache("/tmp/nope", enabled=False))
    snap, meta = client.fetch()
    assert meta.ok is False
    assert snap.available is False
    assert snap.unavailable_reason == "missing LORIS_API_KEY"
