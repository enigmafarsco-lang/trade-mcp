#!/usr/bin/env python3
"""Read-only multi-day M1 momentum + multi-timeframe (M5/M15/H1) replay CLI.

Strictly read-only against MT5: uses only terminal_get_info, get_time_information,
get_marketwatch_symbols, and get_chart_history over loopback HTTP. Never submits
orders, never reads account/deal history, never modifies terminal settings, and
never calls a cloud model.

Evaluates M1 acceleration entries (impulse_close, impulse_2bar, impulse_pullback),
causal higher-timeframe filters (none, M5+M15, M5+M15+H1), uncapped momentum exits
(deceleration+trail vs pure candle trail), fast invalidation cuts, and safe
profit-only pyramiding with small-capital margin tracking.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import uuid
from datetime import timedelta
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
    HARD_MAX_STOP_USD,
    audit_symbol_spec,
    resolve_pricing_basis,
)
from trade_mcp.m1_momentum import (  # noqa: E402
    clean_ohlcv_bars,
    replay_m1_momentum_matrix,
    resample_m1_to_tf,
    split_contiguous_m1,
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


def _fetch_multiday_mt5(
    feed: ReadOnlyNativeFeed,
    *,
    days: int,
) -> tuple[list[list[float]], list[list[float]], list[list[float]], list[list[float]], int]:
    """Fetch bounded multi-day closed M1 + M5 + M15 + H1 bars from local MT5."""
    clock = feed._call("get_time_information", {})
    raw_time = find(clock, "trade_server_time")
    if raw_time is None:
        raw_time = find(clock, "trade_server_last_known_time")
    dt = timestamp(raw_time)
    if dt is None:
        raise Blocked("timestamp_unverified")
    now_label = dt.replace(tzinfo=None)

    m1_all: list[list[float]] = []
    empty_chunks = 0
    for day_offset in range(days):
        chunk_end = now_label - timedelta(days=day_offset) + (timedelta(minutes=2) if day_offset == 0 else timedelta(0))
        chunk_start = now_label - timedelta(days=day_offset + 1)
        try:
            chunk = _fetch_range(feed, period="M1", start=chunk_start, end=chunk_end, limit=1450)
            m1_all.extend(chunk)
        except Blocked as exc:
            if exc.reason in ("empty_data", "insufficient_history"):
                empty_chunks += 1
                continue
            raise

    m1_clean = clean_ohlcv_bars(m1_all)
    if len(m1_clean) > 1:
        m1_clean = m1_clean[:-1]  # Exclude forming final M1 bar
    if len(m1_clean) < 50:
        raise Blocked("insufficient_history")

    def _safe_htf(period: str, lookback_days: int) -> list[list[float]]:
        try:
            rows = _fetch_range(
                feed,
                period=period,
                start=now_label - timedelta(days=lookback_days),
                end=now_label + timedelta(minutes=2),
                limit=1450,
            )
            cleaned = clean_ohlcv_bars(rows)
            return cleaned[:-1] if len(cleaned) > 1 else cleaned
        except Blocked:
            return []

    m5 = _safe_htf("M5", max(days + 7, 14))
    m15 = _safe_htf("M15", max(days + 21, 30))
    h1 = _safe_htf("H1", max(days + 45, 60))
    return m1_clean, m5, m15, h1, empty_chunks


def _mock_multiday() -> tuple[dict[str, Any], dict[str, float], list[list[float]], list[list[float]], list[list[float]], list[list[float]]]:
    """Deterministic multi-wave synthetic M1/M5/M15/H1 series for offline testing."""
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
    base = 1700000040
    m1: list[list[float]] = []
    price = 3600.0
    # 720 synthetic M1 bars (12h) mixing steady trends, impulse bursts, stalls, and chop
    for i in range(720):
        t = float(base + i * 60)
        o = price
        phase = (i // 120) % 4
        if phase == 0:
            # Uptrend with occasional acceleration bars
            delta = 0.25 + (1.8 if i % 17 == 0 else (0.9 if i % 17 == 1 else (0.5 if i % 17 == 2 else 0.0)))
            if i % 17 == 3:
                delta = -0.35
        elif phase == 1:
            # Sideways chop
            delta = 0.35 * math.sin(i * 0.9)
        elif phase == 2:
            # Downtrend with downward acceleration bursts
            delta = -0.25 - (1.8 if i % 19 == 0 else (0.9 if i % 19 == 1 else (0.5 if i % 19 == 2 else 0.0)))
            if i % 19 == 3:
                delta = 0.35
        else:
            # Mixed recovery
            delta = 0.12 + 0.45 * math.cos(i * 0.5)
        c = round(o + delta, 2)
        wick_up = 0.20 if delta >= 0 else 0.35
        wick_dn = 0.35 if delta >= 0 else 0.20
        h = round(max(o, c) + wick_up, 2)
        low = round(min(o, c) - wick_dn, 2)
        m1.append([t, o, h, low, c, 150.0])
        price = c

    m5 = resample_m1_to_tf(m1, 300)
    m15 = resample_m1_to_tf(m1, 900)
    h1 = resample_m1_to_tf(m1, 3600)
    quote = {"bid": price, "ask": round(price + 0.34, 2)}
    return spec, quote, m1, m5, m15, h1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="XAUUSD_i", help="exact broker symbol (default: XAUUSD_i)")
    p.add_argument("--days", type=int, default=7,
                   help="calendar days of M1 history to paginate (1..10, default 7 ~= 5 trading days)")
    p.add_argument("--stop-usd", type=float, default=5.0,
                   help=f"hard protective stop loss USD per leg (1..{HARD_MAX_STOP_USD}, default 5.0)")
    p.add_argument("--max-lot", type=float, default=0.02,
                   help=f"hard lot cap per leg for small capital (0.01..{HARD_MAX_LOT}, default 0.02)")
    p.add_argument("--commission-per-lot", type=float, default=0.0,
                   help="round-trip commission USD/lot assumption (default 0.0)")
    p.add_argument("--spread-price", type=float, default=None,
                   help="override modeled spread in price units (default: live observed ask - bid)")
    p.add_argument("--leverage", type=float, default=500.0,
                   help="account leverage for margin-usage audit (default: 500)")
    p.add_argument("--out", default="", help="optional .json filename inside ignored logs/")
    p.add_argument("--mock", action="store_true", help="run offline smoke test with synthetic multi-TF data")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.symbol):
        raise SystemExit("Invalid symbol format")
    if not 1 <= args.days <= 10:
        raise SystemExit("--days must be between 1 and 10")
    if not 1.0 <= args.leverage <= 2000.0:
        raise SystemExit("--leverage must be between 1 and 2000")

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    out_path = Path(args.out) if args.out else Path("logs") / f"daytrade-m1-momentum-{uuid.uuid4().hex[:12]}.json"

    try:
        tool_calls: dict[str, int] | None = None
        empty_chunks = 0
        if args.mock:
            spec, quote, m1, m5, m15, h1 = _mock_multiday()
        else:
            load_dotenv()
            cfg = Config.from_env()
            if cfg.backend != "native":
                raise Blocked("native_backend_required")
            with loopback_native():
                feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, args.symbol)
                spec, quote = feed.connect()
                m1, m5, m15, h1, empty_chunks = _fetch_multiday_mt5(feed, days=args.days)
                tool_calls = dict(feed.tool_calls)

        spread_price = (
            float(args.spread_price)
            if args.spread_price is not None
            else round(max(0.0, quote["ask"] - quote["bid"]), 6)
        )
        spec_audit = audit_symbol_spec(spec)
        primary_basis, primary_mult = resolve_pricing_basis(spec, "auto")
        segments = split_contiguous_m1(m1)

        matrix = replay_m1_momentum_matrix(
            m1,
            spec=spec,
            symbol=args.symbol,
            spread_price=spread_price,
            m5_bars=m5 or None,
            m15_bars=m15 or None,
            h1_bars=h1 or None,
            stop_usd=args.stop_usd,
            max_lot=args.max_lot,
            commission_per_lot=args.commission_per_lot,
            leverage=args.leverage,
        )

        warnings = [
            "Read-only multi-day M1 momentum replay: zero broker orders were created or submitted.",
            "Intrabar tick path is conservatively approximated from M1 OHLC (SL checked before trailing/deceleration).",
            "Constant spread modeled from live quote; real spreads widen sharply during news spikes and rollover.",
            "Profit-only pyramiding still carries gap risk if price jumps past breakeven stops simultaneously.",
            "Historical replay across several days does not guarantee future profitability or authorize trading.",
        ]
        if args.commission_per_lot == 0.0:
            warnings.append("Commission modeled as 0.0 USD/lot (unverified assumption).")
        if spec_audit.get("warning"):
            warnings.append(str(spec_audit["warning"]))

        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": "multiday_m1_momentum_htf_replay",
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
            "spec_audit": spec_audit,
            "parameters": {
                "days_requested": args.days,
                "stop_usd_per_leg": args.stop_usd,
                "max_lot_per_leg": args.max_lot,
                "commission_assumption_usd_per_lot": args.commission_per_lot,
                "leverage_for_margin_audit": args.leverage,
                "pricing_basis": primary_basis,
                "effective_multiplier": primary_mult,
                "take_profit_ceiling": None,
            },
            "history": {
                "m1_closed_bars": len(m1),
                "m1_contiguous_segments": len(segments),
                "m1_empty_day_chunks_skipped": empty_chunks,
                "m5_closed_bars": len(m5),
                "m15_closed_bars": len(m15),
                "h1_closed_bars": len(h1),
                "first_m1_epoch": int(m1[0][0]) if m1 else None,
                "last_m1_epoch": int(m1[-1][0]) if m1 else None,
            },
            "matrix": matrix,
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
