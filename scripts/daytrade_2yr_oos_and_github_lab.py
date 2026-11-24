#!/usr/bin/env python3
"""Read-only 2-Year (730-Day) True-Unseen-Year OOS & Extended GitHub/Academic Gold Lab.

Fetches up to 730 calendar days of M5, M15, and H1 bars independently from native MT5 MCP
(with automatic chunk retries so transient empty local HTTP replies never drop bars),
detects exact Alpari history depth for each timeframe, and benchmarks 8 distinct strategies
plus a combined low-correlation portfolio:

  1. M5_MOMENTUM_G5_ADX20_35  (Reigning safety champion — tested on unseen Year 1!)
  2. TPSL_LONDON_NATIVE       (GitHub TradingTPSL London Box — reigning $ profit champion)
  3. TPSL_LONDON_CAP12        (Small-capital $12-stop-capped London Box)
  4. H1_DONCHIAN50_LONDON     (GitHub anashussain-124/quant_lab H1 Donchian50, fixed 100oz)
  5. M15_CHANDELIER_BREAKOUT  (M15 Donchian20 + H1 EMA 20/50 + 3xATR Chandelier trail)
  6. M15_EMA20_H1_PULLBACK    (GitHub shorif2000 PR#17 H1 EMA50/200 + M15 EMA20 pullback)
  7. M5_VWAP_EMA200_TRAIL50   (Academic SSRN 6650958: Daily VWAP + EMA200 + EMA50 trail)
  8. M5_LONDON_OPEN_15BP_ORB  (Academic Holmberg/Sonnert +/-15bp London Open ORB)

Strictly read-only: NEVER imports execution modules, NEVER modifies .env, submits 0 orders.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
os.environ["TRADE_MCP_PAPER_TRADING"] = "false"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_m1_momentum_validate import _fetch_range  # noqa: E402
from daytrade_m1_paper_window import ReadOnlyNativeFeed, _save_private_json  # noqa: E402
from daytrade_mt5_readonly_check import Blocked, loopback_native, timestamp  # noqa: E402

from trade_mcp.bridge.native import find  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.daytrade_paper import (  # noqa: E402
    HARD_MAX_LOT,
    audit_symbol_spec,
    resolve_pricing_basis,
)
from trade_mcp.gold_lab_2yr import evaluate_2yr_oos_and_github_lab  # noqa: E402
from trade_mcp.m1_momentum import clean_ohlcv_bars, resample_bars_to_tf  # noqa: E402
from trade_mcp.mcpclient import McpError  # noqa: E402


def _paginate_period_retry(
    feed: ReadOnlyNativeFeed,
    *,
    period: str,
    now_label: datetime,
    total_days: int,
    chunk_days: int,
    retries: int = 2,
    stop_after_consecutive_empty: int = 4,
    min_day_before_stop: int = 30,
) -> tuple[list[list[float]], dict[str, Any]]:
    """Paginate MT5 chart history safely below 1450-bar ceiling with transient/horizon recovery."""
    collected: list[list[float]] = []
    empty_chunks = 0
    tool_error_chunks = 0
    retry_recoveries = 0
    consecutive_empty = 0
    horizon_reached = False
    first_tool_error_day: int | None = None

    offset = 0
    while offset < total_days:
        step = min(chunk_days, total_days - offset)
        chunk_end = now_label - timedelta(days=offset) + (timedelta(minutes=2) if offset == 0 else timedelta(0))
        chunk_start = now_label - timedelta(days=offset + step)
        rows: list[list[float]] = []
        had_tool_error = False
        for attempt in range(retries + 1):
            try:
                rows = _fetch_range(feed, period=period, start=chunk_start, end=chunk_end, limit=1450)
                had_tool_error = False
            except Blocked as exc:
                if exc.reason in ("empty_data", "insufficient_history", "tool_error", "invalid_data"):
                    rows = []
                    if exc.reason == "tool_error":
                        had_tool_error = True
                else:
                    raise
            except McpError:
                rows = []
                had_tool_error = True
            if rows:
                if attempt > 0:
                    retry_recoveries += 1
                break
            if attempt < retries:
                time.sleep(0.30 * (attempt + 1))

        if rows:
            collected.extend(rows)
            consecutive_empty = 0
        else:
            empty_chunks += 1
            consecutive_empty += 1
            if had_tool_error:
                tool_error_chunks += 1
                if first_tool_error_day is None:
                    first_tool_error_day = offset

        offset += step
        if offset >= min_day_before_stop and consecutive_empty >= stop_after_consecutive_empty:
            horizon_reached = True
            break

    cleaned = clean_ohlcv_bars(collected)
    # Drop final potentially incomplete bar
    if len(cleaned) > 1:
        cleaned = cleaned[:-1]
    span = round((cleaned[-1][0] - cleaned[0][0]) / 86400.0, 2) if len(cleaned) >= 2 else 0.0
    print(
        f"[{period}] bars={len(cleaned)} span={span}d empty={empty_chunks} "
        f"tool_errors={tool_error_chunks} retries={retry_recoveries} horizon={horizon_reached}",
        file=sys.stderr,
        flush=True,
    )
    return cleaned, {
        "bars": len(cleaned),
        "span_days": span,
        "empty_chunks_skipped": empty_chunks,
        "tool_error_chunks": tool_error_chunks,
        "first_tool_error_day": first_tool_error_day,
        "retry_recoveries": retry_recoveries,
        "retention_horizon_reached": horizon_reached,
    }


def _fetch_2yr_history_mt5(
    feed: ReadOnlyNativeFeed,
    *,
    days: int,
) -> tuple[
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    dict[str, Any],
]:
    """Fetch up to `days` (default 730d) of M5, M15, and H1 independently from local MT5."""
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

    # Fetch M5, M15, and H1 independently up to `days` (so H1/M15 still get 2yr even if M5 stops earlier)
    m5, m5_meta = _paginate_period_retry(
        feed, period="M5", now_label=now_label, total_days=days, chunk_days=4, stop_after_consecutive_empty=4,
    )
    if len(m5) < 200:
        raise Blocked("insufficient_history_m5")

    m15, m15_meta = _paginate_period_retry(
        feed, period="M15", now_label=now_label, total_days=days + 15, chunk_days=12, stop_after_consecutive_empty=4,
    )
    h1, h1_meta = _paginate_period_retry(
        feed, period="H1", now_label=now_label, total_days=days + 60, chunk_days=30, stop_after_consecutive_empty=3,
    )
    h4 = resample_bars_to_tf(h1, src_seconds=3600, dst_seconds=14400)
    return m5, m15, h1, h4, {
        "mode": "live_mt5_readonly",
        "m5": m5_meta,
        "m15": m15_meta,
        "h1": h1_meta,
        "h4_resampled_bars": len(h4),
    }


def _mock_2yr_data() -> tuple[
    dict[str, Any],
    dict[str, float],
    list[list[float]],
    list[list[float]],
    list[list[float]],
    list[list[float]],
]:
    """Deterministic synthetic 120-day M5/M15/H1/H4 dataset for fast offline smoke testing."""
    spec: dict[str, Any] = {
        "symbol": "XAUUSD_i",
        "point": 0.01,
        "digits": 2,
        "currency_profit": "USD",
        "trade_contract_size": 100.0,
        "trade_tick_size": 0.01,
        "trade_tick_value": 0.10,
        "trade_calc_mode": 4,
        "volume_min": 0.01,
        "volume_max": 50.0,
        "volume_step": 0.01,
    }
    quote = {"bid": 3600.00, "ask": 3600.34}
    start = int(datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp())
    m5: list[list[float]] = []
    px = 2680.0
    for i in range(120 * 288):
        t = start + i * 300
        hr = (t // 3600) % 24
        wave = math.sin(i / 180.0) * 1.0 + math.sin(i / 41.0) * 0.4
        drift = 0.055 if (i % 700) < 420 else -0.038
        if 7 <= hr < 19:
            drift *= 1.4
        o = px
        c = max(1800.0, o + drift + wave * 0.18)
        span = 1.0 + (0.65 if 7 <= hr < 19 else 0.25)
        h = max(o, c) + span * 0.55
        low = min(o, c) - span * 0.55
        m5.append([float(t), round(o, 2), round(h, 2), round(low, 2), round(c, 2), float(45 + (i % 20))])
        px = c
    m15 = resample_bars_to_tf(m5, src_seconds=300, dst_seconds=900)
    h1 = resample_bars_to_tf(m15, src_seconds=900, dst_seconds=3600)
    h4 = resample_bars_to_tf(h1, src_seconds=3600, dst_seconds=14400)
    return spec, quote, m5, m15, h1, h4


def _format_summary(report: dict[str, Any]) -> str:
    fm = report["fetch_meta"]
    lab = report["lab"]
    ret = lab["retention"]
    v = lab["verdict"]
    q = report["live_quote"]
    lines = [
        f"=== 2-Year (730d) Unseen-Year OOS & Extended GitHub Gold Lab ({report['symbol']}) ===",
        f"OK={report['ok']} ORDERS={report['orders_submitted']} MODE={fm.get('mode')} "
        f"BASIS={report['parameters']['pricing_basis']} SPREAD=${q['baseline_spread_price']} "
        f"ELAPSED={report['elapsed_seconds']}s",
    ]
    for tf_k in ("m5", "m15", "h1"):
        r_tf = ret[tf_k]
        tf_m = fm.get(tf_k, {})
        lines.append(
            f"  {tf_k.upper():3s}: bars={tf_m.get('bars')} span={tf_m.get('span_days')}d "
            f"retries={tf_m.get('retry_recoveries', 0)} empty={tf_m.get('empty_chunks_skipped', 0)} "
            f"| unseen_Y1={r_tf['has_unseen_y1']} (Y1={r_tf['y1_span_days']}d {r_tf['y1_range_utc']} | "
            f"Y2={r_tf['y2_span_days']}d {r_tf['y2_range_utc']})"
        )
    lines.append(f"VERDICT: {v['status']} | passed={v['passed_ids']} | best_overall={v['best_overall_id']}")
    lines.append("")

    for r in lab["engines"]:
        f = r["full_period"]
        y1 = r["unseen_year1_2024_2025"]
        y2 = r["year2_2025_2026"]
        mc = r["monte_carlo"]
        st = r["full_cost_stress"]
        total_g = len(r["gates"])
        tag = "PASS" if r["all_gates_passed"] else f"{r['gates_passed_count']}/{total_g}"
        y1_str = (
            f"Y1(UNSEEN) {y1['trades']}t ({y1['wins']}W/{y1['losses']}L) "
            f"net=${y1['net_pnl_usd']:+.2f} PF={y1['pf']} DD=${y1['max_dd_usd']:.2f} "
            f"exclBest=${y1['net_without_best_trade_usd']:+.2f}"
            if y1 is not None
            else "Y1(UNSEEN) N/A (broker retention < 1yr on this TF)"
        )
        lines.append(
            f"[{tag:4s}] {r['id']} ({r['tf']}, {r['family']})\n"
            f"       FULL {f['trades']}t ({f['wins']}W/{f['losses']}L, WR {f['win_rate_pct']}%) "
            f"net=${f['net_pnl_usd']:+.2f} PF={f['pf']} DD=${f['max_dd_usd']:.2f} P/DD={f['profit_to_dd_ratio']} "
            f"exclBest=${f['net_without_best_trade_usd']:+.2f} months+={r['active_months_positive']}\n"
            f"       {y1_str}\n"
            f"       Y2   {y2['trades']}t ({y2['wins']}W/{y2['losses']}L) "
            f"net=${y2['net_pnl_usd']:+.2f} PF={y2['pf']} DD=${y2['max_dd_usd']:.2f} "
            f"exclBest=${y2['net_without_best_trade_usd']:+.2f}\n"
            f"       MC(medDD=${mc['median_max_dd_usd']:.2f} p95DD=${mc['p95_max_dd_usd']:.2f} "
            f"P>100={mc['prob_dd_gt_100_pct']}%) | "
            f"Stress $0.50=${st['stress_spread_050']['net_pnl_usd']:+.2f} "
            f"$0.50+$5=${st['stress_spread_050_comm5']['net_pnl_usd']:+.2f} "
            f"$0.70+$7=${st['stress_spread_070_comm7']['net_pnl_usd']:+.2f}\n"
            f"       Failed: {r['failed_gates']}"
        )

    p = lab["portfolio_combo"]
    ps = p["summary"]
    pmc = p["monte_carlo"]
    lines.append("")
    lines.append(
        f"=== COMBINED PORTFOLIO ({' + '.join(p['components'])}) ===\n"
        f"  {ps['trades']}t ({ps['wins']}W/{ps['losses']}L, WR {ps['win_rate_pct']}%) "
        f"net=${ps['net_pnl_usd']:+.2f} PF={ps['pf']} DD=${ps['max_dd_usd']:.2f} P/DD={ps['profit_to_dd_ratio']} "
        f"| MC medDD=${pmc['median_max_dd_usd']:.2f} p95DD=${pmc['p95_max_dd_usd']:.2f} P>100={pmc['prob_dd_gt_100_pct']}%"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    t0 = time.monotonic()
    p = argparse.ArgumentParser(description="Read-only 2-year unseen-year OOS & extended GitHub gold strategy lab.")
    p.add_argument("--symbol", default="XAUUSD_i")
    p.add_argument("--days", type=int, default=730, help="Target calendar days of history (default 730 = 2 years)")
    p.add_argument("--year2-days", type=int, default=365, help="Recent Year 2 window; older bars = strictly unseen Year 1")
    p.add_argument("--max-lot", type=float, default=0.01)
    p.add_argument("--leverage", type=float, default=500.0)
    p.add_argument("--spread-price", type=float, default=None)
    p.add_argument("--mc-trials", type=int, default=1000)
    p.add_argument("--mock", action="store_true", help="Offline smoke run with synthetic 2-regime bars")
    p.add_argument("--summary", action="store_true", help="Print readable summary instead of full JSON")
    p.add_argument("--out", default=None, help="Output JSON path (0o600 permissions)")
    args = p.parse_args(argv)

    if args.max_lot <= 0 or args.max_lot > HARD_MAX_LOT:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"max_lot must be <= {HARD_MAX_LOT}"}))
        return 2

    out_path = Path(args.out) if args.out else (ROOT / "logs" / "gold_lab_2yr_oos.json")

    try:
        tool_calls: dict[str, int] | None = None
        if args.mock:
            spec, quote, m5, m15, h1, h4 = _mock_2yr_data()
            year2_days = 60
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
            year2_days = args.year2_days
            with loopback_native():
                feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, args.symbol)
                try:
                    spec, quote = feed.connect()
                except Blocked as exc:
                    raise Blocked(f"connect:{exc.reason}") from exc
                m5, m15, h1, h4, fetch_meta = _fetch_2yr_history_mt5(feed, days=args.days)
                tool_calls = dict(feed.tool_calls)

        spread_price = (
            float(args.spread_price)
            if args.spread_price is not None
            else round(max(0.0, quote["ask"] - quote["bid"]), 6)
        )
        spec_audit = audit_symbol_spec(spec)
        primary_basis, primary_mult = resolve_pricing_basis(spec, "auto")

        lab = evaluate_2yr_oos_and_github_lab(
            m5_bars=m5,
            m15_bars=m15,
            h1_bars=h1,
            h4_bars=h4,
            spec=spec,
            symbol=args.symbol,
            spread_price=spread_price,
            max_lot=args.max_lot,
            leverage=args.leverage,
            year2_days=year2_days,
            mc_trials=args.mc_trials,
        )

        report: dict[str, Any] = {
            "ok": True,
            "paper": True,
            "mode": "gold_lab_2yr_unseen_oos_and_github",
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
                "days_requested": args.days,
                "year2_days": year2_days,
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
