#!/usr/bin/env python3
"""Inspect live paper windows, trade logs, and second-by-second tick chart timeline (read-only, ORDERS=0).

Produces a compact human-readable audit of:
  1. All live JSON snapshots in reports/paper_live/*.json
  2. All recorded trade/wave/alert logs in logs/*.jsonl
  3. Exact 15-minute OHLC + volatility + >=$5 swing timeline rebuilt from logs/s1_ticks_*.jsonl

Optionally writes a compact (<30 KB) git-safe JSON summary to reports/paper_live/live_audit_summary.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trade_mcp.s1_impulse_audit import load_tick_stream  # noqa: E402


def _iso_hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def summarize_tick_chart(path: Path) -> dict[str, Any]:
    """Build 15-minute chart buckets + >=$5 swing catalog directly from raw tick-by-second JSONL."""
    ticks = load_tick_stream(path, rebuild_m1=False)
    if not ticks:
        return {"file": path.name, "ticks": 0, "buckets_15m": [], "swings_5usd": []}

    t0 = ticks[0][0]
    t1 = ticks[-1][0]
    dur_min = round((t1 - t0) / 60.0, 1)

    mids: list[float] = []
    spreads: list[float] = []
    sec_hist: deque[tuple[int, float]] = deque(maxlen=400)
    cur_sec: tuple[int, float] | None = None

    # 15-minute chart buckets + 1-minute ATR tracker
    buckets: dict[int, dict[str, Any]] = {}
    m1_bars: dict[int, list[float]] = {}  # bar_id -> [h, l]
    max_abs_60s_all = 0.0
    swings: list[dict[str, Any]] = []
    last_swing_end_ts = 0.0

    # Track 5-minute rolling extremes for >=$5 swings
    win_5m: deque[tuple[float, float]] = deque()

    for ts, bid, ask, _ in ticks:
        mid = 0.5 * (bid + ask)
        sp = ask - bid
        mids.append(mid)
        spreads.append(sp)
        sec = int(ts)

        if cur_sec is None:
            cur_sec = (sec, mid)
        elif sec > cur_sec[0]:
            sec_hist.append(cur_sec)
            while sec_hist and (sec - sec_hist[0][0]) > 320:
                sec_hist.popleft()
            cur_sec = (sec, mid)
        else:
            cur_sec = (sec, mid)

        # Causal 60s move
        m60 = 0.0
        t_cut60 = sec - 60
        for s_ts, s_mid in sec_hist:
            if s_ts >= t_cut60:
                m60 = mid - s_mid
                break
        if abs(m60) > max_abs_60s_all:
            max_abs_60s_all = abs(m60)

        # 1m bar H/L
        m1_id = int(ts // 60)
        if m1_id not in m1_bars:
            m1_bars[m1_id] = [mid, mid]
        else:
            if mid > m1_bars[m1_id][0]:
                m1_bars[m1_id][0] = mid
            if mid < m1_bars[m1_id][1]:
                m1_bars[m1_id][1] = mid

        # 15m bucket
        b15 = int(ts // 900) * 900
        if b15 not in buckets:
            buckets[b15] = {
                "start_ts": b15,
                "start_hm": _hm(b15),
                "o": mid,
                "h": mid,
                "l": mid,
                "c": mid,
                "ticks": 1,
                "max_60s": abs(m60),
                "sp_sum": sp,
                "m1_ids": {m1_id},
            }
        else:
            bk = buckets[b15]
            bk["h"] = max(bk["h"], mid)
            bk["l"] = min(bk["l"], mid)
            bk["c"] = mid
            bk["ticks"] += 1
            bk["max_60s"] = max(bk["max_60s"], abs(m60))
            bk["sp_sum"] += sp
            bk["m1_ids"].add(m1_id)

        # 5m swing detector (non-overlapping >=$5 moves within 5 minutes)
        win_5m.append((ts, mid))
        while win_5m and (ts - win_5m[0][0]) > 300.0:
            win_5m.popleft()
        if ts - last_swing_end_ts >= 180.0 and len(win_5m) >= 20:
            lo_ts, lo_px = min(win_5m, key=lambda x: x[1])
            hi_ts, hi_px = max(win_5m, key=lambda x: x[1])
            if (mid - lo_px) >= 5.0 and (ts - lo_ts) >= 15.0:
                swings.append({
                    "time": _iso_hms(lo_ts)[11:19],
                    "side": "BUY",
                    "move_usd": round(mid - lo_px, 2),
                    "dur_sec": int(ts - lo_ts),
                    "from_px": round(lo_px, 2),
                    "to_px": round(mid, 2),
                    "m60": round(m60, 2),
                })
                last_swing_end_ts = ts
            elif (hi_px - mid) >= 5.0 and (ts - hi_ts) >= 15.0:
                swings.append({
                    "time": _iso_hms(hi_ts)[11:19],
                    "side": "SELL",
                    "move_usd": round(hi_px - mid, 2),
                    "dur_sec": int(ts - hi_ts),
                    "from_px": round(hi_px, 2),
                    "to_px": round(mid, 2),
                    "m60": round(m60, 2),
                })
                last_swing_end_ts = ts

    bucket_rows: list[dict[str, Any]] = []
    for b15 in sorted(buckets.keys()):
        bk = buckets[b15]
        m1_ranges = [m1_bars[i][0] - m1_bars[i][1] for i in bk["m1_ids"] if i in m1_bars]
        avg_m1_rng = round(sum(m1_ranges) / len(m1_ranges), 2) if m1_ranges else 0.0
        bucket_rows.append({
            "hm": bk["start_hm"],
            "o": round(bk["o"], 2),
            "h": round(bk["h"], 2),
            "l": round(bk["l"], 2),
            "c": round(bk["c"], 2),
            "delta": round(bk["c"] - bk["o"], 2),
            "range": round(bk["h"] - bk["l"], 2),
            "max_60s": round(bk["max_60s"], 2),
            "avg_m1_rng": avg_m1_rng,
            "avg_sp": round(bk["sp_sum"] / max(1, bk["ticks"]), 2),
            "ticks": bk["ticks"],
        })

    return {
        "file": path.name,
        "ticks": len(ticks),
        "start_srv": _iso_hms(t0),
        "end_srv": _iso_hms(t1),
        "duration_min": dur_min,
        "low": round(min(mids), 2),
        "high": round(max(mids), 2),
        "range": round(max(mids) - min(mids), 2),
        "avg_spread": round(sum(spreads) / len(spreads), 3),
        "max_spread": round(max(spreads), 3),
        "max_abs_60s": round(max_abs_60s_all, 2),
        "buckets_15m": bucket_rows,
        "swings_5usd": swings,
    }


def summarize_live_reports(reports_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not reports_dir.exists():
        return out
    for p in sorted(reports_dir.glob("*.json")):
        if p.name == "live_audit_summary.json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        schema = str(data.get("schema", "unknown"))
        live_st = data.get("live_state", {}) if isinstance(data.get("live_state"), dict) else {}
        row: dict[str, Any] = {
            "file": p.name,
            "schema": schema,
            "status": data.get("status"),
            "orders_submitted": data.get("orders_submitted"),
            "started_utc": data.get("started_utc"),
            "updated_utc": data.get("updated_utc"),
            "elapsed_min": data.get("elapsed_minutes"),
            "ticks": live_st.get("ticks_processed", data.get("ticks_processed")),
            "reconnects": live_st.get("reconnect_count", data.get("reconnect_count")),
            "errors": live_st.get("consecutive_errors", data.get("consecutive_errors")),
        }
        if "account" in data and isinstance(data["account"], dict):
            a = data["account"]
            row["profit_usd"] = a.get("net_profit_usd")
            row["trades"] = a.get("trades_count")
            row["wl"] = f"{a.get('wins', 0)}/{a.get('losses', 0)}"
            row["max_dd"] = a.get("max_dd_usd")
            row["open_count"] = a.get("open_positions_count")
        elif "engines" in data and isinstance(data["engines"], dict):
            eng_brief: dict[str, Any] = {}
            for k, v in data["engines"].items():
                if isinstance(v, dict):
                    eng_brief[k] = {
                        "waves": v.get("waves_count", 0),
                        "wl": f"{v.get('wins', 0)}/{v.get('losses', 0)}",
                        "pnl": v.get("realized_pnl_usd", 0.0),
                    }
            row["engines"] = eng_brief
            if "combined_dynamic" in data and isinstance(data["combined_dynamic"], dict):
                row["combined_dynamic"] = {
                    "waves": data["combined_dynamic"].get("waves_count", 0),
                    "net": data["combined_dynamic"].get("net_usd", 0.0),
                }
        elif "summary" in data and isinstance(data["summary"], dict):
            s = data["summary"]
            row["profit_usd"] = s.get("realized_pnl_usd")
            row["trades"] = s.get("waves_count")
            row["wl"] = f"{s.get('wins', 0)}/{s.get('losses', 0)}"
            row["max_dd"] = s.get("max_dd_usd")
        out.append(row)
    return out


def summarize_trade_logs(logs_dir: Path) -> list[dict[str, Any]]:
    patterns = (
        "all_weather_paper_trades.jsonl",
        "s1_dual_radar_waves.jsonl",
        "s1_velocity_paper_waves.jsonl",
        "shock_radar_alerts.jsonl",
    )
    out: list[dict[str, Any]] = []
    for name in patterns:
        p = logs_dir / name
        if not p.exists():
            continue
        rows = _read_jsonl_rows(p)
        pnls = [float(r["net_pnl_usd"]) for r in rows if "net_pnl_usd" in r]
        out.append({
            "file": name,
            "count": len(rows),
            "net_pnl_usd": round(sum(pnls), 2) if pnls else None,
            "wins": sum(1 for x in pnls if x > 0) if pnls else None,
            "losses": sum(1 for x in pnls if x <= 0) if pnls else None,
            "last_rows": rows[-10:],
        })
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit live paper windows & tick-by-second chart timeline (ORDERS=0)")
    parser.add_argument("--tick-files", nargs="*", default=[], help="Specific tick JSONL files (default: latest 3)")
    parser.add_argument("--latest-ticks", type=int, default=3, help="How many latest tick files to chart (default 3)")
    parser.add_argument("--save", action="store_true", help="Write compact git-safe JSON to reports/paper_live/live_audit_summary.json")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    reports_dir = ROOT / "reports" / "paper_live"
    logs_dir = ROOT / "logs"

    rep_rows = summarize_live_reports(reports_dir)
    log_rows = summarize_trade_logs(logs_dir)

    if args.tick_files:
        tick_paths = [Path(x) if Path(x).is_absolute() else (ROOT / x) for x in args.tick_files]
    else:
        all_ticks = sorted(logs_dir.glob("s1_ticks_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        tick_paths = list(reversed(all_ticks[: max(1, args.latest_ticks)]))

    tick_charts = [summarize_tick_chart(p) for p in tick_paths if p.exists()]

    print("=== 1) LIVE PAPER WINDOWS (reports/paper_live/*.json) ===")
    if not rep_rows:
        print("  (No JSON report files found in reports/paper_live/)")
    for r in rep_rows:
        print(
            f"  • {r['file']} | schema={r['schema']} | status={r['status']} | "
            f"start={r['started_utc']} -> upd={r['updated_utc']} ({r['elapsed_min']}m) | "
            f"ticks={r['ticks']} err={r['errors']} rec={r['reconnects']} | ORDERS={r['orders_submitted']}"
        )
        if "profit_usd" in r:
            print(
                f"      Profit=${float(r['profit_usd'] or 0):+.2f} | Trades={r['trades']} ({r['wl']}) | MaxDD=${r['max_dd']}"
            )
        if "engines" in r:
            brief = " | ".join(
                f"{k}:{v['wl']}(${float(v['pnl']):+.2f})" for k, v in r["engines"].items()
            )
            print(f"      Engines: {brief}")

    print("\n=== 2) RECORDED TRADE & ALERT LOGS (logs/*.jsonl) ===")
    if not log_rows:
        print("  (No trade/alert JSONL files found in logs/)")
    for lr in log_rows:
        if lr["net_pnl_usd"] is not None:
            print(f"  • {lr['file']}: {lr['count']} rows | {lr['wins']}W/{lr['losses']}L | NET=${lr['net_pnl_usd']:+.2f}")
        else:
            print(f"  • {lr['file']}: {lr['count']} alerts")
        for row in lr["last_rows"][-5:]:
            t_s = str(row.get("closed_utc") or row.get("server_time") or "")[11:19]
            if "net_pnl_usd" in row:
                eng = row.get("engine_label") or row.get("engine") or row.get("regime") or ""
                print(
                    f"      {t_s} [{eng}] {str(row.get('side', '')).upper():4s} "
                    f"{float(row.get('total_volume', 0.0)):.2f}lot "
                    f"{float(row.get('avg_entry', 0.0)):.2f}->{float(row.get('exit_price', 0.0)):.2f} "
                    f"PnL=${float(row['net_pnl_usd']):+.2f} ({row.get('exit_reason', '')})"
                )
            else:
                print(
                    f"      {t_s} [{row.get('tier', '')}] {row.get('side', '')} "
                    f"60s={row.get('move_60s')} eff={row.get('eff_60s')} @ {row.get('bid')}"
                )

    print("\n=== 3) TICK-BY-SECOND CHART TIMELINE (15-MIN BUCKETS + >=$5 SWINGS) ===")
    for tc in tick_charts:
        print(
            f"\n[CHART] {tc['file']} | {tc['start_srv']} -> {tc['end_srv']} ({tc['duration_min']}m, {tc['ticks']} ticks)\n"
            f"        Range: {tc['low']:.2f}..{tc['high']:.2f} (${tc['range']:.2f}) | "
            f"AvgSp=${tc['avg_spread']:.2f} (Max=${tc['max_spread']:.2f}) | Max|60s|=${tc['max_abs_60s']:.2f}"
        )
        print("  TIME  |    OPEN ->   CLOSE (  Δ$  ) |  LOW ..  HIGH  (RNG$ ) | Max60s | M1Rng |  Sp  | Ticks")
        print("  " + "-" * 92)
        for bk in tc["buckets_15m"]:
            print(
                f"  {bk['hm']} | {bk['o']:7.2f} -> {bk['c']:7.2f} ({bk['delta']:+6.2f}) | "
                f"{bk['l']:7.2f}..{bk['h']:7.2f} ({bk['range']:5.2f}) | "
                f"{bk['max_60s']:6.2f} | {bk['avg_m1_rng']:5.2f} | {bk['avg_sp']:4.2f} | {bk['ticks']:5d}"
            )
        if tc["swings_5usd"]:
            print(f"  -- Real >=$5 Swings ({len(tc['swings_5usd'])}) --")
            for sw in tc["swings_5usd"][-15:]:
                print(
                    f"    {sw['time']} {sw['side']:4s} ${sw['move_usd']:+5.2f} in {sw['dur_sec']:3d}s "
                    f"({sw['from_px']:.2f}->{sw['to_px']:.2f}) | m60={sw['m60']:+5.2f}"
                )
        else:
            print("  -- Zero >=$5 swings in 5m window --")

    if args.save:
        out_path = reports_dir / "live_audit_summary.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "live_audit_summary_v1",
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "orders_submitted": 0,
            "live_reports": rep_rows,
            "trade_logs": log_rows,
            "tick_charts": tick_charts,
        }
        tmp = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, out_path)
        print(f"\nSAVED_COMPACT_AUDIT={out_path.relative_to(ROOT)} ({out_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
