"""Configuration for basis_arb.

Every tunable lives here as a named field (no magic numbers in formulas).
CLI flags in cli.py override a subset of these at runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any, Literal

Venue = str


@dataclass(frozen=True, slots=True)
class BasisArbConfig:
    # --- universe and venues ---------------------------------------------------
    universe_mode: Literal["hybrid", "loris", "hyperliquid", "manual"] = "hybrid"
    universe_size: int = 30
    manual_coins: tuple[str, ...] = ()
    venues: tuple[Venue, ...] = ("binance", "bybit", "okx", "hyperliquid")
    quote_assets: tuple[str, ...] = ("USDT", "USDC", "USD", "USDe", "BUSD")
    excluded_coins: tuple[str, ...] = (
        "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDD", "BUSD", "USD", "USDE",
    )
    symbol_overrides: dict[str, str] = field(
        default_factory=lambda: {"WETH": "ETH", "WBTC": "BTC", "WBETH": "ETH"}
    )

    # --- requests / cache ------------------------------------------------------
    request_timeout_seconds: float = 12.0
    request_retries: int = 2
    request_backoff_seconds: float = 0.5
    cache_dir: str = ".cache/basis_arb"
    cache_enabled: bool = True
    loris_cache_ttl_seconds: int = 60
    coingecko_cache_ttl_seconds: int = 21600
    defillama_cache_ttl_seconds: int = 86400
    exchange_cache_ttl_seconds: int = 60
    hyperliquid_cache_ttl_seconds: int = 30

    # --- output ----------------------------------------------------------------
    output_json_path: str = "basis_arb_signals.json"
    max_table_rows: int = 50
    reason_width: int = 60
    show_excluded: bool = True

    # --- execution (Hyperliquid) -------------------------------------------------
    # These fields configure the execution layer. Secret keys are NEVER stored
    # in config — they are read from environment variables only:
    #   HYPERLIQUID_SECRET_KEY       — private key (0x... hex)
    #   HYPERLIQUID_ACCOUNT_ADDRESS — optional sub-account
    #   HYPERLIQUID_ENABLED         — "true" to enable live trading
    #   HYPERLIQUID_SLIPPAGE_BPS    — slippage tolerance (default 5.0)
    hyperliquid_enabled: bool = False
    hyperliquid_slippage_bps: float = 5.0
    hyperliquid_max_slippage_bps: float = 20.0

    # --- carry -----------------------------------------------------------------
    funding_periods_per_day: float = 3.0  # 8h funding -> 3/day
    days_per_year: float = 365.0
    # Execution cost estimate for net-carry calculation.
    # Retail CEX round-trip (spot + perp): ~8 bps.
    # New-venue / maker-tier: ~3–4 bps.  Manual-operator (tread.fi): ~10–15 bps.
    # Expressed as total round-trip basis points (open + close).
    execution_fee_bps_roundtrip: float = 8.0
    basis_convergence_days: float = 30.0
    funding_aggregation: Literal["oi_weighted_median", "median"] = "oi_weighted_median"
    min_oi_weighted_venues: int = 2
    funding_flip_near_zero_8h: float = 0.00005
    basis_blowout_pct: float = 0.05

    # --- unlock trap -----------------------------------------------------------
    unlock_horizon_days: int = 90
    unlock_proximity_half_life_days: float = 30.0
    unlock_pressure_low: float = 0.01
    unlock_pressure_high: float = 0.10
    fallback_unlock_weight: float = 0.75
    overhang_low: float = 1.0
    overhang_high: float = 5.0
    unlock_hard_score: float = 0.90
    unlock_hard_days: int = 30
    unlock_hard_pct_circ: float = 0.05
    overhang_hard: float = 8.0

    # --- liquidity / OI trap ---------------------------------------------------
    min_volume_floor_usd: float = 100_000.0
    oi_spot_vol_ratio_low: float = 1.0
    oi_spot_vol_ratio_high: float = 10.0
    oi_spot_vol_hard_ratio: float = 25.0
    oi_market_cap_ratio_low: float = 0.02
    oi_market_cap_ratio_high: float = 0.25
    oi_market_cap_hard_ratio: float = 0.40

    # --- lead / lag trap -------------------------------------------------------
    lead_lag_lookback_days: int = 7
    lead_lag_bar_interval: str = "1h"
    lead_lag_lags_bars: tuple[int, ...] = (1, 2, 3, 4)
    min_return_bars: int = 48
    min_spot_lead_corr: float = 0.20
    lead_corr_low: float = 0.05
    lead_corr_high: float = 0.25
    spot_up_return_low: float = 0.03
    spot_up_return_high: float = 0.15
    spot_lead_hard_score: float = 0.85
    spot_lead_hard_up_return: float = 0.20

    # --- composite trap / ranking ----------------------------------------------
    unlock_weight: float = 0.35
    spot_illiquidity_weight: float = 0.25
    spot_leading_weight: float = 0.20
    oi_market_cap_weight: float = 0.20
    trap_exclusion_score: float = 0.75
    min_available_trap_subsignals: int = 3

    def snapshot(self) -> dict[str, Any]:
        """A JSON-serializable snapshot of the active config."""
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, tuple):
                v = list(v)
            out[f.name] = v
        return out

    def with_overrides(self, **kwargs: Any) -> "BasisArbConfig":
        clean = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **clean) if clean else self
