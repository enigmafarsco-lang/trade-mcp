"""S1 Velocity Surfer: M1 S/R Context + 1-Second Acceleration + Winner-Only 6-Leg Machine Gun + Deceleration Exit.

Faithfully models a dual-chart tape scalper (M1 structure + 1-second velocity chart)
with strict real-account protections and ZERO broker orders (Paper-only / replay):
  1. M1 Chart Context (closed bars only): EMA9/21 trend direction, 15-bar Swing S/R,
     pullback-resume detection, and guard against firing straight into an untouched S/R wall.
  2. 1-Second Velocity Ratio (S1): compares live 3-second directional speed against
     rolling 90-second baseline "eye speed" (triggers when speed >= vel_mult x baseline
     with high directional straightness/efficiency and live spread within cap).
  3. 1-Second Winner-Only Machine Gun (Legs 1..max_legs): adds Leg k+1 after >= 1s
     ONLY when the most recent leg is ALREADY in net profit after spread and velocity
     is still accelerating — NEVER averages down into losers. Automatically tightens
     the basket stop-loss to Leg-1 breakeven at 2 legs and basket profit-floor at 3+ legs.
  4. Deceleration Exit: closes ALL open legs immediately when 2-second directional
     velocity decays below decel_ratio * peak_vel (or on tight tick-trail / hard SL).
  5. Realistic execution latency (default 250ms) and lossless sub-second JSONL tick
     recording + offline replay backtesting on real recorded Alpari ticks.
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

    # Check if there was a recent counter-trend pullback in last 5 bars followed by resume
    recent5 = window[-6:-1]
    had_green_pb = any(b[4] > b[1] for b in recent5)
    had_red_pb = any(b[4] < b[1] for b in recent5)
    # Also detect clean slope over last 10 closed M1 bars
    slope10 = c - window[-11][4]

    bias = 0
    label = "CHOP_NEUTRAL"
    pb_resume = False

    # Bearish structure: EMA9 < EMA21 and price below EMA21 (or broke below 15-bar support)
    if (ema9[-1] < ema21[-1] and c <= ema21[-1] and slope10 <= 0) or (c < sup and c < o):
        # Guard: avoid firing SELL straight into an unbroken 15-bar support with strong lower-wick rejection
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

    # Bullish structure: EMA9 > EMA21 and price above EMA21 (or broke above 15-bar resistance)
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
    multiplier: float = 100.0
    burst_window_sec: float = 3.0
    decel_window_sec: float = 2.0
    baseline_sec: int = 90
    min_baseline_samples: int = 12
    base_vel_floor: float = 0.18  # minimum baseline $/3s so dead flat ticks never trigger
    vel_mult: float = 2.5  # trigger Leg 1 when live 3s speed >= vel_mult * baseline
    min_burst_price: float = 0.75  # minimum $ move in 3s for Leg 1
    min_efficiency: float = 0.65  # directional straightness over burst window
    max_spread_price: float = 0.90  # skip new entries/adds if news spread blows out above this
    latency_sec: float = 0.25  # realistic fill latency
    add_interval_sec: float = 1.0  # minimum 1 second between winner-pyramid legs
    add_min_profit_price: float = 0.20  # last leg must be >= +$0.20 net of spread before firing next leg
    add_min_vel_ratio: float = 1.4  # still accelerating relative to baseline to add
    initial_stop_mult: float = 1.25  # initial hard SL = max(burst * mult, spread*3.5, min_stop)
    min_stop_price: float = 1.50
    max_stop_price: float = 3.50
    decel_ratio: float = 0.28  # exit when 2s speed drops <= 28% of peak speed
    decel_confirm_ticks: int = 2  # consecutive slow ticks to confirm deceleration
    decel_min_hold_sec: float = 2.0
    trail_arm_price: float = 0.35  # arm tight tick trail once basket avg gain >= $0.35
    trail_min_pullback: float = 0.45
    trail_giveback_frac: float = 0.35
    max_hold_sec: float = 120.0
    win_cooldown_sec: float = 2.0
    loss_cooldown_sec: float = 6.0


@dataclass
class S1VelocitySurferEngine:
    """Stateful real-time 1-second velocity + winner-only 6-leg machine gun + deceleration exit."""

    cfg: S1SurferConfig = field(default_factory=S1SurferConfig)
    initial_capital_usd: float = 1000.0

    # Rolling tick & 1-second history
    ticks: deque[tuple[float, float, float, float]] = field(default_factory=deque)  # (ts, bid, ask, mid)
    sec_series: deque[tuple[int, float]] = field(default_factory=deque)  # (sec_epoch, last_mid)

    # Live position / order state
    pending_order: dict[str, Any] | None = None
    basket: dict[str, Any] | None = None
    cooldown_until: float = 0.0
    next_wave_id: int = 1

    # Stats
    realized_pnl_usd: float = 0.0
    peak_equity_usd: float = 1000.0
    max_dd_usd: float = 0.0
    waves_closed: list[dict[str, Any]] = field(default_factory=list)
    signals_blocked_spread: int = 0
    signals_blocked_m1: int = 0
    last_metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.peak_equity_usd = float(self.initial_capital_usd)

    def _record_tick(self, ts: float, bid: float, ask: float) -> float:
        mid = 0.5 * (bid + ask)
        self.ticks.append((ts, bid, ask, mid))
        keep_sec = max(self.cfg.burst_window_sec + 4.0, 12.0)
        while self.ticks and (ts - self.ticks[0][0]) > keep_sec:
            self.ticks.popleft()

        sec_ep = int(ts)
        if self.sec_series and self.sec_series[-1][0] == sec_ep:
            self.sec_series[-1] = (sec_ep, mid)
        else:
            self.sec_series.append((sec_ep, mid))
        max_hist = self.cfg.baseline_sec + 25
        while len(self.sec_series) > max_hist:
            self.sec_series.popleft()
        return mid

    def _window_move_and_eff(self, ts: float, win_sec: float) -> tuple[float, float, bool]:
        """Return (signed_move, efficiency, has_span) over the last `win_sec` seconds."""
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

    def _baseline_velocity(self, ts: float) -> tuple[float, int]:
        """Rolling median of past 3s absolute moves excluding the active burst window."""
        if len(self.sec_series) < 6:
            return self.cfg.base_vel_floor, 0
        step = max(1, int(round(self.cfg.burst_window_sec)))
        exclude_after = int(ts) - step - 1
        arr = list(self.sec_series)
        samples: list[float] = []
        for idx in range(step, len(arr)):
            t_cur, m_cur = arr[idx]
            if t_cur > exclude_after:
                break
            t_prev, m_prev = arr[idx - step]
            if 1 <= (t_cur - t_prev) <= (step + 3):
                samples.append(abs(m_cur - m_prev))
        if len(samples) < self.cfg.min_baseline_samples:
            return self.cfg.base_vel_floor, len(samples)
        med = float(statistics.median(samples))
        return max(med, self.cfg.base_vel_floor), len(samples)

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
            "peak_vel": round(b["peak_vel"], 3),
            "m1_label": b["m1_label"],
            "net_pnl_usd": pnl,
            "exit_reason": reason,
            "equity_after_usd": round(self.initial_capital_usd + self.realized_pnl_usd, 4),
            "paper_only": True,
            "orders_submitted": 0,
        }
        self.waves_closed.append(rec)
        cd = self.cfg.win_cooldown_sec if pnl > 0 else self.cfg.loss_cooldown_sec
        self.cooldown_until = ts + cd
        return rec

    def _lock_basket_stop(self) -> float:
        """Tighten basket SL to Leg-1 BE at 2 legs and overall profit floor at 3+ legs."""
        assert self.basket is not None
        b = self.basket
        side = b["side"]
        n_legs = len(b["legs"])
        ent1 = float(b["legs"][0]["entry"])
        avg_ent = float(b["avg_entry"])
        cur_sl = float(b["stop"])

        if n_legs == 2:
            # Lock Leg 1 at breakeven (+$0.05 cushion)
            target_sl = (ent1 + 0.05) if side == "buy" else (ent1 - 0.05)
        elif n_legs >= 3:
            # Lock ENTIRE basket average entry at >= breakeven (+$0.05 net cushion)
            target_sl = (avg_ent + 0.05) if side == "buy" else (avg_ent - 0.05)
        else:
            return cur_sl

        new_sl = max(cur_sl, target_sl) if side == "buy" else min(cur_sl, target_sl)
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
        """Process one live Bid/Ask sample (e.g., every 0.25s-0.5s) and return events."""
        events: list[dict[str, Any]] = []
        if bid <= 0 or ask <= 0 or ask < bid:
            return events

        spread = round(ask - bid, 5)
        self._record_tick(ts, bid, ask)
        move_3s, eff_3s, has_3s = self._window_move_and_eff(ts, self.cfg.burst_window_sec)
        move_2s, _, has_2s = self._window_move_and_eff(ts, self.cfg.decel_window_sec)
        base_vel, base_n = self._baseline_velocity(ts)
        vel_ratio = round(abs(move_3s) / base_vel, 2) if (has_3s and base_vel > 0) else 0.0

        self.last_metrics = {
            "ts": ts,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "move_3s": round(move_3s, 3),
            "move_2s": round(move_2s, 3),
            "eff_3s": round(eff_3s, 2),
            "base_vel": round(base_vel, 3),
            "base_samples": base_n,
            "vel_ratio": vel_ratio,
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
                    self.basket = {
                        "wave_id": self.next_wave_id,
                        "side": side,
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
                        "entry": round(fill_px, 4),
                        "stop": stop_px,
                        "vel_ratio": po["vel_ratio"],
                        "move_3s": po["move_3s"],
                        "m1_label": po["m1_label"],
                        "spread": spread,
                    })
                elif po["kind"] == "add" and self.basket is not None and len(self.basket["legs"]) < self.cfg.max_legs:
                    b = self.basket
                    # Re-verify latest leg is still in profit at actual delayed fill tick (no accidental avg-down!)
                    last_ent = float(b["legs"][-1]["entry"])
                    last_net = (bid - last_ent) if side == "buy" else (last_ent - ask)
                    safe_sl = (bid > b["stop"]) if side == "buy" else (ask < b["stop"])
                    if last_net >= self.cfg.add_min_profit_price and safe_sl:
                        old_vol = float(b["total_volume"])
                        new_vol = round(old_vol + self.cfg.leg_lot, 2)
                        new_avg = round((float(b["avg_entry"]) * old_vol + fill_px * self.cfg.leg_lot) / new_vol, 4)
                        b["legs"].append({"entry": round(fill_px, 4), "volume": self.cfg.leg_lot, "ts": ts})
                        b["total_volume"] = new_vol
                        b["avg_entry"] = new_avg
                        b["last_leg_ts"] = ts
                        new_sl = self._lock_basket_stop()
                        events.append({
                            "type": "FIRE_ADD",
                            "wave_id": b["wave_id"],
                            "side": side,
                            "leg_num": len(b["legs"]),
                            "add_price": round(fill_px, 4),
                            "total_volume": new_vol,
                            "avg_entry": new_avg,
                            "stop": new_sl,
                            "last_leg_net": round(last_net, 3),
                            "vel_ratio": vel_ratio,
                        })

        # 2) Manage open basket (Priority: Hard/Locked SL -> Deceleration Exit -> Tick Trail -> Max Hold -> Machine-Gun Add)
        if self.basket is not None:
            b = self.basket
            side = b["side"]
            ex_px = bid if side == "buy" else ask
            hold_sec = ts - b["opened_at"]

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

            # Priority 1: Hard / Locked Basket SL
            sl_hit = (bid <= b["stop"]) if side == "buy" else (ask >= b["stop"])
            if sl_hit:
                reason = "profit_floor_hit" if b["stop_locked"] else "hard_stop_loss"
                closed = self._close_basket(ex_px, ts, reason)
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Priority 2: 1-Second Deceleration / Velocity Stall ("وقتی شتاب کم شد سریع می‌بندم!")
            if hold_sec >= self.cfg.decel_min_hold_sec and has_2s:
                if signed_vel_2s <= (self.cfg.decel_ratio * b["peak_vel"]):
                    b["decel_ticks"] += 1
                else:
                    b["decel_ticks"] = 0
                if b["decel_ticks"] >= self.cfg.decel_confirm_ticks and (avg_gain > 0 or hold_sec >= 5.0):
                    closed = self._close_basket(ex_px, ts, "decel_stall_exit")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events

            # Priority 3: Tight Tick-Trail once profitable
            if avg_gain >= self.cfg.trail_arm_price or (b["stop_locked"] and max_exc >= self.cfg.trail_arm_price):
                allow_pb = max(self.cfg.trail_min_pullback, self.cfg.trail_giveback_frac * max(0.0, max_exc))
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

            # Priority 5: Schedule 1-second Winner-Only Machine Gun Add (Legs 2..max_legs)
            if (
                self.pending_order is None
                and len(b["legs"]) < self.cfg.max_legs
                and (ts - b["last_leg_ts"]) >= self.cfg.add_interval_sec
                and spread <= self.cfg.max_spread_price
            ):
                last_ent = float(b["legs"][-1]["entry"])
                last_net = (bid - last_ent) if side == "buy" else (last_ent - ask)
                if (
                    last_net >= self.cfg.add_min_profit_price
                    and signed_vel_2s >= (0.35 * b["peak_vel"])
                    and vel_ratio >= self.cfg.add_min_vel_ratio
                ):
                    self.pending_order = {
                        "kind": "add",
                        "side": side,
                        "fill_ts": ts + self.cfg.latency_sec,
                    }

            self._update_dd(bid, ask)
            return events

        # 3) Flat: watch for 2.5x+ 1-Second Acceleration aligned with M1 S/R Context
        if self.pending_order is not None or ts < self.cooldown_until or not has_3s:
            self._update_dd(bid, ask)
            return events

        if base_n < self.cfg.min_baseline_samples:
            self._update_dd(bid, ask)
            return events

        min_req_move = max(self.cfg.min_burst_price, spread * 2.2)
        if abs(move_3s) >= min_req_move and vel_ratio >= self.cfg.vel_mult and eff_3s >= self.cfg.min_efficiency:
            burst_dir = 1 if move_3s > 0 else -1
            m1_bias = int(m1_context.get("bias", 0))
            if spread > self.cfg.max_spread_price:
                self.signals_blocked_spread += 1
            elif m1_bias != burst_dir:
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
                    "fill_ts": ts + self.cfg.latency_sec,
                    "stop_dist": round(sdist, 4),
                    "vel_ratio": vel_ratio,
                    "move_3s": round(move_3s, 3),
                    "m1_label": str(m1_context.get("label", "M1_OK")),
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
            "open_basket": self.basket,
            "last_metrics": self.last_metrics,
        }


def append_jsonl_private(path: Path, row: dict[str, Any]) -> None:
    """Append one JSON record to an owner-only (0600) JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.chmod(path, 0o600)


def replay_recorded_ticks(
    tick_jsonl_path: Path,
    *,
    cfg: S1SurferConfig | None = None,
    initial_capital_usd: float = 1000.0,
) -> dict[str, Any]:
    """Deterministically replay a recorded sub-second JSONL tick file offline."""
    eng = S1VelocitySurferEngine(cfg=cfg or S1SurferConfig(), initial_capital_usd=initial_capital_usd)
    tick_count = 0
    last_bid, last_ask = 0.0, 0.0
    with tick_jsonl_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            ts = float(row["ts"])
            bid = float(row["bid"])
            ask = float(row["ask"])
            m1_ctx = row.get("m1") or {"bias": 0, "label": "REPLAY_NONE"}
            eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
            last_bid, last_ask = bid, ask
            tick_count += 1
    # Force-close any open basket at end of recording
    if eng.basket is not None and last_bid > 0 and last_ask > 0:
        ex = last_bid if eng.basket["side"] == "buy" else last_ask
        eng._close_basket(ex, eng.last_metrics.get("ts", 0.0), "eof_replay")
    s = eng.summary(last_bid, last_ask)
    s["ticks_replayed"] = tick_count
    s["waves"] = eng.waves_closed
    return s


def make_synthetic_s1_session() -> tuple[list[list[float]], list[tuple[float, float, float]]]:
    """Deterministic synthetic M1 bearish pullback-resume + 0.25s tick waterfall & deceleration."""
    t0 = 1764080000.0
    # 30 closed M1 bars: downtrend -> 2-bar green pullback -> red resume bar
    m1_bars: list[list[float]] = []
    px = 3520.0
    for i in range(30):
        ts = t0 - (30 - i) * 60.0
        if i in (26, 27):
            chg = +1.1  # 2-bar pullback up
        elif i == 29:
            chg = -1.8  # red resume bar
        else:
            chg = -0.8
        o = px
        c = px + chg
        m1_bars.append([ts, round(o, 2), round(max(o, c) + 0.4, 2), round(min(o, c) - 0.4, 2), round(c, 2), 150.0])
        px = c

    # 140s of 0.25s live ticks: 30s calm noise -> 12s accelerating drop -> 8s stall/bounce
    ticks: list[tuple[float, float, float]] = []
    bid = px
    spread = 0.32
    t = t0 + 10.0
    for step in range(4 * 55):  # 55 seconds @ 4 Hz
        sec = step * 0.25
        if sec < 24.0:
            # Calm normal baseline (~$0.20 per 3s jitter)
            delta = 0.04 if (step % 4 < 2) else -0.04
        elif sec < 36.0:
            # 12-second accelerating waterfall drop (~$1.60 per 3s = ~8x baseline!)
            delta = -0.18
        else:
            # Momentum stalls & slightly bounces -> triggers deceleration exit!
            delta = +0.02
        bid = round(bid + delta, 2)
        ticks.append((t + sec, bid, round(bid + spread, 2)))
    return m1_bars, ticks
