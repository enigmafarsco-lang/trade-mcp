#!/usr/bin/env python3
"""Read-only 365-Day Risk-Brake & Drawdown Validator for Locked M5_5 Strategy.

Strictly read-only against MT5: uses only terminal_get_info, get_time_information,
get_marketwatch_symbols, and paginated get_chart_history over loopback HTTP.
Never submits orders, never reads account/deal history, never modifies terminal
settings, and never calls a cloud model.

Holds the M5_5 signal logic 100% FIXED (pullback_or_2bar, M15+H1+H4, trail_only
0.8R, no rollover 21-24 UTC, fixed 0.01 lot) and tests 6 simple capital-protection
risk brakes across up to 365 calendar days (~1 full year):
  - R0_BASE: baseline ($12 stop, no brakes)
  - R1_DAILY15: daily realized loss brake -$15
  - R2_DAILY15_WEEKLY25: daily -$15 + weekly -$25
  - R3_BRAKE_PLUS_4H_CD: daily -$15 + weekly -$25 + 4h pause after 2 losses
  - R4_STOP10_BRAKES: tighter $10 stop + daily -$15 + weekly -$25
  - R5_STOP8_BRAKES: tighter $8 stop + daily -$12 + weekly -$20

When MT5 retains > 180 days, strictly isolates months 7..12 ago as a brand-new
unseen True Out-of-Sample window.
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
    evaluate_m5_risk_brakes,
    resample_bars_to_tf,
    split_contiguous_bars,
)
from trade_mcp.mcpclient import McpError  # noqa: E402


def _fetch_365d_m5_history_mt5(
    feed: ReadOnlyNativeFeed,
    *,
    m5_days: int,
) -> tuple[
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    dict[str, Any],
]:
    """Fetch bounded up-to-365d M5 + M15 + H1(+H4) bars cleanly from local MT5."""
    clock = feed._call("get_time_information", {})
    raw_time = find(clock, "trade_server_time")
    if raw_time is None:
        raw_time = find(clock, "trade_server_last_known_time")
    dt = timestamp(raw_time)
    if dt is None:
        raise Blocked("timestamp_unverified")
    now_label = dt.replace(tzinfo=None)

    # M5: 4-day chunks (1152 <= 1450)
    m5, m5_empty, m5_horizon = _paginate_period(
        feed,
        period="M5",
        now_label=now_label,
        total_days=m5_days,
        chunk_days=4,
        stop_after_consecutive_empty=3,
        min_day_before_stop=20,
    )
    if len(m5) < 200:
        raise Blocked("insufficient_history")

    actual_span = max(1, int(math.ceil((m5[-1][0] - m5[0][0]) / 86400.0)))
    m15, m15_empty, _ = _paginate_period(
        feed, period="M15", now_label=now_label, total_days=actual_span + 21, chunk_days=12,
    )
    h1, h1_empty, _ = _paginate_period(
        feed, period="H1", now_label=now_label, total_days=actual_span + 90, chunk_days=40,
    )
    h4 = resample_bars_to_tf(h1, src_seconds=3600, dst_seconds=14400)

    meta = {
        "m5_empty_chunks_skipped": m5_empty,
        "m5_retention_horizon_reached": m5_horizon,
        "actual_m5_calendar_span_days": round((m5[-1][0] - m5[0][0]) / 86400.0, 2),
        "m15_empty_chunks_skipped": m15_empty,
        "h1_empty_chunks_skipped": h1_empty,
    }
    return m5, m15, h1, h4, meta


def _mock_365d() -> tuple[
    dict[str, Any],
    dict[str, float],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
]:
    """Deterministic 10-month synthetic M5 dataset spanning > 270 days for fast offline tests."""
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
    start_ep = 1769904000  # 2026-02-01T00:00:00Z
    m5: list[list[float]] = []
    px = 3100.0
    for m_idx in range(10):
        mb = start_ep + m_idx * 30 * 86400
        for i in range(360):
            t = float(mb + i * 300)
            o = px
            phase = ((i + m_idx * 17) // 90) % 4
            if phase == 0:
                delta = 0.55 + (4.0 if i % 19 == 0 else (2.2 if i % 19 == 1 else (1.3 if i % 19 == 2 else 0.0)))
                if i % 19 == 3:
                    delta = -0.7
            elif phase == 2:
                delta = -0.55 - (4.0 if i % 19 == 0 else (2.2 if i % 19 == 1 else (1.3 if i % 19 == 2 else 0.0)))
                if i % 19 == 3:
                    delta = 0.7
            else:
                delta = 0.8 * math.sin(i * 0.65 + m_idx)
            c = round(o + delta, 2)
            m5.append([t, o, round(max(o, c) + 0.55, 2), round(min(o, c) - 0.55, 2), c, 240.0])
            px = c

    m15 = resample_bars_to_tf(m5, src_seconds=300, dst_seconds=900)
    h1 = resample_bars_to_tf(m5, src_seconds=300, dst_seconds=3600)
    h4 = resample_bars_to_tf(h1, src_seconds=3600, dst_seconds=14400)
    w_m15, w_h1, w_h4 = [], [], []
    p = 3020.0
    for k in range(60):
        ts = float(start_ep - (60 - k) * 14400)
        pn = round(p + 1.0, 2)
        w_h4.append([ts, p, pn + 1.5, p - 1.0, pn, 800.0])
        p = pn
    p = 3070.0
    for k in range(60):
        ts = float(start_ep - (60 - k) * 3600)
        pn = round(p + 0.4, 2)
        w_h1.append([ts, p, pn + 0.9, p - 0.5, pn, 400.0])
        p = pn
    p = 3090.0
    for k in range(40):
        ts = float(start_ep - (40 - k) * 900)
        pn = round(p + 0.25, 2)
        w_m15.append([ts, p, pn + 0.5, p - 0.3, pn, 250.0])
        p = pn

    quote = {"bid": px, "ask": round(px + 0.34, 2)}
    return (
        spec,
        quote,
        m5,
        clean_ohlcv_bars(w_m15 + m15),
        clean_ohlcv_bars(w_h1 + h1),
        clean_ohlcv_bars(w_h4 + h4),
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="XAUUSD_i", help="exact broker symbol (default: XAUUSD_i)")
    p.add_argument("--m5-days", type=int, default=365,
                   help="calendar days of M5 history to paginate (90..400, default 365 ~= 1 year)")
    p.add_argument("--prior-seen-days", type=int, default=180,
                   help="recent days already seen in prior OOS run (default 180; older = brand-new unseen OOS)")
    p.add_argument("--max-lot", type=float, default=0.01,
                   help=f"fixed small-capital lot cap (0.01..{HARD_MAX_LOT}, default 0.01)")
    p.add_argument("--spread-price", type=float, default=None,
                   help="override baseline spread in price units (default: live observed ask - bid)")
    p.add_argument("--leverage", type=float, default=500.0,
                   help="account leverage for margin audit (default: 500)")
    p.add_argument("--out", default="", help="optional .json filename inside ignored logs/")
    p.add_argument("--mock", action="store_true", help="run offline smoke test with synthetic 10-month data")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.symbol):
        raise SystemExit("Invalid symbol format")
    if not 90 <= args.m5_days <= 400:
        raise SystemExit("--m5-days must be between 90 and 400")
    if not 1.0 <= args.leverage <= 2000.0:
        raise SystemExit("--leverage must be between 1 and 2000")

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    out_path = Path(args.out) if args.out else Path("logs") / f"daytrade-m5-brakes-365d-{uuid.uuid4().hex[:12]}.json"

    try:
        tool_calls: dict[str, int] | None = None
        fetch_meta: dict[str, Any] = {}
        if args.mock:
            spec, quote, m5, m15, h1, h4 = _mock_365d()
            fetch_meta = {
                "actual_m5_calendar_span_days": round((m5[-1][0] - m5[0][0]) / 86400.0, 2),
                "m5_retention_horizon_reached": False,
            }
        else:
            load_dotenv()
            cfg = Config.from_env()
            if cfg.backend != "native":
                raise Blocked("native_backend_required")
            with loopback_native():
                feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, args.symbol)
                spec, quote = feed.connect()
                m5, m15, h1, h4, fetch_meta = _fetch_365d_m5_history_mt5(feed, m5_days=args.m5_days)
                tool_calls = dict(feed.tool_calls)

        spread_price = (
            float(args.spread_price)
            if args.spread_price is not None
            else round(max(0.0, quote["ask"] - quote["bid"]), 6)
        )
        spec_audit = audit_symbol_spec(spec)
        primary_basis, primary_mult = resolve_pricing_basis(spec, "auto")

        result = evaluate_m5_risk_brakes(
            m5,
            spec=spec,
            symbol=args.symbol,
            spread_price=spread_price,
            m15_bars=m15 or None,
            h1_bars=h1 or None,
            h4_bars=h4 or None,
            max_lot=args.max_lot,
            leverage=args.leverage,
            prior_seen_days=args.prior_seen_days,
        )

        def _iso(ep: float) -> str:
            return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        warnings = [
            "Read-only 365-day risk-brake test: zero broker orders were created or submitted.",
            "Locked M5_5 signal logic was held 100% constant; only protective risk brakes were varied.",
            "Intrabar tick path is conservatively approximated from OHLC (SL checked strictly before trail).",
            "Passing risk-brake gates qualifies for bounded live Paper testing only, not live trading.",
        ]
        if spec_audit.get("warning"):
            warnings.append(str(spec_audit["warning"]))

        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": "m5_locked_risk_brakes_365d_validation",
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
                "prior_seen_days": args.prior_seen_days,
                "max_lot": args.max_lot,
                "max_legs": 1,
                "leverage_for_margin_audit": args.leverage,
                "pricing_basis": primary_basis,
                "effective_multiplier": primary_mult,
            },
            "history": {
                "m5_closed_bars": len(m5),
                "m5_contiguous_segments": len(split_contiguous_bars(m5, bar_seconds=300)),
                "m5_first_utc": _iso(m5[0][0]),
                "m5_last_utc": _iso(m5[-1][0]),
                "m15_closed_bars": len(m15),
                "h1_closed_bars": len(h1),
                "h4_resampled_bars": len(h4),
                "htf_coverage_pct": result["htf_coverage_pct"],
                **fetch_meta,
            },
            "risk_brakes_validation": result,
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
