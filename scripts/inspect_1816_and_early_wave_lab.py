#!/usr/bin/env python3
"""Exact 18:15-18:25 SRV candle/10s X-ray + Early-Wave Limit-TP Engine A/B on ALL 406k+ real ticks (ORDERS=0).

1. Prints exact M1 candles (18:10 -> 18:25 SRV) and 10-second price steps (18:16 -> 18:22 SRV)
   from the stitched latest day's tick stream.
2. Causally tests 6 early-entry + Limit-TP / Fast-Book engines (designed to catch steady $4-$6
   5-minute waterfalls like 18:16->18:21 BEFORE the snapback) across ALL recorded files, NIGHT,
   TODAY, and specifically 18:15->18:25 SRV.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trade_mcp.all_weather_paper import AllWeatherPaperSuite  # noqa: E402
from trade_mcp.s1_impulse_audit import combine_portfolio_waves, load_tick_stream  # noqa: E402

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
    }


def _night_only(waves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        w
        for w in waves
        if float(w.get("opened_at", 0.0)) > 0
        and datetime.fromtimestamp(float(w["opened_at"]), tz=timezone.utc).hour in NIGHT_HOURS
    ]


@dataclass
class EarlyWaveConfig:
    name: str
    mode: str  # "two_bar_mom" | "swing_reject" | "smooth_slope"
    # Two-bar M1 momentum params
    min_2bar_move: float = 2.20
    min_body_ratio: float = 0.55
    break_lookback_bars: int = 5
    # Swing rejection params
    swing_lookback_bars: int = 15
    min_prior_run: float = 2.50
    min_reject_bar: float = 1.20
    # Smooth 2m-3m slope params
    slope_win_sec: int = 150
    min_slope_move: float = 3.20
    min_slope_eff: float = 0.52
    min_20s_push: float = 0.65
    # Risk & Limit-TP booking
    leg_lot: float = 0.02
    sl_price: float = 2.20
    tp_price: float = 2.60  # Limit TP (fills on bid>=tp or ask<=tp, zero exit slippage!)
    be_arm_price: float = 1.20
    max_hold_sec: float = 600.0
    max_spread_price: float = 0.55
    active_server_hours: tuple[int, ...] | None = None
    win_cooldown_sec: float = 60.0
    loss_cooldown_sec: float = 240.0
    multiplier: float = 100.0


@dataclass
class EarlyWaveEngine:
    """Strictly causal Early-Wave + Limit-TP engine for $4-$6 multi-minute intraday drops/rallies (ORDERS=0)."""

    cfg: EarlyWaveConfig
    initial_capital_usd: float = 1000.0

    _cur_bar: dict[str, float] | None = None
    _bars: deque[dict[str, float]] = field(default_factory=lambda: deque(maxlen=60))
    _sec_hist: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=400))
    _last_sec: int = -1
    _prev_ts: float | None = None

    basket: dict[str, Any] | None = None
    cooldown_until: float = 0.0
    next_wave_id: int = 1
    realized_pnl_usd: float = 0.0
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
        self.realized_pnl_usd = round(self.realized_pnl_usd + pnl, 4)
        self.cooldown_until = ts + (self.cfg.win_cooldown_sec if pnl > 0 else self.cfg.loss_cooldown_sec)
        wave = {
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
        self.waves_closed.append(wave)
        return wave

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

        # Update causal closed M1 bars
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

        # Manage open position (Limit TP + BE + Hard SL + Max Hold)
        if self.basket is not None:
            b = self.basket
            side = b["side"]
            if side == "buy":
                if not b["be_armed"] and (bid - b["avg_entry"]) >= self.cfg.be_arm_price:
                    b["stop"] = max(b["stop"], round(b["avg_entry"] + 0.10, 4))
                    b["be_armed"] = True
                if bid <= b["stop"]:
                    self._close_pos(bid, ts, "be_stop" if b["be_armed"] else "sl_hit")
                    return []
                if bid >= b["tp"]:
                    self._close_pos(b["tp"], ts, "limit_tp")
                    return []
            else:
                if not b["be_armed"] and (b["avg_entry"] - ask) >= self.cfg.be_arm_price:
                    b["stop"] = min(b["stop"], round(b["avg_entry"] - 0.10, 4))
                    b["be_armed"] = True
                if ask >= b["stop"]:
                    self._close_pos(ask, ts, "be_stop" if b["be_armed"] else "sl_hit")
                    return []
                if ask <= b["tp"]:
                    self._close_pos(b["tp"], ts, "limit_tp")
                    return []
            if (ts - b["opened_at"]) >= self.cfg.max_hold_sec:
                self._close_pos(bid if side == "buy" else ask, ts, "max_hold")
            return []

        if ts < self.cooldown_until or spread > self.cfg.max_spread_price or not self._in_session(ts):
            return []

        sdir = 0
        if self.cfg.mode == "two_bar_mom" and bar_closed and len(self._bars) >= self.cfg.break_lookback_bars + 3:
            b1 = self._bars[-1]
            b2 = self._bars[-2]
            move2 = b1["c"] - b2["o"]
            rng1 = max(0.01, b1["h"] - b1["l"])
            body1 = abs(b1["c"] - b1["o"]) / rng1
            rng2 = max(0.01, b2["h"] - b2["l"])
            body2 = abs(b2["c"] - b2["o"]) / rng2
            prior = list(self._bars)[-(self.cfg.break_lookback_bars + 2) : -2]
            hi_p = max(x["h"] for x in prior)
            lo_p = min(x["l"] for x in prior)
            if (
                move2 >= self.cfg.min_2bar_move
                and b1["c"] > b1["o"]
                and b2["c"] > b2["o"]
                and body1 >= self.cfg.min_body_ratio
                and body2 >= self.cfg.min_body_ratio * 0.8
                and b1["c"] > hi_p
            ):
                sdir = 1
            elif (
                move2 <= -self.cfg.min_2bar_move
                and b1["c"] < b1["o"]
                and b2["c"] < b2["o"]
                and body1 >= self.cfg.min_body_ratio
                and body2 >= self.cfg.min_body_ratio * 0.8
                and b1["c"] < lo_p
            ):
                sdir = -1

        elif self.cfg.mode == "swing_reject" and bar_closed and len(self._bars) >= self.cfg.swing_lookback_bars + 2:
            b1 = self._bars[-1]
            b2 = self._bars[-2]
            prior = list(self._bars)[-(self.cfg.swing_lookback_bars + 1) : -1]
            hi_p = max(x["h"] for x in prior)
            lo_p = min(x["l"] for x in prior)
            # Bearish rejection right off recent swing high after an upward run
            if (
                (hi_p - lo_p) >= self.cfg.min_prior_run
                and max(b1["h"], b2["h"]) >= hi_p - 0.30
                and (b1["o"] - b1["c"]) >= self.cfg.min_reject_bar
                and b1["c"] < b2["l"]
            ):
                sdir = -1
            # Bullish rejection right off recent swing low after a downward run
            elif (
                (hi_p - lo_p) >= self.cfg.min_prior_run
                and min(b1["l"], b2["l"]) <= lo_p + 0.30
                and (b1["c"] - b1["o"]) >= self.cfg.min_reject_bar
                and b1["c"] > b2["h"]
            ):
                sdir = 1

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
            tp = round(entry_px + self.cfg.tp_price, 4) if sdir == 1 else round(entry_px - self.cfg.tp_price, 4)
            wid = self.next_wave_id
            self.next_wave_id += 1
            self.basket = {
                "wave_id": wid,
                "side": "buy" if sdir == 1 else "sell",
                "opened_at": ts,
                "total_volume": self.cfg.leg_lot,
                "avg_entry": entry_px,
                "stop": stop,
                "tp": tp,
                "be_armed": False,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="18:15-18:25 SRV X-Ray + Early-Wave Limit-TP Lab (ORDERS=0)")
    parser.add_argument("--start-hm", default="18:12", help="Window start HH:MM SRV (default 18:12)")
    parser.add_argument("--end-hm", default="18:25", help="Window end HH:MM SRV (default 18:25)")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    paths = sorted((ROOT / "logs").glob("s1_ticks_*.jsonl"))
    if not paths:
        print("No tick files found in logs/s1_ticks_*.jsonl", file=sys.stderr)
        return 1

    streams = [(p, load_tick_stream(p, rebuild_m1=True)) for p in paths]
    streams = [(p, t) for p, t in streams if t]
    stitched = _stitch_latest_day(streams)

    last_dt = datetime.fromtimestamp(stitched[-1][0], tz=timezone.utc)
    sh, sm = (int(x) for x in args.start_hm.split(":"))
    eh, em = (int(x) for x in args.end_hm.split(":"))
    t_start = last_dt.replace(hour=sh, minute=sm, second=0, microsecond=0).timestamp()
    t_end = last_dt.replace(hour=eh, minute=em, second=59, microsecond=0).timestamp()

    # Build M1 candles in [t_start, t_end]
    m1_bars: dict[int, dict[str, float]] = {}
    for ts, bid, ask, _ in stitched:
        if not (t_start <= ts <= t_end):
            continue
        mid = 0.5 * (bid + ask)
        bid_m = int(ts // 60) * 60
        if bid_m not in m1_bars:
            m1_bars[bid_m] = {"o": mid, "h": mid, "l": mid, "c": mid, "sp": ask - bid}
        else:
            b = m1_bars[bid_m]
            b["h"] = max(b["h"], mid)
            b["l"] = min(b["l"], mid)
            b["c"] = mid

    print(f"=== PART 1: EXACT M1 CANDLES ({args.start_hm} -> {args.end_hm} SRV) ===")
    for m_ts in sorted(m1_bars.keys()):
        b = m1_bars[m_ts]
        chg = b["c"] - b["o"]
        rng = b["h"] - b["l"]
        print(
            f"  {_hm(m_ts)[:5]} | O={b['o']:7.2f} H={b['h']:7.2f} L={b['l']:7.2f} C={b['c']:7.2f} | "
            f"Body={chg:+6.2f} Rng={rng:5.2f} sp={b['sp']:.2f}"
        )

    cfgs = [
        EarlyWaveConfig("EW_2BAR_M1_24H", "two_bar_mom", min_2bar_move=2.20, sl_price=2.20, tp_price=2.60, active_server_hours=None),
        EarlyWaveConfig("EW_2BAR_M1_SESS", "two_bar_mom", min_2bar_move=2.20, sl_price=2.20, tp_price=2.60, active_server_hours=DAY_HOURS),
        EarlyWaveConfig("EW_REJECT_M1_24H", "swing_reject", min_prior_run=2.50, min_reject_bar=1.20, sl_price=2.20, tp_price=2.80, active_server_hours=None),
        EarlyWaveConfig("EW_REJECT_M1_SESS", "swing_reject", min_prior_run=2.50, min_reject_bar=1.20, sl_price=2.20, tp_price=2.80, active_server_hours=DAY_HOURS),
        EarlyWaveConfig("EW_SLOPE_150S_24H", "smooth_slope", slope_win_sec=150, min_slope_move=3.20, min_slope_eff=0.52, sl_price=2.20, tp_price=2.50, active_server_hours=None),
        EarlyWaveConfig("EW_SLOPE_180S_SESS", "smooth_slope", slope_win_sec=180, min_slope_move=3.60, min_slope_eff=0.52, sl_price=2.30, tp_price=2.80, active_server_hours=DAY_HOURS),
    ]

    merged: dict[str, list[dict[str, Any]]] = {"AW_V3_24H": []}
    for c in cfgs:
        merged[c.name] = []

    total_ticks = 0
    for _, ticks in streams:
        total_ticks += len(ticks)
        aw = AllWeatherPaperSuite(initial_capital_usd=1000.0)
        engs = {c.name: EarlyWaveEngine(cfg=c) for c in cfgs}
        for ts, bid, ask, m1_ctx in ticks:
            aw.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
            for eng in engs.values():
                eng.on_tick(ts=ts, bid=bid, ask=ask)
        lts, lbid, lask, _ = ticks[-1]
        aw.close_all_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
        for eng in engs.values():
            eng.force_close_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
        merged["AW_V3_24H"].extend(aw.trades_closed)
        for name, eng in engs.items():
            merged[name].extend(eng.waves_closed)

    # Also run on stitched today to see exact trades inside [t_start, t_end]
    aw_t = AllWeatherPaperSuite(initial_capital_usd=1000.0)
    e_t = {c.name: EarlyWaveEngine(cfg=c) for c in cfgs}
    for ts, bid, ask, m1_ctx in stitched:
        aw_t.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        for eng in e_t.values():
            eng.on_tick(ts=ts, bid=bid, ask=ask)
    lts, lbid, lask, _ = stitched[-1]
    aw_t.close_all_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")
    for eng in e_t.values():
        eng.force_close_open(ts=lts, bid=lbid, ask=lask, reason="eof_replay")

    tod_w: dict[str, list[dict[str, Any]]] = {"AW_V3_24H": list(aw_t.trades_closed)}
    for name, eng in e_t.items():
        tod_w[name] = list(eng.waves_closed)

    print(f"\n=== PART 2: EARLY-WAVE LIMIT-TP A/B ({len(streams)} FILES, {total_ticks} TICKS, ORDERS=0) ===")
    for k in ["AW_V3_24H"] + [c.name for c in cfgs]:
        sa = _stats(merged[k], k)
        sn = _stats(_night_only(merged[k]), k)
        st = _stats(tod_w[k], k)
        win_tr = [w for w in tod_w[k] if t_start <= float(w.get("opened_at", 0.0)) <= t_end]
        sw = _stats(win_tr, k)
        print(
            f"  {k:20s} | ALL:${sa['profit']:+7.2f} DD:-${sa['max_dd']:5.2f} ({sa['wins']:2d}W/{sa['losses']:2d}L PF:{sa['pf']:4.2f}) | "
            f"Night:${sn['profit']:+6.2f} | Today:${st['profit']:+6.2f} | {args.start_hm}-{args.end_hm}:${sw['profit']:+5.2f}({sw['trades']}t)"
        )
        for wt in win_tr:
            print(
                f"      -> {_hm(wt['opened_at'])}->{_hm(wt['closed_at'])} {wt['side'].upper()} "
                f"{wt['avg_entry']:.2f}->{wt['exit_price']:.2f} ${wt['net_pnl_usd']:+.2f} ({wt['exit_reason']})"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
