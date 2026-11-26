#!/usr/bin/env python3
"""V3 Parameter & Constraint Sweep: Risk-Free Slope Stacking + TickRobust Z-Score + Night Quiet Box (ORDERS=0).

Evaluates every strategy across:
  0. AW_V2 INTERNAL PER-ENGINE BREAKDOWN (ALL / NIGHT / TODAY)
  1. ALL RECORDED TICK FILES COMBINED
  2. NIGHT / ASIAN QUIET HOURS (21:00-08:59 SRV)
  3. TODAY'S CONTINUOUS SESSION
  4. TOP 24-HOUR AW_V3 PORTFOLIOS (showing Total Profit, MaxDD, Return/DD, Night Profit, Today Profit)

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
from trade_mcp.quant_github_accel_lab import (  # noqa: E402
    QuietRangeBounceEngine,
    RapidBurstAccelEngine,
    TickRobustZScoreEngine,
    build_quant_sweep_candidates,
)
from trade_mcp.s1_impulse_audit import combine_portfolio_waves, load_tick_stream  # noqa: E402

NIGHT_HOURS = {21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8}


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
    base = (
        f"  {label:28s} | Profit: ${st['profit']:+8.2f} | MaxDD: -${st['max_dd']:6.2f} "
        f"(R/DD:{st['ret_dd']:5.2f}x) | Trades: {st['trades']:3d} "
        f"({st['wins']:2d}W/{st['losses']:2d}L = {st['wr']:5.1f}%) | "
        f"PF:{st['pf']:5.2f} | Legs:{st['avg_legs']:4.2f} MaxLot:{st['max_lot']:4.2f}"
    )
    return f"{base}{extra}"


def _run_file(path: Path) -> dict[str, Any]:
    ticks = load_tick_stream(path, rebuild_m1=True)
    if not ticks:
        return {"file": path.name, "ticks": 0, "end_ts": 0.0, "waves": {}}

    accel_cfgs, zscore_cfgs, box_cfgs = build_quant_sweep_candidates()
    aw_v2 = AllWeatherPaperSuite(initial_capital_usd=1000.0, enable_fast_shock6=True)
    accel_engs = {c.name: RapidBurstAccelEngine(cfg=c) for c in accel_cfgs}
    z_engs = {c.name: TickRobustZScoreEngine(cfg=c) for c in zscore_cfgs}
    box_engs = {c.name: QuietRangeBounceEngine(cfg=c) for c in box_cfgs}

    for ts, bid, ask, m1_ctx in ticks:
        aw_v2.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        for eng in accel_engs.values():
            eng.on_tick(ts=ts, bid=bid, ask=ask)
        for eng in z_engs.values():
            eng.on_tick(ts=ts, bid=bid, ask=ask)
        for eng in box_engs.values():
            eng.on_tick(ts=ts, bid=bid, ask=ask)

    lts, lbid, lask, _ = ticks[-1]
    aw_v2.close_all_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
    for eng in accel_engs.values():
        eng.force_close_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
    for eng in z_engs.values():
        eng.force_close_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
    for eng in box_engs.values():
        eng.force_close_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")

    waves: dict[str, list[dict[str, Any]]] = {"AW_V2_CURRENT": list(aw_v2.trades_closed)}
    for k, eng in accel_engs.items():
        waves[k] = list(eng.waves_closed)
    for k, eng in z_engs.items():
        waves[k] = list(eng.waves_closed)
    for k, eng in box_engs.items():
        waves[k] = list(eng.waves_closed)

    return {"file": path.name, "ticks": len(ticks), "end_ts": lts, "waves": waves}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Causal V3 parameter & constraint sweep (ORDERS=0)")
    parser.add_argument("files", nargs="*", help="Tick JSONL files (default: all logs/s1_ticks_*.jsonl)")
    parser.add_argument("--save", action="store_true", help="Save compact report to reports/paper_live/quant_accel_sweep.json")
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

    accel_cfgs, zscore_cfgs, box_cfgs = build_quant_sweep_candidates()
    all_keys = (
        ["AW_V2_CURRENT"]
        + [c.name for c in accel_cfgs]
        + [c.name for c in zscore_cfgs]
        + [c.name for c in box_cfgs]
    )

    merged: dict[str, list[dict[str, Any]]] = {
        k: [w for r in per_file for w in r["waves"].get(k, [])] for k in all_keys
    }

    # Part 0: AW_V2 internal per-engine X-ray
    aw_sub_names = sorted({str(w.get("engine", "UNKNOWN")) for w in merged["AW_V2_CURRENT"]})
    print("=== PART 0: AW_V2 INTERNAL PER-ENGINE BREAKDOWN (ALL / NIGHT / TODAY) ===")
    aw_breakdown: dict[str, Any] = {}
    for sub in aw_sub_names:
        w_all = [w for w in merged["AW_V2_CURRENT"] if w.get("engine") == sub]
        w_night = _night_only(w_all)
        w_today = [w for w in today_res["waves"].get("AW_V2_CURRENT", []) if w.get("engine") == sub]
        st_all, st_n, st_t = _stats(w_all, sub), _stats(w_night, sub), _stats(w_today, sub)
        aw_breakdown[sub] = {"all": st_all, "night": st_n, "today": st_t}
        print(_fmt(sub, st_all, f" | Night:${st_n['profit']:+6.2f} Today:${st_t['profit']:+6.2f}"))

    print(f"\n=== PART 1: ALL {len(per_file)} FILES ({total_ticks} TICKS, ORDERS=0) — V3 PARAMETER & CONSTRAINT SWEEP ===")
    all_stats = {k: _stats(merged[k], k) for k in all_keys}
    for k in all_keys:
        print(_fmt(k, all_stats[k]))

    print("\n=== PART 2: NIGHT / QUIET HOURS ONLY (21:00-08:59 SRV) ===")
    night_stats = {k: _stats(_night_only(merged[k]), k) for k in all_keys}
    for k in all_keys:
        if night_stats[k]["trades"] > 0 or k == "AW_V2_CURRENT":
            print(_fmt(k, night_stats[k]))

    print(f"\n=== PART 3: TODAY CONTINUOUS SESSION ({today_res['file']}, {today_res['ticks']} ticks) ===")
    today_stats = {k: _stats(today_res["waves"].get(k, []), k) for k in all_keys}
    for k in all_keys:
        if today_stats[k]["trades"] > 0 or k == "AW_V2_CURRENT":
            print(_fmt(k, today_stats[k]))

    # Rank best Risk-Free Accel Stacker (by Return/DD weighted Profit), best Night Burst, best Z-Score, best Box
    def _score(st: dict[str, Any]) -> tuple[float, float]:
        if st["profit"] <= 0:
            return (-1.0, st["profit"])
        return (st["profit"] * min(st["ret_dd"], 5.0), st["profit"])

    best_accel = max([c.name for c in accel_cfgs], key=lambda k: _score(all_stats[k]))
    best_accel_night = max([c.name for c in accel_cfgs], key=lambda k: _score(night_stats[k]))
    best_z = max([c.name for c in zscore_cfgs], key=lambda k: _score(all_stats[k]))
    best_box = max([c.name for c in box_cfgs], key=lambda k: _score(all_stats[k]))

    # Also build de-noised AW_V2 core (only engines profitable over all files) + best V3 additions
    aw_base_all = merged["AW_V2_CURRENT"]
    aw_base_today = today_res["waves"].get("AW_V2_CURRENT", [])

    def _combo(keys_extra: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        w_all = list(aw_base_all)
        w_tod = list(aw_base_today)
        for k in keys_extra:
            if all_stats[k]["profit"] > 0:
                w_all.extend(merged[k])
                w_tod.extend(today_res["waves"].get(k, []))
        return w_all, w_tod

    combo_defs: list[tuple[str, list[str]]] = [
        ("AW_V2_CURRENT", []),
        (f"AW_V2+{best_accel}", [best_accel]),
        (f"AW_V2+{best_z}", [best_z]),
        (f"AW_V2+{best_accel}+{best_z}", [best_accel, best_z]),
        (
            "AW_V3_ALL_WINNERS",
            list(dict.fromkeys([best_accel, best_accel_night, best_z, best_box])),
        ),
    ]

    print("\n=== PART 4: 24-HOUR AW_V3 COMBINED PORTFOLIOS (ALL / NIGHT / TODAY) ===")
    combo_stats: dict[str, Any] = {}
    for label, extras in combo_defs:
        w_all, w_tod = _combo(extras)
        st_all = _stats(w_all, label)
        st_n = _stats(_night_only(w_all), label)
        st_t = _stats(w_tod, label)
        combo_stats[label] = {"all": st_all, "night": st_n, "today": st_t, "added": [k for k in extras if all_stats[k]["profit"] > 0]}
        print(_fmt(label, st_all, f" | Night:${st_n['profit']:+6.2f} Today:${st_t['profit']:+6.2f}"))

    if args.save:
        out_path = ROOT / "reports" / "paper_live" / "quant_accel_sweep.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "quant_accel_sweep_v2",
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "orders_submitted": 0,
            "files_count": len(per_file),
            "total_ticks": total_ticks,
            "today_file": today_res["file"],
            "aw_v2_breakdown": aw_breakdown,
            "best_accel": best_accel,
            "best_accel_night": best_accel_night,
            "best_z": best_z,
            "best_box": best_box,
            "all_stats": all_stats,
            "night_stats": night_stats,
            "today_stats": today_stats,
            "combo_stats": combo_stats,
        }
        tmp = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, out_path)
        print(f"\nSAVED={out_path.relative_to(ROOT)} ({out_path.stat().st_size} bytes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
