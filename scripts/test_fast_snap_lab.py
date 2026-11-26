#!/usr/bin/env python3
"""Causal A/B lab for Fast-Snap / V-Reversal / Acceleration engines on real recorded XAUUSD_i ticks (ORDERS=0).

Evaluates:
1. Today's continuous daily file (zooming directly into the 15:00-15:05 sharp V-spike + full session).
2. All recorded tick JSONL files (standalone engines + combined All-Weather suites).

Strictly read-only / paper-only (ORDERS=0).
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
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trade_mcp.all_weather_paper import AllWeatherPaperSuite  # noqa: E402
from trade_mcp.fast_snap_engines import FastSnapEngine, fast_snap_profiles  # noqa: E402
from trade_mcp.s1_impulse_audit import combine_portfolio_waves, load_tick_stream  # noqa: E402


def _portfolio_stats(waves: list[dict[str, Any]], label: str = "port") -> dict[str, Any]:
    s = combine_portfolio_waves(label, [waves])
    return {
        "trades": s["waves_count"],
        "wins": s["wins"],
        "losses": s["losses"],
        "wr": s["wr_pct"],
        "profit": s["net_usd"],
        "max_dd": s["max_dd_usd"],
        "pf": float(s["pf"]) if s["pf"] is not None else 0.0,
        "max_lot": s["max_lot"],
    }


def _run_file(path: Path) -> dict[str, Any]:
    ticks = load_tick_stream(path, rebuild_m1=True)
    if not ticks:
        return {"file": path.name, "ticks": 0, "aw_waves": [], "fs_waves": {}}

    aw = AllWeatherPaperSuite(initial_capital_usd=1000.0)
    fs_engines = {name: FastSnapEngine(cfg=cfg) for name, cfg in fast_snap_profiles()}

    for ts, bid, ask, m1_ctx in ticks:
        aw.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        for eng in fs_engines.values():
            eng.on_tick(ts=ts, bid=bid, ask=ask)

    last_ts, last_bid, last_ask, _ = ticks[-1]
    aw.close_all_open(ts=last_ts, bid=last_bid, ask=last_ask, reason="eof_replay")
    for eng in fs_engines.values():
        eng.force_close_open(ts=last_ts, bid=last_bid, ask=last_ask, reason="eof_replay")

    return {
        "file": path.name,
        "ticks": len(ticks),
        "start_ts": ticks[0][0],
        "end_ts": ticks[-1][0],
        "aw_waves": list(aw.trades_closed),
        "fs_waves": {name: list(eng.waves_closed) for name, eng in fs_engines.items()},
    }


def _fmt_row(label: str, st: dict[str, Any]) -> str:
    return (
        f"  {label:28s} | Profit: ${st['profit']:+8.2f} | MaxDD: -${st['max_dd']:6.2f} | "
        f"Trades: {st['trades']:3d} ({st['wins']:2d}W/{st['losses']:2d}L = {st['wr']:5.1f}%) | PF: {st['pf']:5.2f}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A/B benchmark for Fast-Snap / V-Reversal / Acceleration engines (ORDERS=0)")
    parser.add_argument("files", nargs="*", help="Tick JSONL files (default: all logs/s1_ticks_*.jsonl)")
    parser.add_argument("--today-file", default="", help="Explicit today continuous file (default: largest recent file)")
    parser.add_argument("--save", action="store_true", help="Save compact report to reports/paper_live/fast_snap_lab.json")
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

    # Identify today's continuous file (largest file among the newest 5 files)
    today_res = None
    if args.today_file:
        tf_name = Path(args.today_file).name
        today_res = next((r for r in per_file if r["file"] == tf_name), None)
    if today_res is None:
        by_end = sorted(per_file, key=lambda r: r.get("end_ts", 0.0), reverse=True)
        today_res = max(by_end[:5], key=lambda r: r["ticks"])

    fs_names = [name for name, _ in fast_snap_profiles()]

    # Aggregate across ALL files
    all_aw = [w for r in per_file for w in r["aw_waves"]]
    all_fs = {n: [w for r in per_file for w in r["fs_waves"].get(n, [])] for n in fs_names}

    combos: list[tuple[str, list[dict[str, Any]]]] = [
        ("BASELINE_AW (Current Live)", all_aw),
        ("AW + VSNAP_30S_SESS", all_aw + all_fs["VSNAP_30S_SESS"]),
        ("AW + VSNAP_SHOCK_6", all_aw + all_fs["VSNAP_SHOCK_6"]),
        ("AW + VTOP_SWEEP_SNAP", all_aw + all_fs["VTOP_SWEEP_SNAP"]),
        ("AW + MICRO_PB_7", all_aw + all_fs["MICRO_PB_7"]),
        ("AW + ACCEL_SCALP_15S", all_aw + all_fs["ACCEL_SCALP_15S"]),
        ("AW + VSNAP30 + VTOP_SWEEP", all_aw + all_fs["VSNAP_30S_SESS"] + all_fs["VTOP_SWEEP_SNAP"]),
        ("AW + SHOCK6 + VTOP_SWEEP", all_aw + all_fs["VSNAP_SHOCK_6"] + all_fs["VTOP_SWEEP_SNAP"]),
        ("AW + SHOCK6 + MICRO_PB_7", all_aw + all_fs["VSNAP_SHOCK_6"] + all_fs["MICRO_PB_7"]),
    ]

    print(f"=== PART 1: TODAY CONTINUOUS FILE ({today_res['file']}, {today_res['ticks']} ticks, ORDERS=0) ===")
    print(_fmt_row("BASELINE_AW (Current)", _portfolio_stats(today_res["aw_waves"])))
    today_summary: dict[str, Any] = {
        "file": today_res["file"],
        "ticks": today_res["ticks"],
        "baseline_aw": _portfolio_stats(today_res["aw_waves"]),
        "engines": {},
        "spike_window_trades": {},
    }
    for n in fs_names:
        waves = today_res["fs_waves"].get(n, [])
        st = _portfolio_stats(waves)
        today_summary["engines"][n] = st
        print(_fmt_row(n, st))
        # Highlight any trades around 14:55..15:10 SRV!
        spike_w = [
            {
                "opened": str(w.get("opened_utc", ""))[11:19],
                "closed": str(w.get("closed_utc", ""))[11:19],
                "side": str(w.get("side", "")).upper(),
                "lot": w.get("total_volume"),
                "entry": w.get("avg_entry"),
                "exit": w.get("exit_price"),
                "pnl": w.get("net_pnl_usd"),
                "reason": w.get("exit_reason"),
            }
            for w in waves
            if "14:55:00" <= str(w.get("opened_utc", ""))[11:19] <= "15:12:00"
        ]
        if spike_w:
            today_summary["spike_window_trades"][n] = spike_w
            for sw in spike_w:
                print(
                    f"      -> SPIKE {sw['opened']}->{sw['closed']} {sw['side']:4s} {sw['lot']:.2f}lot "
                    f"{sw['entry']:.2f}->{sw['exit']:.2f} PnL=${sw['pnl']:+.2f} ({sw['reason']})"
                )

    print(f"\n=== PART 2: ALL {len(per_file)} FILES ({total_ticks} TICKS) — STANDALONE NEW ENGINES ===")
    all_standalone_stats: dict[str, Any] = {}
    for n in fs_names:
        st = _portfolio_stats(all_fs[n])
        all_standalone_stats[n] = st
        print(_fmt_row(n, st))

    print(f"\n=== PART 3: ALL {len(per_file)} FILES ({total_ticks} TICKS) — COMBINED ALL-WEATHER SUITES ===")
    all_combo_stats: dict[str, Any] = {}
    for label, wlist in combos:
        st = _portfolio_stats(wlist)
        all_combo_stats[label] = st
        print(_fmt_row(label, st))

    if args.save:
        out_path = ROOT / "reports" / "paper_live" / "fast_snap_lab.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "fast_snap_lab_v1",
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "orders_submitted": 0,
            "files_count": len(per_file),
            "total_ticks": total_ticks,
            "today": today_summary,
            "all_standalone": all_standalone_stats,
            "all_combos": all_combo_stats,
        }
        tmp = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, out_path)
        print(f"\nSAVED={out_path.relative_to(ROOT)} ({out_path.stat().st_size} bytes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
