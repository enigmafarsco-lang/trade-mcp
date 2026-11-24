#!/usr/bin/env python3
"""Read-only historical M1 replay & broker-spec audit for Day Trader M1.

Strictly read-only against MT5: uses only terminal_get_info, get_time_information,
get_marketwatch_symbols, and get_chart_history over loopback HTTP. Never submits
orders, never reads account history, never modifies terminal settings, and never
calls a cloud model.

Compares baseline (no floating guard) vs floating small-profit guard across
multiple target USD levels ($2, $4, $6, $10 vs -$10 stop) on recent closed M1
bars, and audits trade_contract_size vs trade_tick_value/trade_tick_size.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_m1_paper_window import (  # noqa: E402
    ReadOnlyNativeFeed,
    _save_private_json,
)
from daytrade_mt5_readonly_check import Blocked, loopback_native  # noqa: E402

from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.daytrade_paper import (  # noqa: E402
    HARD_MAX_LOT,
    HARD_MAX_STOP_USD,
    MAX_TARGET_USD,
    MIN_TARGET_USD,
    audit_symbol_spec,
    contiguous_m1_segments,
    replay_m1_matrix,
    resolve_pricing_basis,
)
from trade_mcp.mcpclient import McpError  # noqa: E402


def _mock_history(symbol: str) -> tuple[dict[str, Any], dict[str, float], list[list[float]]]:
    """Deterministic 80-bar M1 dataset with breakouts, stalls, pullbacks, and reversals."""
    spec: dict[str, Any] = {
        "trade_tick_size": 0.01,
        "trade_tick_value": 1.0,
        "trade_contract_size": 100.0,
        "currency_profit": "USD",
        "trade_calc_mode": "cfd",
        "digits": 2,
        "volume_step": 0.01,
        "volume_min": 0.01,
        "volume_max": 10.0,
        "point": 0.01,
        "trade_stops_level": 5.0,
    }
    base = 1700000040
    # Synthetic M1 sequence exercising bullish breakout -> target, stalled pullback,
    # reversal breakdown -> target, and adverse move -> stop.
    prices = [
        2650.0, 2650.2, 2650.1, 2650.3, 2650.2,
        2651.2, 2651.8, 2652.5, 2652.4, 2652.3,
        2652.4, 2652.5, 2653.4, 2653.7, 2653.5,
        2653.6, 2653.5, 2653.4, 2653.5, 2652.5,
        2651.8, 2651.0, 2651.1, 2651.2, 2651.0,
        2650.2, 2649.5, 2649.8, 2650.0, 2650.1,
        2651.0, 2650.3, 2649.2, 2648.4, 2648.6,
        2648.7, 2648.8, 2649.6, 2650.2, 2650.9,
    ]
    bars: list[list[float]] = []
    for idx, close in enumerate(prices):
        prev = prices[idx - 1] if idx > 0 else close
        high = max(prev, close) + 0.35
        low = min(prev, close) - 0.35
        bars.append([base + idx * 60, prev, high, low, close, 120.0])
    quote = {"bid": prices[-1], "ask": round(prices[-1] + 0.25, 2)}
    return spec, quote, bars


def _parse_targets(raw: str) -> list[float]:
    values: list[float] = []
    for part in raw.split(","):
        text = part.strip()
        if not text:
            continue
        val = float(text)
        if not (MIN_TARGET_USD <= val <= MAX_TARGET_USD):
            raise ValueError(f"Target {val} outside {MIN_TARGET_USD}..{MAX_TARGET_USD}")
        values.append(val)
    if not values:
        raise ValueError("At least one target USD value is required")
    return values


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="XAUUSD_i", help="exact broker symbol (default: XAUUSD_i)")
    p.add_argument("--minutes", type=int, default=1440,
                   help="history lookback in minutes (60..1440, default 1440 = 24h)")
    p.add_argument("--limit", type=int, default=1450,
                   help="max M1 bars to request (50..1499, default 1450)")
    p.add_argument("--targets", default="2,4,6,10",
                   help="comma-separated target USD values in 2..10 (default: 2,4,6,10)")
    p.add_argument("--stop-usd", type=float, default=10.0,
                   help=f"max planned stop loss USD (1..{HARD_MAX_STOP_USD}, default 10.0)")
    p.add_argument("--opening-range-bars", type=int, default=3,
                   help="closed M1 bars forming breakout range (2..15, default 3)")
    p.add_argument("--max-lot", type=float, default=HARD_MAX_LOT,
                   help=f"hard paper lot cap (<= {HARD_MAX_LOT}, default {HARD_MAX_LOT})")
    p.add_argument("--commission-per-lot", type=float, default=0.0,
                   help="round-trip commission USD/lot assumption (default 0.0)")
    p.add_argument("--spread-price", type=float, default=None,
                   help="override spread in price units (default: live observed ask - bid)")
    p.add_argument("--out", default="", help="optional .json filename inside ignored logs/")
    p.add_argument("--mock", action="store_true", help="run offline smoke test with synthetic M1 bars")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.symbol):
        raise SystemExit("Invalid symbol format")
    if not 60 <= args.minutes <= 1440 or not 50 <= args.limit <= 1499:
        raise SystemExit("minutes must be 60..1440 and limit must be 50..1499")

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    out_path = Path(args.out) if args.out else Path("logs") / f"daytrade-replay-{uuid.uuid4().hex[:12]}.json"

    try:
        targets = _parse_targets(args.targets)
        tool_calls: dict[str, int] | None = None
        if args.mock:
            spec, quote, bars = _mock_history(args.symbol)
            closed_bars = bars
        else:
            load_dotenv()
            cfg = Config.from_env()
            if cfg.backend != "native":
                raise Blocked("native_backend_required")
            with loopback_native():
                feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, args.symbol)
                spec, quote = feed.connect()
                raw_bars = feed.fetch_m1_bars(minutes=args.minutes, limit=args.limit)
                tool_calls = dict(feed.tool_calls)
            # Drop final forming bar so every replayed M1 candle is closed.
            closed_bars = raw_bars[:-1] if len(raw_bars) > 1 else raw_bars

        spread_price = (
            float(args.spread_price)
            if args.spread_price is not None
            else round(max(0.0, quote["ask"] - quote["bid"]), 6)
        )
        spec_audit = audit_symbol_spec(spec)
        primary_basis, primary_multiplier = resolve_pricing_basis(spec, "auto")
        segments = contiguous_m1_segments(closed_bars)

        matrix_primary = replay_m1_matrix(
            closed_bars,
            spec=spec,
            symbol=args.symbol,
            spread_price=spread_price,
            targets=targets,
            stop_usd=args.stop_usd,
            opening_range_bars=args.opening_range_bars,
            max_lot=args.max_lot,
            commission_per_lot=args.commission_per_lot,
            pricing_basis=primary_basis,
        )

        alternate_basis: str | None = None
        matrix_alternate: list[dict[str, Any]] | None = None
        if spec_audit.get("economics_mismatch") is True:
            alternate_basis = "tick_value" if primary_basis == "contract_size" else "contract_size"
            matrix_alternate = replay_m1_matrix(
                closed_bars,
                spec=spec,
                symbol=args.symbol,
                spread_price=spread_price,
                targets=targets,
                stop_usd=args.stop_usd,
                opening_range_bars=args.opening_range_bars,
                max_lot=args.max_lot,
                commission_per_lot=args.commission_per_lot,
                pricing_basis=alternate_basis,
            )

        warnings = [
            "Read-only historical M1 OHLC replay: zero broker orders were created or submitted.",
            "MT5 SymbolInfo does not expose per-lot broker commission; verify commission in MT5 Specification or deal history.",
            "Intrabar tick order is approximated conservatively from M1 OHLC (SL checked before TP on ambiguous bars).",
            "Constant spread assumption applied from observed quote; real spreads widen around news/rollovers.",
            "Historical replay does not prove future profitability or authorize broker execution.",
        ]
        if args.commission_per_lot == 0.0:
            warnings.append("Commission modeled as 0.0 USD/lot (simulation assumption, not verified broker commission).")
        if spec_audit.get("warning"):
            warnings.append(str(spec_audit["warning"]))

        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": "historical_m1_ohlc_replay_matrix",
            "executable": False,
            "execution_authorized": False,
            "orders_submitted": 0,
            "symbol": args.symbol,
            "live_quote": {
                "bid": quote["bid"],
                "ask": quote["ask"],
                "observed_spread_price": round(quote["ask"] - quote["bid"], 6),
                "modeled_spread_price": spread_price,
            },
            "symbol_spec": {
                "trade_tick_size": spec.get("trade_tick_size"),
                "trade_tick_value": spec.get("trade_tick_value"),
                "trade_contract_size": spec.get("trade_contract_size"),
                "currency_profit": spec.get("currency_profit"),
                "trade_calc_mode": spec.get("trade_calc_mode"),
                "digits": spec.get("digits"),
                "point": spec.get("point"),
                "volume_min": spec.get("volume_min"),
                "volume_max": spec.get("volume_max"),
                "volume_step": spec.get("volume_step"),
                "trade_stops_level": spec.get("trade_stops_level"),
                "swap_long": spec.get("swap_long"),
                "swap_short": spec.get("swap_short"),
                "commission_reported_by_symbol_info": False,
            },
            "spec_audit": spec_audit,
            "history": {
                "closed_bars_used": len(closed_bars),
                "contiguous_segments": len(segments),
                "longest_segment_bars": max((len(s) for s in segments), default=0),
                "first_bar_epoch": int(closed_bars[0][0]) if closed_bars else None,
                "last_bar_epoch": int(closed_bars[-1][0]) if closed_bars else None,
            },
            "parameters": {
                "targets_usd": targets,
                "stop_usd": args.stop_usd,
                "opening_range_bars": args.opening_range_bars,
                "max_lot": args.max_lot,
                "commission_assumption_usd_per_lot": args.commission_per_lot,
                "primary_pricing_basis": primary_basis,
                "primary_multiplier": primary_multiplier,
                "alternate_pricing_basis": alternate_basis,
            },
            "matrix_primary": matrix_primary,
            "matrix_alternate": matrix_alternate,
            "warnings": warnings,
            "private_log_file": out_path.name,
        }
        _save_private_json(out_path, {
            "report": report,
            "tool_calls": tool_calls,
        })
    except (Blocked, McpError, ValueError, OSError) as exc:
        reason = exc.reason if isinstance(exc, Blocked) else type(exc).__name__
        print(json.dumps({
            "ok": False,
            "paper": True,
            "executable": False,
            "execution_authorized": False,
            "orders_submitted": 0,
            "error": reason,
        }, indent=2))
        return 1

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
