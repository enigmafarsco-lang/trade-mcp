#!/usr/bin/env python3
"""Read-only 30-day Walk-Forward, Cost-Stress, and ADX-Sensitivity Validator for M1 Momentum.

Strictly read-only against MT5: uses only terminal_get_info, get_time_information,
get_marketwatch_symbols, and paginated get_chart_history over loopback HTTP.
Never submits orders, never reads account/deal history, never modifies terminal
settings, and never calls a cloud model.

Evaluates focused single-leg small-capital M1 candidates (including pullback+H1,
2bar+H1, and causal pullback_or_2bar hybrid) across:
  1) Full multi-week window (live spread, zero lookahead, conservative contract_size=100)
  2) Non-overlapping weekly walk-forward slices (W1..WN)
  3) Spread + commission cost stress ladder ($0.34 -> $0.50 -> $0.50+$5/lot -> $0.70+$7/lot)
  4) M5 ADX sensitivity sweep (14, 18, 22, 25)
  5) UTC session diagnostics (Asia / London / NY Overlap / NY Late / Rollover)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_m1_paper_window import (  # noqa: E402
    ReadOnlyNativeFeed,
    _save_private_json,
)
from daytrade_mt5_readonly_check import (  # noqa: E402
    Blocked,
    compact_bars,
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
    resample_m1_to_tf,
    split_contiguous_m1,
    validate_m1_momentum_candidates,
)
from trade_mcp.mcpclient import McpError  # noqa: E402


def _fetch_range(
    feed: ReadOnlyNativeFeed,
    *,
    period: str,
    start: Any,
    end: Any,
    limit: int = 1450,
) -> list[list[float]]:
    data = feed._call("get_chart_history", {
        "symbol": feed.symbol,
        "period": period,
        "datetime_from": start.isoformat(timespec="seconds"),
        "datetime_to": end.isoformat(timespec="seconds"),
        "limit": limit,
    })
    bars, _ = compact_bars(data)
    return bars


def _paginate_period(
    feed: ReadOnlyNativeFeed,
    *,
    period: str,
    now_label: datetime,
    total_days: int,
    chunk_days: int,
    stop_after_consecutive_empty: int | None = None,
    min_day_before_stop: int = 7,
) -> tuple[list[list[float]], int, bool]:
    """Paginate MT5 chart history safely below the 1450-bar request ceiling."""
    collected: list[list[float]] = []
    empty_chunks = 0
    consecutive_empty = 0
    horizon_reached = False

    offset = 0
    while offset < total_days:
        step = min(chunk_days, total_days - offset)
        chunk_end = now_label - timedelta(days=offset) + (timedelta(minutes=2) if offset == 0 else timedelta(0))
        chunk_start = now_label - timedelta(days=offset + step)
        try:
            rows = _fetch_range(feed, period=period, start=chunk_start, end=chunk_end, limit=1450)
            if rows:
                collected.extend(rows)
                consecutive_empty = 0
            else:
                empty_chunks += 1
                consecutive_empty += 1
        except Blocked as exc:
            if exc.reason in ("empty_data", "insufficient_history"):
                empty_chunks += 1
                consecutive_empty += 1
            else:
                raise
        offset += step
        if (
            stop_after_consecutive_empty is not None
            and offset >= min_day_before_stop
            and consecutive_empty >= stop_after_consecutive_empty
        ):
            horizon_reached = True
            break

    cleaned = clean_ohlcv_bars(collected)
    if len(cleaned) > 1:
        cleaned = cleaned[:-1]  # Exclude currently forming final bar
    return cleaned, empty_chunks, horizon_reached


def _fetch_validation_history_mt5(
    feed: ReadOnlyNativeFeed,
    *,
    days: int,
) -> tuple[
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    dict[str, Any],
]:
    """Paginate full multi-week M1 + M5 + M15 + H1 history cleanly from local MT5."""
    clock = feed._call("get_time_information", {})
    raw_time = find(clock, "trade_server_time")
    if raw_time is None:
        raw_time = find(clock, "trade_server_last_known_time")
    dt = timestamp(raw_time)
    if dt is None:
        raise Blocked("timestamp_unverified")
    now_label = dt.replace(tzinfo=None)

    # M1: 1-day chunks (~1380 bars/day <= 1450)
    m1, m1_empty, m1_horizon = _paginate_period(
        feed,
        period="M1",
        now_label=now_label,
        total_days=days,
        chunk_days=1,
        stop_after_consecutive_empty=6,
        min_day_before_stop=7,
    )
    if len(m1) < 200:
        raise Blocked("insufficient_history")

    # Actual M1 calendar span (in case broker retains fewer days than requested)
    actual_span_days = max(1, int(math.ceil((m1[-1][0] - m1[0][0]) / 86400.0)))
    m5_days = actual_span_days + 7
    m15_days = actual_span_days + 21
    h1_days = actual_span_days + 45

    # M5: 4-day chunks (4 * 288 = 1152 <= 1450)
    m5, m5_empty, _ = _paginate_period(feed, period="M5", now_label=now_label, total_days=m5_days, chunk_days=4)
    # M15: 12-day chunks (12 * 96 = 1152 <= 1450)
    m15, m15_empty, _ = _paginate_period(feed, period="M15", now_label=now_label, total_days=m15_days, chunk_days=12)
    # H1: 40-day chunks (40 * 24 = 960 <= 1450)
    h1, h1_empty, _ = _paginate_period(feed, period="H1", now_label=now_label, total_days=h1_days, chunk_days=40)

    meta = {
        "m1_empty_day_chunks_skipped": m1_empty,
        "m1_retention_horizon_reached": m1_horizon,
        "m5_empty_chunks_skipped": m5_empty,
        "m15_empty_chunks_skipped": m15_empty,
        "h1_empty_chunks_skipped": h1_empty,
        "actual_m1_calendar_span_days": round((m1[-1][0] - m1[0][0]) / 86400.0, 2),
    }
    return m1, m5, m15, h1, meta


def _mock_multiweek() -> tuple[
    dict[str, Any],
    dict[str, float],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
]:
    """Deterministic 4-week synthetic multi-TF series for fast offline validation tests."""
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
    # 4 weekly blocks spaced 7 days apart, each with 600 contiguous M1 bars (10 hours)
    # plus 40-day H1/M15/M5 warmup before week 1.
    week0_start = 1790035200  # 2026-09-23T00:00:00Z
    m1: list[list[float]] = []
    price = 3580.0
    for w in range(4):
        w_base = week0_start + w * 7 * 86400
        for i in range(600):
            t = float(w_base + i * 60)
            o = price
            phase = ((i + w * 40) // 120) % 4
            if phase == 0:
                delta = 0.22 + (1.75 if i % 17 == 0 else (0.85 if i % 17 == 1 else (0.45 if i % 17 == 2 else 0.0)))
                if i % 17 == 3:
                    delta = -0.35
            elif phase == 1:
                delta = 0.30 * math.sin(i * 0.85 + w)
            elif phase == 2:
                delta = -0.22 - (1.75 if i % 19 == 0 else (0.85 if i % 19 == 1 else (0.45 if i % 19 == 2 else 0.0)))
                if i % 19 == 3:
                    delta = 0.35
            else:
                delta = 0.10 + 0.40 * math.cos(i * 0.5 + w)
            c = round(o + delta, 2)
            wick_up = 0.22 if delta >= 0 else 0.38
            wick_dn = 0.38 if delta >= 0 else 0.22
            h = round(max(o, c) + wick_up, 2)
            low = round(min(o, c) - wick_dn, 2)
            m1.append([t, o, h, low, c, 150.0])
            price = c

    # Build continuous HTF warmup bars from 5 days before week 0 through end of week 3
    m5 = resample_m1_to_tf(m1, 300)
    m15 = resample_m1_to_tf(m1, 900)
    h1 = resample_m1_to_tf(m1, 3600)
    # Prepend synthetic H1/M15/M5 warmup so even bar 0 of week 1 has full causal EMAs/ADX
    warmup_m5: list[list[float]] = []
    warmup_m15: list[list[float]] = []
    warmup_h1: list[list[float]] = []
    p = 3550.0
    for k in range(60):
        ts_h1 = float(week0_start - (60 - k) * 3600)
        p_next = round(p + 0.5, 2)
        warmup_h1.append([ts_h1, p, p_next + 0.8, p - 0.4, p_next, 500.0])
        p = p_next
    p = 3570.0
    for k in range(40):
        ts_m15 = float(week0_start - (40 - k) * 900)
        p_next = round(p + 0.25, 2)
        warmup_m15.append([ts_m15, p, p_next + 0.5, p - 0.3, p_next, 300.0])
        p = p_next
    p = 3572.0
    for k in range(40):
        ts_m5 = float(week0_start - (40 - k) * 300)
        p_next = round(p + 0.20, 2)
        warmup_m5.append([ts_m5, p, p_next + 0.4, p - 0.2, p_next, 200.0])
        p = p_next

    quote = {"bid": price, "ask": round(price + 0.34, 2)}
    return (
        spec,
        quote,
        m1,
        clean_ohlcv_bars(warmup_m5 + m5),
        clean_ohlcv_bars(warmup_m15 + m15),
        clean_ohlcv_bars(warmup_h1 + h1),
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="XAUUSD_i", help="exact broker symbol (default: XAUUSD_i)")
    p.add_argument("--days", type=int, default=30,
                   help="calendar days of M1 history to paginate (7..45, default 30 ~= 4 trading weeks)")
    p.add_argument("--max-lot", type=float, default=0.02,
                   help=f"hard lot cap per trade for small capital (0.01..{HARD_MAX_LOT}, default 0.02)")
    p.add_argument("--spread-price", type=float, default=None,
                   help="override baseline spread in price units (default: live observed ask - bid)")
    p.add_argument("--leverage", type=float, default=500.0,
                   help="account leverage for margin-usage audit (default: 500)")
    p.add_argument("--out", default="", help="optional .json filename inside ignored logs/")
    p.add_argument("--mock", action="store_true", help="run offline smoke test with synthetic 4-week multi-TF data")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.symbol):
        raise SystemExit("Invalid symbol format")
    if not 7 <= args.days <= 45:
        raise SystemExit("--days must be between 7 and 45")
    if not 1.0 <= args.leverage <= 2000.0:
        raise SystemExit("--leverage must be between 1 and 2000")

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    out_path = Path(args.out) if args.out else Path("logs") / f"daytrade-m1-validate-{uuid.uuid4().hex[:12]}.json"

    try:
        tool_calls: dict[str, int] | None = None
        fetch_meta: dict[str, Any] = {}
        if args.mock:
            spec, quote, m1, m5, m15, h1 = _mock_multiweek()
            fetch_meta = {
                "m1_empty_day_chunks_skipped": 0,
                "m1_retention_horizon_reached": False,
                "actual_m1_calendar_span_days": round((m1[-1][0] - m1[0][0]) / 86400.0, 2),
            }
        else:
            load_dotenv()
            cfg = Config.from_env()
            if cfg.backend != "native":
                raise Blocked("native_backend_required")
            with loopback_native():
                feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, args.symbol)
                spec, quote = feed.connect()
                m1, m5, m15, h1, fetch_meta = _fetch_validation_history_mt5(feed, days=args.days)
                tool_calls = dict(feed.tool_calls)

        spread_price = (
            float(args.spread_price)
            if args.spread_price is not None
            else round(max(0.0, quote["ask"] - quote["bid"]), 6)
        )
        spec_audit = audit_symbol_spec(spec)
        primary_basis, primary_mult = resolve_pricing_basis(spec, "auto")
        segments = split_contiguous_m1(m1)

        validation = validate_m1_momentum_candidates(
            m1,
            spec=spec,
            symbol=args.symbol,
            spread_price=spread_price,
            m5_bars=m5 or None,
            m15_bars=m15 or None,
            h1_bars=h1 or None,
            max_lot=args.max_lot,
            leverage=args.leverage,
        )

        warnings = [
            "Read-only multi-week M1 validation: zero broker orders were created or submitted.",
            "Intrabar tick path is conservatively approximated from M1 OHLC (SL checked before trailing/deceleration).",
            "Constant spread modeled per stress tier; live news/rollover spikes can exceed $0.70.",
            "Passing historical walk-forward gates only qualifies a setup for bounded live Paper observation, not live trading.",
        ]
        if spec_audit.get("warning"):
            warnings.append(str(spec_audit["warning"]))

        def _iso(ep: float) -> str:
            return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": "multiweek_m1_momentum_validation",
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
                "days_requested": args.days,
                "max_lot": args.max_lot,
                "max_legs": 1,
                "leverage_for_margin_audit": args.leverage,
                "pricing_basis": primary_basis,
                "effective_multiplier": primary_mult,
                "take_profit_ceiling": None,
            },
            "history": {
                "m1_closed_bars": len(m1),
                "m1_contiguous_segments": len(segments),
                "m5_closed_bars": len(m5),
                "m15_closed_bars": len(m15),
                "h1_closed_bars": len(h1),
                "first_m1_utc": _iso(m1[0][0]),
                "last_m1_utc": _iso(m1[-1][0]),
                "htf_coverage_pct": validation["htf_coverage_pct"],
                "weekly_windows": validation["weekly_windows_meta"],
                **fetch_meta,
            },
            "verdict": validation["verdict"],
            "candidates": validation["candidates"],
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
