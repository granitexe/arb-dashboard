"""Tiny JSON file cache with TTL and stale-on-error reads.

Used to honor polite poll intervals (e.g. Loris <=1/60s) and to survive
transient network failures by falling back to the last good payload.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True, slots=True)
class CacheRead:
    data: Any | None
    age_seconds: Optional[float]
    fresh: bool

    @property
    def hit(self) -> bool:
        return self.data is not None


class JsonCache:
    def __init__(self, cache_dir: str, enabled: bool = True) -> None:
        self.cache_dir = cache_dir
        self.enabled = enabled

    def _path(self, key: str) -> str:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        return os.path.join(self.cache_dir, f"{digest}.json")

    def read(self, key: str, ttl_seconds: float) -> CacheRead:
        """Return cached payload with freshness flag (fresh = within TTL)."""
        if not self.enabled:
            return CacheRead(None, None, False)
        path = self._path(key)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            cached_at = float(blob["_cached_at"])
            age = time.time() - cached_at
            return CacheRead(blob["data"], age, age <= ttl_seconds)
        except (OSError, ValueError, KeyError):
            return CacheRead(None, None, False)

    def write(self, key: str, data: Any) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"_cached_at": time.time(), "data": data}, fh)
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError):
            # Cache is best-effort; never fail the run over it.
            pass
