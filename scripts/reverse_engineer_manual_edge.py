#!/usr/bin/env python3
"""Strictly read-only reverse-engineering of user's real MT5 manual XAUUSD_i edge vs recorded ticks (ORDERS=0).

Aligns closed MT5 manual deals (last N hours) against logs/s1_ticks_*.jsonl to measure the exact
second-by-second tick physics (5s/10s/60s velocity, 10s acceleration, 5m/15m context, 30m box
position, concurrent order stacking, lot vs slope, night vs day profit) — strictly sanitized
(zero account numbers, zero ticket IDs, zero secrets).
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_manual_deals_audit import (  # noqa: E402
    _parse_deals,
    analyze_manual_deals,
)
from daytrade_mt5_readonly_check import Blocked, loopback_native  # noqa: E402

from trade_mcp.bridge.native import find, iso, to_epoch  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.mcpclient import McpHttpClient  # noqa: E402
from trade_mcp.s1_impulse_audit import load_tick_stream  # noqa: E402

NIGHT_HOURS = {21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8}


def _load_merged_ticks(logs_dir: Path) -> tuple[list[float], list[float]]:
    """Load and deduplicate (ts, mid) sorted timeline from all logs/s1_ticks_*.jsonl."""
    pts: dict[int, float] = {}
    for p in sorted(logs_dir.glob("s1_ticks_*.jsonl")):
        for ts, bid, ask, _ in load_tick_stream(p, rebuild_m1=False):
            pts[int(ts)] = 0.5 * (bid + ask)
    if not pts:
        return [], []
    times = sorted(pts.keys())
    return [float(t) for t in times], [pts[t] for t in times]


def _price_at(times: list[float], mids: list[float], target_ts: float, max_gap: float = 15.0) -> float | None:
    if not times:
        return None
    idx = bisect.bisect_right(times, target_ts) - 1
    if idx < 0:
        return None
    if abs(target_ts - times[idx]) > max_gap:
        return None
    return mids[idx]


def _range_stats(times: list[float], mids: list[float], t_end: float, lookback: float) -> tuple[float, float] | None:
    i1 = bisect.bisect_right(times, t_end)
    i0 = bisect.bisect_left(times, t_end - lookback)
    if i1 - i0 < 20:
        return None
    sl = mids[i0:i1]
    return min(sl), max(sl)


def enrich_trades_with_tick_physics(
    trades: list[dict[str, Any]],
    times: list[float],
    mids: list[float],
) -> dict[str, Any]:
    """Attach strictly causal pre-entry tick physics & concurrent stack count (sanitized, no IDs)."""
    enriched: list[dict[str, Any]] = []
    for tr in trades:
        t0 = float(tr["opened_at"])
        side = str(tr["side"]).lower()
        sdir = 1 if side == "buy" else -1
        hr = datetime.fromtimestamp(t0, tz=timezone.utc).hour

        # Count concurrent open trades at entry time (real stacking behavior!)
        concurrent = sum(
            1 for other in trades if float(other["opened_at"]) <= t0 <= float(other["closed_at"])
        )

        p0 = _price_at(times, mids, t0, max_gap=10.0)
        phys: dict[str, Any] = {}
        if p0 is not None:
            p5 = _price_at(times, mids, t0 - 5.0, max_gap=8.0)
            p10 = _price_at(times, mids, t0 - 10.0, max_gap=8.0)
            p20 = _price_at(times, mids, t0 - 20.0, max_gap=10.0)
            p60 = _price_at(times, mids, t0 - 60.0, max_gap=15.0)
            p300 = _price_at(times, mids, t0 - 300.0, max_gap=30.0)
            p900 = _price_at(times, mids, t0 - 900.0, max_gap=60.0)
            m5 = (p0 - p5) if p5 is not None else None
            m10 = (p0 - p10) if p10 is not None else None
            m20 = (p0 - p20) if p20 is not None else None
            m60 = (p0 - p60) if p60 is not None else None
            m5m = (p0 - p300) if p300 is not None else None
            m15m = (p0 - p900) if p900 is not None else None
            accel10 = None
            if m10 is not None and m20 is not None:
                v_now = m10 / 10.0
                v_prev = (m20 - m10) / 10.0
                accel10 = round(v_now - v_prev, 3)

            rng30 = _range_stats(times, mids, t0, 1800.0)
            pos30 = None
            rng30_usd = None
            if rng30 is not None and (rng30[1] - rng30[0]) > 0.2:
                rng30_usd = round(rng30[1] - rng30[0], 2)
                pos30 = round((p0 - rng30[0]) / (rng30[1] - rng30[0]), 2)

            phys = {
                "dir_m5": round(m5 * sdir, 2) if m5 is not None else None,
                "dir_m10": round(m10 * sdir, 2) if m10 is not None else None,
                "dir_m60": round(m60 * sdir, 2) if m60 is not None else None,
                "dir_accel10": round(accel10 * sdir, 3) if accel10 is not None else None,
                "dir_m5m": round(m5m * sdir, 2) if m5m is not None else None,
                "dir_m15m": round(m15m * sdir, 2) if m15m is not None else None,
                "pos_30m": pos30,
                "rng_30m": rng30_usd,
                "with_60s_trend": (m60 * sdir > 0) if m60 is not None else None,
                "with_5m_trend": (m5m * sdir > 0) if m5m is not None else None,
            }

        enriched.append({
            "opened_hm": datetime.fromtimestamp(t0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "hour_srv": hr,
            "is_night": hr in NIGHT_HOURS,
            "side": side,
            "lot": round(float(tr["volume"]), 2),
            "entry": round(float(tr["entry_price"]), 2),
            "exit": round(float(tr["exit_price"]), 2),
            "hold_sec": int(tr["hold_seconds"]),
            "concurrent": concurrent,
            "net_usd": round(float(tr["net_usd"]), 2),
            "phys": phys,
        })

    def _agg(sub: list[dict[str, Any]]) -> dict[str, Any]:
        if not sub:
            return {"trades": 0, "net_usd": 0.0}
        wins = [x for x in sub if x["net_usd"] > 0]
        losses = [x for x in sub if x["net_usd"] <= 0]
        w60 = [x["phys"]["with_60s_trend"] for x in sub if x["phys"].get("with_60s_trend") is not None]
        w5m = [x["phys"]["with_5m_trend"] for x in sub if x["phys"].get("with_5m_trend") is not None]
        dm60 = [x["phys"]["dir_m60"] for x in sub if x["phys"].get("dir_m60") is not None]
        dm10 = [x["phys"]["dir_m10"] for x in sub if x["phys"].get("dir_m10") is not None]
        acc = [x["phys"]["dir_accel10"] for x in sub if x["phys"].get("dir_accel10") is not None]
        return {
            "trades": len(sub),
            "wins": len(wins),
            "losses": len(losses),
            "wr_pct": round(100.0 * len(wins) / len(sub), 1),
            "net_usd": round(sum(x["net_usd"] for x in sub), 2),
            "median_hold_sec": int(statistics.median(x["hold_sec"] for x in sub)),
            "median_lot": round(statistics.median(x["lot"] for x in sub), 2),
            "max_lot": round(max(x["lot"] for x in sub), 2),
            "max_concurrent": max(x["concurrent"] for x in sub),
            "avg_concurrent": round(statistics.mean(x["concurrent"] for x in sub), 2),
            "matched_ticks": len(dm60),
            "pct_with_60s_momentum": round(100.0 * sum(1 for v in w60 if v) / len(w60), 1) if w60 else None,
            "pct_with_5m_trend": round(100.0 * sum(1 for v in w5m if v) / len(w5m), 1) if w5m else None,
            "median_dir_m10": round(statistics.median(dm10), 2) if dm10 else None,
            "median_dir_m60": round(statistics.median(dm60), 2) if dm60 else None,
            "median_dir_accel10": round(statistics.median(acc), 3) if acc else None,
        }

    return {
        "schema": "manual_edge_xray_v1",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "orders_submitted": 0,
        "all": _agg(enriched),
        "night": _agg([x for x in enriched if x["is_night"]]),
        "day": _agg([x for x in enriched if not x["is_night"]]),
        "winners": _agg([x for x in enriched if x["net_usd"] > 0]),
        "losers": _agg([x for x in enriched if x["net_usd"] <= 0]),
        "trades": enriched[-150:],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reverse-engineer user's real MT5 manual edge vs tick stream (ORDERS=0)")
    parser.add_argument("--symbol", default="XAUUSD_i")
    parser.add_argument("--hours", type=int, default=36, help="Lookback hours (default 36)")
    parser.add_argument("--save", action="store_true", help="Write sanitized reports/paper_live/manual_edge_xray.json")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"
    load_dotenv(ROOT / ".env")
    cfg = Config.from_env()
    if cfg.backend != "native":
        raise Blocked(f"requires native backend, got {cfg.backend}")
    url = loopback_native(cfg.mcp_url)

    with McpHttpClient(url, timeout=max(cfg.timeout, 15.0)) as client:
        client.initialize()
        clock = client.call_tool("get_time_information", {})
        raw_t = find(clock, "trade_server_time") or find(clock, "trade_server_last_known_time")
        now_ep = to_epoch(raw_t)
        now_dt = datetime.fromtimestamp(now_ep, tz=timezone.utc)
        start_dt = now_dt - timedelta(hours=max(1, args.hours))
        raw_hist = client.call_tool(
            "get_trading_history_orders",
            {
                "datetime_from": iso(start_dt),
                "datetime_to": iso(now_dt + timedelta(minutes=5)),
                "include_deals": True,
            },
        )

    deals, _ = _parse_deals(raw_hist)
    parsed = analyze_manual_deals(deals, symbol=args.symbol)
    times, mids = _load_merged_ticks(ROOT / "logs")
    xray = enrich_trades_with_tick_physics(parsed["closed_trades"], times, mids)

    print(f"=== MANUAL EDGE X-RAY ({args.symbol}, last {args.hours}h, ORDERS=0) ===")
    for k in ("all", "night", "day", "winners", "losers"):
        s = xray[k]
        if s["trades"] == 0:
            print(f"  {k:8s}: 0 trades")
            continue
        print(
            f"  {k:8s}: {s['trades']:3d} trades ({s['wins']}W/{s['losses']}L = {s['wr_pct']}%) | "
            f"PROFIT=${s['net_usd']:+8.2f} | Hold={s['median_hold_sec']}s | "
            f"Lot={s['median_lot']}..{s['max_lot']} | Stack={s['avg_concurrent']}..{s['max_concurrent']} | "
            f"With60s={s['pct_with_60s_momentum']}% With5m={s['pct_with_5m_trend']}% | "
            f"Med60s={s['median_dir_m60']} Acc10={s['median_dir_accel10']}"
        )

    if args.save:
        out_path = ROOT / "reports" / "paper_live" / "manual_edge_xray.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(xray, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, out_path)
        print(f"SAVED={out_path.relative_to(ROOT)} ({out_path.stat().st_size} bytes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
