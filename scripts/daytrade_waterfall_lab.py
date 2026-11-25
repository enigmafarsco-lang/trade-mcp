#!/usr/bin/env python3
"""Strictly read-only Fast Waterfall / Cascade Trend-Acceleration Lab (M1 45d + M5 517d).

Tests 16 fast, zero-H4-lag, uncapped-ADX waterfall/cascade engines (5-min to 30-min unbroken
directional waves with Kaufman Efficiency Ratio + EMA slope acceleration + Fade controls)
on real Alpari XAUUSD_i history (ZERO orders ever submitted).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_2yr_oos_and_github_lab import _paginate_period_retry  # noqa: E402
from daytrade_m1_paper_window import (  # noqa: E402
    ReadOnlyNativeFeed,
    _save_private_json,
    enrich_spec,
)
from daytrade_mt5_readonly_check import (  # noqa: E402
    Blocked,
    exact_symbol,
    loopback_native,
    reported_spec,
    timestamp,
)
from daytrade_mt5_readonly_check import quote as parse_quote  # noqa: E402

from trade_mcp.bridge.native import McpError, find  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.waterfall_momentum_lab import (  # noqa: E402
    evaluate_waterfall_suite,
    make_synthetic_waterfall_bars,
)


def _fmt(v: float | None) -> str:
    return " None" if v is None else f"{v:5.2f}"


def _print_summary(report: dict[str, Any]) -> None:
    print("=== FAST WATERFALL / CASCADE MOMENTUM LAB (ZERO H4 LAG | UNCAPPED ADX | 0.01 LOT) ===")
    print(
        f"OK={report['ok']} ORDERS={report['orders_submitted']} SYMBOL={report['symbol']} "
        f"SPREAD={report['live_spread']} MODE={report['mode']} ELAPSED={report['elapsed_sec']}s"
    )
    for tf_rep in report.get("timeframes", []):
        tf = tf_rep["timeframe"]
        if "error" in tf_rep:
            print(f"\n[{tf}] ERROR: {tf_rep['error']}")
            continue
        print(f"\n==================== {tf} ({tf_rep['bars']} bars | {tf_rep['span_days']}d | spread={tf_rep['spread_price']}) ====================")
        for r in tf_rep["engines"]:
            b = r["base"]
            mark = "PASS" if r["passed"] else "    "
            print(
                f"  [{mark}] {r['engine']:28s} {b['trades']:4d}t ({r['trades_per_week']:4.1f}/wk, {b['wins']:3d}W/{b['losses']:3d}L WR={_fmt(b['wr'])}%) | "
                f"net=${b['net']:+7.2f} PF={_fmt(b['pf'])} DD=${b['max_dd']:6.2f} P/DD={_fmt(b['p_dd'])} | "
                f"H1/H2=({r['h1_net']:+.1f}/{r['h2_net']:+.1f}) exclB=${b['excl_best']:+.1f} st=${r['stress_net']:+.1f} MC95=${r['mc_p95_dd']:.1f}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Fast Waterfall/Cascade Momentum Lab (M1 & M5).")
    parser.add_argument("--symbol", default="XAUUSD_i")
    parser.add_argument("--m1-days", type=int, default=45, help="M1 days (0 to skip; default 45)")
    parser.add_argument("--m5-days", type=int, default=540, help="M5 days (0 to skip; default 540)")
    parser.add_argument("--mc-trials", type=int, default=400)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    t0 = time.monotonic()

    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = Path(args.out) if args.out else (ROOT / "logs" / f"waterfall_lab_{ts_tag}.json")
    if out_path.exists():
        stem, suf = out_path.stem, out_path.suffix or ".json"
        out_path = out_path.with_name(f"{stem}_{ts_tag}{suf}")

    tf_results: list[dict[str, Any]] = []
    live_spread = 0.32
    mode = "mock_offline" if args.mock else "live_mt5_readonly"

    try:
        if args.mock:
            spec = {
                "point": 0.01, "trade_tick_size": 0.01, "trade_tick_value": 0.10,
                "trade_contract_size": 100.0, "currency_base": "XAU", "currency_profit": "USD",
            }
            m1_bars = make_synthetic_waterfall_bars(n_bars=900, bar_seconds=60)
            tf_results.append(evaluate_waterfall_suite(
                tf_label="M1", bars=m1_bars, spec=spec, spread_price=live_spread, mc_trials=args.mc_trials,
            ))
        else:
            load_dotenv(ROOT / ".env")
            cfg = Config.from_env()
            if cfg.backend != "native":
                raise Blocked("native_backend_required")
            with loopback_native():
                feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, args.symbol)
                feed.connect()
                clock = feed._call("get_time_information", {})
                raw_time = find(clock, "trade_server_time") or find(clock, "trade_server_last_known_time")
                dt = timestamp(raw_time)
                if dt is None:
                    raise Blocked("timestamp_unverified")
                now_label = dt.replace(tzinfo=None)

                row = exact_symbol(
                    feed._call("get_marketwatch_symbols", {"symbol": args.symbol, "limit": 15, "include_hidden": True}),
                    args.symbol,
                )
                spec = enrich_spec(reported_spec(row), row)
                quote = parse_quote(row)
                live_spread = round(max(quote["ask"] - quote["bid"], spec["point"]), 6)

                if args.m1_days > 0:
                    m1_bars, _ = _paginate_period_retry(
                        feed, period="M1", now_label=now_label,
                        total_days=args.m1_days, chunk_days=1, stop_after_consecutive_empty=4,
                    )
                    tf_results.append(evaluate_waterfall_suite(
                        tf_label="M1", bars=m1_bars, spec=spec, spread_price=live_spread, mc_trials=args.mc_trials,
                    ))
                if args.m5_days > 0:
                    m5_bars, _ = _paginate_period_retry(
                        feed, period="M5", now_label=now_label,
                        total_days=args.m5_days, chunk_days=4, stop_after_consecutive_empty=4,
                    )
                    tf_results.append(evaluate_waterfall_suite(
                        tf_label="M5", bars=m5_bars, spec=spec, spread_price=live_spread, mc_trials=args.mc_trials,
                    ))

        report: dict[str, Any] = {
            "ok": True,
            "orders_submitted": 0,
            "mode": mode,
            "symbol": args.symbol,
            "live_spread": live_spread,
            "elapsed_sec": round(time.monotonic() - t0, 2),
            "out_file": str(out_path),
            "timeframes": tf_results,
        }
        _save_private_json(out_path, report)
        if args.summary:
            _print_summary(report)
        else:
            print(json.dumps(report, indent=2))
        return 0
    except (Blocked, McpError, Exception) as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
