#!/usr/bin/env python3
"""AW_V3 24-Hour Champion Evolution Benchmark on All Real Recorded Ticks (ORDERS=0).

Unlocks and stress-tests the repo's PROVEN high-PF engines (MACRO_WAVE_DYN PF 3.95,
VSNAP_SHOCK_6 80% WR / PF 2.44, SHOCK10_DYN_RUN 100% WR, GH_Z_M1_Z22_24H 100% WR)
for 24-hour / night coverage, variable slope lot sizing, slope stacking, and replacing
the weak CALM_M1_PRO_SESS engine (0.21x R/DD).

Strictly read-only / paper-only (ORDERS=0).
"""

from __future__ import annotations

import argparse
import json
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
from trade_mcp.fast_snap_engines import FastSnapEngine, fast_snap_profiles  # noqa: E402
from trade_mcp.quant_github_accel_lab import TickRobustZScoreConfig, TickRobustZScoreEngine  # noqa: E402
from trade_mcp.s1_impulse_audit import (  # noqa: E402
    PullbackDipEngine,
    combine_portfolio_waves,
    load_tick_stream,
    pullback_dip_profiles,
)
from trade_mcp.s1_velocity_surfer import S1VelocitySurferEngine, build_s1_profile_config  # noqa: E402

NIGHT_HOURS = {21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8}
NIGHT_TUPLE = (21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8)


def _stats(waves: list[dict[str, Any]], name: str = "s") -> dict[str, Any]:
    s = combine_portfolio_waves(name, [waves])
    avg_legs = round(sum(int(w.get("legs_count", 1)) for w in waves) / len(waves), 2) if waves else 0.0
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
        "avg_legs": avg_legs,
    }


def _night_only(waves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for w in waves:
        ts = float(w.get("opened_at", 0.0))
        if ts > 0 and datetime.fromtimestamp(ts, tz=timezone.utc).hour in NIGHT_HOURS:
            out.append(w)
    return out


def _fmt(label: str, st: dict[str, Any], extra: str = "") -> str:
    return (
        f"  {label:28s} | Profit: ${st['profit']:+8.2f} | MaxDD: -${st['max_dd']:6.2f} "
        f"(R/DD:{st['ret_dd']:5.2f}x) | Trades: {st['trades']:3d} "
        f"({st['wins']:2d}W/{st['losses']:2d}L = {st['wr']:5.1f}%) | "
        f"PF:{st['pf']:5.2f} | Legs:{st['avg_legs']:4.2f} MaxLot:{st['max_lot']:4.2f}{extra}"
    )


def build_evolution_engines() -> dict[str, Any]:
    """Build all 24h champion evolution candidates (strictly causal, ORDERS=0)."""
    macro_base = dict(pullback_dip_profiles(leg_lot=0.01, max_legs=6))["MACRO_WAVE_DYN"]
    vsnap_base = dict(fast_snap_profiles(base_lot=0.02))["VSNAP_SHOCK_6"]
    shock10_base = build_s1_profile_config("v2.6_SHOCK10_DYN", leg_lot=0.01, max_legs=6)

    engs: dict[str, Any] = {
        # --- 1) MACRO_WAVE_DYN evolution (currently +$102.10, PF 3.95, 6.22x R/DD, 0 night!) ---
        "MACRO_WAVE_SESS": PullbackDipEngine(cfg=macro_base),
        "MACRO_WAVE_24H": PullbackDipEngine(
            cfg=replace(macro_base, active_server_hours=None)
        ),
        "MACRO_WAVE_24H_VARL1": PullbackDipEngine(
            cfg=replace(
                macro_base,
                active_server_hours=None,
                dyn_max_l1_lot=0.05,
                dyn_max_leg_lot=0.12,
                dyn_max_total_lot=0.55,
            )
        ),
        "MACRO_WAVE_24H_M65": PullbackDipEngine(
            cfg=replace(
                macro_base,
                active_server_hours=None,
                min_trend_move_price=6.50,
                min_pullback_price=1.60,
                max_pullback_price=4.50,
                min_resume_price=0.75,
                dyn_max_l1_lot=0.04,
            )
        ),
        "MACRO_WAVE_NIGHT_55": PullbackDipEngine(
            cfg=replace(
                macro_base,
                active_server_hours=NIGHT_TUPLE,
                min_trend_move_price=5.50,
                min_pullback_price=1.40,
                max_pullback_price=3.80,
                min_resume_price=0.65,
                min_stop_price=1.90,
                max_stop_price=2.80,
                be_arm_price=2.00,
                profit_lock_arm_price=3.40,
                profit_lock_floor_price=1.20,
                add_step_price=1.70,
                dyn_max_l1_lot=0.03,
            )
        ),
        # --- 2) VSNAP_SHOCK_6 evolution (currently +$44.09, 80.0% WR, PF 2.44, 0 night!) ---
        "VSNAP6_SESS": FastSnapEngine(cfg=vsnap_base),
        "VSNAP6_24H": FastSnapEngine(
            cfg=replace(vsnap_base, name="VSNAP6_24H", active_server_hours=None)
        ),
        "VSNAP6_24H_VARLOT": FastSnapEngine(
            cfg=replace(
                vsnap_base,
                name="VSNAP6_24H_VARLOT",
                active_server_hours=None,
                leg_lot=0.03,
                max_legs=5,
                add_step_price=1.60,
                add_interval_sec=4.0,
                dyn_max_l1_lot=0.06,
                dyn_max_leg_lot=0.12,
                dyn_max_total_lot=0.45,
            )
        ),
        "VSNAP5_24H": FastSnapEngine(
            cfg=replace(
                vsnap_base,
                name="VSNAP5_24H",
                active_server_hours=None,
                min_fast_move=4.00,
                alt_60s_move=5.00,
                leg_lot=0.02,
                max_legs=5,
                add_step_price=1.60,
                dyn_max_l1_lot=0.05,
            )
        ),
        "VSNAP_NIGHT_45": FastSnapEngine(
            cfg=replace(
                vsnap_base,
                name="VSNAP_NIGHT_45",
                active_server_hours=NIGHT_TUPLE,
                min_fast_move=3.60,
                alt_60s_move=4.50,
                confirm_win_sec=8,
                min_confirm_move=0.75,
                hard_stop_price=2.40,
                be_arm_price=1.25,
                ratchet_arm_price=2.10,
                leg_lot=0.02,
                max_legs=4,
                add_step_price=1.50,
                dyn_max_l1_lot=0.04,
            )
        ),
        # --- 3) SHOCK10_DYN_RUN evolution (currently +$20.21, 100% WR, 0 DD, 24h!) ---
        "SHOCK10_DYN_RUN": S1VelocitySurferEngine(cfg=shock10_base),
        "SHOCK8_DYN_24H": S1VelocitySurferEngine(
            cfg=replace(shock10_base, min_60s_move_price=8.00)
        ),
        # --- 4) GitHub TickRobust Z-Score MR (currently +$12.95, 100% WR, 0 DD, +$5.31 today!) ---
        "GH_Z_M1_Z22_24H": TickRobustZScoreEngine(
            cfg=TickRobustZScoreConfig(
                name="GH_Z_M1_Z22_24H",
                bar_sec=60,
                entry_z=2.2,
                adx_max=22.0,
                min_sd_cost_mult=2.5,
                min_tp_cost_mult=3.5,
                min_sl_price=4.50,
                max_sl_price=7.50,
                leg_lot=0.02,
                active_server_hours=None,
            )
        ),
        "GH_Z_M1_Z22_L03_24H": TickRobustZScoreEngine(
            cfg=TickRobustZScoreConfig(
                name="GH_Z_M1_Z22_L03_24H",
                bar_sec=60,
                entry_z=2.2,
                adx_max=22.0,
                min_sd_cost_mult=2.5,
                min_tp_cost_mult=3.5,
                min_sl_price=4.50,
                max_sl_price=7.50,
                leg_lot=0.03,
                variable_lot_by_z=True,
                max_lot=0.06,
                active_server_hours=None,
            )
        ),
        "GH_Z_M1_Z22_NIGHT_PLUS": TickRobustZScoreEngine(
            cfg=TickRobustZScoreConfig(
                name="GH_Z_M1_Z22_NIGHT_PLUS",
                bar_sec=60,
                entry_z=2.15,
                adx_max=24.0,
                min_sd_cost_mult=2.2,
                min_tp_cost_mult=3.2,
                min_sl_price=4.50,
                max_sl_price=7.50,
                leg_lot=0.03,
                variable_lot_by_z=True,
                max_lot=0.05,
                active_server_hours=NIGHT_TUPLE,
            )
        ),
    }
    return engs


def _run_file(path: Path) -> dict[str, Any]:
    ticks = load_tick_stream(path, rebuild_m1=True)
    if not ticks:
        return {"file": path.name, "ticks": 0, "end_ts": 0.0, "waves": {}}

    aw_v2 = AllWeatherPaperSuite(initial_capital_usd=1000.0, enable_fast_shock6=True)
    engs = build_evolution_engines()

    for ts, bid, ask, m1_ctx in ticks:
        aw_v2.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        for eng in engs.values():
            if isinstance(eng, (S1VelocitySurferEngine, PullbackDipEngine)):
                eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
            else:
                eng.on_tick(ts=ts, bid=bid, ask=ask)

    lts, lbid, lask, _ = ticks[-1]
    aw_v2.close_all_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
    for eng in engs.values():
        if hasattr(eng, "force_close_open"):
            eng.force_close_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
        elif hasattr(eng, "close_open_wave"):
            eng.close_open_wave(ts=lts, bid=lbid, ask=lask, reason="eof_replay")

    waves: dict[str, list[dict[str, Any]]] = {"AW_V2_CURRENT": list(aw_v2.trades_closed)}
    for k, eng in engs.items():
        waves[k] = list(eng.waves_closed)
    return {"file": path.name, "ticks": len(ticks), "end_ts": lts, "waves": waves}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="24h Champion Evolution Benchmark (ORDERS=0)")
    parser.add_argument("files", nargs="*", help="Tick JSONL files (default: all logs/s1_ticks_*.jsonl)")
    parser.add_argument("--save", action="store_true", help="Save reports/paper_live/champions_24h_evolution.json")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    if args.files:
        paths = [Path(p) if Path(p).is_absolute() else (ROOT / p) for p in args.files]
    else:
        paths = sorted((ROOT / "logs").glob("s1_ticks_*.jsonl"))
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("No tick JSONL files found in logs/s1_ticks_*.jsonl", file=sys.stderr)
        return 1

    per_file = [_run_file(p) for p in paths]
    total_ticks = sum(r["ticks"] for r in per_file)
    by_end = sorted(per_file, key=lambda r: r["end_ts"], reverse=True)
    today_res = max(by_end[:5], key=lambda r: r["ticks"])

    keys = ["AW_V2_CURRENT"] + list(build_evolution_engines().keys())
    merged: dict[str, list[dict[str, Any]]] = {
        k: [w for r in per_file for w in r["waves"].get(k, [])] for k in keys
    }

    print(f"=== PART 1: ALL {len(per_file)} FILES ({total_ticks} TICKS, ORDERS=0) — 24H CHAMPION EVOLUTION ===")
    all_stats: dict[str, Any] = {}
    night_stats: dict[str, Any] = {}
    today_stats: dict[str, Any] = {}
    for k in keys:
        st_all = _stats(merged[k], k)
        st_n = _stats(_night_only(merged[k]), k)
        st_t = _stats(today_res["waves"].get(k, []), k)
        all_stats[k] = st_all
        night_stats[k] = st_n
        today_stats[k] = st_t
        print(_fmt(k, st_all, f" | Night:${st_n['profit']:+6.2f}({st_n['trades']:2d}) Today:${st_t['profit']:+6.2f}({st_t['trades']:2d})"))

    # Head-to-head AW_V3 Portfolio Candidates (ALL replacing bleeding CALM_M1_PRO_SESS!)
    portfolios: list[tuple[str, list[str]]] = [
        ("AW_V2_CURRENT (with CALM)", ["AW_V2_CURRENT"]),
        ("AW_V3_CLEAN_SESS", ["SHOCK10_DYN_RUN", "VSNAP6_SESS", "MACRO_WAVE_SESS", "GH_Z_M1_Z22_24H"]),
        ("AW_V3_24H_UNLOCKED", ["SHOCK10_DYN_RUN", "VSNAP6_24H", "MACRO_WAVE_24H", "GH_Z_M1_Z22_24H"]),
        ("AW_V3_24H_PRO_VARLOT", ["SHOCK10_DYN_RUN", "VSNAP6_24H_VARLOT", "MACRO_WAVE_24H_VARL1", "GH_Z_M1_Z22_L03_24H"]),
        ("AW_V3_24H_SHOCK8_PRO", ["SHOCK8_DYN_24H", "VSNAP6_24H_VARLOT", "MACRO_WAVE_24H_VARL1", "GH_Z_M1_Z22_L03_24H"]),
        ("AW_V3_24H_MAX_NIGHT", ["SHOCK10_DYN_RUN", "VSNAP6_24H_VARLOT", "MACRO_WAVE_24H_M65", "GH_Z_M1_Z22_L03_24H"]),
        ("AW_V3_24H_DEDICATED_NIGHT", ["SHOCK10_DYN_RUN", "VSNAP6_24H_VARLOT", "MACRO_WAVE_24H_VARL1", "MACRO_WAVE_NIGHT_55", "GH_Z_M1_Z22_L03_24H"]),
    ]

    print("\n=== PART 2: HEAD-TO-HEAD AW_V3 PORTFOLIOS (ALL / NIGHT / TODAY) ===")
    port_stats: dict[str, Any] = {}
    for label, members in portfolios:
        w_all = [w for m in members for w in merged[m]]
        w_tod = [w for m in members for w in today_res["waves"].get(m, [])]
        st_all = _stats(w_all, label)
        st_n = _stats(_night_only(w_all), label)
        st_t = _stats(w_tod, label)
        port_stats[label] = {"members": members, "all": st_all, "night": st_n, "today": st_t}
        print(_fmt(label, st_all, f" | Night:${st_n['profit']:+6.2f}({st_n['trades']:2d}) Today:${st_t['profit']:+6.2f}({st_t['trades']:2d})"))

    if args.save:
        out_path = ROOT / "reports" / "paper_live" / "champions_24h_evolution.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "champions_24h_evolution_v1",
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "orders_submitted": 0,
            "files_count": len(per_file),
            "total_ticks": total_ticks,
            "today_file": today_res["file"],
            "all_stats": all_stats,
            "night_stats": night_stats,
            "today_stats": today_stats,
            "portfolios": port_stats,
        }
        tmp = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, out_path)
        print(f"\nSAVED={out_path.relative_to(ROOT)} ({out_path.stat().st_size} bytes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
