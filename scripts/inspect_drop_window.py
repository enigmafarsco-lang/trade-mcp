#!/usr/bin/env python3
"""Forensic microscope for recent fast drops/spikes (e.g. since 18:00 SRV) + 5s/10s Vertical-Cliff A/B (ORDERS=0).

1. Locates the latest recorded tick file and zooms into every fast burst since --since-hour (default 18).
2. Prints the EXACT tick physics (m5, m10, m15, m30, m60, eff10, eff30, eff60, accel5, pb30, spread)
   and the EXACT gate that blocked VSNAP6_24H, SHOCK10_DYN_RUN, MACRO_WAVE_DYN, and ZSCORE_MR_24H.
3. Simultaneously benchmarks 5 strictly causal 5s/10s Vertical-Cliff Acceleration variants across
   ALL recorded files, NIGHT, TODAY, and the SINCE-HOUR window so we only ship what beats AW_V3_24H!
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trade_mcp.all_weather_paper import AllWeatherPaperSuite  # noqa: E402
from trade_mcp.fast_snap_engines import FastSnapConfig, FastSnapEngine, fast_snap_profiles  # noqa: E402
from trade_mcp.s1_impulse_audit import combine_portfolio_waves, load_tick_stream  # noqa: E402

NIGHT_HOURS = {21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8}


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
    }


def _night_only(waves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        w
        for w in waves
        if float(w.get("opened_at", 0.0)) > 0
        and datetime.fromtimestamp(float(w["opened_at"]), tz=timezone.utc).hour in NIGHT_HOURS
    ]


def inspect_recent_moves(ticks: list[tuple[float, float, float, dict[str, Any]]], *, since_hour: int) -> list[dict[str, Any]]:
    """Find the steepest 5s/10s/30s/60s moves in ticks at or after since_hour SRV and diagnose VSNAP6_24H gates."""
    if not ticks:
        return []
    # Pick the latest date in this tick stream
    last_dt = datetime.fromtimestamp(ticks[-1][0], tz=timezone.utc)
    cutoff_dt = last_dt.replace(hour=since_hour, minute=0, second=0, microsecond=0)
    if cutoff_dt > last_dt:
        cutoff_ts = ticks[0][0]
    else:
        cutoff_ts = cutoff_dt.timestamp()

    probe_cfg = replace(dict(fast_snap_profiles(base_lot=0.02))["VSNAP_SHOCK_6"], name="VSNAP6_24H", active_server_hours=None)
    probe = FastSnapEngine(cfg=probe_cfg)
    aw_v3 = AllWeatherPaperSuite(initial_capital_usd=1000.0)

    # Track 1-second snapshots for the since_hour window
    top_bursts: list[dict[str, Any]] = []
    last_rec_sec = -1
    for ts, bid, ask, m1_ctx in ticks:
        aw_v3.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        probe.on_tick(ts=ts, bid=bid, ask=ask)
        if ts < cutoff_ts:
            continue
        sec = int(ts)
        if sec == last_rec_sec:
            continue
        last_rec_sec = sec
        mid = 0.5 * (bid + ask)
        sp = round(ask - bid, 3)
        h5, m5, eff5, _, _ = probe._window_stats(sec, mid, 5)
        h10, m10, eff10, _, _ = probe._window_stats(sec, mid, 10)
        h15, m15, eff15, _, _ = probe._window_stats(sec, mid, 15)
        h30, m30, eff30, hi30, lo30 = probe._window_stats(sec, mid, 30)
        h60, m60, eff60, _, _ = probe._window_stats(sec, mid, 60)
        h8, m8, _, _, _ = probe._window_stats(sec, mid, 8)
        # Only inspect notable bursts (|m10| >= 1.50 or |m30| >= 2.50 or |m60| >= 3.50)
        if max(abs(m10), abs(m30) * 0.6, abs(m60) * 0.45) < 1.50:
            continue
        # Diagnose EXACT VSNAP6_24H blockers at this second
        sdir = 1 if (m30 if abs(m30) >= abs(m10) else m10) > 0 else -1
        pb30 = (hi30 - mid) if sdir == 1 else (mid - lo30)
        blockers: list[str] = []
        if sp > probe_cfg.max_spread_price:
            blockers.append(f"spread={sp:.2f}>{probe_cfg.max_spread_price}")
        if not (h30 and abs(m30) >= probe_cfg.min_fast_move):
            blockers.append(f"|m30|={abs(m30):.2f}<{probe_cfg.min_fast_move}")
        elif eff30 < probe_cfg.min_fast_eff:
            blockers.append(f"eff30={eff30:.2f}<{probe_cfg.min_fast_eff}")
        elif not (h8 and (m8 * sdir) >= probe_cfg.min_confirm_move):
            blockers.append(f"m8={m8*sdir:+.2f}<{probe_cfg.min_confirm_move}")
        if not (h60 and abs(m60) >= probe_cfg.alt_60s_move):
            blockers.append(f"|m60|={abs(m60):.2f}<{probe_cfg.alt_60s_move}")
        elif eff60 < probe_cfg.alt_60s_eff:
            blockers.append(f"eff60={eff60:.2f}<{probe_cfg.alt_60s_eff}")
        elif not (h10 and (m10 * sdir) >= probe_cfg.alt_10s_move):
            blockers.append(f"m10={m10*sdir:+.2f}<{probe_cfg.alt_10s_move}")
        if pb30 > probe_cfg.max_pullback_from_30s_ext:
            blockers.append(f"pb30={pb30:.2f}>{probe_cfg.max_pullback_from_30s_ext}")
        if probe.basket is not None:
            blockers.append("already_in_basket")
        elif ts < probe.cooldown_until:
            blockers.append(f"cooldown({int(probe.cooldown_until - ts)}s)")

        top_bursts.append({
            "ts": ts,
            "hm": _hm(ts),
            "mid": round(mid, 2),
            "spread": sp,
            "m5": round(m5, 2) if h5 else 0.0,
            "m10": round(m10, 2) if h10 else 0.0,
            "m15": round(m15, 2) if h15 else 0.0,
            "m30": round(m30, 2) if h30 else 0.0,
            "m60": round(m60, 2) if h60 else 0.0,
            "eff10": round(eff10, 2) if h10 else 0.0,
            "eff15": round(eff15, 2) if h15 else 0.0,
            "eff30": round(eff30, 2) if h30 else 0.0,
            "eff60": round(eff60, 2) if h60 else 0.0,
            "pb30": round(pb30, 2),
            "blockers": blockers,
        })

    # Cluster nearby seconds (30s bucket) keeping peak |m10| + |m30| per burst event
    events: list[dict[str, Any]] = []
    for row in top_bursts:
        score = abs(row["m10"]) * 1.5 + abs(row["m30"]) + abs(row["m60"]) * 0.5
        if events and (row["ts"] - events[-1]["ts"]) <= 35.0:
            prev_score = abs(events[-1]["m10"]) * 1.5 + abs(events[-1]["m30"]) + abs(events[-1]["m60"]) * 0.5
            if score > prev_score:
                events[-1] = row
        else:
            events.append(row)

    # Also return any AW_V3 trades opened >= cutoff_ts
    recent_trades = [t for t in aw_v3.trades_closed if float(t.get("opened_at", 0.0)) >= cutoff_ts]
    if aw_v3.open_positions(ticks[-1][1], ticks[-1][2]):
        recent_trades.append({"open_now": aw_v3.open_positions(ticks[-1][1], ticks[-1][2])})
    return events, recent_trades


def _build_cliff_variants() -> list[tuple[str, FastSnapConfig]]:
    """5 ultra-fast 5s-15s vertical-cliff acceleration engines for A/B testing alongside AW_V3_24H."""
    base = replace(dict(fast_snap_profiles(base_lot=0.02))["VSNAP_SHOCK_6"], active_server_hours=None)
    return [
        # 1) 10-second vertical cliff >= $2.80, high efficiency >= 0.72, 5s confirm >= $1.20 (24h)
        (
            "CLIFF_10S_28_24H",
            replace(
                base,
                name="CLIFF_10S_28_24H",
                fast_win_sec=10,
                min_fast_move=2.80,
                min_fast_eff=0.72,
                confirm_win_sec=5,
                min_confirm_move=1.20,
                alt_60s_move=0.0,
                max_pullback_from_30s_ext=0.55,
                hard_stop_price=2.20,
                be_arm_price=1.10,
                ratchet_arm_price=1.90,
                trail_arm_price=2.50,
                trail_pullback_price=1.50,
            ),
        ),
        # 2) 10-second vertical cliff >= $3.40, high efficiency >= 0.72, 5s confirm >= $1.40 (24h)
        (
            "CLIFF_10S_34_24H",
            replace(
                base,
                name="CLIFF_10S_34_24H",
                fast_win_sec=10,
                min_fast_move=3.40,
                min_fast_eff=0.72,
                confirm_win_sec=5,
                min_confirm_move=1.40,
                alt_60s_move=0.0,
                max_pullback_from_30s_ext=0.60,
                hard_stop_price=2.40,
                be_arm_price=1.20,
                ratchet_arm_price=2.10,
                trail_arm_price=2.80,
                trail_pullback_price=1.70,
            ),
        ),
        # 3) 15-second vertical cliff >= $3.60, high efficiency >= 0.68, 5s confirm >= $1.20 (24h)
        (
            "CLIFF_15S_36_24H",
            replace(
                base,
                name="CLIFF_15S_36_24H",
                fast_win_sec=15,
                min_fast_move=3.60,
                min_fast_eff=0.68,
                confirm_win_sec=5,
                min_confirm_move=1.20,
                alt_60s_move=0.0,
                max_pullback_from_30s_ext=0.65,
                hard_stop_price=2.40,
                be_arm_price=1.25,
                ratchet_arm_price=2.20,
                trail_arm_price=2.90,
                trail_pullback_price=1.80,
            ),
        ),
        # 4) 15-second vertical cliff >= $4.20, high efficiency >= 0.65, 5s confirm >= $1.30 (24h)
        (
            "CLIFF_15S_42_24H",
            replace(
                base,
                name="CLIFF_15S_42_24H",
                fast_win_sec=15,
                min_fast_move=4.20,
                min_fast_eff=0.65,
                confirm_win_sec=5,
                min_confirm_move=1.30,
                alt_60s_move=0.0,
                max_pullback_from_30s_ext=0.70,
                hard_stop_price=2.60,
                be_arm_price=1.35,
                ratchet_arm_price=2.30,
                trail_arm_price=3.10,
                trail_pullback_price=1.95,
            ),
        ),
        # 5) 15-second cliff >= $3.60 + 60s alignment >= $2.50 (blocks pure counter-trend micro-wicks!)
        (
            "CLIFF_15S_36_ALIGN",
            replace(
                base,
                name="CLIFF_15S_36_ALIGN",
                fast_win_sec=15,
                min_fast_move=3.60,
                min_fast_eff=0.68,
                confirm_win_sec=5,
                min_confirm_move=1.20,
                min_60s_align_move=2.50,
                min_60s_align_eff=0.30,
                alt_60s_move=0.0,
                max_pullback_from_30s_ext=0.65,
                hard_stop_price=2.50,
                be_arm_price=1.25,
                ratchet_arm_price=2.20,
                trail_arm_price=3.00,
                trail_pullback_price=1.85,
            ),
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect recent fast drops + 5s/15s Vertical-Cliff A/B (ORDERS=0)")
    parser.add_argument("--since-hour", type=int, default=18, help="Server hour to zoom from (default 18)")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    paths = sorted((ROOT / "logs").glob("s1_ticks_*.jsonl"))
    if not paths:
        print("No tick files in logs/s1_ticks_*.jsonl", file=sys.stderr)
        return 1

    # Load all files, locate latest continuous file (largest within 5m of max mtime)
    streams = [(p, load_tick_stream(p, rebuild_m1=True)) for p in paths]
    streams = [(p, t) for p, t in streams if t]
    max_end = max(t[-1][0] for _, t in streams)
    recent_files = [(p, t) for p, t in streams if (max_end - t[-1][0]) <= 600.0]
    latest_path, latest_ticks = max(recent_files, key=lambda x: len(x[1]))

    print(
        f"=== PART 1: FORENSIC DROP MICROSCOPE ({latest_path.name} | "
        f"{_hm(latest_ticks[0][0])} -> {_hm(latest_ticks[-1][0])} SRV | since {args.since_hour:02d}:00) ==="
    )
    bursts, recent_aw_trades = inspect_recent_moves(latest_ticks, since_hour=args.since_hour)
    if not bursts:
        print(f"  No fast move >= $1.50/10s found since {args.since_hour:02d}:00 SRV in {latest_path.name}.")
    else:
        for b in bursts[-20:]:
            blk = ", ".join(b["blockers"]) if b["blockers"] else "PASSES VSNAP6!"
            print(
                f"  {b['hm']} @ {b['mid']:7.2f} (sp={b['spread']:.2f}) | "
                f"m5={b['m5']:+6.2f} m10={b['m10']:+6.2f}(e={b['eff10']:.2f}) "
                f"m15={b['m15']:+6.2f}(e={b['eff15']:.2f}) m30={b['m30']:+6.2f}(e={b['eff30']:.2f}) "
                f"m60={b['m60']:+6.2f}(e={b['eff60']:.2f}) pb30={b['pb30']:.2f} | BLOCK: {blk}"
            )
    print(f"  AW_V3_24H trades since {args.since_hour:02d}:00 SRV: {len(recent_aw_trades)}")
    for tr in recent_aw_trades:
        print(f"    -> {tr}")

    # Part 2: Benchmark 5s/15s Vertical-Cliff variants across ALL files, NIGHT, TODAY, and SINCE_HOUR!
    cliff_defs = _build_cliff_variants()
    merged: dict[str, list[dict[str, Any]]] = {"AW_V3_24H": []}
    today_w: dict[str, list[dict[str, Any]]] = {"AW_V3_24H": []}
    since_w: dict[str, list[dict[str, Any]]] = {"AW_V3_24H": []}
    for name, _ in cliff_defs:
        merged[name] = []
        today_w[name] = []
        since_w[name] = []

    last_dt = datetime.fromtimestamp(latest_ticks[-1][0], tz=timezone.utc)
    cutoff_ts = last_dt.replace(hour=args.since_hour, minute=0, second=0, microsecond=0).timestamp()
    if cutoff_ts > latest_ticks[-1][0]:
        cutoff_ts = latest_ticks[0][0]

    total_ticks = 0
    for p, ticks in streams:
        total_ticks += len(ticks)
        aw = AllWeatherPaperSuite(initial_capital_usd=1000.0)
        cliffs = {name: FastSnapEngine(cfg=cfg) for name, cfg in cliff_defs}
        for ts, bid, ask, m1_ctx in ticks:
            aw.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
            for eng in cliffs.values():
                eng.on_tick(ts=ts, bid=bid, ask=ask)
        lts, lbid, lask, _ = ticks[-1]
        aw.close_all_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
        for eng in cliffs.values():
            eng.force_close_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")

        merged["AW_V3_24H"].extend(aw.trades_closed)
        if p == latest_path:
            today_w["AW_V3_24H"].extend(aw.trades_closed)
            since_w["AW_V3_24H"].extend(w for w in aw.trades_closed if float(w.get("opened_at", 0.0)) >= cutoff_ts)
        for name, eng in cliffs.items():
            merged[name].extend(eng.waves_closed)
            if p == latest_path:
                today_w[name].extend(eng.waves_closed)
                since_w[name].extend(w for w in eng.waves_closed if float(w.get("opened_at", 0.0)) >= cutoff_ts)

    print(f"\n=== PART 2: 5s/15s VERTICAL-CLIFF A/B ({len(streams)} FILES, {total_ticks} TICKS, ORDERS=0) ===")
    for k in ["AW_V3_24H"] + [n for n, _ in cliff_defs]:
        sa = _stats(merged[k], k)
        sn = _stats(_night_only(merged[k]), k)
        st = _stats(today_w[k], k)
        ss = _stats(since_w[k], k)
        print(
            f"  {k:20s} | ALL: ${sa['profit']:+7.2f} DD:-${sa['max_dd']:5.2f} ({sa['wins']:2d}W/{sa['losses']:2d}L PF:{sa['pf']:4.2f}) | "
            f"Night:${sn['profit']:+6.2f} | Today:${st['profit']:+6.2f} | >={args.since_hour:02d}h:${ss['profit']:+6.2f}({ss['trades']}t)"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
