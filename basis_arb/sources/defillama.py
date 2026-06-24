"""DeFiLlama token-unlock client (keyless via the emissions datasets CDN).

The paid `api.llama.fi/emissions` endpoint is avoided; we use the free CDN:
  - emissionsProtocolsList         -> list of protocol slugs
  - emissions/{slug}               -> detail incl. metadata.events + gecko_id

Slug resolution is cheap: for each target coin we try its CoinGecko id and its
lowercased symbol as candidate slugs (not a 339-way scan), and only accept a
detail whose gecko_id matches the target. Future unlock events within the
horizon are returned as token amounts; the trap signal converts them to a
fraction of circulating supply.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

from ..config import BasisArbConfig
from ..models import DataIssue, SourceRunMetadata, UnlockEvent, utcnow
from .base import fetch_cached_json, safe_float
from .cache import JsonCache

LIST_URL = "https://defillama-datasets.llama.fi/emissionsProtocolsList"
DETAIL_URL = "https://defillama-datasets.llama.fi/emissions/{slug}"


@dataclass(slots=True)
class UnlockSnapshot:
    events_by_coin: dict[str, list[UnlockEvent]] = field(default_factory=dict)
    resolved_coins: set[str] = field(default_factory=set)
    issues: list[DataIssue] = field(default_factory=list)


class DefiLlamaClient:
    def __init__(self, cfg: BasisArbConfig, cache: JsonCache, polite_delay: float = 0.15) -> None:
        self.cfg = cfg
        self.cache = cache
        self.session = requests.Session()
        self.polite_delay = polite_delay

    def fetch_protocol_slugs(self) -> tuple[set[str], SourceRunMetadata]:
        data, meta = fetch_cached_json(
            source="defillama",
            url=LIST_URL,
            cache=self.cache,
            cache_key="defillama:protocols",
            ttl_seconds=self.cfg.defillama_cache_ttl_seconds,
            timeout=self.cfg.request_timeout_seconds,
            retries=self.cfg.request_retries,
            backoff_seconds=self.cfg.request_backoff_seconds,
            session=self.session,
        )
        slugs = {str(s).lower() for s in data} if isinstance(data, list) else set()
        return slugs, meta

    def _fetch_detail(self, slug: str) -> Optional[dict]:
        data, _ = fetch_cached_json(
            source="defillama",
            url=DETAIL_URL.format(slug=slug),
            cache=self.cache,
            cache_key=f"defillama:detail:{slug}",
            ttl_seconds=self.cfg.defillama_cache_ttl_seconds,
            timeout=self.cfg.request_timeout_seconds,
            retries=self.cfg.request_retries,
            backoff_seconds=self.cfg.request_backoff_seconds,
            session=self.session,
        )
        return data if isinstance(data, dict) else None

    def fetch_unlocks(self, targets: dict[str, Optional[str]]) -> tuple[UnlockSnapshot, SourceRunMetadata]:
        """targets: coin -> coingecko_id (or None). Returns unlock events per coin."""
        now = utcnow()
        snap = UnlockSnapshot()
        slugs, list_meta = self.fetch_protocol_slugs()
        if not slugs:
            return snap, SourceRunMetadata("defillama", ok=False, error=list_meta.error or "no protocol list", fetched_at=now)

        for coin, gecko_id in targets.items():
            candidates: list[str] = []
            if gecko_id and gecko_id.lower() in slugs:
                candidates.append(gecko_id.lower())
            lc = coin.lower()
            if lc in slugs and lc not in candidates:
                candidates.append(lc)
            for slug in candidates:
                if self.cache.read(f"defillama:detail:{slug}", self.cfg.defillama_cache_ttl_seconds).fresh is False:
                    time.sleep(self.polite_delay)
                detail = self._fetch_detail(slug)
                if not detail:
                    continue
                detail_gid = str(detail.get("gecko_id") or "").lower()
                # If we have a CoinGecko id, require it to match to avoid slug collisions.
                if gecko_id and detail_gid and detail_gid != gecko_id.lower():
                    continue
                events = self._parse_future_events(detail, slug)
                snap.events_by_coin[coin] = events
                snap.resolved_coins.add(coin)
                break

        return snap, SourceRunMetadata("defillama", ok=True, used_cache=list_meta.used_cache, fetched_at=now)

    def _parse_future_events(self, detail: dict, slug: str) -> list[UnlockEvent]:
        meta = detail.get("metadata")
        events_raw = meta.get("events") if isinstance(meta, dict) else None
        if not isinstance(events_raw, list):
            return []
        now_ts = datetime.now(timezone.utc).timestamp()
        horizon_ts = now_ts + self.cfg.unlock_horizon_days * 86400
        out: list[UnlockEvent] = []
        for ev in events_raw:
            if not isinstance(ev, dict):
                continue
            ts = safe_float(ev.get("timestamp"))
            if ts is None or ts <= now_ts or ts > horizon_ts:
                continue
            tokens = None
            no_of = ev.get("noOfTokens")
            if isinstance(no_of, list):
                nums = [safe_float(x) for x in no_of]
                nums = [n for n in nums if n is not None]
                if nums:
                    tokens = sum(nums)
            elif no_of is not None:
                tokens = safe_float(no_of)
            out.append(UnlockEvent(
                project_slug=slug,
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
                tokens=tokens,
                category=str(ev.get("category")) if ev.get("category") is not None else None,
                unlock_type=str(ev.get("unlockType")) if ev.get("unlockType") is not None else None,
            ))
        return out
