#!/usr/bin/env python3
"""Offline forensic tape audit & 19-architecture structural benchmark on real recorded tick JSONL files (ORDERS=0)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trade_mcp.s1_impulse_audit import (  # noqa: E402
    analyze_tape_physics,
    combine_portfolio_waves,
    load_tick_stream,
    run_structural_benchmark,
)


def _print_table(title: str, rows: list[dict[str, Any]]) -> None:
    print(title)
    print(
        f"{'STRATEGY':18s} | {'WAVES':>5s} | {'LEGS':>4s} | {'MaxLot':>6s} | {'W/L':>7s} | {'WR%':>5s} | "
        f"{'NET $':>8s} | {'PF':>5s} | {'MaxDD':>6s} | {'AvgW':>7s} | {'AvgL':>7s}"
    )
    print("-" * 105)
    for r in rows:
        pf_s = f"{r['pf']:.2f}" if r["pf"] is not None else "  -  "
        wl_s = f"{r['wins']}/{r['losses']}"
        mlot = float(r.get("max_lot", 0.0))
        print(
            f"{r['strategy']:18s} | {r['waves_count']:5d} | {r['total_legs']:4d} | {mlot:6.2f} | "
            f"{wl_s:>7s} | {r['wr_pct']:5.1f} | {r['net_usd']:+8.2f} | "
            f"{pf_s:>5s} | {r['max_dd_usd']:6.2f} | {r['avg_win']:+7.2f} | {r['avg_loss']:+7.2f}"
        )


def _combine_rows(per_file: list[tuple[Path, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    names = [r["strategy"] for r in per_file[0][1]]
    combined: list[dict[str, Any]] = []
    for idx, name in enumerate(names):
        all_waves = [w for _, rows in per_file for w in rows[idx]["waves"]]
        wins = [w["net_pnl_usd"] for w in all_waves if w["net_pnl_usd"] > 0]
        losses = [w["net_pnl_usd"] for w in all_waves if w["net_pnl_usd"] <= 0]
        gw = sum(wins)
        gl = abs(sum(losses))
        pf = round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else None)
        max_lot = round(max((float(w.get("total_volume", 0.0)) for w in all_waves), default=0.0), 2)
        eq = 0.0
        pk = 0.0
        mdd = 0.0
        for w in all_waves:
            eq += w["net_pnl_usd"]
            if eq > pk:
                pk = eq
            mdd = max(mdd, pk - eq)
        combined.append({
            "strategy": name,
            "waves_count": len(all_waves),
            "total_legs": sum(int(w["legs_count"]) for w in all_waves),
            "max_lot": max_lot,
            "wins": len(wins),
            "losses": len(losses),
            "wr_pct": round(100.0 * len(wins) / len(all_waves), 1) if all_waves else 0.0,
            "net_usd": round(sum(w["net_pnl_usd"] for w in all_waves), 2),
            "pf": pf,
            "max_dd_usd": round(mdd, 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "waves": all_waves,
        })
    return combined


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forensic impulse physics & structural strategy audit (ORDERS=0)")
    parser.add_argument("files", nargs="+", help="Recorded logs/s1_ticks_*.jsonl files to analyze")
    parser.add_argument("--rebuild-m1", action="store_true", help="Rebuild causal M1 context when missing")
    parser.add_argument("--leg-lot", type=float, default=0.01, help="Lot per pyramid leg (default 0.01)")
    parser.add_argument("--max-legs", type=int, default=6, help="Max legs per wave (default 6)")
    parser.add_argument("--show-waves", default="", help="Print detailed waves for a specific strategy name")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    paths: list[Path] = []
    for raw in args.files:
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT / p
        paths.append(p)

    per_file: list[tuple[Path, list[dict[str, Any]]]] = []
    total_ticks = 0

    print("=== PART 1: TAPE PHYSICS & CLEAN >=$8 REAL-WAVE CATALOG (DIAGNOSTIC) ===")
    for p in paths:
        ticks = load_tick_stream(p, rebuild_m1=args.rebuild_m1)
        total_ticks += len(ticks)
        phys = analyze_tape_physics(ticks)
        cw = phys["clean_waves"]
        print(
            f"\n[TAPE] {p.name} | ticks={phys['ticks']} ({phys['duration_min']:.1f}m) | "
            f"range={phys['price_low']:.2f}..{phys['price_high']:.2f} | "
            f"max|60s|=${phys['max_abs_60s']:.2f} | "
            f"time(|60s|>=$6:{phys['pct_60s_ge_6']:.1f}%, >=$8:{phys['pct_60s_ge_8']:.1f}%, >=$10:{phys['pct_60s_ge_10']:.1f}%) | "
            f"clean_$8_waves={len(cw)}"
        )
        for w in cw[:8]:
            print(
                f"  [WAVE] {w['start_utc']} {w['side']:4s} ${w['move_usd']:+6.2f} in {w['dur_sec']:3d}s "
                f"({w['start_px']:.2f}->{w['end_px']:.2f}) | pre(m60={w['pre_m60']:+6.2f}, "
                f"e60={w['pre_e60']:.2f}, upd10={w['pre_upd10']:2d}, brk15={w['pre_brk15']:+d})"
            )
        if len(cw) > 8:
            print(f"  ... (+{len(cw) - 8} more clean >=$8 waves)")

        rows = run_structural_benchmark(ticks, leg_lot=args.leg_lot, max_legs=args.max_legs)
        per_file.append((p, rows))

    print("\n=== PART 2: CAUSAL 19-ARCHITECTURE BENCHMARK PER FILE (ORDERS=0) ===")
    for p, rows in per_file:
        by_name = {r["strategy"]: r for r in rows}
        file_rows = list(rows)
        if "SHOCK_10_RUNNER" in by_name and "PB_DIP_V2_SESS" in by_name:
            file_rows.append(
                combine_portfolio_waves(
                    "COMBO_SHK10_PBV2",
                    [by_name["SHOCK_10_RUNNER"]["waves"], by_name["PB_DIP_V2_SESS"]["waves"]],
                )
            )
        if "SHOCK10_DYN_RUN" in by_name and "PB_V2_DYN_SESS" in by_name:
            file_rows.append(
                combine_portfolio_waves(
                    "COMBO_DYN_SUITE",
                    [by_name["SHOCK10_DYN_RUN"]["waves"], by_name["PB_V2_DYN_SESS"]["waves"]],
                )
            )
        if "SHOCK10_DYN_RUN" in by_name and "MACRO_WAVE_DYN" in by_name:
            file_rows.append(
                combine_portfolio_waves(
                    "COMBO_DYN_MACRO",
                    [by_name["SHOCK10_DYN_RUN"]["waves"], by_name["MACRO_WAVE_DYN"]["waves"]],
                )
            )
        _print_table(f"\n--- FILE: {p.name} ---", file_rows)

    if len(per_file) >= 2:
        combined = _combine_rows(per_file)
        c_by_name = {r["strategy"]: r for r in combined}
        if "SHOCK_10_RUNNER" in c_by_name and "PB_DIP_V2_SESS" in c_by_name:
            combined.append(
                combine_portfolio_waves(
                    "COMBO_SHK10_PBV2",
                    [c_by_name["SHOCK_10_RUNNER"]["waves"], c_by_name["PB_DIP_V2_SESS"]["waves"]],
                )
            )
        if "SHOCK10_DYN_RUN" in c_by_name and "PB_V2_DYN_SESS" in c_by_name:
            combined.append(
                combine_portfolio_waves(
                    "COMBO_DYN_SUITE",
                    [c_by_name["SHOCK10_DYN_RUN"]["waves"], c_by_name["PB_V2_DYN_SESS"]["waves"]],
                )
            )
        if "SHOCK10_DYN_RUN" in c_by_name and "MACRO_WAVE_DYN" in c_by_name:
            combined.append(
                combine_portfolio_waves(
                    "COMBO_DYN_MACRO",
                    [c_by_name["SHOCK10_DYN_RUN"]["waves"], c_by_name["MACRO_WAVE_DYN"]["waves"]],
                )
            )
        _print_table(f"\n=== COMBINED ALL {len(per_file)} FILES ({total_ticks} TICKS, ORDERS=0) ===", combined)

    if args.show_waves:
        for p, rows in per_file:
            match = [r for r in rows if r["strategy"] == args.show_waves]
            if match and match[0]["waves"]:
                r0 = match[0]
                print(f"\n--- {args.show_waves} WAVES ON {p.name} (NET=${r0['net_usd']:+.2f}) ---")
                for w in r0["waves"]:
                    print(
                        f"  #{w['wave_id']:2d} {str(w['opened_utc'])[11:19]} [{w['regime']:13s}] "
                        f"{w['side'].upper():4s} {w['legs_count']}L/{w['total_volume']:.2f}lot "
                        f"{w['avg_entry']:.2f}->{w['exit_price']:.2f} "
                        f"PnL=${w['net_pnl_usd']:+7.2f} hold={w['hold_seconds']:5.1f}s ({w['exit_reason']} | {w['m1_label']})"
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
