"""Strictly causal Volatility-Acceleration & Winner-Only Pyramid Lab (M1 & M5).

Tests high-frequency acceleration trading that stays silent during dead chop
and scales trade count + position size ONLY when momentum/volatility accelerates
and existing trades are ALREADY in profit (Winner-Only Pyramiding with BE/profit SL lock):

  - NEVER averages down into losers (proven 0/8 negative earlier).
  - Adds Leg 2 / Leg 3 strictly on bar-close confirmation past +pyr_step_r * R,
    filling at next bar open + real spread, and immediately tightening SL to
    breakeven / locked profit so max downside risk stays bounded at ~1x initial risk.
  - Supports zero-cooldown rapid re-entry after winning exits during active waves.
  - Evaluates H1/H2 stability, exclBest, 1.5x spread stress, and MC p95 DD (ORDERS=0).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from trade_mcp.daytrade_paper import resolve_pricing_basis
from trade_mcp.m1_momentum import atr_series, clean_ohlcv_bars, ema_series
from trade_mcp.pullback_basket_lab import _in_session, _mc_p95_dd, _summarize_pnls


@dataclass(frozen=True)
class AccelPyramidConfig:
    name: str
    mode: str
    # Modes:
    # - "pre_pb_rej": trend_lookback + counter pb_lookback + trend-aligned rejection bar
    # - "vol_cont": volatility expansion (ATR14/ATR100 >= vol_ratio_min) + EMA stack + strong continuation bar
    # - "vol_pb_burst": vol expansion + 1-2 bar micro-pullback + immediate trend resume bar
    # - "shock_break": high absolute ATR shock + 5-bar breakout + EMA9/21 aligned
    # - "vol_fade_ctrl": honest contrarian fade control during vol explosions
    max_legs: int = 3
    leg_volume: float = 0.01
    pyr_step_r: float = 0.85  # add next leg on bar close >= k * pyr_step_r * init_risk
    sl_atr: float = 1.5
    tp_atr: float = 3.2
    trail_atr: float = 1.2
    max_hold_bars: int = 25
    exp_atr_mult: float = 1.4
    body_ratio_min: float = 0.50
    trend_lookback: int = 15
    pb_lookback: int = 5
    trend_min_atr: float = 1.5
    pb_min_atr: float = 0.6
    vol_ratio_min: float = 0.0  # ATR14 / ATR100 threshold (0 = disabled)
    min_atr_price: float = 0.60
    session_hours: tuple[int, int] | None = (7, 20)
    win_cooldown_bars: int = 0
    loss_cooldown_bars: int = 2


def _rolling_mean(vals: list[float], period: int) -> list[float]:
    out = [0.0] * len(vals)
    acc = 0.0
    for i, v in enumerate(vals):
        acc += v
        if i >= period:
            acc -= vals[i - period]
        cnt = min(i + 1, period)
        out[i] = acc / cnt if cnt > 0 else 0.0
    return out


def _detect_accel_signal(
    i: int,
    bars: list[list[float]],
    ema9: list[float],
    ema21: list[float],
    ema50: list[float],
    atr14: list[float],
    atr_base: list[float],
    cfg: AccelPyramidConfig,
) -> str | None:
    """Strictly causal signal evaluation on closed bar i (uses only <= i)."""
    atr = atr14[i]
    if atr < cfg.min_atr_price or atr <= 0:
        return None
    if cfg.vol_ratio_min > 0:
        base = atr_base[i]
        if base <= 0 or (atr / base) < cfg.vol_ratio_min:
            return None
    if not _in_session(bars[i][0], cfg.session_hours):
        return None

    o, h, low, c = bars[i][1], bars[i][2], bars[i][3], bars[i][4]
    rng = h - low
    if rng <= 0:
        return None
    body_ratio = abs(c - o) / rng

    if cfg.mode == "pre_pb_rej":
        lb = cfg.trend_lookback
        pb = cfg.pb_lookback
        if i < lb + 2 or pb >= lb:
            return None
        trend_move = bars[i - pb][4] - bars[i - lb][1]
        pb_move = bars[i - 1][4] - bars[i - pb][4]
        if rng < cfg.exp_atr_mult * atr or body_ratio < cfg.body_ratio_min:
            return None
        if (
            trend_move >= cfg.trend_min_atr * atr
            and pb_move <= -cfg.pb_min_atr * atr
            and ema21[i] >= ema50[i]
            and c > o
            and c > bars[i - 1][2]
        ):
            return "buy"
        if (
            trend_move <= -cfg.trend_min_atr * atr
            and pb_move >= cfg.pb_min_atr * atr
            and ema21[i] <= ema50[i]
            and c < o
            and c < bars[i - 1][3]
        ):
            return "sell"
        return None

    if cfg.mode == "vol_cont":
        if rng < cfg.exp_atr_mult * atr or body_ratio < cfg.body_ratio_min:
            return None
        if ema9[i] > ema21[i] > ema50[i] and c > o and c > bars[i - 1][2]:
            return "buy"
        if ema9[i] < ema21[i] < ema50[i] and c < o and c < bars[i - 1][3]:
            return "sell"
        return None

    if cfg.mode == "vol_pb_burst":
        if i < 5 or rng < cfg.exp_atr_mult * atr or body_ratio < cfg.body_ratio_min:
            return None
        prev1_bear = bars[i - 1][4] < bars[i - 1][1]
        prev2_bear = bars[i - 2][4] < bars[i - 2][1]
        prev1_bull = bars[i - 1][4] > bars[i - 1][1]
        prev2_bull = bars[i - 2][4] > bars[i - 2][1]
        if ema9[i] > ema21[i] > ema50[i] and (prev1_bear or prev2_bear) and c > o and c > bars[i - 1][2]:
            return "buy"
        if ema9[i] < ema21[i] < ema50[i] and (prev1_bull or prev2_bull) and c < o and c < bars[i - 1][3]:
            return "sell"
        return None

    if cfg.mode == "shock_break":
        if i < 10 or rng < cfg.exp_atr_mult * atr or body_ratio < cfg.body_ratio_min:
            return None
        hi5 = max(bars[k][2] for k in range(i - 5, i))
        lo5 = min(bars[k][3] for k in range(i - 5, i))
        if ema9[i] > ema21[i] and c > o and c > hi5:
            return "buy"
        if ema9[i] < ema21[i] and c < o and c < lo5:
            return "sell"
        return None

    if cfg.mode == "vol_fade_ctrl":
        if rng < cfg.exp_atr_mult * atr or body_ratio < cfg.body_ratio_min:
            return None
        # Fade the exploding candle (honest control)
        if c > o and c > bars[i - 1][2]:
            return "sell"
        if c < o and c < bars[i - 1][3]:
            return "buy"
        return None

    return None


def simulate_accel_pyramid(
    bars: list[list[float]],
    cfg: AccelPyramidConfig,
    *,
    spread_price: float,
    multiplier: float,
) -> dict[str, Any]:
    """Strictly causal simulator with Winner-Only bar-close Pyramiding & BE/profit SL lock."""
    n = len(bars)
    if n < 120:
        return {
            "pnls": [], "baskets": 0, "total_legs_executed": 0,
            "avg_legs": 0.0, "max_lots": 0.0, "exit_reasons": {}, "trades_meta": [],
        }

    closes = [b[4] for b in bars]
    ema9 = [float(v) if v is not None else 0.0 for v in ema_series(closes, 9)]
    ema21 = [float(v) if v is not None else 0.0 for v in ema_series(closes, 21)]
    ema50 = [float(v) if v is not None else 0.0 for v in ema_series(closes, 50)]
    atr14 = [float(v) if v is not None else 0.0 for v in atr_series(bars, period=14)]
    atr_base = _rolling_mean(atr14, 100)

    pnls: list[float] = []
    leg_counts: list[int] = []
    exit_reasons: dict[str, int] = {}
    trades_meta: list[dict[str, Any]] = []

    side: str | None = None
    legs: list[float] = []  # spread-adjusted entry fill prices
    sl_px = 0.0
    tp_px = 0.0
    init_risk = 0.0
    entry_idx = -1
    trailed = False
    pending_add = False
    cooldown_until = 105

    def _close_all(exit_px: float, close_idx: int, reason: str) -> None:
        nonlocal side, legs, trailed, pending_add, cooldown_until
        assert side is not None and legs
        total = 0.0
        for ent in legs:
            diff = (exit_px - ent) if side == "buy" else (ent - exit_px)
            total += diff * cfg.leg_volume * multiplier
        pnl_r = round(total, 4)
        pnls.append(pnl_r)
        leg_counts.append(len(legs))
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        trades_meta.append({
            "entry_idx": entry_idx,
            "close_idx": close_idx,
            "side": side,
            "legs": len(legs),
            "pnl": pnl_r,
            "reason": reason,
        })
        side = None
        legs = []
        trailed = False
        pending_add = False
        cd = cfg.win_cooldown_bars if pnl_r > 0 else cfg.loss_cooldown_bars
        cooldown_until = close_idx + cd

    for i in range(105, n - 1):
        bar = bars[i]
        o, h, low, c = bar[1], bar[2], bar[3], bar[4]

        # Execute scheduled winner-pyramid add strictly at bar i open (from bar i-1 close signal)
        if side is not None and pending_add:
            pending_add = False
            if len(legs) < cfg.max_legs:
                add_ent = (o + spread_price) if side == "buy" else o
                # Guard: only fill add if open hasn't gapped past SL
                if (side == "buy" and o > sl_px) or (side == "sell" and (o + spread_price) < sl_px):
                    legs.append(add_ent)

        if side is not None:
            bars_held = i - entry_idx

            # Priority 1: Hard SL FIRST (worst-case intrabar priority, gap-aware)
            if side == "buy":
                if low <= sl_px:
                    fill = min(o, sl_px)
                    _close_all(fill, i, "sl_trail" if trailed else "sl")
                    continue
            else:
                if h + spread_price >= sl_px:
                    fill = max(o + spread_price, sl_px)
                    _close_all(fill, i, "sl_trail" if trailed else "sl")
                    continue

            # Priority 2: TP SECOND
            if side == "buy":
                if h >= tp_px:
                    fill = max(o, tp_px) if o >= tp_px else tp_px
                    _close_all(fill, i, "tp")
                    continue
            else:
                if low + spread_price <= tp_px:
                    fill = min(o + spread_price, tp_px) if (o + spread_price) <= tp_px else tp_px
                    _close_all(fill, i, "tp")
                    continue

            # Priority 3: Bar-close Winner-Only Pyramid check + BE / Profit SL lock
            ent1 = legs[0]
            cur_gain = (c - ent1) if side == "buy" else (ent1 - (c + spread_price))
            k = len(legs)
            if init_risk > 0 and cur_gain >= k * cfg.pyr_step_r * init_risk:
                # Move SL to breakeven on 1st milestone, then lock profit on subsequent milestones
                lock_mult = 0.05 if k == 1 else (0.45 * (k - 1))
                if side == "buy":
                    sl_px = max(sl_px, ent1 + lock_mult * init_risk)
                else:
                    sl_px = min(sl_px, ent1 - lock_mult * init_risk)
                trailed = True
                if k < cfg.max_legs:
                    pending_add = True

            # Priority 4: Monotonic trailing stop once trailed
            if trailed and cfg.trail_atr > 0 and atr14[i] > 0:
                if side == "buy":
                    sl_px = max(sl_px, c - cfg.trail_atr * atr14[i])
                else:
                    sl_px = min(sl_px, (c + spread_price) + cfg.trail_atr * atr14[i])

            # Priority 5: Hard time-stop
            if bars_held >= cfg.max_hold_bars:
                exit_px = c if side == "buy" else (c + spread_price)
                _close_all(exit_px, i, "time_stop")
            continue

        # Flat: check causal signal on closed bar i -> enter at bar i+1 open
        if i < cooldown_until:
            continue
        sig = _detect_accel_signal(i, bars, ema9, ema21, ema50, atr14, atr_base, cfg)
        if sig is None:
            continue

        atr = atr14[i]
        nxt_open = bars[i + 1][1]
        sl_dist = max(spread_price * 3.0, cfg.sl_atr * atr)
        tp_dist = max(spread_price * 4.0, cfg.tp_atr * atr)
        init_risk = sl_dist

        if sig == "buy":
            ent = nxt_open + spread_price
            sl_px = ent - sl_dist
            tp_px = ent + tp_dist
        else:
            ent = nxt_open
            sl_px = ent + sl_dist
            tp_px = ent - tp_dist

        side = sig
        legs = [ent]
        entry_idx = i + 1
        trailed = False
        pending_add = False

    if side is not None and legs:
        last_c = bars[-1][4]
        _close_all(last_c if side == "buy" else (last_c + spread_price), n - 1, "eof")

    return {
        "pnls": pnls,
        "baskets": len(pnls),
        "total_legs_executed": sum(leg_counts),
        "avg_legs": round(statistics.mean(leg_counts), 2) if leg_counts else 0.0,
        "max_lots": round(cfg.max_legs * cfg.leg_volume, 2),
        "exit_reasons": exit_reasons,
        "trades_meta": trades_meta,
    }


def default_m1_accel_configs() -> list[AccelPyramidConfig]:
    return [
        AccelPyramidConfig(
            name="M1_PRE15_PB5_PYRAMID3",
            mode="pre_pb_rej", max_legs=3, pyr_step_r=0.85, sl_atr=1.5, tp_atr=3.5, trail_atr=1.2,
            max_hold_bars=30, exp_atr_mult=1.5, body_ratio_min=0.50, trend_lookback=15, pb_lookback=5,
            trend_min_atr=1.5, pb_min_atr=0.6, min_atr_price=0.60, session_hours=(7, 20),
            win_cooldown_bars=0, loss_cooldown_bars=2,
        ),
        AccelPyramidConfig(
            name="M1_PRE15_RELAXED_PYR3",
            mode="pre_pb_rej", max_legs=3, pyr_step_r=0.85, sl_atr=1.5, tp_atr=3.5, trail_atr=1.2,
            max_hold_bars=30, exp_atr_mult=1.1, body_ratio_min=0.45, trend_lookback=12, pb_lookback=4,
            trend_min_atr=1.0, pb_min_atr=0.3, min_atr_price=0.80, session_hours=(7, 20),
            win_cooldown_bars=0, loss_cooldown_bars=1,
        ),
        AccelPyramidConfig(
            name="M1_VOL_EXPL_CONT_PYR3",
            mode="vol_cont", max_legs=3, pyr_step_r=0.80, sl_atr=1.4, tp_atr=3.5, trail_atr=1.1,
            max_hold_bars=25, exp_atr_mult=1.1, body_ratio_min=0.55, vol_ratio_min=1.50,
            min_atr_price=1.80, session_hours=(7, 20), win_cooldown_bars=0, loss_cooldown_bars=2,
        ),
        AccelPyramidConfig(
            name="M1_VOL_PB_BURST_PYR3",
            mode="vol_pb_burst", max_legs=3, pyr_step_r=0.80, sl_atr=1.4, tp_atr=3.2, trail_atr=1.1,
            max_hold_bars=25, exp_atr_mult=1.0, body_ratio_min=0.50, vol_ratio_min=1.35,
            min_atr_price=1.50, session_hours=(7, 20), win_cooldown_bars=0, loss_cooldown_bars=1,
        ),
        AccelPyramidConfig(
            name="M1_NEWS_SHOCK_PYR3",
            mode="shock_break", max_legs=3, pyr_step_r=0.80, sl_atr=1.4, tp_atr=3.5, trail_atr=1.1,
            max_hold_bars=25, exp_atr_mult=1.1, body_ratio_min=0.55, vol_ratio_min=1.40,
            min_atr_price=2.80, session_hours=(7, 21), win_cooldown_bars=0, loss_cooldown_bars=1,
        ),
        AccelPyramidConfig(
            name="M1_VOL_PB_BURST_1LEG",
            mode="vol_pb_burst", max_legs=1, pyr_step_r=0.90, sl_atr=1.4, tp_atr=2.8, trail_atr=1.1,
            max_hold_bars=25, exp_atr_mult=1.0, body_ratio_min=0.50, vol_ratio_min=1.35,
            min_atr_price=1.50, session_hours=(7, 20), win_cooldown_bars=0, loss_cooldown_bars=1,
        ),
        AccelPyramidConfig(
            name="M1_VOL_EXPL_FADE_CTRL",
            mode="vol_fade_ctrl", max_legs=1, pyr_step_r=1.00, sl_atr=1.5, tp_atr=2.2, trail_atr=1.0,
            max_hold_bars=20, exp_atr_mult=1.5, body_ratio_min=0.55, vol_ratio_min=1.50,
            min_atr_price=1.80, session_hours=(7, 20), win_cooldown_bars=0, loss_cooldown_bars=2,
        ),
    ]


def default_m5_accel_configs() -> list[AccelPyramidConfig]:
    return [
        AccelPyramidConfig(
            name="M5_PRE15_PB5_PYRAMID3",
            mode="pre_pb_rej", max_legs=3, pyr_step_r=0.85, sl_atr=1.5, tp_atr=3.5, trail_atr=1.2,
            max_hold_bars=24, exp_atr_mult=1.4, body_ratio_min=0.50, trend_lookback=12, pb_lookback=3,
            trend_min_atr=1.5, pb_min_atr=0.6, min_atr_price=1.50, session_hours=(7, 20),
            win_cooldown_bars=0, loss_cooldown_bars=2,
        ),
        AccelPyramidConfig(
            name="M5_PRE15_RELAXED_PYR3",
            mode="pre_pb_rej", max_legs=3, pyr_step_r=0.85, sl_atr=1.5, tp_atr=3.5, trail_atr=1.2,
            max_hold_bars=24, exp_atr_mult=1.1, body_ratio_min=0.45, trend_lookback=10, pb_lookback=3,
            trend_min_atr=1.0, pb_min_atr=0.3, min_atr_price=2.00, session_hours=(7, 20),
            win_cooldown_bars=0, loss_cooldown_bars=1,
        ),
        AccelPyramidConfig(
            name="M5_VOL_EXPL_CONT_PYR3",
            mode="vol_cont", max_legs=3, pyr_step_r=0.80, sl_atr=1.4, tp_atr=3.5, trail_atr=1.1,
            max_hold_bars=20, exp_atr_mult=1.1, body_ratio_min=0.55, vol_ratio_min=1.50,
            min_atr_price=3.50, session_hours=(7, 20), win_cooldown_bars=0, loss_cooldown_bars=2,
        ),
        AccelPyramidConfig(
            name="M5_VOL_PB_BURST_PYR3",
            mode="vol_pb_burst", max_legs=3, pyr_step_r=0.80, sl_atr=1.4, tp_atr=3.2, trail_atr=1.1,
            max_hold_bars=20, exp_atr_mult=1.0, body_ratio_min=0.50, vol_ratio_min=1.35,
            min_atr_price=3.00, session_hours=(7, 20), win_cooldown_bars=0, loss_cooldown_bars=1,
        ),
        AccelPyramidConfig(
            name="M5_NEWS_SHOCK_PYR3",
            mode="shock_break", max_legs=3, pyr_step_r=0.80, sl_atr=1.4, tp_atr=3.5, trail_atr=1.1,
            max_hold_bars=20, exp_atr_mult=1.1, body_ratio_min=0.55, vol_ratio_min=1.40,
            min_atr_price=5.50, session_hours=(7, 21), win_cooldown_bars=0, loss_cooldown_bars=1,
        ),
        AccelPyramidConfig(
            name="M5_VOL_PB_BURST_1LEG",
            mode="vol_pb_burst", max_legs=1, pyr_step_r=0.90, sl_atr=1.4, tp_atr=2.8, trail_atr=1.1,
            max_hold_bars=20, exp_atr_mult=1.0, body_ratio_min=0.50, vol_ratio_min=1.35,
            min_atr_price=3.00, session_hours=(7, 20), win_cooldown_bars=0, loss_cooldown_bars=1,
        ),
        AccelPyramidConfig(
            name="M5_VOL_EXPL_FADE_CTRL",
            mode="vol_fade_ctrl", max_legs=1, pyr_step_r=1.00, sl_atr=1.5, tp_atr=2.2, trail_atr=1.0,
            max_hold_bars=18, exp_atr_mult=1.5, body_ratio_min=0.55, vol_ratio_min=1.50,
            min_atr_price=3.50, session_hours=(7, 20), win_cooldown_bars=0, loss_cooldown_bars=2,
        ),
    ]


def evaluate_accel_pyramid_suite(
    *,
    tf_label: str,
    bars: list[list[float]],
    spec: dict[str, Any],
    spread_price: float,
    mc_trials: int = 400,
) -> dict[str, Any]:
    """Evaluate all Acceleration + Winner-Only Pyramid engines on one timeframe."""
    cleaned = clean_ohlcv_bars(bars)
    if len(cleaned) < 150:
        return {"timeframe": tf_label, "bars": len(cleaned), "error": "insufficient_bars", "engines": []}

    basis_name, mult = resolve_pricing_basis(spec, "auto")
    span_days = round((cleaned[-1][0] - cleaned[0][0]) / 86400.0, 2)
    weeks = max(span_days / 7.0, 0.1)
    mid_idx = len(cleaned) // 2
    configs = default_m1_accel_configs() if tf_label.upper() == "M1" else default_m5_accel_configs()

    results: list[dict[str, Any]] = []
    for cfg in configs:
        base_run = simulate_accel_pyramid(cleaned, cfg, spread_price=spread_price, multiplier=mult)
        stress_run = simulate_accel_pyramid(cleaned, cfg, spread_price=spread_price * 1.5, multiplier=mult)

        base_sum = _summarize_pnls(base_run["pnls"])
        stress_sum = _summarize_pnls(stress_run["pnls"])

        h1_pnls = [m["pnl"] for m in base_run["trades_meta"] if m["entry_idx"] < mid_idx]
        h2_pnls = [m["pnl"] for m in base_run["trades_meta"] if m["entry_idx"] >= mid_idx]
        h1_net = round(sum(h1_pnls), 2)
        h2_net = round(sum(h2_pnls), 2)
        mc_p95 = _mc_p95_dd(base_run["pnls"], trials=mc_trials)

        passed = bool(
            base_sum["trades"] >= 25
            and base_sum["net"] > 0
            and (base_sum["pf"] or 0.0) >= 1.25
            and base_sum["excl_best"] > 0
            and h1_net > 0
            and h2_net > 0
            and stress_sum["net"] > 0
        )

        results.append({
            "engine": cfg.name,
            "mode": cfg.mode,
            "max_legs": cfg.max_legs,
            "max_lots": base_run["max_lots"],
            "total_legs": base_run["total_legs_executed"],
            "avg_legs": base_run["avg_legs"],
            "trades_per_week": round(base_sum["trades"] / weeks, 1),
            "legs_per_week": round(base_run["total_legs_executed"] / weeks, 1),
            "base": base_sum,
            "h1_net": h1_net,
            "h2_net": h2_net,
            "stress_net": stress_sum["net"],
            "stress_pf": stress_sum["pf"],
            "mc_p95_dd": mc_p95,
            "exit_reasons": base_run["exit_reasons"],
            "passed": passed,
        })

    results.sort(key=lambda r: (r["passed"], r["base"]["p_dd"] if r["base"]["p_dd"] is not None else -999.0, r["base"]["net"]), reverse=True)
    return {
        "timeframe": tf_label,
        "bars": len(cleaned),
        "span_days": span_days,
        "pricing_basis": basis_name,
        "multiplier": mult,
        "spread_price": round(spread_price, 4),
        "engines": results,
    }


def make_synthetic_accel_bars(n_bars: int = 900, bar_seconds: int = 60) -> list[list[float]]:
    """Deterministic synthetic series: quiet chop followed by London/NY volatility explosions."""
    # Start at 2025-11-25T07:00:00Z so bars stay inside London+NY session
    t0 = 1764054000.0
    bars: list[list[float]] = []
    px = 3500.0
    for i in range(n_bars):
        ts = t0 + i * bar_seconds
        cyc = i % 150
        if cyc < 100:
            # Quiet choppy baseline (low ATR ~0.9)
            chg = +0.4 if i % 2 == 0 else -0.4
            wick = 0.25
        elif cyc < 112:
            # Clean downtrend buildup
            chg = -1.3
            wick = 0.4
        elif cyc < 116:
            # 4-bar pullback up
            chg = +1.4
            wick = 0.4
        elif cyc < 130:
            # High-vol acceleration crash (triggers vol explosion + winner-only 3-leg pyramid!)
            chg = -3.2
            wick = 0.5
        else:
            # Mean revert back to baseline
            chg = +2.5
            wick = 0.5
        o = px
        c = px + chg
        h = max(o, c) + wick
        low = min(o, c) - wick
        bars.append([ts, round(o, 2), round(h, 2), round(low, 2), round(c, 2), 250.0])
        px = c
    return bars
