"""Keyless public-exchange REST clients: Binance, Bybit, OKX.

Used for spot+perp prices (basis), notional open interest, spot 24h volume,
and hourly klines for the spot-leading-perp lead/lag signal. All endpoints are
public and require no key. Funding from these venues is NOT used (Loris is the
sole ranked funding source).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

import requests

from ..config import BasisArbConfig
from ..models import ReturnBar, SourceRunMetadata, VenueMarket, bars_from_closes, utcnow
from ..normalization import (
    binance_perp_symbol,
    binance_spot_symbol,
    bybit_symbol,
    okx_spot_inst,
    okx_swap_inst,
)
from .base import fetch_cached_json, safe_float, safe_positive
from .cache import JsonCache

class _ExchangeBase:
    venue = "exchange"

    def __init__(self, cfg: BasisArbConfig, cache: JsonCache) -> None:
        self.cfg = cfg
        self.cache = cache
        self.session = requests.Session()

    def _get(self, url: str, params: Optional[dict], ttl: int, key: str):
        return fetch_cached_json(
            source=self.venue,
            url=url,
            cache=self.cache,
            cache_key=key,
            ttl_seconds=ttl,
            params=params,
            timeout=self.cfg.request_timeout_seconds,
            retries=self.cfg.request_retries,
            backoff_seconds=self.cfg.request_backoff_seconds,
            session=self.session,
        )

    def _bar_limit(self) -> int:
        return min(self.cfg.lead_lag_lookback_days * 24 + 2, 1000)


class BinanceClient(_ExchangeBase):
    venue = "binance"
    FAPI = "https://fapi.binance.com"
    SAPI = "https://api.binance.com"

    def fetch_market(self, coin: str) -> tuple[Optional[VenueMarket], SourceRunMetadata]:
        now = utcnow()
        perp = binance_perp_symbol(coin)
        spot = binance_spot_symbol(coin)
        ttl = self.cfg.exchange_cache_ttl_seconds
        prem, _ = self._get(f"{self.FAPI}/fapi/v1/premiumIndex", {"symbol": perp}, ttl, f"binance:prem:{perp}")
        oi, _ = self._get(f"{self.FAPI}/fapi/v1/openInterest", {"symbol": perp}, ttl, f"binance:oi:{perp}")
        spot24, _ = self._get(f"{self.SAPI}/api/v3/ticker/24hr", {"symbol": spot}, ttl, f"binance:spot24:{spot}")

        if not isinstance(prem, dict) and not isinstance(spot24, dict):
            return None, SourceRunMetadata(self.venue, ok=False, error="no binance data", fetched_at=now)

        mark = safe_positive(prem.get("markPrice")) if isinstance(prem, dict) else None
        index = safe_positive(prem.get("indexPrice")) if isinstance(prem, dict) else None
        oi_coins = safe_float(oi.get("openInterest")) if isinstance(oi, dict) else None
        oi_usd = oi_coins * mark if (oi_coins is not None and mark is not None) else None
        spot_px = safe_positive(spot24.get("lastPrice")) if isinstance(spot24, dict) else None
        spot_vol = safe_float(spot24.get("quoteVolume")) if isinstance(spot24, dict) else None
        premium = (mark - index) / index if (mark is not None and index) else None

        mkt = VenueMarket(
            venue=self.venue, source_symbol=perp,
            perp_mark_price=mark, perp_index_price=index,
            perp_open_interest_coins=oi_coins, perp_open_interest_usd=oi_usd,
            perp_premium=premium, spot_price=spot_px, spot_daily_volume_usd=spot_vol,
            observed_at=now,
        )
        return mkt, SourceRunMetadata(self.venue, ok=True, fetched_at=now)

    def fetch_return_bars(self, coin: str, market: Market) -> tuple[list[ReturnBar], SourceRunMetadata]:
        now = utcnow()
        interval = self.cfg.lead_lag_bar_interval
        limit = self._bar_limit()
        if market == "spot":
            url = f"{self.SAPI}/api/v3/klines"
            sym = binance_spot_symbol(coin)
        else:
            url = f"{self.FAPI}/fapi/v1/klines"
            sym = binance_perp_symbol(coin)
        data, meta = self._get(url, {"symbol": sym, "interval": interval, "limit": limit},
                               self.cfg.exchange_cache_ttl_seconds, f"binance:kl:{market}:{sym}:{interval}:{limit}")
        bars = _binance_klines_to_bars(data)
        return bars, meta


class BybitClient(_ExchangeBase):
    venue = "bybit"
    BASE = "https://api.bybit.com"

    def fetch_market(self, coin: str) -> tuple[Optional[VenueMarket], SourceRunMetadata]:
        now = utcnow()
        sym = bybit_symbol(coin)
        ttl = self.cfg.exchange_cache_ttl_seconds
        lin, _ = self._get(f"{self.BASE}/v5/market/tickers", {"category": "linear", "symbol": sym}, ttl, f"bybit:lin:{sym}")
        spt, _ = self._get(f"{self.BASE}/v5/market/tickers", {"category": "spot", "symbol": sym}, ttl, f"bybit:spot:{sym}")
        lin_row = _bybit_row(lin)
        spot_row = _bybit_row(spt)
        if lin_row is None and spot_row is None:
            return None, SourceRunMetadata(self.venue, ok=False, error="no bybit data", fetched_at=now)

        mark = safe_positive(lin_row.get("markPrice")) if lin_row else None
        index = safe_positive(lin_row.get("indexPrice")) if lin_row else None
        oi_coins = safe_float(lin_row.get("openInterest")) if lin_row else None
        oi_usd = safe_float(lin_row.get("openInterestValue")) if lin_row else None
        if oi_usd is None and oi_coins is not None and mark is not None:
            oi_usd = oi_coins * mark
        spot_px = safe_positive(spot_row.get("lastPrice")) if spot_row else None
        spot_vol = safe_float(spot_row.get("turnover24h")) if spot_row else None
        premium = (mark - index) / index if (mark is not None and index) else None

        mkt = VenueMarket(
            venue=self.venue, source_symbol=sym,
            perp_mark_price=mark, perp_index_price=index,
            perp_open_interest_coins=oi_coins, perp_open_interest_usd=oi_usd,
            perp_premium=premium, spot_price=spot_px, spot_daily_volume_usd=spot_vol,
            observed_at=now,
        )
        return mkt, SourceRunMetadata(self.venue, ok=True, fetched_at=now)

    def fetch_return_bars(self, coin: str, market: Market) -> tuple[list[ReturnBar], SourceRunMetadata]:
        sym = bybit_symbol(coin)
        category = "spot" if market == "spot" else "linear"
        interval = _bybit_interval(self.cfg.lead_lag_bar_interval)
        limit = self._bar_limit()
        data, meta = self._get(f"{self.BASE}/v5/market/kline",
                               {"category": category, "symbol": sym, "interval": interval, "limit": limit},
                               self.cfg.exchange_cache_ttl_seconds, f"bybit:kl:{category}:{sym}:{interval}:{limit}")
        return _bybit_klines_to_bars(data), meta


class OkxClient(_ExchangeBase):
    venue = "okx"
    BASE = "https://www.okx.com"

    def fetch_market(self, coin: str) -> tuple[Optional[VenueMarket], SourceRunMetadata]:
        now = utcnow()
        swap = okx_swap_inst(coin)
        spot = okx_spot_inst(coin)
        ttl = self.cfg.exchange_cache_ttl_seconds
        mark_r, _ = self._get(f"{self.BASE}/api/v5/public/mark-price", {"instType": "SWAP", "instId": swap}, ttl, f"okx:mark:{swap}")
        oi_r, _ = self._get(f"{self.BASE}/api/v5/public/open-interest", {"instType": "SWAP", "instId": swap}, ttl, f"okx:oi:{swap}")
        idx_r, _ = self._get(f"{self.BASE}/api/v5/market/index-tickers", {"instId": spot}, ttl, f"okx:idx:{spot}")
        spot_r, _ = self._get(f"{self.BASE}/api/v5/market/ticker", {"instId": spot}, ttl, f"okx:spot:{spot}")

        mark = safe_positive(_okx_first(mark_r, "markPx"))
        oi_usd = safe_float(_okx_first(oi_r, "oiUsd"))
        oi_coins = safe_float(_okx_first(oi_r, "oiCcy"))
        index = safe_positive(_okx_first(idx_r, "idxPx"))
        spot_px = safe_positive(_okx_first(spot_r, "last"))
        spot_vol = safe_float(_okx_first(spot_r, "volCcy24h"))
        if mark is None and spot_px is None:
            return None, SourceRunMetadata(self.venue, ok=False, error="no okx data", fetched_at=now)
        premium = (mark - index) / index if (mark is not None and index) else None
        mkt = VenueMarket(
            venue=self.venue, source_symbol=swap,
            perp_mark_price=mark, perp_index_price=index,
            perp_open_interest_coins=oi_coins, perp_open_interest_usd=oi_usd,
            perp_premium=premium, spot_price=spot_px, spot_daily_volume_usd=spot_vol,
            observed_at=now,
        )
        return mkt, SourceRunMetadata(self.venue, ok=True, fetched_at=now)


# --- parsing helpers ----------------------------------------------------------

def _bybit_row(data: object) -> Optional[dict]:
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict):
            rows = result.get("list")
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                return rows[0]
    return None


def _okx_first(data: object, key: str):
    if isinstance(data, dict):
        rows = data.get("data")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0].get(key)
    return None


def _bybit_interval(interval: str) -> str:
    table = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D"}
    return table.get(interval, "60")


def _binance_klines_to_bars(data: object) -> list[ReturnBar]:
    if not isinstance(data, list):
        return []
    closes: list[float] = []
    times: list[datetime] = []
    for row in data:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        close = safe_positive(row[4])
        if close is None:
            continue
        times.append(datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc))
        closes.append(close)
    return bars_from_closes(times, closes)


def _bybit_klines_to_bars(data: object) -> list[ReturnBar]:
    rows = None
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict):
            rows = result.get("list")
    if not isinstance(rows, list):
        return []
    # Bybit returns newest-first; reverse to chronological.
    parsed: list[tuple[datetime, float]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        close = safe_positive(row[4])
        if close is None:
            continue
        parsed.append((datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc), close))
    parsed.sort(key=lambda p: p[0])
    return bars_from_closes([t for t, _ in parsed], [c for _, c in parsed])
