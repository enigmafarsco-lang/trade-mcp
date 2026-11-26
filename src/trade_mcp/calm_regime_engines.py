"""Strictly causal Calm/Normal-Market Multi-Timeframe Engines (M1/M3/M5, ORDERS=0).

Complements the News/Shock engines (SHOCK10_DYN_RUN + MACRO_WAVE_DYN) during calm
15-hour drift and range regimes where 60-second shock moves are absent ($2-$5/60s)
but XAUUSD still generates $10-$105 multi-bar swings across 15m-13h horizons.

Why M1/M3/M5 multi-bar structure works in calm XAUUSD ($0.40 spread):
  - On 10s-30s micro-trades, $0.40 spread consumes 30-45% of the target move.
  - On M1/M3/M5 swings (ATR $2.0-$5.5), $0.40 spread is only 7-15% of one bar,
    and structural ATR stops ($3.50-$9.00) avoid 1-minute chop shakeouts.

All 4 architectures execute strictly causally on live Bid/Ask ticks with 250ms fill
latency, real spread, zero lookahead, and hard stop-losses on every L1 entry:
  1. CALM_M1_EMA_24H  — M1 EMA9/EMA21 pullback-continuation + house-money pyramid
  2. CALM_M3_SWING_24H — M3 EMA8/EMA18 swing pullback-reclaim + house-money pyramid
  3. CALM_M5_TREND_24H — M5 EMA8/EMA18 trend reclaim (gold_lab port) + pyramid
  4. CALM_M5_RANGE_24H — M5 Bollinger(14, 1.9σ) mean-reversion (strict range guard)
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from trade_mcp.s1_velocity_surfer import compute_dynamic_add_lot


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class CalmRegimeConfig:
    mode: str = "trend_reclaim"  # "trend_reclaim" | "range_bb"
    bar_sec: int = 180  # 60 (M1), 180 (M3), or 300 (M5)
    leg_lot: float = 0.02
    max_legs: int = 8
    multiplier: float = 100.0
    latency_sec: float = 0.25
    max_spread_price: float = 0.85

    # Indicator periods (kept compact so offline files warm up cleanly)
    fast_ema: int = 8
    slow_ema: int = 18
    atr_period: int = 10
    bb_period: int = 14
    bb_mult: float = 1.9
    min_bars: int = 14

    # Regime / signal filters
    max_60s_shock_block: float = 10.0  # pause calm engine during >=$10/60s nuclear shock (handled by SHOCK10)
    min_atr_price: float = 1.00  # skip dead flat micro-sleep where spread dominates
    max_atr_price: float = 12.00  # skip extreme news-bar whip
    min_trend_sep_atr: float = 0.20  # trend_reclaim: |fast_ema - slow_ema| >= X * ATR
    min_trend_eff: float = 0.24  # trend_reclaim: net move / path over last 8 bars
    min_body_frac: float = 0.30  # confirmation candle body >= 30% of bar range
    pullback_ema_atr_pad: float = 0.35  # low/high must retrace within fast_ema +/- pad*ATR

    # Range mean-reversion guards (mode == "range_bb")
    range_max_sep_atr: float = 0.75  # block fading if |fast_ema - slow_ema| > 0.75 * ATR (trending!)
    range_max_15m_move: float = 12.0  # block fading vertical >=$12/15m runs
    range_min_wick_or_body: float = 0.35  # closed rejection confirmation inside BB
    range_tp_past_mean_atr: float = 0.15  # target slightly past BB midline

    # Risk & stop-loss (structural ATR, ALWAYS hard SL)
    sl_atr_mult: float = 1.65
    min_stop_price: float = 3.50
    max_stop_price: float = 7.50
    sl_min_breathing_room: float = 2.00

    # Profit management & dynamic house-money pyramiding
    be_arm_atr_mult: float = 1.15  # arm BE once open gain >= max(be_arm_min_price, X * ATR)
    be_arm_min_price: float = 3.20
    profit_lock_arm_price: float = 5.50
    profit_lock_floor_price: float = 2.00
    trail_arm_price: float = 5.00
    trail_atr_mult: float = 1.45
    trail_min_pullback: float = 3.20
    trail_max_pullback: float = 7.50
    trail_giveback_frac: float = 0.32
    max_hold_sec: float = 2700.0  # up to 45 min for calm multi-bar swings

    # Dynamic house-money adds (winner-only, gap-proof locked SL)
    enable_adds: bool = True
    add_step_atr_mult: float = 1.00
    add_step_min_price: float = 3.00
    add_interval_sec: float = 30.0
    dyn_max_leg_lot: float = 0.08
    dyn_max_total_lot: float = 0.35
    dyn_min_lock_profit: float = 0.55
    dyn_profit_ratchet_arm: float = 4.50
    dyn_profit_ratchet_frac: float = 0.55

    # Cooldowns & optional session gate
    win_cooldown_sec: float = 60.0
    loss_cooldown_sec: float = 180.0
    consec_loss_cooldown_sec: float = 480.0
    active_server_hours: tuple[int, ...] | None = None  # optional broker-server active hours (None=24h)


@dataclass
class CalmRegimeEngine:
    """Strictly causal multi-timeframe calm-market tick engine (ORDERS=0)."""

    cfg: CalmRegimeConfig = field(default_factory=CalmRegimeConfig)
    initial_capital_usd: float = 1000.0

    _bars: deque[dict[str, float]] = field(default_factory=lambda: deque(maxlen=240))
    _cur_bar: dict[str, float] | None = None
    _sec_hist: deque[tuple[int, float]] = field(default_factory=lambda: deque(maxlen=960))
    _cur_sec: tuple[int, float] | None = None
    _prev_ts: float | None = None

    _ema_fast: float | None = None
    _ema_slow: float | None = None
    _prev_ema_slow: float | None = None
    _atr: float | None = None

    pending: dict[str, Any] | None = None
    basket: dict[str, Any] | None = None
    cooldown_until: float = 0.0
    consecutive_losses: int = 0
    next_wave_id: int = 1
    realized_pnl_usd: float = 0.0
    peak_equity_usd: float = 1000.0
    max_dd_usd: float = 0.0
    waves_closed: list[dict[str, Any]] = field(default_factory=list)
    last_metrics: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.peak_equity_usd = float(self.initial_capital_usd)

    def seed_closed_bars(self, bars: list[dict[str, float]]) -> None:
        """Optionally warm up indicators from strictly closed historical OHLC bars (live startup)."""
        for b in bars:
            self._push_closed_bar(
                {
                    "bar_id": float(b.get("bar_id", 0)),
                    "o": float(b["o"]),
                    "h": float(b["h"]),
                    "l": float(b["l"]),
                    "c": float(b["c"]),
                }
            )

    def _reset_buffers(self) -> None:
        self._bars.clear()
        self._cur_bar = None
        self._sec_hist.clear()
        self._cur_sec = None
        self._ema_fast = None
        self._ema_slow = None
        self._prev_ema_slow = None
        self._atr = None

    def _push_closed_bar(self, bar: dict[str, float]) -> None:
        prev_c = self._bars[-1]["c"] if self._bars else bar["o"]
        self._bars.append(bar)
        c = bar["c"]
        tr = max(bar["h"] - bar["l"], abs(bar["h"] - prev_c), abs(bar["l"] - prev_c))

        kf = 2.0 / (self.cfg.fast_ema + 1.0)
        ks = 2.0 / (self.cfg.slow_ema + 1.0)
        if self._ema_fast is None:
            self._ema_fast = c
            self._ema_slow = c
            self._prev_ema_slow = c
            self._atr = max(tr, 0.25)
        else:
            self._prev_ema_slow = self._ema_slow
            self._ema_fast = kf * c + (1.0 - kf) * self._ema_fast
            assert self._ema_slow is not None
            self._ema_slow = ks * c + (1.0 - ks) * self._ema_slow
            assert self._atr is not None
            alpha_atr = 1.0 / max(1, self.cfg.atr_period)
            self._atr = (1.0 - alpha_atr) * self._atr + alpha_atr * tr

    def _window_move(self, sec: int, mid: float, lookback_sec: int) -> float:
        t_cut = sec - lookback_sec
        for s_ts, s_mid in self._sec_hist:
            if s_ts >= t_cut:
                return mid - s_mid
        return 0.0

    def unrealized_pnl_usd(self, bid: float, ask: float) -> float:
        if self.basket is None:
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

    def _close(self, exit_px: float, ts: float, reason: str) -> dict[str, Any]:
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
            "regime": b["regime"],
            "legs_count": len(b["legs"]),
            "total_volume": b["total_volume"],
            "avg_entry": b["avg_entry"],
            "exit_price": round(exit_px, 4),
            "stop_final": b["stop"],
            "opened_at": b["opened_at"],
            "opened_utc": _iso_utc(b["opened_at"]),
            "closed_at": float(ts),
            "closed_utc": _iso_utc(ts),
            "hold_seconds": round(max(0.0, ts - b["opened_at"]), 2),
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

    def _trend_efficiency(self, n_bars: int = 8) -> float:
        if len(self._bars) < n_bars + 1:
            return 0.0
        recent = list(self._bars)[-(n_bars + 1) :]
        net = abs(recent[-1]["c"] - recent[0]["c"])
        path = sum(abs(recent[i]["c"] - recent[i - 1]["c"]) for i in range(1, len(recent)))
        return (net / path) if path > 1e-9 else 0.0

    def _bb_stats(self) -> tuple[float, float, float] | None:
        p = self.cfg.bb_period
        if len(self._bars) < p:
            return None
        closes = [b["c"] for b in list(self._bars)[-p:]]
        mean = sum(closes) / p
        var = sum((x - mean) ** 2 for x in closes) / p
        sd = math.sqrt(max(0.0, var))
        return mean, mean + self.cfg.bb_mult * sd, mean - self.cfg.bb_mult * sd

    def _evaluate_closed_bar_signal(
        self,
        *,
        closed_bar: dict[str, float],
        prev_bar: dict[str, float],
        sec: int,
        mid: float,
        spread: float,
        m1_context: dict[str, Any],
    ) -> dict[str, Any] | None:
        if len(self._bars) < self.cfg.min_bars:
            return None
        if self._ema_fast is None or self._ema_slow is None or self._prev_ema_slow is None or self._atr is None:
            return None
        if spread > self.cfg.max_spread_price:
            return None

        atr = float(self._atr)
        if atr < self.cfg.min_atr_price or atr > self.cfg.max_atr_price:
            return None

        m60 = self._window_move(sec, mid, 60)
        if self.cfg.max_60s_shock_block > 0.0 and abs(m60) >= self.cfg.max_60s_shock_block:
            return None

        o, h, low, c = closed_bar["o"], closed_bar["h"], closed_bar["l"], closed_bar["c"]
        rng = max(h - low, 1e-9)
        body = abs(c - o)
        ef = float(self._ema_fast)
        es = float(self._ema_slow)
        sep = abs(ef - es)

        if self.cfg.mode == "trend_reclaim":
            if sep < self.cfg.min_trend_sep_atr * atr:
                return None
            eff = self._trend_efficiency(8)
            if eff < self.cfg.min_trend_eff:
                return None
            if (body / rng) < self.cfg.min_body_frac:
                return None

            pad = self.cfg.pullback_ema_atr_pad * atr
            # Bullish pullback-reclaim into EMA zone
            if ef > es and es >= self._prev_ema_slow and c > o and c > ef:
                retrace_ok = (low <= ef + pad) or (prev_bar["l"] <= ef + pad) or (prev_bar["c"] < prev_bar["o"])
                not_overextended = (c - ef) <= (1.6 * atr)
                if retrace_ok and not_overextended:
                    swing_lo = min(low, prev_bar["l"])
                    raw_sl_dist = max(c - swing_lo + 0.55 * atr, self.cfg.sl_atr_mult * atr)
                    sl_dist = min(max(raw_sl_dist, self.cfg.min_stop_price), self.cfg.max_stop_price)
                    return {
                        "side": "buy",
                        "regime": f"CALM_M{self.cfg.bar_sec // 60}_TREND",
                        "sl_dist": round(sl_dist, 4),
                        "tp_px": None,
                        "atr": round(atr, 3),
                        "detail": f"ema_sep={sep:.2f} eff={eff:.2f} atr={atr:.2f}",
                        "m1_label": str(m1_context.get("label", "CALM")),
                    }

            # Bearish pullback-reclaim into EMA zone
            if ef < es and es <= self._prev_ema_slow and c < o and c < ef:
                retrace_ok = (h >= ef - pad) or (prev_bar["h"] >= ef - pad) or (prev_bar["c"] > prev_bar["o"])
                not_overextended = (ef - c) <= (1.6 * atr)
                if retrace_ok and not_overextended:
                    swing_hi = max(h, prev_bar["h"])
                    raw_sl_dist = max(swing_hi - c + 0.55 * atr, self.cfg.sl_atr_mult * atr)
                    sl_dist = min(max(raw_sl_dist, self.cfg.min_stop_price), self.cfg.max_stop_price)
                    return {
                        "side": "sell",
                        "regime": f"CALM_M{self.cfg.bar_sec // 60}_TREND",
                        "sl_dist": round(sl_dist, 4),
                        "tp_px": None,
                        "atr": round(atr, 3),
                        "detail": f"ema_sep={sep:.2f} eff={eff:.2f} atr={atr:.2f}",
                        "m1_label": str(m1_context.get("label", "CALM")),
                    }
            return None

        if self.cfg.mode == "range_bb":
            # Strict anti-trend guard: NEVER fade trending tape!
            if sep > self.cfg.range_max_sep_atr * atr:
                return None
            m15 = self._window_move(sec, mid, 900)
            if abs(m15) >= self.cfg.range_max_15m_move:
                return None
            bb = self._bb_stats()
            if bb is None:
                return None
            mean, upper, lower = bb
            half_band = upper - mean
            if half_band < max(2.20, 3.5 * spread):
                return None

            lower_wick = min(o, c) - low
            upper_wick = h - max(o, c)

            # Lower BB sweep + rejection close back inside band -> BUY to mean
            if (low <= lower or prev_bar["l"] <= lower) and c > lower and c < (mean - 0.35 * half_band):
                rej_ok = (c > o and (body / rng) >= self.cfg.range_min_wick_or_body) or (
                    (lower_wick / rng) >= self.cfg.range_min_wick_or_body
                )
                if rej_ok:
                    swing_lo = min(low, prev_bar["l"])
                    raw_sl = max(c - swing_lo + 0.45 * atr, self.cfg.sl_atr_mult * atr)
                    sl_dist = min(max(raw_sl, self.cfg.min_stop_price), self.cfg.max_stop_price)
                    tp_px = round(mean + self.cfg.range_tp_past_mean_atr * atr, 4)
                    return {
                        "side": "buy",
                        "regime": f"CALM_M{self.cfg.bar_sec // 60}_RANGE",
                        "sl_dist": round(sl_dist, 4),
                        "tp_px": tp_px,
                        "atr": round(atr, 3),
                        "detail": f"bb_lo={lower:.2f} mean={mean:.2f} atr={atr:.2f}",
                        "m1_label": str(m1_context.get("label", "CALM")),
                    }

            # Upper BB sweep + rejection close back inside band -> SELL to mean
            if (h >= upper or prev_bar["h"] >= upper) and c < upper and c > (mean + 0.35 * half_band):
                rej_ok = (c < o and (body / rng) >= self.cfg.range_min_wick_or_body) or (
                    (upper_wick / rng) >= self.cfg.range_min_wick_or_body
                )
                if rej_ok:
                    swing_hi = max(h, prev_bar["h"])
                    raw_sl = max(swing_hi - c + 0.45 * atr, self.cfg.sl_atr_mult * atr)
                    sl_dist = min(max(raw_sl, self.cfg.min_stop_price), self.cfg.max_stop_price)
                    tp_px = round(mean - self.cfg.range_tp_past_mean_atr * atr, 4)
                    return {
                        "side": "sell",
                        "regime": f"CALM_M{self.cfg.bar_sec // 60}_RANGE",
                        "sl_dist": round(sl_dist, 4),
                        "tp_px": tp_px,
                        "atr": round(atr, 3),
                        "detail": f"bb_hi={upper:.2f} mean={mean:.2f} atr={atr:.2f}",
                        "m1_label": str(m1_context.get("label", "CALM")),
                    }
        return None

    def on_tick(
        self,
        *,
        ts: float,
        bid: float,
        ask: float,
        m1_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return events

        mid = 0.5 * (bid + ask)
        spread = round(ask - bid, 4)
        sec = int(ts)

        # Clean reset across large multi-minute recording gaps
        max_gap = max(180.0, float(self.cfg.bar_sec) * 3.0)
        if self._prev_ts is not None and (ts - self._prev_ts) > max_gap:
            self._reset_buffers()
            self.pending = None
        self._prev_ts = ts

        # Update 1s mid history (for causal 60s/5m/15m moves)
        if self._cur_sec is None:
            self._cur_sec = (sec, mid)
        elif sec > self._cur_sec[0]:
            self._sec_hist.append(self._cur_sec)
            while self._sec_hist and (sec - self._sec_hist[0][0]) > 920:
                self._sec_hist.popleft()
            self._cur_sec = (sec, mid)
        else:
            self._cur_sec = (sec, mid)

        # Update causal multi-minute OHLC bar
        bar_id = int(ts // self.cfg.bar_sec)
        just_closed: dict[str, float] | None = None
        prev_closed: dict[str, float] | None = self._bars[-1] if self._bars else None
        if self._cur_bar is None:
            self._cur_bar = {"bar_id": float(bar_id), "o": mid, "h": mid, "l": mid, "c": mid}
        elif bar_id > int(self._cur_bar["bar_id"]):
            just_closed = dict(self._cur_bar)
            self._push_closed_bar(just_closed)
            self._cur_bar = {"bar_id": float(bar_id), "o": mid, "h": mid, "l": mid, "c": mid}
        else:
            if mid > self._cur_bar["h"]:
                self._cur_bar["h"] = mid
            if mid < self._cur_bar["l"]:
                self._cur_bar["l"] = mid
            self._cur_bar["c"] = mid

        self.last_metrics = {
            "ts": ts,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "bars_ready": len(self._bars),
            "atr": round(self._atr, 3) if self._atr is not None else 0.0,
            "ema_fast": round(self._ema_fast, 2) if self._ema_fast is not None else 0.0,
            "ema_slow": round(self._ema_slow, 2) if self._ema_slow is not None else 0.0,
        }

        # 1) Execute pending fill after causal latency
        if self.pending is not None and ts >= self.pending["fill_ts"]:
            po = self.pending
            self.pending = None
            side = po["side"]
            fill_px = ask if side == "buy" else bid
            if spread <= self.cfg.max_spread_price:
                if po["kind"] == "open" and self.basket is None:
                    sl_dist = float(po["sl_dist"])
                    stop_px = round((fill_px - sl_dist) if side == "buy" else (fill_px + sl_dist), 4)
                    l1_vol = round(self.cfg.leg_lot, 2)
                    self.basket = {
                        "wave_id": self.next_wave_id,
                        "side": side,
                        "regime": str(po["regime"]),
                        "legs": [{"entry": round(fill_px, 4), "volume": l1_vol, "ts": ts}],
                        "total_volume": l1_vol,
                        "avg_entry": round(fill_px, 4),
                        "stop": stop_px,
                        "tp": po.get("tp_px"),
                        "entry_atr": float(po.get("atr", 2.5)),
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
                        "regime": self.basket["regime"],
                        "entry": round(fill_px, 4),
                        "stop": stop_px,
                        "tp": self.basket["tp"],
                        "leg_lot": l1_vol,
                        "detail": po.get("detail", ""),
                        "m1_label": po["m1_label"],
                        "spread": spread,
                    })
                elif po["kind"] == "add" and self.basket is not None:
                    b = self.basket
                    if self.cfg.enable_adds and len(b["legs"]) < self.cfg.max_legs:
                        old_v = float(b["total_volume"])
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
                        if add_vol >= 0.01:
                            new_v = round(old_v + add_vol, 2)
                            new_avg = round((float(b["avg_entry"]) * old_v + fill_px * add_vol) / new_v, 4)
                            b["legs"].append({"entry": round(fill_px, 4), "volume": add_vol, "ts": ts})
                            b["total_volume"] = new_v
                            b["avg_entry"] = new_avg
                            b["last_leg_ts"] = ts
                            pad = max(0.10, self.cfg.dyn_min_lock_profit)
                            if side == "buy":
                                lock_sl = min(new_avg + pad, bid - self.cfg.sl_min_breathing_room)
                                b["stop"] = round(max(b["stop"], lock_sl), 4)
                            else:
                                lock_sl = max(new_avg - pad, ask + self.cfg.sl_min_breathing_room)
                                b["stop"] = round(min(b["stop"], lock_sl), 4)
                            b["stop_locked"] = True
                            events.append({
                                "type": "FIRE_ADD",
                                "wave_id": b["wave_id"],
                                "side": side,
                                "regime": b["regime"],
                                "leg_num": len(b["legs"]),
                                "leg_lot": add_vol,
                                "add_price": round(fill_px, 4),
                                "total_volume": new_v,
                                "avg_entry": new_avg,
                                "stop": b["stop"],
                            })

        # 2) Manage open basket
        if self.basket is not None:
            b = self.basket
            side = b["side"]
            ex_px = bid if side == "buy" else ask
            hold_sec = ts - float(b["opened_at"])
            live_atr = float(self._atr) if self._atr is not None else float(b["entry_atr"])

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

            # 1L Free-Ride BE arm (gap-proof cushion)
            be_arm = max(self.cfg.be_arm_min_price, self.cfg.be_arm_atr_mult * live_atr)
            if len(b["legs"]) == 1 and not b["stop_locked"] and avg_gain >= be_arm:
                pad = max(0.10, self.cfg.dyn_min_lock_profit)
                if side == "buy":
                    t_sl = min(b["avg_entry"] + pad, bid - self.cfg.sl_min_breathing_room)
                    if t_sl > b["stop"]:
                        b["stop"] = round(t_sl, 4)
                        b["stop_locked"] = True
                else:
                    t_sl = max(b["avg_entry"] - pad, ask + self.cfg.sl_min_breathing_room)
                    if t_sl < b["stop"]:
                        b["stop"] = round(t_sl, 4)
                        b["stop_locked"] = True

            # Flat Profit-Lock Floor
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

            # Dynamic Multi-Leg 55% Profit-Lock Ratchet
            if (
                self.cfg.dyn_profit_ratchet_frac > 0.0
                and len(b["legs"]) >= 2
                and avg_gain >= self.cfg.dyn_profit_ratchet_arm
            ):
                r_breath = max(self.cfg.sl_min_breathing_room, 2.40)
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

            # Hard / Locked SL (checked before TP — conservative!)
            if (side == "buy" and bid <= b["stop"]) or (side == "sell" and ask >= b["stop"]):
                reason = ("profit_floor_hit" if avg_gain >= 0 else "locked_sl_gap_slip") if b["stop_locked"] else "hard_stop_loss"
                closed = self._close(ex_px, ts, reason)
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Optional Fixed TP (range_bb mean target)
            if b["tp"] is not None:
                tp_hit = (bid >= float(b["tp"])) if side == "buy" else (ask <= float(b["tp"]))
                if tp_hit:
                    closed = self._close(ex_px, ts, "mean_tp_hit")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events

            # Structural ATR Tick-Trail (trend_reclaim)
            if b["tp"] is None and (
                avg_gain >= self.cfg.trail_arm_price or (b["stop_locked"] and max_exc >= self.cfg.trail_arm_price)
            ):
                base_pb = max(self.cfg.trail_min_pullback, self.cfg.trail_atr_mult * live_atr)
                allow_pb = min(max(base_pb, self.cfg.trail_giveback_frac * max(0.0, max_exc)), self.cfg.trail_max_pullback)
                if pb >= allow_pb and avg_gain > 0:
                    closed = self._close(ex_px, ts, "atr_trail_exit")
                    events.append({"type": "CLOSE_ALL", "wave": closed})
                    self._update_dd(bid, ask)
                    return events

            # Max Hold
            if hold_sec >= self.cfg.max_hold_sec:
                closed = self._close(ex_px, ts, "max_hold_exit")
                events.append({"type": "CLOSE_ALL", "wave": closed})
                self._update_dd(bid, ask)
                return events

            # Winner-Only House-Money Pyramid Add
            if (
                self.cfg.enable_adds
                and self.pending is None
                and len(b["legs"]) < self.cfg.max_legs
                and (ts - float(b["last_leg_ts"])) >= self.cfg.add_interval_sec
                and spread <= self.cfg.max_spread_price
            ):
                last_ent = float(b["legs"][-1]["entry"])
                last_net = (bid - last_ent) if side == "buy" else (last_ent - ask)
                add_step = max(self.cfg.add_step_min_price, self.cfg.add_step_atr_mult * live_atr)
                if last_net >= add_step:
                    est_fill = ask if side == "buy" else bid
                    s_sign = 1.0 if side == "buy" else -1.0
                    proj_lot = compute_dynamic_add_lot(
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
                    if proj_lot >= 0.01:
                        self.pending = {"kind": "add", "side": side, "fill_ts": ts + self.cfg.latency_sec}

            self._update_dd(bid, ask)
            return events

        # 3) Flat: evaluate signal strictly when a multi-minute bar just closed
        srv_hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        in_session = self.cfg.active_server_hours is None or srv_hour in self.cfg.active_server_hours
        if (
            in_session
            and just_closed is not None
            and prev_closed is not None
            and self.pending is None
            and ts >= self.cooldown_until
        ):
            sig = self._evaluate_closed_bar_signal(
                closed_bar=just_closed,
                prev_bar=prev_closed,
                sec=sec,
                mid=mid,
                spread=spread,
                m1_context=m1_context,
            )
            if sig is not None:
                self.pending = {
                    "kind": "open",
                    "side": sig["side"],
                    "regime": sig["regime"],
                    "sl_dist": sig["sl_dist"],
                    "tp_px": sig["tp_px"],
                    "atr": sig["atr"],
                    "detail": sig["detail"],
                    "m1_label": sig["m1_label"],
                    "fill_ts": ts + self.cfg.latency_sec,
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
            "open_basket": self.basket,
            "last_metrics": self.last_metrics,
        }


def calm_regime_profiles(
    *,
    leg_lot: float = 0.02,
    max_legs: int = 8,
) -> list[tuple[str, CalmRegimeConfig]]:
    """Canonical 4 calm-market multi-timeframe architectures (24h, ORDERS=0)."""
    base_lot = max(0.02, round(leg_lot, 2))
    return [
        (
            "CALM_M1_EMA_24H",
            CalmRegimeConfig(
                mode="trend_reclaim",
                bar_sec=60,
                leg_lot=base_lot,
                max_legs=max(8, max_legs),
                fast_ema=9,
                slow_ema=21,
                atr_period=14,
                min_bars=22,
                max_60s_shock_block=10.0,
                min_atr_price=1.00,
                min_trend_sep_atr=0.22,
                min_trend_eff=0.26,
                min_body_frac=0.35,
                pullback_ema_atr_pad=0.30,
                sl_atr_mult=1.80,
                min_stop_price=3.50,
                max_stop_price=6.50,
                sl_min_breathing_room=2.00,
                be_arm_atr_mult=1.20,
                be_arm_min_price=3.20,
                profit_lock_arm_price=5.20,
                profit_lock_floor_price=1.80,
                trail_arm_price=4.80,
                trail_atr_mult=1.50,
                trail_min_pullback=3.20,
                trail_max_pullback=7.00,
                max_hold_sec=2400.0,
                enable_adds=True,
                add_step_atr_mult=1.10,
                add_step_min_price=3.00,
                add_interval_sec=30.0,
                dyn_max_leg_lot=0.08,
                dyn_max_total_lot=0.30,
                dyn_min_lock_profit=0.55,
                dyn_profit_ratchet_arm=4.50,
                dyn_profit_ratchet_frac=0.55,
                win_cooldown_sec=60.0,
                loss_cooldown_sec=180.0,
                consec_loss_cooldown_sec=480.0,
            ),
        ),
        (
            "CALM_M3_SWING_24H",
            CalmRegimeConfig(
                mode="trend_reclaim",
                bar_sec=180,
                leg_lot=base_lot,
                max_legs=max(8, max_legs),
                fast_ema=8,
                slow_ema=18,
                atr_period=10,
                min_bars=12,
                max_60s_shock_block=10.0,
                min_atr_price=1.50,
                min_trend_sep_atr=0.20,
                min_trend_eff=0.24,
                min_body_frac=0.30,
                pullback_ema_atr_pad=0.35,
                sl_atr_mult=1.65,
                min_stop_price=4.00,
                max_stop_price=7.50,
                sl_min_breathing_room=2.20,
                be_arm_atr_mult=1.15,
                be_arm_min_price=3.80,
                profit_lock_arm_price=6.00,
                profit_lock_floor_price=2.20,
                trail_arm_price=5.50,
                trail_atr_mult=1.45,
                trail_min_pullback=3.60,
                trail_max_pullback=8.00,
                max_hold_sec=3000.0,
                enable_adds=True,
                add_step_atr_mult=1.00,
                add_step_min_price=3.20,
                add_interval_sec=45.0,
                dyn_max_leg_lot=0.08,
                dyn_max_total_lot=0.35,
                dyn_min_lock_profit=0.60,
                dyn_profit_ratchet_arm=5.00,
                dyn_profit_ratchet_frac=0.55,
                win_cooldown_sec=90.0,
                loss_cooldown_sec=240.0,
                consec_loss_cooldown_sec=600.0,
            ),
        ),
        (
            "CALM_M5_TREND_24H",
            CalmRegimeConfig(
                mode="trend_reclaim",
                bar_sec=300,
                leg_lot=base_lot,
                max_legs=max(8, max_legs),
                fast_ema=8,
                slow_ema=18,
                atr_period=10,
                min_bars=12,
                max_60s_shock_block=10.0,
                min_atr_price=1.80,
                min_trend_sep_atr=0.18,
                min_trend_eff=0.22,
                min_body_frac=0.28,
                pullback_ema_atr_pad=0.40,
                sl_atr_mult=1.55,
                min_stop_price=4.50,
                max_stop_price=9.00,
                sl_min_breathing_room=2.50,
                be_arm_atr_mult=1.10,
                be_arm_min_price=4.50,
                profit_lock_arm_price=7.00,
                profit_lock_floor_price=2.50,
                trail_arm_price=6.50,
                trail_atr_mult=1.45,
                trail_min_pullback=4.20,
                trail_max_pullback=9.50,
                max_hold_sec=3600.0,
                enable_adds=True,
                add_step_atr_mult=1.00,
                add_step_min_price=3.60,
                add_interval_sec=60.0,
                dyn_max_leg_lot=0.10,
                dyn_max_total_lot=0.40,
                dyn_min_lock_profit=0.65,
                dyn_profit_ratchet_arm=5.50,
                dyn_profit_ratchet_frac=0.55,
                win_cooldown_sec=120.0,
                loss_cooldown_sec=300.0,
                consec_loss_cooldown_sec=900.0,
            ),
        ),
        (
            "CALM_M5_RANGE_24H",
            CalmRegimeConfig(
                mode="range_bb",
                bar_sec=300,
                leg_lot=base_lot,
                max_legs=1,
                fast_ema=8,
                slow_ema=18,
                atr_period=10,
                bb_period=14,
                bb_mult=1.9,
                min_bars=14,
                max_60s_shock_block=5.0,
                min_atr_price=1.40,
                max_atr_price=6.50,
                range_max_sep_atr=0.75,
                range_max_15m_move=12.0,
                range_min_wick_or_body=0.35,
                range_tp_past_mean_atr=0.15,
                sl_atr_mult=1.35,
                min_stop_price=3.50,
                max_stop_price=6.50,
                sl_min_breathing_room=1.80,
                be_arm_atr_mult=1.05,
                be_arm_min_price=3.00,
                profit_lock_arm_price=0.0,
                profit_lock_floor_price=0.0,
                max_hold_sec=2400.0,
                enable_adds=False,
                dyn_min_lock_profit=0.45,
                dyn_profit_ratchet_frac=0.0,
                win_cooldown_sec=120.0,
                loss_cooldown_sec=300.0,
                consec_loss_cooldown_sec=900.0,
            ),
        ),
        (
            "CALM_M5_RR_24H",
            CalmRegimeConfig(
                mode="trend_reclaim",
                bar_sec=300,
                leg_lot=base_lot,
                max_legs=max(8, max_legs),
                fast_ema=8,
                slow_ema=18,
                atr_period=10,
                min_bars=12,
                max_60s_shock_block=10.0,
                min_atr_price=1.80,
                min_trend_sep_atr=0.18,
                min_trend_eff=0.22,
                min_body_frac=0.28,
                pullback_ema_atr_pad=0.40,
                sl_atr_mult=1.25,
                min_stop_price=3.80,
                max_stop_price=5.80,
                sl_min_breathing_room=2.80,
                be_arm_atr_mult=1.15,
                be_arm_min_price=5.00,
                profit_lock_arm_price=7.50,
                profit_lock_floor_price=2.80,
                trail_arm_price=7.00,
                trail_atr_mult=1.50,
                trail_min_pullback=4.50,
                trail_max_pullback=9.50,
                max_hold_sec=3600.0,
                enable_adds=True,
                add_step_atr_mult=0.85,
                add_step_min_price=3.00,
                add_interval_sec=45.0,
                dyn_max_leg_lot=0.10,
                dyn_max_total_lot=0.40,
                dyn_min_lock_profit=0.55,
                dyn_profit_ratchet_arm=4.50,
                dyn_profit_ratchet_frac=0.55,
                win_cooldown_sec=120.0,
                loss_cooldown_sec=300.0,
                consec_loss_cooldown_sec=900.0,
            ),
        ),
        (
            "CALM_M1_EMA_SESS",
            CalmRegimeConfig(
                mode="trend_reclaim",
                bar_sec=60,
                leg_lot=base_lot,
                max_legs=max(8, max_legs),
                fast_ema=9,
                slow_ema=21,
                atr_period=14,
                min_bars=22,
                max_60s_shock_block=10.0,
                min_atr_price=1.80,
                min_trend_sep_atr=0.22,
                min_trend_eff=0.26,
                min_body_frac=0.35,
                pullback_ema_atr_pad=0.30,
                sl_atr_mult=1.60,
                min_stop_price=3.00,
                max_stop_price=5.20,
                sl_min_breathing_room=1.90,
                be_arm_atr_mult=1.15,
                be_arm_min_price=3.00,
                profit_lock_arm_price=5.00,
                profit_lock_floor_price=1.80,
                trail_arm_price=4.60,
                trail_atr_mult=1.45,
                trail_min_pullback=3.00,
                trail_max_pullback=6.80,
                max_hold_sec=2400.0,
                enable_adds=True,
                add_step_atr_mult=1.00,
                add_step_min_price=2.80,
                add_interval_sec=30.0,
                dyn_max_leg_lot=0.08,
                dyn_max_total_lot=0.30,
                dyn_min_lock_profit=0.50,
                dyn_profit_ratchet_arm=4.20,
                dyn_profit_ratchet_frac=0.55,
                win_cooldown_sec=60.0,
                loss_cooldown_sec=180.0,
                consec_loss_cooldown_sec=480.0,
                active_server_hours=(9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19),
            ),
        ),
    ]


def build_calm_regime_profile_config(
    profile_name: str,
    *,
    leg_lot: float = 0.02,
    max_legs: int = 8,
) -> CalmRegimeConfig:
    by_name = dict(calm_regime_profiles(leg_lot=leg_lot, max_legs=max_legs))
    if profile_name not in by_name:
        valid = ", ".join(by_name.keys())
        raise ValueError(f"Unknown Calm-Regime profile '{profile_name}'. Valid profiles: {valid}")
    return by_name[profile_name]
