"""Hyperliquid execution engine — trades perp positions on Hyperliquid.

This module is the ONLY place in the codebase that imports Exchange
(and the signing libraries needed for it). It is the execution layer
for the basis arb tool.

IMPORTANT SECURITY INVARIANTS:
  1. This module NEVER prints or logs private keys, seed phrases, or signatures.
  2. The secret key is read ONLY from the HYPERLIQUID_SECRET_KEY env var.
  3. The key is held in memory ONLY during Exchange initialization.
  4. This module NEVER makes trading decisions — it only executes what the
     trading loop (executor.py) tells it to.
  5. All outbound network calls are validated against the allowlist in safety.py.

GUARDRAIL: this module ONLY imports:
  - eth_account (for LocalAccount / key handling)
  - hyperliquid.exchange.Exchange (trading)
  - hyperliquid.info.Info (read-only market data)
  - safety.py (url validation)
It must NEVER import web3, bitcoinlib, or other key-handling libraries.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, str(__file__.rsplit("/", 2)[0]))

from ..safety import validate_url_host

# Lazy import — only load when actually trading, not on every pipeline run
_wallet: Optional[object] = None
_exchange: Optional[object] = None


@dataclass
class HyperliquidConfig:
    """Configuration for the Hyperliquid execution engine.

    All fields are read from environment variables at runtime.
    No secrets are stored in config files or source code.
    """
    secret_key: Optional[str] = field(default=None)
    account_address: Optional[str] = field(default=None)   # Optional: specific account
    base_url: str = field(default="https://api.hyperliquid.xyz")
    slippage_bps: float = 5.0   # 5 bps = 0.05% slippage tolerance
    max_slippage_bps: float = 20.0  # hard cap: refuse if slippage exceeds this
    timeout_seconds: float = 30.0
    enabled: bool = False        # False = dry-run / read-only mode

    @classmethod
    def from_env(cls) -> "HyperliquidConfig":
        """Load from environment variables.

        Variables read:
          HYPERLIQUID_SECRET_KEY — the private key (hex string, 0x...)
          HYPERLIQUID_ACCOUNT_ADDRESS — optional, for sub-accounts
          HYPERLIQUID_SLIPPAGE_BPS — slippage tolerance (default 5)
          HYPERLIQUID_ENABLED — set to "true" to enable live trading
        """
        secret = os.environ.get("HYPERLIQUID_SECRET_KEY", "").strip() or None
        address = os.environ.get("HYPERLIQUID_ACCOUNT_ADDRESS", "").strip() or None
        slippage = float(os.environ.get("HYPERLIQUID_SLIPPAGE_BPS", "5.0"))
        enabled = os.environ.get("HYPERLIQUID_ENABLED", "false").strip().lower() == "true"

        return cls(
            secret_key=secret,
            account_address=address,
            slippage_bps=slippage,
            enabled=enabled,
        )

    def validate(self) -> tuple[bool, str]:
        """Validate configuration. Returns (is_valid, reason)."""
        if not self.enabled:
            return True, "disabled — dry-run mode"
        if not self.secret_key:
            return False, "HYPERLIQUID_SECRET_KEY env var not set"
        if not self.secret_key.startswith("0x") and len(self.secret_key) != 64:
            return False, "secret key format invalid (expected 0x... 64 hex chars)"
        if self.slippage_bps > self.max_slippage_bps:
            return False, f"slippage {self.slippage_bps} bps exceeds max {self.max_slippage_bps} bps"
        return True, ""


def _load_wallet(secret_key: str):
    """Create an eth_account LocalAccount from a hex private key.

    The key is held in memory for the lifetime of the process.
    It is NEVER printed, logged, or written to disk.
    """
    from eth_account import Account
    # Suppress the deprecated warning
    import warnings
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="eth_account")
    return Account.from_key(secret_key)


def _get_exchange(cfg: HyperliquidConfig):
    """Get or create the Exchange instance (singleton per process)."""
    global _exchange, _wallet
    if _exchange is not None:
        return _exchange

    if not cfg.enabled:
        raise RuntimeError("Exchange requested but HYPERLIQUID_ENABLED=false")

    is_valid, reason = cfg.validate()
    if not is_valid:
        raise RuntimeError(f"Invalid Hyperliquid config: {reason}")

    _wallet = _load_wallet(cfg.secret_key)
    _exchange = _create_exchange(_wallet, cfg)
    return _exchange


def _create_exchange(wallet, cfg: HyperliquidConfig):
    """Create the Exchange instance with wallet and config."""
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils.constants import MAINNET_API_URL

    base_url = cfg.base_url
    if not validate_url_host(base_url):
        raise ValueError(f"Hyperliquid base_url not in allowlist: {base_url}")

    # Pre-fetch meta for the Exchange (needed for asset context)
    info = Info(base_url, skip_ws=True, timeout=cfg.timeout_seconds)

    exchange = Exchange(
        wallet,
        base_url=base_url,
        meta=info.meta_and_asset_ctxs()[0],
        account_address=cfg.account_address,
        timeout=cfg.timeout_seconds,
    )
    return exchange


# ------------------------------------------------------------------
# Trading API — these are the ONLY public entry points for trading
# ------------------------------------------------------------------

def open_short_perp(
    coin: str,
    size: float,
    cfg: Optional[HyperliquidConfig] = None,
) -> dict:
    """Open a SHORT perpetual position on Hyperliquid.

    This is the primary execution primitive for basis arbitrage:
    the basis arb strategy is LONG spot / SHORT perp, so we open shorts here.

    Args:
        coin: canonical coin name (e.g. "BTC", "ETH")
        size: position size in coin units (NOT USD notional)
        cfg: HyperliquidConfig. If None, loads from env.

    Returns:
        dict with keys: success (bool), order_id (str), filled_at (str),
                        slippage_bps (float), error (str or None)
    """
    if cfg is None:
        cfg = HyperliquidConfig.from_env()

    if not cfg.enabled:
        return {
            "success": False,
            "order_id": None,
            "filled_at": None,
            "slippage_bps": None,
            "error": "DRY_RUN: HYPERLIQUID_ENABLED=false",
            "coin": coin,
            "side": "SHORT",
            "size": size,
        }

    try:
        exchange = _get_exchange(cfg)
    except Exception as e:
        return {
            "success": False,
            "order_id": None,
            "filled_at": None,
            "slippage_bps": None,
            "error": f"exchange init failed: {e}",
            "coin": coin,
            "side": "SHORT",
            "size": size,
        }

    try:
        # is_buy=False means SHORT
        # px=None means market order
        # slippage is the max acceptable slippage in dollars (approximated as bps)
        result = exchange.market_open(
            name=coin,
            is_buy=False,      # SHORT
            sz=size,
            px=None,           # market order
            slippage=cfg.slippage_bps,
        )

        # Parse the result
        filled_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        order_data = result.get("data", {}) if isinstance(result, dict) else {}

        # Extract order ID
        order_id = None
        if isinstance(order_data, dict):
            order_id = order_data.get("orderId") or order_data.get("oid") or str(order_data)

        return {
            "success": True,
            "order_id": order_id,
            "filled_at": filled_at,
            "slippage_bps": cfg.slippage_bps,
            "error": None,
            "coin": coin,
            "side": "SHORT",
            "size": size,
            "raw_response": str(result)[:500],  # truncate to avoid log bloat
        }

    except Exception as e:
        return {
            "success": False,
            "order_id": None,
            "filled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "slippage_bps": None,
            "error": f"{type(e).__name__}: {e}",
            "coin": coin,
            "side": "SHORT",
            "size": size,
        }


def close_perp_position(
    coin: str,
    cfg: Optional[HyperliquidConfig] = None,
) -> dict:
    """Close an existing SHORT perpetual position on Hyperliquid.

    Args:
        coin: canonical coin name
        cfg: HyperliquidConfig. If None, loads from env.

    Returns:
        dict with keys: success, order_id, filled_at, slippage_bps, error
    """
    if cfg is None:
        cfg = HyperliquidConfig.from_env()

    if not cfg.enabled:
        return {
            "success": False,
            "order_id": None,
            "filled_at": None,
            "slippage_bps": None,
            "error": "DRY_RUN: HYPERLIQUID_ENABLED=false",
            "coin": coin,
            "action": "CLOSE",
        }

    try:
        exchange = _get_exchange(cfg)
    except Exception as e:
        return {
            "success": False,
            "order_id": None,
            "filled_at": None,
            "slippage_bps": None,
            "error": f"exchange init failed: {e}",
            "coin": coin,
            "action": "CLOSE",
        }

    try:
        result = exchange.market_close(
            name=coin,
            slippage=cfg.slippage_bps,
        )
        filled_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        order_data = result.get("data", {}) if isinstance(result, dict) else {}
        order_id = None
        if isinstance(order_data, dict):
            order_id = order_data.get("orderId") or order_data.get("oid") or str(order_data)

        return {
            "success": True,
            "order_id": order_id,
            "filled_at": filled_at,
            "slippage_bps": cfg.slippage_bps,
            "error": None,
            "coin": coin,
            "action": "CLOSE",
            "raw_response": str(result)[:500],
        }
    except Exception as e:
        return {
            "success": False,
            "order_id": None,
            "filled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "slippage_bps": None,
            "error": f"{type(e).__name__}: {e}",
            "coin": coin,
            "action": "CLOSE",
        }


def get_mark_price(coin: str, cfg: Optional[HyperliquidConfig] = None) -> float | None:
    """Get the current mark price for a coin from Hyperliquid Info API.

    This is read-only — no signing needed.

    Returns:
        The mark price in USD, or None if unavailable.
    """
    if cfg is None:
        cfg = HyperliquidConfig.from_env()

    from hyperliquid.info import Info
    from hyperliquid.utils.constants import MAINNET_API_URL

    try:
        info = Info(cfg.base_url, skip_ws=True, timeout=cfg.timeout_seconds)
        meta, asset_ctxs = info.meta_and_asset_ctxs()
        universe = meta.get("universe") or []
        for asset, ctx in zip(universe, asset_ctxs):
            if asset.get("name") == coin:
                mark = ctx.get("markPx")
                if mark is not None:
                    return float(mark)
        return None
    except Exception:
        return None


def get_account_value(cfg: Optional[HyperliquidConfig] = None) -> dict:
    """Get the account's total value and positions from Hyperliquid.

    Args:
        cfg: HyperliquidConfig. If None, loads from env.

    Returns:
        dict with keys: total_value_usd, positions (list), error
    """
    if cfg is None:
        cfg = HyperliquidConfig.from_env()

    from hyperliquid.info import Info
    from hyperliquid.utils.constants import MAINNET_API_URL

    try:
        if cfg.secret_key:
            wallet = _load_wallet(cfg.secret_key)
            address = wallet.address
        elif cfg.account_address:
            address = cfg.account_address
        else:
            return {"total_value_usd": None, "positions": [], "error": "no address or key available"}

        info = Info(cfg.base_url, skip_ws=True, timeout=cfg.timeout_seconds)
        state = info.user_state(address)

        if not isinstance(state, dict):
            return {"total_value_usd": None, "positions": [], "error": f"unexpected response: {type(state)}"}

        # Parse margin account value
        total_value = state.get("totalValue")
        positions_raw = state.get("assetPositions", []) or []

        positions = []
        for p in positions_raw:
            if isinstance(p, dict):
                pos = p.get("position", {}) or {}
                positions.append({
                    "coin": pos.get("coin"),
                    "szi": pos.get("szi"),   # position size (negative = short)
                    "entry_px": pos.get("entryPx"),
                    "unrealized_pnl": pos.get("unrealizedPnl"),
                })

        return {
            "total_value_usd": float(total_value) if total_value else 0.0,
            "positions": positions,
            "error": None,
        }
    except Exception as e:
        return {"total_value_usd": None, "positions": [], "error": f"{type(e).__name__}: {e}"}
