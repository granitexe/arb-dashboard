from basis_arb.config import BasisArbConfig
from basis_arb.normalization import canonicalize, filter_universe, is_excluded
from basis_arb.sources.coingecko import CoinGeckoClient
from basis_arb.sources.cache import JsonCache

CFG = BasisArbConfig()


def test_canonicalize_variants():
    assert canonicalize("BTCUSDT", CFG) == "BTC"
    assert canonicalize("BTC-USDT-SWAP", CFG) == "BTC"
    assert canonicalize("ETH/USDC", CFG) == "ETH"
    assert canonicalize("eth-perp", CFG) == "ETH"
    assert canonicalize("SOL", CFG) == "SOL"


def test_overrides_applied():
    assert canonicalize("WETH", CFG) == "ETH"
    assert canonicalize("WBTC", CFG) == "BTC"


def test_stablecoins_excluded():
    assert is_excluded("USDT", CFG)
    assert is_excluded("usdc", CFG)
    assert not is_excluded("BTC", CFG)


def test_filter_universe_dedupes_and_drops_excluded():
    out = filter_universe(["BTC", "btc", "USDT", "ETH", ""], CFG)
    assert out == ["BTC", "ETH"]


def test_coingecko_collision_keeps_highest_market_cap():
    rows = [
        {"id": "low-cap", "symbol": "sun", "name": "LowSun", "market_cap": 1_000_000},
        {"id": "big-cap", "symbol": "sun", "name": "BigSun", "market_cap": 900_000_000},
    ]
    snap = CoinGeckoClient(CFG, JsonCache("/tmp/nope", enabled=False))._build(rows)
    assert snap.by_coin["SUN"].gecko_id == "big-cap"
    assert any(i.code == "symbol_collision" for i in snap.issues)
