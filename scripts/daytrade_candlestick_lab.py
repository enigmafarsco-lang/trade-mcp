#!/usr/bin/env python3
"""Strictly read-only Japanese Candlestick & Walk-Forward 1-2 Candle Discovery Lab.

Evaluates on live Alpari MT5 history (ZERO orders ever submitted):
  - All 18 classic named Japanese candlestick patterns (12 Reversal + 6 Continuation)
    across M5, M15, H1 (5yr), H4 (5yr resampled), and D1 (5yr resampled).
  - Strict Train (first 50%) -> Blind OOS Test (second 50%) pattern miner to test
    whether Alpari truly has a repeatable 1- or 2-candle directional edge after spread.
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
)
from daytrade_mt5_readonly_check import quote as parse_quote  # noqa: E402

from trade_mcp.bridge.native import McpError, find  # noqa: E402
from trade_mcp.candlestick_lab import (  # noqa: E402
    evaluate_timeframe_candlesticks,
    make_synthetic_bars_with_embedded_edge,
    resample_ohlcv,
)
from trade_mcp.config import Config, load_dotenv  # noqa: E402


def _fmt_pf(v: float | None) -> str:
    return " None" if v is None else f"{v:5.2f}"


def _print_summary(report: dict[str, Any]) -> None:
    print("=== JAPANESE CANDLESTICK & 1-2 BAR OOS DISCOVERY LAB ===")
    print(
        f"OK={report['ok']} ORDERS={report['orders_submitted']} SYMBOL={report['symbol']} "
        f"MODE={report['mode']} SPREAD={report['live_spread']} ELAPSED={report['elapsed_sec']}s"
    )

    for tf_rep in report.get("timeframes", []):
        tf = tf_rep["timeframe"]
        if "error" in tf_rep:
            print(f"\n[{tf}] SKIPPED: {tf_rep['error']} (bars={tf_rep.get('bars')})")
            continue
        print(
            f"\n==================== {tf} ({tf_rep['bars']} bars | {tf_rep['span_days']}d | "
            f"ATR={tf_rep['median_atr']} | sp/ATR={tf_rep['spread_pct_of_atr']}% | "
            f"base_green={tf_rep['baseline_green_bar_pct']}%) ===================="
        )
        print("  --- A) TOP & BOTTOM CLASSIC NAMED PATTERNS (Next 1-Bar / 2-Bar | Real Spread) ---")
        rows = tf_rep["classic_patterns"]
        valid = [r for r in rows if r["pred"]["n_total"] >= 15]
        for r in valid:
            p = r["pred"]
            h1 = p["h1_all"]
            h2 = p["h2_all"]
            t1 = r.get("trades_1bar") or {}
            sp1 = r.get("trades_1bar_split") or {}
            st1 = r.get("trades_1bar_stress") or {}
            t2 = r.get("trades_2bar") or {}
            sp2 = r.get("trades_2bar_split") or {}
            st2 = r.get("trades_2bar_stress") or {}

            def _is_pass(tr: dict[str, Any], sp: dict[str, Any], st: dict[str, Any]) -> bool:
                return (
                    tr.get("net", 0) > 0
                    and (tr.get("pf") or 0) >= 1.15
                    and sp.get("h1_net", -1) > 0
                    and sp.get("h2_net", -1) > 0
                    and st.get("net", -1) > 0
                )

            p1 = _is_pass(t1, sp1, st1)
            p2 = _is_pass(t2, sp2, st2)
            use_1b = (p1 and not p2) or ((p1 == p2) and t1.get("net", -1e9) > t2.get("net", -1e9))
            best_tag = "1B" if use_1b else "2B"
            bt, bsp, bst = (t1, sp1, st1) if use_1b else (t2, sp2, st2)
            mark = "PASS" if (p1 or p2) else "    "
            kind_tag = "REV " if r["kind"] == "reversal" else "CONT"
            print(
                f"  [{mark}] {kind_tag} {r['pattern']:25s} n={p['n_total']:4d} | "
                f"1B:{h1['hit_dir_pct']:5.1f}% net={h1['avg_net']:+7.3f} (t={h1['t_net']:+5.2f}) | "
                f"2B:{h2['hit_dir_pct']:5.1f}% net={h2['avg_net']:+7.3f} (t={h2['t_net']:+5.2f}) | "
                f"{best_tag}_Tr=${bt.get('net', 0):+7.2f} PF={_fmt_pf(bt.get('pf'))} "
                f"H1/H2=({bsp.get('h1_net', 0):+.1f}/{bsp.get('h2_net', 0):+.1f}) st=${bst.get('net', 0):+.1f}"
            )

        m = tf_rep["miner"]
        print(
            f"  --- B) WALK-FORWARD PATTERN MINER (Scanned={m['candidates_tested']} | "
            f"PassedTrain={m['train_passed']} | OOS_VERIFIED={m['oos_verified_count']}) ---"
        )
        if m["oos_verified"]:
            for rec in m["oos_verified"][:6]:
                tr = rec["train"]
                te = rec["test_oos"]
                ta = rec["trades_all"]
                tt = rec["trades_train"]
                to = rec["trades_test_oos"]
                ts = rec["trades_stress"]
                print(
                    f"  [OOS!] {rec['pattern']:38s} +{rec['horizon']}B | "
                    f"Train:{tr['hit_dir_pct']:5.1f}% (n={tr['n']:3d},t={tr['t_net']:+.2f}) -> "
                    f"OOS:{te['hit_dir_pct']:5.1f}% (n={te['n']:3d},t={te['t_net']:+.2f}) | "
                    f"Net=${ta['net']:+7.2f} PF={_fmt_pf(ta['pf'])} DD=${ta['max_dd']:6.2f} "
                    f"P/DD={_fmt_pf(ta['p_dd'])} (Tr={tt['net']:+.1f}/OOS={to['net']:+.1f}) "
                    f"exclB=${ta['excl_best']:+.1f} st=${ts['net']:+.1f} MC95=${ta.get('mc_p95_dd', 0):.1f}"
                )
        else:
            print("  No mined 1-2 candle pattern survived blind Out-of-Sample + spread stress on this TF.")
            for rec in m["top_train_preview"][:3]:
                tr = rec["train"]
                te = rec["test_oos"]
                print(
                    f"    (best train failed OOS) {rec['pattern']:36s} +{rec['horizon']}B "
                    f"Train:{tr['hit_dir_pct']:5.1f}% (t={tr['t_net']:+.2f}) -> "
                    f"OOS:{te['hit_dir_pct']:5.1f}% (t={te['t_net']:+.2f})"
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strictly read-only Candlestick & 1-2 Bar OOS Discovery Lab.")
    parser.add_argument("--symbol", default="XAUUSD_i", help="Broker symbol (default: XAUUSD_i)")
    parser.add_argument("--m1-days", type=int, default=45, help="M1 history days (0 to skip; default 45)")
    parser.add_argument("--m5-days", type=int, default=540, help="M5 history days (0 to skip)")
    parser.add_argument("--m15-days", type=int, default=750, help="M15 history days (0 to skip)")
    parser.add_argument("--h1-days", type=int, default=1825, help="H1 history days (also builds H4 & D1; default 5yr)")
    parser.add_argument("--mc-trials", type=int, default=400, help="Monte Carlo trials per OOS pattern")
    parser.add_argument("--mock", action="store_true", help="Offline deterministic mock run")
    parser.add_argument("--summary", action="store_true", help="Print readable tables")
    parser.add_argument("--out", default="", help="Output JSON path inside logs/ (auto-unique)")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    t_start = time.monotonic()

    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = Path(args.out) if args.out else (ROOT / "logs" / f"candlestick_lab_{ts_tag}.json")
    if out_path.exists():
        stem, suf = out_path.stem, out_path.suffix or ".json"
        out_path = out_path.with_name(f"{stem}_{ts_tag}{suf}")

    tf_results: list[dict[str, Any]] = []
    symbol = args.symbol
    live_spread = 0.34
    mode = "mock_offline" if args.mock else "live_mt5_readonly"

    try:
        if args.mock:
            spec = {
                "point": 0.01, "trade_tick_size": 0.01, "trade_tick_value": 0.10,
                "trade_contract_size": 100.0, "currency_base": "XAU", "currency_profit": "USD",
                "volume_min": 0.01, "volume_max": 50.0, "volume_step": 0.01, "trade_stops_level": 0.0,
            }
            h1_bars = make_synthetic_bars_with_embedded_edge(n_bars=1500, bar_seconds=3600, seed=11)
            h4_bars = resample_ohlcv(h1_bars, 14400)
            tf_results.append(evaluate_timeframe_candlesticks(
                tf_label="H1", bars=h1_bars, spec=spec, spread_px=live_spread,
                min_train_n=15, min_test_n=8, mc_trials=args.mc_trials,
            ))
            tf_results.append(evaluate_timeframe_candlesticks(
                tf_label="H4", bars=h4_bars, spec=spec, spread_px=live_spread,
                min_train_n=10, min_test_n=5, mc_trials=args.mc_trials,
            ))
        else:
            load_dotenv(ROOT / ".env")
            cfg = Config.from_env()
            url = loopback_native(cfg.mt5_mcp_url)
            feed = ReadOnlyNativeFeed(url, cfg.mt5_mcp_token, symbol, timeout=45.0)
            try:
                row = exact_symbol(
                    feed._call("get_marketwatch_symbols", {"symbol": symbol, "limit": 15, "include_hidden": True}),
                    symbol,
                )
                spec = enrich_spec(reported_spec(row), row)
                c_base = find(row, "currency_base", "base_currency")
                if c_base is not None:
                    spec["currency_base"] = c_base
                quote = parse_quote(row)
                live_spread = round(max(quote["ask"] - quote["bid"], spec["point"]), 6)

                if args.m1_days > 0:
                    m1_bars, _meta = _paginate_period_retry(feed, "M1", args.m1_days, 60)
                    tf_results.append(evaluate_timeframe_candlesticks(
                        tf_label="M1", bars=m1_bars, spec=spec, spread_px=live_spread,
                        min_train_n=40, min_test_n=20, mc_trials=args.mc_trials,
                    ))
                if args.m5_days > 0:
                    m5_bars, _meta = _paginate_period_retry(feed, "M5", args.m5_days, 300)
                    tf_results.append(evaluate_timeframe_candlesticks(
                        tf_label="M5", bars=m5_bars, spec=spec, spread_px=live_spread,
                        min_train_n=40, min_test_n=20, mc_trials=args.mc_trials,
                    ))
                if args.m15_days > 0:
                    m15_bars, _meta = _paginate_period_retry(feed, "M15", args.m15_days, 900)
                    tf_results.append(evaluate_timeframe_candlesticks(
                        tf_label="M15", bars=m15_bars, spec=spec, spread_px=live_spread,
                        min_train_n=40, min_test_n=20, mc_trials=args.mc_trials,
                    ))
                if args.h1_days > 0:
                    h1_bars, _meta = _paginate_period_retry(feed, "H1", args.h1_days, 3600)
                    tf_results.append(evaluate_timeframe_candlesticks(
                        tf_label="H1", bars=h1_bars, spec=spec, spread_px=live_spread,
                        min_train_n=40, min_test_n=20, mc_trials=args.mc_trials,
                    ))
                    h4_bars = resample_ohlcv(h1_bars, 14400)
                    tf_results.append(evaluate_timeframe_candlesticks(
                        tf_label="H4", bars=h4_bars, spec=spec, spread_px=live_spread,
                        min_train_n=25, min_test_n=12, mc_trials=args.mc_trials,
                    ))
                    d1_bars = resample_ohlcv(h1_bars, 86400)
                    tf_results.append(evaluate_timeframe_candlesticks(
                        tf_label="D1", bars=d1_bars, spec=spec, spread_px=live_spread,
                        min_train_n=15, min_test_n=8, mc_trials=args.mc_trials,
                    ))
            finally:
                feed.close()

        report: dict[str, Any] = {
            "ok": True,
            "orders_submitted": 0,
            "mode": mode,
            "symbol": symbol,
            "live_spread": live_spread,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_sec": round(time.monotonic() - t_start, 2),
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
        err = {
            "ok": False,
            "orders_submitted": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(json.dumps(err, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
