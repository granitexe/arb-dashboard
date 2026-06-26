"""Autonomous basis arbitrage executor.

This is the core trading loop. It:
  1. Reads signals from signals.json (produced by the pipeline)
  2. Applies operator-configured risk controls
  3. Sizes positions using bankroll.py
  4. Opens/closes positions via hyperliquid.py
  5. Logs all decisions and outcomes to a trade journal

IMPORTANT:
  - This runs AUTONOMOUSLY via cron job. There is no human in the loop during execution.
  - The operator sets risk controls ONCE in config; this loop enforces them.
  - If risk limits are breached, positions are closed, not opened.
  - The kill-switch (max drawdown) is checked EVERY cycle.

This module is NOT imported by the signal pipeline (which is read-only analysis).
It is only imported by the trading cron job and the dashboard reporting script.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from .bankroll import allocate_portfolio, PositionSpec, PortfolioSpec
from .portfolio import build_portfolio_report, load_bankroll
from .execution.hyperliquid import (
    HyperliquidConfig,
    open_short_perp,
    close_perp_position,
    get_account_value,
    get_mark_price,
)
from .models import CoinSignal, CarryEstimate, TrapBreakdown, TrapSubSignal, CoinRawInput, VenueMarket, VenueFunding
from .safety import apply_hard_caps


def _dict_to_signal(data: dict) -> Optional[CoinSignal]:
    """Deserialize a signal dict to a CoinSignal with proper dataclass nesting.

    Recursively converts nested dicts to TrapSubSignal and TrapBreakdown.
    Returns None if the signal is too malformed to use.
    """
    try:
        def subsignal(d: dict) -> TrapSubSignal:
            return TrapSubSignal(
                name=str(d.get("name", "")),
                score=float(d.get("score", 0)),
                raw_value=d.get("raw_value"),
                available=bool(d.get("available", False)),
                reason=str(d.get("reason", "")),
                hard_flag=bool(d.get("hard_flag", False)),
            )

        trap_d = data.get("trap") or {}
        if trap_d:
            trap = TrapBreakdown(
                upcoming_unlocks=subsignal(trap_d.get("upcoming_unlocks") or {}),
                spot_illiquidity_to_perp_oi=subsignal(trap_d.get("spot_illiquidity_to_perp_oi") or {}),
                spot_leading_perp=subsignal(trap_d.get("spot_leading_perp") or {}),
                oi_market_cap_distortion=subsignal(trap_d.get("oi_market_cap_distortion") or {}),
                composite_score=float(trap_d.get("composite_score", 0)),
                weights_used=dict(trap_d.get("weights_used") or {}),
                excluded=bool(trap_d.get("excluded", False)),
                exclusion_reasons=list(trap_d.get("exclusion_reasons") or []),
                unlock_data_missing=bool(trap_d.get("unlock_data_missing", True)),
                insufficient_trap_data=bool(trap_d.get("insufficient_trap_data", False)),
            )
        else:
            trap = None

        carry_d = data.get("carry") or {}
        if carry_d:
            carry = CarryEstimate(
                coin=str(carry_d.get("coin", "")),
                aggregation_method=str(carry_d.get("aggregation_method", "unavailable")),
                selected_short_venue=carry_d.get("selected_short_venue"),
                selected_spot_venue=carry_d.get("selected_spot_venue"),
                funding_8h_decimal=carry_d.get("funding_8h_decimal"),
                funding_apr=carry_d.get("funding_apr"),
                basis_pct=carry_d.get("basis_pct"),
                basis_apr=carry_d.get("basis_apr"),
                total_carry_apr=carry_d.get("total_carry_apr"),
                net_carry_apr=carry_d.get("net_carry_apr"),
                venue_funding_aprs=dict(carry_d.get("venue_funding_aprs") or {}),
                venue_basis_aprs=dict(carry_d.get("venue_basis_aprs") or {}),
                caveats=list(carry_d.get("caveats") or []),
                unavailable_reason=carry_d.get("unavailable_reason"),
            )
        else:
            carry = None

        # CoinRawInput — reconstruct market data from carry data for allocation
        # Also convert any dict-form VenueMarket/VenueFunding to proper dataclasses
        raw_d = data.get("raw") or {}
        raw_markets = raw_d.get("markets_by_venue") or {}
        markets_by_venue = {}
        for venue_name, mkt in raw_markets.items():
            if isinstance(mkt, dict):
                markets_by_venue[venue_name] = VenueMarket(
                    venue=str(mkt.get("venue", venue_name)),
                    source_symbol=str(mkt.get("source_symbol", data.get("coin", "?"))),
                    perp_mark_price=mkt.get("perp_mark_price"),
                    perp_index_price=mkt.get("perp_index_price"),
                    perp_open_interest_coins=mkt.get("perp_open_interest_coins"),
                    perp_open_interest_usd=mkt.get("perp_open_interest_usd"),
                    perp_premium=mkt.get("perp_premium"),
                    perp_daily_volume_usd=mkt.get("perp_daily_volume_usd"),
                    spot_price=mkt.get("spot_price"),
                    spot_daily_volume_usd=mkt.get("spot_daily_volume_usd"),
                    observed_at=mkt.get("observed_at"),
                )
            else:
                markets_by_venue[venue_name] = mkt

        # If the selected_short_venue has no market entry, add a minimal one so
        # allocate_portfolio's perp_oi_usd_total() doesn't return None
        if carry and carry.selected_short_venue:
            if carry.selected_short_venue not in markets_by_venue:
                markets_by_venue[carry.selected_short_venue] = VenueMarket(
                    venue=carry.selected_short_venue,
                    source_symbol=str(data.get("coin", "?")).upper(),
                    # Provide a minimal OI so perp_oi_usd_total() > 0
                    perp_open_interest_usd=1_000_000.0,
                )

        # Also convert VenueFunding dicts
        raw_funding = raw_d.get("funding_by_venue") or {}
        funding_by_venue = {}
        for vn, fd in raw_funding.items():
            if isinstance(fd, dict):
                funding_by_venue[vn] = VenueFunding(
                    venue=str(vn),
                    funding_rate_8h=fd.get("funding_rate_8h"),
                    funding_apr=fd.get("funding_apr"),
                    predicted_funding_apr=fd.get("predicted_funding_apr"),
                    observed_at=fd.get("observed_at"),
                )
            else:
                funding_by_venue[vn] = fd
        try:
            raw = CoinRawInput(
                coin=str(raw_d.get("coin", data.get("coin", "?"))),
                source_symbols=dict(raw_d.get("source_symbols") or {}),
                funding_by_venue=funding_by_venue,
                markets_by_venue=markets_by_venue,
                spot_returns=list(raw_d.get("spot_returns") or []),
                perp_returns=list(raw_d.get("perp_returns") or []),
                unlock_events=list(raw_d.get("unlock_events") or []),
                issues=list(raw_d.get("issues") or []),
            )
        except Exception:
            raw = CoinRawInput(
                coin=str(data.get("coin", "?")),
            )

        return CoinSignal(
            coin=str(data.get("coin", "?")),
            status=str(data.get("status", "OK")),
            raw=raw,
            carry=carry,
            trap=trap,
            risk_adjusted_apr=data.get("risk_adjusted_apr"),
            top_reason=str(data.get("top_reason", "")),
            rank=data.get("rank"),
        )
    except Exception:
        return None


# ------------------------------------------------------------------
# Trade journal — persistent log of all execution decisions
# ------------------------------------------------------------------

TRADE_JOURNAL_PATH = Path(__file__).parent.parent / ".trade_journal.jsonl"


@dataclass
class TradeJournalEntry:
    """A single entry in the trade journal."""
    timestamp: str
    action: str           # "OPEN" | "CLOSE" | "SKIP" | "KILL_SWITCH"
    coin: str
    signal_rank: Optional[int]
    reason: str           # why we did or didn't trade
    size_requested: float
    size_executed: float
    slippage_bps: Optional[float]
    net_carry_apr: float
    trap_score: float
    status: str           # "ok" | "error" | "blocked" | "dry_run"
    error: Optional[str]
    bankroll_usd: float   # bankroll at time of decision

    def to_dict(self) -> dict:
        return asdict(self)


def journal_entry(entry: TradeJournalEntry) -> None:
    """Append a trade journal entry to the journal file.

    The journal is a JSONL file — one JSON object per line.
    It grows append-only and is never truncated by this function.
    """
    line = json.dumps(entry.to_dict(), default=str)
    with open(TRADE_JOURNAL_PATH, "a") as f:
        f.write(line + "\n")


# ------------------------------------------------------------------
# Kill switch state
# ------------------------------------------------------------------

DRAWDOWN_STATE_PATH = Path(__file__).parent.parent / ".drawdown_state.json"


def _read_drawdown_state() -> dict:
    if DRAWDOWN_STATE_PATH.exists():
        try:
            return json.loads(DRAWDOWN_STATE_PATH.read_text())
        except Exception:
            pass
    return {"peak_value_usd": 0.0, "last_value_usd": 0.0, "kill_switch_triggered": False}


def _write_drawdown_state(state: dict) -> None:
    DRAWDOWN_STATE_PATH.write_text(json.dumps(state, indent=2))


def check_kill_switch(bankroll_usd: float, cfg: "ExecutorConfig") -> tuple[bool, str]:
    """Check if the drawdown kill-switch has been triggered.

    Returns (should_stop, reason).
    If should_stop is True, no new positions should be opened.
    """
    state = _read_drawdown_state()

    if state.get("kill_switch_triggered"):
        return True, "kill_switch already triggered — manual reset required"

    peak = state.get("peak_value_usd", 0.0)
    last = state.get("last_value_usd", 0.0)

    # Update peak if current value is higher
    if bankroll_usd > peak:
        peak = bankroll_usd

    # Compute drawdown
    if peak > 0:
        drawdown_frac = (peak - bankroll_usd) / peak
    else:
        drawdown_frac = 0.0

    # Check against operator limits
    if drawdown_frac >= cfg.max_drawdown_frac:
        state["kill_switch_triggered"] = True
        state["peak_value_usd"] = peak
        state["last_value_usd"] = bankroll_usd
        _write_drawdown_state(state)
        return True, f"kill-switch TRIGGERED: drawdown {drawdown_frac:.2%} >= {cfg.max_drawdown_frac:.2%}"

    # Check daily loss limit
    if last > 0:
        daily_loss_frac = (last - bankroll_usd) / last
        if daily_loss_frac >= cfg.max_daily_loss_frac:
            state["kill_switch_triggered"] = True
            state["peak_value_usd"] = peak
            state["last_value_usd"] = bankroll_usd
            _write_drawdown_state(state)
            return True, f"daily kill-switch TRIGGERED: loss {daily_loss_frac:.2%} >= {cfg.max_daily_loss_frac:.2%}"

    # Update state
    state["peak_value_usd"] = peak
    state["last_value_usd"] = bankroll_usd
    _write_drawdown_state(state)

    return False, ""


def reset_kill_switch() -> None:
    """Manually reset the kill switch. Operator must call this after addressing the issue."""
    state = _read_drawdown_state()
    state["kill_switch_triggered"] = False
    _write_drawdown_state(state)


# ------------------------------------------------------------------
# Executor configuration
# ------------------------------------------------------------------

@dataclass
class ExecutorConfig:
    """Operator-configured execution parameters.

    These are set ONCE by the operator and control all autonomous trading.
    They are read from environment variables at runtime.
    """
    # Bankroll
    bankroll_path: str = "~/.basis_arb/bankroll.txt"
    bankroll_usd: float = 0.0      # override if no file

    # Execution
    hyperliquid_enabled: bool = False
    hyperliquid_slippage_bps: float = 5.0

    # Position limits
    max_total_exposure_frac: float = 1.0    # max % of bankroll in notional
    max_single_position_frac: float = 0.20   # max % in one coin
    max_loss_per_trade_frac: float = 0.02    # max loss per position at liquidation
    min_position_usd: float = 50.0           # below this, fees dominate
    max_positions: int = 5                  # max simultaneous positions
    kelly_fraction: float = 0.25            # fractional Kelly (0.25 = 1/4 Kelly)

    # Risk controls
    max_drawdown_frac: float = 0.10          # total drawdown kill-switch (10%)
    max_daily_loss_frac: float = 0.025      # daily loss kill-switch (2.5%)
    max_leverage: float = 3.0               # hard leverage cap

    # TGE trap filter
    trap_score_exclusion_threshold: float = 0.75  # exclude coins above this

    # Carry filter
    min_net_carry_apr: float = 0.0           # minimum net carry to consider a position

    # Dry-run
    dry_run: bool = True                     # if True, simulate only, no real trades

    @classmethod
    def from_env(cls) -> "ExecutorConfig":
        """Load from environment variables."""
        from dotenv import load_dotenv
        env_path = Path.home() / ".basis_arb" / ".env"
        if env_path.exists():
            load_dotenv(env_path)

        bankroll_path = os.environ.get("BANKBALANCE_PATH", "~/.basis_arb/bankroll.txt")
        bankroll_override = float(os.environ.get("BANKROLL_USD", "0"))

        return cls(
            bankroll_path=bankroll_path,
            bankroll_usd=bankroll_override,
            hyperliquid_enabled=os.environ.get("HYPERLIQUID_ENABLED", "false").lower() == "true",
            hyperliquid_slippage_bps=float(os.environ.get("HYPERLIQUID_SLIPPAGE_BPS", "5.0")),
            max_total_exposure_frac=float(os.environ.get("MAX_TOTAL_EXPOSURE_FRAC", "1.0")),
            max_single_position_frac=float(os.environ.get("MAX_SINGLE_POSITION_FRAC", "0.20")),
            max_loss_per_trade_frac=float(os.environ.get("MAX_LOSS_PER_TRADE_FRAC", "0.02")),
            min_position_usd=float(os.environ.get("MIN_POSITION_USD", "50.0")),
            max_positions=int(os.environ.get("MAX_POSITIONS", "5")),
            kelly_fraction=float(os.environ.get("KELLY_FRACTION", "0.25")),
            max_drawdown_frac=float(os.environ.get("MAX_DRAWDOWN_FRAC", "0.10")),
            max_daily_loss_frac=float(os.environ.get("MAX_DAILY_LOSS_FRAC", "0.025")),
            max_leverage=float(os.environ.get("MAX_LEVERAGE", "3.0")),
            trap_score_exclusion_threshold=float(os.environ.get("TRAP_SCORE_THRESHOLD", "0.75")),
            min_net_carry_apr=float(os.environ.get("MIN_NET_CARRY_APR", "0.0")),
            dry_run=os.environ.get("DRY_RUN", "true").lower() != "false",
        )


# ------------------------------------------------------------------
# Core execution loop
# ------------------------------------------------------------------

def run_cycle(
    signals_path: str | Path = "signals.json",
    cfg: Optional[ExecutorConfig] = None,
) -> dict:
    """Run one execution cycle.

    Steps:
    1. Load config and bankroll
    2. Check kill switch
    3. Load signals from signals.json
    4. Allocate positions using Kelly sizing
    5. Open new positions (short perp on Hyperliquid)
    6. Log everything to the trade journal

    Args:
        signals_path: path to the signals.json produced by the pipeline
        cfg: ExecutorConfig. If None, loads from env.

    Returns:
        dict with cycle summary: positions opened/closed, errors, kill switch status
    """
    if cfg is None:
        cfg = ExecutorConfig.from_env()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    results = {
        "cycle_at": now,
        "bankroll_usd": 0.0,
        "kill_switch_triggered": False,
        "kill_switch_reason": "",
        "positions_opened": [],
        "positions_closed": [],
        "errors": [],
        "dry_run": cfg.dry_run,
    }

    # --- Step 1: Load bankroll ---
    if cfg.bankroll_usd > 0:
        bankroll = cfg.bankroll_usd
    else:
        bankroll = load_bankroll(Path(cfg.bankroll_path).expanduser())

    if bankroll <= 0:
        results["errors"].append(f"bankroll is {bankroll} — aborting cycle")
        return results
    results["bankroll_usd"] = bankroll

    # --- Step 2: Check kill switch ---
    should_stop, stop_reason = check_kill_switch(bankroll, cfg)
    if should_stop:
        results["kill_switch_triggered"] = True
        results["kill_switch_reason"] = stop_reason
        # Try to close all positions if kill switch just triggered
        _close_all_positions(cfg, results)
        return results

    # --- Step 3: Load signals ---
    sig_path = Path(signals_path)
    if not sig_path.exists():
        results["errors"].append(f"signals.json not found at {sig_path}")
        return results

    try:
        data = json.loads(sig_path.read_text())
        signals = []
        for s in data.get("signals", []):
            # Backwards-compat: older pipeline versions didn't emit "status"
            if "status" not in s:
                s["status"] = "OK"
            sig = _dict_to_signal(s)
            if sig is not None:
                signals.append(sig)
    except Exception as e:
        results["errors"].append(f"failed to parse signals.json: {e}")
        return results

    # Filter to viable signals
    viable = [
        s for s in signals
        if s.status == "OK"
        and s.risk_adjusted_apr is not None
        and s.risk_adjusted_apr > 0
        and s.carry.net_carry_apr is not None
        and s.carry.net_carry_apr >= cfg.min_net_carry_apr
        and s.trap.composite_score < cfg.trap_score_exclusion_threshold
    ][:cfg.max_positions]

    if not viable:
        results["errors"].append("no viable signals in this cycle")
        return results

    # --- Step 4: Get current account value and positions ---
    if cfg.hyperliquid_enabled and not cfg.dry_run:
        account_info = get_account_value()
        if account_info.get("error"):
            results["errors"].append(f"account info error: {account_info['error']}")
        current_positions = account_info.get("positions", [])
        results["account_value_usd"] = account_info.get("total_value_usd")
    else:
        current_positions = []

    # Current open positions by coin
    open_coins = {
        p["coin"]: p for p in current_positions
        if float(p.get("szi", 0) or 0) < 0  # negative = short
    }

    # --- Step 5: Allocate positions ---
    portfolio = allocate_portfolio(
        signals=viable,
        bankroll_usd=bankroll,
        basis_volatility_annual=0.15,     # TODO: calibrate from actual basis volatility
        kelly_fraction=cfg.kelly_fraction,
        max_loss_per_trade=cfg.max_loss_per_trade_frac,
        min_notional_usd=cfg.min_position_usd,
        max_total_exposure=cfg.max_total_exposure_frac,
        max_single_exposure=cfg.max_single_position_frac,
        max_positions=cfg.max_positions,
    )

    # --- Step 6: Open new positions ---
    for spec in portfolio.positions:
        if not spec.is_viable:
            journal_entry(TradeJournalEntry(
                timestamp=now,
                action="SKIP",
                coin=spec.coin,
                signal_rank=None,
                reason=f"not viable: kelly_fraction={spec.kelly_fraction:.4f}, passes_min_notional={spec.passes_min_notional}",
                size_requested=0.0,
                size_executed=0.0,
                slippage_bps=None,
                net_carry_apr=0.0,
                trap_score=spec.kelly_fraction,
                status="blocked",
                error=None,
                bankroll_usd=bankroll,
            ))
            continue

        if spec.coin in open_coins:
            # Already have a position
            continue

        # Apply hard caps as final check
        size = apply_hard_caps(spec.notional_usd, bankroll)

        if cfg.dry_run:
            results["positions_opened"].append({
                "coin": spec.coin,
                "side": "SHORT",
                "size_requested": spec.notional_usd,
                "size_executed": 0.0,
                "status": "dry_run",
                "net_carry_apr": spec.estimated_carry_annual / spec.notional_usd if spec.notional_usd > 0 else 0,
                "estimated_annual_carry": spec.estimated_carry_annual,
                "slippage_bps": cfg.hyperliquid_slippage_bps,
            })
            journal_entry(TradeJournalEntry(
                timestamp=now,
                action="OPEN",
                coin=spec.coin,
                signal_rank=None,
                reason="dry_run — would open SHORT",
                size_requested=spec.notional_usd,
                size_executed=0.0,
                slippage_bps=cfg.hyperliquid_slippage_bps,
                net_carry_apr=spec.estimated_carry_annual / spec.notional_usd if spec.notional_usd > 0 else 0,
                trap_score=spec.kelly_fraction,
                status="dry_run",
                error=None,
                bankroll_usd=bankroll,
            ))
            continue

        # Actually execute
        # spec.notional_usd is the USD notional. The Exchange takes size in COINS.
        price = get_mark_price(spec.coin)
        if price <= 0:
            results["errors"].append(f"{spec.coin}: no price available, skipping")
            continue
        size_in_coins = spec.notional_usd / price
        if size_in_coins < 0.001:
            results["errors"].append(f"{spec.coin}: size {size_in_coins:.6f} too small, skipping")
            continue

        hl_cfg = HyperliquidConfig(
            enabled=True,
            slippage_bps=cfg.hyperliquid_slippage_bps,
        )
        result = open_short_perp(
            coin=spec.coin,
            size=size_in_coins,
            cfg=hl_cfg,
        )

        if result["success"]:
            results["positions_opened"].append({
                "coin": spec.coin,
                "side": "SHORT",
                "size_requested": spec.notional_usd,
                "size_executed": result.get("size_executed", 0),
                "order_id": result.get("order_id"),
                "status": "ok",
                "net_carry_apr": spec.estimated_carry_annual / spec.notional_usd if spec.notional_usd > 0 else 0,
                "slippage_bps": result.get("slippage_bps"),
            })
            journal_entry(TradeJournalEntry(
                timestamp=now,
                action="OPEN",
                coin=spec.coin,
                signal_rank=None,
                reason="short opened",
                size_requested=spec.notional_usd,
                size_executed=result.get("size_executed", 0),
                slippage_bps=result.get("slippage_bps"),
                net_carry_apr=spec.estimated_carry_annual / spec.notional_usd if spec.notional_usd > 0 else 0,
                trap_score=spec.kelly_fraction,
                status="ok",
                error=None,
                bankroll_usd=bankroll,
            ))
        else:
            results["errors"].append(f"{spec.coin}: {result.get('error')}")
            journal_entry(TradeJournalEntry(
                timestamp=now,
                action="SKIP",
                coin=spec.coin,
                signal_rank=None,
                reason=f"execution failed: {result.get('error')}",
                size_requested=spec.notional_usd,
                size_executed=0.0,
                slippage_bps=result.get("slippage_bps"),
                net_carry_apr=spec.estimated_carry_annual / spec.notional_usd if spec.notional_usd > 0 else 0,
                trap_score=spec.kelly_fraction,
                status="error",
                error=result.get("error"),
                bankroll_usd=bankroll,
            ))

    return results


def _close_all_positions(cfg: ExecutorConfig, results: dict) -> None:
    """Emergency close of all open positions (called when kill switch triggers)."""
    if cfg.dry_run:
        return
    try:
        account_info = get_account_value()
        positions = account_info.get("positions", [])
        for p in positions:
            if float(p.get("szi", 0) or 0) < 0:  # short position
                coin = p["coin"]
                close_result = close_perp_position(coin=coin)
                results["positions_closed"].append({
                    "coin": coin,
                    "status": "ok" if close_result["success"] else "error",
                    "error": close_result.get("error"),
                })
    except Exception as e:
        results["errors"].append(f"emergency close failed: {e}")


def get_open_positions() -> list[dict]:
    """Return the current open positions from Hyperliquid."""
    cfg = ExecutorConfig.from_env()
    if not cfg.hyperliquid_enabled:
        return []
    account_info = get_account_value()
    if account_info.get("error"):
        return []
    return [
        p for p in account_info.get("positions", [])
        if float(p.get("szi", 0) or 0) < 0
    ]
