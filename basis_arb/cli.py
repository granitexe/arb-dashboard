"""Command-line interface for basis_arb.

Reads LORIS_API_KEY from the environment (optional). Never reads or accepts a
private key, wallet, or any signing credential.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from .config import BasisArbConfig
from .pipeline import run_pipeline
from .report import render_table, write_json_report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m basis_arb",
        description="Read-only delta-neutral basis/funding-rate signals with a TGE-trap score. "
                    "Produces signals only; never places orders or touches a wallet.",
    )
    p.add_argument("--coins", help="comma-separated coins, e.g. BTC,ETH,SOL (overrides universe selection)")
    p.add_argument("--universe-size", type=int, help="number of coins to scan (default 30)")
    p.add_argument("--venues", help="comma-separated venues from binance,bybit,okx,hyperliquid")
    p.add_argument("--horizon-days", type=int, help="unlock look-ahead horizon (default 90)")
    p.add_argument("--basis-convergence-days", type=float, help="assumed basis convergence window (default 30)")
    p.add_argument("--lookback-days", type=int, help="lead/lag return lookback in days (default 7)")
    p.add_argument("--bar-interval", help="lead/lag bar interval, e.g. 1h (default 1h)")
    p.add_argument("--timeout", type=float, help="per-request timeout seconds (default 12)")
    p.add_argument("--retries", type=int, help="transient-error retries (default 2)")
    p.add_argument("--cache-dir", help="cache directory (default .cache/basis_arb)")
    p.add_argument("--no-cache", action="store_true", help="disable the on-disk cache")
    p.add_argument("--max-table-rows", type=int, help="max rows printed to stdout (default 50)")
    p.add_argument("--output-json", help="JSON report path (default basis_arb_signals.json)")
    p.add_argument("--hide-excluded", action="store_true", help="hide trap-excluded coins from the table")
    p.add_argument("--quiet", action="store_true", help="suppress progress logging on stderr")
    p.add_argument("--strict", action="store_true", help="exit non-zero if every data source fails")
    return p


def _config_from_args(args: argparse.Namespace) -> BasisArbConfig:
    manual = tuple(c.strip().upper() for c in args.coins.split(",") if c.strip()) if args.coins else None
    venues = tuple(v.strip().lower() for v in args.venues.split(",") if v.strip()) if args.venues else None
    return BasisArbConfig().with_overrides(
        manual_coins=manual,
        universe_mode="manual" if manual else None,
        universe_size=args.universe_size,
        venues=venues,
        unlock_horizon_days=args.horizon_days,
        basis_convergence_days=args.basis_convergence_days,
        lead_lag_lookback_days=args.lookback_days,
        lead_lag_bar_interval=args.bar_interval,
        request_timeout_seconds=args.timeout,
        request_retries=args.retries,
        cache_dir=args.cache_dir,
        cache_enabled=False if args.no_cache else None,
        max_table_rows=args.max_table_rows,
        output_json_path=args.output_json,
        show_excluded=False if args.hide_excluded else None,
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = _config_from_args(args)

    def progress(msg: str) -> None:
        if not args.quiet:
            print(msg, file=sys.stderr, flush=True)

    key = os.environ.get("LORIS_API_KEY")
    report = run_pipeline(cfg, key, progress)

    print(render_table(report, cfg))
    path = write_json_report(report, cfg.output_json_path)
    print(f"\nJSON report written to {path}", file=sys.stderr)

    if args.strict and report.sources and all(not m.ok for m in report.sources.values()):
        return 2
    return 0
