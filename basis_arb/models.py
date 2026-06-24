"""Data models for basis_arb.

Small immutable value records are frozen; the per-coin aggregate
(`CoinRawInput`) and final `CoinSignal` are mutable because the pipeline
assembles them incrementally as sources return.

`to_jsonable` recursively converts dataclasses / datetimes / floats into a
JSON-serializable structure (used by the JSON report).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

Coin = str  # canonical uppercase base symbol, e.g. "BTC"
Venue = str  # normalized lowercase venue, e.g. "binance", "hyperliquid"
SourceName = str

SignalStatus = Literal["OK", "CARRY_UNAVAILABLE", "EXCLUDED", "DATA_INSUFFICIENT"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class DataIssue:
    source: SourceName
    severity: Literal["info", "warning", "error"]
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class VenueFunding:
    venue: Venue
    source_symbol: str
    funding_8h_decimal: Optional[float]  # Loris bps / 10000
    funding_apr: Optional[float]  # funding_8h_decimal * 3 * 365
    interval_hours: Optional[float]
    observed_at: Optional[datetime] = None
    context_only: bool = False  # True for Hyperliquid-derived funding (never ranked)
    unavailable_reason: Optional[str] = None


@dataclass(frozen=True, slots=True)
class VenueMarket:
    venue: Venue
    source_symbol: str
    perp_mark_price: Optional[float] = None
    perp_index_price: Optional[float] = None
    perp_open_interest_coins: Optional[float] = None
    perp_open_interest_usd: Optional[float] = None
    perp_premium: Optional[float] = None
    perp_daily_volume_usd: Optional[float] = None
    spot_price: Optional[float] = None
    spot_daily_volume_usd: Optional[float] = None
    observed_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class ReturnBar:
    timestamp: datetime
    close: float
    log_return: Optional[float] = None  # None for first bar


def bars_from_closes(times: list[datetime], closes: list[float]) -> list["ReturnBar"]:
    """Build chronological ReturnBars with aligned log returns (first is None)."""
    bars: list[ReturnBar] = []
    prev: Optional[float] = None
    for ts, close in zip(times, closes):
        lr: Optional[float] = None
        if prev is not None and prev > 0 and close > 0:
            lr = math.log(close / prev)
        bars.append(ReturnBar(timestamp=ts, close=close, log_return=lr))
        prev = close
    return bars


@dataclass(frozen=True, slots=True)
class UnlockEvent:
    project_slug: str
    timestamp: datetime
    tokens: Optional[float] = None
    usd_value: Optional[float] = None
    pct_circulating_supply: Optional[float] = None
    category: Optional[str] = None
    unlock_type: Optional[str] = None
    source: str = "defillama"


@dataclass(slots=True)
class CoinRawInput:
    coin: Coin
    source_symbols: dict[SourceName, str] = field(default_factory=dict)
    funding_by_venue: dict[Venue, VenueFunding] = field(default_factory=dict)
    markets_by_venue: dict[Venue, VenueMarket] = field(default_factory=dict)
    hyperliquid_premium: Optional[float] = None
    hyperliquid_mark_price: Optional[float] = None
    hyperliquid_open_interest_usd: Optional[float] = None
    loris_oi_rank: Optional[int] = None
    loris_oi_rank_raw: Optional[str] = None
    coingecko_id: Optional[str] = None
    market_cap_usd: Optional[float] = None
    market_cap_source: Optional[str] = None
    circulating_supply: Optional[float] = None
    total_supply: Optional[float] = None
    fully_diluted_valuation_usd: Optional[float] = None
    spot_returns: list[ReturnBar] = field(default_factory=list)
    perp_returns: list[ReturnBar] = field(default_factory=list)
    lead_lag_venue: Optional[Venue] = None
    unlock_events: list[UnlockEvent] = field(default_factory=list)
    unlock_data_missing: bool = True
    issues: list[DataIssue] = field(default_factory=list)

    # --- convenience aggregates -------------------------------------------------
    def perp_oi_usd_total(self) -> Optional[float]:
        vals = [m.perp_open_interest_usd for m in self.markets_by_venue.values()
                if m.perp_open_interest_usd and m.perp_open_interest_usd > 0]
        return math.fsum(vals) if vals else None

    def spot_volume_usd_total(self) -> Optional[float]:
        vals = [m.spot_daily_volume_usd for m in self.markets_by_venue.values()
                if m.spot_daily_volume_usd and m.spot_daily_volume_usd > 0]
        return math.fsum(vals) if vals else None


@dataclass(frozen=True, slots=True)
class CarryEstimate:
    coin: Coin
    aggregation_method: Literal["oi_weighted_median", "median", "single_venue", "unavailable"]
    selected_short_venue: Optional[Venue] = None
    selected_spot_venue: Optional[Venue] = None
    funding_8h_decimal: Optional[float] = None
    funding_apr: Optional[float] = None
    basis_pct: Optional[float] = None
    basis_apr: Optional[float] = None
    total_carry_apr: Optional[float] = None
    venue_funding_aprs: dict[Venue, float] = field(default_factory=dict)
    venue_basis_aprs: dict[Venue, float] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)
    unavailable_reason: Optional[str] = None


@dataclass(frozen=True, slots=True)
class TrapSubSignal:
    name: str
    score: float  # normalized [0, 1]
    raw_value: Optional[float]
    available: bool
    reason: str
    hard_flag: bool = False


@dataclass(frozen=True, slots=True)
class TrapBreakdown:
    upcoming_unlocks: TrapSubSignal
    spot_illiquidity_to_perp_oi: TrapSubSignal
    spot_leading_perp: TrapSubSignal
    oi_market_cap_distortion: TrapSubSignal
    composite_score: float  # weighted [0, 1]
    weights_used: dict[str, float]
    excluded: bool
    exclusion_reasons: list[str]
    unlock_data_missing: bool
    insufficient_trap_data: bool

    def subsignals(self) -> list[TrapSubSignal]:
        return [
            self.upcoming_unlocks,
            self.spot_illiquidity_to_perp_oi,
            self.spot_leading_perp,
            self.oi_market_cap_distortion,
        ]


@dataclass(slots=True)
class CoinSignal:
    coin: Coin
    status: SignalStatus
    raw: CoinRawInput
    carry: CarryEstimate
    trap: TrapBreakdown
    risk_adjusted_apr: Optional[float]
    top_reason: str
    rank: Optional[int] = None
    generated_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class SourceRunMetadata:
    name: SourceName
    ok: bool
    used_cache: bool = False
    stale: bool = False
    error: Optional[str] = None
    fetched_at: Optional[datetime] = None


@dataclass(slots=True)
class RunReport:
    generated_at: datetime
    config_snapshot: dict[str, Any]
    key_present: dict[str, bool]
    sources: dict[SourceName, SourceRunMetadata]
    signals: list[CoinSignal]


def to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses, datetimes and floats to JSON types."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    return str(obj)
