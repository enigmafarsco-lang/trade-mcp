#!/usr/bin/env python3
"""Strictly Read-Only Live 2-Engine Champions Paper Forward-Tester (ORDERS=0).

Runs our two empirically validated Alpari XAUUSD_i champions live alongside MT5:
  1. M5_MOMENTUM_G5_ADX20_35  (9/9 PASS on 517d Alpari M5)
  2. H1_DONCHIAN50_LONDON     (5-year validated Alpari H1 London breakout)

SAFETY GUARANTEES:
  - Zero orders ever submitted (ReadOnlyNativeFeed allows ONLY 4 read-only MCP tools).
  - TRADE_MCP_ALLOW_TRADING is forced false; never interferes with manual Demo trades.
  - Crash-safe atomic 0600 state file & append-only 0600 JSONL trade journal inside logs/.
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
from daytrade_mt5_readonly_check import (  # noqa: E402
    Blocked,
    compact_bars,
    loopback_native,
    timestamp,
)

from trade_mcp.bridge.native import McpError, find, to_epoch  # noqa: E402
from trade_mcp.champions_paper_engine import (  # noqa: E402
    ChampionsPaperEngine,
    append_trade_journal,
    save_state_atomic,
)
from trade_mcp.config import Config, load_dotenv  # noqa: E402


def _ensure_in_logs(path: Path) -> Path:
    resolved = path.resolve()
    logs_dir = (ROOT / "logs").resolve()
    if resolved.parent != logs_dir:
        raise ValueError(f"File must be a direct child of ignored logs/: {path}")
    return resolved


def _server_now(feed: ReadOnlyNativeFeed) -> tuple[float, datetime]:
    clock = feed._call("get_time_information", {})
    raw = find(clock, "trade_server_time") or find(clock, "trade_server_last_known_time")
    dt = timestamp(raw)
    if dt is None:
        raise Blocked("timestamp_unverified")
    return float(to_epoch(raw)), dt.replace(tzinfo=None)


def _fetch_recent_bars(
    feed: ReadOnlyNativeFeed,
    *,
    period: str,
    now_label: datetime,
    days_back: int,
    limit: int = 800,
) -> list[list[float]]:
    start = now_label - timedelta(days=days_back)
    end = now_label + timedelta(minutes=5)
    body = feed._call(
        "get_chart_history",
        {
            "symbol": feed.symbol,
            "period": period,
            "datetime_from": start.isoformat(timespec="seconds"),
            "datetime_to": end.isoformat(timespec="seconds"),
            "limit": limit,
        },
    )
    bars, _ = compact_bars(body)
    return bars


def _print_event(ev: dict[str, Any]) -> None:
    et = ev.get("type")
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    if et == "OPEN":
        p = ev["position"]
        tp_s = f" TP={p['tp']:.2f}" if p.get("tp") is not None else ""
        print(
            f"[{now_str}] [PAPER OPEN] #{p['trade_id']} {p['engine']} {p['side'].upper()} "
            f"{p['volume']:.2f} lot @ {p['entry']:.2f} | SL={p['stop']:.2f}{tp_s} "
            f"(risk=${p['initial_risk_usd']:.2f}, sp={p['entry_spread_price']:.2f})",
            flush=True,
        )
    elif et == "PYRAMID_ADD":
        print(
            f"[{now_str}] [PAPER PYRAMID +L{ev['leg_num']}] {ev['engine']} {ev['side'].upper()} "
            f"added @ {ev['add_price']:.2f} -> total={ev['total_volume']:.2f} lot avg={ev['avg_entry']:.2f} "
            f"| SL locked @ {ev['stop']:.2f}",
            flush=True,
        )
    elif et == "CLOSE":
        t = ev["trade"]
        legs_tag = f" ({t.get('legs_count', 1)}L)" if int(t.get("legs_count", 1)) > 1 else ""
        print(
            f"[{now_str}] [PAPER CLOSE] #{t['trade_id']} {t['engine']} {t['side'].upper()} "
            f"{t['volume']:.2f} lot{legs_tag} {t['entry']:.2f}->{t['exit']:.2f} | "
            f"PnL=${t['net_pnl_usd']:+.2f} ({t['exit_reason']}) | Eq=${t['equity_after_usd']:.2f}",
            flush=True,
        )
    elif et == "TRAIL":
        print(f"[{now_str}] [PAPER TRAIL] {ev['engine']} stop moved -> {ev['new_stop']:.2f}", flush=True)
    elif et == "ARM_PULLBACK":
        print(f"[{now_str}] [PAPER ARM] {ev['engine']} {ev['side'].upper()} pullback trigger @ bid {ev['trigger_bid']:.2f}", flush=True)
    elif et == "PULLBACK_EXPIRED":
        print(f"[{now_str}] [PAPER EXPIRE] {ev['engine']} {ev['side'].upper()} pullback unfilled -> watching 2-bar", flush=True)


def _short_eng(name: str) -> str:
    if name == ChampionsPaperEngine.ENGINE_M1_ACCEL:
        return "M1P"
    if name == ChampionsPaperEngine.ENGINE_M5_ACCEL:
        return "M5V"
    if name == ChampionsPaperEngine.ENGINE_M5:
        return "M5"
    if name == ChampionsPaperEngine.ENGINE_H1:
        return "H1"
    return name[:4]


def _print_heartbeat(engine: ChampionsPaperEngine, *, bid: float, ask: float) -> None:
    s = engine.summary(bid, ask)
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    sp = ask - bid
    m5_st = s["last_snapshot"].get("m5", {}).get("status", "init")
    h1_st = s["last_snapshot"].get("h1", {}).get("status", "init")
    h1_adx = s["last_snapshot"].get("m5", {}).get("h1_adx")
    accel_str = ""
    if engine.enable_accel_engines:
        m1a = s["last_snapshot"].get("m1_accel", {})
        m5a = s["last_snapshot"].get("m5_accel", {})
        accel_str = (
            f" M1P={m1a.get('status', 'init')}(atr={m1a.get('atr', 0)})"
            f" M5V={m5a.get('status', 'init')}(vr={m5a.get('vol_ratio', 0)})"
        )
    open_desc = ", ".join(
        f"{_short_eng(p['engine'])}:{p['side'].upper()} {p.get('legs_count', 1)}L/{p['volume']:.2f}@{p['entry']:.2f}(SL {p['stop']:.2f})"
        for p in s["open_positions"]
    ) or "FLAT"
    print(
        f"[{now_str}] [LIVE PAPER | ORDERS=0] {engine.symbol} {bid:.2f}/{ask:.2f} (sp={sp:.2f}) | "
        f"Eq=${s['equity_usd']:.2f} (Real=${s['realized_pnl_usd']:+.2f} Unr=${s['unrealized_pnl_usd']:+.2f} "
        f"MaxDD={s['max_dd_pct']:.1f}%) | Closed={s['closed_trades_count']} ({s['wins']}W/{s['losses']}L) | "
        f"Pos=[{open_desc}] | M5={m5_st} H1_ADX={h1_adx} H1={h1_st}{accel_str}",
        flush=True,
    )


def _run_mock_simulation(engine: ChampionsPaperEngine, *, state_path: Path, journal_path: Path, once: bool) -> dict[str, Any]:
    """Deterministic offline verification exercising H1 breakout open -> trail -> tick SL close."""
    # Construct 60 H1 bars ending at 09:00 UTC (inside London 08-16 UTC) with a clean breakout bar
    t_base = 1764028800  # 2025-11-25T00:00:00Z (multiple of 86400)
    h1_bars: list[list[float]] = []
    px = 3500.0
    for i in range(56):
        ts = float(t_base + i * 3600)
        # Keep range bounded [3492 .. 3512] with ATR ~$10 (>= $5.0)
        o = px
        c = 3500.0 + (3.0 if i % 2 == 0 else -3.0)
        h1_bars.append([ts, o, max(o, c) + 5.0, min(o, c) - 5.0, c, 500.0])
        px = c
    # Find next London bar open whose close (bar_open + 3600) falls in 08..15 UTC
    last_ts = int(h1_bars[-1][0]) + 3600
    while ((last_ts + 3600) // 3600) % 24 not in range(8, 16):
        h1_bars.append([float(last_ts), 3500.0, 3506.0, 3494.0, 3501.0, 500.0])
        last_ts += 3600
    # Breakout bar: closes cleanly at 3525 > max(prev 50 highs = 3508)
    h1_bars.append([float(last_ts), 3502.0, 3527.0, 3501.0, 3525.0, 900.0])
    server_now = float(last_ts + 3605)

    # Minimal M5 / M15 bars aligned to same clock
    m15_bars = [[float(last_ts - 900 * (39 - k)), 3500.0, 3505.0, 3495.0, 3501.0, 100.0] for k in range(40)]
    m5_bars = [[float(last_ts - 300 * (49 - k)), 3500.0, 3503.0, 3497.0, 3500.5, 50.0] for k in range(50)]

    evs = engine.on_bars(
        m5_bars=m5_bars, m15_bars=m15_bars, h1_bars=h1_bars,
        bid=3525.0, ask=3525.32, server_now_ts=server_now,
    )
    for ev in evs:
        _print_event(ev)
        if ev["type"] == "CLOSE":
            append_trade_journal(journal_path, ev["trade"])

    if not once and engine.ENGINE_H1 in engine.positions:
        # Next H1 bar surges strongly so Chandelier trail locks in positive profit above entry
        next_ts = float(last_ts + 3600)
        h1_bars.append([next_ts, 3525.0, 3605.0, 3524.0, 3600.0, 950.0])
        evs2 = engine.on_bars(
            m5_bars=m5_bars, m15_bars=m15_bars, h1_bars=h1_bars,
            bid=3566.0, ask=3566.32, server_now_ts=next_ts + 3605.0,
        )
        for ev in evs2:
            _print_event(ev)
        # Live tick retraces to hit trailed stop in profit
        stop_px = engine.positions[engine.ENGINE_H1]["stop"]
        evs3 = engine.on_tick(bid=stop_px - 0.10, ask=stop_px + 0.22, ts=next_ts + 3660.0)
        for ev in evs3:
            _print_event(ev)
            if ev["type"] == "CLOSE":
                append_trade_journal(journal_path, ev["trade"])

    save_state_atomic(state_path, engine.to_state_dict())
    _print_heartbeat(engine, bid=3540.0, ask=3540.32)
    return engine.summary(3540.0, 3540.32)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strictly read-only live 4-Engine Champions + Accel Winner-Pyramid Paper Forward-Tester (ORDERS=0).")
    parser.add_argument("--symbol", default="XAUUSD_i")
    parser.add_argument("--capital", type=float, default=1000.0, help="Starting virtual capital USD (default 1000)")
    parser.add_argument("--risk-pct", type=float, default=1.5, help="Dynamic risk %% per trade (default 1.5; ignored if --fixed-lot)")
    parser.add_argument("--fixed-lot", type=float, default=None, help="Optional fixed virtual lot (e.g. 0.01)")
    parser.add_argument("--max-lot", type=float, default=0.10, help="Max dynamic lot cap (<= 0.10)")
    parser.add_argument("--dd-governor", type=float, default=12.0, help="Cut risk 50%% when DD >= this %% (default 12)")
    parser.add_argument("--champions-only", action="store_true", help="Disable fast M1/M5 Winner-Pyramid acceleration engines")
    parser.add_argument("--accel-leg-lot", type=float, default=0.01, help="Per-leg lot for fast Winner-Pyramid engines (0.01..0.05, default 0.01)")
    parser.add_argument("--poll-seconds", type=float, default=5.0, help="Live Bid/Ask poll interval seconds (default 5)")
    parser.add_argument("--heartbeat-seconds", type=float, default=60.0, help="Status print interval seconds (default 60)")
    parser.add_argument("--minutes", type=float, default=0.0, help="Duration in minutes (0 = run until Ctrl+C)")
    parser.add_argument("--once", action="store_true", help="Run one snapshot evaluation and exit")
    parser.add_argument("--reset-state", action="store_true", help="Start fresh virtual equity instead of resuming state file")
    parser.add_argument("--state-file", default="logs/champions_paper_state.json", help="Persistent 0600 state JSON inside logs/")
    parser.add_argument("--journal-file", default="logs/champions_paper_trades.jsonl", help="Append-only 0600 trade JSONL inside logs/")
    parser.add_argument("--mock", action="store_true", help="Offline deterministic simulation smoke test")
    args = parser.parse_args(argv)
    enable_accel = not args.champions_only

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    state_path = _ensure_in_logs(Path(args.state_file) if Path(args.state_file).is_absolute() else (ROOT / args.state_file))
    journal_path = _ensure_in_logs(Path(args.journal_file) if Path(args.journal_file).is_absolute() else (ROOT / args.journal_file))

    stop_requested = False

    def _handle_sig(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    try:
        if args.mock:
            spec = {
                "point": 0.01, "trade_tick_size": 0.01, "trade_tick_value": 0.10,
                "trade_contract_size": 100.0, "currency_base": "XAU", "currency_profit": "USD",
            }
            engine = ChampionsPaperEngine(
                symbol=args.symbol, spec=spec, initial_capital_usd=args.capital,
                risk_pct=args.risk_pct, fixed_lot=args.fixed_lot, max_lot=args.max_lot,
                dd_governor_pct=args.dd_governor, enable_accel_engines=enable_accel,
                accel_leg_lot=args.accel_leg_lot,
            )
            if state_path.exists() and not args.reset_state:
                engine.load_state_dict(json.loads(state_path.read_text(encoding="utf-8")))
            summary = _run_mock_simulation(engine, state_path=state_path, journal_path=journal_path, once=args.once)
            print(f"OK=True ORDERS=0 MODE=mock_paper CLOSED={summary['closed_trades_count']} EQ=${summary['equity_usd']:.2f}")
            return 0

        load_dotenv(ROOT / ".env")
        cfg = Config.from_env()
        if cfg.backend != "native":
            raise Blocked("native_backend_required")

        with loopback_native():
            feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, args.symbol, timeout=25.0)
            spec, quote = feed.connect()
            engine = ChampionsPaperEngine(
                symbol=args.symbol, spec=spec, initial_capital_usd=args.capital,
                risk_pct=args.risk_pct, fixed_lot=args.fixed_lot, max_lot=args.max_lot,
                dd_governor_pct=args.dd_governor, enable_accel_engines=enable_accel,
                accel_leg_lot=args.accel_leg_lot,
            )
            eng_list = (
                "M5_MOMENTUM_G5_ADX20_35 + H1_DONCHIAN50_LONDON + M1_PRE15_PB5_PYRAMID3 + M5_VOL_PB_BURST_PYR3"
                if enable_accel
                else "M5_MOMENTUM_G5_ADX20_35 + H1_DONCHIAN50_LONDON"
            )
            if state_path.exists() and not args.reset_state:
                engine.load_state_dict(json.loads(state_path.read_text(encoding="utf-8")))
                print(
                    f"[RESUME | ORDERS=0] Loaded {state_path.name}: Eq=${engine.current_equity_usd(quote['bid'], quote['ask']):.2f} "
                    f"Closed={len(engine.closed_trades)} Open={len(engine.positions)} | Engines=[{eng_list}]",
                    flush=True,
                )
            else:
                mode_lbl = f"FIXED {args.fixed_lot:.2f} lot" if args.fixed_lot else f"DYNAMIC {args.risk_pct:.1f}% risk (0.01..{args.max_lot:.2f}, DD-gov {args.dd_governor:.0f}%)"
                print(
                    f"[START | PAPER ONLY | ORDERS=0] {args.symbol} | Capital=${args.capital:.2f} | Sizing={mode_lbl} | "
                    f"AccelLeg={engine.accel_leg_lot:.2f}x3 | Engines=[{eng_list}]",
                    flush=True,
                )

            t_start = time.monotonic()
            deadline = (t_start + args.minutes * 60.0) if args.minutes > 0 else None
            last_m1_bucket = -1
            last_m5_bucket = -1
            last_bar_fetch_mono = 0.0
            last_hb_mono = 0.0
            last_bid, last_ask = quote["bid"], quote["ask"]
            cached_m1: list[list[float]] = []
            cached_m5: list[list[float]] = []
            cached_m15: list[list[float]] = []
            cached_h1: list[list[float]] = []

            while not stop_requested:
                loop_t0 = time.monotonic()
                try:
                    q = feed.fetch_quote()
                    last_bid, last_ask = q["bid"], q["ask"]
                    srv_ts, srv_dt = _server_now(feed)

                    tick_evs = engine.on_tick(bid=last_bid, ask=last_ask, ts=srv_ts)
                    state_dirty = False
                    for ev in tick_evs:
                        _print_event(ev)
                        state_dirty = True
                        if ev["type"] == "CLOSE":
                            append_trade_journal(journal_path, ev["trade"])

                    m1_bucket = int(srv_ts // 60)
                    m5_bucket = int(srv_ts // 300)
                    need_m5 = (m5_bucket != last_m5_bucket) or ((loop_t0 - last_bar_fetch_mono) >= 60.0)
                    need_m1 = enable_accel and (m1_bucket != last_m1_bucket)

                    if need_m5 or need_m1:
                        if need_m1 or (enable_accel and not cached_m1):
                            cached_m1 = _fetch_recent_bars(feed, period="M1", now_label=srv_dt, days_back=1, limit=250)
                            last_m1_bucket = m1_bucket
                        if need_m5 or not cached_m5:
                            cached_m5 = _fetch_recent_bars(feed, period="M5", now_label=srv_dt, days_back=4, limit=600)
                            cached_m15 = _fetch_recent_bars(feed, period="M15", now_label=srv_dt, days_back=8, limit=500)
                            cached_h1 = _fetch_recent_bars(feed, period="H1", now_label=srv_dt, days_back=20, limit=360)
                            last_m5_bucket = m5_bucket
                            last_bar_fetch_mono = loop_t0
                        bar_evs = engine.on_bars(
                            m1_bars=cached_m1 if enable_accel else None,
                            m5_bars=cached_m5,
                            m15_bars=cached_m15,
                            h1_bars=cached_h1,
                            bid=last_bid,
                            ask=last_ask,
                            server_now_ts=srv_ts,
                        )
                        state_dirty = True
                        for ev in bar_evs:
                            _print_event(ev)
                            if ev["type"] == "CLOSE":
                                append_trade_journal(journal_path, ev["trade"])

                    if state_dirty:
                        save_state_atomic(state_path, engine.to_state_dict())

                    if (loop_t0 - last_hb_mono) >= args.heartbeat_seconds or args.once:
                        _print_heartbeat(engine, bid=last_bid, ask=last_ask)
                        last_hb_mono = loop_t0
                except (Blocked, McpError, OSError) as exc:
                    print(f"[WARN] Transient MT5 read error ({exc}); retrying in {args.poll_seconds}s...", file=sys.stderr, flush=True)
                    with contextlib.suppress(Exception):
                        feed.connect()

                if args.once:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                sleep_left = max(0.5, args.poll_seconds - (time.monotonic() - loop_t0))
                time.sleep(sleep_left)

            save_state_atomic(state_path, engine.to_state_dict())
            s = engine.summary(last_bid, last_ask)
            eng_breakdown = " | ".join(
                f"{_short_eng(k)}:{v['trades']}t/{v['legs']}L(${v['net_usd']:+.2f})"
                for k, v in s.get("by_engine", {}).items()
            ) or "none"
            print(
                f"=== PAPER FORWARD-TEST STOPPED CLEANLY (ORDERS=0) ===\n"
                f"EQ=${s['equity_usd']:.2f} REALIZED=${s['realized_pnl_usd']:+.2f} UNREALIZED=${s['unrealized_pnl_usd']:+.2f} "
                f"MAX_DD={s['max_dd_pct']:.2f}% CLOSED={s['closed_trades_count']} ({s['wins']}W/{s['losses']}L) PF={s['pf']}\n"
                f"BY_ENGINE: {eng_breakdown}\n"
                f"STATE={state_path} JOURNAL={journal_path}",
                flush=True,
            )
            return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
