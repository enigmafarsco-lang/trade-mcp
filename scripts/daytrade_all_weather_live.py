#!/usr/bin/env python3
"""MetaTrader-style Live Paper Monitor for the winning All-Weather engine (COMBO_AW_PRO_M1, ORDERS=0).

Runs ONE read-only Alpari MT5 loopback feed driving our 3 validated engines as one unified paper account:
  - NEWS_SHOCK  (SHOCK10_DYN_RUN)  : >=$10/60s news rockets
  - MACRO_WAVE  (MACRO_WAVE_DYN)   : London/NY macro pullback-continuation waves
  - CALM_M1_PRO (CALM_M1_PRO_SESS) : London/NY calm-drift M1 trend engine (warm-started from MT5 M1 history)

Strictly paper-only (`ORDERS=0`, `TRADE_MCP_ALLOW_TRADING=false`). Zero broker orders are ever submitted.
"""

from __future__ import annotations

import argparse
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

from trade_mcp.all_weather_paper import AllWeatherPaperSuite  # noqa: E402
from trade_mcp.bridge.native import McpError, find, to_epoch  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.daytrade_paper import resolve_pricing_basis  # noqa: E402
from trade_mcp.s1_dual_radar import make_synthetic_dual_session  # noqa: E402
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


def _fmt_hhmmss(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def _fmt_usd(val: float) -> str:
    return f"+${val:,.2f}" if val >= 0 else f"-${abs(val):,.2f}"


def _print_trade_event(ev: dict[str, Any], *, ts: float, realized_profit: float) -> None:
    etype = str(ev.get("type", ""))
    label = str(ev.get("engine_label", ev.get("engine", "")))
    t_str = _fmt_hhmmss(ts)
    if etype == "OPEN_L1":
        side = str(ev.get("side", "")).upper()
        lot = float(ev.get("leg_lot", 0.01))
        entry = float(ev.get("entry", 0.0))
        sl = float(ev.get("stop", 0.0))
        print(
            f">>> [OPEN ] {t_str}  {side:4s} {lot:.2f} lot @ {entry:.2f} | SL={sl:.2f}  [{label}]",
            flush=True,
        )
    elif etype == "FIRE_ADD":
        side = str(ev.get("side", "")).upper()
        add_lot = float(ev.get("leg_lot", 0.01))
        tot_lot = float(ev.get("total_volume", add_lot))
        add_px = float(ev.get("add_price", 0.0))
        sl = float(ev.get("stop", 0.0))
        print(
            f">>> [ADD  ] {t_str}  {side:4s} +{add_lot:.2f} -> {tot_lot:.2f} lot @ {add_px:.2f} "
            f"| SL={sl:.2f} (LOCKED)  [{label}]",
            flush=True,
        )
    elif etype == "CLOSE_ALL":
        w = ev.get("wave", {})
        side = str(w.get("side", "")).upper()
        tot_lot = float(w.get("total_volume", 0.0))
        avg_e = float(w.get("avg_entry", 0.0))
        ex_p = float(w.get("exit_price", 0.0))
        pnl = float(w.get("net_pnl_usd", 0.0))
        print(
            f">>> [CLOSE] {t_str}  {side:4s} {tot_lot:.2f} lot {avg_e:.2f}->{ex_p:.2f} "
            f"| Trade: {_fmt_usd(pnl)} | Closed Profit: {_fmt_usd(realized_profit)}  [{label}]",
            flush=True,
        )


def _print_mt5_panel(
    suite: AllWeatherPaperSuite,
    *,
    symbol: str,
    bid: float,
    ask: float,
    srv_time: str,
    now_ts: float | None = None,
    mode_tag: str = "LIVE PAPER",
) -> None:
    acct = suite.account_summary(bid, ask)
    opens = suite.open_positions(bid, ask)
    st = suite.live_status(now_ts)
    pf_s = f"{acct['pf']:.2f}" if acct["pf"] is not None else "-"
    rm_s = f" | Risk={acct['risk_multiplier']:.2g}x" if abs(acct["risk_multiplier"] - 1.0) > 1e-6 else ""
    bar = "=" * 74
    print(f"\n{bar}", flush=True)
    print(
        f"[{mode_tag} | ORDERS=0{rm_s}]  {symbol}   Bid={bid:.2f}  Ask={ask:.2f}  (sp={ask - bid:.2f})   SRV {srv_time}",
        flush=True,
    )
    print(
        f"Balance: ${acct['balance_usd']:,.2f}  |  Equity: ${acct['equity_usd']:,.2f}  |  "
        f"PROFIT: {_fmt_usd(acct['net_profit_usd'])}  (Floating: {_fmt_usd(acct['floating_pnl_usd'])})",
        flush=True,
    )
    print(
        f"Closed: {acct['wins']}W/{acct['losses']}L ({acct['win_rate_pct']:.1f}%)  |  "
        f"Realized: {_fmt_usd(acct['realized_pnl_usd'])}  |  PF: {pf_s}  |  MaxDD: -${acct['max_dd_usd']:.2f}",
        flush=True,
    )
    print(
        f"Status:  {st['headline']}  (15m {st['move_15m']:+.2f} | ATR ${st['atr_m1']:.2f} | 60s {st['move_60s']:+.2f})",
        flush=True,
    )
    print("-" * 74, flush=True)
    if opens:
        print(f"OPEN POSITIONS ({len(opens)}):", flush=True)
        for p in opens:
            lk = " (LOCKED)" if p["stop_locked"] else ""
            print(
                f"  • {p['side']:4s} {p['volume']:.2f} lot ({p['legs']}L) @ {p['avg_entry']:.2f} "
                f"| SL: {p['stop']:.2f}{lk} | P/L: {_fmt_usd(p['pnl_usd'])}  [{p['engine_label']}]",
                flush=True,
            )
    else:
        print("OPEN POSITIONS (0):  —  (Waiting for valid setup)", flush=True)
    print(bar, flush=True)


def _print_final_summary(suite: AllWeatherPaperSuite, *, title: str, bid: float, ask: float) -> None:
    acct = suite.account_summary(bid, ask)
    pf_s = f"{acct['pf']:.2f}" if acct["pf"] is not None else "-"
    print(f"\n{'=' * 74}")
    print(title)
    print(f"{'=' * 74}")
    print(
        f"Balance:  ${acct['balance_usd']:,.2f}  (Start: ${acct['initial_capital_usd']:,.2f}  |  RiskMult: {acct['risk_multiplier']:.2g}x)\n"
        f"PROFIT:   {_fmt_usd(acct['net_profit_usd'])}\n"
        f"MaxDD:   -${acct['max_dd_usd']:.2f}\n"
        f"Trades:   {acct['trades_count']} ({acct['wins']}W / {acct['losses']}L = {acct['win_rate_pct']:.1f}% WR)\n"
        f"PF:       {pf_s}   |   AvgWin: {_fmt_usd(acct['avg_win_usd'])}   |   AvgLoss: {_fmt_usd(acct['avg_loss_usd'])}"
    )
    print("-" * 74)
    for eng_name in suite.engine_order:
        e_trades = [t for t in suite.trades_closed if t["engine"] == eng_name]
        ew = sum(1 for t in e_trades if float(t["net_pnl_usd"]) > 0)
        el = len(e_trades) - ew
        epnl = sum(float(t["net_pnl_usd"]) for t in e_trades)
        print(f"  {eng_name:18s} | {ew:2d}W/{el:<2d}L | Profit: {_fmt_usd(epnl):>10s}")
    print("=" * 74, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="MetaTrader-style Live Paper Monitor for COMBO_AW_V3_24H (ORDERS=0)"
    )
    parser.add_argument("--symbol", default="XAUUSD_i", help="Symbol to watch (default XAUUSD_i)")
    parser.add_argument("--capital", type=float, default=1000.0, help="Starting paper balance USD (default 1000)")
    parser.add_argument(
        "--calm-profile",
        default="ZSCORE_MR_24H",
        choices=("ZSCORE_MR_24H", "CALM_M1_PRO_SESS", "CALM_M1_PRO_150"),
        help="Quiet/Range engine profile (default ZSCORE_MR_24H — 24h GitHub TickRobust Z-Score MR)",
    )
    parser.add_argument(
        "--fast-shock-varlot",
        action="store_true",
        help="Use higher-lot VSNAP6_24H_VARLOT (+$44 night / +$82 total) instead of default safer VSNAP6_24H",
    )
    parser.add_argument(
        "--risk-mult",
        type=float,
        default=1.0,
        help="Lot/risk multiplier across all 4 engines (default 1.0; e.g. 1.5 or 2.0, range 0.25..5.0)",
    )
    parser.add_argument("--poll-seconds", type=float, default=0.30, help="Tick poll interval (default 0.30s)")
    parser.add_argument("--status-seconds", type=float, default=10.0, help="MT5 panel refresh interval (default 10s)")
    parser.add_argument("--minutes", type=float, default=0.0, help="Run duration in minutes (0 = until Ctrl+C)")
    parser.add_argument("--record", action="store_true", help="Record live ticks to logs/s1_ticks_*.jsonl")
    parser.add_argument("--report-file", default="", help="Atomic JSON status path (live defaults to reports/paper_live/all_weather_live.json)")
    parser.add_argument("--report-seconds", type=float, default=30.0, help="JSON report interval (default 30s)")
    parser.add_argument("--replay", nargs="+", default=[], help="Offline replay on recorded logs/s1_ticks_*.jsonl files")
    parser.add_argument("--rebuild-m1", action="store_true", help="Rebuild causal M1 context during --replay")
    parser.add_argument("--mock", action="store_true", help="Run instant synthetic smoke test (no MT5 needed)")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    variant_parts: list[str] = []
    if args.fast_shock_varlot:
        variant_parts.append("varlot")
    if abs(float(args.risk_mult) - 1.0) > 1e-6:
        rm_tag = f"{float(args.risk_mult):.2g}".replace(".", "p")
        variant_parts.append(f"r{rm_tag}x")
    variant_suffix = ("_" + "_".join(variant_parts)) if variant_parts else ""

    if args.report_file:
        report_path = _ensure_in_reports_or_logs(Path(args.report_file))
    elif not args.mock and not args.replay:
        report_path = _ensure_in_reports_or_logs(
            ROOT / "reports" / "paper_live" / f"all_weather_live{variant_suffix}.json"
        )
    else:
        report_path = None
    started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # 1) Offline synthetic smoke test
    if args.mock:
        suite = AllWeatherPaperSuite(
            initial_capital_usd=args.capital,
            calm_profile=args.calm_profile,
            fast_shock_varlot=args.fast_shock_varlot,
            risk_multiplier=args.risk_mult,
        )
        ticks = make_synthetic_dual_session()
        last_ts, last_bid, last_ask = 0.0, 0.0, 0.0
        for ts, bid, ask, m1_ctx in ticks:
            evs = suite.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
            for ev in evs:
                _print_trade_event(ev, ts=ts, realized_profit=suite.realized_pnl_usd)
            last_ts, last_bid, last_ask = ts, bid, ask
        suite.close_all_open(ts=last_ts, bid=last_bid, ask=last_ask, reason="eof_mock")
        _print_final_summary(
            suite,
            title=f"ALL-WEATHER PAPER MOCK SUMMARY ({len(ticks)} TICKS, ORDERS=0)",
            bid=last_bid,
            ask=last_ask,
        )
        if report_path is not None:
            snap = suite.snapshot(
                symbol=args.symbol,
                status="mock_complete",
                started_utc=started_utc,
                elapsed_minutes=0.0,
                last_bid=last_bid,
                last_ask=last_ask,
                server_time_str=_fmt_hhmmss(last_ts),
                ticks_processed=len(ticks),
                ticks_recorded=0,
                reconnect_count=0,
                consecutive_errors=0,
            )
            _write_atomic_json(report_path, snap)
        return 0

    # 2) Offline replay on recorded JSONL tick files
    if args.replay:
        suite = AllWeatherPaperSuite(
            initial_capital_usd=args.capital,
            calm_profile=args.calm_profile,
            fast_shock_varlot=args.fast_shock_varlot,
            risk_multiplier=args.risk_mult,
        )
        total_ticks = 0
        last_ts, last_bid, last_ask = 0.0, 0.0, 0.0
        for idx, raw_p in enumerate(args.replay):
            p = Path(raw_p)
            if not p.is_absolute():
                p = ROOT / p
            if idx > 0:
                suite.reset_file_state()
            ticks = load_tick_stream(p, rebuild_m1=args.rebuild_m1)
            for ts, bid, ask, m1_ctx in ticks:
                evs = suite.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
                for ev in evs:
                    _print_trade_event(ev, ts=ts, realized_profit=suite.realized_pnl_usd)
                last_ts, last_bid, last_ask = ts, bid, ask
                total_ticks += 1
            if ticks:
                end_evs = suite.close_all_open(ts=last_ts, bid=last_bid, ask=last_ask, reason="eof_replay")
                for ev in end_evs:
                    _print_trade_event(ev, ts=last_ts, realized_profit=suite.realized_pnl_usd)
        _print_final_summary(
            suite,
            title=f"ALL-WEATHER PAPER REPLAY ({len(args.replay)} FILES, {total_ticks} TICKS, ORDERS=0)",
            bid=last_bid,
            ask=last_ask,
        )
        if report_path is not None:
            snap = suite.snapshot(
                symbol=args.symbol,
                status="replay_complete",
                started_utc=started_utc,
                elapsed_minutes=0.0,
                last_bid=last_bid,
                last_ask=last_ask,
                server_time_str=_fmt_hhmmss(last_ts),
                ticks_processed=total_ticks,
                ticks_recorded=0,
                reconnect_count=0,
                consecutive_errors=0,
            )
            _write_atomic_json(report_path, snap)
        return 0

    # 3) Live read-only MT5 loopback monitor (ORDERS=0)
    stop_requested = False

    def _handle_sig(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trades_log_path = _ensure_in_logs(ROOT / "logs" / f"all_weather_paper_trades{variant_suffix}.jsonl")
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

            suite = AllWeatherPaperSuite(
                initial_capital_usd=args.capital,
                calm_profile=args.calm_profile,
                fast_shock_varlot=args.fast_shock_varlot,
                risk_multiplier=args.risk_mult,
            )
            suite.set_multiplier(float(mult))

            srv_ts0, srv_dt0 = _server_now(feed)
            clock_offset = srv_ts0 - time.time()
            last_bid, last_ask = float(quote["bid"]), float(quote["ask"])

            # Warm start calm engine from strictly closed MT5 M1 bars
            m1_bars0 = _fetch_m1_bars(feed, now_label=srv_dt0, limit=200)
            seeded = suite.seed_m1_bars(m1_bars0, server_now_ts=srv_ts0, spread=max(0.0, last_ask - last_bid))
            m1_ctx = analyze_m1_sr_context(m1_bars0, server_now_ts=srv_ts0)

            started_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(
                f"[START | ALL-WEATHER PAPER MONITOR | ORDERS=0]  {args.symbol} | "
                f"Calm={args.calm_profile} | RiskMult={args.risk_mult:.2g}x | Start Balance=${args.capital:,.2f} | "
                f"SRV={srv_dt0.strftime('%H:%M:%S')} | M1 Warmup={seeded} bars READY"
                + (f" | REC=logs/{tick_path.name}" if tick_path is not None else "")
                + (f" | REPORT={report_path.relative_to(ROOT)}" if report_path is not None else ""),
                flush=True,
            )

            t_start = time.monotonic()
            deadline = (t_start + args.minutes * 60.0) if args.minutes > 0 else None
            last_m1_mono = t_start
            last_status_mono = 0.0
            last_report_mono = 0.0
            ticks_processed = 0
            ticks_recorded = 0
            reconnect_count = 0
            consecutive_errors = 0

            def _flush_report(status_label: str, srv_str: str) -> None:
                if report_path is None:
                    return
                snap_now = suite.snapshot(
                    symbol=args.symbol,
                    status=status_label,
                    started_utc=started_utc,
                    elapsed_minutes=(time.monotonic() - t_start) / 60.0,
                    last_bid=last_bid,
                    last_ask=last_ask,
                    server_time_str=srv_str,
                    ticks_processed=ticks_processed,
                    ticks_recorded=ticks_recorded,
                    reconnect_count=reconnect_count,
                    consecutive_errors=consecutive_errors,
                )
                _write_atomic_json(report_path, snap_now)

            _print_mt5_panel(
                suite,
                symbol=args.symbol,
                bid=last_bid,
                ask=last_ask,
                srv_time=srv_dt0.strftime("%H:%M:%S"),
                now_ts=srv_ts0,
            )
            _flush_report("running", srv_dt0.strftime("%H:%M:%S"))
            last_status_mono = time.monotonic()
            last_report_mono = last_status_mono

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
                        if (loop_t0 - last_m1_mono) >= 15.0:
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
                                        "bid": bid_now,
                                        "ask": ask_now,
                                        "m1": {
                                            "bias": m1_ctx.get("bias", 0),
                                            "label": m1_ctx.get("label", "NEUTRAL"),
                                            "support": m1_ctx.get("support"),
                                            "resistance": m1_ctx.get("resistance"),
                                        },
                                    }
                                )
                                + "\n"
                            )
                            ticks_recorded += 1
                            if ticks_recorded % 20 == 0:
                                tick_fh.flush()

                        evs = suite.on_tick(ts=now_ts, bid=bid_now, ask=ask_now, m1_context=m1_ctx)
                        for ev in evs:
                            _print_trade_event(
                                ev,
                                ts=now_ts,
                                realized_profit=suite.realized_pnl_usd,
                            )
                            if ev.get("type") == "CLOSE_ALL" and isinstance(ev.get("wave"), dict):
                                append_jsonl_private(trades_log_path, ev["wave"])
                                _flush_report("running", _fmt_hhmmss(now_ts))
                                last_report_mono = time.monotonic()

                        now_mono = time.monotonic()
                        if (now_mono - last_status_mono) >= args.status_seconds:
                            _print_mt5_panel(
                                suite,
                                symbol=args.symbol,
                                bid=bid_now,
                                ask=ask_now,
                                srv_time=_fmt_hhmmss(now_ts),
                                now_ts=now_ts,
                            )
                            last_status_mono = now_mono

                        if report_path is not None and (now_mono - last_report_mono) >= report_interval_sec:
                            _flush_report("running", _fmt_hhmmss(now_ts))
                            last_report_mono = now_mono

                    except (McpError, OSError, Blocked) as exc:
                        consecutive_errors += 1
                        backoff = min(15.0, 2.0 * consecutive_errors)
                        print(
                            f"[WARN | RECONNECT #{consecutive_errors}] {exc} — retrying in {backoff:.0f}s",
                            flush=True,
                        )
                        time.sleep(backoff)
                        try:
                            feed.connect()
                            reconnect_count += 1
                            last_m1_mono = 0.0
                        except Exception as re_exc:
                            print(f"[WARN] reconnect failed: {re_exc}", flush=True)
                        continue

                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    sleep_rem = args.poll_seconds - (time.monotonic() - loop_t0)
                    if sleep_rem > 0:
                        time.sleep(sleep_rem)
            finally:
                if tick_fh is not None:
                    tick_fh.flush()
                    tick_fh.close()
                _flush_report("stopped", _fmt_hhmmss(time.time() + clock_offset))
                _print_final_summary(
                    suite,
                    title=f"STOPPED — ALL-WEATHER PAPER SUMMARY ({args.symbol}, ORDERS=0)",
                    bid=last_bid,
                    ask=last_ask,
                )
    except Blocked as exc:
        print(f"BLOCKED: {exc.reason}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
