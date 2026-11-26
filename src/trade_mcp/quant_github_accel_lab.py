"""Causal 2nd-Derivative Tick-Acceleration Stacker & GitHub TickRobust Z-Score Mean-Reversion Lab (ORDERS=0).

Implements two institutional / open-source GitHub architectures adapted strictly causally for XAUUSD_i:
  1. RapidBurstAccelEngine (inspired by phubet20/rapid-burst + true 2nd-derivative tick physics):
     - Incremental O(1) tick scanner tracking:
       * Velocity v(t) and TRUE Acceleration a(t) = v_now - v_prev (2nd derivative!)
       * Tick-direction pressure (up-ticks vs down-ticks & bid/ask uptick imbalance)
       * Consecutive directional tick run + 5s directional efficiency + burst Z-score
       * Anti-chase excursion from recent swing + exhaustion/deceleration detection
     - Variable L1 lot size scaled continuously by live slope & acceleration
     - Slope-proportional rapid multi-order stacking (1..6 legs) while acceleration stays positive,
       immediately tightening basket SL to BE / profit-lock on every stack
     - Optional deceleration / momentum-flip profit booking at peaks

  2. TickRobustZScoreEngine (causal Python port of n30dyn4m1c/gold-pro-scalper & ronnisnai-frog/Gold-Scalping):
     - Closed-bar M1/M3/M5 Z-Score mean reversion engineered for real Every-Tick XAUUSD spreads
     - Turn-confirmation bar (never catches a falling knife)
     - ADX sideways regime gate + ATR volatility ratio bounds
     - Cost gate: StdDev >= min_sd_cost_mult * spread AND distance-to-mean >= min_tp_cost_mult * spread
     - Limit-at-mean TP (with Gravity SMA retargeting) + breakeven @ 1x ATR + volatility-aware hard SL
     - Operates 24h (specifically covering quiet Asian/night hours and sideways ranges!)

Strictly read-only / paper-only (ORDERS=0), real Bid/Ask fills, realistic latency, gap-aware stops.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from trade_mcp.s1_velocity_surfer import compute_dynamic_add_lot


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


# =====================================================================
# 1) INCREMENTAL O(1) TICK SCANNER (Velocity + 2nd-Derivative Accel + Flow)
# =====================================================================
class _TickRoll:
    """Rolling [now - span, now] tick delta window with O(1) amortised sums."""

    __slots__ = ("span", "q", "n", "sd", "sa", "up", "dn", "ba", "bt", "edge", "_k")

    def __init__(self, span: float) -> None:
        self.span = span
        self.q: deque[tuple[float, float, float, int, int]] = deque()
        self.n = 0
        self.sd = 0.0
        self.sa = 0.0
        self.up = 0
        self.dn = 0
        self.ba = 0
        self.bt = 0
        self.edge: float | None = None
        self._k = 0

    def reset(self) -> None:
        self.q.clear()
        self.n = 0
        self.sd = self.sa = 0.0
        self.up = self.dn = self.ba = self.bt = 0
        self.edge = None
        self._k = 0

    def push(self, t: float, mid: float, d: float, bas: int, bat: int) -> None:
        self.q.append((t, mid, d, bas, bat))
        self.n += 1
        self.sd += d
        self.sa += abs(d)
        if d > 0:
            self.up += 1
        elif d < 0:
            self.dn += 1
        self.ba += bas
        self.bt += bat

    def expire(self, now: float) -> None:
        lim = now - self.span
        q = self.q
        while q and q[0][0] < lim:
            _, mid, d, bas, bat = q.popleft()
            self.edge = mid
            self.n -= 1
            self.sd -= d
            self.sa -= abs(d)
            if d > 0:
                self.up -= 1
            elif d < 0:
                self.dn -= 1
            self.ba -= bas
            self.bt -= bat
            self._k += 1
        if self._k >= 4000:
            self._k = 0
            self.sd = sum(x[2] for x in q)
            self.sa = sum(abs(x[2]) for x in q)

    def net(self, mid: float) -> float:
        if self.edge is not None:
            return mid - self.edge
        if self.q:
            return mid - self.q[0][1]
        return 0.0

    def eff(self, mid: float) -> float:
        if self.sa <= 1e-9:
            return 0.0
        return abs(self.net(mid)) / self.sa


class _MonotonicExt:
    """Rolling O(1) max/min over [now - span, now]."""

    __slots__ = ("span", "mx", "mn")

    def __init__(self, span: float) -> None:
        self.span = span
        self.mx: deque[tuple[float, float]] = deque()
        self.mn: deque[tuple[float, float]] = deque()

    def reset(self) -> None:
        self.mx.clear()
        self.mn.clear()

    def push_update(self, t: float, v: float) -> None:
        mx, mn = self.mx, self.mn
        while mx and mx[-1][1] <= v:
            mx.pop()
        mx.append((t, v))
        while mn and mn[-1][1] >= v:
            mn.pop()
        mn.append((t, v))
        lim = t - self.span
        while mx and mx[0][0] < lim:
            mx.popleft()
        while mn and mn[0][0] < lim:
            mn.popleft()

    @property
    def hi(self) -> float:
        return self.mx[0][1] if self.mx else 0.0

    @property
    def lo(self) -> float:
        return self.mn[0][1] if self.mn else 0.0


class IncrementalTickPhysics:
    """Second-by-second velocity, TRUE 2nd-derivative acceleration, tick imbalance & anti-chase."""

    def __init__(self, vel_win: float = 10.0) -> None:
        self.vel_win = float(vel_win)
        self.r_v1 = _TickRoll(self.vel_win)
        self.r_v2 = _TickRoll(2.0 * self.vel_win)
        self.r5 = _TickRoll(5.0)
        self.r15 = _TickRoll(15.0)
        self.r60 = _TickRoll(60.0)
        # 1-second sampled rolls for noise-free directional efficiency (removes sub-second bid/ask ping-pong)
        self._s_v1 = _TickRoll(self.vel_win)
        self._s_60 = _TickRoll(60.0)
        self._last_sec: int | None = None
        self._last_sec_mid: float = 0.0
        self.x30 = _MonotonicExt(30.0)
        self.x60 = _MonotonicExt(60.0)
        self.x180 = _MonotonicExt(180.0)
        self.last: tuple[float, float, float, float] | None = None
        self.first_t = 0.0
        self.seq_run = 0
        self.signs: deque[int] = deque(maxlen=12)
        self.sign_sum = 0
        self.n = 0

    def reset(self) -> None:
        for r in (self.r_v1, self.r_v2, self.r5, self.r15, self.r60, self._s_v1, self._s_60):
            r.reset()
        for x in (self.x30, self.x60, self.x180):
            x.reset()
        self._last_sec = None
        self._last_sec_mid = 0.0
        self.last = None
        self.first_t = 0.0
        self.seq_run = 0
        self.signs.clear()
        self.sign_sum = 0
        self.n = 0

    def push(self, t: float, bid: float, ask: float) -> dict[str, float]:
        mid = 0.5 * (bid + ask)
        if self.last is not None and (t - self.last[0]) > 120.0:
            self.reset()

        if self.last is None:
            self.first_t = t
            d, bas, bat = 0.0, 0, 0
        else:
            _, pb, pa, pm = self.last
            d = mid - pm
            bas = (1 if bid > pb else -1 if bid < pb else 0) + (1 if ask > pa else -1 if ask < pa else 0)
            bat = int(bid != pb) + int(ask != pa)
            if d != 0.0:
                s = 1 if d > 0.0 else -1
                if self.seq_run == 0 or (self.seq_run > 0) == (s > 0):
                    self.seq_run += s
                else:
                    self.seq_run = s
                if len(self.signs) == self.signs.maxlen:
                    self.sign_sum -= self.signs[0]
                self.signs.append(s)
                self.sign_sum += s

        sec = int(t)
        if self._last_sec is None:
            self._last_sec = sec
            self._last_sec_mid = mid
            self._s_v1.push(float(sec), mid, 0.0, 0, 0)
            self._s_60.push(float(sec), mid, 0.0, 0, 0)
        elif sec > self._last_sec:
            sd_1s = mid - self._last_sec_mid
            self._s_v1.push(float(sec), mid, sd_1s, 0, 0)
            self._s_60.push(float(sec), mid, sd_1s, 0, 0)
            self._s_v1.expire(float(sec))
            self._s_60.expire(float(sec))
            self._last_sec = sec
            self._last_sec_mid = mid

        self.last = (t, bid, ask, mid)
        self.n += 1
        for r in (self.r_v1, self.r_v2, self.r5, self.r15, self.r60):
            r.push(t, mid, d, bas, bat)
            r.expire(t)
        for x in (self.x30, self.x60, self.x180):
            x.push_update(t, mid)

        span = t - self.first_t
        m_v1 = self.r_v1.net(mid)
        m_v2 = self.r_v2.net(mid)
        # Velocity in $/sec over [t-W, t] and over previous [t-2W, t-W]
        vel_now = m_v1 / self.vel_win
        vel_prev = (m_v2 - m_v1) / self.vel_win
        # TRUE 2nd-derivative acceleration (change in velocity $/sec across consecutive W-second windows!)
        accel = vel_now - vel_prev

        m5 = self.r5.net(mid)
        m15 = self.r15.net(mid)
        m60 = self.r60.net(mid)
        eff_v1 = self._s_v1.eff(mid)
        eff60 = self._s_60.eff(mid)

        ud_tot = self.r15.up + self.r15.dn
        imb = (self.r15.up - self.r15.dn) / ud_tot if ud_tot > 0 else 0.0
        ba_imb = (self.r15.ba / self.r15.bt) if self.r15.bt > 0 else 0.0
        flow = 0.5 * imb + 0.5 * ba_imb
        consist = (self.sign_sum / len(self.signs)) if self.signs else 0.0

        # Anti-chase distances from 3m swing low/high
        chase_up = max(0.0, mid - self.x180.lo)
        chase_dn = max(0.0, self.x180.hi - mid)
        pb_from_30_hi = max(0.0, self.x30.hi - mid)
        pb_from_30_lo = max(0.0, mid - self.x30.lo)

        return {
            "ready": 1.0 if (span >= max(2.0 * self.vel_win, 45.0) and self.n >= 20) else 0.0,
            "mid": mid,
            "spread": ask - bid,
            "m_v1": m_v1,
            "m5": m5,
            "m15": m15,
            "m60": m60,
            "vel_now": vel_now,
            "vel_prev": vel_prev,
            "accel": accel,
            "eff_v1": eff_v1,
            "eff60": eff60,
            "flow": flow,
            "consist": consist,
            "seq_run": float(self.seq_run),
            "chase_up": chase_up,
            "chase_dn": chase_dn,
            "pb_30_hi": pb_from_30_hi,
            "pb_30_lo": pb_from_30_lo,
            "rng_60": self.x60.hi - self.x60.lo,
        }


# =====================================================================
# 2) RAPID-BURST SLOPE × ACCELERATION VARIABLE-LOT STACKING ENGINE
# =====================================================================
@dataclass
class RapidBurstAccelConfig:
    """Configuration for 2nd-derivative Tick-Acceleration Variable-Lot & Slope-Stacking Engine."""

    name: str = "RB_ACCEL_STACK"
    vel_win_sec: float = 10.0

    # Signal thresholds (all strictly causal!)
    min_vel_per_sec: float = 0.16  # e.g. >= $1.60 in 10s ($0.16/sec)
    require_accel: bool = True  # TRUE 2nd-derivative filter: vel_now > vel_prev!
    min_accel_per_sec: float = 0.05  # acceleration >= $0.05/sec increase in speed!
    min_60s_move: float = 4.50  # 60s move aligned with burst (blocks 2s micro-noise)
    min_eff_v1: float = 0.42
    min_eff_60: float = 0.35
    min_flow: float = 0.08  # tick-direction + bid/ask pressure proxy
    max_chase_price: float = 12.00  # anti-chase ceiling from 3m swing (0 = off)
    max_pb_30s: float = 0.85  # don't enter after already snapping back off 30s extreme
    max_spread_price: float = 0.65

    # Variable lot size & slope-proportional multi-order stacking
    base_lot: float = 0.02
    variable_l1_lot: bool = True  # scale L1 directly by slope & acceleration!
    slope_ref_per_sec: float = 0.20  # 1x slope reference ($0.20/sec)
    max_l1_lot: float = 0.06
    max_l1_risk_usd: float = 0.0  # >0 caps L1 lot so 1 fakeout <= max_l1_risk_usd while keeping full SL room!
    enable_slope_stacking: bool = True  # number of stacked legs scales directly with slope!
    risk_free_stack_sizing: bool = True  # algebraically guarantees every added leg locks basket >= breakeven!
    max_legs: int = 6
    add_step_price: float = 1.40  # rapid slope stacking step (much faster than old $2.40/30s!)
    add_interval_sec: float = 4.0  # rapid 4s stack cadence while accelerating!
    dyn_max_leg_lot: float = 0.08
    dyn_max_total_lot: float = 0.40
    dyn_min_lock_profit: float = 0.20

    # Risk & Deceleration Exit
    hard_stop_price: float = 2.60
    sl_min_breathing_room: float = 1.20
    be_arm_price: float = 1.40
    be_lock_profit: float = 0.15
    ratchet_arm_price: float = 2.20
    ratchet_frac: float = 0.58
    ratchet_breathing_room: float = 1.15
    trail_arm_price: float = 3.00
    trail_pullback_price: float = 1.90
    decel_exit_enabled: bool = True  # book profits fast when acceleration flips negative after a run!
    decel_exit_min_gain: float = 2.40
    decel_vel_ratio: float = 0.15  # exit when vel drops below 15% of peak_vel (or flips sign)
    max_hold_sec: float = 240.0
    runner_max_hold_sec: float = 720.0

    # Session & cooldowns
    active_server_hours: tuple[int, ...] | None = (9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20)
    win_cooldown_sec: float = 25.0
    loss_cooldown_sec: float = 90.0
    consec_loss_cooldown_sec: float = 240.0
    latency_sec: float = 0.25
    multiplier: float = 100.0


@dataclass
class RapidBurstAccelEngine:
    """Strictly causal 2nd-derivative tick-acceleration + slope-proportional stacking engine (ORDERS=0)."""

    cfg: RapidBurstAccelConfig = field(default_factory=RapidBurstAccelConfig)
    initial_capital_usd: float = 1000.0

    _phys: IncrementalTickPhysics = field(init=False)
    pending: dict[str, Any] | None = None
    basket: dict[str, Any] | None = None
    cooldown_until: float = 0.0
    consecutive_losses: int = 0
    next_wave_id: int = 1
    realized_pnl_usd: float = 0.0
    peak_equity_usd: float = 1000.0
    max_dd_usd: float = 0.0
    waves_closed: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._phys = IncrementalTickPhysics(vel_win=self.cfg.vel_win_sec)
        self.peak_equity_usd = float(self.initial_capital_usd)

    def _in_session(self, ts: float) -> bool:
        if not self.cfg.active_server_hours:
            return True
        return datetime.fromtimestamp(ts, tz=timezone.utc).hour in self.cfg.active_server_hours

    def _slope_target_legs(self, abs_vel: float, dir_accel: float) -> int:
        """How many stacked orders this slope deserves (directly proportional to slope & acceleration!)."""
        if not self.cfg.enable_slope_stacking:
            return 1
        slope_mult = abs_vel / max(0.05, self.cfg.slope_ref_per_sec)
        accel_bonus = 1 if dir_accel >= 0.12 else 0
        raw = int(round(slope_mult * 1.8)) + accel_bonus
        return max(1, min(self.cfg.max_legs, raw))

    def _slope_l1_lot(self, abs_vel: float, dir_accel: float) -> float:
        """Variable L1 lot scaled smoothly with live slope & acceleration."""
        if not self.cfg.variable_l1_lot:
            return round(self.cfg.base_lot, 2)
        slope_ratio = abs_vel / max(0.05, self.cfg.slope_ref_per_sec)
        accel_mult = 1.0 + _clip(dir_accel / max(0.05, self.cfg.slope_ref_per_sec), 0.0, 1.0) * 0.65
        raw = round(self.cfg.base_lot * _clip(slope_ratio * accel_mult, 0.5, 3.5), 2)
        return max(0.01, min(round(self.cfg.max_l1_lot, 2), raw))

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
        self.max_dd_usd = round(max(self.max_dd_usd, self.peak_equity_usd - eq), 4)

    def _close_basket(self, exit_px: float, ts: float, reason: str) -> dict[str, Any]:
        assert self.basket is not None
        b = self.basket
        self.basket = None
        self.pending = None
        diff = (exit_px - b["avg_entry"]) if b["side"] == "buy" else (b["avg_entry"] - exit_px)
        pnl = round(diff * b["total_volume"] * self.cfg.multiplier, 4)
        self.realized_pnl_usd = round(self.realized_pnl_usd + pnl, 4)
        rec = {
            "wave_id": b["wave_id"],
            "side": b["side"],
            "regime": self.cfg.name,
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

    def on_tick(self, *, ts: float, bid: float, ask: float) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return events
        ph = self._phys.push(ts, bid, ask)
        spread = ph["spread"]

        # 1) Execute pending fill
        if self.pending is not None and ts >= self.pending["fill_ts"]:
            pend = self.pending
            self.pending = None
            side = pend["side"]
            fill_px = ask if side == "buy" else bid

            if pend["kind"] == "entry" and self.basket is None:
                lot = float(pend["lot"])
                sl_dist = self.cfg.hard_stop_price
                if self.cfg.max_l1_risk_usd > 0.0:
                    risk_per_lot = max(0.50, sl_dist + spread) * self.cfg.multiplier
                    max_safe = max(0.01, math.floor((self.cfg.max_l1_risk_usd / risk_per_lot) * 100.0) / 100.0)
                    lot = min(lot, max_safe)
                stop = round(fill_px - sl_dist, 4) if side == "buy" else round(fill_px + sl_dist, 4)
                wid = self.next_wave_id
                self.next_wave_id += 1
                self.basket = {
                    "wave_id": wid,
                    "side": side,
                    "opened_at": ts,
                    "last_add_ts": ts,
                    "legs": [{"leg": 1, "entry": fill_px, "vol": lot, "ts": ts}],
                    "total_volume": lot,
                    "avg_entry": fill_px,
                    "best_px": bid if side == "buy" else ask,
                    "peak_vel": float(pend["abs_vel"]),
                    "stop": stop,
                    "stop_locked": False,
                }
                events.append({
                    "type": "OPEN_L1",
                    "wave_id": wid,
                    "side": side,
                    "entry": fill_px,
                    "leg_lot": lot,
                    "total_volume": lot,
                    "stop": stop,
                })
            elif pend["kind"] == "add" and self.basket is not None:
                b = self.basket
                cur_vol = float(b["total_volume"])
                cur_avg = float(b["avg_entry"])
                dir_m60 = ph["m60"] if side == "buy" else -ph["m60"]
                if self.cfg.risk_free_stack_sizing:
                    # Algebraic risk-free pyramid sizing:
                    # Guarantees new_avg + min_lock_profit <= bid - breathing (BUY) so stacking NEVER turns a winner into a loss!
                    breath = self.cfg.sl_min_breathing_room
                    lock_p = self.cfg.dyn_min_lock_profit
                    add_lot = 0.0
                    if side == "buy":
                        target_avg = bid - breath - lock_p
                        if target_avg > cur_avg and fill_px > target_avg + 1e-6:
                            safe_max = cur_vol * (target_avg - cur_avg) / (fill_px - target_avg)
                            slope_want = self._slope_l1_lot(abs(ph["vel_now"]), ph["accel"]) * 1.4
                            raw = min(safe_max, slope_want, self.cfg.dyn_max_leg_lot, max(0.0, self.cfg.dyn_max_total_lot - cur_vol))
                            add_lot = math.floor(raw * 100.0) / 100.0
                    else:
                        target_avg = ask + breath + lock_p
                        if target_avg < cur_avg and target_avg > fill_px + 1e-6:
                            safe_max = cur_vol * (cur_avg - target_avg) / (target_avg - fill_px)
                            slope_want = self._slope_l1_lot(abs(ph["vel_now"]), -ph["accel"]) * 1.4
                            raw = min(safe_max, slope_want, self.cfg.dyn_max_leg_lot, max(0.0, self.cfg.dyn_max_total_lot - cur_vol))
                            add_lot = math.floor(raw * 100.0) / 100.0
                else:
                    add_lot = compute_dynamic_add_lot(
                        side=side,
                        bid=bid,
                        ask=ask,
                        fill_px=fill_px,
                        old_vol=cur_vol,
                        old_avg=cur_avg,
                        breathing_room=self.cfg.sl_min_breathing_room,
                        base_lot=self.cfg.base_lot,
                        max_leg_lot=self.cfg.dyn_max_leg_lot,
                        max_total_lot=self.cfg.dyn_max_total_lot,
                        dir_move_60s=max(dir_m60, 0.0),
                        hold_sec=max(0.0, ts - float(b["opened_at"])),
                        min_lock_profit=self.cfg.dyn_min_lock_profit,
                    )
                if add_lot >= 0.01:
                    new_vol = round(cur_vol + add_lot, 2)
                    new_avg = round((cur_avg * cur_vol + fill_px * add_lot) / new_vol, 4)
                    # Final hard safety verification when risk_free_stack_sizing=True
                    if side == "buy":
                        lock_sl = round(min(new_avg + self.cfg.dyn_min_lock_profit, bid - self.cfg.sl_min_breathing_room), 4)
                        safe_ok = (not self.cfg.risk_free_stack_sizing) or (lock_sl >= new_avg)
                    else:
                        lock_sl = round(max(new_avg - self.cfg.dyn_min_lock_profit, ask + self.cfg.sl_min_breathing_room), 4)
                        safe_ok = (not self.cfg.risk_free_stack_sizing) or (lock_sl <= new_avg)
                    if safe_ok:
                        b["legs"].append({"leg": len(b["legs"]) + 1, "entry": fill_px, "vol": add_lot, "ts": ts})
                        b["total_volume"] = new_vol
                        b["avg_entry"] = new_avg
                        b["last_add_ts"] = ts
                        if (side == "buy" and lock_sl > b["stop"]) or (side == "sell" and lock_sl < b["stop"]):
                            b["stop"] = lock_sl
                            b["stop_locked"] = True
                        events.append({
                            "type": "FIRE_ADD",
                            "wave_id": b["wave_id"],
                            "side": side,
                            "add_price": fill_px,
                            "leg_lot": add_lot,
                            "total_volume": new_vol,
                            "stop": b["stop"],
                        })

        # 2) Manage open basket
        if self.basket is not None:
            b = self.basket
            side = b["side"]
            sdir = 1 if side == "buy" else -1
            ex_px = bid if side == "buy" else ask
            dir_vel = ph["vel_now"] * sdir
            dir_accel = ph["accel"] * sdir
            dir_m60 = ph["m60"] * sdir
            if dir_vel > b["peak_vel"]:
                b["peak_vel"] = dir_vel

            if side == "buy":
                b["best_px"] = max(b["best_px"], bid)
                avg_gain = bid - b["avg_entry"]
                pullback = b["best_px"] - bid
                last_net = bid - b["legs"][-1]["entry"]
            else:
                b["best_px"] = min(b["best_px"], ask)
                avg_gain = b["avg_entry"] - ask
                pullback = ask - b["best_px"]
                last_net = b["legs"][-1]["entry"] - ask

            # Early BE lock
            if not b["stop_locked"] and avg_gain >= self.cfg.be_arm_price:
                if side == "buy":
                    be_sl = round(min(b["avg_entry"] + self.cfg.be_lock_profit, bid - 0.65), 4)
                    if be_sl > b["stop"]:
                        b["stop"] = be_sl
                        b["stop_locked"] = True
                else:
                    be_sl = round(max(b["avg_entry"] - self.cfg.be_lock_profit, ask + 0.65), 4)
                    if be_sl < b["stop"]:
                        b["stop"] = be_sl
                        b["stop_locked"] = True

            # Continuous profit ratchet
            if avg_gain >= self.cfg.ratchet_arm_price:
                if side == "buy":
                    r_sl = round(min(b["avg_entry"] + self.cfg.ratchet_frac * avg_gain, bid - self.cfg.ratchet_breathing_room), 4)
                    if r_sl > b["stop"]:
                        b["stop"] = r_sl
                        b["stop_locked"] = True
                else:
                    r_sl = round(max(b["avg_entry"] - self.cfg.ratchet_frac * avg_gain, ask + self.cfg.ratchet_breathing_room), 4)
                    if r_sl < b["stop"]:
                        b["stop"] = r_sl
                        b["stop_locked"] = True

            # Priority 1: Hard / Locked SL
            if (side == "buy" and bid <= b["stop"]) or (side == "sell" and ask >= b["stop"]):
                reason = (
                    ("profit_floor_hit" if avg_gain >= 0 else "locked_sl_gap_slip")
                    if b["stop_locked"]
                    else "hard_stop_loss"
                )
                closed = self._close_basket(ex_px, ts, reason)
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Priority 2: Deceleration / Velocity-Decay Peak Profit Booking
            if (
                self.cfg.decel_exit_enabled
                and avg_gain >= self.cfg.decel_exit_min_gain
                and dir_accel < 0.0
                and dir_vel <= self.cfg.decel_vel_ratio * b["peak_vel"]
                and (ph["m5"] * sdir) < 0.0
            ):
                closed = self._close_basket(ex_px, ts, "decel_peak_book")
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Priority 3: Pullback Trail
            if avg_gain >= self.cfg.trail_arm_price and pullback >= self.cfg.trail_pullback_price:
                closed = self._close_basket(ex_px, ts, "accel_trail_exit")
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Priority 4: Max Hold
            hold_lim = self.cfg.runner_max_hold_sec if len(b["legs"]) >= 2 else self.cfg.max_hold_sec
            if (ts - b["opened_at"]) >= hold_lim:
                closed = self._close_basket(ex_px, ts, "max_hold_exit")
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Priority 5: Slope-Proportional Rapid Multi-Order Stacking!
            target_legs = self._slope_target_legs(max(0.0, dir_vel), dir_accel)
            if (
                self.cfg.enable_slope_stacking
                and self.pending is None
                and len(b["legs"]) < target_legs
                and spread <= self.cfg.max_spread_price
                and (ts - b["last_add_ts"]) >= self.cfg.add_interval_sec
                and last_net >= self.cfg.add_step_price
                and dir_vel >= self.cfg.min_vel_per_sec * 0.70
                and (not self.cfg.require_accel or dir_accel >= -0.02)
            ):
                est_fill = ask if side == "buy" else bid
                if self.cfg.risk_free_stack_sizing:
                    breath = self.cfg.sl_min_breathing_room
                    lock_p = self.cfg.dyn_min_lock_profit
                    cur_vol = float(b["total_volume"])
                    cur_avg = float(b["avg_entry"])
                    cand = 0.0
                    if side == "buy":
                        target_avg = bid - breath - lock_p
                        if target_avg > cur_avg and est_fill > target_avg + 1e-6:
                            cand = math.floor(
                                min(
                                    cur_vol * (target_avg - cur_avg) / (est_fill - target_avg),
                                    self.cfg.dyn_max_leg_lot,
                                    max(0.0, self.cfg.dyn_max_total_lot - cur_vol),
                                )
                                * 100.0
                            ) / 100.0
                    else:
                        target_avg = ask + breath + lock_p
                        if target_avg < cur_avg and target_avg > est_fill + 1e-6:
                            cand = math.floor(
                                min(
                                    cur_vol * (cur_avg - target_avg) / (target_avg - est_fill),
                                    self.cfg.dyn_max_leg_lot,
                                    max(0.0, self.cfg.dyn_max_total_lot - cur_vol),
                                )
                                * 100.0
                            ) / 100.0
                else:
                    cand = compute_dynamic_add_lot(
                        side=side,
                        bid=bid,
                        ask=ask,
                        fill_px=est_fill,
                        old_vol=float(b["total_volume"]),
                        old_avg=float(b["avg_entry"]),
                        breathing_room=self.cfg.sl_min_breathing_room,
                        base_lot=self.cfg.base_lot,
                        max_leg_lot=self.cfg.dyn_max_leg_lot,
                        max_total_lot=self.cfg.dyn_max_total_lot,
                        dir_move_60s=max(dir_m60, 0.0),
                        hold_sec=max(0.0, ts - b["opened_at"]),
                        min_lock_profit=self.cfg.dyn_min_lock_profit,
                    )
                if cand >= 0.01:
                    self.pending = {"kind": "add", "side": side, "fill_ts": ts + self.cfg.latency_sec}

            self._update_dd(bid, ask)
            return events

        # 3) Flat: evaluate strictly causal velocity + TRUE 2nd-derivative acceleration signal
        if (
            self.pending is None
            and ts >= self.cooldown_until
            and ph["ready"] > 0.5
            and spread <= self.cfg.max_spread_price
            and self._in_session(ts)
        ):
            abs_vel = abs(ph["vel_now"])
            if abs_vel >= self.cfg.min_vel_per_sec and ph["eff_v1"] >= self.cfg.min_eff_v1:
                sdir = 1 if ph["vel_now"] > 0.0 else -1
                dir_accel = ph["accel"] * sdir
                dir_m60 = ph["m60"] * sdir
                dir_flow = ph["flow"] * sdir
                chase = ph["chase_up"] if sdir == 1 else ph["chase_dn"]
                pb30 = ph["pb_30_hi"] if sdir == 1 else ph["pb_30_lo"]

                accel_ok = (not self.cfg.require_accel) or (dir_accel >= self.cfg.min_accel_per_sec)
                m60_ok = (self.cfg.min_60s_move <= 0.0) or (dir_m60 >= self.cfg.min_60s_move and ph["eff60"] >= self.cfg.min_eff_60)
                flow_ok = dir_flow >= self.cfg.min_flow
                chase_ok = (self.cfg.max_chase_price <= 0.0) or (chase <= self.cfg.max_chase_price)
                pb_ok = pb30 <= self.cfg.max_pb_30s

                if accel_ok and m60_ok and flow_ok and chase_ok and pb_ok:
                    lot = self._slope_l1_lot(abs_vel, max(0.0, dir_accel))
                    self.pending = {
                        "kind": "entry",
                        "side": "buy" if sdir == 1 else "sell",
                        "lot": lot,
                        "abs_vel": abs_vel,
                        "fill_ts": ts + self.cfg.latency_sec,
                    }

        self._update_dd(bid, ask)
        return events

    def force_close_open(self, *, ts: float, bid: float, ask: float, reason: str = "eof_replay") -> dict[str, Any] | None:
        if self.basket is None:
            return None
        ex_px = bid if self.basket["side"] == "buy" else ask
        closed = self._close_basket(ex_px, ts, reason)
        self._update_dd(bid, ask)
        return closed


# =====================================================================
# 3) GITHUB TICK-ROBUST CLOSED-BAR Z-SCORE MEAN REVERSION (24H + NIGHT!)
#    (Causal port of n30dyn4m1c/gold-pro-scalper & GoldReversionGuard)
# =====================================================================
@dataclass
class TickRobustZScoreConfig:
    """Closed-bar Z-Score Mean Reversion built for real Every-Tick XAUUSD spreads."""

    name: str = "GH_ZSCORE_M1_24H"
    bar_sec: int = 60  # 60=M1, 180=M3, 300=M5
    mean_period: int = 20
    atr_period: int = 14
    adx_period: int = 14
    atr_avg_period: int = 40

    # Core GitHub TickRobust rules
    entry_z: float = 2.2
    exit_z: float = 0.20
    require_turn: bool = True  # NO falling-knife entries!
    adx_max: float = 24.0  # sideways/quiet regime only
    atr_min_ratio: float = 0.40
    atr_max_ratio: float = 2.00
    min_sd_cost_mult: float = 2.5  # StdDev >= X * spread
    min_tp_cost_mult: float = 3.5  # distance to SMA (TP) >= Y * spread
    max_spread_price: float = 0.55

    # Optional adaptive Night-regime overrides (21:00-08:59 SRV)
    night_adaptive: bool = False
    night_hours: tuple[int, ...] = (21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8)
    night_entry_z: float = 2.15
    night_adx_max: float = 24.0
    night_sd_cost_mult: float = 2.2
    night_tp_cost_mult: float = 3.2

    # Risk & limit-at-mean TP management
    leg_lot: float = 0.02
    variable_lot_by_z: bool = True  # larger Z stretch -> slightly larger lot (bounded)
    max_lot: float = 0.04
    min_sl_price: float = 4.50
    sl_atr_mult: float = 2.5
    max_sl_price: float = 8.50
    be_atr_mult: float = 1.0  # move SL to BE + spread once 1x ATR in profit
    gravity_tp_retarget: bool = True  # retarget limit TP to fresh SMA each closed bar
    max_hold_bars: int = 35
    loss_cooldown_bars: int = 6

    active_server_hours: tuple[int, ...] | None = None  # None = 24h (includes Night/Asia!)
    multiplier: float = 100.0


@dataclass
class TickRobustZScoreEngine:
    """Strictly causal closed-bar Z-Score mean reversion engine (ORDERS=0)."""

    cfg: TickRobustZScoreConfig = field(default_factory=TickRobustZScoreConfig)
    initial_capital_usd: float = 1000.0

    _bars: deque[dict[str, float]] = field(default_factory=lambda: deque(maxlen=200))
    _cur_bar: dict[str, float] | None = None
    _tr_hist: deque[float] = field(default_factory=lambda: deque(maxlen=120))
    _atr_hist: deque[float] = field(default_factory=lambda: deque(maxlen=120))
    _plus_dm: deque[float] = field(default_factory=lambda: deque(maxlen=60))
    _minus_dm: deque[float] = field(default_factory=lambda: deque(maxlen=60))
    _dx_hist: deque[float] = field(default_factory=lambda: deque(maxlen=60))
    _prev_ts: float | None = None

    basket: dict[str, Any] | None = None
    cooldown_bars_left: int = 0
    next_wave_id: int = 1
    realized_pnl_usd: float = 0.0
    peak_equity_usd: float = 1000.0
    max_dd_usd: float = 0.0
    waves_closed: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.peak_equity_usd = float(self.initial_capital_usd)

    def _in_session(self, ts: float) -> bool:
        if not self.cfg.active_server_hours:
            return True
        return datetime.fromtimestamp(ts, tz=timezone.utc).hour in self.cfg.active_server_hours

    def _push_closed_bar(self, bar: dict[str, float]) -> None:
        if self._bars:
            prev = self._bars[-1]
            tr = max(bar["h"] - bar["l"], abs(bar["h"] - prev["c"]), abs(bar["l"] - prev["c"]))
            up_m = bar["h"] - prev["h"]
            dn_m = prev["l"] - bar["l"]
            pdm = up_m if (up_m > dn_m and up_m > 0.0) else 0.0
            mdm = dn_m if (dn_m > up_m and dn_m > 0.0) else 0.0
        else:
            tr = bar["h"] - bar["l"]
            pdm = mdm = 0.0

        self._bars.append(bar)
        self._tr_hist.append(tr)
        self._plus_dm.append(pdm)
        self._minus_dm.append(mdm)

        p = self.cfg.atr_period
        if len(self._tr_hist) >= p:
            atr_val = sum(list(self._tr_hist)[-p:]) / p
            self._atr_hist.append(atr_val)
        ap = self.cfg.adx_period
        if len(self._tr_hist) >= ap:
            tr_sum = sum(list(self._tr_hist)[-ap:])
            if tr_sum > 1e-9:
                pdi = 100.0 * sum(list(self._plus_dm)[-ap:]) / tr_sum
                mdi = 100.0 * sum(list(self._minus_dm)[-ap:]) / tr_sum
                dx = 100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 1e-9 else 0.0
                self._dx_hist.append(dx)

    def _indicators(self) -> dict[str, float] | None:
        n = max(self.cfg.mean_period, self.cfg.atr_period)
        if len(self._bars) < n + 2 or not self._atr_hist:
            return None
        closes = [b["c"] for b in list(self._bars)[-self.cfg.mean_period :]]
        mean = sum(closes) / len(closes)
        var = sum((c - mean) ** 2 for c in closes) / len(closes)
        sd = math.sqrt(max(0.0, var))
        atr = self._atr_hist[-1]
        atr_avg = (
            sum(list(self._atr_hist)[-self.cfg.atr_avg_period :]) / min(len(self._atr_hist), self.cfg.atr_avg_period)
        )
        adx = (
            sum(list(self._dx_hist)[-self.cfg.adx_period :]) / min(len(self._dx_hist), self.cfg.adx_period)
            if self._dx_hist
            else 0.0
        )
        c1 = self._bars[-1]["c"]
        c2 = self._bars[-2]["c"]
        z1 = (c1 - mean) / sd if sd > 1e-9 else 0.0
        z2 = (c2 - mean) / sd if sd > 1e-9 else 0.0
        return {
            "mean": mean,
            "sd": sd,
            "atr": atr,
            "atr_ratio": (atr / atr_avg) if atr_avg > 1e-9 else 1.0,
            "adx": adx,
            "c1": c1,
            "c2": c2,
            "z1": z1,
            "z2": z2,
        }

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
        self.max_dd_usd = round(max(self.max_dd_usd, self.peak_equity_usd - eq), 4)

    def _close_pos(self, exit_px: float, ts: float, reason: str) -> dict[str, Any]:
        assert self.basket is not None
        b = self.basket
        self.basket = None
        diff = (exit_px - b["avg_entry"]) if b["side"] == "buy" else (b["avg_entry"] - exit_px)
        pnl = round(diff * b["total_volume"] * self.cfg.multiplier, 4)
        self.realized_pnl_usd = round(self.realized_pnl_usd + pnl, 4)
        rec = {
            "wave_id": b["wave_id"],
            "side": b["side"],
            "regime": self.cfg.name,
            "legs_count": 1,
            "total_volume": b["total_volume"],
            "avg_entry": b["avg_entry"],
            "leg1_entry": b["avg_entry"],
            "exit_price": round(exit_px, 4),
            "stop_final": b["stop"],
            "opened_at": b["opened_at"],
            "opened_utc": _iso_utc(b["opened_at"]),
            "closed_at": float(ts),
            "closed_utc": _iso_utc(ts),
            "hold_seconds": round(max(0.0, ts - b["opened_at"]), 2),
            "net_pnl_usd": pnl,
            "exit_reason": reason,
            "equity_after_usd": round(self.initial_capital_usd + self.realized_pnl_usd, 4),
            "paper_only": True,
            "orders_submitted": 0,
        }
        self.waves_closed.append(rec)
        if pnl <= 0:
            self.cooldown_bars_left = self.cfg.loss_cooldown_bars
        return rec

    def on_tick(self, *, ts: float, bid: float, ask: float) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return events
        mid = 0.5 * (bid + ask)
        spread = ask - bid

        # Handle discontinuous file gaps cleanly
        if self._prev_ts is not None and (ts - self._prev_ts) > max(600.0, self.cfg.bar_sec * 6.0):
            self._cur_bar = None
            self._bars.clear()
            self._tr_hist.clear()
            self._atr_hist.clear()
            self._plus_dm.clear()
            self._minus_dm.clear()
            self._dx_hist.clear()
        self._prev_ts = ts

        # 1) Real-tick SL & Server-Side Limit TP execution (every tick!)
        if self.basket is not None:
            b = self.basket
            side = b["side"]
            if side == "buy":
                if bid <= b["stop"]:
                    closed = self._close_pos(bid, ts, "be_stop_hit" if b["be_armed"] else "hard_stop_loss")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events
                # Limit TP fills at bid >= tp (exact Bid/Ask realism, zero extra market slippage!)
                if bid >= b["tp"]:
                    closed = self._close_pos(b["tp"], ts, "limit_tp_mean")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events
            else:
                if ask >= b["stop"]:
                    closed = self._close_pos(ask, ts, "be_stop_hit" if b["be_armed"] else "hard_stop_loss")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events
                if ask <= b["tp"]:
                    closed = self._close_pos(b["tp"], ts, "limit_tp_mean")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events

        # 2) Build closed bars strictly causally from ticks
        bar_id = int(ts // self.cfg.bar_sec)
        bar_closed = False
        if self._cur_bar is None:
            self._cur_bar = {"bar_id": float(bar_id), "o": mid, "h": mid, "l": mid, "c": mid}
        elif bar_id > int(self._cur_bar["bar_id"]):
            self._push_closed_bar(self._cur_bar)
            self._cur_bar = {"bar_id": float(bar_id), "o": mid, "h": mid, "l": mid, "c": mid}
            bar_closed = True
        else:
            self._cur_bar["h"] = max(self._cur_bar["h"], mid)
            self._cur_bar["l"] = min(self._cur_bar["l"], mid)
            self._cur_bar["c"] = mid

        if not bar_closed:
            self._update_dd(bid, ask)
            return events

        if self.cooldown_bars_left > 0:
            self.cooldown_bars_left -= 1

        ind = self._indicators()
        if ind is None:
            self._update_dd(bid, ask)
            return events

        # 3) Once-per-closed-bar position management (exactly per TickRobust!)
        if self.basket is not None:
            b = self.basket
            b["bars_held"] += 1
            side = b["side"]
            z1 = ind["z1"]
            # Backup Z-reverted close
            if (side == "buy" and z1 >= -self.cfg.exit_z) or (side == "sell" and z1 <= self.cfg.exit_z):
                ex_px = bid if side == "buy" else ask
                closed = self._close_pos(ex_px, ts, "mean_z_exit")
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events
            # Time exit
            if b["bars_held"] >= self.cfg.max_hold_bars:
                ex_px = bid if side == "buy" else ask
                closed = self._close_pos(ex_px, ts, "max_hold_bars")
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events
            # Breakeven @ 1x ATR in profit
            if self.cfg.be_atr_mult > 0.0 and not b["be_armed"]:
                be_trig = self.cfg.be_atr_mult * ind["atr"]
                if side == "buy" and (bid - b["avg_entry"]) >= be_trig:
                    b["stop"] = max(b["stop"], round(b["avg_entry"] + spread + 0.05, 4))
                    b["be_armed"] = True
                    b["stop_locked"] = True
                elif side == "sell" and (b["avg_entry"] - ask) >= be_trig:
                    b["stop"] = min(b["stop"], round(b["avg_entry"] - spread - 0.05, 4))
                    b["be_armed"] = True
                    b["stop_locked"] = True
            # Gravity TP retarget to fresh SMA (never below cost-covering floor)
            if self.cfg.gravity_tp_retarget:
                if side == "buy":
                    floor_tp = b["avg_entry"] + spread + 0.15
                    target = max(ind["mean"], floor_tp)
                    if target < b["tp"] - 0.20 and target > bid + 0.10:
                        b["tp"] = round(target, 4)
                else:
                    floor_tp = b["avg_entry"] - spread - 0.15
                    target = min(ind["mean"], floor_tp)
                    if target > b["tp"] + 0.20 and target < ask - 0.10:
                        b["tp"] = round(target, 4)

            self._update_dd(bid, ask)
            return events

        # 4) Flat: evaluate closed-bar TickRobust Z-Score entry
        if self.cooldown_bars_left == 0 and spread <= self.cfg.max_spread_price and self._in_session(ts):
            is_night = self.cfg.night_adaptive and (
                datetime.fromtimestamp(ts, tz=timezone.utc).hour in self.cfg.night_hours
            )
            eff_entry_z = self.cfg.night_entry_z if is_night else self.cfg.entry_z
            eff_adx_max = self.cfg.night_adx_max if is_night else self.cfg.adx_max
            eff_sd_mult = self.cfg.night_sd_cost_mult if is_night else self.cfg.min_sd_cost_mult
            eff_tp_mult = self.cfg.night_tp_cost_mult if is_night else self.cfg.min_tp_cost_mult

            z1 = ind["z1"]
            z2 = ind["z2"]
            # Allow turn bar right after a >=eff_entry_z extreme (still stretched >= 0.70*eff_entry_z)
            sdir = 0
            if z1 <= -eff_entry_z or (z2 <= -eff_entry_z and z1 <= -0.70 * eff_entry_z):
                sdir = 1
            elif z1 >= eff_entry_z or (z2 >= eff_entry_z and z1 >= 0.70 * eff_entry_z):
                sdir = -1
            if sdir != 0:
                # Turn confirmation (NO falling knife!)
                turn_ok = (not self.cfg.require_turn) or (
                    (sdir == 1 and ind["c1"] >= ind["c2"]) or (sdir == -1 and ind["c1"] <= ind["c2"])
                )
                adx_ok = (eff_adx_max <= 0.0) or (ind["adx"] <= eff_adx_max)
                vol_ok = self.cfg.atr_min_ratio <= ind["atr_ratio"] <= self.cfg.atr_max_ratio
                sd_ok = ind["sd"] >= (eff_sd_mult * max(spread, 0.20))

                entry_px = ask if sdir == 1 else bid
                tp_dist = (ind["mean"] - entry_px) if sdir == 1 else (entry_px - ind["mean"])
                tp_ok = tp_dist >= (eff_tp_mult * max(spread, 0.20))

                if turn_ok and adx_ok and vol_ok and sd_ok and tp_ok:
                    sl_dist = min(self.cfg.max_sl_price, max(self.cfg.min_sl_price, self.cfg.sl_atr_mult * ind["atr"]))
                    lot = self.cfg.leg_lot
                    if self.cfg.variable_lot_by_z:
                        z_mult = _clip(abs(z1) / eff_entry_z, 1.0, 2.0)
                        lot = max(0.01, min(self.cfg.max_lot, round(self.cfg.leg_lot * z_mult, 2)))
                    stop = round(entry_px - sl_dist, 4) if sdir == 1 else round(entry_px + sl_dist, 4)
                    tp = round(ind["mean"], 4)
                    wid = self.next_wave_id
                    self.next_wave_id += 1
                    self.basket = {
                        "wave_id": wid,
                        "side": "buy" if sdir == 1 else "sell",
                        "opened_at": ts,
                        "bars_held": 0,
                        "legs": [{"leg": 1, "entry": entry_px, "vol": lot, "ts": ts}],
                        "total_volume": lot,
                        "avg_entry": entry_px,
                        "stop": stop,
                        "tp": tp,
                        "be_armed": False,
                        "stop_locked": False,
                    }
                    events.append({
                        "type": "OPEN_L1",
                        "wave_id": wid,
                        "side": "buy" if sdir == 1 else "sell",
                        "entry": entry_px,
                        "leg_lot": lot,
                        "total_volume": lot,
                        "stop": stop,
                        "tp": tp,
                    })

        self._update_dd(bid, ask)
        return events

    def seed_closed_bars(self, bars: list[dict[str, float]]) -> int:
        """Warm up M1 Z-Score / ADX / ATR history causally from closed historical M1 bars."""
        count = 0
        for b in bars:
            self._push_closed_bar(
                {
                    "bar_id": float(b.get("bar_id", count)),
                    "o": float(b["o"]),
                    "h": float(b["h"]),
                    "l": float(b["l"]),
                    "c": float(b["c"]),
                }
            )
            count += 1
        return count

    def live_status(self, ts: float | None = None) -> dict[str, Any]:
        """Human-readable 24h market radar status for ZSCORE_MR."""
        ind = self._indicators()
        bars_n = len(self._bars)
        need = max(self.cfg.mean_period, self.cfg.atr_period) + 2
        in_sess = self._in_session(ts) if ts is not None else True
        if ind is None:
            return {
                "in_session": in_sess,
                "bars_ready": bars_n,
                "atr": 0.0,
                "adx": 0.0,
                "z1": 0.0,
                "move_60s": 0.0,
                "htf_move": 0.0,
                "eff": 0.0,
                "ema_dir": "FLAT",
                "reason": f"WARMING UP 24H RADAR ({bars_n}/{need} closed M1 bars)",
            }
        z1 = round(ind["z1"], 2)
        adx = round(ind["adx"], 1)
        atr = round(ind["atr"], 2)
        is_night = self.cfg.night_adaptive and ts is not None and (
            datetime.fromtimestamp(ts, tz=timezone.utc).hour in self.cfg.night_hours
        )
        mode_tag = "NIGHT-24H" if is_night else "DAY-24H"
        eff_z = self.cfg.night_entry_z if is_night else self.cfg.entry_z
        eff_adx = self.cfg.night_adx_max if is_night else self.cfg.adx_max
        if self.cooldown_bars_left > 0:
            reason = f"{mode_tag} COOLDOWN ({self.cooldown_bars_left}m left | Z={z1:+.2f} ADX={adx:.0f})"
        elif adx > eff_adx:
            reason = f"{mode_tag} TRENDING (ADX={adx:.0f}>{eff_adx:.0f} — FastShock6/MacroWave hunting | Z={z1:+.2f})"
        elif abs(z1) >= eff_z * 0.85:
            reason = f"{mode_tag} STRETCHED (Z={z1:+.2f} near ±{eff_z:.2f}, ADX={adx:.0f} — waiting turn bar)"
        else:
            reason = f"{mode_tag} HUNTING 24H (Z={z1:+.2f}/±{eff_z:.2f}, ADX={adx:.0f}, ATR=${atr:.2f})"
        htf = round(self._bars[-1]["c"] - self._bars[-15]["c"], 2) if len(self._bars) >= 15 else 0.0
        m60 = round(self._bars[-1]["c"] - self._bars[-2]["c"], 2) if len(self._bars) >= 2 else 0.0
        return {
            "in_session": True,
            "bars_ready": bars_n,
            "atr": atr,
            "adx": adx,
            "z1": z1,
            "move_60s": m60,
            "htf_move": htf,
            "eff": 0.0,
            "ema_dir": "UP" if htf > 0 else ("DOWN" if htf < 0 else "FLAT"),
            "reason": reason,
        }

    def force_close_open(self, *, ts: float, bid: float, ask: float, reason: str = "eof_replay") -> dict[str, Any] | None:
        if self.basket is None:
            return None
        ex_px = bid if self.basket["side"] == "buy" else ask
        closed = self._close_pos(ex_px, ts, reason)
        self._update_dd(bid, ask)
        return closed


# =====================================================================
# 4) QUIET-MARKET / NIGHT 30-MIN BOX REVERSION ENGINE (NO TREND FIGHTING!)
# =====================================================================
@dataclass
class QuietRangeBounceConfig:
    """Strictly causal 30-min calm-box edge mean reversion for night & quiet range hours."""

    name: str = "QR_BOX30_NIGHT"
    box_bars: int = 30  # 30 closed M1 bars = 30-minute box
    adx_period: int = 14
    adx_max: float = 20.0  # strict sideways-only regime gate!
    min_box_range: float = 3.60  # must exceed spread enough to pay Limit TP cleanly
    max_box_range: float = 14.00  # above $14 = violent trending session, stand aside
    edge_frac: float = 0.12  # enter only near bottom 12% or top 12% of 30m box
    min_tp_cost_mult: float = 3.0  # TP distance to box mid >= 3.0x spread
    sl_price: float = 2.60
    be_trigger_price: float = 1.20
    max_hold_sec: float = 1200.0
    leg_lot: float = 0.02
    max_spread_price: float = 0.55
    active_server_hours: tuple[int, ...] | None = (21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8)
    win_cooldown_sec: float = 120.0
    loss_cooldown_sec: float = 600.0
    multiplier: float = 100.0


@dataclass
class QuietRangeBounceEngine:
    """Causal calm-range box edge bounce with closed-M1 ADX gate & 5s tick-turn confirmation (ORDERS=0)."""

    cfg: QuietRangeBounceConfig = field(default_factory=QuietRangeBounceConfig)
    initial_capital_usd: float = 1000.0

    _phys: IncrementalTickPhysics = field(default_factory=lambda: IncrementalTickPhysics(vel_win=5.0))
    _cur_bar: dict[str, float] | None = None
    _bars: deque[dict[str, float]] = field(default_factory=lambda: deque(maxlen=80))
    _tr_hist: deque[float] = field(default_factory=lambda: deque(maxlen=60))
    _plus_dm: deque[float] = field(default_factory=lambda: deque(maxlen=60))
    _minus_dm: deque[float] = field(default_factory=lambda: deque(maxlen=60))
    _dx_hist: deque[float] = field(default_factory=lambda: deque(maxlen=60))
    _prev_ts: float | None = None

    basket: dict[str, Any] | None = None
    cooldown_until: float = 0.0
    next_wave_id: int = 1
    realized_pnl_usd: float = 0.0
    peak_equity_usd: float = 1000.0
    max_dd_usd: float = 0.0
    waves_closed: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.peak_equity_usd = float(self.initial_capital_usd)

    def _in_session(self, ts: float) -> bool:
        if not self.cfg.active_server_hours:
            return True
        return datetime.fromtimestamp(ts, tz=timezone.utc).hour in self.cfg.active_server_hours

    def _push_bar(self, bar: dict[str, float]) -> None:
        if self._bars:
            prev = self._bars[-1]
            tr = max(bar["h"] - bar["l"], abs(bar["h"] - prev["c"]), abs(bar["l"] - prev["c"]))
            up_m = bar["h"] - prev["h"]
            dn_m = prev["l"] - bar["l"]
            pdm = up_m if (up_m > dn_m and up_m > 0.0) else 0.0
            mdm = dn_m if (dn_m > up_m and dn_m > 0.0) else 0.0
        else:
            tr = bar["h"] - bar["l"]
            pdm = mdm = 0.0
        self._bars.append(bar)
        self._tr_hist.append(tr)
        self._plus_dm.append(pdm)
        self._minus_dm.append(mdm)
        ap = self.cfg.adx_period
        if len(self._tr_hist) >= ap:
            tr_sum = sum(list(self._tr_hist)[-ap:])
            if tr_sum > 1e-9:
                pdi = 100.0 * sum(list(self._plus_dm)[-ap:]) / tr_sum
                mdi = 100.0 * sum(list(self._minus_dm)[-ap:]) / tr_sum
                dx = 100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 1e-9 else 0.0
                self._dx_hist.append(dx)

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
        self.max_dd_usd = round(max(self.max_dd_usd, self.peak_equity_usd - eq), 4)

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
        events: list[dict[str, Any]] = []
        if bid <= 0 or ask <= 0 or ask < bid:
            return events
        if self._prev_ts is not None and (ts - self._prev_ts) > 300.0:
            self._cur_bar = None
            self._bars.clear()
            self._tr_hist.clear()
            self._plus_dm.clear()
            self._minus_dm.clear()
            self._dx_hist.clear()
        self._prev_ts = ts

        spread = ask - bid
        mid = 0.5 * (bid + ask)
        ph = self._phys.push(ts, bid, ask)

        # Update causal closed M1 bars
        bar_id = int(ts // 60)
        if self._cur_bar is None:
            self._cur_bar = {"bar_id": float(bar_id), "o": mid, "h": mid, "l": mid, "c": mid}
        elif bar_id > int(self._cur_bar["bar_id"]):
            self._push_bar(self._cur_bar)
            self._cur_bar = {"bar_id": float(bar_id), "o": mid, "h": mid, "l": mid, "c": mid}
        else:
            self._cur_bar["h"] = max(self._cur_bar["h"], mid)
            self._cur_bar["l"] = min(self._cur_bar["l"], mid)
            self._cur_bar["c"] = mid

        # Manage open position
        if self.basket is not None:
            b = self.basket
            side = b["side"]
            if side == "buy":
                if not b["be_armed"] and (bid - b["avg_entry"]) >= self.cfg.be_trigger_price:
                    b["stop"] = max(b["stop"], round(b["avg_entry"] + spread + 0.05, 4))
                    b["be_armed"] = True
                if bid <= b["stop"]:
                    closed = self._close_pos(bid, ts, "be_stop_hit" if b["be_armed"] else "box_sl")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events
                if bid >= b["tp"]:
                    closed = self._close_pos(b["tp"], ts, "box_mid_tp")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events
            else:
                if not b["be_armed"] and (b["avg_entry"] - ask) >= self.cfg.be_trigger_price:
                    b["stop"] = min(b["stop"], round(b["avg_entry"] - spread - 0.05, 4))
                    b["be_armed"] = True
                if ask >= b["stop"]:
                    closed = self._close_pos(ask, ts, "be_stop_hit" if b["be_armed"] else "box_sl")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events
                if ask <= b["tp"]:
                    closed = self._close_pos(b["tp"], ts, "box_mid_tp")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events
            if (ts - b["opened_at"]) >= self.cfg.max_hold_sec:
                ex_px = bid if side == "buy" else ask
                closed = self._close_pos(ex_px, ts, "box_max_hold")
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events
            self._update_dd(bid, ask)
            return events

        # Flat: check strict sideways 30m box + 5s tick-turn bounce
        if (
            ts >= self.cooldown_until
            and spread <= self.cfg.max_spread_price
            and self._in_session(ts)
            and ph["ready"] > 0.5
            and len(self._bars) >= self.cfg.box_bars
            and len(self._dx_hist) >= self.cfg.adx_period
        ):
            ap = self.cfg.adx_period
            adx = sum(list(self._dx_hist)[-ap:]) / ap
            if adx <= self.cfg.adx_max:
                recent = list(self._bars)[-self.cfg.box_bars :]
                box_hi = max(b["h"] for b in recent)
                box_lo = min(b["l"] for b in recent)
                box_rng = box_hi - box_lo
                if self.cfg.min_box_range <= box_rng <= self.cfg.max_box_range:
                    box_mid = 0.5 * (box_hi + box_lo)
                    pos = (mid - box_lo) / box_rng
                    # Wait for inward 15-second turn + inward flow (NEVER catch a falling knife!)
                    sdir = 0
                    if pos <= self.cfg.edge_frac and ph["m15"] >= 0.12 and ph["flow"] >= 0.15:
                        sdir = 1
                    elif pos >= (1.0 - self.cfg.edge_frac) and ph["m15"] <= -0.12 and ph["flow"] <= -0.15:
                        sdir = -1
                    if sdir != 0:
                        entry_px = ask if sdir == 1 else bid
                        tp_dist = (box_mid - entry_px) if sdir == 1 else (entry_px - box_mid)
                        if tp_dist >= self.cfg.min_tp_cost_mult * max(spread, 0.20):
                            stop = round(entry_px - self.cfg.sl_price, 4) if sdir == 1 else round(entry_px + self.cfg.sl_price, 4)
                            wid = self.next_wave_id
                            self.next_wave_id += 1
                            self.basket = {
                                "wave_id": wid,
                                "side": "buy" if sdir == 1 else "sell",
                                "opened_at": ts,
                                "total_volume": self.cfg.leg_lot,
                                "avg_entry": entry_px,
                                "stop": stop,
                                "tp": round(box_mid, 4),
                                "be_armed": False,
                            }
                            events.append({
                                "type": "OPEN_L1",
                                "wave_id": wid,
                                "side": "buy" if sdir == 1 else "sell",
                                "entry": entry_px,
                                "leg_lot": self.cfg.leg_lot,
                                "total_volume": self.cfg.leg_lot,
                                "stop": stop,
                                "tp": round(box_mid, 4),
                            })

        self._update_dd(bid, ask)
        return events

    def force_close_open(self, *, ts: float, bid: float, ask: float, reason: str = "eof_replay") -> dict[str, Any] | None:
        if self.basket is None:
            return None
        ex_px = bid if self.basket["side"] == "buy" else ask
        closed = self._close_pos(ex_px, ts, reason)
        self._update_dd(bid, ask)
        return closed


def build_quant_sweep_candidates() -> tuple[
    list[RapidBurstAccelConfig],
    list[TickRobustZScoreConfig],
    list[QuietRangeBounceConfig],
]:
    """V3 systematic parameter & constraint sweep: Risk-Free Slope Stacking + TickRobust Z + Night Box."""
    day_hrs = (9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20)
    night_hrs = (21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8)

    accel_cfgs = [
        # --- Proven V2 baseline winners for exact A/B comparison ---
        RapidBurstAccelConfig(
            name="RB_03_ACCEL_VARLOT",
            vel_win_sec=10.0,
            min_vel_per_sec=0.18,
            require_accel=True,
            min_accel_per_sec=0.06,
            min_60s_move=5.00,
            variable_l1_lot=True,
            max_l1_lot=0.06,
            enable_slope_stacking=False,
            decel_exit_enabled=True,
            active_server_hours=day_hrs,
        ),
        # --- V3 Upgrade 1: Risk-Capped Variable L1 (caps 1 fakeout <= $9.50 -> cuts DD in half!) ---
        RapidBurstAccelConfig(
            name="RB_V3_RISKCAP_1L",
            vel_win_sec=10.0,
            min_vel_per_sec=0.18,
            require_accel=True,
            min_accel_per_sec=0.06,
            min_60s_move=5.00,
            variable_l1_lot=True,
            max_l1_lot=0.06,
            max_l1_risk_usd=9.50,
            enable_slope_stacking=False,
            decel_exit_enabled=True,
            active_server_hours=day_hrs,
        ),
        # --- V3 Upgrade 2: RISK-FREE ALGEBRAIC SLOPE STACKING (1..6 legs, NEVER adds DD!), Session ---
        RapidBurstAccelConfig(
            name="RB_V3_RFSTACK_SESS",
            vel_win_sec=10.0,
            min_vel_per_sec=0.18,
            require_accel=True,
            min_accel_per_sec=0.06,
            min_60s_move=5.00,
            variable_l1_lot=True,
            max_l1_lot=0.06,
            max_l1_risk_usd=10.00,
            enable_slope_stacking=True,
            risk_free_stack_sizing=True,
            max_legs=6,
            add_step_price=1.40,
            add_interval_sec=4.0,
            sl_min_breathing_room=0.95,
            dyn_min_lock_profit=0.15,
            decel_exit_enabled=True,
            active_server_hours=day_hrs,
        ),
        # --- V3 Upgrade 3: RISK-FREE SLOPE STACKING + LET RUNNERS RUN (No early decel cut!) ---
        RapidBurstAccelConfig(
            name="RB_V3_RFSTACK_RUN",
            vel_win_sec=10.0,
            min_vel_per_sec=0.18,
            require_accel=True,
            min_accel_per_sec=0.06,
            min_60s_move=5.00,
            variable_l1_lot=True,
            max_l1_lot=0.06,
            max_l1_risk_usd=10.00,
            enable_slope_stacking=True,
            risk_free_stack_sizing=True,
            max_legs=6,
            add_step_price=1.40,
            add_interval_sec=4.0,
            sl_min_breathing_room=0.95,
            dyn_min_lock_profit=0.15,
            decel_exit_enabled=False,
            active_server_hours=day_hrs,
        ),
        # --- V3 Upgrade 4: 24-HOUR RISK-FREE SLOPE STACKING (Day + Night bursts!) ---
        RapidBurstAccelConfig(
            name="RB_V3_RFSTACK_24H",
            vel_win_sec=10.0,
            min_vel_per_sec=0.18,
            require_accel=True,
            min_accel_per_sec=0.06,
            min_60s_move=5.00,
            variable_l1_lot=True,
            max_l1_lot=0.06,
            max_l1_risk_usd=10.00,
            enable_slope_stacking=True,
            risk_free_stack_sizing=True,
            max_legs=6,
            add_step_price=1.40,
            add_interval_sec=4.0,
            sl_min_breathing_room=0.95,
            dyn_min_lock_profit=0.15,
            decel_exit_enabled=True,
            active_server_hours=None,
        ),
        # --- V3 Upgrade 5: NIGHT-ONLY STEEP BURST STACKER (built from RB_06's +$17.84 night win!) ---
        RapidBurstAccelConfig(
            name="RB_V3_NIGHT_BURST",
            vel_win_sec=10.0,
            min_vel_per_sec=0.20,
            require_accel=True,
            min_accel_per_sec=0.07,
            min_60s_move=5.50,
            min_eff_v1=0.48,
            min_eff_60=0.38,
            variable_l1_lot=True,
            max_l1_lot=0.06,
            max_l1_risk_usd=10.00,
            enable_slope_stacking=True,
            risk_free_stack_sizing=True,
            max_legs=6,
            add_step_price=1.35,
            add_interval_sec=4.0,
            sl_min_breathing_room=0.95,
            dyn_min_lock_profit=0.15,
            decel_exit_enabled=False,
            active_server_hours=night_hrs,
        ),
        # --- V3 Upgrade 6: FULL UNCAPPED SLOPE LOT + RISK-FREE STACKING (higher upside test) ---
        RapidBurstAccelConfig(
            name="RB_V3_RFSTACK_BIGLOT",
            vel_win_sec=10.0,
            min_vel_per_sec=0.18,
            require_accel=True,
            min_accel_per_sec=0.06,
            min_60s_move=5.00,
            variable_l1_lot=True,
            max_l1_lot=0.06,
            max_l1_risk_usd=0.0,
            enable_slope_stacking=True,
            risk_free_stack_sizing=True,
            max_legs=6,
            add_step_price=1.40,
            add_interval_sec=4.0,
            sl_min_breathing_room=0.95,
            dyn_min_lock_profit=0.15,
            decel_exit_enabled=True,
            active_server_hours=day_hrs,
        ),
    ]

    zscore_cfgs = [
        # --- Proven 100%-WR Z=2.2 baseline + Variable Lot & Tight-SL sensitivity grid ---
        TickRobustZScoreConfig(
            name="GH_Z_M1_Z22_24H",
            bar_sec=60,
            entry_z=2.2,
            adx_max=22.0,
            min_sd_cost_mult=2.5,
            min_tp_cost_mult=3.5,
            min_sl_price=4.50,
            active_server_hours=None,
        ),
        TickRobustZScoreConfig(
            name="GH_Z_M1_Z22_VARLOT",
            bar_sec=60,
            entry_z=2.2,
            adx_max=22.0,
            min_sd_cost_mult=2.5,
            min_tp_cost_mult=3.5,
            min_sl_price=3.60,
            max_sl_price=4.50,
            leg_lot=0.03,
            variable_lot_by_z=True,
            max_lot=0.06,
            active_server_hours=None,
        ),
        TickRobustZScoreConfig(
            name="GH_Z_M1_Z21_TIGHT_24H",
            bar_sec=60,
            entry_z=2.10,
            adx_max=22.0,
            min_sd_cost_mult=2.0,
            min_tp_cost_mult=3.0,
            min_sl_price=3.20,
            max_sl_price=4.00,
            leg_lot=0.03,
            variable_lot_by_z=True,
            max_lot=0.05,
            active_server_hours=None,
        ),
        TickRobustZScoreConfig(
            name="GH_Z_M1_Z21_NIGHT",
            bar_sec=60,
            entry_z=2.10,
            adx_max=24.0,
            min_sd_cost_mult=1.9,
            min_tp_cost_mult=2.8,
            min_sl_price=3.20,
            max_sl_price=4.00,
            leg_lot=0.03,
            variable_lot_by_z=True,
            max_lot=0.05,
            active_server_hours=night_hrs,
        ),
        TickRobustZScoreConfig(
            name="GH_Z_M1_Z195_FLAT_24H",
            bar_sec=60,
            entry_z=1.95,
            adx_max=18.0,
            min_sd_cost_mult=1.8,
            min_tp_cost_mult=2.8,
            min_sl_price=2.80,
            max_sl_price=3.60,
            leg_lot=0.03,
            variable_lot_by_z=True,
            max_lot=0.05,
            active_server_hours=None,
        ),
        TickRobustZScoreConfig(
            name="GH_Z_M2_Z21_24H",
            bar_sec=120,
            entry_z=2.10,
            adx_max=22.0,
            min_sd_cost_mult=2.2,
            min_tp_cost_mult=3.2,
            min_sl_price=3.60,
            max_sl_price=4.80,
            leg_lot=0.03,
            variable_lot_by_z=True,
            max_lot=0.05,
            active_server_hours=None,
        ),
    ]

    box_cfgs = [
        QuietRangeBounceConfig(
            name="QR_BOX30_NIGHT",
            adx_max=20.0,
            min_box_range=3.60,
            max_box_range=14.00,
            sl_price=2.60,
            leg_lot=0.03,
            active_server_hours=night_hrs,
        ),
        QuietRangeBounceConfig(
            name="QR_BOX30_24H",
            adx_max=18.0,
            min_box_range=4.00,
            max_box_range=14.00,
            sl_price=2.60,
            leg_lot=0.03,
            active_server_hours=None,
        ),
    ]

    return accel_cfgs, zscore_cfgs, box_cfgs
