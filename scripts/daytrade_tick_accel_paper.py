#!/usr/bin/env python3
"""Read-only Live 2-3 Second Tick-Acceleration + 1H/5H Trend Paper Scalper.

Faithfully tests the user's real manual second-chart scalping style on LIVE Alpari MT5
Bid/Ask ticks with ZERO broker orders (`ORDERS=0`), real live spread, and realistic fill latency:
  1. Checks live 1-hour (1H) AND 5-hour (5H) trend alignment from closed MT5 H1 bars.
  2. Polls live Bid/Ask ticks ~4x/sec (every 0.25s) and builds a rolling 2.5-second window.
  3. Fires a Paper scalp when price accelerates sharply over 2-3s IN THE SAME DIRECTION
     as both 1H & 5H trend (second half of window faster than first half, move >= min_burst_usd)
     while live spread <= max_spread_usd.
  4. Enforces realistic execution latency (waits latency_ms before filling at next real Ask/Bid).
  5. Exits fast: hard stop (--stop-usd), instant profit-lock trailing exit once net P&L clears
     spread (locks profits when 2-3s velocity stalls or retraces 40% from peak), or max hold timeout.

Strictly read-only: NEVER submits, modifies, closes, or proposes any broker orders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
os.environ["TRADE_MCP_PAPER_TRADING"] = "false"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from daytrade_m1_momentum_validate import _fetch_range  # noqa: E402
from daytrade_m1_paper_window import ReadOnlyNativeFeed, _save_private_json  # noqa: E402
from daytrade_mt5_readonly_check import Blocked, loopback_native, timestamp  # noqa: E402

from trade_mcp.bridge.native import find  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.daytrade_paper import HARD_MAX_LOT, resolve_pricing_basis  # noqa: E402
from trade_mcp.m1_momentum import clean_ohlcv_bars  # noqa: E402
from trade_mcp.mcpclient import McpError  # noqa: E402


def compute_1h_5h_trend(h1_bars: list[list[float]]) -> dict[str, Any]:
    """Compute strictly causal 1-hour & 5-hour directional bias from closed H1 bars."""
    clean = clean_ohlcv_bars(h1_bars)
    if len(clean) < 7:
        return {"direction": 0, "label": "NEUTRAL_INSUFFICIENT_H1", "delta_1h": 0.0, "delta_5h": 0.0}
    # Drop final potentially incomplete bar
    closed = clean[:-1]
    last = closed[-1]
    delta_1h = round(last[4] - last[1], 3)
    delta_5h = round(last[4] - closed[-6][4], 3)
    if delta_1h > 0 and delta_5h > 0:
        d, lbl = 1, "BULLISH_1H_5H"
    elif delta_1h < 0 and delta_5h < 0:
        d, lbl = -1, "BEARISH_1H_5H"
    else:
        d, lbl = 0, "MIXED_WAIT"
    return {"direction": d, "label": lbl, "delta_1h": delta_1h, "delta_5h": delta_5h}


@dataclass
class TickAccelPaperEngine:
    """Pure-Python live 2-3 second tick-acceleration paper simulator with realistic latency."""

    multiplier: float = 100.0
    lot: float = 0.01
    window_sec: float = 2.5
    min_burst_usd: float = 0.80
    max_spread_usd: float = 0.45
    latency_sec: float = 0.25
    stop_usd: float = 6.0
    profit_arm_usd: float = 0.50
    profit_giveback_ratio: float = 0.40
    max_hold_sec: float = 20.0
    cooldown_sec: float = 5.0

    ticks: deque[tuple[float, float, float]] = field(default_factory=deque)
    pending_entry: dict[str, Any] | None = None
    open_pos: dict[str, Any] | None = None
    cooldown_until: float = 0.0
    trades: list[dict[str, Any]] = field(default_factory=list)
    bursts_detected: int = 0
    bursts_blocked_spread: int = 0
    bursts_blocked_trend: int = 0

    def on_tick(self, ts: float, bid: float, ask: float, *, trend_dir: int) -> dict[str, Any] | None:
        """Process one live Bid/Ask sample and return a closed trade event if one just closed."""
        mid = 0.5 * (bid + ask)
        spread = max(0.0, ask - bid)
        self.ticks.append((ts, bid, ask))
        while self.ticks and (ts - self.ticks[0][0]) > max(self.window_sec + 1.0, 4.0):
            self.ticks.popleft()

        # 1) Execute pending delayed entry once latency_sec has elapsed (honest live fill!)
        if self.pending_entry is not None and ts >= self.pending_entry["fill_after"]:
            side = self.pending_entry["side"]
            entry_px = ask if side == "buy" else bid
            self.open_pos = {
                "side": side,
                "entry": entry_px,
                "opened_at": ts,
                "trigger_mid": self.pending_entry["trigger_mid"],
                "entry_spread": spread,
                "peak_net_usd": -spread * self.lot * self.multiplier,
                "armed": False,
                "burst_move": self.pending_entry["burst_move"],
            }
            self.pending_entry = None

        # 2) Manage open paper scalp position
        if self.open_pos is not None:
            p = self.open_pos
            side = p["side"]
            exit_px = bid if side == "buy" else ask
            diff = (exit_px - p["entry"]) if side == "buy" else (p["entry"] - exit_px)
            net_usd = diff * self.lot * self.multiplier
            if net_usd > p["peak_net_usd"]:
                p["peak_net_usd"] = net_usd
            if net_usd >= self.profit_arm_usd:
                p["armed"] = True

            reason: str | None = None
            if net_usd <= -self.stop_usd:
                reason = "hard_stop"
            elif p["armed"] and net_usd <= p["peak_net_usd"] * (1.0 - self.profit_giveback_ratio):
                reason = "profit_lock_stall"
            elif (ts - p["opened_at"]) >= self.max_hold_sec:
                reason = "max_hold_timeout"

            if reason is not None:
                rec = {
                    "side": side,
                    "opened_at": round(p["opened_at"], 3),
                    "closed_at": round(ts, 3),
                    "hold_seconds": round(ts - p["opened_at"], 2),
                    "entry": round(p["entry"], 3),
                    "exit": round(exit_px, 3),
                    "entry_spread_price": round(p["entry_spread"], 4),
                    "burst_move_price": round(p["burst_move"], 4),
                    "peak_net_usd": round(p["peak_net_usd"], 4),
                    "net_pnl_usd": round(net_usd, 4),
                    "exit_reason": reason,
                }
                self.trades.append(rec)
                self.open_pos = None
                self.cooldown_until = ts + self.cooldown_sec
                return rec
            return None

        # 3) Detect 2-3 second acceleration burst when flat & not cooling down
        if self.pending_entry is not None or ts < self.cooldown_until:
            return None
        win = [x for x in self.ticks if (ts - x[0]) <= self.window_sec]
        if len(win) < 4 or (win[-1][0] - win[0][0]) < 1.2:
            return None

        mids = [0.5 * (x[1] + x[2]) for x in win]
        total_move = mids[-1] - mids[0]
        half_idx = len(mids) // 2
        first_half = mids[half_idx] - mids[0]
        second_half = mids[-1] - mids[half_idx]

        burst_dir = 0
        # Acceleration condition: total 2.5s move >= min_burst_usd AND 2nd half faster than 1st half in same direction
        if total_move >= self.min_burst_usd and second_half > max(0.0, first_half * 0.9):
            burst_dir = 1
        elif total_move <= -self.min_burst_usd and second_half < min(0.0, first_half * 0.9):
            burst_dir = -1

        if burst_dir == 0:
            return None
        self.bursts_detected += 1

        if spread > self.max_spread_usd:
            self.bursts_blocked_spread += 1
            return None
        if trend_dir != burst_dir:
            self.bursts_blocked_trend += 1
            return None

        self.pending_entry = {
            "side": "buy" if burst_dir == 1 else "sell",
            "fill_after": ts + self.latency_sec,
            "trigger_mid": mid,
            "burst_move": total_move,
        }
        return None

    def force_close_if_open(self, ts: float, bid: float, ask: float) -> dict[str, Any] | None:
        if self.open_pos is None:
            return None
        p = self.open_pos
        side = p["side"]
        exit_px = bid if side == "buy" else ask
        diff = (exit_px - p["entry"]) if side == "buy" else (p["entry"] - exit_px)
        net_usd = diff * self.lot * self.multiplier
        rec = {
            "side": side,
            "opened_at": round(p["opened_at"], 3),
            "closed_at": round(ts, 3),
            "hold_seconds": round(ts - p["opened_at"], 2),
            "entry": round(p["entry"], 3),
            "exit": round(exit_px, 3),
            "entry_spread_price": round(p["entry_spread"], 4),
            "burst_move_price": round(p["burst_move"], 4),
            "peak_net_usd": round(p["peak_net_usd"], 4),
            "net_pnl_usd": round(net_usd, 4),
            "exit_reason": "session_end",
        }
        self.trades.append(rec)
        self.open_pos = None
        return rec

    def summary(self) -> dict[str, Any]:
        pnls = [t["net_pnl_usd"] for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gp = sum(wins)
        gl = abs(sum(losses))
        pf = round(gp / gl, 3) if gl > 0 else (99.0 if gp > 0 else None)
        eq = 0.0
        peak = 0.0
        dd = 0.0
        for p in pnls:
            eq += p
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        avg_hold = round(sum(t["hold_seconds"] for t in self.trades) / len(self.trades), 2) if self.trades else 0.0
        avg_sp = round(sum(t["entry_spread_price"] for t in self.trades) / len(self.trades), 4) if self.trades else 0.0
        return {
            "bursts_detected": self.bursts_detected,
            "bursts_blocked_spread": self.bursts_blocked_spread,
            "bursts_blocked_trend": self.bursts_blocked_trend,
            "trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(100.0 * len(wins) / len(self.trades), 2) if self.trades else 0.0,
            "net_pnl_usd": round(sum(pnls), 2),
            "pf": pf,
            "max_dd_usd": round(dd, 2),
            "avg_hold_seconds": avg_hold,
            "avg_entry_spread_price": avg_sp,
        }


def _run_mock_stream(engine: TickAccelPaperEngine) -> list[dict[str, Any]]:
    """Deterministic synthetic 1-second burst sequence for offline verification."""
    t0 = 1764000000.0
    price = 3600.0
    sampled: list[dict[str, Any]] = []
    # 20 seconds: flat -> sharp bullish 2.5s burst (+1.60) -> push (+1.80) -> stall pullback
    deltas = [
        0.02, 0.03, -0.02, 0.01,
        0.15, 0.20, 0.30, 0.45, 0.55,  # sharp acceleration burst!
        0.45, 0.40, 0.35, 0.25, 0.15,  # continuation into profit
        -0.10, -0.25, -0.35, -0.20,    # deceleration / stall -> profit lock exit!
        0.05, 0.02,
    ]
    for i, d in enumerate(deltas):
        ts = t0 + i * 0.5
        price = round(price + d, 3)
        bid = price
        ask = round(price + 0.32, 3)
        sampled.append({"ts": ts, "bid": bid, "ask": ask})
        engine.on_tick(ts, bid, ask, trend_dir=1)
    engine.force_close_if_open(sampled[-1]["ts"], sampled[-1]["bid"], sampled[-1]["ask"])
    return sampled


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only live 2-3s tick-acceleration + 1H/5H trend Paper scalper.")
    p.add_argument("--symbol", default="XAUUSD_i")
    p.add_argument("--minutes", type=float, default=15.0, help="Live paper capture window in minutes (default 15)")
    p.add_argument("--poll-ms", type=int, default=250, help="Quote poll interval in milliseconds (default 250ms)")
    p.add_argument("--latency-ms", type=int, default=250, help="Simulated execution fill latency in ms (default 250ms)")
    p.add_argument("--burst-usd", type=float, default=0.80, help="Min 2.5s acceleration move in $/oz (default 0.80)")
    p.add_argument("--max-spread", type=float, default=0.45, help="Max allowed live spread in $/oz (default 0.45)")
    p.add_argument("--stop-usd", type=float, default=6.0, help="Hard emergency stop per trade in USD (default 6.0)")
    p.add_argument("--max-lot", type=float, default=0.01)
    p.add_argument("--mock", action="store_true", help="Run fast offline mock stream")
    p.add_argument("--summary", action="store_true")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)

    if args.max_lot <= 0 or args.max_lot > HARD_MAX_LOT:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"max_lot must be <= {HARD_MAX_LOT}"}))
        return 2

    out_path = Path(args.out) if args.out else (ROOT / "logs" / "tick_accel_paper.json")
    try:
        if args.mock:
            mult = 100.0
            engine = TickAccelPaperEngine(
                multiplier=mult,
                lot=args.max_lot,
                min_burst_usd=args.burst_usd,
                max_spread_usd=args.max_spread,
                latency_sec=args.latency_ms / 1000.0,
                stop_usd=args.stop_usd,
            )
            samples = _run_mock_stream(engine)
            trend_info = {"direction": 1, "label": "BULLISH_1H_5H", "delta_1h": 4.2, "delta_5h": 11.5}
            mode = "mock_offline"
        else:
            load_dotenv()
            cfg = Config.from_env()
            if cfg.backend != "native":
                raise Blocked("native_backend_required")
            mode = "live_mt5_tick_paper_readonly"
            samples = []
            with loopback_native():
                feed = ReadOnlyNativeFeed(cfg.mcp_url, cfg.mcp_token, args.symbol)
                spec, _ = feed.connect()
                _, mult = resolve_pricing_basis(spec, "auto")
                engine = TickAccelPaperEngine(
                    multiplier=mult,
                    lot=args.max_lot,
                    min_burst_usd=args.burst_usd,
                    max_spread_usd=args.max_spread,
                    latency_sec=args.latency_ms / 1000.0,
                    stop_usd=args.stop_usd,
                )

                def _refresh_trend() -> dict[str, Any]:
                    clock = feed._call("get_time_information", {})
                    raw_t = find(clock, "trade_server_time") or find(clock, "trade_server_last_known_time")
                    dt = timestamp(raw_t)
                    if dt is None:
                        return {"direction": 0, "label": "CLOCK_UNAVAILABLE", "delta_1h": 0.0, "delta_5h": 0.0}
                    now_lbl = dt.replace(tzinfo=None)
                    h1 = _fetch_range(feed, period="H1", start=now_lbl - timedelta(hours=16), end=now_lbl + timedelta(minutes=2), limit=30)
                    return compute_1h_5h_trend(h1)

                trend_info = _refresh_trend()
                print(
                    f"[LIVE PAPER START] symbol={args.symbol} minutes={args.minutes} "
                    f"trend={trend_info['label']} (1H={trend_info['delta_1h']:+.2f}, 5H={trend_info['delta_5h']:+.2f}) "
                    f"burst>=${args.burst_usd} latency={args.latency_ms}ms ORDERS=0",
                    file=sys.stderr,
                    flush=True,
                )
                t_start = time.monotonic()
                t_end = t_start + args.minutes * 60.0
                last_trend_refresh = t_start
                last_q = {"bid": 0.0, "ask": 0.0}
                while time.monotonic() < t_end:
                    now_m = time.monotonic()
                    if now_m - last_trend_refresh >= 60.0:
                        trend_info = _refresh_trend()
                        last_trend_refresh = now_m
                    q = feed.fetch_quote()
                    last_q = q
                    now_epoch = time.time()
                    samples.append({"ts": round(now_epoch, 3), "bid": q["bid"], "ask": q["ask"]})
                    closed = engine.on_tick(now_epoch, q["bid"], q["ask"], trend_dir=int(trend_info["direction"]))
                    if closed is not None:
                        print(
                            f"  [PAPER TRADE] {closed['side'].upper()} hold={closed['hold_seconds']}s "
                            f"net=${closed['net_pnl_usd']:+.2f} peak=${closed['peak_net_usd']:+.2f} "
                            f"reason={closed['exit_reason']} spread=${closed['entry_spread_price']}",
                            file=sys.stderr,
                            flush=True,
                        )
                    time.sleep(max(0.05, args.poll_ms / 1000.0))
                if last_q["bid"] > 0:
                    engine.force_close_if_open(time.time(), last_q["bid"], last_q["ask"])

        report = {
            "ok": True,
            "paper": True,
            "mode": mode,
            "executable": False,
            "execution_authorized": False,
            "orders_submitted": 0,
            "symbol": args.symbol,
            "recorded_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "trend_1h_5h": trend_info,
            "samples_count": len(samples),
            "summary": engine.summary(),
            "trades": engine.trades,
        }
        saved = _save_private_json(out_path, report)
        report["log_path"] = str(saved or out_path)
        if args.summary:
            s = report["summary"]
            print(
                f"=== LIVE 2-3s TICK-ACCELERATION + 1H/5H TREND PAPER SCALPER ({args.symbol}) ===\n"
                f"OK=True ORDERS=0 MODE={mode} SAMPLES={len(samples)} TREND={trend_info['label']}\n"
                f"BURSTS={s['bursts_detected']} (blocked_spread={s['bursts_blocked_spread']}, blocked_trend={s['bursts_blocked_trend']})\n"
                f"TRADES={s['trades']} ({s['wins']}W/{s['losses']}L, WR={s['win_rate_pct']}%) "
                f"NET=${s['net_pnl_usd']:+.2f} PF={s['pf']} DD=${s['max_dd_usd']:.2f} "
                f"AVG_HOLD={s['avg_hold_seconds']}s AVG_SPREAD=${s['avg_entry_spread_price']}"
            )
        else:
            print(json.dumps(report, indent=2))
        return 0
    except Blocked as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "blocked": exc.reason}, indent=2))
        return 1
    except (McpError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "orders_submitted": 0, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
