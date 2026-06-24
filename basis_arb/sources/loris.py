"""Loris Tools client: cross-venue funding rates + OI rankings.

GET https://api.loris.tools/funding with header `X-Api-Key: $LORIS_API_KEY`.
Funding values are 8h-normalized basis points -> divide by 10000 for the raw
8h decimal. Missing key is a first-class NON-FATAL state: no HTTP call is made
and carry is later marked unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..config import BasisArbConfig
from ..models import SourceRunMetadata, VenueFunding, utcnow
from ..normalization import canonicalize
from .base import http_get_json, safe_float
from .cache import JsonCache

LORIS_URL = "https://api.loris.tools/funding"


@dataclass(slots=True)
class LorisSnapshot:
    funding_by_venue: dict[str, dict[str, VenueFunding]] = field(default_factory=dict)
    oi_rankings: dict[str, tuple[Optional[int], str]] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)
    default_oi_rank: Optional[str] = None
    timestamp: Optional[datetime] = None
    unavailable_reason: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None and bool(self.funding_by_venue)


def _parse_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_rank(value: object) -> tuple[Optional[int], str]:
    raw = str(value)
    digits = ""
    for ch in raw:
        if ch.isdigit():
            digits += ch
        else:
            break
    return (int(digits) if digits else None), raw


class LorisClient:
    def __init__(self, api_key: Optional[str], cfg: BasisArbConfig, cache: JsonCache) -> None:
        self.api_key = (api_key or "").strip()
        self.cfg = cfg
        self.cache = cache

    def fetch(self) -> tuple[LorisSnapshot, SourceRunMetadata]:
        now = utcnow()
        if not self.api_key:
            snap = LorisSnapshot(unavailable_reason="missing LORIS_API_KEY")
            return snap, SourceRunMetadata("loris", ok=False, error="missing LORIS_API_KEY", fetched_at=now)

        cache_key = "loris:funding"
        cached = self.cache.read(cache_key, self.cfg.loris_cache_ttl_seconds)
        if cached.fresh:
            return self._parse(cached.data), SourceRunMetadata(
                "loris", ok=True, used_cache=True, fetched_at=now
            )

        out = http_get_json(
            LORIS_URL,
            headers={"X-Api-Key": self.api_key},
            timeout=self.cfg.request_timeout_seconds,
            retries=self.cfg.request_retries,
            backoff_seconds=self.cfg.request_backoff_seconds,
        )
        if out.ok:
            self.cache.write(cache_key, out.data)
            return self._parse(out.data), SourceRunMetadata("loris", ok=True, fetched_at=now)

        # Distinguish auth states for a clear, non-fatal message.
        if out.status == 401:
            reason = "invalid/missing LORIS_API_KEY (HTTP 401)"
        elif out.status == 403:
            reason = "key lacks tier access (HTTP 403)"
        else:
            reason = out.error or "loris request failed"

        if cached.hit:
            snap = self._parse(cached.data)
            return snap, SourceRunMetadata("loris", ok=True, used_cache=True, stale=True, error=reason, fetched_at=now)
        return LorisSnapshot(unavailable_reason=reason), SourceRunMetadata("loris", ok=False, error=reason, fetched_at=now)

    def _parse(self, data: object) -> LorisSnapshot:
        if not isinstance(data, dict):
            return LorisSnapshot(unavailable_reason="unexpected loris payload")
        snap = LorisSnapshot(
            timestamp=_parse_timestamp(data.get("timestamp")),
            default_oi_rank=str(data["default_oi_rank"]) if data.get("default_oi_rank") is not None else None,
        )
        ppd = self.cfg.funding_periods_per_day
        dpy = self.cfg.days_per_year
        intervals = data.get("funding_intervals") or {}
        funding_rates = data.get("funding_rates") or {}
        if isinstance(funding_rates, dict):
            for venue, by_symbol in funding_rates.items():
                if not isinstance(by_symbol, dict):
                    continue
                v = str(venue).lower()
                bucket: dict[str, VenueFunding] = {}
                for sym, bps in by_symbol.items():
                    bps_f = safe_float(bps)
                    if bps_f is None:
                        continue
                    coin = canonicalize(str(sym), self.cfg)
                    if not coin:
                        continue
                    f8 = bps_f / 10000.0
                    interval = None
                    venue_intervals = intervals.get(venue) if isinstance(intervals, dict) else None
                    if isinstance(venue_intervals, dict):
                        interval = safe_float(venue_intervals.get(sym))
                    bucket[coin] = VenueFunding(
                        venue=v,
                        source_symbol=str(sym),
                        funding_8h_decimal=f8,
                        funding_apr=f8 * ppd * dpy,
                        interval_hours=interval,
                        observed_at=snap.timestamp,
                    )
                if bucket:
                    snap.funding_by_venue[v] = bucket

        oi = data.get("oi_rankings") or {}
        if isinstance(oi, dict):
            for sym, rank in oi.items():
                coin = canonicalize(str(sym), self.cfg)
                if coin:
                    snap.oi_rankings[coin] = _parse_rank(rank)

        syms = data.get("symbols")
        if isinstance(syms, list):
            snap.symbols = [canonicalize(str(s), self.cfg) for s in syms]
        return snap
