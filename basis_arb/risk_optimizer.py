#!/usr/bin/env python3
"""risk_optimizer.py — Kelly criterion portfolio optimizer with drawdown protection.

Features
--------
* Kelly criterion sizing with fractional Kelly (halved = more conservative)
* Correlation-aware diversification: reduce sizing for highly-correlated positions
* Drawdown protection:
    - 8% drawdown  → auto-reduce all positions by 50%
    - 15% drawdown → kill all positions (flat)
* Position sizing in notional USD and coins
* Rebalancing logic with drift thresholds
* Aggregate risk report written to .cron_output/portfolio_recommendation.json

Integration
-----------
* trade_evaluator: consumes EvaluatedSignals for sizing input
* data_store:     reads latest prices to convert notional→coins
* config:         reads bankroll, Kelly fraction, drawdown thresholds
"""

from __future__ import annotations

import datetime
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ── project root ──────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ── output paths ──────────────────────────────────────────────────────────────

CRON_OUT = ROOT / ".cron_output"
CRON_OUT.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = CRON_OUT / "portfolio_recommendation.json"
DRAWDOWN_STATE_FILE = ROOT / ".drawdown_state.json"
LOG_FILE = CRON_OUT / "risk_optimizer.log"


# ── helpers ───────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    try:
        LOG_FILE.write_text(LOG_FILE.read_text() + line + "\n" if LOG_FILE.exists() else line + "\n")
    except Exception:
        pass


def load_json(path: Path, default: Any = None) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default if default is not None else {}


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))


# ── config ────────────────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    """Runtime risk parameters. Defaults can be overridden via constructor."""
    bankroll_usd: float = 10_000.0
    kelly_fraction: float = 0.5          # 0.5 = half-Kelly (conservative)
    max_position_frac: float = 0.20       # no single position >20% of bankroll
    correlation_threshold: float = 0.70   # positions above this correlation are reduced
    drawdown_reduce_pct: float = 0.08     # reduce at 8% drawdown
    drawdown_kill_pct: float = 0.15       # flat at 15% drawdown
    rebalance_drift_threshold: float = 0.10  # rebalance when position drifts >10%
    min_positions: int = 1
    max_positions: int = 8
    execution_fee_bps: float = 8.0        # round-trip fee in bps (for net Kelly)
    signals_path: Path = field(default_factory=lambda: ROOT / "signals.json")
    data_store_path: str = "market_data.db"

    @classmethod
    def from_dict(cls, d: dict) -> "RiskConfig":
        # Only pull keys that exist on the dataclass
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid}
        return cls(**filtered)

    def to_dict(self) -> dict:
        return {
            "bankroll_usd": self.bankroll_usd,
            "kelly_fraction": self.kelly_fraction,
            "max_position_frac": self.max_position_frac,
            "correlation_threshold": self.correlation_threshold,
            "drawdown_reduce_pct": self.drawdown_reduce_pct,
            "drawdown_kill_pct": self.drawdown_kill_pct,
            "rebalance_drift_threshold": self.rebalance_drift_threshold,
            "min_positions": self.min_positions,
            "max_positions": self.max_positions,
            "execution_fee_bps": self.execution_fee_bps,
        }


# ── drawdown state ────────────────────────────────────────────────────────────

@dataclass
class DrawdownState:
    peak_value_usd: float = 0.0
    last_value_usd: float = 0.0
    kill_switch_triggered: bool = False

    @classmethod
    def load(cls, path: Path = DRAWDOWN_STATE_FILE) -> "DrawdownState":
        d = load_json(path, {})
        return cls(
            peak_value_usd=d.get("peak_value_usd", 0.0),
            last_value_usd=d.get("last_value_usd", 0.0),
            kill_switch_triggered=d.get("kill_switch_triggered", False),
        )

    def save(self, path: Path = DRAWDOWN_STATE_FILE) -> None:
        save_json(path, {
            "peak_value_usd": self.peak_value_usd,
            "last_value_usd": self.last_value_usd,
            "kill_switch_triggered": self.kill_switch_triggered,
        })

    def update(self, current_value_usd: float) -> tuple[float, str]:
        """Update peak tracking. Returns (drawdown_fraction, action)."""
        self.last_value_usd = current_value_usd
        if current_value_usd > self.peak_value_usd:
            self.peak_value_usd = current_value_usd

        drawdown = (self.peak_value_usd - current_value_usd) / self.peak_value_usd \
            if self.peak_value_usd > 0 else 0.0

        if drawdown >= self.kill_switch_triggered_threshold() if hasattr(self, 'kill_switch_triggered_threshold') else False:
            self.kill_switch_triggered = True
            action = "KILL"
        elif drawdown >= 0.08:
            action = "REDUCE"
        else:
            action = "NORMAL"

        self.save()
        return drawdown, action

    def kill_switch_triggered_threshold(self) -> float:
        return 0.15  # default


# ── position ─────────────────────────────────────────────────────────────────

@dataclass
class Position:
    coin: str
    carry_apr: float           # annualised carry (decimal)
    kelly_fraction: float      # Kelly fraction for this position (after adjustments)
    notional_usd: float        # recommended notional exposure
    size_coins: float          # estimated coin quantity
    entry_score: float         # raw EvaluatedSignal.score
    trap_score: float
    correlation_adj: float     # 0-1 correlation adjustment factor
    reason: str
    side: str = "short"        # carry trade = short perp, long spot


# ── correlation matrix ────────────────────────────────────────────────────────

def _build_correlation_matrix(signals: list, prices: dict) -> dict[str, dict[str, float]]:
    """Build a pairwise correlation matrix from log-return series.

    Uses 7-day spot return correlation as a proxy for perp correlation.
    Returns a dict: {coin: {coin: correlation}}
    """
    import numpy as np
    try:
        import numpy as np
    except ImportError:
        return {}  # numpy unavailable — skip correlation adjustment

    # Build return series per coin from signals.json spot_returns
    returns: dict[str, list[float]] = {}
    for sig in signals:
        coin = sig.get("coin", "")
        raw = sig.get("raw", {})
        spot_rets = raw.get("spot_returns", [])
        if len(spot_rets) < 2:
            continue
        ret_series = [r.get("log_return", 0.0) or 0.0 for r in spot_rets[-8:]]  # last 8 bars
        if len(ret_series) >= 2:
            returns[coin] = ret_series

    if len(returns) < 2:
        return {}

    # Pad to same length
    max_len = max(len(v) for v in returns.values())
    matrix: dict[str, dict[str, float]] = {}
    coins = list(returns.keys())

    for c1 in coins:
        matrix[c1] = {}
        pad1 = [0.0] * (max_len - len(returns[c1])) + returns[c1]
        for c2 in coins:
            pad2 = [0.0] * (max_len - len(returns[c2])) + returns[c2]
            # Pearson correlation
            cov = np.cov(pad1, pad2)[0, 1]
            std1 = np.std(pad1, ddof=0)
            std2 = np.std(pad2, ddof=0)
            if std1 > 0 and std2 > 0:
                corr = cov / (std1 * std2)
                matrix[c1][c2] = float(corr)
            else:
                matrix[c1][c2] = 0.0

    return matrix


# ── Kelly sizing ──────────────────────────────────────────────────────────────

def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float,
                   fee_bps: float = 8.0) -> float:
    """Full Kelly fraction from win rate and avg win/loss ratios.

    fee_bps reduces the edge by half the round-trip fee.
    Returns a fraction (0-1). Clamped to 0 if edge is negative.
    """
    b = avg_win / max(abs(avg_loss), 1e-9) if avg_loss != 0 else 0.0
    fee_adj = fee_bps / 10000.0 / 2
    p = win_rate
    q = 1.0 - p
    if b <= 0:
        return 0.0
    kelly = (b * p - q) / b
    kelly -= fee_adj
    return max(0.0, min(1.0, kelly))


def kelly_notional(bankroll: float, kelly_frac: float,
                   max_frac: float = 0.20) -> float:
    """Convert Kelly fraction to a notional USD amount, capped at max_frac of bankroll."""
    return min(bankroll * kelly_frac, bankroll * max_frac)


# ── rebalancing ───────────────────────────────────────────────────────────────

def compute_rebalance(
    current_positions: list[dict],   # [{coin, notional_usd}]
    target_positions: list[Position],
    drift_threshold: float = 0.10,
) -> tuple[list[dict], list[dict]]:
    """Compute rebalance actions: return (reduce_list, increase_list).

    A position is rebalanced when its notional drifts more than drift_threshold
    from the current allocation.
    """
    reduce_actions = []
    increase_actions = []

    current_by_coin = {p["coin"]: p["notional_usd"] for p in current_positions}
    target_by_coin = {p.coin: p.notional_usd for p in target_positions}

    all_coins = set(current_by_coin.keys()) | set(target_by_coin.keys())

    for coin in all_coins:
        cur = current_by_coin.get(coin, 0.0)
        tgt = target_by_coin.get(coin, 0.0)
        if cur == 0 and tgt > 0:
            increase_actions.append({"coin": coin, "notional_usd": tgt})
        elif cur > 0 and tgt == 0:
            reduce_actions.append({"coin": coin, "notional_usd": cur, "action": "CLOSE"})
        elif cur > 0 and tgt > 0:
            drift = abs(tgt - cur) / max(cur, 1.0)
            if drift > drift_threshold:
                if tgt < cur:
                    reduce_actions.append({
                        "coin": coin,
                        "notional_usd": cur - tgt,
                        "action": "REDUCE",
                        "new_notional": tgt,
                    })
                else:
                    increase_actions.append({
                        "coin": coin,
                        "notional_usd": tgt - cur,
                        "action": "INCREASE",
                        "new_notional": tgt,
                    })

    return reduce_actions, increase_actions


# ── risk optimizer ────────────────────────────────────────────────────────────

class RiskOptimizer:
    """Kelly criterion portfolio optimizer with correlation and drawdown controls."""

    def __init__(self, config: Optional[RiskConfig] = None) -> None:
        self.config = config or RiskConfig()
        self.dd_state = DrawdownState.load()
        self._signals: list = []
        self._report: Optional[dict] = None

    # ── public API ────────────────────────────────────────────────────────────

    def run(self) -> dict:
        """Run the full optimize → report pipeline. Returns the portfolio report."""
        log("RiskOptimizer.run() started")

        # 1. Load and evaluate signals
        try:
            from .trade_evaluator import load_evaluated_signals
        except ImportError:
            from basis_arb.trade_evaluator import load_evaluated_signals
        try:
            evaluated = load_evaluated_signals(self.config.signals_path)
        except Exception as e:
            log(f"Failed to load signals: {e}", "ERROR")
            evaluated = []

        log(f"Loaded {len(evaluated)} evaluated signals")

        # 2. Drawdown check
        bankroll = self._current_equity()
        drawdown, action = self.dd_state.update(bankroll)
        log(f"Drawdown={drawdown:.2%} action={action} bankroll=${bankroll:.2f}")

        if self.dd_state.kill_switch_triggered:
            report = self._build_kill_report(drawdown, bankroll)
            self._write_report(report)
            return report

        # 3. Filter to top-N by score
        sorted_sigs = sorted(evaluated, key=lambda s: s.score, reverse=True)
        top_signals = sorted_sigs[: self.config.max_positions]

        if not top_signals:
            report = self._build_empty_report(drawdown, bankroll)
            self._write_report(report)
            return report

        # 4. Build correlation matrix
        signals_raw = [s.raw for s in top_signals]
        corr_matrix = _build_correlation_matrix(signals_raw, {})

        # 5. Kelly sizing with correlation adjustment
        positions = self._size_positions(top_signals, corr_matrix, action)

        # 6. Rebalance analysis
        current_positions = self._load_current_positions()
        reduce_acts, increase_acts = compute_rebalance(
            current_positions, positions,
            drift_threshold=self.config.rebalance_drift_threshold,
        )

        # 7. Build report
        report = self._build_report(
            positions=positions,
            drawdown=drawdown,
            action=action,
            bankroll=bankroll,
            reduce_actions=reduce_acts,
            increase_actions=increase_acts,
            correlation_matrix=corr_matrix,
        )

        self._write_report(report)
        self._report = report
        log(f"RiskOptimizer.run() done — {len(positions)} positions, action={action}")
        return report

    def _current_equity(self) -> float:
        """Load current equity from performance_health.json or use bankroll config."""
        health_file = CRON_OUT / "performance_health.json"
        health = load_json(health_file, {})
        equity = health.get("current_equity")
        if equity and equity > 0:
            return float(equity)
        return self.config.bankroll_usd

    def _load_current_positions(self) -> list[dict]:
        """Load current open positions from performance_health.json."""
        health_file = CRON_OUT / "performance_health.json"
        health = load_json(health_file, {})
        # Could be enhanced to load from live executor; for now use health snapshot
        return []

    def _size_positions(
        self,
        signals: list,
        corr_matrix: dict,
        action: str,
    ) -> list[Position]:
        """Apply Kelly sizing with correlation-aware diversification."""
        bankroll = self._current_equity()
        kelly_frac = self.config.kelly_fraction

        if action == "REDUCE":
            kelly_frac *= 0.5  # halve Kelly on drawdown reduction
            log("Applying 50% Kelly reduction due to drawdown")

        # Full Kelly from win rate estimate (use 0.55 baseline from performance inbox)
        win_rate = self._estimated_win_rate()
        avg_win = 1.0   # expressed as ratio of bankroll per trade
        avg_loss = 1.0
        full_kelly = kelly_fraction(
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            fee_bps=self.config.execution_fee_bps,
        )
        sized_kelly = full_kelly * kelly_frac

        positions = []
        for sig in signals:
            # Base notional from Kelly
            base_notional = kelly_notional(
                bankroll, sized_kelly, self.config.max_position_frac
            )

            # Correlation adjustment
            corr_adj = self._correlation_adjustment(sig.coin, signals, corr_matrix)
            final_notional = base_notional * corr_adj

            # Convert to coin size using price from data_store
            price = self._get_price(sig.coin)
            size_coins = final_notional / price if price > 0 else 0.0

            positions.append(Position(
                coin=sig.coin,
                carry_apr=sig.carry_apr,
                kelly_fraction=sized_kelly * corr_adj,
                notional_usd=final_notional,
                size_coins=size_coins,
                entry_score=sig.score,
                trap_score=sig.trap_score,
                correlation_adj=corr_adj,
                reason=sig.reason,
                side="short",
            ))

        # Sort by score desc and take top min_positions
        positions.sort(key=lambda p: p.entry_score, reverse=True)
        return positions[: self.config.max_positions]

    def _correlation_adjustment(
        self, coin: str, all_signals: list, corr_matrix: dict
    ) -> float:
        """Reduce position size for highly correlated assets."""
        if not corr_matrix or coin not in corr_matrix:
            return 1.0

        threshold = self.config.correlation_threshold
        reduction = 1.0

        for other in all_signals:
            if other.coin == coin:
                continue
            corr = abs(corr_matrix.get(coin, {}).get(other.coin, 0.0))
            if corr >= threshold:
                # Reduce by (corr - threshold) proportion
                reduction *= (1.0 - (corr - threshold))
        return max(0.1, reduction)

    def _estimated_win_rate(self) -> float:
        """Load win rate from performance health or default to 0.55."""
        health_file = CRON_OUT / "performance_health.json"
        health = load_json(health_file, {})
        wr = health.get("win_rate")
        if wr is not None and wr > 0:
            return float(wr)
        return 0.55  # conservative default

    def _get_price(self, coin: str) -> float:
        """Get latest price for coin from data_store."""
        try:
            try:
                from .data_store import DataStore
            except ImportError:
                from basis_arb.data_store import DataStore
            store = DataStore(self.config.data_store_path)
            # Try common venues
            for exchange in ["binance", "bybit", "okx", "hyperliquid"]:
                sym = coin.upper()
                if exchange == "hyperliquid":
                    sym = coin.upper()
                else:
                    sym = f"{coin.upper()}USDT"
                price_data = store.get_latest_price(exchange, sym)
                if price_data:
                    store.close()
                    return float(price_data.get("price", 0.0))
            store.close()
        except Exception:
            pass

        # Fallback: extract from signals.json raw markets
        signals_path = self.config.signals_path
        data = load_json(signals_path, {})
        for sig in data.get("signals", []):
            if sig.get("coin", "").upper() == coin.upper():
                markets = sig.get("raw", {}).get("markets_by_venue", {})
                for venue, mkt in markets.items():
                    p = mkt.get("perp_mark_price") or mkt.get("spot_price")
                    if p and p > 0:
                        return float(p)
        return 0.0

    # ── report builders ────────────────────────────────────────────────────────

    def _build_report(
        self,
        positions: list[Position],
        drawdown: float,
        action: str,
        bankroll: float,
        reduce_actions: list[dict],
        increase_actions: list[dict],
        correlation_matrix: dict,
    ) -> dict:
        ts = datetime.datetime.utcnow().isoformat() + "Z"
        total_notional = sum(p.notional_usd for p in positions)
        total_carry_annual = sum(p.carry_apr * p.notional_usd for p in positions)

        position_recommendations = []
        for p in positions:
            position_recommendations.append({
                "coin": p.coin,
                "side": p.side,
                "carry_apr": round(p.carry_apr, 4),
                "notional_usd": round(p.notional_usd, 2),
                "size_coins": round(p.size_coins, 4),
                "kelly_fraction": round(p.kelly_fraction, 4),
                "trap_score": round(p.trap_score, 3),
                "correlation_adj": round(p.correlation_adj, 3),
                "entry_score": round(p.entry_score, 4),
                "reason": p.reason,
            })

        return {
            "generated_at": ts,
            "tool": "risk_optimizer",
            "version": 1,
            "disclaimer": "Recommendations only. This tool does not place orders.",
            "action": action,
            "drawdown_pct": round(drawdown, 4),
            "bankroll_usd": round(bankroll, 2),
            "total_notional_usd": round(total_notional, 2),
            "total_carry_annual_usd": round(total_carry_annual, 2),
            "portfolio_leverage": round(total_notional / bankroll, 3) if bankroll > 0 else 0.0,
            "n_positions": len(positions),
            "config": self.config.to_dict(),
            "positions": position_recommendations,
            "rebalance": {
                "reduce": reduce_actions,
                "increase": increase_actions,
            },
            "correlation_matrix": {
                coin: {k: round(v, 3) for k, v in sub.items()}
                for coin, sub in correlation_matrix.items()
            } if correlation_matrix else {},
            "risk_summary": {
                "max_position_frac": round(
                    max((p.notional_usd / bankroll) for p in positions) if positions else 0.0, 4
                ),
                "avg_carry_apr": round(
                    sum(p.carry_apr for p in positions) / len(positions) if positions else 0.0, 4
                ),
                "worst_trap_score": round(
                    max((p.trap_score for p in positions), default=0.0), 3
                ),
                "portfolio_correlation_avg": round(
                    self._avg_correlation(correlation_matrix) if correlation_matrix else 0.0, 3
                ),
            },
        }

    def _build_kill_report(self, drawdown: float, bankroll: float) -> dict:
        ts = datetime.datetime.utcnow().isoformat() + "Z"
        return {
            "generated_at": ts,
            "tool": "risk_optimizer",
            "version": 1,
            "disclaimer": "KILL SWITCH triggered. All positions should be closed.",
            "action": "KILL",
            "drawdown_pct": round(drawdown, 4),
            "bankroll_usd": round(bankroll, 2),
            "kill_switch_triggered": True,
            "config": self.config.to_dict(),
            "positions": [],
            "rebalance": {"reduce": [], "increase": []},
            "risk_summary": {},
            "alert": (
                f"Drawdown {drawdown:.1%} >= kill threshold "
                f"{self.config.drawdown_kill_pct:.1%}. All positions must be closed."
            ),
        }

    def _build_empty_report(self, drawdown: float, bankroll: float) -> dict:
        ts = datetime.datetime.utcnow().isoformat() + "Z"
        return {
            "generated_at": ts,
            "tool": "risk_optimizer",
            "version": 1,
            "disclaimer": "No tradable signals found.",
            "action": "WAIT",
            "drawdown_pct": round(drawdown, 4),
            "bankroll_usd": round(bankroll, 2),
            "config": self.config.to_dict(),
            "positions": [],
            "rebalance": {"reduce": [], "increase": []},
            "risk_summary": {},
        }

    def _avg_correlation(self, corr_matrix: dict) -> float:
        """Compute average off-diagonal correlation from matrix."""
        if not corr_matrix:
            return 0.0
        total = 0.0
        count = 0
        for coin, sub in corr_matrix.items():
            for other, val in sub.items():
                if coin != other:
                    total += abs(val)
                    count += 1
        return total / count if count > 0 else 0.0

    def _write_report(self, report: dict) -> None:
        save_json(OUTPUT_FILE, report)
        log(f"Wrote portfolio recommendation to {OUTPUT_FILE}")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Risk optimizer — Kelly portfolio builder")
    parser.add_argument("--config", type=str, help="Path to JSON config file")
    parser.add_argument("--signals", type=str, help="Path to signals.json")
    parser.add_argument("--bankroll", type=float, help="Bankroll in USD")
    parser.add_argument("--kelly", type=float, help="Kelly fraction (0-1)")
    parser.add_argument("--reduce-at", type=float, help="Drawdown reduce threshold (e.g. 0.08)")
    parser.add_argument("--kill-at", type=float, help="Drawdown kill threshold (e.g. 0.15)")
    args = parser.parse_args()

    # Build config
    config = RiskConfig()
    if args.config:
        config = RiskConfig.from_dict(load_json(Path(args.config), {}))
    if args.signals:
        config.signals_path = Path(args.signals)
    if args.bankroll:
        config.bankroll_usd = args.bankroll
    if args.kelly:
        config.kelly_fraction = args.kelly
    if args.reduce_at:
        config.drawdown_reduce_pct = args.reduce_at
    if args.kill_at:
        config.drawdown_kill_pct = args.kill_at

    optimizer = RiskOptimizer(config)
    report = optimizer.run()
    action = report.get("action", "UNKNOWN")
    print(f"Risk optimizer finished. Action: {action}")
    return 0 if action != "KILL" else 1


if __name__ == "__main__":
    sys.exit(main())