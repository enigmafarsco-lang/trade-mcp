#!/usr/bin/env python3
"""Causal A/B for Day-Only Expanded Macro-Wave (6m-10m/$6-$7), M1 3-Bar Accel Cascade & Smooth Slope (ORDERS=0).

Tests strictly on ALL recorded tick files (413k+ ticks), NIGHT, TODAY, and the exact 18:15-18:35 SRV
accelerating waterfall + pullback-continuation move, head-to-head against shipped AW_V3_24H.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trade_mcp.all_weather_paper import AllWeatherPaperSuite  # noqa: E402
from trade_mcp.s1_impulse_audit import (  # noqa: E402
    PullbackDipEngine,
    combine_portfolio_waves,
    load_tick_stream,
    pullback_dip_profiles,
)

NIGHT_HOURS = {21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8}
DAY_HOURS = (9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20)


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def _stats(waves: list[dict[str, Any]], name: str) -> dict[str, Any]:
    s = combine_portfolio_waves(name, [waves])
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
    }


def _night_only(waves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        w
        for w in waves
        if float(w.get("opened_at", 0.0)) > 0
        and datetime.fromtimestamp(float(w["opened_at"]), tz=timezone.utc).hour in NIGHT_HOURS
    ]


@dataclass
class AccelCascadeConfig:
    """True 3-bar M1 accelerating body cascade (|b1| > |b2| > |b3|) or smooth multi-minute slope."""

    name: str
    mode: str  # "m1_cascade" | "smooth_slope"
    min_3bar_move: float = 2.60
    min_b1_body: float = 1.00
    break_bars: int = 12
    slope_win_sec: int = 180
    min_slope_move: float = 3.50
    min_slope_eff: float = 0.42
    min_20s_push: float = 0.65
    leg_lot: float = 0.02
    sl_price: float = 2.40
    be_arm_price: float = 1.30
    ratchet_arm_price: float = 2.20
    ratchet_frac: float = 0.55
    ratchet_breathing: float = 1.15
    trail_arm_price: float = 3.00
    trail_pullback: float = 1.80
    max_hold_sec: float = 900.0
    max_spread_price: float = 0.55
    active_server_hours: tuple[int, ...] | None = None
    win_cooldown_sec: float = 60.0
    loss_cooldown_sec: float = 240.0
    multiplier: float = 100.0


@dataclass
class AccelCascadeEngine:
    """Causal M1 accelerating candle cascade & smooth-slope trend engine (ORDERS=0)."""

    cfg: AccelCascadeConfig
    initial_capital_usd: float = 1000.0

    _cur_bar: dict[str, float] | None = None
    _bars: deque[dict[str, float]] = field(default_factory=lambda: deque(maxlen=60))
    _sec_hist: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=420))
    _last_sec: int = -1
    _prev_ts: float | None = None
    basket: dict[str, Any] | None = None
    cooldown_until: float = 0.0
    next_wave_id: int = 1
    waves_closed: list[dict[str, Any]] = field(default_factory=list)

    def _in_session(self, ts: float) -> bool:
        if not self.cfg.active_server_hours:
            return True
        return datetime.fromtimestamp(ts, tz=timezone.utc).hour in self.cfg.active_server_hours

    def _win_move_eff(self, sec: int, mid: float, win: int) -> tuple[bool, float, float]:
        if not self._sec_hist:
            return False, 0.0, 0.0
        target = sec - win
        pts: list[tuple[int, float]] = []
        for s_ts, px in reversed(self._sec_hist):
            pts.append((s_ts, px))
            if s_ts <= target:
                break
        pts.reverse()
        if (sec - pts[0][0]) < int(win * 0.7):
            return False, 0.0, 0.0
        path = 0.0
        prev = pts[0][1]
        for _, px in pts[1:]:
            path += abs(px - prev)
            prev = px
        path += abs(mid - prev)
        net = mid - pts[0][1]
        return True, net, (abs(net) / path if path > 1e-9 else 0.0)

    def _close_pos(self, exit_px: float, ts: float, reason: str) -> dict[str, Any]:
        assert self.basket is not None
        b = self.basket
        self.basket = None
        diff = (exit_px - b["avg_entry"]) if b["side"] == "buy" else (b["avg_entry"] - exit_px)
        pnl = round(diff * b["total_volume"] * self.cfg.multiplier, 4)
        self.cooldown_until = ts + (self.cfg.win_cooldown_sec if pnl > 0 else self.cfg.loss_cooldown_sec)
        w = {
            "engine": self.cfg.name,
            "wave_id": b["wave_id"],
            "side": b["side"],
            "opened_at": b["opened_at"],
            "closed_at": ts,
            "hold_sec": round(ts - b["opened_at"], 1),
            "legs_count": 1,
            "total_volume": b["total_volume"],
            "avg_entry": b["avg_entry"],
            "exit_price": exit_px,
            "exit_reason": reason,
            "net_pnl_usd": pnl,
            "orders_submitted": 0,
        }
        self.waves_closed.append(w)
        return w

    def on_tick(self, *, ts: float, bid: float, ask: float) -> list[dict[str, Any]]:
        if bid <= 0 or ask <= 0 or ask < bid:
            return []
        if self._prev_ts is not None and (ts - self._prev_ts) > 300.0:
            self._cur_bar = None
            self._bars.clear()
            self._sec_hist.clear()
            self._last_sec = -1
        self._prev_ts = ts

        spread = ask - bid
        mid = 0.5 * (bid + ask)
        sec = int(ts)
        if sec > self._last_sec:
            self._sec_hist.append((sec, mid))
            self._last_sec = sec

        bar_id = int(ts // 60)
        bar_closed = False
        if self._cur_bar is None:
            self._cur_bar = {"bar_id": float(bar_id), "o": mid, "h": mid, "l": mid, "c": mid}
        elif bar_id > int(self._cur_bar["bar_id"]):
            self._bars.append(self._cur_bar)
            self._cur_bar = {"bar_id": float(bar_id), "o": mid, "h": mid, "l": mid, "c": mid}
            bar_closed = True
        else:
            self._cur_bar["h"] = max(self._cur_bar["h"], mid)
            self._cur_bar["l"] = min(self._cur_bar["l"], mid)
            self._cur_bar["c"] = mid

        if self.basket is not None:
            b = self.basket
            side = b["side"]
            ex_px = bid if side == "buy" else ask
            if side == "buy":
                b["best_px"] = max(b["best_px"], bid)
                gain = bid - b["avg_entry"]
                pb = b["best_px"] - bid
            else:
                b["best_px"] = min(b["best_px"], ask)
                gain = b["avg_entry"] - ask
                pb = ask - b["best_px"]

            if not b["stop_locked"] and gain >= self.cfg.be_arm_price:
                if side == "buy":
                    nsl = round(min(b["avg_entry"] + 0.15, bid - 0.65), 4)
                    if nsl > b["stop"]:
                        b["stop"] = nsl
                        b["stop_locked"] = True
                else:
                    nsl = round(max(b["avg_entry"] - 0.15, ask + 0.65), 4)
                    if nsl < b["stop"]:
                        b["stop"] = nsl
                        b["stop_locked"] = True

            if gain >= self.cfg.ratchet_arm_price:
                if side == "buy":
                    rsl = round(min(b["avg_entry"] + self.cfg.ratchet_frac * gain, bid - self.cfg.ratchet_breathing), 4)
                    if rsl > b["stop"]:
                        b["stop"] = rsl
                        b["stop_locked"] = True
                else:
                    rsl = round(max(b["avg_entry"] - self.cfg.ratchet_frac * gain, ask + self.cfg.ratchet_breathing), 4)
                    if rsl < b["stop"]:
                        b["stop"] = rsl
                        b["stop_locked"] = True

            if (side == "buy" and bid <= b["stop"]) or (side == "sell" and ask >= b["stop"]):
                self._close_pos(ex_px, ts, "ratchet_stop" if b["stop_locked"] else "hard_sl")
                return []
            if gain >= self.cfg.trail_arm_price and pb >= self.cfg.trail_pullback:
                self._close_pos(ex_px, ts, "trail_exit")
                return []
            if (ts - b["opened_at"]) >= self.cfg.max_hold_sec:
                self._close_pos(ex_px, ts, "max_hold")
            return []

        if ts < self.cooldown_until or spread > self.cfg.max_spread_price or not self._in_session(ts):
            return []

        sdir = 0
        if self.cfg.mode == "m1_cascade" and bar_closed and len(self._bars) >= self.cfg.break_bars + 4:
            b1, b2, b3 = self._bars[-1], self._bars[-2], self._bars[-3]
            d1, d2, d3 = b1["c"] - b1["o"], b2["c"] - b2["o"], b3["c"] - b3["o"]
            prior = list(self._bars)[-(self.cfg.break_bars + 3) : -3]
            hi_p = max(x["h"] for x in prior)
            lo_p = min(x["l"] for x in prior)
            # TRUE 3-bar discrete candle acceleration: same sign AND |d1| > |d2| > |d3|!
            if (
                d1 >= self.cfg.min_b1_body
                and d1 > d2 > d3 > 0.0
                and (b1["c"] - b3["o"]) >= self.cfg.min_3bar_move
                and b1["c"] > hi_p
            ):
                sdir = 1
            elif (
                d1 <= -self.cfg.min_b1_body
                and d1 < d2 < d3 < 0.0
                and (b3["o"] - b1["c"]) >= self.cfg.min_3bar_move
                and b1["c"] < lo_p
            ):
                sdir = -1
        elif self.cfg.mode == "smooth_slope":
            ok_s, ms, eff_s = self._win_move_eff(sec, mid, self.cfg.slope_win_sec)
            ok_20, m20, _ = self._win_move_eff(sec, mid, 20)
            if ok_s and ok_20 and abs(ms) >= self.cfg.min_slope_move and eff_s >= self.cfg.min_slope_eff:
                cand = 1 if ms > 0 else -1
                if (m20 * cand) >= self.cfg.min_20s_push:
                    sdir = cand

        if sdir != 0:
            entry_px = ask if sdir == 1 else bid
            stop = round(entry_px - self.cfg.sl_price, 4) if sdir == 1 else round(entry_px + self.cfg.sl_price, 4)
            wid = self.next_wave_id
            self.next_wave_id += 1
            self.basket = {
                "wave_id": wid,
                "side": "buy" if sdir == 1 else "sell",
                "opened_at": ts,
                "total_volume": self.cfg.leg_lot,
                "avg_entry": entry_px,
                "best_px": bid if sdir == 1 else ask,
                "stop": stop,
                "stop_locked": False,
            }
        return []

    def force_close_open(self, *, ts: float, bid: float, ask: float, reason: str = "eof_replay") -> dict[str, Any] | None:
        if self.basket is None:
            return None
        return self._close_pos(bid if self.basket["side"] == "buy" else ask, ts, reason)


def _stitch_latest_day(streams: list[tuple[Path, list[tuple[float, float, float, dict[str, Any]]]]]) -> list[tuple[float, float, float, dict[str, Any]]]:
    max_ts = max(t[-1][0] for _, t in streams)
    day_start = datetime.fromtimestamp(max_ts, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    by_ts: dict[float, tuple[float, float, float, dict[str, Any]]] = {}
    for _, ticks in streams:
        if ticks[-1][0] < day_start:
            continue
        for row in ticks:
            if row[0] >= day_start:
                by_ts[row[0]] = row
    return [by_ts[k] for k in sorted(by_ts.keys())]


def build_candidates() -> dict[str, Any]:
    macro_base = dict(pullback_dip_profiles(leg_lot=0.01, max_legs=6))["MACRO_WAVE_DYN"]
    return {
        # --- Day-Only Expanded MACRO_WAVE_DYN (NEVER night!) ---
        "MACRO_BASE_5M_80": PullbackDipEngine(cfg=macro_base),
        "MACRO_SESS_5M_62": PullbackDipEngine(
            cfg=replace(macro_base, trend_window_sec=300.0, min_trend_move_price=6.20, min_pullback_price=1.80)
        ),
        "MACRO_SESS_8M_60": PullbackDipEngine(
            cfg=replace(
                macro_base,
                trend_window_sec=480.0,
                extreme_window_sec=240.0,
                min_trend_move_price=6.00,
                min_pullback_price=1.70,
            )
        ),
        "MACRO_SESS_8M_65": PullbackDipEngine(
            cfg=replace(
                macro_base,
                trend_window_sec=480.0,
                extreme_window_sec=240.0,
                min_trend_move_price=6.50,
                min_pullback_price=1.80,
            )
        ),
        "MACRO_SESS_10M_70": PullbackDipEngine(
            cfg=replace(
                macro_base,
                trend_window_sec=600.0,
                extreme_window_sec=300.0,
                min_trend_move_price=7.00,
                min_pullback_price=2.00,
            )
        ),
        # --- True 3-Bar Accelerating Candle Cascade (|b1| > |b2| > |b3|) ---
        "M1_CASCADE_24H": AccelCascadeEngine(
            cfg=AccelCascadeConfig("M1_CASCADE_24H", "m1_cascade", min_3bar_move=2.60, min_b1_body=1.00, active_server_hours=None)
        ),
        "M1_CASCADE_SESS": AccelCascadeEngine(
            cfg=AccelCascadeConfig("M1_CASCADE_SESS", "m1_cascade", min_3bar_move=2.60, min_b1_body=1.00, active_server_hours=DAY_HOURS)
        ),
        # --- Smooth Multi-Minute Slope (calibrating 100% WR EW_SLOPE survivor!) ---
        "SLOPE_150S_E42_24H": AccelCascadeEngine(
            cfg=AccelCascadeConfig("SLOPE_150S_E42_24H", "smooth_slope", slope_win_sec=150, min_slope_move=3.60, min_slope_eff=0.42, active_server_hours=None)
        ),
        "SLOPE_180S_E42_24H": AccelCascadeEngine(
            cfg=AccelCascadeConfig("SLOPE_180S_E42_24H", "smooth_slope", slope_win_sec=180, min_slope_move=4.20, min_slope_eff=0.42, active_server_hours=None)
        ),
        "SLOPE_180S_E45_SESS": AccelCascadeEngine(
            cfg=AccelCascadeConfig("SLOPE_180S_E45_SESS", "smooth_slope", slope_win_sec=180, min_slope_move=4.00, min_slope_eff=0.45, active_server_hours=DAY_HOURS)
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Day-Only Expanded Macro-Wave + 3-Bar Cascade A/B (ORDERS=0)")
    parser.add_argument("--start-hm", default="18:15")
    parser.add_argument("--end-hm", default="18:35")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    paths = sorted((ROOT / "logs").glob("s1_ticks_*.jsonl"))
    if not paths:
        print("No tick files in logs/s1_ticks_*.jsonl", file=sys.stderr)
        return 1

    streams = [(p, load_tick_stream(p, rebuild_m1=True)) for p in paths]
    streams = [(p, t) for p, t in streams if t]
    stitched = _stitch_latest_day(streams)

    last_dt = datetime.fromtimestamp(stitched[-1][0], tz=timezone.utc)
    sh, sm = (int(x) for x in args.start_hm.split(":"))
    eh, em = (int(x) for x in args.end_hm.split(":"))
    t_start = last_dt.replace(hour=sh, minute=sm, second=0, microsecond=0).timestamp()
    t_end = last_dt.replace(hour=eh, minute=em, second=59, microsecond=0).timestamp()

    keys = ["AW_V3_24H"] + list(build_candidates().keys())
    merged: dict[str, list[dict[str, Any]]] = {k: [] for k in keys}
    total_ticks = 0
    for _, ticks in streams:
        total_ticks += len(ticks)
        aw = AllWeatherPaperSuite(initial_capital_usd=1000.0)
        cands = build_candidates()
        for ts, bid, ask, m1_ctx in ticks:
            aw.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
            for eng in cands.values():
                if isinstance(eng, PullbackDipEngine):
                    eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
                else:
                    eng.on_tick(ts=ts, bid=bid, ask=ask)
        lts, lbid, lask, _ = ticks[-1]
        aw.close_all_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
        for eng in cands.values():
            if hasattr(eng, "force_close_open"):
                eng.force_close_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
            elif hasattr(eng, "close_open_wave"):
                eng.close_open_wave(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
        merged["AW_V3_24H"].extend(aw.trades_closed)
        for name, eng in cands.items():
            merged[name].extend(eng.waves_closed)

    # Stitched today run for 18:15-18:35 exact trade audit
    aw_t = AllWeatherPaperSuite(initial_capital_usd=1000.0)
    c_t = build_candidates()
    for ts, bid, ask, m1_ctx in stitched:
        aw_t.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        for eng in c_t.values():
            if isinstance(eng, PullbackDipEngine):
                eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
            else:
                eng.on_tick(ts=ts, bid=bid, ask=ask)
    lts, lbid, lask, _ = stitched[-1]
    aw_t.close_all_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
    for eng in c_t.values():
        if hasattr(eng, "force_close_open"):
            eng.force_close_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
        elif hasattr(eng, "close_open_wave"):
            eng.close_open_wave(ts=lts, bid=lbid, ask=lask, reason="eof_replay")

    tod_w: dict[str, list[dict[str, Any]]] = {"AW_V3_24H": list(aw_t.trades_closed)}
    for name, eng in c_t.items():
        tod_w[name] = list(eng.waves_closed)

    print(f"=== MACRO_SESS EXPANSION + 3-BAR CASCADE + SMOOTH SLOPE ({len(streams)} FILES, {total_ticks} TICKS, ORDERS=0) ===")
    for k in keys:
        sa = _stats(merged[k], k)
        sn = _stats(_night_only(merged[k]), k)
        st = _stats(tod_w[k], k)
        win_tr = [w for w in tod_w[k] if t_start <= float(w.get("opened_at", 0.0)) <= t_end]
        sw = _stats(win_tr, k)
        print(
            f"  {k:20s} | ALL:${sa['profit']:+7.2f} DD:-${sa['max_dd']:5.2f} (R/DD:{sa['ret_dd']:4.2f}x {sa['wins']:2d}W/{sa['losses']:2d}L PF:{sa['pf']:4.2f}) | "
            f"Night:${sn['profit']:+6.2f} | Today:${st['profit']:+6.2f} | {args.start_hm}-{args.end_hm}:${sw['profit']:+5.2f}({sw['trades']}t)"
        )
        for wt in win_tr:
            print(
                f"      -> {_hm(wt['opened_at'])}->{_hm(wt['closed_at'])} {wt['side'].upper()} "
                f"{wt['total_volume']:.2f}lot {wt['avg_entry']:.2f}->{wt['exit_price']:.2f} ${wt['net_pnl_usd']:+.2f} ({wt['exit_reason']})"
            )

    # Also test swapping MACRO_BASE_5M_80 -> best MACRO_SESS inside AW_V3_24H!
    aw_no_macro = [w for w in merged["AW_V3_24H"] if w.get("engine") != "MACRO_WAVE_DYN"]
    aw_no_macro_tod = [w for w in tod_w["AW_V3_24H"] if w.get("engine") != "MACRO_WAVE_DYN"]
    print("\n=== HEAD-TO-HEAD: SWAPPING MACRO_WAVE INSIDE AW_V3_24H ===")
    for mk in ("MACRO_BASE_5M_80", "MACRO_SESS_5M_62", "MACRO_SESS_8M_60", "MACRO_SESS_8M_65", "MACRO_SESS_10M_70"):
        ca = _stats(aw_no_macro + merged[mk], mk)
        cn = _stats(_night_only(aw_no_macro + merged[mk]), mk)
        ct = _stats(aw_no_macro_tod + tod_w[mk], mk)
        cs = _stats([w for w in (aw_no_macro_tod + tod_w[mk]) if t_start <= float(w.get("opened_at", 0.0)) <= t_end], mk)
        print(
            f"  AW_V3({mk:17s}) | ALL:${ca['profit']:+7.2f} DD:-${ca['max_dd']:5.2f} (R/DD:{ca['ret_dd']:4.2f}x {ca['wins']:2d}W/{ca['losses']:2d}L PF:{ca['pf']:4.2f}) | "
            f"Night:${cn['profit']:+6.2f} | Today:${ct['profit']:+6.2f} | {args.start_hm}-{args.end_hm}:${cs['profit']:+5.2f}({cs['trades']}t)"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
