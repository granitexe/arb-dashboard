"""Symbol canonicalization and per-venue symbol builders.

Joins coins across sources whose tickers differ (Loris "BTC", Binance
"BTCUSDT", OKX "BTC-USDT-SWAP", CoinGecko id "bitcoin"). The canonical key
is the uppercase base asset, e.g. "BTC".
"""
from __future__ import annotations

import re
from typing import Iterable

from .config import BasisArbConfig

_SEP_RE = re.compile(r"[-_/ ]+")
_DERIV_MARKERS = {"PERP", "SWAP", "PERPETUAL", "USDM", "COINM", "LINEAR", "INVERSE", "FUTURES"}


def canonicalize(symbol: str, cfg: BasisArbConfig) -> str:
    """Return the canonical uppercase base symbol for any source ticker."""
    if not symbol:
        return ""
    overrides = {k.upper(): v.upper() for k, v in cfg.symbol_overrides.items()}
    quotes = tuple(sorted((q.upper() for q in cfg.quote_assets), key=len, reverse=True))

    s = symbol.strip().upper()
    if s in overrides:
        return overrides[s]

    parts = [p for p in _SEP_RE.split(s) if p]
    parts = [p for p in parts if p not in _DERIV_MARKERS] or [s]

    if len(parts) >= 2:
        base = parts[0]
    else:
        base = parts[0]
        for q in quotes:
            if base.endswith(q) and len(base) > len(q):
                base = base[: -len(q)]
                break

    return overrides.get(base, base)


def is_excluded(coin: str, cfg: BasisArbConfig) -> bool:
    return coin.upper() in {c.upper() for c in cfg.excluded_coins}


def filter_universe(coins: Iterable[str], cfg: BasisArbConfig) -> list[str]:
    """De-dupe (preserving order) and drop excluded/empty coins."""
    seen: set[str] = set()
    out: list[str] = []
    for c in coins:
        c = (c or "").upper()
        if not c or c in seen or is_excluded(c, cfg):
            continue
        seen.add(c)
        out.append(c)
    return out


# --- per-venue symbol builders ------------------------------------------------

def binance_perp_symbol(coin: str) -> str:
    return f"{coin.upper()}USDT"


def binance_spot_symbol(coin: str) -> str:
    return f"{coin.upper()}USDT"


def bybit_symbol(coin: str) -> str:
    return f"{coin.upper()}USDT"


def okx_swap_inst(coin: str) -> str:
    return f"{coin.upper()}-USDT-SWAP"


def okx_spot_inst(coin: str) -> str:
    return f"{coin.upper()}-USDT"
