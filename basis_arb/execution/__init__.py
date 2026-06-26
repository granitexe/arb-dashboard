"""Execution layer — Hyperliquid trading via the official Python SDK.

Modules:
  hyperliquid.py — the Hyperliquid Exchange SDK integration.
                    ONLY module in the codebase that handles secret keys.

Public API:
  HyperliquidConfig — config (loads from env vars)
  open_short_perp()  — open a SHORT perp position
  close_perp_position() — close an existing short
  get_account_value()  — read current positions and account value

The trading loop (executor.py) is the only consumer of this module.
"""
from .hyperliquid import (
    HyperliquidConfig,
    open_short_perp,
    close_perp_position,
    get_account_value,
)

__all__ = [
    "HyperliquidConfig",
    "open_short_perp",
    "close_perp_position",
    "get_account_value",
]
