"""Orchestration: fetch sources, assemble per-coin inputs, compute & rank.

Bulk snapshots (Loris funding, Hyperliquid contexts, CoinGecko markets,
DeFiLlama protocol list) are fetched once; per-coin market/kline data is
fetched per universe coin. Every source failure degrades gracefully.
"""
from __future__ import annotations

import dataclasses
import sys
from typing import Callable, Optional

from .config import BasisArbConfig
from .models import (
    CoinRawInput,
    CoinSignal,
    RunReport,
    SourceRunMetadata,
    utcnow,
)
from .normalization import filter_universe, is_excluded
from .signals.carry import estimate_carry
from .signals.ranking import build_signal, rank_signals
from .signals.trap import compute_trap
from .sources.cache import JsonCache
from .sources.coingecko import CoinGeckoClient
from .sources.defillama import DefiLlamaClient
from .sources.exchanges import BinanceClient, BybitClient, OkxClient
from .sources.hyperliquid_info import HyperliquidInfoClient
from .sources.loris import LorisClient

ProgressFn = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def run_pipeline(
    cfg: BasisArbConfig,
    loris_api_key: Optional[str],
    progress: Optional[ProgressFn] = None,
) -> RunReport:
    log = progress or _noop
    cache = JsonCache(cfg.cache_dir, enabled=cfg.cache_enabled)
    sources: dict[str, SourceRunMetadata] = {}

    loris = LorisClient(loris_api_key, cfg, cache)
    hl = HyperliquidInfoClient(cfg, cache)
    cg = CoinGeckoClient(cfg, cache)
    dl = DefiLlamaClient(cfg, cache)
    exchange_clients = _build_exchange_clients(cfg, cache)

    log("fetching Loris funding ...")
    loris_snap, m = loris.fetch(); sources["loris"] = m
    log("fetching Hyperliquid contexts ...")
    hl_snap, m = hl.fetch_contexts(); sources["hyperliquid"] = m
    log("fetching CoinGecko markets ...")
    cg_snap, m = cg.fetch_markets(); sources["coingecko"] = m

    universe = _select_universe(cfg, hl_snap, loris_snap, cg_snap)
    log(f"universe: {len(universe)} coins")

    raws: list[CoinRawInput] = []
    for i, coin in enumerate(universe, start=1):
        log(f"[{i}/{len(universe)}] {coin}")
        raws.append(_assemble_coin(coin, cfg, hl_snap, loris_snap, cg_snap, exchange_clients))

    # Unlocks: one DeFiLlama pass over the assembled universe.
    log("fetching DeFiLlama unlock schedules ...")
    targets = {r.coin: r.coingecko_id for r in raws}
    unlock_snap, m = dl.fetch_unlocks(targets); sources["defillama"] = m
    for r in raws:
        events = unlock_snap.events_by_coin.get(r.coin, [])
        r.unlock_events = events
        r.unlock_data_missing = r.coin not in unlock_snap.resolved_coins

    now = utcnow()
    signals: list[CoinSignal] = []
    for r in raws:
        carry = estimate_carry(r, cfg)
        trap = compute_trap(r, cfg, now)
        signals.append(build_signal(r, carry, trap, cfg))
    ranked = rank_signals(signals)

    return RunReport(
        generated_at=now,
        config_snapshot=cfg.snapshot(),
        key_present={"LORIS_API_KEY": bool((loris_api_key or "").strip())},
        sources=sources,
        signals=ranked,
    )


def _build_exchange_clients(cfg: BasisArbConfig, cache: JsonCache) -> list:
    registry = {"binance": BinanceClient, "bybit": BybitClient, "okx": OkxClient}
    return [registry[v](cfg, cache) for v in cfg.venues if v in registry]


def _select_universe(cfg, hl_snap, loris_snap, cg_snap) -> list[str]:
    if cfg.universe_mode == "manual" or cfg.manual_coins:
        return filter_universe(cfg.manual_coins, cfg)

    hl_coins = [c for c in hl_snap.perp_markets if not is_excluded(c, cfg)]

    def key(c: str):
        rank_entry = loris_snap.oi_rankings.get(c)
        rank = rank_entry[0] if (rank_entry and rank_entry[0] is not None) else 10 ** 9
        oi = hl_snap.perp_markets[c].perp_open_interest_usd or 0.0
        return (rank, -oi)

    hl_coins.sort(key=key)
    return hl_coins[: cfg.universe_size]


def _assemble_coin(coin, cfg, hl_snap, loris_snap, cg_snap, exchange_clients) -> CoinRawInput:
    raw = CoinRawInput(coin=coin)

    # Hyperliquid perp context.
    hl_mkt = hl_snap.perp_markets.get(coin)
    hl_spot = hl_snap.spot.get(coin)
    if hl_mkt is not None:
        if hl_spot:
            hl_mkt = dataclasses.replace(
                hl_mkt,
                spot_price=hl_spot.get("spot_price"),
                spot_daily_volume_usd=hl_spot.get("spot_daily_volume_usd"),
            )
        raw.markets_by_venue["hyperliquid"] = hl_mkt
        raw.hyperliquid_premium = hl_mkt.perp_premium
        raw.hyperliquid_mark_price = hl_mkt.perp_mark_price
        raw.hyperliquid_open_interest_usd = hl_mkt.perp_open_interest_usd
        raw.source_symbols["hyperliquid"] = hl_mkt.source_symbol

    # Public exchange markets.
    for client in exchange_clients:
        mkt, _ = client.fetch_market(coin)
        if mkt is not None:
            raw.markets_by_venue[client.venue] = mkt
            raw.source_symbols[client.venue] = mkt.source_symbol

    # Loris funding + OI rank.
    for venue, by_coin in loris_snap.funding_by_venue.items():
        vf = by_coin.get(coin)
        if vf is not None:
            raw.funding_by_venue[venue] = vf
            raw.source_symbols.setdefault("loris", vf.source_symbol)
    rank_entry = loris_snap.oi_rankings.get(coin)
    if rank_entry is not None:
        raw.loris_oi_rank, raw.loris_oi_rank_raw = rank_entry

    # CoinGecko market cap + supply (primary), Hyperliquid spot as fallback.
    cgm = cg_snap.by_coin.get(coin)
    if cgm is not None:
        raw.coingecko_id = cgm.gecko_id
        raw.market_cap_usd = cgm.market_cap_usd
        raw.market_cap_source = "coingecko" if cgm.market_cap_usd is not None else None
        raw.circulating_supply = cgm.circulating_supply
        raw.total_supply = cgm.total_supply
        raw.fully_diluted_valuation_usd = cgm.fully_diluted_valuation_usd
        raw.source_symbols["coingecko"] = cgm.gecko_id
    if raw.market_cap_usd is None and hl_spot and hl_spot.get("market_cap"):
        raw.market_cap_usd = hl_spot.get("market_cap")
        raw.market_cap_source = "hyperliquid_spot"
        raw.circulating_supply = raw.circulating_supply or hl_spot.get("circulating_supply")
        raw.total_supply = raw.total_supply or hl_spot.get("total_supply")

    # Lead/lag return bars (Binance primary, Bybit fallback).
    raw.spot_returns, raw.perp_returns, raw.lead_lag_venue = _fetch_lead_lag(coin, cfg, exchange_clients)
    return raw


def _fetch_lead_lag(coin, cfg, exchange_clients):
    by_venue = {c.venue: c for c in exchange_clients}
    for venue in ("binance", "bybit"):
        client = by_venue.get(venue)
        if client is None or not hasattr(client, "fetch_return_bars"):
            continue
        spot, _ = client.fetch_return_bars(coin, "spot")
        perp, _ = client.fetch_return_bars(coin, "perp")
        if len(spot) >= cfg.min_return_bars and len(perp) >= cfg.min_return_bars:
            return spot, perp, venue
    return [], [], None
