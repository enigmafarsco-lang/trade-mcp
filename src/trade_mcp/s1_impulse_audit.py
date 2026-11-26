"""Multi-file forensic tape audit & causal structural benchmark for XAUUSD_i S1 ticks (ORDERS=0).

Answers two empirical questions on real recorded Alpari Bid/Ask JSONL streams:
1. Tape Physics & Real-Wave Catalog: How many genuine >=$8 directional accelerations occurred,
   at what UTC times, and what were their causal pre-wave signatures (60s move, 60s efficiency,
   10s quote updates, 15-minute range breakout)?
2. Structural Strategy Benchmark (100% causal, real Bid/Ask + 0.25s fill latency):
   Compares:
   - High-Energy Shock Gates (sleep through choppy $4-$5 oscillations, wake ONLY on real shocks)
   - Causal 15-Minute Range Breakout + Acceleration
   - Inverted Pullback-Dip Entry (buy -$2..-$5 pullbacks inside strong 5m/10m trends instead of 3s tops)
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trade_mcp.s1_velocity_surfer import (
    S1SurferConfig,
    S1VelocitySurferEngine,
    _mini_m1_context,
    analyze_m1_sr_context,
    build_s1_profile_config,
    compute_dynamic_add_lot,
    compute_dynamic_l1_lot,
)


def load_tick_stream(
    tick_jsonl_path: Path,
    *,
    rebuild_m1: bool = False,
) -> list[tuple[float, float, float, dict[str, Any]]]:
    """Load recorded JSONL ticks once in memory (with optional causal M1 rebuild for legacy files)."""
    rows: list[tuple[float, float, float, dict[str, Any]]] = []
    synth_bars: list[list[float]] = []
    cur_min: dict[str, Any] | None = None
    last_cached_min = -1
    cached_m1: dict[str, Any] = {"bias": 0, "label": "WARMING_UP", "support": None, "resistance": None}

    with tick_jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            ts = float(rec["ts"])
            bid = float(rec["bid"])
            ask = float(rec["ask"])
            if bid <= 0.0 or ask <= 0.0 or ask < bid:
                continue
            m1_ctx = rec.get("m1") or {"bias": 0, "label": "UNKNOWN"}

            if rebuild_m1 or m1_ctx.get("label") in ("INSUFFICIENT_M1", "WARMING_UP", "UNKNOWN", None):
                mid = 0.5 * (bid + ask)
                min_ep = int(ts // 60) * 60
                if cur_min is None:
                    cur_min = {"ts": float(min_ep), "o": mid, "h": mid, "l": mid, "c": mid}
                elif min_ep > int(cur_min["ts"]):
                    synth_bars.append([cur_min["ts"], cur_min["o"], cur_min["h"], cur_min["l"], cur_min["c"], 1.0])
                    if len(synth_bars) > 200:
                        synth_bars = synth_bars[-200:]
                    cur_min = {"ts": float(min_ep), "o": mid, "h": mid, "l": mid, "c": mid}
                else:
                    cur_min["h"] = max(cur_min["h"], mid)
                    cur_min["l"] = min(cur_min["l"], mid)
                    cur_min["c"] = mid

                if min_ep != last_cached_min:
                    last_cached_min = min_ep
                    if len(synth_bars) >= 25:
                        cached_m1 = analyze_m1_sr_context(synth_bars, server_now_ts=ts)
                    else:
                        cached_m1 = _mini_m1_context(synth_bars)
                m1_ctx = cached_m1

            rows.append((ts, bid, ask, m1_ctx))
    return rows


def analyze_tape_physics(
    ticks: list[tuple[float, float, float, dict[str, Any]]],
    *,
    min_wave_usd: float = 8.0,
    max_wave_sec: float = 180.0,
    max_adverse_usd: float = 3.0,
) -> dict[str, Any]:
    """Summarize real tape physics and catalog genuine >=$8 directional waves (diagnostic only)."""
    if not ticks:
        return {
            "ticks": 0,
            "duration_min": 0.0,
            "price_low": 0.0,
            "price_high": 0.0,
            "max_abs_60s": 0.0,
            "pct_60s_ge_6": 0.0,
            "pct_60s_ge_8": 0.0,
            "pct_60s_ge_10": 0.0,
            "clean_waves": [],
        }

    # Build 1-second sampled snapshots with exact causal features (fast O(N))
    sec_rows: list[dict[str, Any]] = []
    s1_hist: deque[tuple[int, float, float]] = deque()  # (sec, close, abs_step)
    range_15s: deque[list[float]] = deque()
    upd_ts: deque[float] = deque()
    prev_q: tuple[float, float] | None = None
    last_sec = -1
    p_lo = 1e18
    p_hi = -1e18
    max_60 = 0.0
    n_ge6 = 0
    n_ge8 = 0
    n_ge10 = 0

    for ts, bid, ask, _ in ticks:
        mid = 0.5 * (bid + ask)
        if mid < p_lo:
            p_lo = mid
        if mid > p_hi:
            p_hi = mid
        if prev_q is None or bid != prev_q[0] or ask != prev_q[1]:
            upd_ts.append(ts)
            prev_q = (bid, ask)
        while upd_ts and (ts - upd_ts[0]) > 10.0:
            upd_ts.popleft()

        b_id = int(ts // 15.0)
        if not range_15s or range_15s[-1][0] < b_id:
            range_15s.append([float(b_id), mid, mid])
            while len(range_15s) > 64:
                range_15s.popleft()
        else:
            if mid > range_15s[-1][1]:
                range_15s[-1][1] = mid
            if mid < range_15s[-1][2]:
                range_15s[-1][2] = mid

        sec = int(ts)
        if sec != last_sec:
            prev_c = s1_hist[-1][1] if s1_hist else mid
            s1_hist.append((sec, mid, abs(mid - prev_c)))
            while s1_hist and (sec - s1_hist[0][0]) > 60:
                s1_hist.popleft()
            last_sec = sec
            if len(s1_hist) >= 20:
                m60 = mid - s1_hist[0][1]
                path60 = sum(x[2] for x in s1_hist)
                e60 = abs(m60) / path60 if path60 > 1e-9 else 0.0
                am60 = abs(m60)
                if am60 > max_60:
                    max_60 = am60
                if am60 >= 6.0:
                    n_ge6 += 1
                if am60 >= 8.0:
                    n_ge8 += 1
                if am60 >= 10.0:
                    n_ge10 += 1
            else:
                m60, e60 = 0.0, 0.0

            prior = [b for b in range_15s if b[0] < b_id]
            brk15 = 0
            if len(prior) >= 20:
                if mid > max(b[1] for b in prior):
                    brk15 = 1
                elif mid < min(b[2] for b in prior):
                    brk15 = -1

            sec_rows.append({
                "sec": sec,
                "mid": mid,
                "sp": round(ask - bid, 3),
                "m60": round(m60, 2),
                "e60": round(e60, 2),
                "upd10": len(upd_ts),
                "brk15": brk15,
            })

    # Post-hoc catalog of genuine non-overlapping >=$8 directional waves (strictly diagnostic!)
    clean_waves: list[dict[str, Any]] = []
    i = 0
    n_sec = len(sec_rows)
    while i < n_sec:
        r0 = sec_rows[i]
        t0 = r0["sec"]
        p0 = r0["mid"]
        best_up = 0.0
        best_up_idx = i
        min_up_adv = 0.0
        best_dn = 0.0
        best_dn_idx = i
        min_dn_adv = 0.0

        j = i + 1
        while j < n_sec and (sec_rows[j]["sec"] - t0) <= max_wave_sec:
            diff = sec_rows[j]["mid"] - p0
            # Track clean bull wave
            if min_up_adv >= -max_adverse_usd:
                if diff < min_up_adv:
                    min_up_adv = diff
                if diff > best_up and min_up_adv >= -max_adverse_usd:
                    best_up = diff
                    best_up_idx = j
            # Track clean bear wave
            if min_dn_adv <= max_adverse_usd:
                if diff > min_dn_adv:
                    min_dn_adv = diff
                if -diff > best_dn and min_dn_adv <= max_adverse_usd:
                    best_dn = -diff
                    best_dn_idx = j
            if min_up_adv < -max_adverse_usd and min_dn_adv > max_adverse_usd:
                break
            j += 1

        if best_up >= min_wave_usd or best_dn >= min_wave_usd:
            if best_up >= best_dn:
                end_r = sec_rows[best_up_idx]
                side = "BUY"
                mag = round(best_up, 2)
                jump_to = best_up_idx + 1
            else:
                end_r = sec_rows[best_dn_idx]
                side = "SELL"
                mag = round(best_dn, 2)
                jump_to = best_dn_idx + 1
            clean_waves.append({
                "start_utc": datetime.fromtimestamp(t0, tz=timezone.utc).strftime("%H:%M:%S"),
                "side": side,
                "move_usd": mag,
                "dur_sec": int(end_r["sec"] - t0),
                "start_px": round(p0, 2),
                "end_px": round(end_r["mid"], 2),
                "pre_m60": r0["m60"],
                "pre_e60": r0["e60"],
                "pre_upd10": r0["upd10"],
                "pre_brk15": r0["brk15"],
            })
            i = jump_to
        else:
            i += 5  # fast step across quiet seconds

    dur_min = max(0.0, (ticks[-1][0] - ticks[0][0]) / 60.0)
    denom = max(1, n_sec)
    return {
        "ticks": len(ticks),
        "duration_min": round(dur_min, 1),
        "price_low": round(p_lo, 2),
        "price_high": round(p_hi, 2),
        "max_abs_60s": round(max_60, 2),
        "pct_60s_ge_6": round(100.0 * n_ge6 / denom, 2),
        "pct_60s_ge_8": round(100.0 * n_ge8 / denom, 2),
        "pct_60s_ge_10": round(100.0 * n_ge10 / denom, 2),
        "clean_waves": clean_waves,
    }


ALPARI_LONDON_NY_HOURS: tuple[int, ...] = tuple(range(9, 20))
"""Active Frankfurt/London/NY institutional liquidity window on Alpari broker server clock (09:00-19:59 EET/EEST ≈ 07:00-17:59 UTC winter)."""

ALPARI_LONDON_NY_EXTENDED_HOURS: tuple[int, ...] = tuple(range(9, 21))
"""Extended London/NY window on Alpari broker server clock (09:00-20:59 EET/EEST ≈ 07:00-18:59 UTC winter)."""


@dataclass
class PullbackDipConfig:
    """Causal trend-pullback dip-buyer config (enters on -$2..-$5 dip + S1 resumption, NOT 3s tops)."""

    leg_lot: float = 0.01
    max_legs: int = 4
    multiplier: float = 100.0
    trend_window_sec: float = 300.0  # 5-min trend displacement window
    min_trend_move_price: float = 8.0  # require >= $8 net directional trend over trend_window_sec
    min_trend_efficiency: float = 0.0  # optional causal 15s-step straight-line efficiency in [0,1] (0=off)
    extreme_window_sec: float = 180.0  # recent swing extreme window (3-min)
    min_pullback_price: float = 2.0  # wait for at least $2.00 dip off recent swing extreme
    max_pullback_price: float = 5.5  # don't catch falling knives beyond $5.50 pullback
    resume_window_sec: float = 3.0  # S1 resumption bounce window
    min_resume_price: float = 0.80  # first +$0.80/3s bounce back in trend direction triggers entry
    max_spread_price: float = 0.90
    latency_sec: float = 0.25
    stop_price: float = 4.50  # fixed fallback stop when use_pivot_stop=False
    use_pivot_stop: bool = False  # causal structural stop right beyond the pullback pivot low/high
    pivot_lookback_sec: float = 45.0  # seconds to locate recent pullback trough/peak
    pivot_stop_buffer: float = 0.70  # $ clearance past pivot extreme
    min_stop_price: float = 2.00  # floor for structural pivot stop
    max_stop_price: float = 3.00  # hard cap for structural pivot stop (fixes -$4.42 AvgL asymmetry)
    be_arm_price: float = 2.50  # free-ride BE lock at +$2.50
    profit_lock_arm_price: float = 0.0  # optional mid-wave profit floor arm (0=off)
    profit_lock_floor_price: float = 0.0  # guaranteed locked profit floor once profit_lock_arm_price hit
    sl_min_breathing_room: float = 1.80
    add_step_price: float = 2.50  # risk-free pyramid leg spacing
    add_interval_sec: float = 5.0
    trail_arm_price: float = 3.50
    trail_min_pullback: float = 2.50
    trail_max_pullback: float = 5.00
    trail_giveback_frac: float = 0.30
    max_hold_sec: float = 600.0
    win_cooldown_sec: float = 15.0
    loss_cooldown_sec: float = 60.0
    consec_loss_cooldown_sec: float = 0.0  # optional step-back cooldown after >=2 consecutive losses (0=off)
    active_server_hours: tuple[int, ...] | None = None  # optional broker-server active session hours (None=24h)
    dynamic_sizing: bool = False  # Acceleration x Duration house-money lot scaling (opt-in)
    dyn_max_l1_lot: float = 0.06
    dyn_max_leg_lot: float = 0.10
    dyn_max_total_lot: float = 0.50
    dyn_min_lock_profit: float = 0.45  # gap-proof locked SL cushion above avg_entry on dynamic adds/BE
    dyn_profit_ratchet_arm: float = 3.50  # arm % profit-lock ratchet once multi-leg avg_gain >= $X
    dyn_profit_ratchet_frac: float = 0.55  # lock this fraction of open avg_gain behind breathing room


@dataclass
class PullbackDipEngine:
    """100% causal Bid/Ask simulator for M1-trend pullback-dip entry + risk-free winner pyramiding."""

    cfg: PullbackDipConfig = field(default_factory=PullbackDipConfig)
    initial_capital_usd: float = 1000.0

    # O(1) rolling histories (1-second downsampled for 5m/10m trend + raw ticks for 3s bounce)
    _ticks_3s: deque[tuple[float, float]] = field(default_factory=deque)
    _sec_hist: deque[tuple[int, float, float, float]] = field(default_factory=deque)  # (sec, hi, lo, close)
    _cur_sec: list[float] | None = None
    _last_tick_ts: float = 0.0

    pending: dict[str, Any] | None = None
    basket: dict[str, Any] | None = None
    cooldown_until: float = 0.0
    consecutive_losses: int = 0
    next_wave_id: int = 1

    realized_pnl_usd: float = 0.0
    peak_equity_usd: float = 1000.0
    max_dd_usd: float = 0.0
    waves_closed: list[dict[str, Any]] = field(default_factory=list)
    signals_blocked_session: int = 0
    signals_blocked_efficiency: int = 0
    signals_blocked_spread: int = 0
    last_metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.peak_equity_usd = float(self.initial_capital_usd)

    def unrealized_pnl_usd(self, bid: float, ask: float) -> float:
        if self.basket is None or bid <= 0 or ask <= 0:
            return 0.0
        ex = bid if self.basket["side"] == "buy" else ask
        diff = (ex - self.basket["avg_entry"]) if self.basket["side"] == "buy" else (self.basket["avg_entry"] - ex)
        return round(diff * self.basket["total_volume"] * self.cfg.multiplier, 4)

    def _unrealized(self, bid: float, ask: float) -> float:
        return self.unrealized_pnl_usd(bid, ask)

    def _update_dd(self, bid: float, ask: float) -> None:
        eq = self.initial_capital_usd + self.realized_pnl_usd + self._unrealized(bid, ask)
        if eq > self.peak_equity_usd:
            self.peak_equity_usd = eq
        dd = max(0.0, self.peak_equity_usd - eq)
        if dd > self.max_dd_usd:
            self.max_dd_usd = round(dd, 4)

    def _close(self, exit_px: float, ts: float, reason: str) -> dict[str, Any]:
        assert self.basket is not None
        b = self.basket
        diff = (exit_px - b["avg_entry"]) if b["side"] == "buy" else (b["avg_entry"] - exit_px)
        pnl = round(diff * b["total_volume"] * self.cfg.multiplier, 4)
        self.realized_pnl_usd = round(self.realized_pnl_usd + pnl, 4)
        rec = {
            "wave_id": b["wave_id"],
            "side": b["side"],
            "regime": "PULLBACK_DIP",
            "opened_at": float(b["opened_at"]),
            "opened_utc": datetime.fromtimestamp(b["opened_at"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "closed_at": float(ts),
            "closed_utc": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hold_seconds": round(max(0.0, ts - b["opened_at"]), 2),
            "legs_count": len(b["legs"]),
            "total_volume": b["total_volume"],
            "avg_entry": b["avg_entry"],
            "leg1_entry": b["legs"][0]["entry"],
            "exit_price": round(exit_px, 4),
            "stop_final": b["stop"],
            "net_pnl_usd": pnl,
            "exit_reason": reason,
            "m1_label": b["m1_label"],
            "s1_candles_elapsed": int(max(1.0, ts - b["opened_at"])),
            "equity_after_usd": round(self.initial_capital_usd + self.realized_pnl_usd, 4),
            "paper_only": True,
            "orders_submitted": 0,
        }
        self.waves_closed.append(rec)
        self.basket = None
        self.pending = None
        if pnl > 0:
            self.consecutive_losses = 0
            cd = self.cfg.win_cooldown_sec
        else:
            self.consecutive_losses += 1
            if self.cfg.consec_loss_cooldown_sec > 0.0 and self.consecutive_losses >= 2:
                cd = self.cfg.consec_loss_cooldown_sec
            else:
                cd = self.cfg.loss_cooldown_sec
        self.cooldown_until = ts + cd
        return rec

    def _can_add_risk_free(self, side: str, bid: float, ask: float, fill_px: float) -> bool:
        assert self.basket is not None
        b = self.basket
        old_v = b["total_volume"]
        new_v = round(old_v + self.cfg.leg_lot, 2)
        new_avg = (b["avg_entry"] * old_v + fill_px * self.cfg.leg_lot) / new_v
        if side == "buy":
            return (bid - self.cfg.sl_min_breathing_room) >= (new_avg + 0.05)
        return (ask + self.cfg.sl_min_breathing_room) <= (new_avg - 0.05)

    def _window_move(self, sec: int, mid: float, lookback_sec: int) -> float:
        """Causal signed price move over lookback_sec using completed 1s history."""
        t_cut = sec - lookback_sec
        for row in self._sec_hist:
            if row[0] >= t_cut:
                return mid - row[3]
        return 0.0

    def _trend_efficiency_15s(self, t_cut: int, ref_trend: float, mid: float) -> float:
        """Causal 15-second bar straight-line efficiency in [0, 1] over trend_window_sec."""
        path = 0.0
        prev_c = ref_trend
        last_sample_sec = t_cut
        for row in self._sec_hist:
            if row[0] < t_cut:
                continue
            if (row[0] - last_sample_sec) >= 15:
                path += abs(row[3] - prev_c)
                prev_c = row[3]
                last_sample_sec = row[0]
        path += abs(mid - prev_c)
        if path <= 1e-9:
            return 0.0
        return min(1.0, abs(mid - ref_trend) / path)

    def _compute_initial_stop(self, *, side: str, fill_px: float, sec: int) -> float:
        if not self.cfg.use_pivot_stop:
            return round(fill_px - self.cfg.stop_price, 4) if side == "buy" else round(fill_px + self.cfg.stop_price, 4)

        piv_cut = sec - int(self.cfg.pivot_lookback_sec)
        piv_lo = self._cur_sec[2] if self._cur_sec is not None else fill_px
        piv_hi = self._cur_sec[1] if self._cur_sec is not None else fill_px
        for row in self._sec_hist:
            if row[0] >= piv_cut:
                if row[2] < piv_lo:
                    piv_lo = row[2]
                if row[1] > piv_hi:
                    piv_hi = row[1]

        if side == "buy":
            raw_stop = piv_lo - self.cfg.pivot_stop_buffer
            raw_dist = fill_px - raw_stop
            sdist = min(max(raw_dist, self.cfg.min_stop_price), self.cfg.max_stop_price)
            return round(fill_px - sdist, 4)

        raw_stop = piv_hi + self.cfg.pivot_stop_buffer
        raw_dist = raw_stop - fill_px
        sdist = min(max(raw_dist, self.cfg.min_stop_price), self.cfg.max_stop_price)
        return round(fill_px + sdist, 4)

    def on_tick(self, *, ts: float, bid: float, ask: float, m1_context: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if bid <= 0 or ask <= 0 or ask < bid:
            return events
        mid = 0.5 * (bid + ask)
        spread = round(ask - bid, 5)
        sec = int(ts)
        srv_hour = (sec // 3600) % 24
        in_session = (self.cfg.active_server_hours is None) or (srv_hour in self.cfg.active_server_hours)

        # Reset trend/pullback history cleanly after rollover/disconnect gaps (> 90s)
        if self._last_tick_ts > 0.0 and (ts - self._last_tick_ts) > 90.0:
            self._ticks_3s.clear()
            self._sec_hist.clear()
            self._cur_sec = None
            self.pending = None
        self._last_tick_ts = ts

        # Update 3s tick history
        self._ticks_3s.append((ts, mid))
        while self._ticks_3s and (ts - self._ticks_3s[0][0]) > (self.cfg.resume_window_sec + 1.0):
            self._ticks_3s.popleft()

        # Update 1s OHLC history up to max(trend_window_sec, extreme_window_sec, 900s when dynamic_sizing) + 15s
        max_sec_keep = int(
            max(self.cfg.trend_window_sec, self.cfg.extreme_window_sec, 900.0 if self.cfg.dynamic_sizing else 0.0)
        ) + 15
        if self._cur_sec is None:
            self._cur_sec = [float(sec), mid, mid, mid]
        elif sec > int(self._cur_sec[0]):
            self._sec_hist.append((int(self._cur_sec[0]), self._cur_sec[1], self._cur_sec[2], self._cur_sec[3]))
            while self._sec_hist and (sec - self._sec_hist[0][0]) > max_sec_keep:
                self._sec_hist.popleft()
            self._cur_sec = [float(sec), mid, mid, mid]
        else:
            if mid > self._cur_sec[1]:
                self._cur_sec[1] = mid
            if mid < self._cur_sec[2]:
                self._cur_sec[2] = mid
            self._cur_sec[3] = mid

        self.last_metrics = {
            "ts": ts,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "server_hour": srv_hour,
            "in_session": in_session,
            "m1_bias": int(m1_context.get("bias", 0)),
            "m1_label": str(m1_context.get("label", "UNKNOWN")),
        }

        # 1) Execute pending fill after latency
        if self.pending is not None and ts >= self.pending["fill_ts"]:
            po = self.pending
            self.pending = None
            side = po["side"]
            fill_px = ask if side == "buy" else bid
            if spread <= self.cfg.max_spread_price:
                if po["kind"] == "open" and self.basket is None:
                    stop_px = self._compute_initial_stop(side=side, fill_px=fill_px, sec=sec)
                    if self.cfg.dynamic_sizing:
                        m60 = abs(self._window_move(sec, mid, 60))
                        m5 = max(abs(float(po.get("trend_move", 0.0))), abs(self._window_move(sec, mid, 300)))
                        m15 = abs(self._window_move(sec, mid, 900))
                        l1_vol = compute_dynamic_l1_lot(
                            base_lot=self.cfg.leg_lot,
                            max_l1_lot=self.cfg.dyn_max_l1_lot,
                            max_total_lot=self.cfg.dyn_max_total_lot,
                            abs_move_60s=m60,
                            abs_move_5m=m5,
                            abs_move_15m=m15,
                        )
                    else:
                        l1_vol = round(self.cfg.leg_lot, 2)
                    self.basket = {
                        "wave_id": self.next_wave_id,
                        "side": side,
                        "regime": "PULLBACK_DIP",
                        "legs": [{"entry": round(fill_px, 4), "volume": l1_vol, "ts": ts}],
                        "total_volume": l1_vol,
                        "avg_entry": round(fill_px, 4),
                        "stop": stop_px,
                        "stop_locked": False,
                        "opened_at": ts,
                        "last_leg_ts": ts,
                        "best_px": bid if side == "buy" else ask,
                        "m1_label": po["m1_label"],
                    }
                    self.next_wave_id += 1
                    events.append({
                        "type": "OPEN_L1",
                        "wave_id": self.basket["wave_id"],
                        "side": side,
                        "regime": "PULLBACK_DIP",
                        "entry": round(fill_px, 4),
                        "stop": stop_px,
                        "leg_lot": l1_vol,
                        "trend_move": round(float(po.get("trend_move", 0.0)), 2),
                        "trend_eff": round(float(po.get("trend_eff", 0.0)), 2),
                        "pullback_depth": round(float(po.get("pullback_depth", 0.0)), 2),
                        "bounce_3s": round(float(po.get("bounce_3s", 0.0)), 2),
                        "m1_label": po["m1_label"],
                        "spread": spread,
                    })
                elif po["kind"] == "add" and self.basket is not None:
                    b = self.basket
                    if len(b["legs"]) < self.cfg.max_legs:
                        old_v = float(b["total_volume"])
                        if self.cfg.dynamic_sizing:
                            s_sign = 1.0 if side == "buy" else -1.0
                            add_vol = compute_dynamic_add_lot(
                                side=side,
                                bid=bid,
                                ask=ask,
                                fill_px=fill_px,
                                old_vol=old_v,
                                old_avg=float(b["avg_entry"]),
                                breathing_room=self.cfg.sl_min_breathing_room,
                                base_lot=self.cfg.leg_lot,
                                max_leg_lot=self.cfg.dyn_max_leg_lot,
                                max_total_lot=self.cfg.dyn_max_total_lot,
                                dir_move_60s=s_sign * self._window_move(sec, mid, 60),
                                dir_move_5m=s_sign * self._window_move(sec, mid, 300),
                                hold_sec=max(0.0, ts - float(b["opened_at"])),
                                min_lock_profit=self.cfg.dyn_min_lock_profit,
                            )
                            rf_ok = add_vol >= 0.01
                        else:
                            add_vol = round(self.cfg.leg_lot, 2)
                            rf_ok = self._can_add_risk_free(side, bid, ask, fill_px)
                        if rf_ok:
                            last_ent = float(b["legs"][-1]["entry"])
                            last_net = (bid - last_ent) if side == "buy" else (last_ent - ask)
                            new_v = round(old_v + add_vol, 2)
                            new_avg = round((b["avg_entry"] * old_v + fill_px * add_vol) / new_v, 4)
                            b["legs"].append({"entry": round(fill_px, 4), "volume": add_vol, "ts": ts})
                            b["total_volume"] = new_v
                            b["avg_entry"] = new_avg
                            b["last_leg_ts"] = ts
                            lock_pad = max(0.08, self.cfg.dyn_min_lock_profit) if self.cfg.dynamic_sizing else 0.08
                            if side == "buy":
                                lock_sl = min(new_avg + lock_pad, bid - self.cfg.sl_min_breathing_room)
                                b["stop"] = round(max(b["stop"], lock_sl), 4)
                            else:
                                lock_sl = max(new_avg - lock_pad, ask + self.cfg.sl_min_breathing_room)
                                b["stop"] = round(min(b["stop"], lock_sl), 4)
                            b["stop_locked"] = True
                            events.append({
                                "type": "FIRE_ADD",
                                "wave_id": b["wave_id"],
                                "side": side,
                                "regime": "PULLBACK_DIP",
                                "leg_num": len(b["legs"]),
                                "leg_lot": add_vol,
                                "add_price": round(fill_px, 4),
                                "total_volume": new_v,
                                "avg_entry": new_avg,
                                "stop": b["stop"],
                                "last_leg_net": round(last_net, 3),
                            })

        # 2) Manage open basket (always managed regardless of session hour!)
        if self.basket is not None:
            b = self.basket
            side = b["side"]
            ex_px = bid if side == "buy" else ask
            hold_sec = ts - b["opened_at"]
            if side == "buy":
                b["best_px"] = max(b["best_px"], bid)
                avg_gain = bid - b["avg_entry"]
                max_exc = b["best_px"] - b["legs"][0]["entry"]
                pb = b["best_px"] - bid
            else:
                b["best_px"] = min(b["best_px"], ask)
                avg_gain = b["avg_entry"] - ask
                max_exc = b["legs"][0]["entry"] - b["best_px"]
                pb = ask - b["best_px"]

            # 1L Free-Ride BE arm
            if len(b["legs"]) == 1 and not b["stop_locked"] and avg_gain >= self.cfg.be_arm_price:
                be_pad = max(0.08, self.cfg.dyn_min_lock_profit) if self.cfg.dynamic_sizing else 0.08
                if side == "buy":
                    t_sl = min(b["avg_entry"] + be_pad, bid - self.cfg.sl_min_breathing_room)
                    if t_sl > b["stop"]:
                        b["stop"] = round(t_sl, 4)
                        b["stop_locked"] = True
                else:
                    t_sl = max(b["avg_entry"] - be_pad, ask + self.cfg.sl_min_breathing_room)
                    if t_sl < b["stop"]:
                        b["stop"] = round(t_sl, 4)
                        b["stop_locked"] = True

            # Optional Profit-Lock Floor Ratchet (prevents +$3.80 winners collapsing to +$0.08 BE!)
            if (
                self.cfg.profit_lock_arm_price > 0.0
                and self.cfg.profit_lock_floor_price > 0.0
                and avg_gain >= self.cfg.profit_lock_arm_price
            ):
                if side == "buy":
                    p_sl = min(b["avg_entry"] + self.cfg.profit_lock_floor_price, bid - self.cfg.sl_min_breathing_room)
                    if p_sl > b["stop"]:
                        b["stop"] = round(p_sl, 4)
                        b["stop_locked"] = True
                else:
                    p_sl = max(b["avg_entry"] - self.cfg.profit_lock_floor_price, ask + self.cfg.sl_min_breathing_room)
                    if p_sl < b["stop"]:
                        b["stop"] = round(p_sl, 4)
                        b["stop_locked"] = True

            # Dynamic Multi-Leg Profit-Lock Ratchet (locks % of big pyramided surges behind >=$2.20 breathing room)
            if (
                self.cfg.dynamic_sizing
                and self.cfg.dyn_profit_ratchet_frac > 0.0
                and len(b["legs"]) >= 2
                and avg_gain >= self.cfg.dyn_profit_ratchet_arm
            ):
                r_breath = max(self.cfg.sl_min_breathing_room, 2.20)
                if side == "buy":
                    r_sl = min(b["avg_entry"] + self.cfg.dyn_profit_ratchet_frac * avg_gain, bid - r_breath)
                    if r_sl > b["stop"]:
                        b["stop"] = round(r_sl, 4)
                        b["stop_locked"] = True
                else:
                    r_sl = max(b["avg_entry"] - self.cfg.dyn_profit_ratchet_frac * avg_gain, ask + r_breath)
                    if r_sl < b["stop"]:
                        b["stop"] = round(r_sl, 4)
                        b["stop_locked"] = True

            # Hard / Locked SL
            if (side == "buy" and bid <= b["stop"]) or (side == "sell" and ask >= b["stop"]):
                reason = ("profit_floor_hit" if avg_gain >= 0 else "locked_sl_gap_slip") if b["stop_locked"] else "hard_stop_loss"
                closed = self._close(ex_px, ts, reason)
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Tick-Trail with capped pullback
            if avg_gain >= self.cfg.trail_arm_price or (b["stop_locked"] and max_exc >= self.cfg.trail_arm_price):
                allow_pb = min(
                    max(self.cfg.trail_min_pullback, self.cfg.trail_giveback_frac * max(0.0, max_exc)),
                    self.cfg.trail_max_pullback,
                )
                if pb >= allow_pb and avg_gain > 0:
                    closed = self._close(ex_px, ts, "tick_trail_exit")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events

            # Max Hold
            if hold_sec >= self.cfg.max_hold_sec:
                closed = self._close(ex_px, ts, "max_hold_exit")
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Risk-Free Pyramid Add
            if (
                self.pending is None
                and len(b["legs"]) < self.cfg.max_legs
                and (ts - b["last_leg_ts"]) >= self.cfg.add_interval_sec
                and spread <= self.cfg.max_spread_price
            ):
                last_ent = b["legs"][-1]["entry"]
                last_net = (bid - last_ent) if side == "buy" else (last_ent - ask)
                est_fill = ask if side == "buy" else bid
                if self.cfg.dynamic_sizing:
                    s_sign = 1.0 if side == "buy" else -1.0
                    rf_ok = (
                        compute_dynamic_add_lot(
                            side=side,
                            bid=bid,
                            ask=ask,
                            fill_px=est_fill,
                            old_vol=float(b["total_volume"]),
                            old_avg=float(b["avg_entry"]),
                            breathing_room=self.cfg.sl_min_breathing_room,
                            base_lot=self.cfg.leg_lot,
                            max_leg_lot=self.cfg.dyn_max_leg_lot,
                            max_total_lot=self.cfg.dyn_max_total_lot,
                            dir_move_60s=s_sign * self._window_move(sec, mid, 60),
                            dir_move_5m=s_sign * self._window_move(sec, mid, 300),
                            hold_sec=hold_sec,
                            min_lock_profit=self.cfg.dyn_min_lock_profit,
                        )
                        >= 0.01
                    )
                else:
                    rf_ok = self._can_add_risk_free(side, bid, ask, est_fill)
                if last_net >= self.cfg.add_step_price and rf_ok:
                    self.pending = {"kind": "add", "side": side, "fill_ts": ts + self.cfg.latency_sec}

            self._update_dd(bid, ask)
            return events

        # 3) Flat: evaluate causal trend + pullback dip + S1 resumption bounce
        if self.pending is not None or ts < self.cooldown_until:
            self._update_dd(bid, ask)
            return events
        if len(self._sec_hist) < int(self.cfg.trend_window_sec * 0.5) or len(self._ticks_3s) < 2:
            self._update_dd(bid, ask)
            return events

        # Find trend move over trend_window_sec
        t_cut = sec - int(self.cfg.trend_window_sec)
        ref_trend = None
        for row in self._sec_hist:
            if row[0] >= t_cut:
                ref_trend = row[3]
                break
        if ref_trend is None:
            self._update_dd(bid, ask)
            return events
        trend_move = mid - ref_trend
        self.last_metrics["trend_move"] = round(trend_move, 2)
        if abs(trend_move) < self.cfg.min_trend_move_price:
            self._update_dd(bid, ask)
            return events
        tdir = 1 if trend_move > 0 else -1

        # Require matching M1 bias (strictly aligned with macro M1 trend)
        m1_bias = int(m1_context.get("bias", 0))
        if m1_bias != tdir:
            self._update_dd(bid, ask)
            return events

        # Compute recent extreme over extreme_window_sec (excluding last 3s so dip is real)
        ext_cut = sec - int(self.cfg.extreme_window_sec)
        ext_hi = -1e18
        ext_lo = 1e18
        ext_n = 0
        for row in self._sec_hist:
            if ext_cut <= row[0] <= (sec - 3):
                if row[1] > ext_hi:
                    ext_hi = row[1]
                if row[2] < ext_lo:
                    ext_lo = row[2]
                ext_n += 1
        if ext_n < 15:
            self._update_dd(bid, ask)
            return events

        pullback_depth = (ext_hi - mid) if tdir == 1 else (mid - ext_lo)
        self.last_metrics["pullback_depth"] = round(pullback_depth, 2)
        if not (self.cfg.min_pullback_price <= pullback_depth <= self.cfg.max_pullback_price):
            self._update_dd(bid, ask)
            return events

        # S1 resumption bounce over resume_window_sec
        cut_3s = ts - self.cfg.resume_window_sec
        ref_3s = self._ticks_3s[0][1]
        for t_ts, t_mid in self._ticks_3s:
            if t_ts >= cut_3s:
                ref_3s = t_mid
                break
        bounce_3s = (mid - ref_3s) * tdir
        self.last_metrics["bounce_3s"] = round(bounce_3s, 2)
        if bounce_3s >= self.cfg.min_resume_price:
            if spread > self.cfg.max_spread_price:
                self.signals_blocked_spread += 1
            elif not in_session:
                self.signals_blocked_session += 1
            else:
                trend_eff = self._trend_efficiency_15s(t_cut, ref_trend, mid)
                self.last_metrics["trend_eff"] = round(trend_eff, 2)
                if self.cfg.min_trend_efficiency > 0.0 and trend_eff < self.cfg.min_trend_efficiency:
                    self.signals_blocked_efficiency += 1
                else:
                    self.pending = {
                        "kind": "open",
                        "side": "buy" if tdir == 1 else "sell",
                        "fill_ts": ts + self.cfg.latency_sec,
                        "m1_label": str(m1_context.get("label", "UNKNOWN")),
                        "trend_move": trend_move,
                        "trend_eff": trend_eff,
                        "pullback_depth": pullback_depth,
                        "bounce_3s": bounce_3s,
                    }

        self._update_dd(bid, ask)
        return events

    def summary(self, bid: float = 0.0, ask: float = 0.0) -> dict[str, Any]:
        wins = [w for w in self.waves_closed if w["net_pnl_usd"] > 0]
        losses = [w for w in self.waves_closed if w["net_pnl_usd"] <= 0]
        gw = sum(w["net_pnl_usd"] for w in wins)
        gl = abs(sum(w["net_pnl_usd"] for w in losses))
        pf = round(gw / gl, 3) if gl > 0 else (99.0 if gw > 0 else None)
        total_legs = sum(int(w["legs_count"]) for w in self.waves_closed)
        return {
            "paper_only": True,
            "orders_submitted": 0,
            "initial_capital_usd": self.initial_capital_usd,
            "realized_pnl_usd": self.realized_pnl_usd,
            "unrealized_pnl_usd": self.unrealized_pnl_usd(bid, ask),
            "equity_usd": round(self.initial_capital_usd + self.realized_pnl_usd + self.unrealized_pnl_usd(bid, ask), 4),
            "max_dd_usd": self.max_dd_usd,
            "waves_count": len(self.waves_closed),
            "total_legs_executed": total_legs,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(100.0 * len(wins) / len(self.waves_closed), 1) if self.waves_closed else 0.0,
            "pf": pf,
            "avg_win_usd": round(gw / len(wins), 2) if wins else 0.0,
            "avg_loss_usd": round(-gl / len(losses), 2) if losses else 0.0,
            "signals_blocked_spread": self.signals_blocked_spread,
            "signals_blocked_session": self.signals_blocked_session,
            "signals_blocked_efficiency": self.signals_blocked_efficiency,
            "open_basket": self.basket,
            "last_metrics": self.last_metrics,
        }


def pullback_dip_profiles(
    *,
    leg_lot: float = 0.01,
    max_legs: int = 4,
) -> list[tuple[str, PullbackDipConfig]]:
    """Canonical Pullback-Dip configs: V1 baseline + non-overfit V2 (pivot SL + R:R + trend eff + London/NY session)."""
    return [
        (
            "PULLBACK_DIP_5M",
            PullbackDipConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                trend_window_sec=300.0,
                min_trend_move_price=8.0,
                extreme_window_sec=180.0,
                min_pullback_price=2.0,
                max_pullback_price=5.5,
                min_resume_price=0.80,
                stop_price=4.50,
                be_arm_price=2.50,
            ),
        ),
        (
            "PULLBACK_DIP_10M",
            PullbackDipConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                trend_window_sec=600.0,
                min_trend_move_price=12.0,
                extreme_window_sec=300.0,
                min_pullback_price=2.5,
                max_pullback_price=7.0,
                min_resume_price=1.00,
                stop_price=5.50,
                be_arm_price=3.00,
                trail_arm_price=4.00,
                trail_min_pullback=3.00,
                trail_max_pullback=6.00,
            ),
        ),
        (
            "PB_DIP_V2_24H",
            PullbackDipConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                trend_window_sec=300.0,
                min_trend_move_price=8.0,
                min_trend_efficiency=0.30,
                extreme_window_sec=180.0,
                min_pullback_price=2.0,
                max_pullback_price=4.8,
                min_resume_price=0.85,
                use_pivot_stop=True,
                pivot_lookback_sec=45.0,
                pivot_stop_buffer=0.70,
                min_stop_price=2.00,
                max_stop_price=3.00,
                be_arm_price=2.40,
                profit_lock_arm_price=3.80,
                profit_lock_floor_price=1.40,
                sl_min_breathing_room=1.60,
                add_step_price=2.20,
                add_interval_sec=5.0,
                trail_arm_price=4.20,
                trail_min_pullback=2.20,
                trail_max_pullback=4.50,
                trail_giveback_frac=0.28,
                max_hold_sec=600.0,
                win_cooldown_sec=15.0,
                loss_cooldown_sec=60.0,
                consec_loss_cooldown_sec=180.0,
                active_server_hours=None,
            ),
        ),
        (
            "PB_DIP_V2_SESS",
            PullbackDipConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                trend_window_sec=300.0,
                min_trend_move_price=8.0,
                min_trend_efficiency=0.30,
                extreme_window_sec=180.0,
                min_pullback_price=2.0,
                max_pullback_price=4.8,
                min_resume_price=0.85,
                use_pivot_stop=True,
                pivot_lookback_sec=45.0,
                pivot_stop_buffer=0.70,
                min_stop_price=2.00,
                max_stop_price=3.00,
                be_arm_price=2.40,
                profit_lock_arm_price=3.80,
                profit_lock_floor_price=1.40,
                sl_min_breathing_room=1.60,
                add_step_price=2.20,
                add_interval_sec=5.0,
                trail_arm_price=4.20,
                trail_min_pullback=2.20,
                trail_max_pullback=4.50,
                trail_giveback_frac=0.28,
                max_hold_sec=600.0,
                win_cooldown_sec=15.0,
                loss_cooldown_sec=60.0,
                consec_loss_cooldown_sec=180.0,
                active_server_hours=ALPARI_LONDON_NY_HOURS,
            ),
        ),
        (
            "PB_DIP_V2_SAFE",
            PullbackDipConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                trend_window_sec=300.0,
                min_trend_move_price=9.0,
                min_trend_efficiency=0.35,
                extreme_window_sec=180.0,
                min_pullback_price=2.0,
                max_pullback_price=4.5,
                min_resume_price=0.90,
                use_pivot_stop=True,
                pivot_lookback_sec=45.0,
                pivot_stop_buffer=0.65,
                min_stop_price=1.90,
                max_stop_price=2.60,
                be_arm_price=2.20,
                profit_lock_arm_price=3.50,
                profit_lock_floor_price=1.30,
                sl_min_breathing_room=1.50,
                add_step_price=2.20,
                add_interval_sec=5.0,
                trail_arm_price=4.00,
                trail_min_pullback=2.00,
                trail_max_pullback=4.20,
                trail_giveback_frac=0.28,
                max_hold_sec=600.0,
                win_cooldown_sec=20.0,
                loss_cooldown_sec=75.0,
                consec_loss_cooldown_sec=240.0,
                active_server_hours=ALPARI_LONDON_NY_HOURS,
            ),
        ),
        (
            "PB_V2_DYN_SESS",
            PullbackDipConfig(
                leg_lot=max(0.02, leg_lot),
                max_legs=max(12, max_legs),
                trend_window_sec=300.0,
                min_trend_move_price=8.0,
                min_trend_efficiency=0.30,
                extreme_window_sec=180.0,
                min_pullback_price=2.0,
                max_pullback_price=4.8,
                min_resume_price=0.85,
                use_pivot_stop=True,
                pivot_lookback_sec=45.0,
                pivot_stop_buffer=0.70,
                min_stop_price=2.00,
                max_stop_price=3.00,
                be_arm_price=2.40,
                profit_lock_arm_price=3.80,
                profit_lock_floor_price=1.40,
                sl_min_breathing_room=1.60,
                add_step_price=2.20,
                add_interval_sec=5.0,
                trail_arm_price=4.20,
                trail_min_pullback=2.20,
                trail_max_pullback=4.50,
                trail_giveback_frac=0.28,
                max_hold_sec=600.0,
                win_cooldown_sec=15.0,
                loss_cooldown_sec=60.0,
                consec_loss_cooldown_sec=180.0,
                active_server_hours=ALPARI_LONDON_NY_HOURS,
                dynamic_sizing=True,
                dyn_max_l1_lot=0.02,
                dyn_max_leg_lot=0.10,
                dyn_max_total_lot=0.50,
                dyn_min_lock_profit=0.45,
            ),
        ),
        (
            "MACRO_WAVE_DYN",
            PullbackDipConfig(
                leg_lot=max(0.02, leg_lot),
                max_legs=max(14, max_legs),
                trend_window_sec=300.0,
                min_trend_move_price=8.0,
                min_trend_efficiency=0.30,
                extreme_window_sec=180.0,
                min_pullback_price=2.0,
                max_pullback_price=5.0,
                min_resume_price=0.85,
                use_pivot_stop=True,
                pivot_lookback_sec=45.0,
                pivot_stop_buffer=0.70,
                min_stop_price=2.20,
                max_stop_price=3.20,
                be_arm_price=2.60,
                profit_lock_arm_price=4.50,
                profit_lock_floor_price=1.80,
                sl_min_breathing_room=1.60,
                add_step_price=2.00,
                add_interval_sec=4.0,
                trail_arm_price=5.50,
                trail_min_pullback=3.20,
                trail_max_pullback=6.50,
                trail_giveback_frac=0.34,
                max_hold_sec=900.0,
                win_cooldown_sec=15.0,
                loss_cooldown_sec=60.0,
                consec_loss_cooldown_sec=180.0,
                active_server_hours=ALPARI_LONDON_NY_HOURS,
                dynamic_sizing=True,
                dyn_max_l1_lot=0.02,
                dyn_max_leg_lot=0.10,
                dyn_max_total_lot=0.60,
                dyn_min_lock_profit=0.45,
            ),
        ),
    ]


def build_pullback_dip_profile_config(
    profile_name: str,
    *,
    leg_lot: float = 0.01,
    max_legs: int = 4,
) -> PullbackDipConfig:
    """Resolve a canonical Pullback-Dip profile name to its exact PullbackDipConfig."""
    by_name = dict(pullback_dip_profiles(leg_lot=leg_lot, max_legs=max_legs))
    if profile_name not in by_name:
        valid = ", ".join(by_name.keys())
        raise ValueError(f"Unknown Pullback-Dip profile '{profile_name}'. Valid profiles: {valid}")
    return by_name[profile_name]


def _summarize_waves(name: str, waves: list[dict[str, Any]], max_dd_usd: float) -> dict[str, Any]:
    wins = [w["net_pnl_usd"] for w in waves if w["net_pnl_usd"] > 0]
    losses = [w["net_pnl_usd"] for w in waves if w["net_pnl_usd"] <= 0]
    gw = sum(wins)
    gl = abs(sum(losses))
    pf = round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else None)
    max_lot = round(max((float(w.get("total_volume", 0.0)) for w in waves), default=0.0), 2)
    return {
        "strategy": name,
        "waves_count": len(waves),
        "total_legs": sum(int(w["legs_count"]) for w in waves),
        "max_lot": max_lot,
        "wins": len(wins),
        "losses": len(losses),
        "wr_pct": round(100.0 * len(wins) / len(waves), 1) if waves else 0.0,
        "net_usd": round(sum(w["net_pnl_usd"] for w in waves), 2),
        "pf": pf,
        "max_dd_usd": round(max_dd_usd, 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "waves": waves,
    }


def combine_portfolio_waves(name: str, wave_lists: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Chronologically merge closed waves from independent side-by-side engines to compute exact combined stats."""
    merged = [w for wl in wave_lists for w in wl]
    merged.sort(key=lambda w: (float(w.get("closed_at", 0.0)), str(w.get("closed_utc", ""))))
    eq = 0.0
    pk = 0.0
    mdd = 0.0
    for w in merged:
        eq += float(w["net_pnl_usd"])
        if eq > pk:
            pk = eq
        mdd = max(mdd, pk - eq)
    return _summarize_waves(name, merged, mdd)


def _s1_shock_cfg(
    *,
    leg_lot: float,
    max_legs: int,
    min_60s: float,
    min_eff_60s: float,
    req_brk15: bool = False,
    min_upd10: int = 0,
    fast_cut: bool = False,
) -> S1SurferConfig:
    return S1SurferConfig(
        leg_lot=leg_lot,
        max_legs=max_legs,
        enable_news_c1=False,
        vel_mult=3.0,
        min_burst_price=1.20,
        min_60s_move_price=min_60s,
        min_60s_efficiency=min_eff_60s,
        require_15m_breakout=req_brk15,
        min_updates_10s=min_upd10,
        add_interval_sec=2.0,
        add_min_profit_price=1.00,
        min_stop_price=1.80,
        max_stop_price=2.60,
        decel_loss_cut_sec=6.0,
        disable_decel_exit=True,
        decel_only_in_loss_1l=fast_cut,
        loss_cut_min_loss_price=0.85,
        candle_cap_adds=False,
        sl_min_breathing_room=1.10,
        be_arm_price=1.40,
        require_be_on_add=True,
        s1_reversal_cut_body=0.0,
        trail_arm_price=1.80,
        trail_min_pullback=1.35,
        trail_max_pullback_price=3.60,
        trail_giveback_frac=0.32,
        multi_leg_giveback_frac=0.22,
        max_hold_sec=240.0,
        loss_cooldown_sec=30.0,
        consec_loss_cooldown_sec=60.0,
    )


def run_structural_benchmark(
    ticks: list[tuple[float, float, float, dict[str, Any]]],
    *,
    leg_lot: float = 0.01,
    max_legs: int = 6,
) -> list[dict[str, Any]]:
    """Evaluate 23 distinct causal architectures (fixed baselines, dynamic news runners, S10 hybrids, and calm M1/M3/M5)."""
    from trade_mcp.calm_regime_engines import CalmRegimeEngine, calm_regime_profiles
    from trade_mcp.s10_hybrid_engines import S10HybridEngine, s10_hybrid_profiles

    s1_specs: list[tuple[str, S1SurferConfig]] = [
        ("BASE_SAFE_RIP4", build_s1_profile_config("v2.4_SAFE_RIP4", leg_lot=leg_lot, max_legs=max_legs)),
        ("SHOCK_8_EFF45", build_s1_profile_config("v2.5_SHOCK_8", leg_lot=leg_lot, max_legs=max_legs)),
        ("SHOCK_10_EFF50", build_s1_profile_config("v2.5_SHOCK_10", leg_lot=leg_lot, max_legs=max_legs)),
        ("SHOCK_10_RUNNER", build_s1_profile_config("v2.5_SHOCK_10_RUNNER", leg_lot=leg_lot, max_legs=max_legs)),
        ("SHOCK10_DYN_RUN", build_s1_profile_config("v2.6_SHOCK10_DYN", leg_lot=leg_lot, max_legs=max_legs)),
        ("SHOCK_12_EFF50", _s1_shock_cfg(leg_lot=leg_lot, max_legs=max_legs, min_60s=12.0, min_eff_60s=0.50)),
        (
            "BRK15M_RIP6_E40",
            _s1_shock_cfg(leg_lot=leg_lot, max_legs=max_legs, min_60s=6.0, min_eff_60s=0.40, req_brk15=True),
        ),
        (
            "BRK15M_RIP8_E45",
            _s1_shock_cfg(leg_lot=leg_lot, max_legs=max_legs, min_60s=8.0, min_eff_60s=0.45, req_brk15=True),
        ),
    ]
    dip_specs = pullback_dip_profiles(leg_lot=leg_lot, max_legs=min(4, max_legs))
    hybrid_specs = s10_hybrid_profiles(leg_lot=leg_lot, max_legs=min(3, max_legs))

    results: list[dict[str, Any]] = []
    for name, cfg in s1_specs:
        eng = S1VelocitySurferEngine(cfg=cfg)
        for ts, bid, ask, m1_ctx in ticks:
            eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        if eng.basket is not None and ticks:
            ts_l, b_l, a_l, _ = ticks[-1]
            ex = b_l if eng.basket["side"] == "buy" else a_l
            eng._close_basket(ex, ts_l, "eof_replay")
        results.append(_summarize_waves(name, eng.waves_closed, eng.max_dd_usd))

    for name, dcfg in dip_specs:
        deng = PullbackDipEngine(cfg=dcfg)
        for ts, bid, ask, m1_ctx in ticks:
            deng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        if deng.basket is not None and ticks:
            ts_l, b_l, a_l, _ = ticks[-1]
            ex = b_l if deng.basket["side"] == "buy" else a_l
            deng._close(ex, ts_l, "eof_replay")
        results.append(_summarize_waves(name, deng.waves_closed, deng.max_dd_usd))

    for name, hcfg in hybrid_specs:
        heng = S10HybridEngine(cfg=hcfg)
        for ts, bid, ask, m1_ctx in ticks:
            heng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        if heng.basket is not None and ticks:
            ts_l, b_l, a_l, _ = ticks[-1]
            ex = b_l if heng.basket["side"] == "buy" else a_l
            heng._close(ex, ts_l, "eof_replay")
        results.append(_summarize_waves(name, heng.waves_closed, heng.max_dd_usd))

    calm_specs = calm_regime_profiles(leg_lot=max(0.02, leg_lot), max_legs=max(8, max_legs))
    for name, ccfg in calm_specs:
        ceng = CalmRegimeEngine(cfg=ccfg)
        for ts, bid, ask, m1_ctx in ticks:
            ceng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        if ceng.basket is not None and ticks:
            ts_l, b_l, a_l, _ = ticks[-1]
            ex = b_l if ceng.basket["side"] == "buy" else a_l
            ceng._close(ex, ts_l, "eof_replay")
        results.append(_summarize_waves(name, ceng.waves_closed, ceng.max_dd_usd))

    return results
