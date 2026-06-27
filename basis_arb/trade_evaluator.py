#!/usr/bin/env python3
"""trade_evaluator.py — Comprehensive trade evaluation with cost model, P&L analysis, and position sizing.

Provides TradeEvaluator class that:
  - Loads signals from signals.json (via data_store or direct file)
  - Computes full cost model: slippage, fees, funding, borrow
  - Renders a go/no-go verdict per signal
  - Break-even analysis: what rate/funding is needed to break even
  - P&L range: pessimistic / expected / optimistic / worst_case
  - Kelly-based position sizing
  - Writes structured JSONL to .cron_output/trade_evaluations.jsonl

Integrates with:
  - basis_arb.data_store.DataStore (optional, for historical context)
  - signals.json (primary input)
  - config params (execution_fee_bps, slippage_bps, funding_periods_per_day, etc.)
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ── paths ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
CRON_OUT = ROOT / ".cron_output"
CRON_OUT.mkdir(parents=True, exist_ok=True)
SIGNALS_PATH = ROOT / "signals.json"


# ── cost model constants ─────────────────────────────────────────────────────

@dataclass
class CostConfig:
    """Editable cost-model parameters. Sensible defaults; override via __init__ or env."""

    # Execution fees (decimal)
    execution_fee_bps_roundtrip: float = 8.0          # total roundtrip maker/taker bps
    slippage_bps: float = 5.0                          # expected slippage bps
    slippage_worst_bps: float = 20.0                   # worst-case slippage bps

    # Funding (funding_8h is per 8-hour period)
    funding_periods_per_day: float = 3.0              # 3 × 8h periods per day
    days_per_year: float = 365.0

    # Borrow (annualised rate for spot leg, if applicable)
    borrow_rate_annual: float = 0.05                   # 5% default borrow rate

    # Kelly multiplier (fraction of full Kelly to use; 0.5 = half-Kelly)
    kelly_multiplier: float = 0.25                     # conservative
    kelly_max_position_pct: float = 0.20               # max 20% of bankroll per trade

    # P&L scenario multipliers (applied to carry)
    scenario_pessimistic_mult: float = 0.50            # carry × 0.5
    scenario_expected_mult: float = 1.00               # carry × 1.0
    scenario_optimistic_mult: float = 1.50             # carry × 1.5
    scenario_worst_case_mult: float = 0.00             # carry × 0 (full loss of carry, flat)

    # Rejection thresholds
    min_carry_apr: float = 0.02                        # minimum 2% annualised carry
    max_oi_mcap_ratio: float = 0.40                   # reject OI/mcap > 40%
    min_volume_usd: float = 100_000.0                 # minimum 24h perp volume
    trap_score_reject: float = 0.75                    # reject trap score above this

    # Break-even horizon
    break_even_days: int = 30                          # days to use for break-even calc

    @classmethod
    def from_config_dict(cls, cfg: dict) -> "CostConfig":
        """Build CostConfig from a config dict (e.g. signals.json config section)."""
        c = cfg.get("config", {})
        return cls(
            execution_fee_bps_roundtrip=c.get("execution_fee_bps_roundtrip", 8.0),
            slippage_bps=c.get("hyperliquid_slippage_bps", 5.0),
            slippage_worst_bps=c.get("hyperliquid_max_slippage_bps", 20.0),
            funding_periods_per_day=c.get("funding_periods_per_day", 3.0),
            days_per_year=c.get("days_per_year", 365.0),
            borrow_rate_annual=c.get("borrow_rate_annual", 0.05),
            kelly_multiplier=c.get("kelly_multiplier", 0.25),
            kelly_max_position_pct=c.get("kelly_max_position_pct", 0.20),
            min_carry_apr=c.get("min_carry_apr", 0.02),
            max_oi_mcap_ratio=c.get("oi_market_cap_hard_ratio", 0.40),
            min_volume_usd=c.get("min_volume_floor_usd", 100_000.0),
            trap_score_reject=c.get("trap_exclusion_score", 0.75),
        )


# ── evaluation dataclasses ────────────────────────────────────────────────────

@dataclass
class CostBreakdown:
    """Itemised cost components for one signal evaluation."""
    slippage_bps: float
    slippage_worst_bps: float
    fees_bps: float
    borrow_rate_apr: float
    borrow_cost_pct: float
    funding_cost_pct: float
    net_cost_pct: float
    breakeven_funding_8h: float
    breakeven_carry_apr: float
    slippage_dollar: float
    fees_dollar: float
    borrow_dollar: float
    funding_dollar: float


@dataclass
class PLRange:
    """P&L range across four scenarios."""
    pessimistic_pnl_pct: float
    expected_pnl_pct: float
    optimistic_pnl_pct: float
    worst_case_pnl_pct: float
    pessimistic_pnl_dollar: float
    expected_pnl_dollar: float
    optimistic_pnl_dollar: float
    worst_case_pnl_dollar: float


@dataclass
class PositionSizing:
    """Kelly-based position sizing result."""
    kelly_fraction: float
    adjusted_fraction: float
    max_fraction: float
    recommended_size_usd: float
    risk_units: float


@dataclass
class TradeEvaluation:
    """Complete per-signal evaluation result."""
    coin: str
    evaluated_at: str
    carry_apr: float
    trap_score: float
    oi_market_cap_ratio: float
    perp_volume_usd: float
    funding_rate_8h: float
    perp_premium: float
    cost: CostBreakdown
    go_no_go: str
    verdict_reasons: list[str]
    score: float
    break_even_days: int
    breakeven_funding_8h: float
    breakeven_carry_apr: float
    pnl: PLRange
    sizing: PositionSizing
    signal_rank: int
    raw: dict = field(default_factory=dict)


# ── cost model ───────────────────────────────────────────────────────────────

def compute_cost_model(
    funding_8h: float,
    perp_premium: float,
    entry_price: float,
    position_size_usd: float,
    horizon_days: int,
    cfg: CostConfig,
) -> CostBreakdown:
    """
    Compute itemised cost breakdown for a basis-arbitrage trade.

    Parameters
    ----------
    funding_8h  : current 8-hour funding rate (decimal, e.g. 0.0001 = 0.01%)
    perp_premium: current perpetual premium vs spot (decimal, e.g. -0.001 = -0.1%)
    entry_price : estimated entry price (used for slippage dollar calc)
    position_size_usd : notional position size in USD
    horizon_days : expected hold horizon in days
    cfg          : CostConfig with fee/slippage parameters

    Returns
    -------
    CostBreakdown
    """
    # Slippage
    slippage_bps = cfg.slippage_bps
    slippage_worst_bps = cfg.slippage_worst_bps
    slippage_dollar = position_size_usd * (slippage_bps / 10_000)
    slippage_worst_dollar = position_size_usd * (slippage_worst_bps / 10_000)

    # Execution fees
    fees_bps = cfg.execution_fee_bps_roundtrip
    fees_dollar = position_size_usd * (fees_bps / 10_000)

    # Borrow cost (annualised, applied to spot notional for the horizon)
    borrow_rate_apr = cfg.borrow_rate_annual
    borrow_cost_pct = borrow_rate_apr * (horizon_days / cfg.days_per_year)
    borrow_dollar = position_size_usd * borrow_cost_pct

    # Funding cost (we receive if funding_8h > 0, pay if negative)
    periods = horizon_days * cfg.funding_periods_per_day
    # Net funding: we earn the positive funding AND pay the negative funding (premium)
    net_funding_8h = funding_8h + perp_premium
    funding_cost_pct = net_funding_8h * periods
    funding_dollar = position_size_usd * funding_cost_pct

    # Total net cost
    net_cost_pct = (
        slippage_bps / 10_000
        + fees_bps / 10_000
        + borrow_cost_pct
        + funding_cost_pct
    )

    # Break-even: what funding_8h just covers costs?
    non_funding_cost_pct = (
        slippage_bps / 10_000
        + fees_bps / 10_000
        + borrow_cost_pct
    )
    breakeven_funding_8h = (
        -(non_funding_cost_pct / periods) - perp_premium
        if periods > 0 else 0.0
    )
    breakeven_carry_apr = (
        breakeven_funding_8h * cfg.funding_periods_per_day * cfg.days_per_year
    )

    return CostBreakdown(
        slippage_bps=slippage_bps,
        slippage_worst_bps=slippage_worst_bps,
        fees_bps=fees_bps,
        borrow_rate_apr=borrow_rate_apr,
        borrow_cost_pct=borrow_cost_pct,
        funding_cost_pct=funding_cost_pct,
        net_cost_pct=net_cost_pct,
        breakeven_funding_8h=breakeven_funding_8h,
        breakeven_carry_apr=breakeven_carry_apr,
        slippage_dollar=slippage_dollar,
        fees_dollar=fees_dollar,
        borrow_dollar=borrow_dollar,
        funding_dollar=funding_dollar,
    )


# ── P&L range ─────────────────────────────────────────────────────────────────

def compute_pnl_range(
    carry_apr: float,
    cost: CostBreakdown,
    position_size_usd: float,
    horizon_days: int,
    cfg: CostConfig,
) -> PLRange:
    """
    Compute P&L range across four scenarios.

    pessimistic : carry at 50% efficiency
    expected    : carry as estimated
    optimistic  : carry at 150% efficiency
    worst_case  : carry fully reverses, worst-case slippage + all fees
    """
    carry_pct = carry_apr * (horizon_days / cfg.days_per_year)

    def pnl_for(carry_mult: float, slippage_mult: float = 1.0) -> tuple[float, float]:
        earned = carry_pct * carry_mult
        cost_pct = (
            cost.slippage_bps / 10_000 * slippage_mult
            + cost.fees_bps / 10_000
            + cost.borrow_cost_pct
            + cost.funding_cost_pct
        )
        pnl_pct = earned - cost_pct
        pnl_dollar = position_size_usd * pnl_pct
        return pnl_pct, pnl_dollar

    pess_pct, pess_dol = pnl_for(cfg.scenario_pessimistic_mult)
    exp_pct, exp_dol = pnl_for(cfg.scenario_expected_mult)
    opt_pct, opt_dol = pnl_for(cfg.scenario_optimistic_mult)
    wc_pct, wc_dol = pnl_for(0.0, slippage_mult=2.0)

    return PLRange(
        pessimistic_pnl_pct=pess_pct,
        expected_pnl_pct=exp_pct,
        optimistic_pnl_pct=opt_pct,
        worst_case_pnl_pct=wc_pct,
        pessimistic_pnl_dollar=pess_dol,
        expected_pnl_dollar=exp_dol,
        optimistic_pnl_dollar=opt_dol,
        worst_case_pnl_dollar=wc_dol,
    )


# ── position sizing ───────────────────────────────────────────────────────────

def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Classic Kelly Criterion: f* = (p·W - q·L) / W
    where p = win_rate, W = avg_win/loss ratio, q = 1-p.
    Returns fraction of bankroll.
    """
    if avg_loss <= 0 or avg_win <= 0 or win_rate <= 0:
        return 0.0
    b = avg_win / avg_loss
    if b == 0:
        return 0.0
    q = 1.0 - win_rate
    f = (b * win_rate - q) / b
    return max(0.0, min(1.0, f))


def compute_position_sizing(
    carry_apr: float,
    cost: CostBreakdown,
    horizon_days: int,
    bankroll_usd: float,
    cfg: CostConfig,
) -> PositionSizing:
    """
    Compute Kelly-based position size.

    Uses conservative assumptions when empirical data isn't available:
      - win_rate = 0.65 (historical carry-win rate estimate)
      - avg_loss includes slippage + fees + borrow + worst-case funding
    """
    win_rate = 0.65
    avg_win_pct = (
        carry_apr
        * (horizon_days / cfg.days_per_year)
        * cfg.scenario_expected_mult
    )
    avg_loss_pct = (
        cost.slippage_bps / 10_000
        + cost.fees_bps / 10_000
        + cost.borrow_cost_pct
        + abs(cost.funding_cost_pct)
    )

    kelly = kelly_fraction(win_rate, avg_win_pct, avg_loss_pct)
    adjusted = kelly * cfg.kelly_multiplier
    max_frac = cfg.kelly_max_position_pct
    final_frac = min(adjusted, max_frac)

    recommended_size_usd = final_frac * bankroll_usd
    risk_units = avg_win_pct / avg_loss_pct if avg_loss_pct > 0 else 0.0

    return PositionSizing(
        kelly_fraction=kelly,
        adjusted_fraction=adjusted,
        max_fraction=max_frac,
        recommended_size_usd=recommended_size_usd,
        risk_units=risk_units,
    )


# ── TradeEvaluator ────────────────────────────────────────────────────────────

class TradeEvaluator:
    """
    Evaluate individual trade signals with full cost model, P&L range,
    go/no-go verdict, break-even analysis, and Kelly position sizing.

    Integrates with:
      - signals.json (signals input)
      - DataStore (optional historical funding data)
      - CostConfig (cost model parameters)
    """

    def __init__(
        self,
        signals_path: str | Path | None = None,
        db_path: str | Path | None = None,
        bankroll_usd: float = 100_000.0,
        horizon_days: int = 30,
        cfg: CostConfig | None = None,
        config_dict: dict | None = None,
    ) -> None:
        self.signals_path = Path(signals_path) if signals_path else SIGNALS_PATH
        self.db_path = Path(db_path) if db_path else None
        self.bankroll_usd = bankroll_usd
        self.horizon_days = horizon_days

        if cfg is not None:
            self.cfg = cfg
        elif config_dict is not None:
            self.cfg = CostConfig.from_config_dict(config_dict)
        else:
            self.cfg = CostConfig()

        self._store: Optional[Any] = None
        if self.db_path and self.db_path.exists():
            try:
                from basis_arb.data_store import DataStore
                self._store = DataStore(str(self.db_path))
            except Exception:
                pass

    # ── signal loading ───────────────────────────────────────────────────────

    def load_signals(self) -> list[dict]:
        """Load raw signals list from signals.json."""
        if not self.signals_path.exists():
            return []
        try:
            data = json.loads(self.signals_path.read_text())
            return data.get("signals", [])
        except Exception:
            return []

    def _extract_signal_metrics(self, sig: dict) -> dict:
        """Extract OI, volume, funding, premium from a signal dict."""
        coin = sig.get("coin", "UNKNOWN")
        carry = sig.get("carry", {})
        trap = sig.get("trap", {})
        raw = sig.get("raw", {})

        carry_apr = carry.get("net_carry_apr") or carry.get("total_carry_apr", 0.0)
        trap_score = trap.get("composite_score", 0.0)
        oi_mcap_ratio = 0.0
        perp_volume_usd = 0.0
        funding_8h = 0.0
        perp_premium = 0.0
        entry_price = 0.0

        markets = raw.get("markets_by_venue", {})
        mcap = raw.get("market_cap_usd", 1.0) or 1.0
        funding_by_venue = raw.get("funding_by_venue", {})

        for venue, mkt in markets.items():
            oi_usd = mkt.get("perp_open_interest_usd", 0.0) or 0.0
            if mcap > 0:
                ratio = oi_usd / mcap
                if ratio > oi_mcap_ratio:
                    oi_mcap_ratio = ratio
            vol = mkt.get("perp_daily_volume_usd", 0.0) or 0.0
            perp_volume_usd = max(perp_volume_usd, vol)
            prem = mkt.get("perp_premium")
            if prem is not None:
                perp_premium = prem
            ep = mkt.get("perp_mark_price")
            if ep and ep > 0:
                entry_price = ep
            fund = funding_by_venue.get(venue, {})
            f8h = fund.get("funding_8h_decimal")
            if f8h is not None:
                funding_8h = f8h
            if funding_8h == 0.0:
                f_apr = mkt.get("funding_apr")
                if f_apr is not None and self.cfg.funding_periods_per_day > 0:
                    funding_8h = f_apr / (
                        self.cfg.funding_periods_per_day * self.cfg.days_per_year
                    )

        if carry_apr == 0.0 and funding_8h != 0.0:
            carry_apr = (
                funding_8h
                * self.cfg.funding_periods_per_day
                * self.cfg.days_per_year
            )

        return {
            "coin": coin,
            "carry_apr": float(carry_apr) if carry_apr is not None else 0.0,
            "trap_score": float(trap_score),
            "oi_market_cap_ratio": float(oi_mcap_ratio),
            "perp_volume_usd": float(perp_volume_usd),
            "funding_8h": float(funding_8h),
            "perp_premium": float(perp_premium),
            "entry_price": float(entry_price) if entry_price > 0 else 1.0,
            "rank": sig.get("rank", 0),
        }

    # ── per-signal evaluation ─────────────────────────────────────────────────

    def evaluate_signal(self, sig: dict) -> Optional[TradeEvaluation]:
        """Evaluate a single signal dict and return a TradeEvaluation."""
        m = self._extract_signal_metrics(sig)
        coin = m["coin"]
        rank = m["rank"]

        excluded = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDD", "BUSD", "USD", "USDE"}
        if coin in excluded:
            return None

        carry_apr = m["carry_apr"]
        trap_score = m["trap_score"]
        oi_mcap_ratio = m["oi_market_cap_ratio"]
        perp_volume_usd = m["perp_volume_usd"]
        funding_8h = m["funding_8h"]
        perp_premium = m["perp_premium"]
        entry_price = m["entry_price"]

        reference_size = self.bankroll_usd * 0.01  # 1% of bankroll as reference notional

        cost = compute_cost_model(
            funding_8h=funding_8h,
            perp_premium=perp_premium,
            entry_price=entry_price,
            position_size_usd=reference_size,
            horizon_days=self.horizon_days,
            cfg=self.cfg,
        )

        pnl = compute_pnl_range(
            carry_apr=carry_apr,
            cost=cost,
            position_size_usd=reference_size,
            horizon_days=self.horizon_days,
            cfg=self.cfg,
        )

        sizing = compute_position_sizing(
            carry_apr=carry_apr,
            cost=cost,
            horizon_days=self.horizon_days,
            bankroll_usd=self.bankroll_usd,
            cfg=self.cfg,
        )

        verdict_reasons: list[str] = []
        go_no_go = "GO"

        if trap_score >= self.cfg.trap_score_reject:
            verdict_reasons.append(
                f"trap={trap_score:.3f}>={self.cfg.trap_score_reject}"
            )
            go_no_go = "NO_GO"
        if carry_apr < self.cfg.min_carry_apr:
            verdict_reasons.append(
                f"carry_apr={carry_apr:.2%}<min={self.cfg.min_carry_apr:.2%}"
            )
            if go_no_go != "NO_GO":
                go_no_go = "MARGINAL"
        if oi_mcap_ratio > self.cfg.max_oi_mcap_ratio:
            verdict_reasons.append(
                f"oi_mcap={oi_mcap_ratio:.1%}>max={self.cfg.max_oi_mcap_ratio:.1%}"
            )
            if go_no_go != "NO_GO":
                go_no_go = "NO_GO"
        if perp_volume_usd < self.cfg.min_volume_usd:
            verdict_reasons.append(
                f"vol=${perp_volume_usd/1e6:.1f}M<${self.cfg.min_volume_usd/1e6:.0f}M"
            )
            if go_no_go != "NO_GO":
                go_no_go = "MARGINAL"
        net_carry = carry_apr - cost.breakeven_carry_apr
        if carry_apr > 0 and net_carry < 0:
            verdict_reasons.append(f"net_carry={net_carry:.2%}<0 (insufficient funding)")

        trap_adj = max(0.0, 1.0 - trap_score) ** 2
        vol_adj = min(
            1.0,
            math.sqrt(max(0.0, perp_volume_usd) / max(1.0, self.cfg.min_volume_usd)),
        )
        score = carry_apr * trap_adj * vol_adj

        if not verdict_reasons:
            verdict_reasons.append("all checks passed")

        return TradeEvaluation(
            coin=coin,
            evaluated_at=datetime.utcnow().isoformat() + "Z",
            carry_apr=carry_apr,
            trap_score=trap_score,
            oi_market_cap_ratio=oi_mcap_ratio,
            perp_volume_usd=perp_volume_usd,
            funding_rate_8h=funding_8h,
            perp_premium=perp_premium,
            cost=cost,
            go_no_go=go_no_go,
            verdict_reasons=verdict_reasons,
            score=score,
            break_even_days=self.horizon_days,
            breakeven_funding_8h=cost.breakeven_funding_8h,
            breakeven_carry_apr=cost.breakeven_carry_apr,
            pnl=pnl,
            sizing=sizing,
            signal_rank=rank,
            raw=sig,
        )

    # ── batch evaluation ──────────────────────────────────────────────────────

    def evaluate_all(self) -> list[TradeEvaluation]:
        """Load all signals and return evaluated TradeEvaluations."""
        signals = self.load_signals()
        results = []
        for sig in signals:
            ev = self.evaluate_signal(sig)
            if ev is not None:
                results.append(ev)
        return results

    # ── serialisation ──────────────────────────────────────────────────────────

    @staticmethod
    def _to_primitives(ev: TradeEvaluation) -> dict:
        """Convert TradeEvaluation to a plain dict for JSON serialisation."""
        return {
            "coin": ev.coin,
            "evaluated_at": ev.evaluated_at,
            "carry_apr": ev.carry_apr,
            "trap_score": ev.trap_score,
            "oi_market_cap_ratio": ev.oi_market_cap_ratio,
            "perp_volume_usd": ev.perp_volume_usd,
            "funding_rate_8h": ev.funding_rate_8h,
            "perp_premium": ev.perp_premium,
            "cost": {
                "slippage_bps": ev.cost.slippage_bps,
                "slippage_worst_bps": ev.cost.slippage_worst_bps,
                "fees_bps": ev.cost.fees_bps,
                "borrow_rate_apr": ev.cost.borrow_rate_apr,
                "borrow_cost_pct": ev.cost.borrow_cost_pct,
                "funding_cost_pct": ev.cost.funding_cost_pct,
                "net_cost_pct": ev.cost.net_cost_pct,
                "breakeven_funding_8h": ev.cost.breakeven_funding_8h,
                "breakeven_carry_apr": ev.cost.breakeven_carry_apr,
                "slippage_dollar": ev.cost.slippage_dollar,
                "fees_dollar": ev.cost.fees_dollar,
                "borrow_dollar": ev.cost.borrow_dollar,
                "funding_dollar": ev.cost.funding_dollar,
            },
            "go_no_go": ev.go_no_go,
            "verdict_reasons": ev.verdict_reasons,
            "score": ev.score,
            "break_even_days": ev.break_even_days,
            "breakeven_funding_8h": ev.breakeven_funding_8h,
            "breakeven_carry_apr": ev.breakeven_carry_apr,
            "pnl": {
                "pessimistic_pnl_pct": ev.pnl.pessimistic_pnl_pct,
                "expected_pnl_pct": ev.pnl.expected_pnl_pct,
                "optimistic_pnl_pct": ev.pnl.optimistic_pnl_pct,
                "worst_case_pnl_pct": ev.pnl.worst_case_pnl_pct,
                "pessimistic_pnl_dollar": ev.pnl.pessimistic_pnl_dollar,
                "expected_pnl_dollar": ev.pnl.expected_pnl_dollar,
                "optimistic_pnl_dollar": ev.pnl.optimistic_pnl_dollar,
                "worst_case_pnl_dollar": ev.pnl.worst_case_pnl_dollar,
            },
            "sizing": {
                "kelly_fraction": ev.sizing.kelly_fraction,
                "adjusted_fraction": ev.sizing.adjusted_fraction,
                "max_fraction": ev.sizing.max_fraction,
                "recommended_size_usd": ev.sizing.recommended_size_usd,
                "risk_units": ev.sizing.risk_units,
            },
            "signal_rank": ev.signal_rank,
        }

    def write_jsonl(
        self,
        evaluations: list[TradeEvaluation],
        output_path: Path | None = None,
    ) -> Path:
        """Write evaluations as newline-delimited JSON to .cron_output/trade_evaluations.jsonl."""
        path = output_path or (CRON_OUT / "trade_evaluations.jsonl")
        with open(path, "w") as f:
            for ev in evaluations:
                f.write(json.dumps(self._to_primitives(ev), default=str) + "\n")
        return path

    def summarize(self, evaluations: list[TradeEvaluation]) -> str:
        """Build a human-readable summary string."""
        go = [e for e in evaluations if e.go_no_go == "GO"]
        no_go = [e for e in evaluations if e.go_no_go == "NO_GO"]
        marginal = [e for e in evaluations if e.go_no_go == "MARGINAL"]

        lines = [
            f"Trade Evaluation Summary ({datetime.utcnow().isoformat()}Z)",
            f"  Total evaluated : {len(evaluations)}",
            f"  GO              : {len(go)}",
            f"  MARGINAL        : {len(marginal)}",
            f"  NO_GO           : {len(no_go)}",
            "",
        ]
        if go:
            lines.append("  GO signals (top 5 by score):")
            for e in sorted(go, key=lambda x: x.score, reverse=True)[:5]:
                lines.append(
                    f"    {e.coin:<10} carry={e.carry_apr:.2%}  "
                    f"funding_8h={e.funding_rate_8h:.4f}  "
                    f"score={e.score:.4f}  "
                    f"size=${e.sizing.recommended_size_usd:,.0f}"
                )
        if marginal:
            lines.append("  MARGINAL signals:")
            for e in marginal[:3]:
                bad = [r for r in e.verdict_reasons if r != "all checks passed"]
                lines.append(f"    {e.coin:<10} {bad}")
        return "\n".join(lines)


# ── convenience loaders ──────────────────────────────────────────────────────

def load_config_dict(signals_path: Path | None = None) -> dict:
    """Load the config section from signals.json."""
    p = signals_path or SIGNALS_PATH
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def run_evaluation(
    signals_path: str | Path | None = None,
    db_path: str | Path | None = None,
    output_path: str | Path | None = None,
    bankroll_usd: float = 100_000.0,
    horizon_days: int = 30,
) -> tuple[list[TradeEvaluation], Path]:
    """
    Run the full trade evaluation pipeline.

    Returns (evaluations, output_path).
    """
    config_dict = load_config_dict(
        Path(signals_path) if signals_path else None
    )
    evaluator = TradeEvaluator(
        signals_path=signals_path,
        db_path=db_path,
        bankroll_usd=bankroll_usd,
        horizon_days=horizon_days,
        config_dict=config_dict,
    )
    evaluations = evaluator.evaluate_all()
    out_path = evaluator.write_jsonl(
        evaluations,
        Path(output_path) if output_path else None,
    )
    print(evaluator.summarize(evaluations))
    return evaluations, out_path


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Trade Evaluator — cost model, P&L range, position sizing"
    )
    parser.add_argument("--signals", type=str, default=None, help="Path to signals.json")
    parser.add_argument("--db", type=str, default=None, help="Path to market_data.db")
    parser.add_argument("--bankroll", type=float, default=100_000.0, help="Bankroll in USD")
    parser.add_argument("--horizon", type=int, default=30, help="Evaluation horizon in days")
    parser.add_argument("--output", type=str, default=None, help="Output JSONL path")
    args = parser.parse_args()

    evals, path = run_evaluation(
        signals_path=args.signals,
        db_path=args.db,
        output_path=args.output,
        bankroll_usd=args.bankroll,
        horizon_days=args.horizon,
    )
    print(f"\nWritten {len(evals)} evaluations to {path}")
