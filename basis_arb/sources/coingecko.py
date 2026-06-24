"""CoinGecko client: market cap, circulating/total supply, FDV.

One bulk `coins/markets` request (top market caps), mapped by uppercase
symbol. On symbol collisions the highest-market-cap row wins. Keyless;
rate-limited, so we cache aggressively (default 6h).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from ..config import BasisArbConfig
from ..models import DataIssue, SourceRunMetadata
from .base import fetch_cached_json, safe_float, safe_positive
from .cache import JsonCache

MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"


@dataclass(frozen=True, slots=True)
class CoinGeckoMarket:
    gecko_id: str
    symbol: str
    name: str
    current_price: Optional[float]
    market_cap_usd: Optional[float]
    circulating_supply: Optional[float]
    total_supply: Optional[float]
    fully_diluted_valuation_usd: Optional[float]
    total_volume_usd: Optional[float]


@dataclass(slots=True)
class CoinGeckoSnapshot:
    by_coin: dict[str, CoinGeckoMarket]
    issues: list[DataIssue]


class CoinGeckoClient:
    def __init__(self, cfg: BasisArbConfig, cache: JsonCache) -> None:
        self.cfg = cfg
        self.cache = cache
        self.session = requests.Session()

    def fetch_markets(self, pages: int = 1, per_page: int = 250) -> tuple[CoinGeckoSnapshot, SourceRunMetadata]:
        rows: list[dict] = []
        meta_final: Optional[SourceRunMetadata] = None
        for page in range(1, max(1, pages) + 1):
            params = {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": per_page,
                "page": page,
                "price_change_percentage": "",
            }
            data, meta = fetch_cached_json(
                source="coingecko",
                url=MARKETS_URL,
                cache=self.cache,
                cache_key=f"coingecko:markets:{per_page}:{page}",
                ttl_seconds=self.cfg.coingecko_cache_ttl_seconds,
                params=params,
                timeout=self.cfg.request_timeout_seconds,
                retries=self.cfg.request_retries,
                backoff_seconds=self.cfg.request_backoff_seconds,
                session=self.session,
            )
            meta_final = meta
            if isinstance(data, list):
                rows.extend(r for r in data if isinstance(r, dict))
            else:
                break  # stop paging on first failure
        snap = self._build(rows)
        ok = bool(rows)
        if meta_final is None:
            meta_final = SourceRunMetadata("coingecko", ok=ok)
        return snap, meta_final

    def _build(self, rows: list[dict]) -> CoinGeckoSnapshot:
        by_coin: dict[str, CoinGeckoMarket] = {}
        issues: list[DataIssue] = []
        for r in rows:
            symbol = str(r.get("symbol", "")).upper()
            if not symbol:
                continue
            mkt = CoinGeckoMarket(
                gecko_id=str(r.get("id", "")),
                symbol=symbol,
                name=str(r.get("name", "")),
                current_price=safe_positive(r.get("current_price")),
                market_cap_usd=safe_positive(r.get("market_cap")),
                circulating_supply=safe_positive(r.get("circulating_supply")),
                total_supply=safe_positive(r.get("total_supply")),
                fully_diluted_valuation_usd=safe_positive(r.get("fully_diluted_valuation")),
                total_volume_usd=safe_float(r.get("total_volume")),
            )
            existing = by_coin.get(symbol)
            if existing is None:
                by_coin[symbol] = mkt
            else:
                # Collision: keep the higher market cap; warn.
                cur = existing.market_cap_usd or 0.0
                new = mkt.market_cap_usd or 0.0
                if new > cur:
                    by_coin[symbol] = mkt
                issues.append(DataIssue(
                    "coingecko", "warning", "symbol_collision",
                    f"symbol {symbol} maps to multiple ids; kept highest market cap",
                ))
        return CoinGeckoSnapshot(by_coin=by_coin, issues=issues)
