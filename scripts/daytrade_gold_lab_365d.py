#!/usr/bin/env python3
"""Strictly read-only 365-day GoldAgent Lab Head-to-Head Multi-Engine Bake-Off.

Compares 11 strategies (our locked M5_5 + real GitHub XAUUSD repos + deterministic SMC
+ vendored day-trader-skill) on the EXACT same 365-day Alpari MT5 XAUUSD_i M5 dataset
(100 oz/lot CFD Leverage, live spread, 0.01 lot, zero lookahead, conservative SL-before-TP/trail
OHLC fills, dual-half split, 4-level cost stress, + 1,000-run Monte Carlo):
  1. M5_MOMENTUM_G5           — Locked M5_5 + London/NY + H1 ADX >= 20
  2. M5_MOMENTUM_G5_ADX20_35  — EXPLORATORY: G5 skipping exhausted H1 ADX >= 35
  3. TPSL_LONDON_BREAKOUT     — GitHub TradingTPSL: Classic London Box Breakout (BE 1.5R, TP 2.5R)
  4. NY_ORB_BREAKOUT          — GitHub Rushi-Javiya/xauusd-day-trader: NY Opening Range Breakout (2R)
  5. NY_ORB_RETEST            — GitHub Rushi-Javiya/xauusd-day-trader: NY ORB Break + Retest (2R)
  6. NY_SESSION_SWEEP         — GitHub Rushi-Javiya & GoldAI: PDH/PDL/Asia/London Liquidity Sweep (2R)
  7. VOL_SQUEEZE_KELTNER      — GitHub Aurum / Carter: Bollinger-inside-Keltner Squeeze release
  8. SMC_BOS_FVG              — Deterministic SMC: Swing BOS + FVG retest (H1+H4)
  9. SMC_LIQUIDITY_SWEEP      — Deterministic SMC: Intra-session fractal Sweep rejection (H4)
 10. SKILL_BREAKOUT20_MA20    — Vendored day-trader-skill: breakout_20_bars + reclaim_ma20
 11. SKILL_RSI_BOUNCE         — Vendored day-trader-skill: RSI14 <30/>70 bounce (H4)

Strictly read-only: NEVER submits, modifies, closes, or proposes any broker orders.
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
from trade_mcp.gold_lab import evaluate_gold_lab_bakeoff  # noqa: E402
from trade_mcp.m1_momentum import split_contiguous_bars  # noqa: E402
from trade_mcp.mcpclient import McpError  # noqa: E402


def _format_summary(report: dict[str, Any]) -> str:
    q = report["live_quote"]
    h = report["history"]
    bo = report["bakeoff"]
    vd = bo["verdict"]
    lines = [
        f"OK={report['ok']} ORDERS={report['orders_submitted']} BASIS={report['parameters']['pricing_basis']} "
        f"SPREAD=${q['baseline_spread_price']} ({q['bid']}/{q['ask']})",
        f"HISTORY: M5={h['m5_closed_bars']} ({h['m5_first_utc']} -> {h['m5_last_utc']}, "
        f"{h.get('actual_m5_calendar_span_days')}d) HTF={h['htf_coverage_pct']}",
        f"VERDICT: {vd['status']} | passed={vd['passed_ids']} | "
        f"recommended={vd['recommended_for_live_paper']} | best_overall={vd['best_overall_diagnostic_id']}",
        "\n=== HEAD-TO-HEAD 365-DAY GOLDAGENT LAB BAKE-OFF (0.01 lot | $12 stop | London+NY) ===",
    ]
    for e in bo["engines"]:
        fp = e["full_period"]
        h1 = e["half1_older_180d"]
        h2 = e["half2_recent_180d"]
        mc = e["monte_carlo"]
        st = e["full_cost_stress"]
        flag = " [EXPLORATORY]" if e["exploratory_in_sample_flag"] else ""
        lines.append(
            f"\n[{e['id']}]{flag} ({e['family']}) | gates={e['gates_passed_count']}/{len(e['gates'])} "
            f"pass={e['all_gates_passed']} failed={e['failed_gates']}"
        )
        lines.append(
            f"  FULL: {fp['trades']}t ({fp['wins']}W/{fp['losses']}L WR={fp['win_rate_pct']}%) "
            f"net=${fp['net_pnl_usd']:+.2f} PF={fp['pf']} DD=${fp['max_dd_usd']} "
            f"P/DD={fp['profit_to_dd_ratio']} exclBest=${fp['net_without_best_trade_usd']:+.2f} "
            f"maxStreak={fp['max_loss_streak']} posMonths={e['active_months_positive']}"
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
            f"  MONTE_CARLO({mc['trials']}): medDD=${mc['median_max_dd_usd']} "
            f"p95DD=${mc['p95_max_dd_usd']} p99DD=${mc['p99_max_dd_usd']} "
            f"P(DD>$100)={mc['prob_dd_gt_100_pct']}%"
        )
        lines.append(
            "  STRESS: "
            f"base=${st['base_live_spread']['net_pnl_usd']:+.2f}(PF={st['base_live_spread']['pf']}) | "
            f"0.50=${st['stress_spread_050']['net_pnl_usd']:+.2f}(PF={st['stress_spread_050']['pf']}) | "
            f"0.50+$5=${st['stress_spread_050_comm5']['net_pnl_usd']:+.2f}(PF={st['stress_spread_050_comm5']['pf']}) | "
            f"0.70+$7=${st['stress_spread_070_comm7']['net_pnl_usd']:+.2f}(PF={st['stress_spread_070_comm7']['pf']})"
        )
        lines.append(f"  CAPITAL_GUIDE: {e['capital_sizing_guide']}")

    best_id = vd["recommended_for_live_paper"] or vd["best_overall_diagnostic_id"]
    if best_id:
        be = next(x for x in bo["engines"] if x["id"] == best_id)
        lines.append(f"\n=== MONTHLY BREAKDOWN ({best_id}) ===")
        for m in be["monthly_breakdown"]:
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
    p.add_argument("--m5-days", type=int, default=365, help="calendar days of M5 history (90..400, default 365)")
    p.add_argument("--split-recent-days", type=int, default=180, help="recent half window in days (default 180)")
    p.add_argument("--max-lot", type=float, default=0.01, help=f"lot cap (0.01..{HARD_MAX_LOT}, default 0.01)")
    p.add_argument("--mc-trials", type=int, default=1000, help="Monte Carlo reshuffle trials (default 1000)")
    p.add_argument("--spread-price", type=float, default=None, help="override spread in price units")
    p.add_argument("--leverage", type=float, default=500.0, help="account leverage for margin audit")
    p.add_argument("--out", default="", help="optional .json path inside ignored logs/")
    p.add_argument("--summary", action="store_true", help="print compact human-readable report to stdout")
    p.add_argument("--mock", action="store_true", help="run offline smoke test with synthetic 10-month data")
    return p


def main(argv: list[str] | None = None) -> int:
    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    args = build_parser().parse_args(argv)

    if not (90 <= args.m5_days <= 400):
        raise SystemExit("--m5-days must be between 90 and 400")
    if not (0.01 <= args.max_lot <= HARD_MAX_LOT):
        raise SystemExit(f"--max-lot must be between 0.01 and {HARD_MAX_LOT}")
    if not (100 <= args.mc_trials <= 5000):
        raise SystemExit("--mc-trials must be between 100 and 5000")

    out_path = Path(args.out) if args.out else Path("logs") / f"{timestamp()}-gold-lab-365d.json"

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

        bakeoff = evaluate_gold_lab_bakeoff(
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
            mc_trials=args.mc_trials,
        )

        def _iso(ep: float) -> str:
            return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": "gold_lab_365d_multi_engine_bakeoff",
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
                "mc_trials": args.mc_trials,
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
                "htf_coverage_pct": bakeoff["htf_coverage_pct"],
                **fetch_meta,
            },
            "bakeoff": bakeoff,
            "warnings": [
                "Strictly read-only 365-day multi-engine bake-off: zero broker orders submitted.",
                "M5_MOMENTUM_G5_ADX20_35 is flagged exploratory_in_sample_flag=True; require fresh OOS before relying on it.",
            ],
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
