#!/usr/bin/env python3
"""Read-only Multi-Capital Dynamic Sizing (0.01..0.10) & Combined Champions Portfolio Lab.

Benchmarks our two verified Alpari XAUUSD_i champions solo and RUNNING TOGETHER
across starting capitals $100, $1,000, and $4,000 with dynamic risk-based lot sizing
in [0.01 .. 0.10] (tighter stop => larger lot, wider stop => smaller lot):

  1. M5_MOMENTUM_G5_ADX20_35 (9/9 gates PASS over ~517d M5)
  2. H1_DONCHIAN50_LONDON    (audited year-by-year up to 5 years / 1,825d H1, ATR>=5 vs ATR>=8)
  3. BOTH_COMBINED           (both engines running simultaneously on the SAME account)

Strictly read-only: NEVER imports execution modules, NEVER writes .env, submits 0 orders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
os.environ["TRADE_MCP_PAPER_TRADING"] = "false"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_2yr_oos_and_github_lab import _mock_2yr_data, _paginate_period_retry  # noqa: E402
from daytrade_m1_paper_window import ReadOnlyNativeFeed, _save_private_json  # noqa: E402
from daytrade_mt5_readonly_check import Blocked, loopback_native, timestamp  # noqa: E402

from trade_mcp.bridge.native import find  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.daytrade_paper import audit_symbol_spec, resolve_pricing_basis  # noqa: E402
from trade_mcp.m1_momentum import resample_bars_to_tf  # noqa: E402
from trade_mcp.mcpclient import McpError  # noqa: E402
from trade_mcp.portfolio_sizing_lab import evaluate_champions_portfolio_lab  # noqa: E402


def _fetch_champions_history_mt5(
    feed: ReadOnlyNativeFeed,
    *,
    m5_days: int,
    h1_days: int,
) -> tuple[
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    dict[str, Any],
]:
    """Fetch full retained M5 (~520d) + M15 + up to 5 years (1825d) of H1 from local MT5."""
    try:
        clock = feed._call("get_time_information", {})
    except Blocked as exc:
        raise Blocked(f"get_time_information:{exc.reason}") from exc
    raw_time = find(clock, "trade_server_time")
    if raw_time is None:
        raw_time = find(clock, "trade_server_last_known_time")
    dt = timestamp(raw_time)
    if dt is None:
        raise Blocked("timestamp_unverified")
    now_label = dt.replace(tzinfo=None)

    m5, m5_meta = _paginate_period_retry(
        feed, period="M5", now_label=now_label, total_days=m5_days, chunk_days=4, stop_after_consecutive_empty=4,
    )
    if len(m5) < 200:
        raise Blocked("insufficient_history_m5")
    m15_target = min(max(int(m5_meta["span_days"]) + 20, 60), m5_days + 20)
    m15, m15_meta = _paginate_period_retry(
        feed, period="M15", now_label=now_label, total_days=m15_target, chunk_days=12, stop_after_consecutive_empty=4,
    )
    h1, h1_meta = _paginate_period_retry(
        feed, period="H1", now_label=now_label, total_days=h1_days, chunk_days=30, stop_after_consecutive_empty=3,
    )
    h4 = resample_bars_to_tf(h1, src_seconds=3600, dst_seconds=14400)
    return m5, m15, h1, h4, {
        "mode": "live_mt5_readonly",
        "m5": m5_meta,
        "m15": m15_meta,
        "h1": h1_meta,
        "h4_resampled_bars": len(h4),
    }


def _format_summary(report: dict[str, Any]) -> str:
    fm = report["fetch_meta"]
    lab = report["lab"]
    q = report["live_quote"]
    ov = lab["overlap_window"]
    h1a = lab["h1_multiyear_audit"]

    lines = [
        f"=== CHAMPIONS MULTI-CAPITAL DYNAMIC SIZING (0.01..0.10) & COMBINED PORTFOLIO ({report['symbol']}) ===",
        f"OK={report['ok']} ORDERS={report['orders_submitted']} MODE={fm.get('mode')} "
        f"BASIS={report['parameters']['pricing_basis']} SPREAD=${q['baseline_spread_price']} "
        f"ELAPSED={report['elapsed_seconds']}s",
        f"OVERLAP WINDOW (M5+H1): {ov['span_days']}d {ov['range_utc']} | "
        f"M5={ov['m5_trades']}t | H1={ov['h1_overlap_trades']}t | COMBINED={ov['combined_trades']}t | "
        f"Monthly Corr(M5,H1)={ov['monthly_pnl_correlation']}",
        "",
        f"=== PART 1: H1_DONCHIAN50_LONDON MULTI-YEAR AUDIT ({h1a['h1_span_days']}d {h1a['h1_range_utc']}, 0.01 lot) ===",
    ]
    for label, key in (("H1 ATR >= $5.0 (standard)", "atr_ge_5"), ("H1 ATR >= $8.0 (high-vol filter)", "atr_ge_8")):
        blk = h1a[key]
        s = blk["summary_001_lot"]
        lines.append(
            f"  [{label}] TOTAL: {s['trades']}t ({s['wins']}W/{s['losses']}L, WR {s['win_rate_pct']}%) "
            f"net=${s['net_pnl_usd']:+.2f} PF={s['pf']} DD=${s['max_dd_usd']:.2f} exclBest=${s['net_without_best_trade_usd']:+.2f}"
        )
        for yr in blk["by_year"]:
            lines.append(
                f"     {yr['year']}: {yr['trades']:3d}t ({yr['wins']:2d}W/{yr['losses']:2d}L, WR {yr['win_rate_pct']:5.1f}%) "
                f"net=${yr['net_pnl_usd']:+8.2f} PF={str(yr['pf']):>6s} DD=${yr['max_dd_usd']:6.2f}"
            )

    lines.append("")
    lines.append("=== PART 2: DYNAMIC SIZING (0.01..0.10 LOT) ACROSS $100 / $1,000 / $4,000 (SOLO vs COMBINED) ===")
    for _cap_key, cap_blk in lab["capital_matrix"].items():
        cap = cap_blk["initial_capital_usd"]
        mr = cap_blk["min_001_lot_risk_pct"]
        sf = cap_blk["strict_2pct_feasibility"]
        lines.append(
            f"\n--- STARTING CAPITAL: ${cap:,.0f} "
            f"(0.01 lot min risk: M5={mr['m5_stop_12']}% | H1={mr['h1_stop_25']}% | "
            f"strict 2% doable on BOTH: {sf['BOTH_COMBINED']['trades_taken_strict_2pct']}/"
            f"{sf['BOTH_COMBINED']['trades_taken_strict_2pct'] + sf['BOTH_COMBINED']['trades_skipped_min_lot_too_big']} trades) ---"
        )
        for b_id in ("M5_ONLY", "H1_ONLY_OVERLAP", "BOTH_COMBINED", "H1_FULL_MULTIYEAR"):
            b = cap_blk["bundles"][b_id]
            lines.append(f"  [{b_id}] {b['label']}")
            for pr in b["profiles"]:
                sim = pr["simulation"]
                mc = pr["monte_carlo"]
                ls = sim["lot_stats"]
                cc = sim["concurrency"]
                blown_tag = f"BLOWN@{sim['blown_at_trade']}" if sim["blown_account"] else "OK"
                lines.append(
                    f"    {pr['profile_id']:17s} | End=${sim['final_equity_usd']:9,.2f} ({sim['return_pct']:+7.1f}%) "
                    f"MaxDD={sim['max_dd_pct']:5.1f}% (${sim['max_dd_usd']:7,.2f}) PF={str(sim['pf']):>5s} "
                    f"Lot[{ls['min_lot']:.2f}/{ls['avg_lot']:.2f}/{ls['max_lot']:.2f}] "
                    f"OpenMax={cc['max_simultaneous_open']} MargPk={cc['peak_margin_pct']:4.1f}% [{blown_tag}] "
                    f"| MC(Med=${mc['median_final_equity_usd']:,.0f} P5=${mc['p05_final_equity_usd']:,.0f} "
                    f"P95DD={mc['p95_max_dd_pct']:4.1f}% Blown={mc['prob_blown_pct']:4.1f}%)"
                )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    t0 = time.monotonic()
    p = argparse.ArgumentParser(description="Read-only multi-capital dynamic sizing & combined champions lab.")
    p.add_argument("--symbol", default="XAUUSD_i")
    p.add_argument("--m5-days", type=int, default=540, help="Target calendar days for M5 (Alpari retains ~517d)")
    p.add_argument("--h1-days", type=int, default=1825, help="Target calendar days for H1 (default 1825 = 5 years)")
    p.add_argument("--capitals", default="100,1000,4000", help="Comma-separated starting capitals in USD")
    p.add_argument("--min-lot", type=float, default=0.01)
    p.add_argument("--max-lot", type=float, default=0.10)
    p.add_argument("--leverage", type=float, default=500.0)
    p.add_argument("--spread-price", type=float, default=None)
    p.add_argument("--mc-trials", type=int, default=1000)
    p.add_argument("--mock", action="store_true", help="Offline smoke run")
    p.add_argument("--summary", action="store_true", help="Print readable summary table")
    p.add_argument("--out", default=None, help="Output JSON path inside ignored logs/")
    args = p.parse_args(argv)

    capitals = tuple(float(x.strip()) for x in args.capitals.split(",") if x.strip())
    if not capitals or any(c <= 0 for c in capitals):
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": "invalid capitals"}))
        return 2
    if args.min_lot < 0.01 or args.max_lot > 0.10 or args.min_lot > args.max_lot:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": "lots must stay within [0.01, 0.10]"}))
        return 2

    out_path = Path(args.out) if args.out else (ROOT / "logs" / "champions_portfolio_lab.json")

    try:
        tool_calls: dict[str, int] | None = None
        if args.mock:
            spec, quote, m5, m15, h1, h4 = _mock_2yr_data()
            fetch_meta: dict[str, Any] = {
                "mode": "mock_offline",
                "m5": {"bars": len(m5), "span_days": round((m5[-1][0] - m5[0][0]) / 86400.0, 2)},
                "m15": {"bars": len(m15), "span_days": round((m15[-1][0] - m15[0][0]) / 86400.0, 2)},
                "h1": {"bars": len(h1), "span_days": round((h1[-1][0] - h1[0][0]) / 86400.0, 2)},
            }
        else:
            load_dotenv()
            cfg = Config.from_env()
            if cfg.backend != "native":
                raise Blocked("native_backend_required")
            with loopback_native():
                feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, args.symbol)
                try:
                    spec, quote = feed.connect()
                except Blocked as exc:
                    raise Blocked(f"connect:{exc.reason}") from exc
                m5, m15, h1, h4, fetch_meta = _fetch_champions_history_mt5(
                    feed, m5_days=args.m5_days, h1_days=args.h1_days,
                )
                tool_calls = dict(feed.tool_calls)

        spread_price = (
            float(args.spread_price)
            if args.spread_price is not None
            else round(max(0.0, quote["ask"] - quote["bid"]), 6)
        )
        spec_audit = audit_symbol_spec(spec)
        primary_basis, primary_mult = resolve_pricing_basis(spec, "auto")

        lab = evaluate_champions_portfolio_lab(
            m5_bars=m5,
            m15_bars=m15,
            h1_bars=h1,
            h4_bars=h4,
            spec=spec,
            symbol=args.symbol,
            spread_price=spread_price,
            leverage=args.leverage,
            capitals=capitals,
            min_lot=args.min_lot,
            max_lot=args.max_lot,
            mc_trials=args.mc_trials,
        )

        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": "champions_portfolio_dynamic_sizing_lab",
            "executable": False,
            "execution_authorized": False,
            "orders_submitted": 0,
            "symbol": args.symbol,
            "elapsed_seconds": round(time.monotonic() - t0, 2),
            "live_quote": {
                "bid": quote["bid"],
                "ask": quote["ask"],
                "observed_spread_price": round(quote["ask"] - quote["bid"], 6),
                "baseline_spread_price": spread_price,
            },
            "spec_audit": spec_audit,
            "parameters": {
                "m5_days": args.m5_days,
                "h1_days": args.h1_days,
                "capitals": list(capitals),
                "min_lot": args.min_lot,
                "max_lot": args.max_lot,
                "mc_trials": args.mc_trials,
                "pricing_basis": primary_basis,
                "effective_multiplier": primary_mult,
            },
            "fetch_meta": fetch_meta,
            "lab": lab,
        }
        if tool_calls is not None:
            report["mt5_tool_calls"] = tool_calls

        saved = _save_private_json(out_path, report)
        report["log_path"] = str(saved or out_path)
        if args.summary:
            print(_format_summary(report))
        else:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    except Blocked as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "blocked": exc.reason}, indent=2))
        return 1
    except (McpError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
