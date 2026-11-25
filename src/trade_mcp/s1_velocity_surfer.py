"""S1 Velocity Surfer v2: M1 S/R + S1 Candle-Count (C1/C2/C3/C4) Playbook + Winner Machine-Gun + Decel Exit.

Faithfully implements the dual-chart M1 + 1-Second (S1) tape playbook with strict
real-account protections and ZERO broker orders (Paper-only + tick recorder/replay):

  1. Chart 1 (M1 closed bars): trend direction, Swing Support/Resistance, and pullback-resume.
  2. Chart 2 (S1 1-second OHLC candles built from 0.25s-0.30s live Bid/Ask ticks):
     - Normal Session Regime (`NORMAL_C3`): waits for 2 consecutive same-direction S1
       candles (C1, C2) with >= vel_mult (2.5x) acceleration, then enters on Candle 3 (C3)
       when C3 continues in the M1 direction.
     - News / Session-Open Steep Shock Regime (`NEWS_STEEP_C1`): when C1 prints an
       explosive steep candle (>= news_c1_mult x baseline body) aligned with M1:
         * C1-C2 Probe: opens light probe (capped at probe_max_legs=2) and waits for C2
           to confirm direction — if C2 reverses (fake news wick), cuts IMMEDIATELY!
         * C3 Full-Send: once C2 confirms and prior leg is in net profit, unlocks rapid
           winner-only legs up to max_legs (6) with automatic BE / profit-floor SL lock.
         * C4 Slope-Adaptive Exhaustion Guard:
             - Extreme vertical blow-off (>= extreme_slope_mult): stops adding after C3
               and tightens deceleration/trail exit immediately before V-snapback.
             - Moderate/good slope: C4 Caution mode locks 50% of unrealized profit and
               blocks new adds on C5+ (exit management only).
  3. Instant Deceleration Exit: closes ALL open legs when 2-second directional speed
     drops below decel_ratio * peak_vel, or on tight tick-trail / locked profit-floor / hard SL.
"""

from __future__ import annotations

import json
import os
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .m1_momentum import atr_series, clean_ohlcv_bars, ema_series


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def analyze_m1_sr_context(
    m1_bars: list[list[float]],
    *,
    server_now_ts: float | None = None,
    sr_lookback: int = 15,
) -> dict[str, Any]:
    """Evaluate strictly closed M1 bars for trend direction, pullback-resume, and Swing S/R."""
    clean = clean_ohlcv_bars(m1_bars)
    if server_now_ts is not None:
        closed = [b for b in clean if (float(b[0]) + 60.0) <= (server_now_ts + 1.0)]
    else:
        closed = clean[:-1] if len(clean) > 1 else []

    if len(closed) < max(25, sr_lookback + 2):
        return {
            "bias": 0,
            "label": "INSUFFICIENT_M1",
            "support": None,
            "resistance": None,
            "atr_m1": 0.0,
            "pullback_resume": False,
            "last_close": 0.0,
        }

    window = closed[-60:]
    closes = [b[4] for b in window]
    ema9 = [float(v) if v is not None else closes[i] for i, v in enumerate(ema_series(closes, 9))]
    ema21 = [float(v) if v is not None else closes[i] for i, v in enumerate(ema_series(closes, 21))]
    atr_vals = [float(v) if v is not None else 0.0 for v in atr_series(window, period=14)]

    last = window[-1]
    prev_sr = window[-(sr_lookback + 1) : -1]
    sup = min(b[3] for b in prev_sr)
    res = max(b[2] for b in prev_sr)
    atr_now = max(atr_vals[-1], 0.10)
    c = float(last[4])
    o = float(last[1])
    h = float(last[2])
    low = float(last[3])

    recent5 = window[-6:-1]
    had_green_pb = any(b[4] > b[1] for b in recent5)
    had_red_pb = any(b[4] < b[1] for b in recent5)
    slope10 = c - window[-11][4]

    bias = 0
    label = "CHOP_NEUTRAL"
    pb_resume = False

    if (ema9[-1] < ema21[-1] and c <= ema21[-1] and slope10 <= 0) or (c < sup and c < o):
        lower_wick = min(o, c) - low
        rng = max(h - low, 1e-9)
        dist_to_sup = c - sup
        if 0.0 < dist_to_sup < (0.25 * atr_now) and (lower_wick / rng) >= 0.45:
            bias = 0
            label = "BEAR_AT_SUPPORT_WAIT"
        else:
            bias = -1
            if c < sup:
                label = "BEAR_SUP_BREAK"
            elif had_green_pb and c < o:
                label = "BEAR_PB_RESUME"
                pb_resume = True
            else:
                label = "BEAR_TREND"

    elif (ema9[-1] > ema21[-1] and c >= ema21[-1] and slope10 >= 0) or (c > res and c > o):
        upper_wick = h - max(o, c)
        rng = max(h - low, 1e-9)
        dist_to_res = res - c
        if 0.0 < dist_to_res < (0.25 * atr_now) and (upper_wick / rng) >= 0.45:
            bias = 0
            label = "BULL_AT_RESISTANCE_WAIT"
        else:
            bias = 1
            if c > res:
                label = "BULL_RES_BREAK"
            elif had_red_pb and c > o:
                label = "BULL_PB_RESUME"
                pb_resume = True
            else:
                label = "BULL_TREND"

    return {
        "bias": bias,
        "label": label,
        "support": round(sup, 2),
        "resistance": round(res, 2),
        "atr_m1": round(atr_now, 3),
        "pullback_resume": pb_resume,
        "last_close": round(c, 2),
    }


@dataclass
class S1SurferConfig:
    leg_lot: float = 0.01
    max_legs: int = 6
    probe_max_legs: int = 2  # light probe cap during news C1-C2 before C2 confirms
    multiplier: float = 100.0
    burst_window_sec: float = 3.0
    decel_window_sec: float = 2.0
    baseline_sec: int = 90
    min_baseline_samples: int = 12
    base_vel_floor: float = 0.18  # minimum baseline $/3s
    base_s1_body_floor: float = 0.08  # minimum baseline 1-second candle body $
    vel_mult: float = 2.5  # normal-session C3 trigger: live 3s speed >= 2.5x baseline
    news_c1_mult: float = 5.0  # news steep C1 probe: |body(C1)| >= 5x baseline S1 body
    news_c1_min_body: float = 0.55  # minimum $ body for steep news C1 probe
    news_c1_min_vel_ratio: float = 2.0  # prevent 1s whipsaw bounce (e.g. vel=0.6x) from faking news C1
    news_c1_min_3s_move: float = 0.55  # minimum directional 3s move $ for steep news C1
    c2_min_confirm_body: float = 0.20  # minimum $ C2 body in wave direction to confirm NEWS_STEEP_C1
    c3_min_last_slope: float = 1.5  # NORMAL_C3: latest completed candle C2 must still have active slope
    c3_min_last_body: float = 0.20  # NORMAL_C3: latest completed candle C2 minimum body $
    sr_wall_buffer_price: float = 0.80  # block entering right INTO closed M1 Resistance/Support wall before clean break
    sr_break_clear_price: float = 0.30  # clean clearance beyond M1 S/R counts as breakout, not wall
    min_60s_move_price: float = 0.0  # optional ripping-tape gate: require >= $X directional move over last 60s (0=off)
    extreme_slope_mult: float = 9.0  # extreme vertical blow-off -> stop adding at C5+ & tighten exit
    extreme_3s_move: float = 3.50  # or >= $3.50 move in 3s counts as extreme blow-off
    min_burst_price: float = 0.90  # minimum 3s move for normal C3 entry (spread-aware)
    min_efficiency: float = 0.65
    max_spread_price: float = 0.90
    latency_sec: float = 0.25
    add_interval_sec: float = 0.60  # winner add cadence
    c3_add_interval_sec: float = 0.35  # rapid-fire inside confirmed C3 when prior leg is in profit
    add_min_profit_price: float = 0.20  # last leg must be >= +$0.20 net of spread before firing next leg
    add_min_vel_ratio: float = 1.4
    initial_stop_mult: float = 1.25
    min_stop_price: float = 1.50
    max_stop_price: float = 3.50
    decel_ratio: float = 0.28
    steep_decel_ratio: float = 0.45  # tighter decel exit after extreme blow-off
    decel_confirm_ticks: int = 2
    decel_min_hold_sec: float = 1.5
    decel_loss_cut_sec: float = 2.0  # cut dead/stalled losing 1s scalp quickly instead of holding 5s
    disable_decel_exit: bool = False  # wave-rider mode: ride normal 2-5s breaths via structural SL & trail
    decel_only_in_loss_1l: bool = False  # hybrid: cut stalled 1L losers fast after decel_loss_cut_sec, never cut winners
    loss_cut_min_loss_price: float = 0.0  # only trigger stalled_loss_cut once 1L is at least -$X in loss (avoids cutting -$0.30 breaths)
    candle_cap_adds: bool = True  # when False (wave-rider), winner pyramiding continues across multi-second waves
    sl_min_breathing_room: float = 0.0  # min $ gap between locked SL and live exit price (prevents $0.20 choke-outs)
    be_arm_price: float = 0.0  # lock 1L to breakeven (+$0.08) once open gain reaches this $ amount (0=off)
    require_be_on_add: bool = False  # risk-free pyramid gate: only add L2..L6 if post-add SL >= new_avg_entry
    s1_reversal_cut_body: float = 0.30  # cut 1L immediately if a completed S1 candle reverses >= $0.30 in loss
    trail_arm_price: float = 0.35
    trail_min_pullback: float = 0.45
    trail_max_pullback_price: float = 0.0  # optional hard $ cap on pullback from peak (prevents $12 giveback on 6L!)
    trail_giveback_frac: float = 0.35
    multi_leg_giveback_frac: float = 0.0  # optional tighter giveback fraction once basket reaches >= 2 legs
    steep_giveback_frac: float = 0.22
    max_hold_sec: float = 120.0
    win_cooldown_sec: float = 2.0
    loss_cooldown_sec: float = 20.0
    consec_loss_cooldown_sec: float = 45.0  # step back after >=2 consecutive choppy losses


@dataclass
class S1VelocitySurferEngine:
    """Stateful real-time M1 S/R + S1 Candle-Count (C1/C2/C3/C4) + Winner Machine-Gun + Decel Exit."""

    cfg: S1SurferConfig = field(default_factory=S1SurferConfig)
    initial_capital_usd: float = 1000.0

    # Rolling tick & 1-second OHLC candle history
    ticks: deque[tuple[float, float, float, float]] = field(default_factory=deque)  # (ts, bid, ask, mid)
    s1_bars: deque[dict[str, Any]] = field(default_factory=deque)  # completed 1-second OHLC bars
    cur_s1: dict[str, Any] | None = None

    # Live position / order state
    pending_order: dict[str, Any] | None = None
    basket: dict[str, Any] | None = None
    cooldown_until: float = 0.0
    consecutive_losses: int = 0
    next_wave_id: int = 1

    # Stats
    realized_pnl_usd: float = 0.0
    peak_equity_usd: float = 1000.0
    max_dd_usd: float = 0.0
    waves_closed: list[dict[str, Any]] = field(default_factory=list)
    signals_blocked_spread: int = 0
    signals_blocked_m1: int = 0
    signals_blocked_quiet: int = 0
    last_metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.peak_equity_usd = float(self.initial_capital_usd)

    def _record_tick(self, ts: float, bid: float, ask: float) -> tuple[float, dict[str, Any] | None]:
        """Update tick deque and 1-second OHLC candles; return (mid, newly_completed_s1_bar)."""
        mid = 0.5 * (bid + ask)
        self.ticks.append((ts, bid, ask, mid))
        keep_sec = max(self.cfg.burst_window_sec + 4.0, 12.0)
        while self.ticks and (ts - self.ticks[0][0]) > keep_sec:
            self.ticks.popleft()

        sec_ep = int(ts)
        completed_bar: dict[str, Any] | None = None
        if self.cur_s1 is None:
            self.cur_s1 = {"sec": sec_ep, "o": mid, "h": mid, "l": mid, "c": mid}
        elif sec_ep > self.cur_s1["sec"]:
            # Finalize previous 1-second candle
            prev = self.cur_s1
            prev["body"] = round(prev["c"] - prev["o"], 5)
            prev["rng"] = round(prev["h"] - prev["l"], 5)
            self.s1_bars.append(prev)
            completed_bar = prev
            max_hist = self.cfg.baseline_sec + 30
            while len(self.s1_bars) > max_hist:
                self.s1_bars.popleft()
            # Open new forming candle from previous close (continuous tape) or current mid
            self.cur_s1 = {"sec": sec_ep, "o": prev["c"], "h": max(prev["c"], mid), "l": min(prev["c"], mid), "c": mid}
        else:
            self.cur_s1["h"] = max(self.cur_s1["h"], mid)
            self.cur_s1["l"] = min(self.cur_s1["l"], mid)
            self.cur_s1["c"] = mid

        return mid, completed_bar

    def _window_move_and_eff(self, ts: float, win_sec: float) -> tuple[float, float, bool]:
        if len(self.ticks) < 2:
            return 0.0, 0.0, False
        cutoff = ts - win_sec
        start_idx = 0
        for idx, tk in enumerate(self.ticks):
            if tk[0] >= cutoff:
                start_idx = max(0, idx - 1) if (tk[0] - cutoff > 0.25 and idx > 0) else idx
                break
        span = ts - self.ticks[start_idx][0]
        if span < max(0.8, win_sec * 0.5):
            return 0.0, 0.0, False
        net = self.ticks[-1][3] - self.ticks[start_idx][3]
        path = 0.0
        for k in range(start_idx + 1, len(self.ticks)):
            path += abs(self.ticks[k][3] - self.ticks[k - 1][3])
        eff = (abs(net) / path) if path > 1e-9 else 0.0
        return net, eff, True

    def _baselines(self, ts: float) -> tuple[float, float, int]:
        """Return (base_vel_3s, base_s1_body, sample_count) from past completed S1 bars."""
        if len(self.s1_bars) < 6:
            return self.cfg.base_vel_floor, self.cfg.base_s1_body_floor, 0
        step = max(1, int(round(self.cfg.burst_window_sec)))
        exclude_after = int(ts) - step - 1
        arr = list(self.s1_bars)
        vel_samples: list[float] = []
        body_samples: list[float] = []
        for idx in range(step, len(arr)):
            bar = arr[idx]
            if bar["sec"] > exclude_after:
                break
            body_samples.append(abs(float(bar["body"])))
            prev = arr[idx - step]
            if 1 <= (bar["sec"] - prev["sec"]) <= (step + 3):
                vel_samples.append(abs(float(bar["c"]) - float(prev["c"])))
        if len(vel_samples) < self.cfg.min_baseline_samples:
            return self.cfg.base_vel_floor, self.cfg.base_s1_body_floor, len(vel_samples)
        base_vel = max(float(statistics.median(vel_samples)), self.cfg.base_vel_floor)
        base_body = max(float(statistics.median(body_samples)), self.cfg.base_s1_body_floor)
        return base_vel, base_body, len(vel_samples)

    def _tape_move_60s(self, ts: float, mid: float) -> tuple[float, bool]:
        """Return signed 60-second tape displacement ($/min) strictly from past completed S1 bars."""
        if not self.s1_bars:
            return 0.0, False
        cutoff = int(ts) - 60
        ref_bar = None
        for bar in self.s1_bars:
            if bar["sec"] >= cutoff:
                ref_bar = bar
                break
        if ref_bar is None:
            return 0.0, False
        span = int(ts) - int(ref_bar["sec"])
        if span < 20:
            return 0.0, False
        return round(mid - float(ref_bar["o"]), 3), True

    def _s1_candle_streak(self, base_body: float) -> tuple[int, int, float, float]:
        """Return (streak_count, streak_dir, c1_slope_mult, last_slope_mult) on completed S1 candles."""
        if not self.s1_bars:
            return 0, 0, 0.0, 0.0
        arr = list(self.s1_bars)
        last = arr[-1]
        last_body = float(last["body"])
        min_dir_body = max(0.03, base_body * 0.35)
        if abs(last_body) < min_dir_body:
            return 0, 0, 0.0, 0.0
        sdir = 1 if last_body > 0 else -1
        count = 1
        first_body = abs(last_body)
        for k in range(len(arr) - 2, -1, -1):
            b = arr[k]
            if (arr[k + 1]["sec"] - b["sec"]) > 2:
                break
            bd = float(b["body"])
            if (sdir == 1 and bd >= min_dir_body) or (sdir == -1 and bd <= -min_dir_body):
                count += 1
                first_body = abs(bd)
            else:
                break
        return count, sdir, round(first_body / base_body, 2), round(abs(last_body) / base_body, 2)

    def unrealized_pnl_usd(self, bid: float, ask: float) -> float:
        if not self.basket or bid <= 0 or ask <= 0:
            return 0.0
        b = self.basket
        ex = bid if b["side"] == "buy" else ask
        diff = (ex - b["avg_entry"]) if b["side"] == "buy" else (b["avg_entry"] - ex)
        return round(diff * b["total_volume"] * self.cfg.multiplier, 4)

    def _update_dd(self, bid: float, ask: float) -> None:
        eq = self.initial_capital_usd + self.realized_pnl_usd + self.unrealized_pnl_usd(bid, ask)
        if eq > self.peak_equity_usd:
            self.peak_equity_usd = eq
        dd = max(0.0, self.peak_equity_usd - eq)
        if dd > self.max_dd_usd:
            self.max_dd_usd = round(dd, 4)

    def _close_basket(self, exit_px: float, ts: float, reason: str) -> dict[str, Any]:
        assert self.basket is not None
        b = self.basket
        self.basket = None
        self.pending_order = None
        diff = (exit_px - b["avg_entry"]) if b["side"] == "buy" else (b["avg_entry"] - exit_px)
        pnl = round(diff * b["total_volume"] * self.cfg.multiplier, 4)
        self.realized_pnl_usd = round(self.realized_pnl_usd + pnl, 4)
        rec = {
            "wave_id": b["wave_id"],
            "side": b["side"],
            "regime": b["regime"],
            "legs_count": len(b["legs"]),
            "total_volume": b["total_volume"],
            "avg_entry": b["avg_entry"],
            "leg1_entry": b["legs"][0]["entry"],
            "exit_price": round(exit_px, 4),
            "stop_final": b["stop"],
            "opened_at": b["opened_at"],
            "opened_utc": _iso_utc(b["opened_at"]),
            "closed_at": float(ts),
            "closed_utc": _iso_utc(ts),
            "hold_seconds": round(max(0.0, ts - b["opened_at"]), 2),
            "s1_candles_elapsed": int(b["wave_candle_num"]),
            "peak_slope_mult": round(float(b["peak_slope_mult"]), 2),
            "extreme_blowoff": bool(b["extreme_blowoff"]),
            "peak_vel": round(b["peak_vel"], 3),
            "m1_label": b["m1_label"],
            "net_pnl_usd": pnl,
            "exit_reason": reason,
            "equity_after_usd": round(self.initial_capital_usd + self.realized_pnl_usd, 4),
            "paper_only": True,
            "orders_submitted": 0,
        }
        self.waves_closed.append(rec)
        if pnl > 0:
            self.consecutive_losses = 0
            cd = self.cfg.win_cooldown_sec
        else:
            self.consecutive_losses += 1
            cd = self.cfg.consec_loss_cooldown_sec if self.consecutive_losses >= 2 else self.cfg.loss_cooldown_sec
        self.cooldown_until = ts + cd
        return rec

    def _can_add_risk_free(self, *, side: str, bid: float, ask: float, fill_px: float) -> bool:
        """True if adding one leg keeps post-add locked SL safely at/beyond new_avg_entry with breathing room."""
        if not self.cfg.require_be_on_add or self.basket is None:
            return True
        b = self.basket
        old_vol = float(b["total_volume"])
        new_vol = round(old_vol + self.cfg.leg_lot, 2)
        new_avg = (float(b["avg_entry"]) * old_vol + fill_px * self.cfg.leg_lot) / new_vol
        breath = max(0.0, self.cfg.sl_min_breathing_room)
        if side == "buy":
            proj_sl = bid - breath
            return proj_sl >= (new_avg + 0.05)
        proj_sl = ask + breath
        return proj_sl <= (new_avg - 0.05)

    def _lock_basket_stop(self, *, bid: float = 0.0, ask: float = 0.0, lock_half_profit: bool = False) -> float:
        """Tighten basket SL: optional 1L BE, Leg-1/basket BE at 2+ legs, 50% lock in C4 caution."""
        assert self.basket is not None
        b = self.basket
        side = b["side"]
        n_legs = len(b["legs"])
        ent1 = float(b["legs"][0]["entry"])
        avg_ent = float(b["avg_entry"])
        cur_sl = float(b["stop"])

        target_sl = cur_sl
        if n_legs == 1 and self.cfg.be_arm_price > 0.0 and bid > 0 and ask > 0:
            ex = bid if side == "buy" else ask
            gain = (ex - avg_ent) if side == "buy" else (avg_ent - ex)
            if gain >= self.cfg.be_arm_price:
                target_sl = (ent1 + 0.08) if side == "buy" else (ent1 - 0.08)
        elif n_legs == 2:
            if self.cfg.require_be_on_add:
                target_sl = (avg_ent + 0.05) if side == "buy" else (avg_ent - 0.05)
            else:
                target_sl = (ent1 + 0.05) if side == "buy" else (ent1 - 0.05)
        elif n_legs >= 3:
            target_sl = (avg_ent + 0.05) if side == "buy" else (avg_ent - 0.05)

        if lock_half_profit and bid > 0 and ask > 0:
            ex = bid if side == "buy" else ask
            gain = (ex - avg_ent) if side == "buy" else (avg_ent - ex)
            if gain > 0.30:
                half_sl = (avg_ent + 0.50 * gain) if side == "buy" else (avg_ent - 0.50 * gain)
                target_sl = max(target_sl, half_sl) if side == "buy" else min(target_sl, half_sl)

        # Never suffocate a winning trade by placing SL closer than sl_min_breathing_room to live bid/ask!
        if self.cfg.sl_min_breathing_room > 0.0 and bid > 0 and ask > 0:
            ex = bid if side == "buy" else ask
            if side == "buy":
                target_sl = min(target_sl, ex - self.cfg.sl_min_breathing_room)
            else:
                target_sl = max(target_sl, ex + self.cfg.sl_min_breathing_room)

        new_sl = max(cur_sl, target_sl) if side == "buy" else min(cur_sl, target_sl)
        if new_sl != cur_sl:
            b["stop"] = round(new_sl, 4)
            b["stop_locked"] = True
        return b["stop"]

    def on_tick(
        self,
        *,
        ts: float,
        bid: float,
        ask: float,
        m1_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Process one live Bid/Ask sample (~0.25s-0.30s) and return execution/management events."""
        events: list[dict[str, Any]] = []
        if bid <= 0 or ask <= 0 or ask < bid:
            return events

        spread = round(ask - bid, 5)
        mid, completed_s1 = self._record_tick(ts, bid, ask)
        move_3s, eff_3s, has_3s = self._window_move_and_eff(ts, self.cfg.burst_window_sec)
        move_2s, _, has_2s = self._window_move_and_eff(ts, self.cfg.decel_window_sec)
        move_60s, has_60s = self._tape_move_60s(ts, mid)
        base_vel, base_body, base_n = self._baselines(ts)
        vel_ratio = round(abs(move_3s) / base_vel, 2) if (has_3s and base_vel > 0) else 0.0
        streak_n, streak_dir, c1_slope, last_slope = self._s1_candle_streak(base_body)

        self.last_metrics = {
            "ts": ts,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "move_3s": round(move_3s, 3),
            "move_2s": round(move_2s, 3),
            "move_60s": round(move_60s, 2),
            "eff_3s": round(eff_3s, 2),
            "base_vel": round(base_vel, 3),
            "base_s1_body": round(base_body, 3),
            "base_samples": base_n,
            "vel_ratio": vel_ratio,
            "s1_streak": streak_n * streak_dir,
            "s1_slope": last_slope,
            "m1_bias": int(m1_context.get("bias", 0)),
            "m1_label": str(m1_context.get("label", "UNKNOWN")),
        }

        # 1) Execute pending latency-delayed fill if ready
        if self.pending_order is not None and ts >= self.pending_order["fill_ts"]:
            po = self.pending_order
            self.pending_order = None
            if spread <= self.cfg.max_spread_price:
                side = po["side"]
                fill_px = ask if side == "buy" else bid
                if po["kind"] == "open" and self.basket is None:
                    sdist = po["stop_dist"]
                    stop_px = round((fill_px - sdist) if side == "buy" else (fill_px + sdist), 4)
                    regime = po["regime"]
                    start_cnum = int(po["wave_candle_num"])  # 2 if entered right after steep C1; 3 if entered on C3
                    init_slope = float(po["slope_mult"])
                    is_extreme = init_slope >= self.cfg.extreme_slope_mult or abs(move_3s) >= self.cfg.extreme_3s_move
                    self.basket = {
                        "wave_id": self.next_wave_id,
                        "side": side,
                        "regime": regime,
                        "c2_confirmed": regime != "NEWS_STEEP_C1",  # NEWS_STEEP_C1 waits for C2 close confirmation!
                        "wave_candle_num": start_cnum,
                        "peak_slope_mult": init_slope,
                        "extreme_blowoff": is_extreme,
                        "legs": [{"entry": round(fill_px, 4), "volume": self.cfg.leg_lot, "ts": ts}],
                        "total_volume": round(self.cfg.leg_lot, 2),
                        "avg_entry": round(fill_px, 4),
                        "stop": stop_px,
                        "stop_locked": False,
                        "opened_at": ts,
                        "last_leg_ts": ts,
                        "best_px": bid if side == "buy" else ask,
                        "peak_vel": max(abs(move_2s), abs(move_3s) * 0.67, 0.30),
                        "decel_ticks": 0,
                        "m1_label": po["m1_label"],
                    }
                    self.next_wave_id += 1
                    events.append({
                        "type": "OPEN_L1",
                        "wave_id": self.basket["wave_id"],
                        "side": side,
                        "regime": regime,
                        "candle_num": start_cnum,
                        "entry": round(fill_px, 4),
                        "stop": stop_px,
                        "vel_ratio": po["vel_ratio"],
                        "slope_mult": init_slope,
                        "move_3s": po["move_3s"],
                        "m1_label": po["m1_label"],
                        "spread": spread,
                    })
                elif po["kind"] == "add" and self.basket is not None:
                    b = self.basket
                    eff_max_legs = self.cfg.probe_max_legs if not b["c2_confirmed"] else self.cfg.max_legs
                    if len(b["legs"]) < eff_max_legs:
                        last_ent = float(b["legs"][-1]["entry"])
                        last_net = (bid - last_ent) if side == "buy" else (last_ent - ask)
                        safe_sl = (bid > b["stop"]) if side == "buy" else (ask < b["stop"])
                        risk_free_ok = self._can_add_risk_free(side=side, bid=bid, ask=ask, fill_px=fill_px)
                        if last_net >= self.cfg.add_min_profit_price and safe_sl and risk_free_ok:
                            old_vol = float(b["total_volume"])
                            new_vol = round(old_vol + self.cfg.leg_lot, 2)
                            new_avg = round((float(b["avg_entry"]) * old_vol + fill_px * self.cfg.leg_lot) / new_vol, 4)
                            b["legs"].append({"entry": round(fill_px, 4), "volume": self.cfg.leg_lot, "ts": ts})
                            b["total_volume"] = new_vol
                            b["avg_entry"] = new_avg
                            b["last_leg_ts"] = ts
                            lock_half = (b["wave_candle_num"] >= 4) and (not self.cfg.disable_decel_exit)
                            new_sl = self._lock_basket_stop(bid=bid, ask=ask, lock_half_profit=lock_half)
                            events.append({
                                "type": "FIRE_ADD",
                                "wave_id": b["wave_id"],
                                "side": side,
                                "regime": b["regime"],
                                "candle_num": b["wave_candle_num"],
                                "leg_num": len(b["legs"]),
                                "add_price": round(fill_px, 4),
                                "total_volume": new_vol,
                                "avg_entry": new_avg,
                                "stop": new_sl,
                                "last_leg_net": round(last_net, 3),
                                "vel_ratio": vel_ratio,
                            })

        # 2) Manage open basket (S1 candle progression + C2 confirm/cut + C4 caution + SL/Decel/Trail + Machine-Gun Add)
        if self.basket is not None:
            b = self.basket
            side = b["side"]
            sdir = 1 if side == "buy" else -1
            ex_px = bid if side == "buy" else ask
            hold_sec = ts - b["opened_at"]

            # Track S1 candle progression inside active wave whenever a 1-second candle closes
            if completed_s1 is not None and completed_s1["sec"] >= int(b["opened_at"]):
                c_body = float(completed_s1["body"])
                c_slope = abs(c_body) / base_body if base_body > 0 else 0.0
                if c_slope > b["peak_slope_mult"]:
                    b["peak_slope_mult"] = c_slope
                if c_slope >= self.cfg.extreme_slope_mult or abs(move_3s) >= self.cfg.extreme_3s_move:
                    b["extreme_blowoff"] = True

                # News C1 Probe -> check C2 close confirmation (requires real body >= c2_min_confirm_body!)
                if not b["c2_confirmed"] and b["wave_candle_num"] == 2:
                    c2_min = max(self.cfg.c2_min_confirm_body, spread * 0.45)
                    if (sdir == 1 and c_body >= c2_min) or (sdir == -1 and c_body <= -c2_min):
                        b["c2_confirmed"] = True
                        b["wave_candle_num"] = 3  # now entering Candle 3 ("کله‌خری" unlocked!)
                        events.append({
                            "type": "C2_CONFIRMED",
                            "wave_id": b["wave_id"],
                            "side": side,
                            "c2_body": round(c_body, 3),
                            "extreme_blowoff": b["extreme_blowoff"],
                        })
                    else:
                        # Fake or weak steep C1 wick: C2 failed to confirm -> cut immediately!
                        closed = self._close_basket(ex_px, ts, "c2_unconfirmed_cut")
                        events.append({"type": "CLOSE_ALL", "wave": closed})
                        self._update_dd(bid, ask)
                        return events
                else:
                    b["wave_candle_num"] += 1
                    # Fast S1 reversal cut: if still on 1L in loss and a completed S1 candle reverses strongly against us!
                    cur_gain = (bid - b["avg_entry"]) if side == "buy" else (b["avg_entry"] - ask)
                    if (
                        len(b["legs"]) == 1
                        and cur_gain <= 0.0
                        and self.cfg.s1_reversal_cut_body > 0.0
                        and (
                            (sdir == 1 and c_body <= -self.cfg.s1_reversal_cut_body)
                            or (sdir == -1 and c_body >= self.cfg.s1_reversal_cut_body)
                        )
                    ):
                        closed = self._close_basket(ex_px, ts, "s1_reversal_cut")
                        events.append({"type": "CLOSE_ALL", "wave": closed})
                        self._update_dd(bid, ask)
                        return events
                    # On C4+ in moderate slope, tighten stop to lock 50% of open profit ("کندل ۴ با احتیاط")
                    # In wave-rider mode (disable_decel_exit=True), only lock 50% once gain >= trail_arm_price!
                    if b["wave_candle_num"] >= 4 and (
                        not self.cfg.disable_decel_exit or cur_gain >= self.cfg.trail_arm_price
                    ):
                        self._lock_basket_stop(bid=bid, ask=ask, lock_half_profit=True)

            # Update best favorable exit price and peak 2s velocity
            if side == "buy":
                b["best_px"] = max(b["best_px"], bid)
                signed_vel_2s = move_2s if has_2s else 0.0
                avg_gain = bid - b["avg_entry"]
                max_exc = b["best_px"] - b["legs"][0]["entry"]
                pullback = b["best_px"] - bid
            else:
                b["best_px"] = min(b["best_px"], ask)
                signed_vel_2s = -move_2s if has_2s else 0.0
                avg_gain = b["avg_entry"] - ask
                max_exc = b["legs"][0]["entry"] - b["best_px"]
                pullback = ask - b["best_px"]

            if signed_vel_2s > b["peak_vel"]:
                b["peak_vel"] = signed_vel_2s

            # Optional 1L Free-Ride Breakeven Arm: once 1L clears be_arm_price, lock SL at BE (+$0.08)
            if (
                self.cfg.be_arm_price > 0.0
                and len(b["legs"]) == 1
                and not b["stop_locked"]
                and avg_gain >= self.cfg.be_arm_price
            ):
                self._lock_basket_stop(bid=bid, ask=ask, lock_half_profit=False)

            # Priority 1: Hard / Locked Basket SL
            sl_hit = (bid <= b["stop"]) if side == "buy" else (ask >= b["stop"])
            if sl_hit:
                if b["stop_locked"]:
                    reason = "profit_floor_hit" if avg_gain >= 0 else "locked_sl_gap_slip"
                else:
                    reason = "hard_stop_loss"
                closed = self._close_basket(ex_px, ts, reason)
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Priority 2: Slope-Adaptive Deceleration Exit ("وقتی شتاب کم شد سریع می‌بندم!")
            if not self.cfg.disable_decel_exit:
                eff_decel = self.cfg.steep_decel_ratio if (b["extreme_blowoff"] and b["wave_candle_num"] >= 4) else self.cfg.decel_ratio
                if hold_sec >= self.cfg.decel_min_hold_sec and has_2s:
                    if signed_vel_2s <= (eff_decel * b["peak_vel"]):
                        b["decel_ticks"] += 1
                    else:
                        b["decel_ticks"] = 0
                    if b["decel_ticks"] >= self.cfg.decel_confirm_ticks and (
                        avg_gain > 0 or signed_vel_2s < 0 or hold_sec >= self.cfg.decel_loss_cut_sec
                    ):
                        closed = self._close_basket(ex_px, ts, "decel_stall_exit")
                        events.append({"type": "CLOSE_ALL", "wave": closed})
                        self._update_dd(bid, ask)
                        return events
            elif (
                self.cfg.decel_only_in_loss_1l
                and len(b["legs"]) == 1
                and avg_gain <= -max(0.0, self.cfg.loss_cut_min_loss_price)
                and hold_sec >= self.cfg.decel_loss_cut_sec
                and has_2s
            ):
                # Hybrid fast-loss-cut: cut dead/reversing 1L losers quickly while NEVER touching winners or shallow breaths!
                if signed_vel_2s <= 0.0:
                    b["decel_ticks"] += 1
                else:
                    b["decel_ticks"] = 0
                if b["decel_ticks"] >= self.cfg.decel_confirm_ticks:
                    closed = self._close_basket(ex_px, ts, "stalled_loss_cut")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events

            # Priority 3: Tight Tick-Trail (tighter on multi-leg baskets + optional hard pullback cap!)
            if self.cfg.disable_decel_exit:
                if self.cfg.multi_leg_giveback_frac > 0.0 and len(b["legs"]) >= 2:
                    eff_giveback = self.cfg.multi_leg_giveback_frac
                    min_pb = min(self.cfg.trail_min_pullback, 1.15)
                else:
                    eff_giveback = self.cfg.trail_giveback_frac
                    min_pb = self.cfg.trail_min_pullback
            else:
                eff_giveback = self.cfg.steep_giveback_frac if (b["extreme_blowoff"] or b["wave_candle_num"] >= 4) else self.cfg.trail_giveback_frac
                min_pb = self.cfg.trail_min_pullback * (0.75 if b["extreme_blowoff"] else 1.0)
            if avg_gain >= self.cfg.trail_arm_price or (b["stop_locked"] and max_exc >= self.cfg.trail_arm_price):
                allow_pb = max(min_pb, eff_giveback * max(0.0, max_exc))
                if self.cfg.trail_max_pullback_price > 0.0:
                    allow_pb = min(allow_pb, max(min_pb, self.cfg.trail_max_pullback_price))
                if pullback >= allow_pb and avg_gain > 0:
                    closed = self._close_basket(ex_px, ts, "tick_trail_exit")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events

            # Priority 4: Hard Time-Stop
            if hold_sec >= self.cfg.max_hold_sec:
                closed = self._close_basket(ex_px, ts, "max_hold_exit")
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Priority 5: Slope- & Candle-Aware Winner-Only Machine-Gun Add
            # - Micro-scalp mode (candle_cap_adds=True): stop adds at C5+ on extreme blow-off, C6+ on moderate slope
            # - Wave-rider mode (candle_cap_adds=False): add winner legs across the full wave on real $1+ steps!
            cnum = int(b["wave_candle_num"])
            if self.cfg.candle_cap_adds:
                can_add_by_candle = not ((b["extreme_blowoff"] and cnum >= 5) or cnum >= 6)
                eff_interval = self.cfg.c3_add_interval_sec if (b["c2_confirmed"] and cnum in (3, 4)) else self.cfg.add_interval_sec
            else:
                can_add_by_candle = True
                eff_interval = self.cfg.add_interval_sec

            eff_max_legs = self.cfg.probe_max_legs if not b["c2_confirmed"] else self.cfg.max_legs

            if (
                can_add_by_candle
                and self.pending_order is None
                and len(b["legs"]) < eff_max_legs
                and (ts - b["last_leg_ts"]) >= eff_interval
                and spread <= self.cfg.max_spread_price
            ):
                last_ent = float(b["legs"][-1]["entry"])
                last_net = (bid - last_ent) if side == "buy" else (last_ent - ask)
                if self.cfg.candle_cap_adds:
                    min_vel_req = self.cfg.add_min_vel_ratio * (1.25 if cnum == 4 else 1.0)
                    mom_ok = signed_vel_2s >= (0.35 * b["peak_vel"]) and vel_ratio >= min_vel_req
                else:
                    mom_ok = signed_vel_2s > 0.0
                est_fill = ask if side == "buy" else bid
                if (
                    last_net >= self.cfg.add_min_profit_price
                    and mom_ok
                    and self._can_add_risk_free(side=side, bid=bid, ask=ask, fill_px=est_fill)
                ):
                    self.pending_order = {
                        "kind": "add",
                        "side": side,
                        "fill_ts": ts + self.cfg.latency_sec,
                    }

            self._update_dd(bid, ask)
            return events

        # 3) Flat: evaluate BOTH Playbook Regimes (M1 Bias MUST match S1 direction!)
        if self.pending_order is not None or ts < self.cooldown_until or base_n < self.cfg.min_baseline_samples:
            self._update_dd(bid, ask)
            return events

        m1_bias = int(m1_context.get("bias", 0))
        m1_label = str(m1_context.get("label", "M1_OK"))
        m1_sup = m1_context.get("support")
        m1_res = m1_context.get("resistance")
        # Live causal M1 S/R break: if closed M1 is neutral/choppy right before a news shock,
        # breaking cleanly beyond closed M1 Swing Support/Resistance unlocks direction immediately!
        if m1_bias == 0:
            if m1_sup is not None and mid < (float(m1_sup) - self.cfg.sr_break_clear_price):
                m1_bias = -1
                m1_label = "LIVE_M1_SUP_BREAK"
            elif m1_res is not None and mid > (float(m1_res) + self.cfg.sr_break_clear_price):
                m1_bias = 1
                m1_label = "LIVE_M1_RES_BREAK"

        triggered_regime: str | None = None
        burst_dir = 0
        wave_cnum = 3
        slope_metric = last_slope

        # Regime A: News / Session-Open Steep C1 Probe (just closed steep Candle 1 + real 3s directional vel!)
        if completed_s1 is not None and streak_n == 1 and streak_dir != 0:
            c1_abs = abs(float(completed_s1["body"]))
            news_3s_ok = (not has_3s) or (
                (move_3s * streak_dir) >= max(self.cfg.news_c1_min_3s_move, spread * 1.8)
                and vel_ratio >= self.cfg.news_c1_min_vel_ratio
            )
            if (
                last_slope >= self.cfg.news_c1_mult
                and c1_abs >= max(self.cfg.news_c1_min_body, spread * 1.6)
                and news_3s_ok
            ):
                triggered_regime = "NEWS_STEEP_C1"
                burst_dir = streak_dir
                wave_cnum = 2  # entering right as C2 opens; wait for C2 close confirmation
                slope_metric = last_slope

        # Regime B: Normal Active Session C3 Entry (2 completed same-color S1 candles + C2 still strong + C3 continuing!)
        if triggered_regime is None and has_3s and streak_n >= 2 and streak_dir != 0:
            last_close = float(self.s1_bars[-1]["c"])
            last_body_abs = abs(float(self.s1_bars[-1]["body"]))
            c3_continuing = (mid > last_close) if streak_dir == 1 else (mid < last_close)
            min_req_move = max(self.cfg.min_burst_price, spread * 2.2)
            if (
                c3_continuing
                and last_slope >= self.cfg.c3_min_last_slope
                and last_body_abs >= self.cfg.c3_min_last_body
                and (move_3s * streak_dir) >= min_req_move
                and vel_ratio >= self.cfg.vel_mult
                and eff_3s >= self.cfg.min_efficiency
            ):
                triggered_regime = "NORMAL_C3"
                burst_dir = streak_dir
                wave_cnum = 3
                slope_metric = max(c1_slope, last_slope)

        if triggered_regime is not None and burst_dir != 0:
            # Live M1 S/R wall guard: never BUY right underneath M1 Resistance (R-0.80 .. R+0.30)
            # or SELL right on top of M1 Support (S-0.30 .. S+0.80) before a clean breakout!
            blocked_by_sr_wall = False
            if self.cfg.sr_wall_buffer_price > 0.0:
                if burst_dir == 1 and m1_res is not None:
                    r_val = float(m1_res)
                    if (r_val - self.cfg.sr_wall_buffer_price) <= mid <= (r_val + self.cfg.sr_break_clear_price):
                        blocked_by_sr_wall = True
                elif burst_dir == -1 and m1_sup is not None:
                    s_val = float(m1_sup)
                    if (s_val - self.cfg.sr_break_clear_price) <= mid <= (s_val + self.cfg.sr_wall_buffer_price):
                        blocked_by_sr_wall = True

            if spread > self.cfg.max_spread_price:
                self.signals_blocked_spread += 1
            elif self.cfg.min_60s_move_price > 0.0 and (not has_60s or (move_60s * burst_dir) < self.cfg.min_60s_move_price):
                self.signals_blocked_quiet += 1
            elif m1_bias != burst_dir or blocked_by_sr_wall:
                self.signals_blocked_m1 += 1
            else:
                side = "buy" if burst_dir == 1 else "sell"
                sdist = min(
                    max(abs(move_3s) * self.cfg.initial_stop_mult, spread * 3.5, self.cfg.min_stop_price),
                    self.cfg.max_stop_price,
                )
                self.pending_order = {
                    "kind": "open",
                    "side": side,
                    "regime": triggered_regime,
                    "wave_candle_num": wave_cnum,
                    "slope_mult": slope_metric,
                    "fill_ts": ts + self.cfg.latency_sec,
                    "stop_dist": round(sdist, 4),
                    "vel_ratio": vel_ratio,
                    "move_3s": round(move_3s, 3),
                    "m1_label": m1_label,
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
            "signals_blocked_spread": self.signals_blocked_spread,
            "signals_blocked_m1": self.signals_blocked_m1,
            "signals_blocked_quiet": self.signals_blocked_quiet,
            "open_basket": self.basket,
            "last_metrics": self.last_metrics,
        }


def append_jsonl_private(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.chmod(path, 0o600)


def _mini_m1_context(closed_bars: list[list[float]]) -> dict[str, Any]:
    """Strictly causal mini M1 S/R & direction context for short live recordings (< 25 closed M1 bars)."""
    if len(closed_bars) < 2:
        return {
            "bias": 0,
            "label": "SYNTH_M1_WARMUP",
            "support": None,
            "resistance": None,
            "atr_m1": 0.0,
            "pullback_resume": False,
            "last_close": closed_bars[-1][4] if closed_bars else 0.0,
        }
    window = closed_bars[-15:]
    prev = window[:-1]
    last = window[-1]
    sup = min(b[3] for b in prev)
    res = max(b[2] for b in prev)
    c = float(last[4])
    o = float(last[1])
    net_dir = c - float(window[0][1])
    full_dir = c - float(closed_bars[0][1])
    bias = 0
    label = "SYNTH_M1_CHOP"
    if c < sup or (c < o and net_dir < -0.80 and full_dir <= 0.0):
        bias = -1
        label = "SYNTH_M1_BEAR"
    elif c > res or (c > o and net_dir > 0.80 and full_dir >= 0.0):
        bias = 1
        label = "SYNTH_M1_BULL"
    return {
        "bias": bias,
        "label": label,
        "support": round(sup, 3),
        "resistance": round(res, 3),
        "atr_m1": round(statistics.mean(max(b[2] - b[3], 0.1) for b in window), 3),
        "pullback_resume": False,
        "last_close": round(c, 3),
    }


def replay_recorded_ticks(
    tick_jsonl_path: Path,
    *,
    cfg: S1SurferConfig | None = None,
    initial_capital_usd: float = 1000.0,
    rebuild_m1: bool = False,
) -> dict[str, Any]:
    eng = S1VelocitySurferEngine(cfg=cfg or S1SurferConfig(), initial_capital_usd=initial_capital_usd)
    tick_count = 0
    rebuilt_m1_ticks = 0
    last_bid, last_ask = 0.0, 0.0

    # Strictly causal M1 bar aggregator from recorded live ticks (used automatically when
    # recorded M1 labels are missing / INSUFFICIENT_M1 / WARMING_UP, or when rebuild_m1=True)
    synth_m1_bars: list[list[float]] = []
    cur_m1_bucket: int | None = None
    cur_m1_bar: list[float] | None = None
    synth_m1_ctx: dict[str, Any] = {"bias": 0, "label": "SYNTH_M1_WARMUP", "support": None, "resistance": None}

    with tick_jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            ts = float(row["ts"])
            bid = float(row["bid"])
            ask = float(row["ask"])
            mid = 0.5 * (bid + ask)

            m1_bucket = int(ts // 60) * 60
            if cur_m1_bar is None:
                cur_m1_bucket = m1_bucket
                cur_m1_bar = [float(m1_bucket), mid, mid, mid, mid, 1.0]
            elif m1_bucket > int(cur_m1_bucket or 0):
                synth_m1_bars.append(cur_m1_bar)
                if len(synth_m1_bars) > 120:
                    synth_m1_bars = synth_m1_bars[-120:]
                if len(synth_m1_bars) >= 25:
                    synth_m1_ctx = analyze_m1_sr_context(synth_m1_bars, server_now_ts=ts)
                else:
                    synth_m1_ctx = _mini_m1_context(synth_m1_bars)
                cur_m1_bucket = m1_bucket
                cur_m1_bar = [float(m1_bucket), mid, mid, mid, mid, 1.0]
            else:
                cur_m1_bar[2] = max(cur_m1_bar[2], mid)
                cur_m1_bar[3] = min(cur_m1_bar[3], mid)
                cur_m1_bar[4] = mid
                cur_m1_bar[5] += 1.0

            rec_m1 = row.get("m1")
            rec_lbl = str(rec_m1.get("label", "")) if isinstance(rec_m1, dict) else ""
            if rebuild_m1 or not isinstance(rec_m1, dict) or rec_lbl in ("", "INSUFFICIENT_M1", "WARMING_UP", "REPLAY_NONE"):
                m1_ctx = synth_m1_ctx
                rebuilt_m1_ticks += 1
            else:
                m1_ctx = rec_m1

            eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
            last_bid, last_ask = bid, ask
            tick_count += 1
    if eng.basket is not None and last_bid > 0 and last_ask > 0:
        ex = last_bid if eng.basket["side"] == "buy" else last_ask
        eng._close_basket(ex, eng.last_metrics.get("ts", 0.0), "eof_replay")
    s = eng.summary(last_bid, last_ask)
    s["ticks_replayed"] = tick_count
    s["rebuilt_m1_ticks"] = rebuilt_m1_ticks
    s["waves"] = eng.waves_closed
    return s


def s1_calibration_profiles(
    *,
    leg_lot: float = 0.01,
    max_legs: int = 6,
) -> list[tuple[str, S1SurferConfig]]:
    """Canonical named S1 parameter profiles shared between --replay-sweep and live --profile."""
    return [
        (
            "v2.0_LEGACY_5S",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=2.5,
                min_burst_price=0.70,
                add_min_profit_price=0.15,
                news_c1_min_vel_ratio=0.0,
                news_c1_min_3s_move=0.0,
                c2_min_confirm_body=0.01,
                c3_min_last_slope=0.0,
                c3_min_last_body=0.0,
                sr_wall_buffer_price=0.0,
                decel_min_hold_sec=2.0,
                decel_loss_cut_sec=5.0,
                s1_reversal_cut_body=0.0,
                loss_cooldown_sec=6.0,
                consec_loss_cooldown_sec=6.0,
            ),
        ),
        (
            "v2.1_DEFAULT",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=2.5,
                min_burst_price=0.90,
                add_min_profit_price=0.20,
                loss_cooldown_sec=20.0,
                consec_loss_cooldown_sec=45.0,
            ),
        ),
        (
            "v2.1_FILTER_1.2",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=2.8,
                min_burst_price=1.20,
                add_min_profit_price=0.25,
                loss_cooldown_sec=25.0,
                consec_loss_cooldown_sec=50.0,
            ),
        ),
        (
            "v2.1_FASTCUT_1.3",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.0,
                min_burst_price=1.30,
                add_min_profit_price=0.18,
                decel_min_hold_sec=1.2,
                decel_loss_cut_sec=1.5,
                s1_reversal_cut_body=0.22,
                loss_cooldown_sec=25.0,
                consec_loss_cooldown_sec=50.0,
            ),
        ),
        (
            "v2.1_STRONG_1.5",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.2,
                min_burst_price=1.50,
                add_min_profit_price=0.18,
                loss_cooldown_sec=25.0,
                consec_loss_cooldown_sec=50.0,
            ),
        ),
        (
            "v2.1_SNIPER_2.0",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.5,
                min_burst_price=2.00,
                add_min_profit_price=0.18,
                loss_cooldown_sec=30.0,
                consec_loss_cooldown_sec=60.0,
            ),
        ),
        (
            "v2.1_NEWS_ONLY",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=4.2,
                min_burst_price=2.20,
                news_c1_mult=4.5,
                news_c1_min_body=0.65,
                c2_min_confirm_body=0.25,
                add_min_profit_price=0.18,
                loss_cooldown_sec=25.0,
                consec_loss_cooldown_sec=50.0,
            ),
        ),
        (
            "v2.2_RIP_4.0",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.0,
                min_burst_price=1.20,
                min_60s_move_price=4.0,
                add_min_profit_price=0.18,
                loss_cooldown_sec=20.0,
                consec_loss_cooldown_sec=45.0,
            ),
        ),
        (
            "v2.2_RIP_5.5",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.2,
                min_burst_price=1.50,
                min_60s_move_price=5.5,
                add_min_profit_price=0.18,
                loss_cooldown_sec=25.0,
                consec_loss_cooldown_sec=50.0,
            ),
        ),
        (
            "v2.3_WAVE_RIP4",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.0,
                min_burst_price=1.20,
                min_60s_move_price=4.0,
                add_interval_sec=2.5,
                add_min_profit_price=1.00,
                min_stop_price=2.20,
                max_stop_price=3.50,
                disable_decel_exit=True,
                candle_cap_adds=False,
                sl_min_breathing_room=1.20,
                s1_reversal_cut_body=0.0,
                trail_arm_price=1.80,
                trail_min_pullback=1.40,
                trail_giveback_frac=0.35,
                max_hold_sec=180.0,
                loss_cooldown_sec=30.0,
                consec_loss_cooldown_sec=60.0,
            ),
        ),
        (
            "v2.3_WAVE_RIP5.5",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.2,
                min_burst_price=1.50,
                min_60s_move_price=5.5,
                add_interval_sec=3.0,
                add_min_profit_price=1.20,
                min_stop_price=2.50,
                max_stop_price=3.80,
                disable_decel_exit=True,
                candle_cap_adds=False,
                sl_min_breathing_room=1.40,
                s1_reversal_cut_body=0.0,
                trail_arm_price=2.00,
                trail_min_pullback=1.60,
                trail_giveback_frac=0.32,
                max_hold_sec=240.0,
                loss_cooldown_sec=30.0,
                consec_loss_cooldown_sec=60.0,
            ),
        ),
        (
            "v2.3_WAVE_WIDE",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.0,
                min_burst_price=1.40,
                min_60s_move_price=4.5,
                add_interval_sec=4.0,
                add_min_profit_price=1.50,
                min_stop_price=2.80,
                max_stop_price=4.20,
                disable_decel_exit=True,
                candle_cap_adds=False,
                sl_min_breathing_room=1.80,
                s1_reversal_cut_body=0.0,
                trail_arm_price=2.50,
                trail_min_pullback=2.00,
                trail_giveback_frac=0.30,
                max_hold_sec=300.0,
                loss_cooldown_sec=30.0,
                consec_loss_cooldown_sec=60.0,
            ),
        ),
        (
            "v2.4_SAFE_RIP4",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.0,
                min_burst_price=1.20,
                min_60s_move_price=4.0,
                add_interval_sec=2.0,
                add_min_profit_price=1.00,
                min_stop_price=1.80,
                max_stop_price=2.60,
                disable_decel_exit=True,
                candle_cap_adds=False,
                sl_min_breathing_room=1.10,
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
            ),
        ),
        (
            "v2.4_SAFE_HYBRID",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.2,
                min_burst_price=1.50,
                min_60s_move_price=5.5,
                add_interval_sec=2.0,
                add_min_profit_price=1.00,
                min_stop_price=1.60,
                max_stop_price=2.40,
                disable_decel_exit=True,
                candle_cap_adds=False,
                sl_min_breathing_room=1.10,
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
            ),
        ),
        (
            "v2.4_SAFE_FASTCUT",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.2,
                min_burst_price=1.50,
                min_60s_move_price=5.5,
                add_interval_sec=2.0,
                add_min_profit_price=1.00,
                min_stop_price=1.60,
                max_stop_price=2.40,
                decel_loss_cut_sec=3.0,
                disable_decel_exit=True,
                decel_only_in_loss_1l=True,
                candle_cap_adds=False,
                sl_min_breathing_room=1.10,
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
            ),
        ),
        (
            "v2.4_GOLD_RIP4",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.0,
                min_burst_price=1.20,
                min_60s_move_price=4.0,
                add_interval_sec=2.0,
                add_min_profit_price=1.00,
                min_stop_price=1.80,
                max_stop_price=2.60,
                decel_loss_cut_sec=6.0,
                disable_decel_exit=True,
                decel_only_in_loss_1l=True,
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
            ),
        ),
        (
            "v2.4_GOLD_RIP5.5",
            S1SurferConfig(
                leg_lot=leg_lot,
                max_legs=max_legs,
                vel_mult=3.2,
                min_burst_price=1.50,
                min_60s_move_price=5.5,
                add_interval_sec=2.0,
                add_min_profit_price=1.00,
                min_stop_price=1.60,
                max_stop_price=2.40,
                decel_loss_cut_sec=6.0,
                disable_decel_exit=True,
                decel_only_in_loss_1l=True,
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
            ),
        ),
    ]


def build_s1_profile_config(
    profile_name: str,
    *,
    leg_lot: float = 0.01,
    max_legs: int = 6,
) -> S1SurferConfig:
    """Resolve a canonical profile name to its exact S1SurferConfig."""
    by_name = dict(s1_calibration_profiles(leg_lot=leg_lot, max_legs=max_legs))
    if profile_name not in by_name:
        valid = ", ".join(by_name.keys())
        raise ValueError(f"Unknown S1 profile '{profile_name}'. Valid profiles: {valid}")
    return by_name[profile_name]


def replay_calibration_sweep(
    tick_jsonl_path: Path,
    *,
    leg_lot: float = 0.01,
    max_legs: int = 6,
    initial_capital_usd: float = 1000.0,
    rebuild_m1: bool = False,
) -> list[dict[str, Any]]:
    """Run all canonical S1 parameter profiles side-by-side on the exact same recorded ticks (ORDERS=0)."""
    profiles = s1_calibration_profiles(leg_lot=leg_lot, max_legs=max_legs)
    rows: list[dict[str, Any]] = []
    for name, pcfg in profiles:
        res = replay_recorded_ticks(
            tick_jsonl_path,
            cfg=pcfg,
            initial_capital_usd=initial_capital_usd,
            rebuild_m1=rebuild_m1,
        )
        wins = [w["net_pnl_usd"] for w in res["waves"] if w["net_pnl_usd"] > 0]
        losses = [w["net_pnl_usd"] for w in res["waves"] if w["net_pnl_usd"] <= 0]
        res["profile"] = name
        res["avg_win_usd"] = round(sum(wins) / len(wins), 2) if wins else 0.0
        res["avg_loss_usd"] = round(sum(losses) / len(losses), 2) if losses else 0.0
        rows.append(res)
    return rows


def make_synthetic_s1_session(*, mode: str = "normal_c3") -> tuple[list[list[float]], list[tuple[float, float, float]]]:
    """Deterministic synthetic M1 bearish pullback-resume + 0.25s S1 tick sessions.

    Modes:
      - "normal_c3": 24s calm baseline -> 2 normal bearish S1 candles -> C3+ acceleration -> stall exit
      - "news_steep_confirm": 24s calm -> explosive C1 -> confirming C2 -> full-send C3 -> stall exit
      - "news_steep_fakeout": 24s calm -> explosive C1 -> reversing C2 -> instant c2_unconfirmed_cut
    """
    t0 = 1764080000.0
    m1_bars: list[list[float]] = []
    px = 3520.0
    for i in range(30):
        ts = t0 - (30 - i) * 60.0
        if i in (26, 27):
            chg = +1.1
        elif i == 29:
            chg = -1.8
        else:
            chg = -0.8
        o = px
        c = px + chg
        m1_bars.append([ts, round(o, 2), round(max(o, c) + 0.4, 2), round(min(o, c) - 0.4, 2), round(c, 2), 150.0])
        px = c

    ticks: list[tuple[float, float, float]] = []
    bid = px
    spread = 0.32
    t = t0 + 10.0
    for step in range(4 * 48):  # 48 seconds @ 4 Hz
        sec = step * 0.25
        if sec < 24.0:
            delta = 0.04 if (step % 4 < 2) else -0.04
        elif mode == "normal_c3":
            if sec < 26.0:
                # 2 moderate bearish S1 candles (~$0.36/s each, below news_c1_min_body=0.55!)
                delta = -0.09
            elif sec < 33.0:
                # C3+ good smooth acceleration (~$0.64/s, below extreme blow-off)
                delta = -0.16
            else:
                delta = +0.02
        elif mode == "news_steep_confirm":
            if sec < 25.0:
                # Explosive steep C1 (~$0.80/s!)
                delta = -0.20
            elif sec < 26.0:
                # Confirming C2 (~$0.68/s)
                delta = -0.17
            elif sec < 30.0:
                # C3 full-send + C4 follow-through
                delta = -0.28
            else:
                delta = +0.03
        elif mode == "news_steep_fakeout":
            if sec < 25.0:
                # Explosive steep C1 (~$0.80/s)
                delta = -0.20
            elif sec < 26.0:
                # C2 REVERSES UP (fake news wick!)
                delta = +0.18
            else:
                delta = +0.25
        else:
            delta = 0.0
        bid = round(bid + delta, 2)
        ticks.append((t + sec, bid, round(bid + spread, 2)))
    return m1_bars, ticks
