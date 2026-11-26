#!/usr/bin/env python3
"""Strictly read-only Live Shock Radar + Dual Paper Runner (v2.5 Shock + Pullback-Dip V2, ORDERS=0).

Runs ONE 0.30s read-only Alpari MT5 loopback feed driving:
  1. 24/7 ShockRadar (sleeps through chop; alerts ONLY on high-efficiency >=$6/$8/$10 per 60s shocks)
  2. v2.5_SHOCK_10         (100% WR / $0 DD validated shock gate)
  3. v2.5_SHOCK_10_RUNNER  (same $10/60s gate with shock-scaled trail breathing room)
  4. v2.5_SHOCK_8          (wider $8/60s, eff>=0.45 shock sample collector)
  5. PB_DIP_V2_SESS        (Pullback-Dip V2: pivot SL + profit-lock + trend eff + London/NY 09-19 SRV)
  6. PB_DIP_V2_24H         (Pullback-Dip V2 24h shadow control to validate session filter OOS)

All orders are strictly virtual (`ORDERS=0`, `TRADE_MCP_ALLOW_TRADING=false`).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
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
from trade_mcp.s1_dual_radar import (  # noqa: E402
    DualRadarPaperSuite,
    ShockRadarConfig,
    make_synthetic_dual_session,
)
from trade_mcp.s1_impulse_audit import load_tick_stream  # noqa: E402
from trade_mcp.s1_velocity_surfer import analyze_m1_sr_context, append_jsonl_private  # noqa: E402


def _ensure_in_logs(path: Path) -> Path:
    logs_dir = (ROOT / "logs").resolve()
    resolved = path.resolve()
    if resolved.parent != logs_dir:
        raise ValueError(f"Output path must sit directly inside logs/: {path}")
    return resolved


def _ensure_in_reports_or_logs(path: Path) -> Path:
    resolved = path.resolve()
    allowed_roots = ((ROOT / "reports").resolve(), (ROOT / "logs").resolve())
    if not any(root == resolved.parent or root in resolved.parents for root in allowed_roots):
        raise ValueError(f"Report path must sit inside reports/ or logs/: {path}")
    if resolved.suffix.lower() != ".json":
        raise ValueError(f"Report path must end with .json: {path}")
    return resolved


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _server_now(feed: ReadOnlyNativeFeed) -> tuple[float, datetime]:
    clock = feed._call("get_time_information", {})
    raw_time = find(clock, "trade_server_time") or find(clock, "trade_server_last_known_time")
    dt = timestamp(raw_time)
    if dt is None:
        raise Blocked("timestamp_unverified")
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


def _emit_alert_sound(alert: dict[str, Any], *, enable_bell: bool, enable_sound: bool) -> None:
    if enable_bell:
        sys.stdout.write("\a")
        sys.stdout.flush()
    if not enable_sound:
        return
    tier = str(alert.get("tier", ""))
    side = str(alert.get("side", ""))
    move_60s = float(alert.get("move_60s", 0.0))
    eff_60s = float(alert.get("eff_60s", 0.0))
    bid = float(alert.get("bid", 0.0))
    with contextlib.suppress(Exception):
        notify = shutil.which("notify-send")
        if notify and tier in ("SHOCK_8", "SHOCK_10"):
            urgency = "critical" if tier == "SHOCK_10" else "normal"
            subprocess.Popen(
                [
                    notify,
                    "-u",
                    urgency,
                    f"XAUUSD {tier} ({side})",
                    f"60s={move_60s:+.2f} eff={eff_60s:.2f} @ {bid:.2f}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    with contextlib.suppress(Exception):
        paplay = shutil.which("paplay")
        if paplay:
            sound_candidates = (
                "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"
                if tier == "SHOCK_10"
                else "/usr/share/sounds/freedesktop/stereo/complete.oga",
                "/usr/share/sounds/freedesktop/stereo/bell.oga",
            )
            for sfile in sound_candidates:
                if Path(sfile).exists():
                    subprocess.Popen(
                        [paplay, sfile],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    break


def _print_radar_alert(alert: dict[str, Any]) -> None:
    tier = alert["tier"]
    esc_tag = " [ESCALATED]" if alert.get("escalated") else ""
    sess_tag = "ACTIVE_LND_NY" if alert.get("in_london_ny") else "OFF_SESSION"
    bar = "=" * 78
    print(
        f"\n{bar}\n"
        f">>> [SHOCK RADAR | {tier}{esc_tag} | {alert['side']}] #{alert['alert_id']} "
        f"@ SRV {alert['server_time'][11:19]} ({sess_tag})\n"
        f"    Quote={alert['bid']:.2f}/{alert['ask']:.2f} (sp={alert['spread']:.2f}) | "
        f"60s={alert['move_60s']:+.2f} eff={alert['eff_60s']:.2f} | 3s={alert['move_3s']:+.2f} | "
        f"upd10s={alert['updates_10s']}\n"
        f"    M1={alert['m1_label']} (bias={alert['m1_bias']:+d}, S={alert.get('support')} R={alert.get('resistance')})\n"
        f"{bar}",
        flush=True,
    )


def _print_engine_event(eng_name: str, ev: dict[str, Any]) -> None:
    et = ev.get("type")
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if et == "OPEN_L1":
        regime = str(ev.get("regime", ""))
        if regime == "PULLBACK_DIP":
            extra = (
                f"trend={ev.get('trend_move', 0):+.2f}(e={ev.get('trend_eff', 0):.2f}) "
                f"dip=${ev.get('pullback_depth', 0):.2f} bounce=${ev.get('bounce_3s', 0):+.2f}"
            )
        elif regime in ("SR_SWEEP", "SR_BREAK", "EMA_RECLAIM"):
            tp_part = f" TP={ev['tp']:.2f}" if ev.get("tp") is not None else ""
            extra = f"{regime} {ev.get('detail', '')}{tp_part}".strip()
        else:
            extra = f"vel={ev.get('vel_ratio', 0):.1f}x ({ev.get('move_3s', 0):+.2f}/3s)"
        lot_part = f" ({ev['leg_lot']:.2f}lot)" if ev.get("leg_lot") is not None else ""
        print(
            f"[{now_str}Z] [{eng_name:20s} | OPEN L1] #{ev['wave_id']} {ev['side'].upper()}{lot_part} @ {ev['entry']:.2f} "
            f"| SL={ev['stop']:.2f} | {extra} | M1={ev.get('m1_label', '')}",
            flush=True,
        )
    elif et == "FIRE_ADD":
        add_part = f"+{ev['leg_lot']:.2f}lot " if ev.get("leg_lot") is not None else ""
        print(
            f"[{now_str}Z] [{eng_name:20s} | ADD +L{ev['leg_num']}] #{ev['wave_id']} {ev['side'].upper()} "
            f"{add_part}@ {ev['add_price']:.2f} -> {ev['total_volume']:.2f}lot avg={ev['avg_entry']:.2f} | SL={ev['stop']:.2f}",
            flush=True,
        )
    elif et == "CLOSE_ALL":
        w = ev["wave"]
        print(
            f"[{now_str}Z] [{eng_name:20s} | CLOSE  ] #{w['wave_id']} {w['side'].upper()} "
            f"{w['legs_count']}L/{w['total_volume']:.2f}lot {w['avg_entry']:.2f}->{w['exit_price']:.2f} "
            f"| PnL=${w['net_pnl_usd']:+.2f} ({w['exit_reason']}, hold={w['hold_seconds']:.1f}s) "
            f"| Eq=${w['equity_after_usd']:.2f}",
            flush=True,
        )


def _print_status(suite: DualRadarPaperSuite, m1_ctx: dict[str, Any], bid: float, ask: float) -> None:
    rm = suite.radar.last_metrics
    now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
    srv_hr = rm.get("server_hour", 0)
    sess = "ACTIVE" if rm.get("in_london_ny") else "OFF-SESS"
    print(
        f"[{now_utc}Z | SRV {srv_hr:02d}h {sess:8s}] {bid:.2f}/{ask:.2f} (sp={rm.get('spread', 0):.2f}) | "
        f"Radar={suite.radar.state:9s} (60s={rm.get('move_60s', 0):+.2f} eff={rm.get('eff_60s', 0):.2f} "
        f"upd10={rm.get('updates_10s', 0):2d} alerts={sum(suite.radar.alerts_by_tier.values())}) | "
        f"M1={m1_ctx.get('label')}",
        flush=True,
    )
    parts: list[str] = []
    short_labels = {
        "v2.5_SHOCK_10": "SHK10",
        "v2.5_SHOCK_10_RUNNER": "RUN10",
        "v2.5_SHOCK_8": "SHK8",
        "PB_DIP_V2_SESS": "PB_SESS",
        "PB_DIP_V2_24H": "PB_24H",
        "SHOCK10_DYN_RUN": "SHK10D",
        "PB_V2_DYN_SESS": "PBDYN",
        "MACRO_WAVE_DYN": "MACRO",
        "SR_SWEEP_VSA_24H": "SWP24",
        "SR_SWEEP_VSA_SESS": "SWPS",
        "SR_BREAK_VSA_S10": "BRK10",
        "EMA_RECLAIM_S10": "EMAR",
    }
    for name in suite.engine_order:
        eng = suite.engines[name]
        s = eng.summary(bid, ask)
        pos_tag = f"*{eng.basket['side'][0].upper()}{len(eng.basket['legs'])}" if eng.basket else ""
        parts.append(f"{short_labels.get(name, name)}:{s['waves_count']}w(${s['realized_pnl_usd']:+.2f}{pos_tag})")
    print("  [PAPER | ORDERS=0] " + " | ".join(parts), flush=True)


def _print_suite_table(title: str, snap: dict[str, Any]) -> None:
    print(title)
    r = snap["radar"]
    print(
        f"RADAR ALERTS: total={r['total_alerts']} "
        f"(PRE_SHOCK={r['alerts_by_tier'].get('PRE_SHOCK', 0)}, "
        f"SHOCK_8={r['alerts_by_tier'].get('SHOCK_8', 0)}, "
        f"SHOCK_10={r['alerts_by_tier'].get('SHOCK_10', 0)})"
    )
    print(
        f"{'ENGINE':22s} | {'WAVES':>5s} | {'LEGS':>4s} | {'W/L':>7s} | {'WR%':>5s} | "
        f"{'NET $':>8s} | {'PF':>5s} | {'MaxDD':>6s} | {'AvgW':>6s} | {'AvgL':>6s}"
    )
    print("-" * 98)
    for name, edata in snap["engines"].items():
        s = edata["summary"]
        pf_s = f"{s['pf']:.2f}" if s["pf"] is not None else "  -  "
        wl_s = f"{s['wins']}/{s['losses']}"
        print(
            f"{name:22s} | {s['waves_count']:5d} | {s['total_legs_executed']:4d} | "
            f"{wl_s:>7s} | {s['win_rate_pct']:5.1f} | {s['realized_pnl_usd']:+8.2f} | "
            f"{pf_s:>5s} | {s['max_dd_usd']:6.2f} | {s['avg_win_usd']:+6.2f} | {s['avg_loss_usd']:+6.2f}"
        )
    c = snap["combined_primary"]
    pf_c = f"{c['pf']:.2f}" if c["pf"] is not None else "  -  "
    wl_c = f"{c['wins']}/{c['losses']}"
    print("-" * 98)
    print(
        f"{'COMBO(RUN10+PB_SESS)':22s} | {c['waves_count']:5d} | {c['total_legs']:4d} | "
        f"{wl_c:>7s} | {c['wr_pct']:5.1f} | {c['net_usd']:+8.2f} | "
        f"{pf_c:>5s} | {c['max_dd_usd']:6.2f} | {c['avg_win']:+6.2f} | {c['avg_loss']:+6.2f}"
    )
    if "combined_dynamic" in snap:
        cd = snap["combined_dynamic"]
        pf_d = f"{cd['pf']:.2f}" if cd["pf"] is not None else "  -  "
        wl_d = f"{cd['wins']}/{cd['losses']}"
        print(
            f"{'COMBO_DYN(SHK10D+PB)':22s} | {cd['waves_count']:5d} | {cd['total_legs']:4d} | "
            f"{wl_d:>7s} | {cd['wr_pct']:5.1f} | {cd['net_usd']:+8.2f} | "
            f"{pf_d:>5s} | {cd['max_dd_usd']:6.2f} | {cd['avg_win']:+6.2f} | {cd['avg_loss']:+6.2f}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Live Shock Radar + Dual Paper Runner (v2.5 Shock + Pullback-Dip V2, ORDERS=0)."
    )
    parser.add_argument("--symbol", default="XAUUSD_i")
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--leg-lot", type=float, default=0.01, help="Lot per pyramid leg (0.01..0.05, default 0.01)")
    parser.add_argument("--max-legs", type=int, default=6, help="Max legs per Shock wave (Pullback-Dip caps at min(4, max))")
    parser.add_argument("--pre-shock", type=float, default=6.0, help="Pre-shock radar 60s threshold $ (default 6.0)")
    parser.add_argument("--shock8", type=float, default=8.0, help="Shock-8 radar 60s threshold $ (default 8.0)")
    parser.add_argument("--shock10", type=float, default=10.0, help="Shock-10 radar 60s threshold $ (default 10.0)")
    parser.add_argument("--alert-cooldown", type=float, default=60.0, help="Radar alert anti-spam cooldown seconds (default 60)")
    parser.add_argument("--sound", action="store_true", help="Enable Linux paplay + notify-send audio/desktop alert on SHOCK_8/SHOCK_10")
    parser.add_argument("--no-bell", action="store_true", help="Disable terminal bell (\\a) on radar alerts")
    parser.add_argument("--poll-seconds", type=float, default=0.30, help="Live tick poll interval seconds (default 0.30)")
    parser.add_argument("--status-seconds", type=float, default=30.0, help="Console heartbeat interval seconds (default 30)")
    parser.add_argument("--record", action="store_true", help="Also record raw ticks to logs/s1_ticks_*.jsonl (default OFF to pair cleanly with record_ticks_live.py)")
    parser.add_argument("--report-file", default="", help="Continuously write compact git-safe JSON report inside reports/ or logs/")
    parser.add_argument("--report-seconds", type=float, default=300.0, help="Periodic flush seconds for --report-file (default 300)")
    parser.add_argument("--minutes", type=float, default=0.0, help="Run duration minutes (0 = 24/7 until Ctrl+C)")
    parser.add_argument(
        "--include-dynamic",
        action="store_true",
        help="Also run Acceleration x Duration dynamic-lot runners (SHOCK10_DYN_RUN, PB_V2_DYN_SESS, MACRO_WAVE_DYN, ORDERS=0)",
    )
    parser.add_argument(
        "--include-hybrids",
        action="store_true",
        help="Also run the 4 S10 VSA/SR/EMA hybrid engines side-by-side (ORDERS=0)",
    )
    parser.add_argument("--replay", nargs="+", default=[], help="Offline replay 1+ recorded tick JSONL files through Dual Radar + engines")
    parser.add_argument("--rebuild-m1", action="store_true", help="Rebuild causal M1 context during --replay")
    parser.add_argument("--mock", action="store_true", help="Run deterministic offline verification of Radar + Shock + Pullback-Dip")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    leg_lot = max(0.01, min(0.05, round(args.leg_lot, 2)))
    max_legs = max(1, min(6, int(args.max_legs)))
    radar_cfg = ShockRadarConfig(
        pre_shock_60s=float(args.pre_shock),
        shock8_60s=float(args.shock8),
        shock10_60s=float(args.shock10),
        alert_cooldown_sec=max(5.0, float(args.alert_cooldown)),
    )

    report_path: Path | None = None
    if args.report_file:
        rp = Path(args.report_file)
        if not rp.is_absolute():
            rp = ROOT / rp
        report_path = _ensure_in_reports_or_logs(rp)

    if args.mock:
        ticks = make_synthetic_dual_session()
        suite = DualRadarPaperSuite(
            initial_capital_usd=args.capital,
            leg_lot=leg_lot,
            max_legs=max_legs,
            radar_cfg=radar_cfg,
            include_dynamic=bool(args.include_dynamic),
            include_hybrids=bool(args.include_hybrids),
        )
        started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for ts, bid, ask, m1_ctx in ticks:
            out = suite.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
            if out["radar_alert"] is not None:
                _print_radar_alert(out["radar_alert"])
            for ename, ev in out["engine_events"]:
                _print_engine_event(ename, ev)
        ts_l, b_l, a_l, m1_l = ticks[-1]
        for ename, ev in suite.close_all_open(ts=ts_l, bid=b_l, ask=a_l, reason="eof_mock"):
            _print_engine_event(ename, ev)
        snap = suite.snapshot(
            symbol=args.symbol,
            status="mock_complete",
            started_utc=started_utc,
            elapsed_minutes=round((ticks[-1][0] - ticks[0][0]) / 60.0, 2),
            m1_ctx=m1_l,
            last_bid=b_l,
            last_ask=a_l,
            ticks_processed=len(ticks),
            ticks_recorded=0,
            reconnect_count=0,
            consecutive_errors=0,
        )
        _print_suite_table("\n=== DUAL RADAR + PAPER MOCK VERIFICATION (ORDERS=0) ===", snap)
        if report_path is not None:
            _write_atomic_json(report_path, snap)
        print(
            f"OK=True ORDERS=0 MODE=mock_dual_radar ALERTS={snap['radar']['total_alerts']} "
            f"SHK10_NET=${snap['engines']['v2.5_SHOCK_10']['summary']['realized_pnl_usd']:+.2f} "
            f"PB_SESS_NET=${snap['engines']['PB_DIP_V2_SESS']['summary']['realized_pnl_usd']:+.2f} "
            f"PB_SESS_BLOCKED_OFFSESS={suite.engines['PB_DIP_V2_SESS'].signals_blocked_session}"
        )
        return 0

    if args.replay:
        suite = DualRadarPaperSuite(
            initial_capital_usd=args.capital,
            leg_lot=leg_lot,
            max_legs=max_legs,
            radar_cfg=radar_cfg,
            include_dynamic=bool(args.include_dynamic),
            include_hybrids=bool(args.include_hybrids),
        )
        started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
        total_ticks = 0
        last_ts, last_bid, last_ask = 0.0, 0.0, 0.0
        last_m1: dict[str, Any] = {"bias": 0, "label": "UNKNOWN"}
        for raw_p in args.replay:
            p = Path(raw_p)
            if not p.is_absolute():
                p = ROOT / p
            ticks = load_tick_stream(p, rebuild_m1=args.rebuild_m1)
            for ts, bid, ask, m1_ctx in ticks:
                out = suite.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
                if out["radar_alert"] is not None:
                    _print_radar_alert(out["radar_alert"])
                last_ts, last_bid, last_ask, last_m1 = ts, bid, ask, m1_ctx
                total_ticks += 1
        suite.close_all_open(ts=last_ts, bid=last_bid, ask=last_ask, reason="eof_replay")
        snap = suite.snapshot(
            symbol=args.symbol,
            status="replay_complete",
            started_utc=started_utc,
            elapsed_minutes=0.0,
            m1_ctx=last_m1,
            last_bid=last_bid,
            last_ask=last_ask,
            ticks_processed=total_ticks,
            ticks_recorded=0,
            reconnect_count=0,
            consecutive_errors=0,
        )
        _print_suite_table(
            f"\n=== DUAL RADAR + {len(suite.engine_order)}-ENGINE REPLAY ({len(args.replay)} FILES, {total_ticks} TICKS, ORDERS=0) ===",
            snap,
        )
        if report_path is not None:
            _write_atomic_json(report_path, snap)
            print(f"SAVED_REPORT={report_path}")
        return 0

    stop_requested = False

    def _handle_sig(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    alerts_log_path = _ensure_in_logs(ROOT / "logs" / "shock_radar_alerts.jsonl")
    waves_log_path = _ensure_in_logs(ROOT / "logs" / "s1_dual_radar_waves.jsonl")
    tick_path = _ensure_in_logs(ROOT / "logs" / f"s1_ticks_{args.symbol}_{ts_tag}.jsonl") if args.record else None
    report_interval_sec = max(10.0, float(args.report_seconds))

    try:
        load_dotenv(ROOT / ".env")
        app_cfg = Config.from_env()
        if app_cfg.backend != "native":
            raise Blocked("native_backend_required")

        with loopback_native():
            feed = ReadOnlyNativeFeed(app_cfg.mcp_url, app_cfg.mcp_token, args.symbol, timeout=15.0)
            spec, quote = feed.connect()
            _, mult = resolve_pricing_basis(spec, "auto")

            suite = DualRadarPaperSuite(
                initial_capital_usd=args.capital,
                leg_lot=leg_lot,
                max_legs=max_legs,
                radar_cfg=radar_cfg,
                include_dynamic=bool(args.include_dynamic),
                include_hybrids=bool(args.include_hybrids),
            )
            suite.set_multiplier(float(mult))

            srv_ts0, srv_dt0 = _server_now(feed)
            clock_offset = srv_ts0 - time.time()
            started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(
                f"[START | DUAL SHOCK RADAR + {len(suite.engine_order)}-ENGINE PAPER | ORDERS=0] {args.symbol} | "
                f"SRV={srv_dt0.strftime('%H:%M:%S')} (Active LND/NY=09..19 SRV) | "
                f"Radar(PRE>=${radar_cfg.pre_shock_60s:.0f}, S8>=${radar_cfg.shock8_60s:.0f}, S10>=${radar_cfg.shock10_60s:.0f}) | "
                f"Engines={','.join(suite.engine_order)} | lot={leg_lot:.2f} | poll={args.poll_seconds}s"
                + (f" | REC=logs/{tick_path.name}" if tick_path is not None else " | REC=OFF")
                + (f" | REPORT={report_path.relative_to(ROOT)} ({int(report_interval_sec)}s)" if report_path else ""),
                flush=True,
            )

            t_start = time.monotonic()
            deadline = (t_start + args.minutes * 60.0) if args.minutes > 0 else None
            last_m1_mono = 0.0
            last_status_mono = 0.0
            last_report_mono = 0.0
            m1_ctx: dict[str, Any] = {"bias": 0, "label": "WARMING_UP", "support": None, "resistance": None}
            last_bid, last_ask = float(quote["bid"]), float(quote["ask"])
            ticks_processed = 0
            ticks_recorded = 0
            reconnect_count = 0
            consecutive_errors = 0

            def _flush_report(status_label: str) -> None:
                if report_path is None:
                    return
                snap_now = suite.snapshot(
                    symbol=args.symbol,
                    status=status_label,
                    started_utc=started_utc,
                    elapsed_minutes=(time.monotonic() - t_start) / 60.0,
                    m1_ctx=m1_ctx,
                    last_bid=last_bid,
                    last_ask=last_ask,
                    ticks_processed=ticks_processed,
                    ticks_recorded=ticks_recorded,
                    reconnect_count=reconnect_count,
                    consecutive_errors=consecutive_errors,
                )
                _write_atomic_json(report_path, snap_now)

            _flush_report("running")
            last_report_mono = time.monotonic()

            tick_fh = None
            if tick_path is not None:
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
                        bid_now, ask_now = float(q["bid"]), float(q["ask"])
                        if bid_now <= 0.0 or ask_now <= 0.0 or ask_now < bid_now:
                            time.sleep(max(0.5, args.poll_seconds))
                            continue

                        last_bid, last_ask = bid_now, ask_now
                        consecutive_errors = 0
                        now_ts = time.time() + clock_offset
                        ticks_processed += 1

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

                        res = suite.on_tick(ts=now_ts, bid=last_bid, ask=last_ask, m1_context=m1_ctx)
                        changed = False
                        if res["radar_alert"] is not None:
                            alert = res["radar_alert"]
                            _print_radar_alert(alert)
                            _emit_alert_sound(alert, enable_bell=(not args.no_bell), enable_sound=args.sound)
                            append_jsonl_private(alerts_log_path, alert)
                            changed = True

                        for ename, ev in res["engine_events"]:
                            _print_engine_event(ename, ev)
                            if ev["type"] == "CLOSE_ALL":
                                rec = dict(ev["wave"])
                                rec["engine"] = ename
                                append_jsonl_private(waves_log_path, rec)
                                changed = True

                        if changed or (loop_t0 - last_report_mono) >= report_interval_sec:
                            _flush_report("running")
                            last_report_mono = loop_t0

                        if (loop_t0 - last_status_mono) >= args.status_seconds:
                            _print_status(suite, m1_ctx, last_bid, last_ask)
                            last_status_mono = loop_t0
                    except (Blocked, McpError, OSError, TimeoutError, ValueError) as exc:
                        consecutive_errors += 1
                        reconnect_count += 1
                        backoff = min(15.0, float(consecutive_errors))
                        print(
                            f"[WARN] Dual radar transient error ({exc}); retry #{consecutive_errors} in {backoff:.0f}s...",
                            file=sys.stderr,
                            flush=True,
                        )
                        if (loop_t0 - last_report_mono) >= report_interval_sec:
                            with contextlib.suppress(Exception):
                                _flush_report("running")
                                last_report_mono = loop_t0
                        time.sleep(backoff)
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

            for ename, ev in suite.close_all_open(
                ts=time.time() + clock_offset,
                bid=last_bid,
                ask=last_ask,
                reason="session_stop",
            ):
                _print_engine_event(ename, ev)
                rec = dict(ev["wave"])
                rec["engine"] = ename
                append_jsonl_private(waves_log_path, rec)

            _flush_report("stopped")
            final_snap = suite.snapshot(
                symbol=args.symbol,
                status="stopped",
                started_utc=started_utc,
                elapsed_minutes=(time.monotonic() - t_start) / 60.0,
                m1_ctx=m1_ctx,
                last_bid=last_bid,
                last_ask=last_ask,
                ticks_processed=ticks_processed,
                ticks_recorded=ticks_recorded,
                reconnect_count=reconnect_count,
                consecutive_errors=consecutive_errors,
            )
            _print_suite_table(f"\n=== DUAL SHOCK RADAR + {len(suite.engine_order)}-ENGINE PAPER STOPPED (ORDERS=0) ===", final_snap)
            return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
