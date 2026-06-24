"""Builders for synthetic inputs used across the offline unit tests."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from basis_arb.models import (
    CoinRawInput,
    ReturnBar,
    UnlockEvent,
    VenueFunding,
    VenueMarket,
)

UTC = timezone.utc
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def vmarket(venue, *, mark=None, index=None, oi_usd=None, spot=None, spot_vol=None, premium=None):
    return VenueMarket(
        venue=venue, source_symbol=f"{venue.upper()}SYM",
        perp_mark_price=mark, perp_index_price=index, perp_open_interest_usd=oi_usd,
        perp_premium=premium, spot_price=spot, spot_daily_volume_usd=spot_vol,
    )


def vfunding(venue, funding_8h, *, interval=8.0, context_only=False):
    return VenueFunding(
        venue=venue, source_symbol="S",
        funding_8h_decimal=funding_8h, funding_apr=funding_8h * 3 * 365,
        interval_hours=interval, context_only=context_only,
    )


def event(days_from_now, tokens, *, category="unlock"):
    return UnlockEvent(
        project_slug="proj",
        timestamp=datetime.now(UTC) + timedelta(days=days_from_now),
        tokens=tokens, category=category,
    )


def rbars(log_returns):
    """ReturnBars on a fixed hourly grid with the given log returns."""
    return [ReturnBar(_BASE + timedelta(hours=i), close=1.0, log_return=lr)
            for i, lr in enumerate(log_returns)]


def lead_lag_series(n, *, drift, lag):
    """Return (spot_bars, perp_bars) where perp lags spot by `lag` bars.

    full[t] drives both: spot[t] = full[t+lag], perp[t] = full[t]. So
    spot[t] == perp[t+lag] => spot leads perp by `lag`.
    """
    full = [drift + 0.02 * math.sin(i * 0.7) for i in range(n + lag)]
    spot = full[lag:]
    perp = full[:n]
    return rbars(spot), rbars(perp)


def make_raw(coin="ABC", *, markets=None, funding=None, market_cap=None, circ=None,
             total=None, fdv=None, events=None, unlock_missing=True,
             spot_returns=None, perp_returns=None, loris_rank=None):
    raw = CoinRawInput(coin=coin)
    if markets:
        raw.markets_by_venue = {m.venue: m for m in markets}
    if funding:
        raw.funding_by_venue = {f.venue: f for f in funding}
    raw.market_cap_usd = market_cap
    raw.circulating_supply = circ
    raw.total_supply = total
    raw.fully_diluted_valuation_usd = fdv
    if events is not None:
        raw.unlock_events = events
    raw.unlock_data_missing = unlock_missing
    if spot_returns is not None:
        raw.spot_returns = spot_returns
    if perp_returns is not None:
        raw.perp_returns = perp_returns
    raw.loris_oi_rank = loris_rank
    return raw
