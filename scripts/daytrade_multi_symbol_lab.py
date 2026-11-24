#!/usr/bin/env python3
"""Read-only Multi-Symbol Cross-Asset Generalization & Diversified Portfolio Lab.

Auto-discovers liquid symbols across Metals, Forex, JPY Crosses, Indices, Oil, and Crypto
directly from the user's live Alpari MT5 Market Watch, and tests our proven Gold champions
using symbol-adaptive economics (zero Gold-hardcoded price thresholds):

  1. H1_DONCHIAN50_LONDON  (08-16 UTC, adaptive min-ATR, $25 max stop cap at 0.01 lot)
  2. H1_DONCHIAN50_LDN_NY  (07-20 UTC, for US Indices / Oil / JPY / NY movers)
  3. M5_MOMENTUM_ADAPTIVE  (M5_5 + London/NY + H1 ADX 20..35, adaptive ATR stop <= $12)

Also builds a Diversified Multi-Symbol Portfolio on $1,000 and $4,000 with dynamic 0.01..0.10
lot sizing across all OOS-qualified symbols.

Strictly read-only: NEVER imports execution modules, NEVER modifies .env, submits 0 orders.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
os.environ["TRADE_MCP_PAPER_TRADING"] = "false"

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

from trade_mcp.bridge.native import find, records  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.daytrade_paper import HARD_MAX_LOT  # noqa: E402
from trade_mcp.m1_momentum import resample_bars_to_tf  # noqa: E402
from trade_mcp.mcpclient import McpError  # noqa: E402
from trade_mcp.multi_symbol_lab import (  # noqa: E402
    build_multisymbol_portfolio_report,
    evaluate_symbol_bundle,
    select_candidate_symbols,
)


def _discover_marketwatch_symbols(feed: ReadOnlyNativeFeed, *, max_symbols: int) -> tuple[list[str], int]:
    """Query MT5 Market Watch (including hidden symbols) and auto-pick diverse liquid symbols."""
    raw = feed._call("get_marketwatch_symbols", {"limit": 300, "include_hidden": True})
    rows = records(raw, "symbols", "data", "items", "rows")
    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        nm = find(row, "name", "symbol")
        if isinstance(nm, str) and nm.strip():
            names.append(nm.strip())
    picked = select_candidate_symbols(names, max_symbols=max_symbols)
    return picked, len(names)


def _inspect_symbol_live(feed: ReadOnlyNativeFeed, sym: str) -> tuple[dict[str, Any], dict[str, float]]:
    """Read live spec & quote; if symbol is hidden in MT5 Market Watch, subscribe it cleanly first."""
    def _read_once() -> tuple[dict[str, Any], dict[str, float]]:
        row = exact_symbol(
            feed._call("get_marketwatch_symbols", {"symbol": sym, "limit": 15, "include_hidden": True}),
            sym,
        )
        spec = enrich_spec(reported_spec(row), row)
        c_base = find(row, "currency_base", "base_currency")
        if c_base is not None:
            spec["currency_base"] = c_base
        return spec, parse_quote(row)

    try:
        return _read_once()
    except Blocked as exc:
        if exc.reason not in ("missing_broker_spec", "invalid_data"):
            raise
        # Symbol is hidden / not yet subscribed in MT5 Market Watch -> add to Market Watch (non-trading)
        try:
            feed.client.call_tool("add_marketwatch_symbol", {"symbol": sym})
        except McpError:
            raise exc from None
        last_err: Blocked = exc
        for delay in (0.6, 1.0, 1.4):
            time.sleep(delay)
            try:
                return _read_once()
            except Blocked as e2:
                last_err = e2
        raise last_err from None


def _mock_multisymbol_inputs() -> list[dict[str, Any]]:
    """Synthetic multi-asset dataset (Gold, EURUSD, USDJPY) for fast offline smoke testing."""
    start = int(datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp())
    assets = [
        ("XAUUSD_i", 3000.0, 1.8, 0.32, {"symbol": "XAUUSD_i", "point": 0.01, "digits": 2, "currency_base": "XAU", "currency_profit": "USD", "trade_contract_size": 100.0, "trade_tick_size": 0.01, "trade_tick_value": 0.10, "trade_calc_mode": 4, "volume_min": 0.01, "volume_max": 50.0, "volume_step": 0.01}),
        ("EURUSD_i", 1.0800, 0.00055, 0.00012, {"symbol": "EURUSD_i", "point": 0.00001, "digits": 5, "currency_base": "EUR", "currency_profit": "USD", "trade_contract_size": 100000.0, "trade_tick_size": 0.00001, "trade_tick_value": 1.0, "trade_calc_mode": 0, "volume_min": 0.01, "volume_max": 50.0, "volume_step": 0.01}),
        ("USDJPY_i", 154.00, 0.09, 0.016, {"symbol": "USDJPY_i", "point": 0.001, "digits": 3, "currency_base": "USD", "currency_profit": "JPY", "trade_contract_size": 100000.0, "trade_tick_size": 0.001, "trade_tick_value": 0.65, "trade_calc_mode": 0, "volume_min": 0.01, "volume_max": 50.0, "volume_step": 0.01}),
    ]
    out: list[dict[str, Any]] = []
    for sym, p0, amp, sp, spec in assets:
        h1: list[list[float]] = []
        px = p0
        for i in range(180 * 24):
            t = start + i * 3600
            cycle = i % 220
            drift = amp * (0.35 if cycle < 130 else -0.25)
            o = px
            c = max(p0 * 0.5, o + drift + math.sin(i / 11.0) * amp * 0.5)
            h = max(o, c) + amp * 0.9
            low = min(o, c) - amp * 0.9
            h1.append([float(t), round(o, 5), round(h, 5), round(low, 5), round(c, 5), 120.0])
            px = c
        out.append({"symbol": sym, "spec": spec, "spread": sp, "h1": h1, "m5": None, "m15": None})
    return out


def _format_summary(report: dict[str, Any]) -> str:
    lab = report["lab"]
    lines = [
        "=== MULTI-SYMBOL CROSS-ASSET GENERALIZATION & PORTFOLIO LAB ===",
        f"OK={report['ok']} ORDERS={report['orders_submitted']} MODE={report['mode']} "
        f"TESTED={lab['symbols_tested_count']} QUALIFIED_H1_LDN={lab['qualified_h1_london_symbols']} "
        f"ELAPSED={report['elapsed_seconds']}s",
        "",
        "=== LEADERBOARD BY SYMBOL (0.01 lot | exact broker contract_size/tick_value | real live spread) ===",
    ]
    for r in lab["symbols"]:
        sm = r["spec_meta"]
        ldn = r["engines"]["H1_DONCHIAN50_LONDON"]
        lny = r["engines"]["H1_DONCHIAN50_LDN_NY"]
        m5e = r["engines"].get("M5_MOMENTUM_ADAPTIVE")
        tag = "QUAL" if ldn["qualifies_oos"] else "    "
        s1 = ldn["summary"]
        s2 = lny["summary"]
        h1_half = f"H1=${ldn['half1']['net_pnl_usd']:+.1f} H2=${ldn['half2']['net_pnl_usd']:+.1f}" if (ldn["half1"] and ldn["half2"]) else ""
        lines.append(
            f"[{tag}] {r['symbol']:10s} ({sm['pricing_basis']:13s} mult={sm['multiplier']:g} sp={sm['live_spread_price']} "
            f"sp/ATR={sm['spread_pct_of_h1_atr']:4.1f}% marg=${sm['est_margin_001_usd']:5.2f} H1={sm['h1_span_days']}d)\n"
            f"       H1_LDN(08-16): {s1['trades']:3d}t ({s1['wins']}W/{s1['losses']}L) "
            f"net=${s1['net_pnl_usd']:+8.2f} PF={str(s1['pf']):>5s} DD=${s1['max_dd_usd']:6.2f} "
            f"P/DD={str(s1['profit_to_dd_ratio']):>5s} exclBest=${s1['net_without_best_trade_usd']:+7.2f} "
            f"stress=${ldn['stress_1_5x_comm5']['net_pnl_usd']:+7.2f} {h1_half}\n"
            f"       H1_LNY(07-20): {s2['trades']:3d}t ({s2['wins']}W/{s2['losses']}L) "
            f"net=${s2['net_pnl_usd']:+8.2f} PF={str(s2['pf']):>5s} DD=${s2['max_dd_usd']:6.2f} "
            f"P/DD={str(s2['profit_to_dd_ratio']):>5s} stress=${lny['stress_1_5x_comm5']['net_pnl_usd']:+7.2f}"
        )
        if m5e is not None:
            s3 = m5e["summary"]
            lines.append(
                f"       M5_ADAPT(${m5e['adaptive_stop_usd']}): {s3['trades']:3d}t ({s3['wins']}W/{s3['losses']}L) "
                f"net=${s3['net_pnl_usd']:+8.2f} PF={str(s3['pf']):>5s} DD=${s3['max_dd_usd']:6.2f} "
                f"P/DD={str(s3['profit_to_dd_ratio']):>5s} stress=${m5e['stress_1_5x_comm5']['net_pnl_usd']:+7.2f}"
            )

    port = lab["multisymbol_h1_portfolio"]
    lines.append("")
    lines.append(
        f"=== MULTI-SYMBOL DIVERSIFIED H1 PORTFOLIO ({port['included_symbols']} | {port['total_trades']} trades) ==="
    )
    if not port["by_capital"]:
        lines.append("  No symbols met strict OOS qualification criteria.")
    for cap_k, cblk in port["by_capital"].items():
        f0 = cblk["fixed_001"]
        d15 = cblk["dyn_1_5pct_ddgov"]
        mc15 = cblk["mc_dyn_1_5pct_ddgov"]
        lines.append(
            f"  [{cap_k.upper():8s}] FIXED_0.01: End=${f0['final_equity_usd']:9,.2f} ({f0['return_pct']:+6.1f}%) "
            f"MaxDD={f0['max_dd_pct']:4.1f}% PF={f0['pf']} OpenMax={f0['concurrency']['max_simultaneous_open']}\n"
            f"             DYN_1.5%+DD: End=${d15['final_equity_usd']:9,.2f} ({d15['return_pct']:+6.1f}%) "
            f"MaxDD={d15['max_dd_pct']:4.1f}% PF={d15['pf']} Lot[{d15['lot_stats']['min_lot']:.2f}/{d15['lot_stats']['avg_lot']:.2f}/{d15['lot_stats']['max_lot']:.2f}] "
            f"| MC(Med=${mc15['median_final_equity_usd']:,.0f} P5=${mc15['p05_final_equity_usd']:,.0f} "
            f"P95DD={mc15['p95_max_dd_pct']:4.1f}% Blown={mc15['prob_blown_pct']:4.1f}%)"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    t0 = time.monotonic()
    p = argparse.ArgumentParser(description="Read-only multi-symbol cross-asset generalization & portfolio lab.")
    p.add_argument("--symbols", default=None, help="Comma-separated symbols (default: auto-discover from MT5 Market Watch)")
    p.add_argument("--max-symbols", type=int, default=12, help="Max auto-discovered symbols (default 12)")
    p.add_argument("--h1-days", type=int, default=1095, help="Target calendar days of H1 per symbol (default 1095 = 3 years)")
    p.add_argument("--m5-days", type=int, default=180, help="Target calendar days of M5 per symbol (0 = skip M5 for fast H1 scan)")
    p.add_argument("--max-lot", type=float, default=0.01)
    p.add_argument("--leverage", type=float, default=500.0)
    p.add_argument("--mc-trials", type=int, default=500)
    p.add_argument("--mock", action="store_true")
    p.add_argument("--summary", action="store_true")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)

    if args.max_lot <= 0 or args.max_lot > HARD_MAX_LOT:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"max_lot must be <= {HARD_MAX_LOT}"}))
        return 2

    out_path = Path(args.out) if args.out else (ROOT / "logs" / "multi_symbol_lab.json")

    try:
        sym_results: list[dict[str, Any]] = []
        skipped_symbols: list[dict[str, str]] = []
        marketwatch_total = 0

        if args.mock:
            mode = "mock_offline"
            for item in _mock_multisymbol_inputs():
                sym_results.append(
                    evaluate_symbol_bundle(
                        symbol=item["symbol"],
                        spec=item["spec"],
                        spread_price=item["spread"],
                        h1_bars=item["h1"],
                        m5_bars=item["m5"],
                        m15_bars=item["m15"],
                        max_lot=args.max_lot,
                        leverage=args.leverage,
                        mc_trials=args.mc_trials,
                    )
                )
        else:
            mode = "live_mt5_readonly"
            load_dotenv()
            cfg = Config.from_env()
            if cfg.backend != "native":
                raise Blocked("native_backend_required")
            with loopback_native():
                feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, "XAUUSD_i")
                try:
                    feed.connect()
                except Blocked as exc:
                    raise Blocked(f"connect:{exc.reason}") from exc

                clock = feed._call("get_time_information", {})
                raw_time = find(clock, "trade_server_time") or find(clock, "trade_server_last_known_time")
                dt = timestamp(raw_time)
                if dt is None:
                    raise Blocked("timestamp_unverified")
                now_label = dt.replace(tzinfo=None)

                if args.symbols:
                    candidates = [s.strip() for s in args.symbols.split(",") if s.strip()]
                else:
                    candidates, marketwatch_total = _discover_marketwatch_symbols(feed, max_symbols=args.max_symbols)

                print(
                    f"[MULTI-SYMBOL] discovered {len(candidates)} candidates (from {marketwatch_total} MarketWatch rows): {candidates}",
                    file=sys.stderr,
                    flush=True,
                )

                for sym in candidates:
                    feed.symbol = sym
                    try:
                        spec, quote = _inspect_symbol_live(feed, sym)
                        spread = round(max(0.0, quote["ask"] - quote["bid"]), 6)
                        h1, _ = _paginate_period_retry(
                            feed, period="H1", now_label=now_label, total_days=args.h1_days, chunk_days=30, stop_after_consecutive_empty=3,
                        )
                        if len(h1) < 120:
                            skipped_symbols.append({"symbol": sym, "reason": f"insufficient_h1({len(h1)})"})
                            continue
                        m5: list[list[float]] | None = None
                        m15: list[list[float]] | None = None
                        if args.m5_days > 0:
                            m5_raw, _ = _paginate_period_retry(
                                feed, period="M5", now_label=now_label, total_days=args.m5_days, chunk_days=4, stop_after_consecutive_empty=4,
                            )
                            if len(m5_raw) >= 300:
                                m5 = m5_raw
                                m15 = resample_bars_to_tf(m5_raw, src_seconds=300, dst_seconds=900)
                        sym_results.append(
                            evaluate_symbol_bundle(
                                symbol=sym,
                                spec=spec,
                                spread_price=spread,
                                h1_bars=h1,
                                m5_bars=m5,
                                m15_bars=m15,
                                max_lot=args.max_lot,
                                leverage=args.leverage,
                                mc_trials=args.mc_trials,
                            )
                        )
                    except (Blocked, McpError, ValueError) as exc:
                        skipped_symbols.append({"symbol": sym, "reason": str(exc)})
                        print(f"  [SKIP {sym}] {exc}", file=sys.stderr, flush=True)

        lab = build_multisymbol_portfolio_report(sym_results, capitals=(1000.0, 4000.0), mc_trials=args.mc_trials)
        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": mode,
            "executable": False,
            "execution_authorized": False,
            "orders_submitted": 0,
            "elapsed_seconds": round(time.monotonic() - t0, 2),
            "marketwatch_total": marketwatch_total,
            "skipped_symbols": skipped_symbols,
            "lab": lab,
        }
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
