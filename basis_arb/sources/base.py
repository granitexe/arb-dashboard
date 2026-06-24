"""Shared source helpers: a resilient HTTP-GET-JSON wrapper and numeric parsing.

No source here imports any trading/signing code. Network failures never raise
out of these helpers; they return a typed outcome the pipeline can degrade on.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

_DEFAULT_HEADERS = {
    "User-Agent": "basis-arb-tool/0.1 (read-only signal scanner)",
    "Accept": "application/json",
}
# HTTP statuses that are pointless to retry.
_NO_RETRY_STATUS = {400, 401, 403, 404, 422}


@dataclass(frozen=True, slots=True)
class HttpOutcome:
    data: Any | None
    status: Optional[int]
    error: Optional[str]

    @property
    def ok(self) -> bool:
        return self.error is None and self.data is not None


def http_get_json(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    timeout: float = 12.0,
    retries: int = 2,
    backoff_seconds: float = 0.5,
    session: Optional[requests.Session] = None,
) -> HttpOutcome:
    merged = dict(_DEFAULT_HEADERS)
    if headers:
        merged.update(headers)
    get = (session.get if session is not None else requests.get)

    last_err: Optional[str] = None
    last_status: Optional[int] = None
    attempts = max(1, retries + 1)
    for attempt in range(attempts):
        try:
            resp = get(url, headers=merged, params=params, timeout=timeout)
            last_status = resp.status_code
            if resp.status_code == 200:
                try:
                    return HttpOutcome(resp.json(), 200, None)
                except ValueError as exc:
                    return HttpOutcome(None, 200, f"invalid JSON: {exc}")
            # Non-200.
            body = resp.text[:200].replace("\n", " ")
            last_err = f"HTTP {resp.status_code}: {body}"
            if resp.status_code in _NO_RETRY_STATUS:
                return HttpOutcome(None, resp.status_code, last_err)
            # else fall through to retry (429 / 5xx / other)
        except requests.RequestException as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < attempts - 1:
            time.sleep(backoff_seconds * (2 ** attempt))
    return HttpOutcome(None, last_status, last_err or "request failed")


def safe_float(value: Any) -> Optional[float]:
    """Parse a number from str/int/float; reject None/empty/NaN/inf."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def safe_positive(value: Any) -> Optional[float]:
    f = safe_float(value)
    if f is None or f <= 0.0:
        return None
    return f


def fetch_cached_json(
    *,
    source: str,
    url: str,
    cache: "Any",
    cache_key: str,
    ttl_seconds: float,
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    timeout: float = 12.0,
    retries: int = 2,
    backoff_seconds: float = 0.5,
    session: Optional[requests.Session] = None,
):
    """Cache-first GET with stale-on-error fallback.

    Returns ``(data | None, SourceRunMetadata)``. Fresh cache short-circuits the
    network; on network failure the last good payload is returned with
    ``stale=True``; only a failure with no cache yields ``ok=False``.
    """
    from ..models import SourceRunMetadata, utcnow  # local import avoids cycle

    now = utcnow()
    cached = cache.read(cache_key, ttl_seconds)
    if cached.fresh:
        return cached.data, SourceRunMetadata(source, ok=True, used_cache=True, fetched_at=now)
    out = http_get_json(
        url, headers=headers, params=params, timeout=timeout,
        retries=retries, backoff_seconds=backoff_seconds, session=session,
    )
    if out.ok:
        cache.write(cache_key, out.data)
        return out.data, SourceRunMetadata(source, ok=True, fetched_at=now)
    if cached.hit:
        return cached.data, SourceRunMetadata(
            source, ok=True, used_cache=True, stale=True, error=out.error, fetched_at=now
        )
    return None, SourceRunMetadata(source, ok=False, error=out.error, fetched_at=now)
