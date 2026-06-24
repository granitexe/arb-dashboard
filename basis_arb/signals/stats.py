"""Pure math helpers (stdlib only). No I/O, no global state."""
from __future__ import annotations

import math
from typing import Optional, Sequence


def clip01(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def linear_score(x: float, low: float, high: float) -> float:
    """Map [low, high] -> [0, 1] linearly, clipped outside."""
    if high == low:
        return 1.0 if x >= high else 0.0
    return clip01((x - low) / (high - low))


def log_score(x: float, low: float, high: float) -> float:
    """Log-space normalization for positive ratios. x <= 0 -> 0."""
    if x <= 0.0 or low <= 0.0 or high <= 0.0 or high == low:
        return 0.0 if x <= 0.0 else clip01((x - low) / (high - low))
    return clip01((math.log(x) - math.log(low)) / (math.log(high) - math.log(low)))


def median(values: Sequence[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> Optional[float]:
    """Lower weighted median. Ignores non-positive weights."""
    pairs = [(v, w) for v, w in zip(values, weights) if w is not None and w > 0 and v is not None]
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[0])
    total = math.fsum(w for _, w in pairs)
    if total <= 0:
        return None
    half = total / 2.0
    cum = 0.0
    for v, w in pairs:
        cum += w
        if cum >= half:
            return v
    return pairs[-1][0]


def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Pearson correlation. Returns None if undefined (n < 2 or zero variance)."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 2:
        return None
    mx = math.fsum(x for x, _ in pairs) / n
    my = math.fsum(y for _, y in pairs) / n
    sxy = math.fsum((x - mx) * (y - my) for x, y in pairs)
    sxx = math.fsum((x - mx) ** 2 for x, _ in pairs)
    syy = math.fsum((y - my) ** 2 for _, y in pairs)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    denom = math.sqrt(sxx * syy)
    if denom == 0.0:
        return None
    return sxy / denom


def log_returns(closes: Sequence[float]) -> list[Optional[float]]:
    """Element-aligned log returns; first element is None."""
    out: list[Optional[float]] = [None]
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev is not None and cur is not None and prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
        else:
            out.append(None)
    return out


def safe_div(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or den is None or den == 0:
        return None
    return num / den
