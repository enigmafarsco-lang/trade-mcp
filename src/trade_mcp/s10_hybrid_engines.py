"""Causal S10 (10-second VSA candle) + S/R + EMA-Reclaim Hybrid Engines for XAUUSD_i (ORDERS=0).

Bridges proven multi-year OHLCV concepts (VSA effort/wick morphology, SMC liquidity sweeps,
causal 15m Swing S/R breakouts, and `day-trader-skill` EMA reclaim) onto sub-second live
Bid/Ask tick streams with realistic 0.25s fill latency, live spread protection, and
non-saturating realized micro-path effort (`sum |dMid|` per 10s bar — immune to the 0.30s
polling quote-count ceiling during active London/NY sessions):

  1. SR_SWEEP_VSA_24H / SR_SWEEP_VSA_SESS:
     Range / stop-hunt mean-reversion engine. Detects causal 15-minute Support/Resistance
     liquidity sweeps (wick past S/R -> firm close back inside box + >=40% rejection tail
     + VSA tick-update volume surge) and targets reversion toward box midpoint with tight
     structural stop beyond the sweep wick.

  2. SR_BREAK_VSA_S10:
     Adapts the validated 750-day `SR_BREAKOUT_2C_VSA_M15` architecture to 10-second VSA
     candles during active London/NY hours: 2 clean accelerating S10 candles with rising
     quote-update volume breaking outside the strictly-prior 15m S/R box in M1 direction.

  3. EMA_RECLAIM_S10:
     Medium-wave ($4+ / 5m) trend pullback & EMA reclaim engine during active London/NY
     hours (bridges calm $1 chop and heavy $8+ Pullback-Dip impulses): aligns S10 EMA18/54
     + M1 trend bias, waits for pullback to EMA18, and enters on a clean VSA reclaim bar.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from trade_mcp.s1_impulse_audit import ALPARI_LONDON_NY_HOURS


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class S10HybridConfig:
    """Configuration for causal S10 VSA / S/R / EMA-Reclaim hybrid tick engines."""

    mode: str  # "sr_sweep" | "sr_break" | "ema_reclaim"
    leg_lot: float = 0.01
    max_legs: int = 1
    multiplier: float = 100.0
    bar_sec: int = 10
    sr_lookback_bars: int = 90  # 90 x 10s = 15 minutes strictly-prior S/R box
    min_sr_bars: int = 36  # min 6 minutes warmup before trading S/R
    vol_ma_bars: int = 20  # 20 x 10s = 3.3m baseline quote-update activity
    max_spread_price: float = 0.90
    latency_sec: float = 0.25

    # Mode-specific signal thresholds
    min_box_span_price: float = 3.50  # SR_SWEEP: need enough range for spread-safe reversion
    sweep_poke_price: float = 0.15  # SR_SWEEP: wick must poke past S/R by >= $0.15
    sweep_close_inside_price: float = 0.05  # SR_SWEEP: close back inside box by >= $0.05
    sweep_min_wick_ratio: float = 0.40  # SR_SWEEP: rejection tail >= 40% of bar range
    sweep_min_range_price: float = 0.80  # SR_SWEEP: bar range >= $0.80 (2x spread)
    sweep_max_trend_move_5m: float = 5.00  # SR_SWEEP: only block vertical trends with >= $5 move in 5m
    sweep_max_trend_eff: float = 0.58  # SR_SWEEP: avoid fading clean runaway vertical trends
    sweep_tp_box_frac: float = 0.60  # SR_SWEEP: target 60% reversion across range (clamped)

    break_min_body_ratio: float = 0.55  # SR_BREAK: clean body >= 55% of range
    break_max_opp_wick_ratio: float = 0.22  # SR_BREAK: tiny opposing rejection wick <= 22%
    break_min_b2_body: float = 0.75  # SR_BREAK: bar 2 body >= $0.75
    break_min_2c_body: float = 1.40  # SR_BREAK: combined 2-candle body >= $1.40
    break_clear_price: float = 0.30  # SR_BREAK: clean close beyond prior S/R
    break_max_start_ext: float = 0.60  # SR_BREAK: b1 open must not already be far past S/R

    ema_fast_period: int = 18  # 18 x 10s = 3-min EMA
    ema_slow_period: int = 54  # 54 x 10s = 9-min EMA
    ema_min_trend_5m: float = 4.00  # EMA_RECLAIM: medium $4+ / 5-min directional push
    ema_min_trend_eff: float = 0.32  # EMA_RECLAIM: smooth trend efficiency
    ema_pb_lookback_bars: int = 6  # look for EMA18 touch inside last 60s
    ema_pb_tol_price: float = 0.25  # within $0.25 of EMA18 counts as pullback tap
    ema_reclaim_clear_price: float = 0.20  # close >= $0.20 back on trend side of EMA18
    ema_min_body_ratio: float = 0.48
    ema_min_body_price: float = 0.55

    min_vol_ratio: float = 1.20  # VSA tick-update activity surge vs 20-bar MA

    # Risk & Exit management
    pivot_stop_buffer: float = 0.65
    min_stop_price: float = 1.60
    max_stop_price: float = 2.80
    tp_rr: float = 0.0  # fixed R-multiple TP when > 0 (used by SR_SWEEP)
    min_tp_price: float = 2.20
    max_tp_price: float = 5.50
    be_arm_price: float = 1.70
    profit_lock_arm_price: float = 0.0
    profit_lock_floor_price: float = 0.0
    sl_min_breathing_room: float = 1.15
    add_step_price: float = 2.00
    add_interval_sec: float = 5.0
    trail_arm_price: float = 3.60
    trail_min_pullback: float = 1.80
    trail_max_pullback: float = 4.20
    trail_giveback_frac: float = 0.30
    max_hold_sec: float = 360.0
    win_cooldown_sec: float = 20.0
    loss_cooldown_sec: float = 60.0
    consec_loss_cooldown_sec: float = 180.0
    active_server_hours: tuple[int, ...] | None = ALPARI_LONDON_NY_HOURS


def _s10_bar_metrics(bar: dict[str, float]) -> dict[str, float]:
    o, h, low, c = bar["o"], bar["h"], bar["l"], bar["c"]
    rng = max(h - low, 1e-9)
    body = abs(c - o)
    body_ratio = body / rng
    lower_wick = min(o, c) - low
    upper_wick = h - max(o, c)
    if c > o:
        d = 1
        opp_wick_ratio = upper_wick / rng
    elif c < o:
        d = -1
        opp_wick_ratio = lower_wick / rng
    else:
        d = 0
        opp_wick_ratio = max(upper_wick, lower_wick) / rng
    return {
        "dir": float(d),
        "body": body,
        "rng": rng,
        "body_ratio": body_ratio,
        "lower_wick_ratio": lower_wick / rng,
        "upper_wick_ratio": upper_wick / rng,
        "opp_wick_ratio": opp_wick_ratio,
    }


@dataclass
class S10HybridEngine:
    """Strictly causal Bid/Ask simulator for S10 VSA / S/R / EMA-Reclaim hybrid strategies."""

    cfg: S10HybridConfig
    initial_capital_usd: float = 1000.0

    _s10_bars: deque[dict[str, float]] = field(default_factory=deque)
    _cur_s10: dict[str, float] | None = None
    _prev_mid: float | None = None
    _last_ts: float = 0.0
    _ema_fast: float | None = None
    _ema_slow: float | None = None
    _ema_fast_hist: deque[float] = field(default_factory=deque)

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
    signals_blocked_spread: int = 0
    signals_blocked_regime: int = 0
    last_metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.peak_equity_usd = float(self.initial_capital_usd)
        if self.cfg.mode not in ("sr_sweep", "sr_break", "ema_reclaim"):
            raise ValueError(f"Unsupported S10HybridConfig mode: {self.cfg.mode}")

    def _reset_bars(self) -> None:
        self._s10_bars.clear()
        self._cur_s10 = None
        self._prev_mid = None
        self._ema_fast = None
        self._ema_slow = None
        self._ema_fast_hist.clear()
        self.pending = None

    def unrealized_pnl_usd(self, bid: float, ask: float) -> float:
        if self.basket is None or bid <= 0 or ask <= 0:
            return 0.0
        ex = bid if self.basket["side"] == "buy" else ask
        diff = (ex - self.basket["avg_entry"]) if self.basket["side"] == "buy" else (self.basket["avg_entry"] - ex)
        return round(diff * self.basket["total_volume"] * self.cfg.multiplier, 4)

    def _update_dd(self, bid: float, ask: float) -> None:
        eq = self.initial_capital_usd + self.realized_pnl_usd + self.unrealized_pnl_usd(bid, ask)
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
            "regime": self.cfg.mode.upper(),
            "opened_at": float(b["opened_at"]),
            "opened_utc": _iso_utc(b["opened_at"]),
            "closed_at": float(ts),
            "closed_utc": _iso_utc(ts),
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

    def _update_s10_bar(self, ts: float, mid: float) -> dict[str, float] | None:
        # Realized micro-path effort (sum |dMid| per 10s bar): unlike raw quote-change count,
        # this does NOT saturate at the 0.30s polling Nyquist ceiling (~33 updates/10s) during London/NY.
        delta_effort = abs(mid - self._prev_mid) if self._prev_mid is not None else 0.0
        self._prev_mid = mid

        bucket = int(ts // self.cfg.bar_sec)
        completed: dict[str, float] | None = None
        if self._cur_s10 is None:
            self._cur_s10 = {
                "bucket": float(bucket),
                "o": mid,
                "h": mid,
                "l": mid,
                "c": mid,
                "vol": delta_effort,
            }
        elif bucket > int(self._cur_s10["bucket"]):
            completed = self._cur_s10
            completed.update(_s10_bar_metrics(completed))
            self._s10_bars.append(completed)
            max_keep = max(self.cfg.sr_lookback_bars + 25, self.cfg.ema_slow_period + 25, 130)
            while len(self._s10_bars) > max_keep:
                self._s10_bars.popleft()

            # Update causal S10 EMAs on strictly closed bars
            c_val = completed["c"]
            if self._ema_fast is None:
                self._ema_fast = c_val
                self._ema_slow = c_val
            else:
                af = 2.0 / (self.cfg.ema_fast_period + 1.0)
                aslow = 2.0 / (self.cfg.ema_slow_period + 1.0)
                self._ema_fast = af * c_val + (1.0 - af) * self._ema_fast
                self._ema_slow = aslow * c_val + (1.0 - aslow) * float(self._ema_slow)
            self._ema_fast_hist.append(self._ema_fast)
            while len(self._ema_fast_hist) > 40:
                self._ema_fast_hist.popleft()

            self._cur_s10 = {
                "bucket": float(bucket),
                "o": mid,
                "h": mid,
                "l": mid,
                "c": mid,
                "vol": delta_effort,
            }
        else:
            if mid > self._cur_s10["h"]:
                self._cur_s10["h"] = mid
            if mid < self._cur_s10["l"]:
                self._cur_s10["l"] = mid
            self._cur_s10["c"] = mid
            self._cur_s10["vol"] += delta_effort
        return completed

    def _trend_5m_move_and_eff(self, bars: list[dict[str, float]]) -> tuple[float, float]:
        """Causal 5-minute (30 x S10 bars) net move & straight-line efficiency in [0,1]."""
        n_5m = max(6, int(round(300.0 / self.cfg.bar_sec)))
        if len(bars) < n_5m:
            return 0.0, 0.0
        win = bars[-n_5m:]
        net = win[-1]["c"] - win[0]["o"]
        path = sum(abs(win[k]["c"] - win[k - 1]["c"]) for k in range(1, len(win))) + abs(win[0]["c"] - win[0]["o"])
        eff = min(1.0, abs(net) / path) if path > 1e-9 else 0.0
        return net, eff

    def _evaluate_signal(
        self,
        *,
        ts: float,
        spread: float,
        in_session: bool,
        m1_context: dict[str, Any],
    ) -> dict[str, Any] | None:
        bars = list(self._s10_bars)
        n = len(bars)
        if n < max(self.cfg.vol_ma_bars + 2, 24):
            return None

        last = bars[-1]
        prior_vol = [b["vol"] for b in bars[-(self.cfg.vol_ma_bars + 1) : -1]]
        vol_ma = max(0.40, sum(prior_vol) / len(prior_vol))
        vol_ratio = last["vol"] / vol_ma

        m1_bias = int(m1_context.get("bias", 0))
        m1_label = str(m1_context.get("label", "UNKNOWN"))
        m1_sup = m1_context.get("support")
        m1_res = m1_context.get("resistance")

        mode = self.cfg.mode
        signal: dict[str, Any] | None = None

        if mode == "sr_sweep":
            if n < self.cfg.min_sr_bars + 1:
                return None
            prior_box = bars[-(self.cfg.sr_lookback_bars + 1) : -1]
            box_hi = max(b["h"] for b in prior_box)
            box_lo = min(b["l"] for b in prior_box)
            box_span = box_hi - box_lo
            if box_span < self.cfg.min_box_span_price:
                return None

            move_5m, eff_5m = self._trend_5m_move_and_eff(bars[:-1])
            if abs(move_5m) >= self.cfg.sweep_max_trend_move_5m and eff_5m > self.cfg.sweep_max_trend_eff:
                self.signals_blocked_regime += 1
                return None

            if last["rng"] < self.cfg.sweep_min_range_price or vol_ratio < self.cfg.min_vol_ratio:
                return None

            # Bullish stop-hunt below 15m Support -> rejection tail + close firmly back inside box
            if (
                last["l"] <= (box_lo - self.cfg.sweep_poke_price)
                and last["c"] >= (box_lo + self.cfg.sweep_close_inside_price)
                and last["lower_wick_ratio"] >= self.cfg.sweep_min_wick_ratio
            ):
                signal = {
                    "side": "buy",
                    "pivot_extreme": last["l"],
                    "box_span": box_span,
                    "vol_ratio": round(vol_ratio, 2),
                    "m1_label": m1_label,
                    "detail": f"sweep_lo={box_lo:.2f} tail={last['lower_wick_ratio']:.2f} vr={vol_ratio:.1f}x",
                }
            # Bearish stop-hunt above 15m Resistance -> rejection tail + close firmly back inside box
            elif (
                last["h"] >= (box_hi + self.cfg.sweep_poke_price)
                and last["c"] <= (box_hi - self.cfg.sweep_close_inside_price)
                and last["upper_wick_ratio"] >= self.cfg.sweep_min_wick_ratio
            ):
                signal = {
                    "side": "sell",
                    "pivot_extreme": last["h"],
                    "box_span": box_span,
                    "vol_ratio": round(vol_ratio, 2),
                    "m1_label": m1_label,
                    "detail": f"sweep_hi={box_hi:.2f} tail={last['upper_wick_ratio']:.2f} vr={vol_ratio:.1f}x",
                }

        elif mode == "sr_break":
            if n < self.cfg.min_sr_bars + 2:
                return None
            b1, b2 = bars[-2], bars[-1]
            d1, d2 = int(b1["dir"]), int(b2["dir"])
            if d1 == 0 or d1 != d2:
                return None

            prior_box = bars[-(self.cfg.sr_lookback_bars + 2) : -2]
            box_hi = max(b["h"] for b in prior_box)
            box_lo = min(b["l"] for b in prior_box)

            # Live M1 S/R break fallback when closed M1 is neutral
            eff_bias = m1_bias
            if eff_bias == 0:
                if m1_res is not None and b2["c"] > float(m1_res) + 0.30:
                    eff_bias = 1
                elif m1_sup is not None and b2["c"] < float(m1_sup) - 0.30:
                    eff_bias = -1
            if eff_bias != d2:
                return None

            clean_2c = (
                b1["body_ratio"] >= self.cfg.break_min_body_ratio
                and b2["body_ratio"] >= self.cfg.break_min_body_ratio
                and b1["opp_wick_ratio"] <= self.cfg.break_max_opp_wick_ratio
                and b2["opp_wick_ratio"] <= self.cfg.break_max_opp_wick_ratio
                and b2["body"] >= self.cfg.break_min_b2_body
                and (b1["body"] + b2["body"]) >= self.cfg.break_min_2c_body
                and vol_ratio >= self.cfg.min_vol_ratio
                and (b2["body"] >= 0.85 * b1["body"] or b2["vol"] >= b1["vol"])
            )
            if not clean_2c:
                return None

            if (
                d2 == 1
                and b1["o"] <= (box_hi + self.cfg.break_max_start_ext)
                and b2["c"] >= (box_hi + self.cfg.break_clear_price)
            ):
                signal = {
                    "side": "buy",
                    "pivot_extreme": min(b1["l"], b2["l"]),
                    "vol_ratio": round(vol_ratio, 2),
                    "m1_label": m1_label,
                    "detail": f"brk_hi={box_hi:.2f} 2c=${b1['body'] + b2['body']:.2f} vr={vol_ratio:.1f}x",
                }
            elif (
                d2 == -1
                and b1["o"] >= (box_lo - self.cfg.break_max_start_ext)
                and b2["c"] <= (box_lo - self.cfg.break_clear_price)
            ):
                signal = {
                    "side": "sell",
                    "pivot_extreme": max(b1["h"], b2["h"]),
                    "vol_ratio": round(vol_ratio, 2),
                    "m1_label": m1_label,
                    "detail": f"brk_lo={box_lo:.2f} 2c=${b1['body'] + b2['body']:.2f} vr={vol_ratio:.1f}x",
                }

        elif mode == "ema_reclaim":
            if n < self.cfg.ema_slow_period or self._ema_fast is None or self._ema_slow is None:
                return None
            ef = float(self._ema_fast)
            es = float(self._ema_slow)
            move_5m, eff_5m = self._trend_5m_move_and_eff(bars)
            if abs(move_5m) < self.cfg.ema_min_trend_5m or eff_5m < self.cfg.ema_min_trend_eff:
                return None

            tdir = 1 if move_5m > 0 else -1
            if (tdir == 1 and ef <= es) or (tdir == -1 and ef >= es):
                return None
            if m1_bias != tdir:
                return None

            if (
                int(last["dir"]) != tdir
                or last["body_ratio"] < self.cfg.ema_min_body_ratio
                or last["body"] < self.cfg.ema_min_body_price
                or vol_ratio < self.cfg.min_vol_ratio
            ):
                return None

            recent_pb = bars[-(self.cfg.ema_pb_lookback_bars + 1) : -1]
            ef_hist = list(self._ema_fast_hist)
            if not recent_pb or len(ef_hist) < 2:
                return None
            prev_ef = ef_hist[-2]
            prev_bar = bars[-2]

            if tdir == 1:
                pb_low = min(min(b["l"] for b in recent_pb), last["l"])
                near_ema_now = (
                    last["l"] <= (ef + self.cfg.ema_pb_tol_price)
                    or prev_bar["l"] <= (prev_ef + self.cfg.ema_pb_tol_price)
                    or prev_bar["c"] <= (prev_ef + self.cfg.ema_pb_tol_price + 0.10)
                )
                above_slow = pb_low >= (es - 1.20)
                reclaimed = last["c"] >= (ef + self.cfg.ema_reclaim_clear_price)
                not_chasing = (last["c"] - ef) <= 2.20
                if near_ema_now and above_slow and reclaimed and not_chasing:
                    signal = {
                        "side": "buy",
                        "pivot_extreme": pb_low,
                        "vol_ratio": round(vol_ratio, 2),
                        "m1_label": m1_label,
                        "detail": f"m5={move_5m:+.2f}(e={eff_5m:.2f}) ef={ef:.2f} vr={vol_ratio:.1f}x",
                    }
            else:
                pb_high = max(max(b["h"] for b in recent_pb), last["h"])
                near_ema_now = (
                    last["h"] >= (ef - self.cfg.ema_pb_tol_price)
                    or prev_bar["h"] >= (prev_ef - self.cfg.ema_pb_tol_price)
                    or prev_bar["c"] >= (prev_ef - self.cfg.ema_pb_tol_price - 0.10)
                )
                below_slow = pb_high <= (es + 1.20)
                reclaimed = last["c"] <= (ef - self.cfg.ema_reclaim_clear_price)
                not_chasing = (ef - last["c"]) <= 2.20
                if near_ema_now and below_slow and reclaimed and not_chasing:
                    signal = {
                        "side": "sell",
                        "pivot_extreme": pb_high,
                        "vol_ratio": round(vol_ratio, 2),
                        "m1_label": m1_label,
                        "detail": f"m5={move_5m:+.2f}(e={eff_5m:.2f}) ef={ef:.2f} vr={vol_ratio:.1f}x",
                    }

        if signal is None:
            return None
        if spread > self.cfg.max_spread_price:
            self.signals_blocked_spread += 1
            return None
        if not in_session:
            self.signals_blocked_session += 1
            return None
        return signal

    def on_tick(self, *, ts: float, bid: float, ask: float, m1_context: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return events

        if self._last_ts > 0.0 and (ts - self._last_ts) > 60.0:
            self._reset_bars()
        self._last_ts = ts

        mid = 0.5 * (bid + ask)
        spread = round(ask - bid, 5)
        srv_hour = (int(ts) // 3600) % 24
        in_session = (self.cfg.active_server_hours is None) or (srv_hour in self.cfg.active_server_hours)
        completed_s10 = self._update_s10_bar(ts, mid)

        self.last_metrics = {
            "ts": ts,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "server_hour": srv_hour,
            "in_session": in_session,
            "s10_bars": len(self._s10_bars),
            "ema_fast": round(self._ema_fast, 2) if self._ema_fast is not None else None,
            "ema_slow": round(self._ema_slow, 2) if self._ema_slow is not None else None,
            "m1_label": str(m1_context.get("label", "UNKNOWN")),
        }

        # 1) Execute pending fill after realistic latency
        if self.pending is not None and ts >= self.pending["fill_ts"]:
            po = self.pending
            self.pending = None
            side = po["side"]
            fill_px = ask if side == "buy" else bid
            if spread <= self.cfg.max_spread_price:
                if po["kind"] == "open" and self.basket is None:
                    piv = float(po["pivot_extreme"])
                    if side == "buy":
                        raw_dist = fill_px - (piv - self.cfg.pivot_stop_buffer)
                        sdist = min(max(raw_dist, self.cfg.min_stop_price), self.cfg.max_stop_price)
                        stop_px = round(fill_px - sdist, 4)
                    else:
                        raw_dist = (piv + self.cfg.pivot_stop_buffer) - fill_px
                        sdist = min(max(raw_dist, self.cfg.min_stop_price), self.cfg.max_stop_price)
                        stop_px = round(fill_px + sdist, 4)

                    tp_px: float | None = None
                    if self.cfg.tp_rr > 0.0:
                        tp_dist = sdist * self.cfg.tp_rr
                        if "box_span" in po:
                            tp_dist = max(tp_dist, float(po["box_span"]) * self.cfg.sweep_tp_box_frac)
                        tp_dist = min(max(tp_dist, self.cfg.min_tp_price), self.cfg.max_tp_price)
                        tp_px = round(fill_px + tp_dist, 4) if side == "buy" else round(fill_px - tp_dist, 4)

                    self.basket = {
                        "wave_id": self.next_wave_id,
                        "side": side,
                        "regime": self.cfg.mode.upper(),
                        "legs": [{"entry": round(fill_px, 4), "volume": self.cfg.leg_lot, "ts": ts}],
                        "total_volume": self.cfg.leg_lot,
                        "avg_entry": round(fill_px, 4),
                        "stop": stop_px,
                        "tp": tp_px,
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
                        "regime": self.cfg.mode.upper(),
                        "entry": round(fill_px, 4),
                        "stop": stop_px,
                        "tp": tp_px,
                        "detail": po.get("detail", ""),
                        "m1_label": po["m1_label"],
                        "spread": spread,
                    })
                elif po["kind"] == "add" and self.basket is not None:
                    b = self.basket
                    if len(b["legs"]) < self.cfg.max_legs and self._can_add_risk_free(side, bid, ask, fill_px):
                        last_ent = float(b["legs"][-1]["entry"])
                        last_net = (bid - last_ent) if side == "buy" else (last_ent - ask)
                        old_v = b["total_volume"]
                        new_v = round(old_v + self.cfg.leg_lot, 2)
                        new_avg = round((b["avg_entry"] * old_v + fill_px * self.cfg.leg_lot) / new_v, 4)
                        b["legs"].append({"entry": round(fill_px, 4), "volume": self.cfg.leg_lot, "ts": ts})
                        b["total_volume"] = new_v
                        b["avg_entry"] = new_avg
                        b["last_leg_ts"] = ts
                        if side == "buy":
                            lock_sl = min(new_avg + 0.08, bid - self.cfg.sl_min_breathing_room)
                            b["stop"] = round(max(b["stop"], lock_sl), 4)
                        else:
                            lock_sl = max(new_avg - 0.08, ask + self.cfg.sl_min_breathing_room)
                            b["stop"] = round(min(b["stop"], lock_sl), 4)
                        b["stop_locked"] = True
                        events.append({
                            "type": "FIRE_ADD",
                            "wave_id": b["wave_id"],
                            "side": side,
                            "regime": self.cfg.mode.upper(),
                            "leg_num": len(b["legs"]),
                            "add_price": round(fill_px, 4),
                            "total_volume": new_v,
                            "avg_entry": new_avg,
                            "stop": b["stop"],
                            "last_leg_net": round(last_net, 3),
                        })

        # 2) Manage open basket (always active even across session boundary)
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
            if len(b["legs"]) == 1 and not b["stop_locked"] and self.cfg.be_arm_price > 0.0 and avg_gain >= self.cfg.be_arm_price:
                if side == "buy":
                    t_sl = min(b["avg_entry"] + 0.08, bid - self.cfg.sl_min_breathing_room)
                    if t_sl > b["stop"]:
                        b["stop"] = round(t_sl, 4)
                        b["stop_locked"] = True
                else:
                    t_sl = max(b["avg_entry"] - 0.08, ask + self.cfg.sl_min_breathing_room)
                    if t_sl < b["stop"]:
                        b["stop"] = round(t_sl, 4)
                        b["stop_locked"] = True

            # Optional Profit-Lock floor
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

            # Hard / Locked SL (checked conservatively BEFORE TP)
            if (side == "buy" and bid <= b["stop"]) or (side == "sell" and ask >= b["stop"]):
                reason = ("profit_floor_hit" if avg_gain >= 0 else "locked_sl_gap_slip") if b["stop_locked"] else "hard_stop_loss"
                closed = self._close(ex_px, ts, reason)
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Optional fixed TP (used by SR_SWEEP mean-reversion)
            if b["tp"] is not None and (
                (side == "buy" and bid >= float(b["tp"])) or (side == "sell" and ask <= float(b["tp"]))
            ):
                closed = self._close(ex_px, ts, "take_profit_hit")
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Tick-Trail (for trend/breakout runners when tp_rr == 0)
            if self.cfg.trail_arm_price > 0.0 and (
                avg_gain >= self.cfg.trail_arm_price or (b["stop_locked"] and max_exc >= self.cfg.trail_arm_price)
            ):
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

            # Risk-Free Winner Pyramid (only when max_legs > 1)
            if (
                self.cfg.max_legs > 1
                and self.pending is None
                and len(b["legs"]) < self.cfg.max_legs
                and (ts - b["last_leg_ts"]) >= self.cfg.add_interval_sec
                and spread <= self.cfg.max_spread_price
            ):
                last_ent = float(b["legs"][-1]["entry"])
                last_net = (bid - last_ent) if side == "buy" else (last_ent - ask)
                est_fill = ask if side == "buy" else bid
                if last_net >= self.cfg.add_step_price and self._can_add_risk_free(side, bid, ask, est_fill):
                    self.pending = {"kind": "add", "side": side, "fill_ts": ts + self.cfg.latency_sec}

            self._update_dd(bid, ask)
            return events

        # 3) Flat: evaluate S10 hybrid signals strictly on S10 bar close
        if completed_s10 is not None and self.pending is None and ts >= self.cooldown_until:
            sig = self._evaluate_signal(ts=ts, spread=spread, in_session=in_session, m1_context=m1_context)
            if sig is not None:
                self.pending = {
                    "kind": "open",
                    "fill_ts": ts + self.cfg.latency_sec,
                    **sig,
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
            "signals_blocked_regime": self.signals_blocked_regime,
            "open_basket": self.basket,
            "last_metrics": self.last_metrics,
        }


def s10_hybrid_profiles(
    *,
    leg_lot: float = 0.01,
    max_legs: int = 3,
) -> list[tuple[str, S10HybridConfig]]:
    """Canonical S10 VSA / S/R / EMA-Reclaim hybrid profiles for offline audit & live Paper."""
    pyr_legs = max(1, min(3, int(max_legs)))
    return [
        (
            "SR_SWEEP_VSA_24H",
            S10HybridConfig(
                mode="sr_sweep",
                leg_lot=leg_lot,
                max_legs=1,
                min_box_span_price=3.50,
                sweep_poke_price=0.15,
                sweep_close_inside_price=0.05,
                sweep_min_wick_ratio=0.40,
                sweep_min_range_price=0.80,
                sweep_max_trend_eff=0.58,
                sweep_tp_box_frac=0.60,
                min_vol_ratio=1.20,
                pivot_stop_buffer=0.60,
                min_stop_price=1.60,
                max_stop_price=2.60,
                tp_rr=1.50,
                min_tp_price=2.40,
                max_tp_price=5.00,
                be_arm_price=1.60,
                sl_min_breathing_room=1.00,
                trail_arm_price=0.0,
                max_hold_sec=300.0,
                win_cooldown_sec=30.0,
                loss_cooldown_sec=60.0,
                consec_loss_cooldown_sec=180.0,
                active_server_hours=None,
            ),
        ),
        (
            "SR_SWEEP_VSA_SESS",
            S10HybridConfig(
                mode="sr_sweep",
                leg_lot=leg_lot,
                max_legs=1,
                min_box_span_price=3.50,
                sweep_poke_price=0.15,
                sweep_close_inside_price=0.05,
                sweep_min_wick_ratio=0.40,
                sweep_min_range_price=0.80,
                sweep_max_trend_eff=0.58,
                sweep_tp_box_frac=0.60,
                min_vol_ratio=1.20,
                pivot_stop_buffer=0.60,
                min_stop_price=1.60,
                max_stop_price=2.60,
                tp_rr=1.50,
                min_tp_price=2.40,
                max_tp_price=5.00,
                be_arm_price=1.60,
                sl_min_breathing_room=1.00,
                trail_arm_price=0.0,
                max_hold_sec=300.0,
                win_cooldown_sec=30.0,
                loss_cooldown_sec=60.0,
                consec_loss_cooldown_sec=180.0,
                active_server_hours=ALPARI_LONDON_NY_HOURS,
            ),
        ),
        (
            "SR_BREAK_VSA_S10",
            S10HybridConfig(
                mode="sr_break",
                leg_lot=leg_lot,
                max_legs=pyr_legs,
                break_min_body_ratio=0.55,
                break_max_opp_wick_ratio=0.22,
                break_min_b2_body=0.75,
                break_min_2c_body=1.40,
                break_clear_price=0.30,
                break_max_start_ext=0.60,
                min_vol_ratio=1.25,
                pivot_stop_buffer=0.65,
                min_stop_price=2.00,
                max_stop_price=3.00,
                tp_rr=0.0,
                be_arm_price=1.90,
                profit_lock_arm_price=3.40,
                profit_lock_floor_price=1.25,
                sl_min_breathing_room=1.35,
                add_step_price=2.00,
                trail_arm_price=3.80,
                trail_min_pullback=1.80,
                trail_max_pullback=4.20,
                trail_giveback_frac=0.30,
                max_hold_sec=360.0,
                win_cooldown_sec=20.0,
                loss_cooldown_sec=60.0,
                consec_loss_cooldown_sec=180.0,
                active_server_hours=ALPARI_LONDON_NY_HOURS,
            ),
        ),
        (
            "EMA_RECLAIM_S10",
            S10HybridConfig(
                mode="ema_reclaim",
                leg_lot=leg_lot,
                max_legs=pyr_legs,
                ema_fast_period=18,
                ema_slow_period=54,
                ema_min_trend_5m=4.00,
                ema_min_trend_eff=0.32,
                ema_pb_lookback_bars=6,
                ema_pb_tol_price=0.25,
                ema_reclaim_clear_price=0.20,
                ema_min_body_ratio=0.48,
                ema_min_body_price=0.55,
                min_vol_ratio=1.05,
                pivot_stop_buffer=0.65,
                min_stop_price=1.80,
                max_stop_price=2.80,
                tp_rr=0.0,
                be_arm_price=1.80,
                profit_lock_arm_price=3.20,
                profit_lock_floor_price=1.20,
                sl_min_breathing_room=1.30,
                add_step_price=2.00,
                trail_arm_price=3.60,
                trail_min_pullback=1.75,
                trail_max_pullback=4.00,
                trail_giveback_frac=0.30,
                max_hold_sec=420.0,
                win_cooldown_sec=20.0,
                loss_cooldown_sec=60.0,
                consec_loss_cooldown_sec=180.0,
                active_server_hours=ALPARI_LONDON_NY_HOURS,
            ),
        ),
    ]


def build_s10_hybrid_profile_config(
    profile_name: str,
    *,
    leg_lot: float = 0.01,
    max_legs: int = 3,
) -> S10HybridConfig:
    """Resolve a canonical S10 hybrid profile name to its exact S10HybridConfig."""
    by_name = dict(s10_hybrid_profiles(leg_lot=leg_lot, max_legs=max_legs))
    if profile_name not in by_name:
        valid = ", ".join(by_name.keys())
        raise ValueError(f"Unknown S10 hybrid profile '{profile_name}'. Valid profiles: {valid}")
    return by_name[profile_name]
