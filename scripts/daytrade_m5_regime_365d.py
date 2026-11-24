#!/usr/bin/env python3
"""Strictly read-only 365-day market-regime diagnostic & filter validation for locked M5_5.

Holds locked M5_5 signal logic 100% FIXED (pullback_or_2bar | M15+H1+H4 | trail_only 0.8R
| $12 stop | 0.01 lot) and tests only 2 physical gold-market regime controls:
  1. Active institutional trading sessions (London+NY 07-20 UTC / Core 07-17 UTC vs 22h)
  2. Closed H1 trend-strength gate (H1 ADX >= 20 / >= 25)
plus a baseline anatomy breakdown by UTC session and closed H1 ADX across BOTH 6-month halves.

Enforces strict dual-half anti-overfitting gates and submits ZERO orders or trade proposals.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_m1_paper_window import ReadOnlyNativeFeed, _save_private_json  # noqa: E402
from daytrade_m5_risk_brakes_365d import (  # noqa: E402
    _fetch_365d_m5_history_mt5,
    _mock_365d,
)
from daytrade_mt5_readonly_check import Blocked, loopback_native, timestamp  # noqa: E402

from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.daytrade_paper import (  # noqa: E402
    HARD_MAX_LOT,
    audit_symbol_spec,
    resolve_pricing_basis,
)
from trade_mcp.m1_momentum import (  # noqa: E402
    evaluate_m5_regime_filters,
    split_contiguous_bars,
)
from trade_mcp.mcpclient import McpError  # noqa: E402


def _format_compact_summary(report: dict[str, Any]) -> str:
    lines: list[str] = []
    q = report["live_quote"]
    h = report["history"]
    rv = report["regime_validation"]
    sp = rv["split"]
    vd = rv["verdict"]
    lines.append(
        f"OK={report['ok']} ORDERS={report['orders_submitted']} "
        f"BASIS={report['parameters']['pricing_basis']} "
        f"SPREAD=${q['baseline_spread_price']} ({q['bid']}/{q['ask']})"
    )
    lines.append(
        f"HISTORY: M5={h['m5_closed_bars']} ({h['m5_first_utc']} -> {h['m5_last_utc']}, "
        f"{h.get('actual_m5_calendar_span_days')}d, retention_hit={h.get('m5_retention_horizon_reached')}) "
        f"HTF={h['htf_coverage_pct']}"
    )
    lines.append(
        f"SPLIT: half1={sp['half1_older_bars']} ({sp['half1_older_span_days']}d {sp['half1_older_range_utc']}) | "
        f"half2={sp['half2_recent_bars']} ({sp['half2_recent_span_days']}d {sp['half2_recent_range_utc']})"
    )
    lines.append(
        f"VERDICT: {vd['status']} | passed={vd['passed_ids']} | "
        f"recommended={vd['recommended_for_live_paper']} | best_diag={vd['best_diagnostic_id']}"
    )

    anat = rv.get("baseline_regime_anatomy") or {}
    lines.append("\n=== G0_BASE ANATOMY: BY UTC SESSION (FULL | HALF1 | HALF2) ===")
    for k, blk in (anat.get("by_utc_session") or {}).items():
        f, h1, h2 = blk["full"], blk["half1_older"], blk["half2_recent"]
        lines.append(
            f"  {k:22s} | FULL: {f['trades']:3d}t {f['wins']:2d}W/{f['losses']:2d}L net=${f['net_pnl_usd']:+7.2f} PF={f['profit_factor']} "
            f"| H1: {h1['trades']:3d}t ${h1['net_pnl_usd']:+7.2f} PF={h1['profit_factor']} "
            f"| H2: {h2['trades']:3d}t ${h2['net_pnl_usd']:+7.2f} PF={h2['profit_factor']}"
        )

    lines.append("\n=== G0_BASE ANATOMY: BY CLOSED H1 ADX (FULL | HALF1 | HALF2) ===")
    for k, blk in (anat.get("by_closed_h1_adx") or {}).items():
        f, h1, h2 = blk["full"], blk["half1_older"], blk["half2_recent"]
        lines.append(
            f"  {k:22s} | FULL: {f['trades']:3d}t {f['wins']:2d}W/{f['losses']:2d}L net=${f['net_pnl_usd']:+7.2f} PF={f['profit_factor']} "
            f"| H1: {h1['trades']:3d}t ${h1['net_pnl_usd']:+7.2f} PF={h1['profit_factor']} "
            f"| H2: {h2['trades']:3d}t ${h2['net_pnl_usd']:+7.2f} PF={h2['profit_factor']}"
        )

    lines.append("\n=== REGIME VARIANTS ===")
    for v in rv["variants"]:
        fp = v["full_period"]
        h1 = v["half1_older_180d"]
        h2 = v["half2_recent_180d"]
        st = v["full_cost_stress"]
        total_g = len(v["gates"])
        lines.append(
            f"\n[{v['id']}] {v['label']} | gates={v['gates_passed_count']}/{total_g} "
            f"pass={v['all_gates_passed']} failed={v['failed_gates']}"
        )
        lines.append(
            f"  FULL: {fp['trades']}t ({fp['wins']}W/{fp['losses']}L WR={fp['win_rate_pct']}%) "
            f"net=${fp['net_pnl_usd']:+.2f} PF={fp['pf']} DD=${fp['max_dd_usd']} "
            f"P/DD={fp['profit_to_dd_ratio']} exclBest=${fp['net_without_best_trade_usd']:+.2f} "
            f"maxLossStreak={fp['max_loss_streak']} posMonths={v['active_months_positive']}"
        )
        if h1:
            lines.append(
                f"  H1 (Nov-May): {h1['trades']}t ({h1['wins']}W/{h1['losses']}L) "
                f"net=${h1['net_pnl_usd']:+.2f} PF={h1['pf']} DD=${h1['max_dd_usd']} "
                f"exclBest=${h1['net_without_best_trade_usd']:+.2f}"
            )
        if h2:
            lines.append(
                f"  H2 (May-Nov): {h2['trades']}t ({h2['wins']}W/{h2['losses']}L) "
                f"net=${h2['net_pnl_usd']:+.2f} PF={h2['pf']} DD=${h2['max_dd_usd']} "
                f"exclBest=${h2['net_without_best_trade_usd']:+.2f}"
            )
        lines.append(
            "  STRESS: "
            f"base=${st['base_live_spread']['net_pnl_usd']:+.2f}(PF={st['base_live_spread']['pf']}) | "
            f"0.50=${st['stress_spread_050']['net_pnl_usd']:+.2f}(PF={st['stress_spread_050']['pf']}) | "
            f"0.50+$5=${st['stress_spread_050_comm5']['net_pnl_usd']:+.2f}(PF={st['stress_spread_050_comm5']['pf']}) | "
            f"0.70+$7=${st['stress_spread_070_comm7']['net_pnl_usd']:+.2f}(PF={st['stress_spread_070_comm7']['pf']})"
        )
        lines.append(
            f"  FILTERS={v['filters_triggered']} | CAPITAL_GUIDE={v['capital_sizing_guide']}"
        )

    best_id = vd["recommended_for_live_paper"] or vd["best_diagnostic_id"]
    if best_id:
        bv = next(x for x in rv["variants"] if x["id"] == best_id)
        lines.append(f"\n=== MONTHLY BREAKDOWN ({best_id}) ===")
        for m in bv["monthly_breakdown"]:
            lines.append(
                f"  {m['month']}: {m['trades']:2d}t ({m['wins']:2d}W/{m['losses']:2d}L) "
                f"net=${m['net_pnl_usd']:+7.2f} PF={m['pf']}"
            )
    if report.get("log_path"):
        lines.append(f"\nSAVED: {report['log_path']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="XAUUSD_i", help="exact broker symbol (default: XAUUSD_i)")
    p.add_argument("--m5-days", type=int, default=365,
                   help="calendar days of M5 history to paginate (90..400, default 365 ~= 1 year)")
    p.add_argument("--split-recent-days", type=int, default=180,
                   help="recent half window in days for dual-half test (default 180)")
    p.add_argument("--max-lot", type=float, default=0.01,
                   help=f"fixed small-capital lot cap (0.01..{HARD_MAX_LOT}, default 0.01)")
    p.add_argument("--spread-price", type=float, default=None,
                   help="override baseline spread in price units (default: live observed ask - bid)")
    p.add_argument("--leverage", type=float, default=500.0,
                   help="account leverage for margin audit (default: 500)")
    p.add_argument("--out", default="", help="optional .json filename inside ignored logs/")
    p.add_argument("--summary", action="store_true",
                   help="print compact human-readable report to stdout (full JSON still saved to --out)")
    p.add_argument("--mock", action="store_true", help="run offline smoke test with synthetic 10-month data")
    return p


def main(argv: list[str] | None = None) -> int:
    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    args = build_parser().parse_args(argv)

    if not (90 <= args.m5_days <= 400):
        raise SystemExit("--m5-days must be between 90 and 400")
    if not (30 <= args.split_recent_days <= 240):
        raise SystemExit("--split-recent-days must be between 30 and 240")
    if not (0.01 <= args.max_lot <= HARD_MAX_LOT):
        raise SystemExit(f"--max-lot must be between 0.01 and {HARD_MAX_LOT}")

    out_path = Path(args.out) if args.out else Path("logs") / f"{timestamp()}-m5-regime-365d.json"

    try:
        tool_calls: dict[str, int] | None = None
        if args.mock:
            spec, quote, m5, m15, h1, h4 = _mock_365d()
            fetch_meta: dict[str, Any] = {
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

        result = evaluate_m5_regime_filters(
            m5,
            spec=spec,
            symbol=args.symbol,
            spread_price=spread_price,
            m15_bars=m15 or None,
            h1_bars=h1 or None,
            h4_bars=h4 or None,
            max_lot=args.max_lot,
            leverage=args.leverage,
            split_recent_days=args.split_recent_days,
        )

        def _iso(ep: float) -> str:
            return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        warnings = [
            "Read-only 365-day market-regime test: zero broker orders were created or submitted.",
            "Locked M5_5 signal logic was held 100% constant; only session & closed H1 ADX filters varied.",
            "Dual-half anti-overfitting gate active: both 6-month halves must improve independently.",
        ]
        if spec_audit.get("warning"):
            warnings.append(str(spec_audit["warning"]))

        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": "m5_locked_regime_365d_validation",
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
                "split_recent_days": args.split_recent_days,
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
            "regime_validation": result,
            "warnings": warnings,
        }
        if tool_calls is not None:
            report["mt5_tool_calls"] = tool_calls

        saved = _save_private_json(out_path, report)
        report["log_path"] = str(saved or out_path)
        if args.summary:
            print(_format_compact_summary(report))
        else:
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
