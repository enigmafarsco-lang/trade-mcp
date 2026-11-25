#!/usr/bin/env python3
"""Strictly read-only S1 Velocity Surfer Live Paper Scalper + Sub-Second Tick Recorder (ORDERS=0).

Models the exact dual-chart tape scalping workflow:
  - Chart 1 (M1): trend direction, Swing Support/Resistance, and pullback-resume bias.
  - Chart 2 (S1 / 0.25s-0.5s live ticks): adaptive eye-speed ratio (>= 2.5x 90s baseline),
    1-second Winner-Only rapid-fire machine gun (Legs 1..6 ONLY while prior leg is in profit),
    automatic Leg-1 BE & basket profit-floor lock, and instant Deceleration Exit when speed fades.

Features:
  - Strictly virtual / read-only (`ORDERS=0`, `TRADE_MCP_ALLOW_TRADING=false`).
  - Automatically records every live Bid/Ask tick + M1 context to `logs/s1_ticks_*.jsonl` (0600)
    so you can scientifically replay & tune thresholds offline (`--replay <file.jsonl>`).
  - Realistic 250ms execution latency and live spread cap guard.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_m1_paper_window import ReadOnlyNativeFeed  # noqa: E402
from daytrade_mt5_readonly_check import Blocked, compact_bars, loopback_native, timestamp  # noqa: E402

from trade_mcp.bridge.native import McpError, find, to_epoch  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.daytrade_paper import resolve_pricing_basis  # noqa: E402
from trade_mcp.s1_velocity_surfer import (  # noqa: E402
    S1SurferConfig,
    S1VelocitySurferEngine,
    analyze_m1_sr_context,
    append_jsonl_private,
    make_synthetic_s1_session,
    replay_calibration_sweep,
    replay_recorded_ticks,
)


def _ensure_in_logs(path: Path) -> Path:
    logs_dir = (ROOT / "logs").resolve()
    resolved = path.resolve()
    if resolved.parent != logs_dir:
        raise ValueError(f"Output path must sit directly inside logs/: {path}")
    return resolved


def _server_now(feed: ReadOnlyNativeFeed) -> tuple[float, datetime]:
    clock = feed._call("get_time_information", {})
    raw_time = find(clock, "trade_server_time") or find(clock, "trade_server_last_known_time")
    dt = timestamp(raw_time)
    if dt is None:
        raise Blocked("timestamp_unverified")
    # CRITICAL: use to_epoch(raw_time) — NOT dt.timestamp() — so naive broker wall-clock times
    # stay in the exact same epoch reference as compact_bars() regardless of host OS timezone!
    return float(to_epoch(raw_time)), dt.replace(tzinfo=None)


def _fetch_m1_bars(feed: ReadOnlyNativeFeed, *, now_label: datetime, limit: int = 200) -> list[list[float]]:
    start = now_label - timedelta(hours=3)
    end = now_label + timedelta(minutes=2)
    body = feed._call(
        "get_chart_history",
        {
            "symbol": feed.symbol,
            "period": "M1",
            "datetime_from": start.isoformat(timespec="seconds"),
            "datetime_to": end.isoformat(timespec="seconds"),
            "limit": limit,
        },
    )
    bars, _ = compact_bars(body)
    return bars


def _print_event(ev: dict[str, Any]) -> None:
    et = ev.get("type")
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if et == "OPEN_L1":
        print(
            f"[{now_str}] [S1 OPEN L1 | {ev.get('regime')} C{ev.get('candle_num')}] #{ev['wave_id']} "
            f"{ev['side'].upper()} @ {ev['entry']:.2f} | vel={ev['vel_ratio']:.1f}x slope={ev.get('slope_mult', 0):.1f}x "
            f"({ev['move_3s']:+.2f}/3s) | M1={ev['m1_label']} | SL={ev['stop']:.2f} (sp={ev['spread']:.2f})",
            flush=True,
        )
    elif et == "C2_CONFIRMED":
        tag = "EXTREME_BLOWOFF" if ev.get("extreme_blowoff") else "GOOD_SLOPE"
        print(
            f"[{now_str}] [S1 C2 CONFIRMED -> C3 UNLOCKED] #{ev['wave_id']} {ev['side'].upper()} "
            f"c2_body={ev['c2_body']:+.2f} ({tag})",
            flush=True,
        )
    elif et == "FIRE_ADD":
        print(
            f"[{now_str}] [S1 FIRE +L{ev['leg_num']} | C{ev.get('candle_num')}] #{ev['wave_id']} {ev['side'].upper()} "
            f"@ {ev['add_price']:.2f} (prev +${ev['last_leg_net']:.2f}) -> {ev['total_volume']:.2f} lot avg={ev['avg_entry']:.2f} "
            f"| SL locked @ {ev['stop']:.2f} (vel={ev['vel_ratio']:.1f}x)",
            flush=True,
        )
    elif et == "CLOSE_ALL":
        w = ev["wave"]
        print(
            f"[{now_str}] [S1 CLOSE ALL | {w.get('regime')}] #{w['wave_id']} {w['side'].upper()} "
            f"{w['legs_count']}L/{w['total_volume']:.2f}lot {w['avg_entry']:.2f}->{w['exit_price']:.2f} "
            f"| PnL=${w['net_pnl_usd']:+.2f} ({w['exit_reason']}, hold={w['hold_seconds']:.1f}s, C{w.get('s1_candles_elapsed')}) "
            f"| Eq=${w['equity_after_usd']:.2f}",
            flush=True,
        )


def _print_status(eng: S1VelocitySurferEngine, m1_ctx: dict[str, Any], bid: float, ask: float) -> None:
    s = eng.summary(bid, ask)
    m = s["last_metrics"]
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
    b = s["open_basket"]
    if b:
        pos_s = f"{b['side'].upper()} {b['regime']} C{b['wave_candle_num']} {len(b['legs'])}L/{b['total_volume']:.2f}@{b['avg_entry']:.2f}(SL {b['stop']:.2f})"
    else:
        pos_s = "FLAT"
    print(
        f"[{now_str}] [S1 SURFER | ORDERS=0] {bid:.2f}/{ask:.2f} (sp={m.get('spread', 0):.2f}) | "
        f"S1(streak={m.get('s1_streak', 0):+d} slope={m.get('s1_slope', 0):.1f}x vel={m.get('vel_ratio', 0):.1f}x 60s={m.get('move_60s', 0):+.2f}) | "
        f"M1={m1_ctx.get('label')} (S={m1_ctx.get('support')} R={m1_ctx.get('resistance')}) | "
        f"Pos=[{pos_s}] | Waves={s['waves_count']} ({s['total_legs_executed']}L, {s['wins']}W/{s['losses']}L) "
        f"Real=${s['realized_pnl_usd']:+.2f} Unr=${s['unrealized_pnl_usd']:+.2f}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only S1 Velocity Surfer Paper Scalper + Sub-Second Tick Recorder.")
    parser.add_argument("--symbol", default="XAUUSD_i")
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--leg-lot", type=float, default=0.01, help="Lot per winner-pyramid leg (0.01..0.05, default 0.01)")
    parser.add_argument("--max-legs", type=int, default=6, help="Max winner-only legs per wave (1..6, default 6)")
    parser.add_argument("--vel-mult", type=float, default=2.5, help="Eye-speed multiplier vs 90s baseline (default 2.5)")
    parser.add_argument("--min-burst", type=float, default=0.90, help="Min 3-second $ burst for Leg 1 (default 0.90)")
    parser.add_argument("--min-60s-move", type=float, default=0.0, help="Ripping-tape gate: require >= $X directional 60s move (0=off)")
    parser.add_argument("--max-spread", type=float, default=0.90, help="Max allowed live spread $ (default 0.90)")
    parser.add_argument("--add-interval", type=float, default=0.60, help="Min seconds between winner legs (default 0.60)")
    parser.add_argument("--add-profit", type=float, default=0.20, help="Min net $ move on last leg before adding (default 0.20)")
    parser.add_argument("--decel-ratio", type=float, default=0.28, help="Exit when 2s speed <= this fraction of peak (default 0.28)")
    parser.add_argument("--poll-seconds", type=float, default=0.30, help="Live tick poll interval seconds (default 0.30)")
    parser.add_argument("--status-seconds", type=float, default=15.0, help="Heartbeat print interval seconds (default 15)")
    parser.add_argument("--minutes", type=float, default=0.0, help="Run duration minutes (0 = until Ctrl+C)")
    parser.add_argument("--no-record", action="store_true", help="Disable recording ticks to logs/s1_ticks_*.jsonl")
    parser.add_argument("--replay", default="", help="Replay a recorded logs/s1_ticks_*.jsonl file offline")
    parser.add_argument("--replay-all", action="store_true", help="Show ALL waves in --replay (no 80-wave cap)")
    parser.add_argument("--replay-sweep", nargs="+", default=[], help="Compare S1 calibration profiles across 1 or more recorded tick JSONL files")
    parser.add_argument("--show-profile", default="", help="Print per-file wave list for a specific profile during --replay-sweep")
    parser.add_argument("--rebuild-m1", action="store_true", help="Rebuild causal M1 context from recorded ticks during --replay")
    parser.add_argument("--wave-rider", action="store_true", help="Enable v2.3 breath-proof Wave-Rider exits (no 1.8s panic decel cut, wider trail & SL breathing room)")
    parser.add_argument("--safe-hybrid", action="store_true", help="Enable v2.4 Safe-Pyramid Hybrid (fast 1L loss cut, BE arm, risk-free pyramid gate, capped pullback trail)")
    parser.add_argument("--sl-breath", type=float, default=None, help="Minimum $ breathing room between locked SL and current bid/ask")
    parser.add_argument("--mock", action="store_true", help="Run deterministic offline synthetic waterfall verification")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    leg_lot = max(0.01, min(0.05, round(args.leg_lot, 2)))
    max_legs = max(1, min(6, int(args.max_legs)))

    if args.safe_hybrid:
        cfg = S1SurferConfig(
            leg_lot=leg_lot,
            max_legs=max_legs,
            vel_mult=args.vel_mult,
            min_burst_price=args.min_burst,
            min_60s_move_price=args.min_60s_move,
            max_spread_price=args.max_spread,
            add_interval_sec=max(2.0, args.add_interval),
            add_min_profit_price=max(1.00, args.add_profit),
            min_stop_price=1.60,
            max_stop_price=2.40,
            decel_loss_cut_sec=3.0,
            disable_decel_exit=True,
            decel_only_in_loss_1l=True,
            candle_cap_adds=False,
            sl_min_breathing_room=1.10 if args.sl_breath is None else max(0.0, args.sl_breath),
            be_arm_price=1.40,
            require_be_on_add=True,
            s1_reversal_cut_body=0.0,
            trail_arm_price=1.80,
            trail_min_pullback=1.20,
            trail_max_pullback_price=2.80,
            trail_giveback_frac=0.32,
            multi_leg_giveback_frac=0.18,
            max_hold_sec=240.0,
            loss_cooldown_sec=30.0,
            consec_loss_cooldown_sec=60.0,
        )
    elif args.wave_rider:
        cfg = S1SurferConfig(
            leg_lot=leg_lot,
            max_legs=max_legs,
            vel_mult=args.vel_mult,
            min_burst_price=args.min_burst,
            min_60s_move_price=args.min_60s_move,
            max_spread_price=args.max_spread,
            add_interval_sec=max(2.5, args.add_interval),
            add_min_profit_price=max(1.00, args.add_profit),
            min_stop_price=2.50,
            max_stop_price=3.80,
            disable_decel_exit=True,
            candle_cap_adds=False,
            sl_min_breathing_room=1.40 if args.sl_breath is None else max(0.0, args.sl_breath),
            s1_reversal_cut_body=0.0,
            trail_arm_price=2.00,
            trail_min_pullback=1.60,
            trail_giveback_frac=0.32,
            max_hold_sec=240.0,
            loss_cooldown_sec=30.0,
            consec_loss_cooldown_sec=60.0,
        )
    else:
        cfg = S1SurferConfig(
            leg_lot=leg_lot,
            max_legs=max_legs,
            vel_mult=args.vel_mult,
            min_burst_price=args.min_burst,
            min_60s_move_price=args.min_60s_move,
            max_spread_price=args.max_spread,
            add_interval_sec=args.add_interval,
            add_min_profit_price=args.add_profit,
            decel_ratio=args.decel_ratio,
            sl_min_breathing_room=0.0 if args.sl_breath is None else max(0.0, args.sl_breath),
        )

    def _print_waves(waves: list[dict[str, Any]], *, show_all: bool = False) -> None:
        subset = waves if (show_all or len(waves) <= 80) else waves[-80:]
        for w in subset:
            t_hms = str(w.get("opened_utc", ""))[11:19]
            print(
                f"  #{w['wave_id']:2d} {t_hms} [{w.get('regime', 'S1'):13s}] {w['side'].upper():4s} "
                f"{w['legs_count']}L/{w['total_volume']:.2f}lot {w['avg_entry']:.2f}->{w['exit_price']:.2f} "
                f"PnL=${w['net_pnl_usd']:+6.2f} hold={w['hold_seconds']:4.1f}s C{w.get('s1_candles_elapsed', 0):2d} "
                f"({w['exit_reason']} | {w.get('m1_label', '')})"
            )

    if args.replay_sweep:
        sweep_paths: list[Path] = []
        for raw_p in args.replay_sweep:
            p = Path(raw_p)
            if not p.is_absolute():
                p = ROOT / p
            sweep_paths.append(p)

        per_file_rows: list[tuple[Path, list[dict[str, Any]]]] = []
        for p in sweep_paths:
            rows = replay_calibration_sweep(
                p,
                leg_lot=leg_lot,
                max_legs=max_legs,
                initial_capital_usd=args.capital,
                rebuild_m1=args.rebuild_m1,
            )
            per_file_rows.append((p, rows))

        def _print_sweep_table(title: str, rows: list[dict[str, Any]]) -> None:
            print(title)
            print(
                f"{'PROFILE':16s} | {'WAVES':>5s} | {'LEGS':>4s} | {'W/L':>7s} | {'WR%':>5s} | "
                f"{'NET $':>8s} | {'PF':>5s} | {'MaxDD':>6s} | {'AvgW':>6s} | {'AvgL':>6s}"
            )
            print("-" * 92)
            for r in rows:
                pf_s = f"{r['pf']:.2f}" if r["pf"] is not None else "  -  "
                wl_s = f"{r['wins']}/{r['losses']}"
                print(
                    f"{r['profile']:16s} | {r['waves_count']:5d} | {r['total_legs_executed']:4d} | "
                    f"{wl_s:>7s} | {r['win_rate_pct']:5.1f} | {r['realized_pnl_usd']:+8.2f} | "
                    f"{pf_s:>5s} | {r['max_dd_usd']:6.2f} | {r['avg_win_usd']:+6.2f} | {r['avg_loss_usd']:+6.2f}"
                )

        if len(per_file_rows) == 1:
            p0, rows0 = per_file_rows[0]
            _print_sweep_table(
                f"=== S1 VELOCITY CALIBRATION SWEEP (ORDERS=0) ===\nFILE={p0} TICKS={rows0[0]['ticks_replayed']}",
                rows0,
            )
            chosen = [r for r in rows0 if r["profile"] == args.show_profile] if args.show_profile else []
            best = chosen[0] if chosen else max(rows0, key=lambda x: (x["realized_pnl_usd"], -x["max_dd_usd"]))
            hdr = "PROFILE WAVES" if chosen else "BEST PROFILE WAVES"
            print(f"\n--- {hdr}: {best['profile']} (NET=${best['realized_pnl_usd']:+.2f}, DD=${best['max_dd_usd']:.2f}) ---")
            _print_waves(best["waves"], show_all=True)
        else:
            for p, rows in per_file_rows:
                _print_sweep_table(f"\n=== FILE: {p.name} (TICKS={rows[0]['ticks_replayed']}) ===", rows)
            # Combined multi-session aggregate across all recorded files
            prof_names = [r["profile"] for r in per_file_rows[0][1]]
            combined: list[dict[str, Any]] = []
            for idx, pname in enumerate(prof_names):
                all_waves = [w for _, rows in per_file_rows for w in rows[idx]["waves"]]
                wins = [w["net_pnl_usd"] for w in all_waves if w["net_pnl_usd"] > 0]
                losses = [w["net_pnl_usd"] for w in all_waves if w["net_pnl_usd"] <= 0]
                gw = sum(wins)
                gl = abs(sum(losses))
                pf = round(gw / gl, 3) if gl > 0 else (99.0 if gw > 0 else None)
                net = round(sum(w["net_pnl_usd"] for w in all_waves), 4)
                eq = 0.0
                pk = 0.0
                mdd = 0.0
                for w in all_waves:
                    eq += w["net_pnl_usd"]
                    if eq > pk:
                        pk = eq
                    mdd = max(mdd, pk - eq)
                combined.append({
                    "profile": pname,
                    "waves_count": len(all_waves),
                    "total_legs_executed": sum(int(w["legs_count"]) for w in all_waves),
                    "wins": len(wins),
                    "losses": len(losses),
                    "win_rate_pct": round(100.0 * len(wins) / len(all_waves), 1) if all_waves else 0.0,
                    "realized_pnl_usd": net,
                    "pf": pf,
                    "max_dd_usd": round(mdd, 2),
                    "avg_win_usd": round(sum(wins) / len(wins), 2) if wins else 0.0,
                    "avg_loss_usd": round(sum(losses) / len(losses), 2) if losses else 0.0,
                })
            tot_ticks = sum(rows[0]["ticks_replayed"] for _, rows in per_file_rows)
            _print_sweep_table(
                f"\n=== COMBINED S1 CALIBRATION SWEEP ({len(per_file_rows)} FILES, {tot_ticks} TICKS, ORDERS=0) ===",
                combined,
            )
            if args.show_profile:
                for p, rows in per_file_rows:
                    match = [r for r in rows if r["profile"] == args.show_profile]
                    if match:
                        r0 = match[0]
                        print(f"\n--- {args.show_profile} WAVES ON {p.name} (NET=${r0['realized_pnl_usd']:+.2f}, DD=${r0['max_dd_usd']:.2f}) ---")
                        _print_waves(r0["waves"], show_all=True)
        return 0

    if args.replay:
        rep_path = Path(args.replay)
        if not rep_path.is_absolute():
            rep_path = ROOT / rep_path
        res = replay_recorded_ticks(rep_path, cfg=cfg, initial_capital_usd=args.capital, rebuild_m1=args.rebuild_m1)
        rebuilt_note = f" | M1_REBUILT_FROM_TICKS={res['rebuilt_m1_ticks']}" if res.get("rebuilt_m1_ticks", 0) > 0 else ""
        print(
            f"=== S1 VELOCITY REPLAY (ORDERS=0) ===\n"
            f"FILE={rep_path} TICKS={res['ticks_replayed']} WAVES={res['waves_count']} ({res['total_legs_executed']}L, "
            f"{res['wins']}W/{res['losses']}L WR={res['win_rate_pct']}%) | NET=${res['realized_pnl_usd']:+.2f} "
            f"PF={res['pf']} DD=${res['max_dd_usd']:.2f} | blocked(sp={res['signals_blocked_spread']}, m1={res['signals_blocked_m1']}){rebuilt_note}"
        )
        _print_waves(res["waves"], show_all=args.replay_all)
        return 0

    if args.mock:
        m1_bars, ticks = make_synthetic_s1_session()
        m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
        eng = S1VelocitySurferEngine(cfg=cfg, initial_capital_usd=args.capital)
        for ts, bid, ask in ticks:
            for ev in eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx):
                _print_event(ev)
        s = eng.summary(ticks[-1][1], ticks[-1][2])
        print(
            f"OK=True ORDERS=0 MODE=mock_s1 WAVES={s['waves_count']} LEGS={s['total_legs_executed']} "
            f"NET=${s['realized_pnl_usd']:+.2f} DD=${s['max_dd_usd']:.2f}"
        )
        return 0

    stop_requested = False

    def _handle_sig(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tick_path = _ensure_in_logs(ROOT / "logs" / f"s1_ticks_{args.symbol}_{ts_tag}.jsonl")
    journal_path = _ensure_in_logs(ROOT / "logs" / "s1_surfer_waves.jsonl")

    try:
        load_dotenv(ROOT / ".env")
        app_cfg = Config.from_env()
        if app_cfg.backend != "native":
            raise Blocked("native_backend_required")

        with loopback_native():
            feed = ReadOnlyNativeFeed(app_cfg.mcp_url, app_cfg.mcp_token, args.symbol, timeout=15.0)
            spec, quote = feed.connect()
            _, mult = resolve_pricing_basis(spec, "auto")
            cfg.multiplier = float(mult)

            eng = S1VelocitySurferEngine(cfg=cfg, initial_capital_usd=args.capital)
            print(
                f"[START | S1 VELOCITY SURFER | PAPER ONLY | ORDERS=0] {args.symbol} | "
                f"Leg={leg_lot:.2f}x{max_legs} (max {leg_lot * max_legs:.2f} lot ONLY in profit) | "
                f"vel>={cfg.vel_mult}x burst>=${cfg.min_burst_price} maxSp=${cfg.max_spread_price} "
                f"decel<={cfg.decel_ratio:.0%} | poll={args.poll_seconds}s"
                + ("" if args.no_record else f" | REC={tick_path.name}"),
                flush=True,
            )

            t_start = time.monotonic()
            deadline = (t_start + args.minutes * 60.0) if args.minutes > 0 else None
            last_m1_mono = 0.0
            last_status_mono = 0.0
            m1_ctx: dict[str, Any] = {"bias": 0, "label": "WARMING_UP", "support": None, "resistance": None}
            last_bid, last_ask = quote["bid"], quote["ask"]
            ticks_recorded = 0

            # Keep server clock offset so we don't have to call get_time_information on every 0.3s tick
            srv_ts0, _ = _server_now(feed)
            clock_offset = srv_ts0 - time.time()

            tick_fh = None
            if not args.no_record:
                tick_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(tick_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                tick_fh = os.fdopen(fd, "a", encoding="utf-8")
                os.chmod(tick_path, 0o600)

            try:
                while not stop_requested:
                    loop_t0 = time.monotonic()
                    try:
                        if (loop_t0 - last_m1_mono) >= 15.0 or last_m1_mono == 0.0:
                            srv_ts, srv_dt = _server_now(feed)
                            clock_offset = srv_ts - time.time()
                            m1_bars = _fetch_m1_bars(feed, now_label=srv_dt, limit=200)
                            m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=srv_ts)
                            last_m1_mono = loop_t0

                        q = feed.fetch_quote()
                        last_bid, last_ask = q["bid"], q["ask"]
                        now_ts = time.time() + clock_offset

                        if tick_fh is not None:
                            tick_fh.write(
                                json.dumps(
                                    {
                                        "ts": round(now_ts, 3),
                                        "bid": last_bid,
                                        "ask": last_ask,
                                        "m1": {
                                            "bias": m1_ctx["bias"],
                                            "label": m1_ctx["label"],
                                            "support": m1_ctx.get("support"),
                                            "resistance": m1_ctx.get("resistance"),
                                            "atr_m1": m1_ctx.get("atr_m1"),
                                        },
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                            ticks_recorded += 1
                            if ticks_recorded % 20 == 0:
                                tick_fh.flush()

                        evs = eng.on_tick(ts=now_ts, bid=last_bid, ask=last_ask, m1_context=m1_ctx)
                        for ev in evs:
                            _print_event(ev)
                            if ev["type"] == "CLOSE_ALL":
                                append_jsonl_private(journal_path, ev["wave"])

                        if (loop_t0 - last_status_mono) >= args.status_seconds:
                            _print_status(eng, m1_ctx, last_bid, last_ask)
                            last_status_mono = loop_t0
                    except (Blocked, McpError, OSError) as exc:
                        print(f"[WARN] Transient tick read error ({exc}); retrying...", file=sys.stderr, flush=True)
                        with contextlib.suppress(Exception):
                            feed.connect()

                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    sleep_left = max(0.05, args.poll_seconds - (time.monotonic() - loop_t0))
                    time.sleep(sleep_left)
            finally:
                if tick_fh is not None:
                    tick_fh.flush()
                    tick_fh.close()

            # Close any virtual basket cleanly on exit
            if eng.basket is not None and last_bid > 0 and last_ask > 0:
                ex = last_bid if eng.basket["side"] == "buy" else last_ask
                w = eng._close_basket(ex, time.time() + clock_offset, "session_stop")
                _print_event({"type": "CLOSE_ALL", "wave": w})
                append_jsonl_private(journal_path, w)

            s = eng.summary(last_bid, last_ask)
            print(
                f"=== S1 VELOCITY SURFER STOPPED (ORDERS=0) ===\n"
                f"WAVES={s['waves_count']} ({s['total_legs_executed']}L, {s['wins']}W/{s['losses']}L WR={s['win_rate_pct']}%) | "
                f"NET=${s['realized_pnl_usd']:+.2f} PF={s['pf']} DD=${s['max_dd_usd']:.2f} | "
                f"TICKS_REC={ticks_recorded}"
                + ("" if args.no_record else f" | REPLAY: python3 scripts/daytrade_s1_velocity_paper.py --replay-sweep {tick_path}"),
                flush=True,
            )
            return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
