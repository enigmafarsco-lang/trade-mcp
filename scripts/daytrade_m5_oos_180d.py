#!/usr/bin/env python3
"""Read-only 180-Day True Out-of-Sample Validator for Locked M5_5 (+ Volume-Surge Diagnostic).

Strictly read-only against MT5: uses only terminal_get_info, get_time_information,
get_marketwatch_symbols, and paginated get_chart_history over loopback HTTP.
Never submits orders, never reads account/deal history, never modifies terminal
settings, and never calls a cloud model.

Part 1 — Locked True Out-of-Sample Test (NO parameter tuning):
  Evaluates the exact locked M5_5 strategy (and sibling control M5_3) over up to
  180 calendar days (~6 months) of M5 bars, strictly separating:
    - unseen_older_oos: everything strictly BEFORE the recent 60 days (4 unseen months)
    - seen_recent_window: the recent 60 days already viewed in prior screening
    - full_period: entire fetched span with month-by-month breakdown & concentration audit.

Part 2 — Separate Diagnostic (Volume-Surge Entry + Volume-Dryup Exit):
  Tests tick-volume surge entry + exit when momentum slows OR tick volume dries up
  on M1 (30d) and M5 (180d), strictly isolated from the locked OOS verdict.
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
    ROLLOVER_BLOCKED_UTC_HOURS,
    _plain_trade_summary,
    build_htf_index,
    clean_ohlcv_bars,
    evaluate_locked_m5_oos,
    replay_m1_momentum,
    resample_bars_to_tf,
    split_contiguous_bars,
)
from trade_mcp.mcpclient import McpError  # noqa: E402


def _fetch_oos_history_mt5(
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
    """Fetch bounded 180-day M5/M15/H1(+H4) and 30-day M1 bars cleanly from local MT5."""
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
        min_day_before_stop=14,
    )
    if len(m5) < 200:
        raise Blocked("insufficient_history")

    actual_m5_span = max(1, int(math.ceil((m5[-1][0] - m5[0][0]) / 86400.0)))
    m15, m15_empty, _ = _paginate_period(
        feed, period="M15", now_label=now_label, total_days=actual_m5_span + 21, chunk_days=12,
    )
    h1, h1_empty, _ = _paginate_period(
        feed, period="H1", now_label=now_label, total_days=actual_m5_span + 90, chunk_days=40,
    )
    h4 = resample_bars_to_tf(h1, src_seconds=3600, dst_seconds=14400)

    # M1 (optional diagnostic window for user's M1 volume-surge/dryup method)
    m1: list[list[float]] = []
    m1_empty = 0
    m1_horizon = False
    if m1_days > 0:
        m1, m1_empty, m1_horizon = _paginate_period(
            feed,
            period="M1",
            now_label=now_label,
            total_days=m1_days,
            chunk_days=1,
            stop_after_consecutive_empty=6,
            min_day_before_stop=7,
        )

    meta = {
        "m5_empty_chunks_skipped": m5_empty,
        "m5_retention_horizon_reached": m5_horizon,
        "actual_m5_calendar_span_days": round((m5[-1][0] - m5[0][0]) / 86400.0, 2),
        "m15_empty_chunks_skipped": m15_empty,
        "h1_empty_chunks_skipped": h1_empty,
        "m1_empty_day_chunks_skipped": m1_empty,
        "m1_retention_horizon_reached": m1_horizon,
        "actual_m1_calendar_span_days": (
            round((m1[-1][0] - m1[0][0]) / 86400.0, 2) if len(m1) >= 2 else 0.0
        ),
    }
    return m5, m15, h1, h4, m1, meta


def _run_experimental_volume_check(
    *,
    spec: dict[str, Any],
    symbol: str,
    spread_price: float,
    m5: list[list[float]],
    m15: list[list[float]],
    h1: list[list[float]],
    h4: list[list[float]],
    m1: list[list[float]],
    m5_max_lot: float,
    leverage: float,
) -> dict[str, Any]:
    """Separate diagnostic testing tick-volume surge entry + volume-dryup exit."""
    rows: list[dict[str, Any]] = []

    if len(m1) >= 100:
        m1_htf = (
            build_htf_index(m5, period_seconds=300, ema_fast=9, ema_slow=21),
            build_htf_index(m15, period_seconds=900, ema_fast=9, ema_slow=21),
            build_htf_index(h1, period_seconds=3600, ema_fast=20, ema_slow=50),
        )
        m1_vol_close = replay_m1_momentum(
            m1,
            spec=spec,
            symbol=symbol,
            spread_price=spread_price,
            bar_seconds=60,
            htf_indices=m1_htf,
            entry_mode="impulse_close",
            htf_filter="m5_m15_h1",
            exit_mode="decel_and_trail",
            stop_usd=5.0,
            max_lot=0.02,
            max_legs=1,
            min_m5_adx=18.0,
            min_decel_profit_usd=1.0,
            trail_arm_usd=1.5,
            min_volume_ratio=1.4,
            volume_dryup_ratio=0.60,
            blocked_utc_hours=ROLLOVER_BLOCKED_UTC_HOURS,
            leverage=leverage,
        )
        rows.append({
            "id": "M1_VOL_CLOSE",
            "timeframe": "M1",
            "label": "M1 close | M5+M15+H1 | vol>=1.4x | exit on decel OR vol<=0.60x (>=$1) | $5",
            "summary": _plain_trade_summary(m1_vol_close),
        })

        m1_vol_pb = replay_m1_momentum(
            m1,
            spec=spec,
            symbol=symbol,
            spread_price=spread_price,
            bar_seconds=60,
            htf_indices=m1_htf,
            entry_mode="impulse_pullback",
            htf_filter="m5_m15_h1",
            exit_mode="decel_and_trail",
            stop_usd=5.0,
            max_lot=0.02,
            max_legs=1,
            min_m5_adx=18.0,
            min_decel_r=0.8,
            trail_arm_r=0.8,
            min_volume_ratio=1.4,
            volume_dryup_ratio=0.60,
            blocked_utc_hours=ROLLOVER_BLOCKED_UTC_HOURS,
            leverage=leverage,
        )
        rows.append({
            "id": "M1_VOL_PULLBACK",
            "timeframe": "M1",
            "label": "M1 pullback | M5+M15+H1 | vol>=1.4x | exit on decel OR vol<=0.60x (>=0.8R) | $5",
            "summary": _plain_trade_summary(m1_vol_pb),
        })

    if len(m5) >= 100:
        m5_htf = (
            build_htf_index(m15, period_seconds=900, ema_fast=9, ema_slow=21),
            build_htf_index(h1, period_seconds=3600, ema_fast=9, ema_slow=21),
            build_htf_index(h4, period_seconds=14400, ema_fast=20, ema_slow=50),
        )
        m5_vol = replay_m1_momentum(
            m5,
            spec=spec,
            symbol=symbol,
            spread_price=spread_price,
            bar_seconds=300,
            htf_indices=m5_htf,
            entry_mode="pullback_or_2bar",
            htf_filter="m15_h1_h4",
            exit_mode="decel_and_trail",
            stop_usd=12.0,
            max_lot=m5_max_lot,
            max_legs=1,
            min_m5_adx=18.0,
            min_decel_r=1.0,
            trail_arm_r=0.8,
            min_volume_ratio=1.3,
            volume_dryup_ratio=0.60,
            blocked_utc_hours=ROLLOVER_BLOCKED_UTC_HOURS,
            leverage=leverage,
        )
        rows.append({
            "id": "M5_VOL_DRYUP",
            "timeframe": "M5",
            "label": "M5 pullback_or_2bar | M15+H1+H4 | vol>=1.3x | exit on decel OR vol<=0.60x (>=1.0R) | $12",
            "summary": _plain_trade_summary(m5_vol),
        })

    return {
        "diagnostic_only_not_part_of_locked_oos_verdict": True,
        "note": "Tests tick-volume surge on entry + exit when momentum slows or tick volume dries up.",
        "results": rows,
    }


def _mock_180d() -> tuple[
    dict[str, Any],
    dict[str, float],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
]:
    """Deterministic 6-month synthetic M5 + 1-month M1 series for fast offline tests."""
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
    start_ep = 1780012800  # 2026-05-29T00:00:00Z
    m5: list[list[float]] = []
    px = 3350.0
    # 6 monthly blocks (spaced 30 days apart), 400 contiguous M5 bars each -> spans 150+ days cleanly
    for m_idx in range(6):
        mb = start_ep + m_idx * 30 * 86400
        for i in range(400):
            t = float(mb + i * 300)
            o = px
            phase = ((i + m_idx * 20) // 100) % 4
            is_burst = (i % 19 == 0)
            if phase == 0:
                delta = 0.6 + (4.2 if is_burst else (2.4 if i % 19 == 1 else (1.5 if i % 19 == 2 else 0.0)))
                if i % 19 == 3:
                    delta = -0.8
            elif phase == 2:
                delta = -0.6 - (4.2 if is_burst else (2.4 if i % 19 == 1 else (1.5 if i % 19 == 2 else 0.0)))
                if i % 19 == 3:
                    delta = 0.8
            else:
                delta = 0.7 * math.sin(i * 0.6 + m_idx)
            c = round(o + delta, 2)
            vol = 520.0 if is_burst else (160.0 if i % 19 == 3 else 240.0)
            m5.append([t, o, round(max(o, c) + 0.6, 2), round(min(o, c) - 0.6, 2), c, vol])
            px = c

    m1: list[list[float]] = []
    p1 = px - 20.0
    m1_base = start_ep + 5 * 30 * 86400
    for i in range(600):
        t = float(m1_base + i * 60)
        o = p1
        is_burst = (i % 17 == 0)
        delta = 0.25 + (1.7 if is_burst else (0.9 if i % 17 == 1 else (-0.3 if i % 17 == 3 else 0.0)))
        c = round(o + delta, 2)
        vol = 300.0 if is_burst else (90.0 if i % 17 == 3 else 130.0)
        m1.append([t, o, round(max(o, c) + 0.25, 2), round(min(o, c) - 0.25, 2), c, vol])
        p1 = c

    m15 = resample_bars_to_tf(m5, src_seconds=300, dst_seconds=900)
    h1 = resample_bars_to_tf(m5, src_seconds=300, dst_seconds=3600)
    h4 = resample_bars_to_tf(h1, src_seconds=3600, dst_seconds=14400)
    w_m15, w_h1, w_h4 = [], [], []
    p = 3280.0
    for k in range(60):
        ts = float(start_ep - (60 - k) * 14400)
        pn = round(p + 1.0, 2)
        w_h4.append([ts, p, pn + 2.0, p - 1.0, pn, 800.0])
        p = pn
    p = 3320.0
    for k in range(60):
        ts = float(start_ep - (60 - k) * 3600)
        pn = round(p + 0.5, 2)
        w_h1.append([ts, p, pn + 1.0, p - 0.5, pn, 400.0])
        p = pn
    p = 3340.0
    for k in range(40):
        ts = float(start_ep - (40 - k) * 900)
        pn = round(p + 0.25, 2)
        w_m15.append([ts, p, pn + 0.6, p - 0.3, pn, 250.0])
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
    p.add_argument("--m5-days", type=int, default=180,
                   help="calendar days of M5 history to paginate (90..365, default 180 ~= 6 months)")
    p.add_argument("--recent-seen-days", type=int, default=60,
                   help="most recent days already seen in screening (default 60; strictly older bars = True OOS)")
    p.add_argument("--m1-days", type=int, default=30,
                   help="calendar days of M1 history for separate volume-surge/dryup diagnostic (0..45, default 30)")
    p.add_argument("--m5-max-lot", type=float, default=0.01,
                   help=f"fixed small-capital lot cap for M5 (0.01..{HARD_MAX_LOT}, default 0.01)")
    p.add_argument("--spread-price", type=float, default=None,
                   help="override baseline spread in price units (default: live observed ask - bid)")
    p.add_argument("--leverage", type=float, default=500.0,
                   help="account leverage for margin audit (default: 500)")
    p.add_argument("--out", default="", help="optional .json filename inside ignored logs/")
    p.add_argument("--mock", action="store_true", help="run offline smoke test with synthetic 6-month data")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.symbol):
        raise SystemExit("Invalid symbol format")
    if not 90 <= args.m5_days <= 365:
        raise SystemExit("--m5-days must be between 90 and 365")
    if not 0 <= args.m1_days <= 45:
        raise SystemExit("--m1-days must be between 0 and 45")
    if not 1.0 <= args.leverage <= 2000.0:
        raise SystemExit("--leverage must be between 1 and 2000")

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    out_path = Path(args.out) if args.out else Path("logs") / f"daytrade-m5-oos-180d-{uuid.uuid4().hex[:12]}.json"

    try:
        tool_calls: dict[str, int] | None = None
        fetch_meta: dict[str, Any] = {}
        if args.mock:
            spec, quote, m5, m15, h1, h4, m1 = _mock_180d()
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
                m5, m15, h1, h4, m1, fetch_meta = _fetch_oos_history_mt5(
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

        oos = evaluate_locked_m5_oos(
            m5,
            spec=spec,
            symbol=args.symbol,
            spread_price=spread_price,
            m15_bars=m15 or None,
            h1_bars=h1 or None,
            h4_bars=h4 or None,
            max_lot=args.m5_max_lot,
            leverage=args.leverage,
            recent_seen_days=args.recent_seen_days,
        )

        vol_diag = _run_experimental_volume_check(
            spec=spec,
            symbol=args.symbol,
            spread_price=spread_price,
            m5=m5,
            m15=m15,
            h1=h1,
            h4=h4,
            m1=m1,
            m5_max_lot=args.m5_max_lot,
            leverage=args.leverage,
        )

        def _iso(ep: float) -> str:
            return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        warnings = [
            "Read-only 180-day True OOS validation: zero broker orders were created or submitted.",
            "Locked M5_5 and M5_3 parameters were NOT modified from prior 60-day screening.",
            "Experimental tick-volume diagnostic is isolated from the locked OOS pass/fail verdict.",
            "Intrabar tick path is conservatively approximated from OHLC (SL checked strictly before trail/decel).",
        ]
        if spec_audit.get("warning"):
            warnings.append(str(spec_audit["warning"]))

        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": "m5_locked_180d_true_oos_validation",
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
                "recent_seen_days": args.recent_seen_days,
                "m1_days_for_volume_diag": args.m1_days,
                "m5_max_lot": args.m5_max_lot,
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
                "m1_closed_bars": len(m1),
                "htf_coverage_pct": oos["htf_coverage_pct"],
                **fetch_meta,
            },
            "locked_m5_oos": oos,
            "experimental_volume_method_check": vol_diag,
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
