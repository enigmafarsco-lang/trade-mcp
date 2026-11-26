#!/usr/bin/env python3
"""True latest-second drop microscope + GitHub Donchian Structural Breakout A/B (ORDERS=0).

1. Stitches the latest day's tick files up to the VERY LAST recorded second (never missing post-18:29 ticks!).
2. Prints every fast move >= $1.50/10s or >= $2.50/30s since --since-hour (broker SRV time) and exact blockers.
3. Benchmarks 6 causal GitHub Donchian Structural Breakout variants (complement to ZSCORE_MR when ADX >= 25)
   across ALL recorded files, NIGHT, TODAY, and >=since-hour + combined AW_V3_24H + BEST_DONCHIAN!
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trade_mcp.all_weather_paper import AllWeatherPaperSuite  # noqa: E402
from trade_mcp.fast_snap_engines import FastSnapEngine, fast_snap_profiles  # noqa: E402
from trade_mcp.quant_github_accel_lab import DonchianBreakoutConfig, DonchianBreakoutEngine  # noqa: E402
from trade_mcp.s1_impulse_audit import combine_portfolio_waves, load_tick_stream  # noqa: E402

NIGHT_HOURS = {21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8}
DAY_HOURS = (9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20)


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def _stats(waves: list[dict[str, Any]], name: str) -> dict[str, Any]:
    s = combine_portfolio_waves(name, [waves])
    ret_dd = round(s["net_usd"] / s["max_dd_usd"], 2) if s["max_dd_usd"] > 0.01 else (99.0 if s["net_usd"] > 0 else 0.0)
    return {
        "trades": s["waves_count"],
        "wins": s["wins"],
        "losses": s["losses"],
        "wr": s["wr_pct"],
        "profit": s["net_usd"],
        "max_dd": s["max_dd_usd"],
        "ret_dd": ret_dd,
        "pf": float(s["pf"]) if s["pf"] is not None else 0.0,
        "max_lot": s["max_lot"],
    }


def _night_only(waves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        w
        for w in waves
        if float(w.get("opened_at", 0.0)) > 0
        and datetime.fromtimestamp(float(w["opened_at"]), tz=timezone.utc).hour in NIGHT_HOURS
    ]


def _stitch_latest_day(streams: list[tuple[Path, list[tuple[float, float, float, dict[str, Any]]]]]) -> tuple[list[tuple[float, float, float, dict[str, Any]]], list[str]]:
    """Stitch all tick files touching the latest broker calendar date, deduplicated by timestamp."""
    max_ts = max(t[-1][0] for _, t in streams)
    day_start = datetime.fromtimestamp(max_ts, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    by_ts: dict[float, tuple[float, float, float, dict[str, Any]]] = {}
    used: list[str] = []
    for p, ticks in streams:
        if ticks[-1][0] < day_start:
            continue
        used.append(f"{p.name}({_hm(ticks[0][0])}->{_hm(ticks[-1][0])})")
        for row in ticks:
            if row[0] >= day_start:
                by_ts[row[0]] = row
    stitched = [by_ts[k] for k in sorted(by_ts.keys())]
    return stitched, used


def _build_donchian_cfgs() -> list[DonchianBreakoutConfig]:
    return [
        # 1) Exact GitHub XAU_Quant_Reversion_Breakout.mq5 spec (M1 Donchian 30 + EMA50 + ADX>=25, 24h)
        DonchianBreakoutConfig(
            name="GH_DONCH_M1_30_24H",
            bar_sec=60,
            donch_period=30,
            ema_period=50,
            adx_min=25.0,
            intrabar_break=True,
            break_margin_price=0.25,
            min_channel_range=3.00,
            leg_lot=0.02,
            active_server_hours=None,
        ),
        # 2) Faster M1 Donchian 20 + EMA50 + ADX>=25 + 15s confirm (24h)
        DonchianBreakoutConfig(
            name="GH_DONCH_M1_20_24H",
            bar_sec=60,
            donch_period=20,
            ema_period=50,
            adx_min=25.0,
            intrabar_break=True,
            break_margin_price=0.25,
            min_channel_range=3.00,
            min_30s_confirm=0.40,
            leg_lot=0.02,
            active_server_hours=None,
        ),
        # 3) M1 Donchian 30 London/NY Session only
        DonchianBreakoutConfig(
            name="GH_DONCH_M1_30_SESS",
            bar_sec=60,
            donch_period=30,
            ema_period=50,
            adx_min=25.0,
            intrabar_break=True,
            break_margin_price=0.25,
            min_channel_range=3.50,
            leg_lot=0.02,
            active_server_hours=DAY_HOURS,
        ),
        # 4) M1 Donchian 20 + Tick Acceleration + Variable Slope Lot + Risk-Free Pyramid (24h!)
        DonchianBreakoutConfig(
            name="GH_DONCH_M1_20_PYR",
            bar_sec=60,
            donch_period=20,
            ema_period=50,
            adx_min=25.0,
            intrabar_break=True,
            break_margin_price=0.25,
            min_channel_range=3.20,
            min_30s_confirm=0.45,
            require_accel=True,
            leg_lot=0.02,
            variable_l1_lot=True,
            max_l1_lot=0.04,
            enable_pyramid=True,
            max_legs=4,
            add_step_price=2.00,
            active_server_hours=None,
        ),
        # 5) M1 Donchian 30 + Risk-Free Pyramid + Closed-Bar Confirm (fewer fakeouts!)
        DonchianBreakoutConfig(
            name="GH_DONCH_M1_30_CLOSE",
            bar_sec=60,
            donch_period=30,
            ema_period=50,
            adx_min=25.0,
            intrabar_break=False,
            break_margin_price=0.15,
            min_channel_range=3.50,
            leg_lot=0.02,
            enable_pyramid=True,
            max_legs=4,
            active_server_hours=None,
        ),
        # 6) M3 (180s) Structural Donchian 15 (45-minute structure break!) + EMA34 + ADX>=22 + Pyramid
        DonchianBreakoutConfig(
            name="GH_DONCH_M3_15_24H",
            bar_sec=180,
            donch_period=15,
            ema_period=34,
            adx_min=22.0,
            intrabar_break=True,
            break_margin_price=0.35,
            min_channel_range=4.50,
            min_30s_confirm=0.50,
            leg_lot=0.02,
            enable_pyramid=True,
            max_legs=4,
            add_step_price=2.20,
            active_server_hours=None,
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="True latest-second drop microscope + GitHub Donchian Breakout A/B (ORDERS=0)")
    parser.add_argument("--since-hour", type=int, default=18, help="Broker server hour to inspect from (default 18)")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    paths = sorted((ROOT / "logs").glob("s1_ticks_*.jsonl"))
    if not paths:
        print("No tick files in logs/s1_ticks_*.jsonl", file=sys.stderr)
        return 1

    streams = [(p, load_tick_stream(p, rebuild_m1=True)) for p in paths]
    streams = [(p, t) for p, t in streams if t]
    stitched, used_files = _stitch_latest_day(streams)

    last_dt = datetime.fromtimestamp(stitched[-1][0], tz=timezone.utc)
    cutoff_ts = last_dt.replace(hour=args.since_hour, minute=0, second=0, microsecond=0).timestamp()
    if cutoff_ts > stitched[-1][0]:
        cutoff_ts = stitched[0][0]

    print(
        f"=== PART 1: TRUE LATEST STITCHED TIMELINE ({_hm(stitched[0][0])} -> {_hm(stitched[-1][0])} SRV | "
        f"{len(stitched)} ticks | {len(used_files)} files merged) ==="
    )
    for uf in used_files:
        print(f"  file: {uf}")

    # Zoom into every move >= $1.80/10s or >= $2.50/30s or >= $3.50/60s since since_hour
    probe = FastSnapEngine(cfg=dict(fast_snap_profiles(base_lot=0.02))["VSNAP_SHOCK_6"])
    bursts: list[dict[str, Any]] = []
    last_sec = -1
    for ts, bid, ask, _ in stitched:
        probe.on_tick(ts=ts, bid=bid, ask=ask)
        if ts < cutoff_ts:
            continue
        sec = int(ts)
        if sec == last_sec:
            continue
        last_sec = sec
        mid = 0.5 * (bid + ask)
        h5, m5, _, _, _ = probe._window_stats(sec, mid, 5)
        h10, m10, e10, _, _ = probe._window_stats(sec, mid, 10)
        h30, m30, e30, _, _ = probe._window_stats(sec, mid, 30)
        h60, m60, e60, _, _ = probe._window_stats(sec, mid, 60)
        h300, m300, _, _, _ = probe._window_stats(sec, mid, 300)
        if max(abs(m10), abs(m30) * 0.65, abs(m60) * 0.5) < 1.50:
            continue
        row = {
            "ts": ts,
            "hm": _hm(ts),
            "mid": round(mid, 2),
            "sp": round(ask - bid, 2),
            "m5": round(m5, 2) if h5 else 0.0,
            "m10": round(m10, 2) if h10 else 0.0,
            "e10": round(e10, 2) if h10 else 0.0,
            "m30": round(m30, 2) if h30 else 0.0,
            "e30": round(e30, 2) if h30 else 0.0,
            "m60": round(m60, 2) if h60 else 0.0,
            "e60": round(e60, 2) if h60 else 0.0,
            "m5m": round(m300, 2) if h300 else 0.0,
        }
        sc = abs(row["m10"]) * 1.5 + abs(row["m30"]) + abs(row["m60"]) * 0.5
        if bursts and (ts - bursts[-1]["ts"]) <= 35.0:
            prev_sc = abs(bursts[-1]["m10"]) * 1.5 + abs(bursts[-1]["m30"]) + abs(bursts[-1]["m60"]) * 0.5
            if sc > prev_sc:
                bursts[-1] = row
        else:
            bursts.append(row)

    print(f"\n  Biggest moves since {args.since_hour:02d}:00 SRV (up to {_hm(stitched[-1][0])} SRV):")
    for b in bursts[-20:]:
        print(
            f"    {b['hm']} @ {b['mid']:7.2f} (sp={b['sp']:.2f}) | "
            f"m5={b['m5']:+6.2f} m10={b['m10']:+6.2f}(e={b['e10']:.2f}) "
            f"m30={b['m30']:+6.2f}(e={b['e30']:.2f}) m60={b['m60']:+6.2f}(e={b['e60']:.2f}) m5m={b['m5m']:+6.2f}"
        )

    # Part 2: Benchmark GitHub Donchian Breakout on ALL files + Stitched Today + >=since_hour!
    cfgs = _build_donchian_cfgs()
    merged: dict[str, list[dict[str, Any]]] = {"AW_V3_24H": []}
    for c in cfgs:
        merged[c.name] = []

    total_ticks = 0
    for _, ticks in streams:
        total_ticks += len(ticks)
        aw = AllWeatherPaperSuite(initial_capital_usd=1000.0)
        d_engs = {c.name: DonchianBreakoutEngine(cfg=c) for c in cfgs}
        for ts, bid, ask, m1_ctx in ticks:
            aw.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
            for eng in d_engs.values():
                eng.on_tick(ts=ts, bid=bid, ask=ask)
        lts, lbid, lask, _ = ticks[-1]
        aw.close_all_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
        for eng in d_engs.values():
            eng.force_close_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
        merged["AW_V3_24H"].extend(aw.trades_closed)
        for name, eng in d_engs.items():
            merged[name].extend(eng.waves_closed)

    # Also run on the stitched today timeline (so continuous post-18:00 trades are tested cleanly!)
    aw_t = AllWeatherPaperSuite(initial_capital_usd=1000.0)
    d_t = {c.name: DonchianBreakoutEngine(cfg=c) for c in cfgs}
    for ts, bid, ask, m1_ctx in stitched:
        aw_t.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        for eng in d_t.values():
            eng.on_tick(ts=ts, bid=bid, ask=ask)
    lts, lbid, lask, _ = stitched[-1]
    aw_t.close_all_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
    for eng in d_t.values():
        eng.force_close_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")

    today_w: dict[str, list[dict[str, Any]]] = {"AW_V3_24H": list(aw_t.trades_closed)}
    for name, eng in d_t.items():
        today_w[name] = list(eng.waves_closed)

    print(f"\n=== PART 2: GITHUB DONCHIAN STRUCTURAL BREAKOUT A/B ({len(streams)} FILES, {total_ticks} TICKS, ORDERS=0) ===")
    all_st: dict[str, dict[str, Any]] = {}
    for k in ["AW_V3_24H"] + [c.name for c in cfgs]:
        sa = _stats(merged[k], k)
        sn = _stats(_night_only(merged[k]), k)
        st = _stats(today_w[k], k)
        ss = _stats([w for w in today_w[k] if float(w.get("opened_at", 0.0)) >= cutoff_ts], k)
        all_st[k] = sa
        print(
            f"  {k:22s} | ALL: ${sa['profit']:+7.2f} DD:-${sa['max_dd']:5.2f} (R/DD:{sa['ret_dd']:4.2f}x {sa['wins']:2d}W/{sa['losses']:2d}L PF:{sa['pf']:4.2f}) | "
            f"Night:${sn['profit']:+6.2f} | Today:${st['profit']:+6.2f}({st['trades']:2d}) | >={args.since_hour:02d}h:${ss['profit']:+6.2f}({ss['trades']}t)"
        )

    best_d = max([c.name for c in cfgs], key=lambda k: all_st[k]["profit"])
    if all_st[best_d]["profit"] > 0:
        combo_all = merged["AW_V3_24H"] + merged[best_d]
        combo_tod = today_w["AW_V3_24H"] + today_w[best_d]
        ca = _stats(combo_all, "COMBO")
        cn = _stats(_night_only(combo_all), "COMBO")
        ct = _stats(combo_tod, "COMBO")
        cs = _stats([w for w in combo_tod if float(w.get("opened_at", 0.0)) >= cutoff_ts], "COMBO")
        print(
            f"\n  AW_V3+{best_d:16s} | ALL: ${ca['profit']:+7.2f} DD:-${ca['max_dd']:5.2f} (R/DD:{ca['ret_dd']:4.2f}x {ca['wins']:2d}W/{ca['losses']:2d}L PF:{ca['pf']:4.2f}) | "
            f"Night:${cn['profit']:+6.2f} | Today:${ct['profit']:+6.2f}({ct['trades']:2d}) | >={args.since_hour:02d}h:${cs['profit']:+6.2f}({cs['trades']}t)"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
