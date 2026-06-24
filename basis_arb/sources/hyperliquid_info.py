"""Hyperliquid data via the read-only Info API ONLY.

GUARDRAIL: this module imports exactly `Info` and `MAINNET_API_URL`. It must
never import `hyperliquid.exchange`, signing utilities, `eth_account`, or any
wallet/private-key code. It only reads public market context.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL

from ..config import BasisArbConfig
from ..models import SourceRunMetadata, VenueFunding, VenueMarket, utcnow
from ..normalization import canonicalize
from .base import safe_float, safe_positive
from .cache import JsonCache

VENUE = "hyperliquid"


@dataclass(slots=True)
class HyperliquidSnapshot:
    perp_markets: dict[str, VenueMarket] = field(default_factory=dict)
    perp_funding: dict[str, VenueFunding] = field(default_factory=dict)
    spot: dict[str, dict[str, Optional[float]]] = field(default_factory=dict)

    @property
    def coins(self) -> list[str]:
        return list(self.perp_markets.keys())


class HyperliquidInfoClient:
    def __init__(self, cfg: BasisArbConfig, cache: JsonCache, base_url: str = MAINNET_API_URL) -> None:
        self.cfg = cfg
        self.cache = cache
        self.base_url = base_url

    def fetch_contexts(self) -> tuple[HyperliquidSnapshot, SourceRunMetadata]:
        now = utcnow()
        cache_key = "hyperliquid:contexts"
        cached = self.cache.read(cache_key, self.cfg.hyperliquid_cache_ttl_seconds)
        if cached.fresh:
            return self._parse(cached.data), SourceRunMetadata(VENUE, ok=True, used_cache=True, fetched_at=now)

        try:
            info = Info(self.base_url, skip_ws=True, timeout=self.cfg.request_timeout_seconds)
            perp_meta, perp_ctxs = info.meta_and_asset_ctxs()
            spot_meta, spot_ctxs = info.spot_meta_and_asset_ctxs()
            raw: dict[str, Any] = {"perp": [perp_meta, perp_ctxs], "spot": [spot_meta, spot_ctxs]}
            self.cache.write(cache_key, raw)
            return self._parse(raw), SourceRunMetadata(VENUE, ok=True, fetched_at=now)
        except Exception as exc:  # network/SDK errors must not abort the run
            err = f"{type(exc).__name__}: {exc}"
            if cached.hit:
                return self._parse(cached.data), SourceRunMetadata(
                    VENUE, ok=True, used_cache=True, stale=True, error=err, fetched_at=now
                )
            return HyperliquidSnapshot(), SourceRunMetadata(VENUE, ok=False, error=err, fetched_at=now)

    def _parse(self, raw: Any) -> HyperliquidSnapshot:
        snap = HyperliquidSnapshot()
        if not isinstance(raw, dict):
            return snap
        ppd, dpy = self.cfg.funding_periods_per_day, self.cfg.days_per_year

        perp = raw.get("perp")
        if isinstance(perp, list) and len(perp) == 2 and isinstance(perp[0], dict):
            universe = perp[0].get("universe") or []
            ctxs = perp[1] or []
            for asset, ctx in zip(universe, ctxs):
                if not isinstance(asset, dict) or not isinstance(ctx, dict):
                    continue
                coin = canonicalize(str(asset.get("name", "")), self.cfg)
                if not coin:
                    continue
                mark = safe_positive(ctx.get("markPx"))
                oi_coins = safe_float(ctx.get("openInterest"))
                oi_usd = oi_coins * mark if (oi_coins is not None and mark is not None) else None
                snap.perp_markets[coin] = VenueMarket(
                    venue=VENUE,
                    source_symbol=str(asset.get("name", "")),
                    perp_mark_price=mark,
                    perp_index_price=safe_positive(ctx.get("oraclePx")),
                    perp_open_interest_coins=oi_coins,
                    perp_open_interest_usd=oi_usd,
                    perp_premium=safe_float(ctx.get("premium")),
                    perp_daily_volume_usd=safe_float(ctx.get("dayNtlVlm")),
                )
                hourly = safe_float(ctx.get("funding"))
                if hourly is not None:
                    f8 = hourly * 8.0
                    snap.perp_funding[coin] = VenueFunding(
                        venue=VENUE,
                        source_symbol=str(asset.get("name", "")),
                        funding_8h_decimal=f8,
                        funding_apr=f8 * ppd * dpy,
                        interval_hours=1.0,
                        context_only=True,
                    )

        spot = raw.get("spot")
        if isinstance(spot, list) and len(spot) == 2:
            spot_ctxs = spot[1] or []
            for ctx in spot_ctxs:
                if not isinstance(ctx, dict):
                    continue
                coin = canonicalize(str(ctx.get("coin", "")), self.cfg)
                if not coin:
                    continue
                mark = safe_positive(ctx.get("markPx")) or safe_positive(ctx.get("midPx"))
                circ = safe_positive(ctx.get("circulatingSupply"))
                total = safe_positive(ctx.get("totalSupply"))
                mcap = circ * mark if (circ is not None and mark is not None) else None
                snap.spot[coin] = {
                    "spot_price": safe_positive(ctx.get("midPx")) or mark,
                    "circulating_supply": circ,
                    "total_supply": total,
                    "market_cap": mcap,
                    "spot_daily_volume_usd": safe_float(ctx.get("dayNtlVlm")),
                }
        return snap
