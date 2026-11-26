"""Strictly causal Fast Sharp-Impulse, V-Snap, Sweep-Reversal & Acceleration engines (ORDERS=0).

Designed to close the exact structural blind spots observed on real Alpari XAUUSD_i tick streams:
1. VSNAP_30S_SESS / VSNAP_30S_24H: Sharp 25s/60s directional bursts (NO 15m-breakout or lagging M1-EMA lock).
2. VSNAP_SHOCK_6: Mid-tier acceleration runner closing the $6-$10/60s gap below SHOCK10_DYN_RUN.
3. VTOP_SWEEP_SNAP: 40m liquidity-sweep + fast V-reversal hunter (catches box-edge fakeouts & V-tops/bottoms).
4. MICRO_PB_7: Fast 4m impulse ($7+) -> pullback -> causal resumption wave with dynamic lot scaling.
5. ACCEL_SCALP_15S: Active London/NY 15s-acceleration surge scalper with tight SL and early BE/ratchet.

All fills use real Bid/Ask quotes (BUY @ Ask, SELL @ Bid), realistic execution latency, gap-aware stops,
and ZERO lookahead.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from trade_mcp.s1_velocity_surfer import compute_dynamic_add_lot, compute_dynamic_l1_lot


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class FastSnapConfig:
    """Configuration for a strictly causal Fast-Snap / V-Reversal / Acceleration tick engine."""

    name: str = "VSNAP_30S_SESS"
    mode: str = "fast_impulse"  # "fast_impulse" | "sweep_reversal" | "micro_pullback"

    # Sizing & execution realism
    leg_lot: float = 0.02
    max_legs: int = 8
    multiplier: float = 100.0
    latency_sec: float = 0.25
    max_spread_price: float = 0.65
    dynamic_sizing: bool = True
    dyn_max_l1_lot: float = 0.04
    dyn_max_leg_lot: float = 0.08
    dyn_max_total_lot: float = 0.30
    dyn_min_lock_profit: float = 0.45

    # Session filter (broker server hours; None = 24h)
    active_server_hours: tuple[int, ...] | None = (9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20)

    # Fast-impulse triggers (mode == "fast_impulse")
    fast_win_sec: int = 25
    min_fast_move: float = 3.60
    min_fast_eff: float = 0.58
    confirm_win_sec: int = 5
    min_confirm_move: float = 0.65
    alt_60s_move: float = 5.50
    alt_60s_eff: float = 0.44
    alt_10s_move: float = 1.10
    min_60s_align_move: float = 0.0  # optional 60s alignment floor (e.g. ACCEL_SCALP_15S)
    min_60s_align_eff: float = 0.0
    max_pullback_from_30s_ext: float = 0.70

    # Sweep-reversal triggers (mode == "sweep_reversal")
    sweep_lookback_sec: int = 2400  # 40 minutes rolling high/low
    sweep_min_history_sec: int = 600  # require >=10m history before sweeping
    sweep_min_range: float = 6.00  # require real >=$6 box/swing
    sweep_touch_pad: float = 0.60  # touch within $0.60 of (or break beyond) prior 40m high/low
    sweep_recent_sec: int = 75  # peak/trough printed within last 75s
    sweep_min_snap: float = 2.30  # snapped back >=$2.30 from extreme
    sweep_max_snap: float = 5.80  # don't chase if already snapped >$5.80
    sweep_15s_move: float = 1.20  # actively snapping in last 15s
    sweep_20s_eff: float = 0.48

    # Micro-pullback triggers (mode == "micro_pullback")
    pb_window_sec: int = 240  # 4-minute impulse lookback
    pb_min_impulse: float = 7.00
    pb_min_retrace_frac: float = 0.25
    pb_max_retrace_frac: float = 0.62
    pb_min_dip_price: float = 1.80
    pb_resume_15s_move: float = 1.10
    pb_resume_15s_eff: float = 0.50

    # Risk & profit management
    hard_stop_price: float = 2.50
    max_stop_price: float = 4.20
    sl_min_breathing_room: float = 1.15
    be_arm_price: float = 1.25
    be_lock_profit: float = 0.15
    ratchet_arm_price: float = 2.20
    ratchet_frac: float = 0.55
    ratchet_breathing_room: float = 1.10
    trail_arm_price: float = 2.80
    trail_pullback_price: float = 1.85
    max_hold_sec: float = 240.0
    runner_max_hold_sec: float = 900.0

    # House-money pyramiding adds
    enable_adds: bool = True
    add_step_price: float = 2.40
    add_interval_sec: float = 15.0
    add_min_60s_move: float = 5.50

    # Cooldowns
    win_cooldown_sec: float = 35.0
    loss_cooldown_sec: float = 90.0
    consec_loss_cooldown_sec: float = 240.0


@dataclass
class FastSnapEngine:
    """Strictly causal tick-speed engine for sharp spikes, V-reversals, and micro-pullbacks (ORDERS=0)."""

    cfg: FastSnapConfig = field(default_factory=FastSnapConfig)
    initial_capital_usd: float = 1000.0

    # 1-second causal history (up to 45 minutes = 2700s)
    _sec_hist: deque[tuple[int, float, float, float]] = field(default_factory=lambda: deque(maxlen=2800))
    _cur_sec: list[Any] | None = None  # [sec, mid, high_mid, low_mid]
    _prev_ts: float | None = None

    # Micro-pullback state machine
    _pb_arm: dict[str, Any] | None = None

    pending: dict[str, Any] | None = None
    basket: dict[str, Any] | None = None
    cooldown_until: float = 0.0
    consecutive_losses: int = 0
    next_wave_id: int = 1
    realized_pnl_usd: float = 0.0
    peak_equity_usd: float = 1000.0
    max_dd_usd: float = 0.0
    waves_closed: list[dict[str, Any]] = field(default_factory=list)

    def reset(self) -> None:
        self._sec_hist.clear()
        self._cur_sec = None
        self._prev_ts = None
        self._pb_arm = None
        self.pending = None
        self.basket = None
        self.cooldown_until = 0.0
        self.consecutive_losses = 0
        self.next_wave_id = 1
        self.realized_pnl_usd = 0.0
        self.peak_equity_usd = self.initial_capital_usd
        self.max_dd_usd = 0.0
        self.waves_closed.clear()

    def _in_session(self, ts: float) -> bool:
        if not self.cfg.active_server_hours:
            return True
        hr = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        return hr in self.cfg.active_server_hours

    def _update_sec_hist(self, ts: float, mid: float) -> None:
        sec = int(ts)
        if self._prev_ts is not None and (ts - self._prev_ts) > 120.0:
            # Discontinuous file gap: reset rolling buffers so cross-gap ghosts cannot fire
            self._sec_hist.clear()
            self._cur_sec = [sec, mid, mid, mid]
            self._pb_arm = None
            self._prev_ts = ts
            return
        self._prev_ts = ts
        if self._cur_sec is None:
            self._cur_sec = [sec, mid, mid, mid]
        elif sec > self._cur_sec[0]:
            self._sec_hist.append((int(self._cur_sec[0]), float(self._cur_sec[1]), float(self._cur_sec[2]), float(self._cur_sec[3])))
            self._cur_sec = [sec, mid, mid, mid]
        else:
            self._cur_sec[1] = mid
            if mid > self._cur_sec[2]:
                self._cur_sec[2] = mid
            if mid < self._cur_sec[3]:
                self._cur_sec[3] = mid

    def _window_stats(self, sec: int, mid: float, win_sec: int) -> tuple[bool, float, float, float, float]:
        """Causal (has_win, net_move, efficiency, hi, lo) over [sec-win_sec, sec]."""
        if not self._sec_hist:
            return False, 0.0, 0.0, mid, mid
        target = sec - win_sec
        pts: list[tuple[int, float, float, float]] = []
        for row in reversed(self._sec_hist):
            pts.append(row)
            if row[0] <= target:
                break
        if not pts:
            return False, 0.0, 0.0, mid, mid
        pts.reverse()
        span = sec - pts[0][0]
        if span < max(3, int(win_sec * 0.65)):
            return False, 0.0, 0.0, mid, mid
        start_px = pts[0][1]
        hi = mid
        lo = mid
        path = 0.0
        prev_px = start_px
        for _, m, h, l_px in pts:
            if h > hi:
                hi = h
            if l_px < lo:
                lo = l_px
            path += abs(m - prev_px)
            prev_px = m
        path += abs(mid - prev_px)
        net = mid - start_px
        eff = (abs(net) / path) if path > 1e-9 else 0.0
        return True, net, eff, hi, lo

    def _rolling_extremes(
        self, sec: int, mid: float, lookback_sec: int, exclude_recent_sec: int
    ) -> tuple[bool, float, float, float, float, float, int, int]:
        """Causal prior [sec-lookback, sec-exclude] hi/lo AND recent [sec-exclude, sec] hi/lo + timestamps."""
        if not self._sec_hist:
            return False, mid, mid, mid, mid, 0.0, sec, sec
        oldest_sec = self._sec_hist[0][0]
        if (sec - oldest_sec) < self.cfg.sweep_min_history_sec:
            return False, mid, mid, mid, mid, 0.0, sec, sec

        cutoff_old = sec - lookback_sec
        cutoff_rec = sec - exclude_recent_sec

        prior_hi = -1e18
        prior_lo = 1e18
        has_prior = False
        rec_hi = mid
        rec_lo = mid
        rec_hi_ts = sec
        rec_lo_ts = sec

        for s_ts, _, h, l_px in reversed(self._sec_hist):
            if s_ts < cutoff_old:
                break
            if s_ts >= cutoff_rec:
                if h > rec_hi:
                    rec_hi = h
                    rec_hi_ts = s_ts
                if l_px < rec_lo:
                    rec_lo = l_px
                    rec_lo_ts = s_ts
            else:
                has_prior = True
                if h > prior_hi:
                    prior_hi = h
                if l_px < prior_lo:
                    prior_lo = l_px

        if not has_prior:
            return False, mid, mid, rec_hi, rec_lo, 0.0, rec_hi_ts, rec_lo_ts
        rng = max(prior_hi, rec_hi) - min(prior_lo, rec_lo)
        return True, prior_hi, prior_lo, rec_hi, rec_lo, rng, rec_hi_ts, rec_lo_ts

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

    def _detect_signal(self, ts: float, mid: float, spread: float) -> tuple[str | None, float, float]:
        """Return (side, custom_stop_dist, dir_60s_speed) if a strictly causal entry triggers."""
        if spread > self.cfg.max_spread_price or not self._in_session(ts):
            return None, 0.0, 0.0

        sec = int(ts)
        has_60, m60, eff60, _, _ = self._window_stats(sec, mid, 60)

        if self.cfg.mode == "fast_impulse":
            has_f, mf, eff_f, hi_f, lo_f = self._window_stats(sec, mid, self.cfg.fast_win_sec)
            has_c, mc, _, _, _ = self._window_stats(sec, mid, self.cfg.confirm_win_sec)
            has_10, m10, _, _, _ = self._window_stats(sec, mid, 10)
            _, _, _, hi_30, lo_30 = self._window_stats(sec, mid, 30)

            sdir = 0
            # Trigger A: Fast sharp 15s-30s burst
            if (
                has_f
                and abs(mf) >= self.cfg.min_fast_move
                and eff_f >= self.cfg.min_fast_eff
                and has_c
            ):
                cand = 1 if mf > 0 else -1
                if (mc * cand) >= self.cfg.min_confirm_move:
                    align_ok = True
                    if self.cfg.min_60s_align_move > 0.0:
                        align_ok = (
                            has_60
                            and (m60 * cand) >= self.cfg.min_60s_align_move
                            and eff60 >= self.cfg.min_60s_align_eff
                        )
                    if align_ok:
                        sdir = cand

            # Trigger B: Strong 60s sharp surge (e.g. -$6..-$9.45 in 60s!)
            if (
                sdir == 0
                and self.cfg.alt_60s_move > 0.0
                and has_60
                and abs(m60) >= self.cfg.alt_60s_move
                and eff60 >= self.cfg.alt_60s_eff
                and has_10
            ):
                cand = 1 if m60 > 0 else -1
                if (m10 * cand) >= self.cfg.alt_10s_move:
                    sdir = cand

            if sdir == 0:
                return None, 0.0, 0.0

            # Anti-exhaustion: don't enter after already snapping back sharply off the 30s extreme
            pb_ext = (hi_30 - mid) if sdir == 1 else (mid - lo_30)
            if pb_ext > self.cfg.max_pullback_from_30s_ext:
                return None, 0.0, 0.0

            speed = abs(m60) if has_60 else abs(mf) * 1.5
            return ("buy" if sdir == 1 else "sell"), self.cfg.hard_stop_price, speed

        if self.cfg.mode == "sweep_reversal":
            ok, prior_hi, prior_lo, rec_hi, rec_lo, rng, _, _ = self._rolling_extremes(
                sec, mid, self.cfg.sweep_lookback_sec, self.cfg.sweep_recent_sec
            )
            if not ok or rng < self.cfg.sweep_min_range:
                return None, 0.0, 0.0
            has_15, m15, _, _, _ = self._window_stats(sec, mid, 15)
            has_20, _, eff20, _, _ = self._window_stats(sec, mid, 20)
            if not (has_15 and has_20 and eff20 >= self.cfg.sweep_20s_eff):
                return None, 0.0, 0.0

            # Bearish V-top sweep: swept/touched 40m high within last 75s, now snapping down sharply
            if rec_hi >= (prior_hi - self.cfg.sweep_touch_pad):
                snap_down = rec_hi - mid
                if (
                    self.cfg.sweep_min_snap <= snap_down <= self.cfg.sweep_max_snap
                    and m15 <= -self.cfg.sweep_15s_move
                ):
                    sl_dist = min(self.cfg.max_stop_price, max(self.cfg.hard_stop_price, snap_down + 0.65))
                    return "sell", sl_dist, max(abs(m15) * 2.5, abs(m60) if has_60 else 0.0)

            # Bullish V-bottom sweep: swept/touched 40m low within last 75s, now snapping up sharply
            if rec_lo <= (prior_lo + self.cfg.sweep_touch_pad):
                snap_up = mid - rec_lo
                if (
                    self.cfg.sweep_min_snap <= snap_up <= self.cfg.sweep_max_snap
                    and m15 >= self.cfg.sweep_15s_move
                ):
                    sl_dist = min(self.cfg.max_stop_price, max(self.cfg.hard_stop_price, snap_up + 0.65))
                    return "buy", sl_dist, max(abs(m15) * 2.5, abs(m60) if has_60 else 0.0)
            return None, 0.0, 0.0

        if self.cfg.mode == "micro_pullback":
            has_pb, _, _, hi_w, lo_w = self._window_stats(sec, mid, self.cfg.pb_window_sec)
            if not has_pb:
                return None, 0.0, 0.0
            # Locate timestamps of hi_w / lo_w causally in pb_window_sec
            cutoff = sec - self.cfg.pb_window_sec
            hi_val, hi_ts = mid, sec
            lo_val, lo_ts = mid, sec
            for s_ts, _, h, l_px in reversed(self._sec_hist):
                if s_ts < cutoff:
                    break
                if h >= hi_val:
                    hi_val, hi_ts = h, s_ts
                if l_px <= lo_val:
                    lo_val, lo_ts = l_px, s_ts

            imp_rng = hi_val - lo_val
            if imp_rng >= self.cfg.pb_min_impulse and abs(hi_ts - lo_ts) >= 15:
                sdir = 1 if hi_ts > lo_ts else -1
                if sdir == 1:
                    retrace = hi_val - mid
                else:
                    retrace = mid - lo_val
                frac = retrace / imp_rng if imp_rng > 1e-9 else 0.0
                if (
                    self.cfg.pb_min_retrace_frac <= frac <= self.cfg.pb_max_retrace_frac
                    and retrace >= self.cfg.pb_min_dip_price
                ):
                    if self._pb_arm is None or self._pb_arm["dir"] != sdir or (ts - self._pb_arm["ts"]) > 180.0:
                        self._pb_arm = {"dir": sdir, "ts": ts, "imp": imp_rng, "best_dip": mid}
                    else:
                        self._pb_arm["ts"] = ts
                        if sdir == 1:
                            self._pb_arm["best_dip"] = min(self._pb_arm["best_dip"], mid)
                        else:
                            self._pb_arm["best_dip"] = max(self._pb_arm["best_dip"], mid)

            if self._pb_arm is not None:
                if (ts - self._pb_arm["ts"]) > 120.0:
                    self._pb_arm = None
                    return None, 0.0, 0.0
                has_15, m15, eff15, _, _ = self._window_stats(sec, mid, 15)
                sdir = int(self._pb_arm["dir"])
                if has_15 and (m15 * sdir) >= self.cfg.pb_resume_15s_move and eff15 >= self.cfg.pb_resume_15s_eff:
                    imp_speed = max(float(self._pb_arm["imp"]) * 0.65, abs(m60) if has_60 else 0.0)
                    self._pb_arm = None
                    return ("buy" if sdir == 1 else "sell"), self.cfg.hard_stop_price, imp_speed
            return None, 0.0, 0.0

        return None, 0.0, 0.0

    def on_tick(self, *, ts: float, bid: float, ask: float) -> list[dict[str, Any]]:
        """Process one live/replayed Bid/Ask tick strictly causally (ORDERS=0)."""
        events: list[dict[str, Any]] = []
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return events

        mid = 0.5 * (bid + ask)
        spread = ask - bid
        self._update_sec_hist(ts, mid)
        sec = int(ts)
        has_60, m60, _, _, _ = self._window_stats(sec, mid, 60)

        # 1) Execute pending fill once latency elapsed
        if self.pending is not None and ts >= self.pending["fill_ts"]:
            pend = self.pending
            self.pending = None
            side = pend["side"]
            fill_px = ask if side == "buy" else bid

            if pend["kind"] == "entry" and self.basket is None:
                sl_dist = float(pend["sl_dist"])
                lot = (
                    compute_dynamic_l1_lot(
                        base_lot=self.cfg.leg_lot,
                        max_l1_lot=self.cfg.dyn_max_l1_lot,
                        max_total_lot=self.cfg.dyn_max_total_lot,
                        abs_move_60s=float(pend["speed"]),
                    )
                    if self.cfg.dynamic_sizing
                    else self.cfg.leg_lot
                )
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
                hold_sec = max(0.0, ts - float(b["opened_at"]))
                dir_m60 = (m60 if side == "buy" else -m60) if has_60 else 0.0
                if self.cfg.dynamic_sizing:
                    add_lot = compute_dynamic_add_lot(
                        side=side,
                        bid=bid,
                        ask=ask,
                        fill_px=fill_px,
                        old_vol=cur_vol,
                        old_avg=cur_avg,
                        breathing_room=self.cfg.sl_min_breathing_room,
                        base_lot=self.cfg.leg_lot,
                        max_leg_lot=self.cfg.dyn_max_leg_lot,
                        max_total_lot=self.cfg.dyn_max_total_lot,
                        dir_move_60s=dir_m60,
                        hold_sec=hold_sec,
                        min_lock_profit=self.cfg.dyn_min_lock_profit,
                    )
                else:
                    add_lot = self.cfg.leg_lot if (cur_vol + self.cfg.leg_lot) <= self.cfg.dyn_max_total_lot else 0.0
                if add_lot >= 0.01:
                    new_vol = round(cur_vol + add_lot, 2)
                    new_avg = round((cur_avg * cur_vol + fill_px * add_lot) / new_vol, 4)
                    b["legs"].append({"leg": len(b["legs"]) + 1, "entry": fill_px, "vol": add_lot, "ts": ts})
                    b["total_volume"] = new_vol
                    b["avg_entry"] = new_avg
                    b["last_add_ts"] = ts
                    # Lock basket stop safely above/below new avg entry with min_lock_profit
                    if side == "buy":
                        lock_sl = round(min(new_avg + self.cfg.dyn_min_lock_profit, bid - self.cfg.sl_min_breathing_room), 4)
                        if lock_sl > b["stop"]:
                            b["stop"] = lock_sl
                            b["stop_locked"] = True
                    else:
                        lock_sl = round(max(new_avg - self.cfg.dyn_min_lock_profit, ask + self.cfg.sl_min_breathing_room), 4)
                        if lock_sl < b["stop"]:
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
            ex_px = bid if side == "buy" else ask
            if side == "buy":
                b["best_px"] = max(b["best_px"], bid)
                avg_gain = bid - b["avg_entry"]
                pullback = b["best_px"] - bid
                last_net = bid - b["legs"][-1]["entry"]
                dir_m60 = m60 if has_60 else 0.0
            else:
                b["best_px"] = min(b["best_px"], ask)
                avg_gain = b["avg_entry"] - ask
                pullback = ask - b["best_px"]
                last_net = b["legs"][-1]["entry"] - ask
                dir_m60 = -m60 if has_60 else 0.0

            # Early BE lock
            if not b["stop_locked"] and avg_gain >= self.cfg.be_arm_price:
                if side == "buy":
                    be_sl = round(min(b["avg_entry"] + self.cfg.be_lock_profit, bid - 0.60), 4)
                    if be_sl > b["stop"]:
                        b["stop"] = be_sl
                        b["stop_locked"] = True
                else:
                    be_sl = round(max(b["avg_entry"] - self.cfg.be_lock_profit, ask + 0.60), 4)
                    if be_sl < b["stop"]:
                        b["stop"] = be_sl
                        b["stop_locked"] = True

            # Continuous 55% Profit-Lock Ratchet
            if avg_gain >= self.cfg.ratchet_arm_price:
                if side == "buy":
                    r_sl = round(
                        min(b["avg_entry"] + self.cfg.ratchet_frac * avg_gain, bid - self.cfg.ratchet_breathing_room),
                        4,
                    )
                    if r_sl > b["stop"]:
                        b["stop"] = r_sl
                        b["stop_locked"] = True
                else:
                    r_sl = round(
                        max(b["avg_entry"] - self.cfg.ratchet_frac * avg_gain, ask + self.cfg.ratchet_breathing_room),
                        4,
                    )
                    if r_sl < b["stop"]:
                        b["stop"] = r_sl
                        b["stop_locked"] = True

            # Priority 1: Hard / Locked SL (gap-aware!)
            sl_hit = (bid <= b["stop"]) if side == "buy" else (ask >= b["stop"])
            if sl_hit:
                reason = (
                    ("profit_floor_hit" if avg_gain >= 0 else "locked_sl_gap_slip")
                    if b["stop_locked"]
                    else "hard_stop_loss"
                )
                closed = self._close_basket(ex_px, ts, reason)
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Priority 2: Trailing Pullback from Peak
            if avg_gain >= self.cfg.trail_arm_price and pullback >= self.cfg.trail_pullback_price:
                closed = self._close_basket(ex_px, ts, "snap_trail_exit")
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Priority 3: Max Hold Exit (longer if multi-leg runner!)
            hold_limit = self.cfg.runner_max_hold_sec if len(b["legs"]) >= 2 else self.cfg.max_hold_sec
            if (ts - b["opened_at"]) >= hold_limit:
                closed = self._close_basket(ex_px, ts, "max_hold_exit")
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Priority 4: House-Money Dynamic Pyramiding Add (only on real accelerating runners!)
            if (
                self.cfg.enable_adds
                and self.pending is None
                and len(b["legs"]) < self.cfg.max_legs
                and spread <= self.cfg.max_spread_price
                and (ts - b["last_add_ts"]) >= self.cfg.add_interval_sec
                and last_net >= self.cfg.add_step_price
                and dir_m60 >= self.cfg.add_min_60s_move
            ):
                est_fill = ask if side == "buy" else bid
                cand_lot = compute_dynamic_add_lot(
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
                    dir_move_60s=dir_m60,
                    hold_sec=max(0.0, ts - b["opened_at"]),
                    min_lock_profit=self.cfg.dyn_min_lock_profit,
                )
                if cand_lot >= 0.01:
                    self.pending = {"kind": "add", "side": side, "fill_ts": ts + self.cfg.latency_sec}

            self._update_dd(bid, ask)
            return events

        # 3) Flat: check causal signal
        if self.pending is None and ts >= self.cooldown_until:
            side, sl_dist, speed = self._detect_signal(ts, mid, spread)
            if side is not None:
                self.pending = {
                    "kind": "entry",
                    "side": side,
                    "sl_dist": sl_dist,
                    "speed": speed,
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

    def summary(self) -> dict[str, Any]:
        wins = [w for w in self.waves_closed if w["net_pnl_usd"] > 0]
        losses = [w for w in self.waves_closed if w["net_pnl_usd"] <= 0]
        gw = sum(w["net_pnl_usd"] for w in wins)
        gl = abs(sum(w["net_pnl_usd"] for w in losses))
        pf = round(gw / gl, 2) if gl > 1e-9 else (99.99 if gw > 0 else 0.0)
        return {
            "strategy": self.cfg.name,
            "waves_count": len(self.waves_closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round((len(wins) / len(self.waves_closed)) * 100.0, 1) if self.waves_closed else 0.0,
            "realized_pnl_usd": round(self.realized_pnl_usd, 2),
            "profit_factor": pf,
            "max_dd_usd": round(self.max_dd_usd, 2),
            "avg_win_usd": round(gw / len(wins), 2) if wins else 0.0,
            "avg_loss_usd": round(-gl / len(losses), 2) if losses else 0.0,
            "max_lot": round(max((w["total_volume"] for w in self.waves_closed), default=0.0), 2),
            "waves": self.waves_closed,
        }


def fast_snap_profiles(*, base_lot: float = 0.02) -> list[tuple[str, FastSnapConfig]]:
    """6 distinct causal Fast-Snap / V-Reversal / Acceleration architectures for real-tick A/B testing."""
    sess = (9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20)
    return [
        (
            "VSNAP_30S_SESS",
            FastSnapConfig(
                name="VSNAP_30S_SESS",
                mode="fast_impulse",
                leg_lot=base_lot,
                active_server_hours=sess,
                fast_win_sec=25,
                min_fast_move=3.60,
                min_fast_eff=0.58,
                confirm_win_sec=5,
                min_confirm_move=0.65,
                alt_60s_move=5.50,
                alt_60s_eff=0.44,
                alt_10s_move=1.10,
                max_pullback_from_30s_ext=0.70,
                hard_stop_price=2.50,
                be_arm_price=1.25,
                ratchet_arm_price=2.20,
                ratchet_frac=0.55,
                trail_arm_price=2.80,
                trail_pullback_price=1.85,
                max_hold_sec=240.0,
            ),
        ),
        (
            "VSNAP_30S_24H",
            FastSnapConfig(
                name="VSNAP_30S_24H",
                mode="fast_impulse",
                leg_lot=base_lot,
                active_server_hours=None,
                fast_win_sec=25,
                min_fast_move=3.60,
                min_fast_eff=0.58,
                confirm_win_sec=5,
                min_confirm_move=0.65,
                alt_60s_move=5.50,
                alt_60s_eff=0.44,
                alt_10s_move=1.10,
                max_pullback_from_30s_ext=0.70,
                hard_stop_price=2.50,
                be_arm_price=1.25,
                ratchet_arm_price=2.20,
                ratchet_frac=0.55,
                trail_arm_price=2.80,
                trail_pullback_price=1.85,
                max_hold_sec=240.0,
            ),
        ),
        (
            "VSNAP_SHOCK_6",
            FastSnapConfig(
                name="VSNAP_SHOCK_6",
                mode="fast_impulse",
                leg_lot=base_lot,
                active_server_hours=sess,
                fast_win_sec=30,
                min_fast_move=4.80,
                min_fast_eff=0.56,
                confirm_win_sec=8,
                min_confirm_move=0.90,
                alt_60s_move=6.00,
                alt_60s_eff=0.44,
                alt_10s_move=1.00,
                max_pullback_from_30s_ext=0.80,
                hard_stop_price=2.80,
                be_arm_price=1.50,
                ratchet_arm_price=2.50,
                ratchet_frac=0.55,
                ratchet_breathing_room=1.30,
                trail_arm_price=3.40,
                trail_pullback_price=2.20,
                max_hold_sec=300.0,
                dyn_max_l1_lot=0.05,
                dyn_max_leg_lot=0.10,
                dyn_max_total_lot=0.35,
            ),
        ),
        (
            "VTOP_SWEEP_SNAP",
            FastSnapConfig(
                name="VTOP_SWEEP_SNAP",
                mode="sweep_reversal",
                leg_lot=base_lot,
                active_server_hours=sess,
                sweep_lookback_sec=2400,
                sweep_min_history_sec=600,
                sweep_min_range=6.00,
                sweep_touch_pad=0.60,
                sweep_recent_sec=75,
                sweep_min_snap=2.30,
                sweep_max_snap=5.80,
                sweep_15s_move=1.20,
                sweep_20s_eff=0.48,
                hard_stop_price=2.60,
                max_stop_price=4.20,
                be_arm_price=1.60,
                ratchet_arm_price=2.60,
                ratchet_frac=0.55,
                ratchet_breathing_room=1.35,
                trail_arm_price=3.60,
                trail_pullback_price=2.30,
                max_hold_sec=420.0,
            ),
        ),
        (
            "MICRO_PB_7",
            FastSnapConfig(
                name="MICRO_PB_7",
                mode="micro_pullback",
                leg_lot=base_lot,
                active_server_hours=sess,
                pb_window_sec=240,
                pb_min_impulse=7.00,
                pb_min_retrace_frac=0.25,
                pb_max_retrace_frac=0.62,
                pb_min_dip_price=1.80,
                pb_resume_15s_move=1.10,
                pb_resume_15s_eff=0.50,
                hard_stop_price=2.60,
                be_arm_price=1.40,
                ratchet_arm_price=2.40,
                ratchet_frac=0.55,
                ratchet_breathing_room=1.25,
                trail_arm_price=3.20,
                trail_pullback_price=2.00,
                max_hold_sec=360.0,
            ),
        ),
        (
            "ACCEL_SCALP_15S",
            FastSnapConfig(
                name="ACCEL_SCALP_15S",
                mode="fast_impulse",
                leg_lot=base_lot,
                active_server_hours=sess,
                max_spread_price=0.55,
                fast_win_sec=15,
                min_fast_move=2.30,
                min_fast_eff=0.64,
                confirm_win_sec=4,
                min_confirm_move=0.55,
                alt_60s_move=0.0,  # purely 15s acceleration aligned with 60s
                min_60s_align_move=3.40,
                min_60s_align_eff=0.36,
                max_pullback_from_30s_ext=0.50,
                hard_stop_price=2.00,
                sl_min_breathing_room=0.95,
                be_arm_price=1.05,
                be_lock_profit=0.12,
                ratchet_arm_price=1.80,
                ratchet_frac=0.58,
                ratchet_breathing_room=0.95,
                trail_arm_price=2.30,
                trail_pullback_price=1.45,
                max_hold_sec=210.0,
                add_step_price=2.00,
                add_min_60s_move=4.50,
            ),
        ),
    ]
