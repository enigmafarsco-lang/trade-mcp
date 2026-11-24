#!/usr/bin/env python3
"""Read-only Side-by-Side Validator: M5 (60d, 0.01 lot) vs Final M1 Structural Fix.

Strictly read-only against MT5: uses only terminal_get_info, get_time_information,
get_marketwatch_symbols, and paginated get_chart_history over loopback HTTP.
Never submits orders, never reads account/deal history, never modifies terminal
settings, and never calls a cloud model.

Compares in one auditable run:
  Part A — M5 Momentum/Acceleration (default 60 calendar days ~= 8 weeks,
           fixed small-capital 0.01 lot -> ~$7.25 margin, causal M15+H1+H4,
           rollover 21-24 UTC blocked, R-multiple breathing room).
  Part B — Final Fixed-Hypothesis M1 Test (default 45 calendar days, 0.02 lot,
           stop $5, causal M5+M15+H1, rollover 21-24 UTC blocked, min decel 1.2R
           and 1.5R benchmark).

Both arms are scored against the exact same 8 anti-overfitting gates:
  sample >= 40, full PnL > 0 & PF >= 1.15, maxDD <= $50, margin <= $25,
  weekly consistency (>= 65% positive weeks, worst week >= -$25),
  BOTH chronological halves positive (Half-1 & Half-2 PF >= 1.05),
  survives spread $0.50 (PF >= 1.05), and ADX stability (>= 3/4 thresholds).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_m1_momentum_validate import _paginate_period  # noqa: E402
from daytrade_m1_paper_window import (  # noqa: E402
    ReadOnlyNativeFeed,
    _save_private_json,
)
from daytrade_mt5_readonly_check import (  # noqa: E402
    Blocked,
    loopback_native,
    timestamp,
)

from trade_mcp.bridge.native import find  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.daytrade_paper import (  # noqa: E402
    HARD_MAX_LOT,
    audit_symbol_spec,
    resolve_pricing_basis,
)
from trade_mcp.m1_momentum import (  # noqa: E402
    clean_ohlcv_bars,
    resample_bars_to_tf,
    split_contiguous_bars,
    validate_timeframe_candidates,
)
from trade_mcp.mcpclient import McpError  # noqa: E402


def _fetch_m5_and_m1_history_mt5(
    feed: ReadOnlyNativeFeed,
    *,
    m5_days: int,
    m1_days: int,
) -> tuple[
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    dict[str, Any],
]:
    """Fetch bounded multi-month M5/M15/H1(+H4) and multi-week M1 bars from local MT5."""
    clock = feed._call("get_time_information", {})
    raw_time = find(clock, "trade_server_time")
    if raw_time is None:
        raw_time = find(clock, "trade_server_last_known_time")
    dt = timestamp(raw_time)
    if dt is None:
        raise Blocked("timestamp_unverified")
    now_label = dt.replace(tzinfo=None)

    # 1) M5: 4-day chunks (4 * 288 = 1152 <= 1450)
    m5, m5_empty, m5_horizon = _paginate_period(
        feed,
        period="M5",
        now_label=now_label,
        total_days=m5_days,
        chunk_days=4,
        stop_after_consecutive_empty=3,
        min_day_before_stop=14,
    )
    if len(m5) < 200:
        raise Blocked("insufficient_history")

    actual_m5_span = max(1, int(math.ceil((m5[-1][0] - m5[0][0]) / 86400.0)))

    # 2) M15: 12-day chunks covering M5 span + 21d warmup
    m15, m15_empty, _ = _paginate_period(
        feed,
        period="M15",
        now_label=now_label,
        total_days=actual_m5_span + 21,
        chunk_days=12,
    )
    # 3) H1: 40-day chunks covering M5 span + 90d warmup (so H4 EMA50 is 100% warmed up!)
    h1, h1_empty, _ = _paginate_period(
        feed,
        period="H1",
        now_label=now_label,
        total_days=actual_m5_span + 90,
        chunk_days=40,
    )
    # Derive exact causal H4 from H1 (avoids any broker H4 period quirks)
    h4 = resample_bars_to_tf(h1, src_seconds=3600, dst_seconds=14400)

    # 4) M1: 1-day chunks up to m1_days (stops early if broker M1 retention horizon reached)
    m1, m1_empty, m1_horizon = _paginate_period(
        feed,
        period="M1",
        now_label=now_label,
        total_days=m1_days,
        chunk_days=1,
        stop_after_consecutive_empty=6,
        min_day_before_stop=7,
    )
    if len(m1) < 200:
        raise Blocked("insufficient_history")

    meta = {
        "m5_empty_chunks_skipped": m5_empty,
        "m5_retention_horizon_reached": m5_horizon,
        "actual_m5_calendar_span_days": round((m5[-1][0] - m5[0][0]) / 86400.0, 2),
        "m15_empty_chunks_skipped": m15_empty,
        "h1_empty_chunks_skipped": h1_empty,
        "m1_empty_day_chunks_skipped": m1_empty,
        "m1_retention_horizon_reached": m1_horizon,
        "actual_m1_calendar_span_days": round((m1[-1][0] - m1[0][0]) / 86400.0, 2),
    }
    return m5, m15, h1, h4, m1, meta


def _mock_m5_and_m1() -> tuple[
    dict[str, Any],
    dict[str, float],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
]:
    """Deterministic synthetic 6-week M5 + 4-week M1 dataset for fast offline testing."""
    spec: dict[str, Any] = {
        "trade_tick_size": 0.01,
        "trade_tick_value": 0.1,
        "trade_contract_size": 100.0,
        "currency_profit": "USD",
        "trade_calc_mode": "CFD Leverage",
        "digits": 2,
        "volume_step": 0.01,
        "volume_min": 0.01,
        "volume_max": 50.0,
        "point": 0.01,
        "trade_stops_level": 0.0,
    }
    start_ep = 1788048000  # 2026-08-30T00:00:00Z
    # 6 contiguous weekly blocks of M5 bars (each 800 bars ~= 2.8 days) -> passes default min_bars=700
    m5: list[list[float]] = []
    px = 3520.0
    for w in range(6):
        wb = start_ep + w * 7 * 86400
        for i in range(800):
            t = float(wb + i * 300)
            o = px
            phase = ((i + w * 25) // 160) % 4
            if phase == 0:
                delta = 0.6 + (4.2 if i % 19 == 0 else (2.4 if i % 19 == 1 else (1.5 if i % 19 == 2 else 0.0)))
                if i % 19 == 3:
                    delta = -0.8
            elif phase == 1:
                delta = 0.8 * math.sin(i * 0.7 + w)
            elif phase == 2:
                delta = -0.6 - (4.2 if i % 23 == 0 else (2.4 if i % 23 == 1 else (1.5 if i % 23 == 2 else 0.0)))
                if i % 23 == 3:
                    delta = 0.8
            else:
                delta = 0.3 + 1.0 * math.cos(i * 0.4 + w)
            c = round(o + delta, 2)
            wu = 0.5 if delta >= 0 else 0.9
            wd = 0.9 if delta >= 0 else 0.5
            m5.append([t, o, round(max(o, c) + wu, 2), round(min(o, c) - wd, 2), c, 300.0])
            px = c

    # 4 weekly blocks of M1 bars (each 600 bars)
    m1: list[list[float]] = []
    p1 = 3560.0
    for w in range(4):
        wb = start_ep + (w + 2) * 7 * 86400
        for i in range(600):
            t = float(wb + i * 60)
            o = p1
            phase = ((i + w * 30) // 120) % 4
            if phase == 0:
                delta = 0.22 + (1.7 if i % 17 == 0 else (0.9 if i % 17 == 1 else 0.0))
            elif phase == 2:
                delta = -0.22 - (1.7 if i % 19 == 0 else (0.9 if i % 19 == 1 else 0.0))
            else:
                delta = 0.3 * math.sin(i * 0.8 + w)
            c = round(o + delta, 2)
            m1.append([t, o, round(max(o, c) + 0.3, 2), round(min(o, c) - 0.3, 2), c, 120.0])
            p1 = c

    m15 = resample_bars_to_tf(m5, src_seconds=300, dst_seconds=900)
    h1 = resample_bars_to_tf(m5, src_seconds=300, dst_seconds=3600)
    h4 = resample_bars_to_tf(h1, src_seconds=3600, dst_seconds=14400)

    # Prepend clean warmup bars so M15/H1/H4 are 100% warmed up from bar 0
    w_m15, w_h1, w_h4 = [], [], []
    p = 3450.0
    for k in range(60):
        ts = float(start_ep - (60 - k) * 14400)
        pn = round(p + 1.0, 2)
        w_h4.append([ts, p, pn + 2.0, p - 1.0, pn, 900.0])
        p = pn
    p = 3490.0
    for k in range(60):
        ts = float(start_ep - (60 - k) * 3600)
        pn = round(p + 0.5, 2)
        w_h1.append([ts, p, pn + 1.2, p - 0.6, pn, 500.0])
        p = pn
    p = 3505.0
    for k in range(40):
        ts = float(start_ep - (40 - k) * 900)
        pn = round(p + 0.3, 2)
        w_m15.append([ts, p, pn + 0.8, p - 0.4, pn, 300.0])
        p = pn

    quote = {"bid": px, "ask": round(px + 0.34, 2)}
    return (
        spec,
        quote,
        m5,
        clean_ohlcv_bars(w_m15 + m15),
        clean_ohlcv_bars(w_h1 + h1),
        clean_ohlcv_bars(w_h4 + h4),
        m1,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="XAUUSD_i", help="exact broker symbol (default: XAUUSD_i)")
    p.add_argument("--m5-days", type=int, default=60,
                   help="calendar days of M5 history to paginate (14..90, default 60 ~= 8 trading weeks)")
    p.add_argument("--m1-days", type=int, default=45,
                   help="calendar days of M1 history to paginate (14..60, default 45)")
    p.add_argument("--m5-max-lot", type=float, default=0.01,
                   help=f"fixed small-capital lot cap for M5 (0.01..{HARD_MAX_LOT}, default 0.01 -> ~$7.25 margin)")
    p.add_argument("--m1-max-lot", type=float, default=0.02,
                   help=f"lot cap for final M1 test (0.01..{HARD_MAX_LOT}, default 0.02)")
    p.add_argument("--spread-price", type=float, default=None,
                   help="override baseline spread in price units (default: live observed ask - bid)")
    p.add_argument("--leverage", type=float, default=500.0,
                   help="account leverage for margin-usage audit (default: 500)")
    p.add_argument("--out", default="", help="optional .json filename inside ignored logs/")
    p.add_argument("--mock", action="store_true", help="run offline smoke test with synthetic M5+M1 data")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.symbol):
        raise SystemExit("Invalid symbol format")
    if not 14 <= args.m5_days <= 90:
        raise SystemExit("--m5-days must be between 14 and 90")
    if not 14 <= args.m1_days <= 60:
        raise SystemExit("--m1-days must be between 14 and 60")
    if not 1.0 <= args.leverage <= 2000.0:
        raise SystemExit("--leverage must be between 1 and 2000")

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    out_path = Path(args.out) if args.out else Path("logs") / f"daytrade-m5-vs-m1-{uuid.uuid4().hex[:12]}.json"

    try:
        tool_calls: dict[str, int] | None = None
        fetch_meta: dict[str, Any] = {}
        if args.mock:
            spec, quote, m5, m15, h1, h4, m1 = _mock_m5_and_m1()
            fetch_meta = {
                "actual_m5_calendar_span_days": round((m5[-1][0] - m5[0][0]) / 86400.0, 2),
                "actual_m1_calendar_span_days": round((m1[-1][0] - m1[0][0]) / 86400.0, 2),
                "m5_retention_horizon_reached": False,
                "m1_retention_horizon_reached": False,
            }
        else:
            load_dotenv()
            cfg = Config.from_env()
            if cfg.backend != "native":
                raise Blocked("native_backend_required")
            with loopback_native():
                feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, args.symbol)
                spec, quote = feed.connect()
                m5, m15, h1, h4, m1, fetch_meta = _fetch_m5_and_m1_history_mt5(
                    feed, m5_days=args.m5_days, m1_days=args.m1_days,
                )
                tool_calls = dict(feed.tool_calls)

        spread_price = (
            float(args.spread_price)
            if args.spread_price is not None
            else round(max(0.0, quote["ask"] - quote["bid"]), 6)
        )
        spec_audit = audit_symbol_spec(spec)
        primary_basis, primary_mult = resolve_pricing_basis(spec, "auto")

        # Part A: M5 (60d, 0.01 lot, causal M15 + H1 + H4)
        m5_val = validate_timeframe_candidates(
            m5,
            timeframe="M5",
            spec=spec,
            symbol=args.symbol,
            spread_price=spread_price,
            primary_htf_bars=m15 or None,
            mid_htf_bars=h1 or None,
            macro_htf_bars=h4 or None,
            max_lot=args.m5_max_lot,
            leverage=args.leverage,
            min_bars_per_window=500 if args.mock else 700,
        )

        # Part B: Final M1 structural fix (causal M5 + M15 + H1, rollover blocked, decel >= 1.2R / 1.5R)
        m1_val = validate_timeframe_candidates(
            m1,
            timeframe="M1",
            spec=spec,
            symbol=args.symbol,
            spread_price=spread_price,
            primary_htf_bars=m5 or None,
            mid_htf_bars=m15 or None,
            macro_htf_bars=h1 or None,
            max_lot=args.m1_max_lot,
            leverage=args.leverage,
            min_bars_per_window=500 if args.mock else 3500,
        )

        m5_passed = m5_val["verdict"]["passed_ids"]
        m1_passed = m1_val["verdict"]["passed_ids"]
        if m5_passed and m1_passed:
            overall_status = "BOTH_M5_AND_M1_HAVE_PASSING_CANDIDATES"
            recommended = m5_val["verdict"]["recommended_for_live_paper"]
        elif m5_passed:
            overall_status = "M5_PASSED_M1_FAILED_PROMOTE_M5_TO_LIVE_PAPER"
            recommended = m5_val["verdict"]["recommended_for_live_paper"]
        elif m1_passed:
            overall_status = "M1_PASSED_M5_FAILED_PROMOTE_M1_TO_LIVE_PAPER"
            recommended = m1_val["verdict"]["recommended_for_live_paper"]
        else:
            overall_status = "NEITHER_PASSED_DO_NOT_PROMOTE"
            recommended = None

        def _iso(ep: float) -> str:
            return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        warnings = [
            "Read-only M5 vs M1 validation: zero broker orders were created or submitted.",
            "Intrabar tick path is conservatively approximated from OHLC (SL checked strictly before TP/trail/decel).",
            "Rollover hours 21:00-23:59 UTC are blocked structurally; live news spikes can still exceed $0.70 spread.",
            "Passing historical gates only qualifies a candidate for bounded live Paper observation, not live trading.",
        ]
        if spec_audit.get("warning"):
            warnings.append(str(spec_audit["warning"]))

        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": "m5_vs_m1_side_by_side_validation",
            "executable": False,
            "execution_authorized": False,
            "orders_submitted": 0,
            "symbol": args.symbol,
            "live_quote": {
                "bid": quote["bid"],
                "ask": quote["ask"],
                "observed_spread_price": round(quote["ask"] - quote["bid"], 6),
                "baseline_spread_price": spread_price,
            },
            "spec_audit": spec_audit,
            "parameters": {
                "m5_days_requested": args.m5_days,
                "m1_days_requested": args.m1_days,
                "m5_max_lot": args.m5_max_lot,
                "m1_max_lot": args.m1_max_lot,
                "max_legs": 1,
                "leverage_for_margin_audit": args.leverage,
                "pricing_basis": primary_basis,
                "effective_multiplier": primary_mult,
                "rollover_blocked_utc_hours": [21, 22, 23],
            },
            "history": {
                "m5_closed_bars": len(m5),
                "m5_contiguous_segments": len(split_contiguous_bars(m5, bar_seconds=300)),
                "m5_first_utc": _iso(m5[0][0]),
                "m5_last_utc": _iso(m5[-1][0]),
                "m15_closed_bars": len(m15),
                "h1_closed_bars": len(h1),
                "h4_resampled_bars": len(h4),
                "m1_closed_bars": len(m1),
                "m1_contiguous_segments": len(split_contiguous_bars(m1, bar_seconds=60)),
                "m1_first_utc": _iso(m1[0][0]),
                "m1_last_utc": _iso(m1[-1][0]),
                "m5_htf_coverage_pct": m5_val["htf_coverage_pct"],
                "m1_htf_coverage_pct": m1_val["htf_coverage_pct"],
                "m5_weekly_windows": m5_val["weekly_windows_meta"],
                "m1_weekly_windows": m1_val["weekly_windows_meta"],
                **fetch_meta,
            },
            "comparison_verdict": {
                "status": overall_status,
                "m5_passed_ids": m5_passed,
                "m1_passed_ids": m1_passed,
                "recommended_for_live_paper": recommended,
                "m5_best_diagnostic_id": m5_val["verdict"]["best_diagnostic_id"],
                "m1_best_diagnostic_id": m1_val["verdict"]["best_diagnostic_id"],
            },
            "m5_validation": m5_val,
            "m1_final_validation": m1_val,
            "warnings": warnings,
        }
        if tool_calls is not None:
            report["mt5_tool_calls"] = tool_calls

        saved = _save_private_json(out_path, report)
        report["log_path"] = str(saved)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    except Blocked as exc:
        print(json.dumps({
            "ok": False,
            "paper": True,
            "executable": False,
            "execution_authorized": False,
            "orders_submitted": 0,
            "blocked": exc.reason,
            "symbol": args.symbol,
        }, indent=2))
        return 1
    except (McpError, ValueError, OSError) as exc:
        print(json.dumps({
            "ok": False,
            "paper": True,
            "executable": False,
            "execution_authorized": False,
            "orders_submitted": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "symbol": args.symbol,
        }, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
