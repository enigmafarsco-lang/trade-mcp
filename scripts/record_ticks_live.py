#!/usr/bin/env python3
"""24/7 strictly read-only live Bid/Ask + M1 context tick recorder for Alpari MT5 (ORDERS=0).

Records compact JSONL lines compatible with all S1/tick replay and research tools:
  {"ts": <broker_epoch_sec>, "bid": <float>, "ask": <float>, "m1": {"bias": ..., "label": ..., "support": ..., "resistance": ..., "atr_m1": ...}}

Features:
- Strictly read-only 4-tool loopback allowlist (`ORDERS=0`, never sends orders or proposals).
- Automatic UTC daily file rotation (`logs/s1_ticks_<SYMBOL>_YYYYMMDD.jsonl`, chmod 0600).
- Auto-recovery with backoff (1..15s) across MT5 terminal restarts and daily rollover gaps.
- Zero strategy overhead — designed to run quietly in a background terminal for days/weeks.
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
from trade_mcp.s1_velocity_surfer import analyze_m1_sr_context  # noqa: E402


def _daily_tick_path(symbol: str, utc_day: str, *, logs_dir: Path | None = None) -> Path:
    base = (ROOT / "logs" if logs_dir is None else logs_dir).resolve()
    safe_sym = "".join(ch if (ch.isalnum() or ch in ("_", "-")) else "_" for ch in symbol)
    target = (base / f"s1_ticks_{safe_sym}_{utc_day}.jsonl").resolve()
    if target.parent != base:
        raise ValueError(f"Invalid tick path outside logs/: {target}")
    return target


def _open_private_append(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    fh = os.fdopen(fd, "a", encoding="utf-8")
    os.chmod(path, 0o600)
    return fh


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


def _format_tick_line(now_ts: float, bid: float, ask: float, m1_ctx: dict[str, Any]) -> str:
    return (
        json.dumps(
            {
                "ts": round(now_ts, 3),
                "bid": bid,
                "ask": ask,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="24/7 read-only live Bid/Ask + M1 tick recorder (ORDERS=0)")
    parser.add_argument("--symbol", default="XAUUSD_i", help="Broker symbol (default XAUUSD_i)")
    parser.add_argument("--poll-seconds", type=float, default=0.30, help="Quote poll interval seconds (default 0.30)")
    parser.add_argument("--m1-refresh-seconds", type=float, default=15.0, help="M1 context refresh seconds (default 15)")
    parser.add_argument("--status-seconds", type=float, default=60.0, help="Console heartbeat interval seconds (default 60)")
    parser.add_argument("--dedupe-unchanged", action="store_true", help="Skip writing tick when bid & ask are unchanged and <1s elapsed")
    parser.add_argument("--minutes", type=float, default=0.0, help="Optional max runtime in minutes (0 = forever until Ctrl+C)")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    stop_requested = False

    def _handle_sig(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    try:
        load_dotenv(ROOT / ".env")
        app_cfg = Config.from_env()
        if app_cfg.backend != "native":
            raise Blocked("native_backend_required")

        with loopback_native():
            feed = ReadOnlyNativeFeed(app_cfg.mcp_url, app_cfg.mcp_token, args.symbol, timeout=15.0)
            _, quote = feed.connect()
            srv_ts0, _ = _server_now(feed)
            clock_offset = srv_ts0 - time.time()

            t_start = time.monotonic()
            deadline = (t_start + args.minutes * 60.0) if args.minutes > 0 else None
            last_m1_mono = 0.0
            last_status_mono = 0.0
            m1_ctx: dict[str, Any] = {"bias": 0, "label": "WARMING_UP", "support": None, "resistance": None, "atr_m1": None}
            last_bid, last_ask = float(quote["bid"]), float(quote["ask"])
            last_written_ts = 0.0
            ticks_total = 0
            ticks_day = 0
            reconnects = 0
            consecutive_errors = 0

            cur_day = datetime.now(timezone.utc).strftime("%Y%m%d")
            cur_path = _daily_tick_path(args.symbol, cur_day)
            tick_fh = _open_private_append(cur_path)

            print(
                f"[START | LIVE TICK RECORDER | READ-ONLY | ORDERS=0] {args.symbol} | "
                f"poll={args.poll_seconds}s m1={args.m1_refresh_seconds}s | file=logs/{cur_path.name}",
                flush=True,
            )

            try:
                while not stop_requested:
                    loop_t0 = time.monotonic()
                    try:
                        # Rotate daily file cleanly at 00:00 UTC
                        day_now = datetime.now(timezone.utc).strftime("%Y%m%d")
                        if day_now != cur_day:
                            tick_fh.flush()
                            tick_fh.close()
                            cur_day = day_now
                            cur_path = _daily_tick_path(args.symbol, cur_day)
                            tick_fh = _open_private_append(cur_path)
                            ticks_day = 0
                            print(f"[ROTATE] Switched to daily tick log: logs/{cur_path.name}", flush=True)

                        if (loop_t0 - last_m1_mono) >= args.m1_refresh_seconds or last_m1_mono == 0.0:
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

                        now_ts = time.time() + clock_offset
                        consecutive_errors = 0
                        unchanged = (bid_now == last_bid and ask_now == last_ask and (now_ts - last_written_ts) < 1.0)
                        last_bid, last_ask = bid_now, ask_now

                        if not (args.dedupe_unchanged and unchanged):
                            tick_fh.write(_format_tick_line(now_ts, bid_now, ask_now, m1_ctx))
                            last_written_ts = now_ts
                            ticks_total += 1
                            ticks_day += 1
                            if ticks_total % 25 == 0:
                                tick_fh.flush()

                        if (loop_t0 - last_status_mono) >= args.status_seconds:
                            tick_fh.flush()
                            size_mb = cur_path.stat().st_size / (1024 * 1024) if cur_path.exists() else 0.0
                            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                            print(
                                f"[{now_utc}Z] [REC | ORDERS=0] {args.symbol} {last_bid:.2f}/{last_ask:.2f} "
                                f"(sp={last_ask - last_bid:.2f}) | M1={m1_ctx['label']} | "
                                f"ticks={ticks_total} (today={ticks_day}, {size_mb:.2f} MB) | "
                                f"reconnects={reconnects} | logs/{cur_path.name}",
                                flush=True,
                            )
                            last_status_mono = loop_t0
                    except (Blocked, McpError, OSError, TimeoutError, ValueError) as exc:
                        consecutive_errors += 1
                        reconnects += 1
                        backoff = min(15.0, float(consecutive_errors))
                        print(
                            f"[WARN] Recorder transient error ({exc}); retry #{consecutive_errors} in {backoff:.0f}s...",
                            file=sys.stderr,
                            flush=True,
                        )
                        time.sleep(backoff)
                        with contextlib.suppress(Exception):
                            feed.connect()

                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    sleep_left = max(0.05, args.poll_seconds - (time.monotonic() - loop_t0))
                    time.sleep(sleep_left)
            finally:
                tick_fh.flush()
                tick_fh.close()

            size_mb = cur_path.stat().st_size / (1024 * 1024) if cur_path.exists() else 0.0
            print(
                f"=== LIVE TICK RECORDER STOPPED (ORDERS=0) ===\n"
                f"SYMBOL={args.symbol} TICKS_TOTAL={ticks_total} RECONNECTS={reconnects} "
                f"LAST_FILE=logs/{cur_path.name} ({size_mb:.2f} MB)",
                flush=True,
            )
            return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
