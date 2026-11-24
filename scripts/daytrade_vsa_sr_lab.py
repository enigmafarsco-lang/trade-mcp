#!/usr/bin/env python3
"""Read-only Instagram 2-Candle VSA / Wick-Body / Acceleration & Support-Resistance Lab.

Tests directly on real Alpari MT5 XAUUSD_i history (M5 ~517d, M15 ~744d, H1 up to 5 years):
  Part 1: Exact statistical hit-rate of predicting the NEXT 1 & NEXT 2 candles from
          2 same-color candles + wick/body ratio + acceleration + tick volume + 1H/5H trend
          (showing BOTH gross accuracy and net result AFTER Alpari's ~$0.33 spread!).
  Part 2: 7 full causal strategy backtests (Instagram hold-2-bars on M5/M15/H1,
          M15 VSA acceleration + trail, Causal S/R Breakout + VSA, Causal S/R Rejection Bounce).

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
from trade_mcp.daytrade_paper import HARD_MAX_LOT, audit_symbol_spec, resolve_pricing_basis  # noqa: E402
from trade_mcp.mcpclient import McpError  # noqa: E402
from trade_mcp.vsa_sr_lab import evaluate_vsa_and_sr_lab  # noqa: E402


def _fetch_vsa_history_mt5(
    feed: ReadOnlyNativeFeed,
    *,
    m5_days: int,
    m15_days: int,
    h1_days: int,
) -> tuple[list[list[float]], list[list[float]], list[list[float]], dict[str, Any]]:
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
    m15, m15_meta = _paginate_period_retry(
        feed, period="M15", now_label=now_label, total_days=m15_days, chunk_days=12, stop_after_consecutive_empty=4,
    )
    h1, h1_meta = _paginate_period_retry(
        feed, period="H1", now_label=now_label, total_days=h1_days, chunk_days=30, stop_after_consecutive_empty=3,
    )
    return m5, m15, h1, {"mode": "live_mt5_readonly", "m5": m5_meta, "m15": m15_meta, "h1": h1_meta}


def _format_summary(report: dict[str, Any]) -> str:
    lab = report["lab"]
    q = report["live_quote"]
    lines = [
        f"=== INSTAGRAM 2-CANDLE VSA / WICK-BODY / ACCEL & SUPPORT-RESISTANCE LAB ({report['symbol']}) ===",
        f"OK={report['ok']} ORDERS={report['orders_submitted']} BASIS={report['parameters']['pricing_basis']} "
        f"SPREAD=${q['baseline_spread_price']} ELAPSED={report['elapsed_seconds']}s",
        f"RANGES: M5={lab['ranges']['M5']} | M15={lab['ranges']['M15']} | H1={lab['ranges']['H1']}",
        "",
        "=== PART 1: NEXT 1 & NEXT 2 CANDLE PREDICTABILITY (vs 50% coin-flip, AFTER live spread) ===",
    ]
    for tf in ("M5", "M15", "H1"):
        blk = lab["predictability"][tf]
        lines.append(f"  [{tf}] ({blk['bars']} bars, spread=${blk['spread_price']})")
        for pat, st in blk["patterns"].items():
            if st.get("n", 0) == 0:
                lines.append(f"    {pat:28s}: n=0")
                continue
            lines.append(
                f"    {pat:28s}: n={st['n']:5d} | next1={st['next1_same_dir_pct']:5.1f}% "
                f"both2={st['next2_both_same_dir_pct']:5.1f}% | 2barWin(gross)={st['next2_win_gross_pct']:5.1f}% "
                f"2barWin(net)={st['next2_win_after_spread_pct']:5.1f}% | "
                f"avg2bar=${st['avg_2bar_move_price']:+.3f} netAfterSpread=${st['avg_2bar_net_after_spread_price']:+.3f}/oz"
            )

    lines.append("")
    lines.append("=== PART 2: FULL CAUSAL STRATEGY BACKTESTS (0.01 lot | 100 oz | live spread) ===")
    for e in lab["engines"]:
        f = e["full"]
        mc = e["monte_carlo"]
        h1 = e["half1"]
        h2 = e["half2"]
        h_str = ""
        if h1 and h2:
            h_str = f" | H1=${h1['net_pnl_usd']:+.2f}(PF {h1['pf']}) H2=${h2['net_pnl_usd']:+.2f}(PF {h2['pf']})"
        lines.append(
            f"  [{e['id']:24s}] ({e['tf']:3s}) {f['trades']:4d}t ({f['wins']}W/{f['losses']}L, WR {f['win_rate_pct']:5.1f}%) "
            f"net=${f['net_pnl_usd']:+8.2f} PF={str(f['pf']):>5s} DD=${f['max_dd_usd']:6.2f} P/DD={str(f['profit_to_dd_ratio']):>5s} "
            f"exclBest=${f['net_without_best_trade_usd']:+7.2f}{h_str}\n"
            f"     MC(p95DD=${mc['p95_max_dd_usd']:.2f}) | Stress $0.50+$5=${e['stress_050_c5']['net_pnl_usd']:+.2f} "
            f"$0.70+$7=${e['stress_070_c7']['net_pnl_usd']:+.2f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    t0 = time.monotonic()
    p = argparse.ArgumentParser(description="Read-only Instagram 2-candle VSA & Support/Resistance lab.")
    p.add_argument("--symbol", default="XAUUSD_i")
    p.add_argument("--m5-days", type=int, default=540)
    p.add_argument("--m15-days", type=int, default=750)
    p.add_argument("--h1-days", type=int, default=1825)
    p.add_argument("--max-lot", type=float, default=0.01)
    p.add_argument("--leverage", type=float, default=500.0)
    p.add_argument("--spread-price", type=float, default=None)
    p.add_argument("--mc-trials", type=int, default=1000)
    p.add_argument("--mock", action="store_true")
    p.add_argument("--summary", action="store_true")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)

    if args.max_lot <= 0 or args.max_lot > HARD_MAX_LOT:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"max_lot must be <= {HARD_MAX_LOT}"}))
        return 2

    out_path = Path(args.out) if args.out else (ROOT / "logs" / "vsa_sr_lab.json")
    try:
        tool_calls: dict[str, int] | None = None
        if args.mock:
            spec, quote, m5, m15, h1, _h4 = _mock_2yr_data()
            fetch_meta: dict[str, Any] = {"mode": "mock_offline"}
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
                m5, m15, h1, fetch_meta = _fetch_vsa_history_mt5(
                    feed, m5_days=args.m5_days, m15_days=args.m15_days, h1_days=args.h1_days,
                )
                tool_calls = dict(feed.tool_calls)

        spread_price = (
            float(args.spread_price)
            if args.spread_price is not None
            else round(max(0.0, quote["ask"] - quote["bid"]), 6)
        )
        spec_audit = audit_symbol_spec(spec)
        primary_basis, primary_mult = resolve_pricing_basis(spec, "auto")
        lab = evaluate_vsa_and_sr_lab(
            m5_bars=m5,
            m15_bars=m15,
            h1_bars=h1,
            spec=spec,
            spread_price=spread_price,
            max_lot=args.max_lot,
            leverage=args.leverage,
            mc_trials=args.mc_trials,
        )
        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": "vsa_2candle_and_sr_lab",
            "executable": False,
            "execution_authorized": False,
            "orders_submitted": 0,
            "symbol": args.symbol,
            "elapsed_seconds": round(time.monotonic() - t0, 2),
            "live_quote": {
                "bid": quote["bid"],
                "ask": quote["ask"],
                "baseline_spread_price": spread_price,
            },
            "spec_audit": spec_audit,
            "parameters": {
                "max_lot": args.max_lot,
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
