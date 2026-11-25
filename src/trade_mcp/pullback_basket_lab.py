"""Strictly causal Pullback-Rejection & Bounded Scale-In Basket Lab (M1 & M5).

Directly tests the automatable, hard-stop-protected core of directional
pullback scale-in scalping observed in manual MT5 audit data:
  - Explosive expansion bar aligned with short/medium trend
  - 15-bar trend + 5-bar counter-pullback + trend-aligned rejection bar
  - Bounded 3-leg pullback scale-in basket (max 0.03 lot total, NEVER uncapped)
  - Hard basket stop-loss & hard time-stop (NEVER holds underwater 14 hours)
  - Quick basket TP (high win-rate scalp) vs asymmetric runner control
  - Bounded mean-reversion fade basket control

Zero lookahead:
  - Signals evaluate strictly on closed bar i (using bars 0..i).
  - Leg 1 enters at bar i+1 open + conservative spread.
  - On every subsequent bar j:
      1) Hard basket SL is checked FIRST (worst-case intrabar priority, gap-aware).
      2) Basket TP is checked SECOND (against already-open legs only).
      3) Bounded scale-in adds (up to max_legs) are checked THIRD and NEVER
         allowed to take profit on the exact bar they were added.
      4) Hard time-stop closes all open legs at bar close.

Strictly read-only research module (ORDERS=0).
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from trade_mcp.daytrade_paper import resolve_pricing_basis
from trade_mcp.m1_momentum import atr_series, clean_ohlcv_bars, ema_series
from trade_mcp.waterfall_momentum_lab import _efficiency_ratio


@dataclass(frozen=True)
class BasketEngineConfig:
    name: str
    mode: str
    # Modes:
    # - "exp_bar": trend-aligned explosive expansion bar (range >= exp_atr_mult * ATR)
    # - "pre_pb_rej": medium trend (trend_lookback) + short counter-pullback (pb_lookback) + rejection bar
    # - "trend_pb_bar": clean EMA9/21/50 trend + counter-color pullback bar
    # - "fade_ext": extended straight move fade control (bounded hard SL)
    max_legs: int = 1
    leg_volume: float = 0.01
    add_step_atr: float = 0.5
    scale_window_bars: int = 6
    tp_atr: float = 0.8
    sl_atr: float = 2.0
    max_hold_bars: int = 15
    exp_atr_mult: float = 2.0
    body_ratio_min: float = 0.50
    trend_lookback: int = 15
    pb_lookback: int = 5
    trend_min_atr: float = 1.5
    pb_min_atr: float = 0.6
    er_min: float = 0.45
    session_hours: tuple[int, int] | None = None
    trail_at_r: float = 0.0  # >0 enables runner breakeven/trail (1-leg mode)
    cooldown_bars: int = 2
    min_atr_price: float = 0.50


def _bar_hour_utc(ts: float) -> int:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).hour


def _in_session(ts: float, hours: tuple[int, int] | None) -> bool:
    if hours is None:
        return True
    h = _bar_hour_utc(ts)
    lo, hi = hours
    return lo <= h < hi


def _max_dd(pnls: list[float]) -> float:
    eq = 0.0
    peak = 0.0
    dd = 0.0
    for p in pnls:
        eq += p
        if eq > peak:
            peak = eq
        if peak - eq > dd:
            dd = peak - eq
    return dd


def _mc_p95_dd(pnls: list[float], trials: int, seed: int = 42) -> float:
    if len(pnls) < 2 or trials <= 0:
        return round(_max_dd(pnls), 2)
    rng = random.Random(seed)
    buf = list(pnls)
    dds: list[float] = []
    for _ in range(trials):
        rng.shuffle(buf)
        dds.append(_max_dd(buf))
    dds.sort()
    idx = min(len(dds) - 1, int(0.95 * len(dds)))
    return round(dds[idx], 2)


def _summarize_pnls(pnls: list[float]) -> dict[str, Any]:
    n = len(pnls)
    if n == 0:
        return {
            "trades": 0, "wins": 0, "losses": 0, "wr": 0.0,
            "net": 0.0, "pf": None, "max_dd": 0.0, "p_dd": None,
            "excl_best": 0.0, "avg_win": None, "avg_loss": None, "payoff": None,
        }
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp = sum(wins)
    gl = abs(sum(losses))
    net = round(sum(pnls), 2)
    dd = round(_max_dd(pnls), 2)
    pf = round(gp / gl, 3) if gl > 0 else (99.0 if gp > 0 else None)
    p_dd = round(net / dd, 2) if dd > 0 else None
    best = max(pnls)
    excl_best = round(net - max(0.0, best), 2)
    avg_w = round(gp / len(wins), 2) if wins else None
    avg_l = round(gl / len(losses), 2) if losses else None
    payoff = round(avg_w / avg_l, 2) if (avg_w is not None and avg_l and avg_l > 0) else None
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "wr": round(100.0 * len(wins) / n, 2),
        "net": net,
        "pf": pf,
        "max_dd": dd,
        "p_dd": p_dd,
        "excl_best": excl_best,
        "avg_win": avg_w,
        "avg_loss": avg_l,
        "payoff": payoff,
    }


def _detect_signal(
    i: int,
    bars: list[list[float]],
    ema9: list[float],
    ema21: list[float],
    ema50: list[float],
    atr14: list[float],
    cfg: BasketEngineConfig,
) -> str | None:
    """Strictly causal signal evaluation on closed bar i (uses only <= i)."""
    atr = atr14[i]
    if atr < cfg.min_atr_price or atr <= 0:
        return None
    if not _in_session(bars[i][0], cfg.session_hours):
        return None

    o, h, low, c = bars[i][1], bars[i][2], bars[i][3], bars[i][4]
    rng = h - low
    if rng <= 0:
        return None
    body = abs(c - o)
    body_ratio = body / rng

    if cfg.mode == "exp_bar":
        if rng < cfg.exp_atr_mult * atr or body_ratio < cfg.body_ratio_min:
            return None
        if c > o and ema9[i] > ema21[i] > ema50[i]:
            return "buy"
        if c < o and ema9[i] < ema21[i] < ema50[i]:
            return "sell"
        return None

    if cfg.mode == "pre_pb_rej":
        lb = cfg.trend_lookback
        pb = cfg.pb_lookback
        if i < lb + 2 or pb >= lb:
            return None
        # Trend move over [i-lb .. i-pb] (before the counter pullback)
        trend_move = bars[i - pb][4] - bars[i - lb][1]
        # Counter pullback over [i-pb .. i-1] (strictly before trigger bar i)
        pb_move = bars[i - 1][4] - bars[i - pb][4]
        if rng < cfg.exp_atr_mult * atr or body_ratio < cfg.body_ratio_min:
            return None
        # Bullish continuation: medium UP, short pullback DOWN, trigger bar slams UP
        if (
            trend_move >= cfg.trend_min_atr * atr
            and pb_move <= -cfg.pb_min_atr * atr
            and ema21[i] >= ema50[i]
            and c > o
            and c > bars[i - 1][2]
        ):
            return "buy"
        # Bearish continuation: medium DOWN, short pullback UP, trigger bar slams DOWN
        if (
            trend_move <= -cfg.trend_min_atr * atr
            and pb_move >= cfg.pb_min_atr * atr
            and ema21[i] <= ema50[i]
            and c < o
            and c < bars[i - 1][3]
        ):
            return "sell"
        return None

    if cfg.mode == "trend_pb_bar":
        if i < 12:
            return None
        er, _, _ = _efficiency_ratio(bars, i - 1, 10)
        if er < cfg.er_min:
            return None
        # Uptrend + bearish pullback bar near EMA9/21
        if ema9[i] > ema21[i] > ema50[i] and c < o and body >= 0.35 * atr and low <= ema9[i] and c >= ema50[i]:
            return "buy"
        # Downtrend + bullish pullback bar near EMA9/21
        if ema9[i] < ema21[i] < ema50[i] and c > o and body >= 0.35 * atr and h >= ema9[i] and c <= ema50[i]:
            return "sell"
        return None

    if cfg.mode == "fade_ext":
        lb = cfg.trend_lookback
        if i < lb + 2:
            return None
        er, _, _ = _efficiency_ratio(bars, i, lb)
        net_move = bars[i][4] - bars[i - lb][4]
        if er < cfg.er_min or abs(net_move) < cfg.trend_min_atr * atr:
            return None
        # Fade extended straight move (bounded by hard basket SL)
        if net_move > 0:
            return "sell"
        if net_move < 0:
            return "buy"
        return None

    return None


def simulate_basket_engine(
    bars: list[list[float]],
    cfg: BasketEngineConfig,
    *,
    spread_price: float,
    multiplier: float,
) -> dict[str, Any]:
    """Run strictly causal bar-by-bar simulation for 1-leg or bounded multi-leg basket."""
    n = len(bars)
    if n < 80:
        return {"pnls": [], "baskets": 0, "avg_legs": 0.0, "max_lots": 0.0, "exit_reasons": {}, "trades_meta": []}

    closes = [b[4] for b in bars]
    ema9 = [float(v) if v is not None else 0.0 for v in ema_series(closes, 9)]
    ema21 = [float(v) if v is not None else 0.0 for v in ema_series(closes, 21)]
    ema50 = [float(v) if v is not None else 0.0 for v in ema_series(closes, 50)]
    atr14 = [float(v) if v is not None else 0.0 for v in atr_series(bars, period=14)]

    pnls: list[float] = []
    trades_meta: list[dict[str, Any]] = []
    leg_counts: list[int] = []
    exit_reasons: dict[str, int] = {}

    side: str | None = None
    legs: list[float] = []  # entry fill prices (already spread-adjusted for BUY)
    leg1_raw_open = 0.0
    sl_px = 0.0
    tp_move = 0.0
    add_step = 0.0
    init_risk_dist = 0.0
    entry_idx = -1
    trailed = False
    cooldown_until = 55

    def _close_basket(exit_bid_or_ask: float, close_idx: int, reason: str) -> None:
        nonlocal side, legs, trailed, cooldown_until
        assert side is not None and legs
        total_pnl = 0.0
        for ent in legs:
            diff = (exit_bid_or_ask - ent) if side == "buy" else (ent - exit_bid_or_ask)
            total_pnl += diff * cfg.leg_volume * multiplier
        pnl_r = round(total_pnl, 4)
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
        cooldown_until = close_idx + cfg.cooldown_bars

    for i in range(55, n - 1):
        bar = bars[i]
        o, h, low, c = bar[1], bar[2], bar[3], bar[4]

        # 1. Manage open basket on bar i (which is >= entry_idx)
        if side is not None:
            bars_held = i - entry_idx

            # Priority 1: Hard basket SL (worst-case intrabar priority, gap-aware)
            if side == "buy":
                if low <= sl_px:
                    fill = min(o, sl_px)
                    _close_basket(fill, i, "sl_trail" if trailed else "sl")
                    continue
            else:
                # SELL closes at ask = bid + spread; SL triggered when bar.high + spread >= sl_px
                if h + spread_price >= sl_px:
                    fill = max(o + spread_price, sl_px)
                    _close_basket(fill, i, "sl_trail" if trailed else "sl")
                    continue

            # Priority 2: Basket TP against currently open legs
            avg_ent = sum(legs) / len(legs)
            if side == "buy":
                target_bid = avg_ent + tp_move
                if h >= target_bid:
                    fill = max(o, target_bid) if o >= target_bid else target_bid
                    _close_basket(fill, i, "tp")
                    continue
            else:
                target_ask = avg_ent - tp_move
                # Need low + spread <= target_ask -> low <= target_ask - spread
                if low + spread_price <= target_ask:
                    fill = min(o + spread_price, target_ask) if (o + spread_price) <= target_ask else target_ask
                    _close_basket(fill, i, "tp")
                    continue

            # Optional runner trail (1-leg mode): move SL to BE+0.2R once bar closes >= trail_at_r
            if cfg.trail_at_r > 0 and len(legs) == 1 and not trailed and init_risk_dist > 0:
                if side == "buy" and (c - avg_ent) >= cfg.trail_at_r * init_risk_dist:
                    sl_px = max(sl_px, avg_ent + 0.15 * init_risk_dist)
                    trailed = True
                elif side == "sell" and (avg_ent - (c + spread_price)) >= cfg.trail_at_r * init_risk_dist:
                    sl_px = min(sl_px, avg_ent - 0.15 * init_risk_dist)
                    trailed = True

            # Priority 3: Bounded pullback scale-in (cannot TP on same bar added)
            if len(legs) < cfg.max_legs and bars_held < cfg.scale_window_bars:
                while len(legs) < cfg.max_legs:
                    k = len(legs)
                    if side == "buy":
                        trig = leg1_raw_open - k * add_step
                        if trig > sl_px and low <= trig:
                            add_fill = min(o, trig) + spread_price
                            legs.append(add_fill)
                        else:
                            break
                    else:
                        trig = leg1_raw_open + k * add_step
                        if trig + spread_price < sl_px and h >= trig:
                            add_fill = max(o, trig)
                            legs.append(add_fill)
                        else:
                            break

            # Priority 4: Hard time-stop (NEVER hold underwater indefinitely)
            if bars_held >= cfg.max_hold_bars:
                exit_px = c if side == "buy" else (c + spread_price)
                _close_basket(exit_px, i, "time_stop")
            continue

        # 2. Flat: evaluate strictly causal signal on closed bar i -> enter at bar i+1 open
        if i < cooldown_until:
            continue
        sig = _detect_signal(i, bars, ema9, ema21, ema50, atr14, cfg)
        if sig is None:
            continue

        atr = atr14[i]
        nxt_open = bars[i + 1][1]
        leg1_raw_open = nxt_open
        tp_move = max(spread_price * 1.5, cfg.tp_atr * atr)
        add_step = max(spread_price * 1.2, cfg.add_step_atr * atr)
        sl_dist = max(spread_price * 3.0, cfg.sl_atr * atr)
        init_risk_dist = sl_dist

        if sig == "buy":
            ent = nxt_open + spread_price
            sl_px = ent - sl_dist
        else:
            ent = nxt_open
            sl_px = ent + sl_dist
        side = sig
        legs = [ent]
        entry_idx = i + 1
        trailed = False

    # Close any open basket at last bar close
    if side is not None and legs:
        last_c = bars[-1][4]
        _close_basket(last_c if side == "buy" else (last_c + spread_price), n - 1, "eof")

    return {
        "pnls": pnls,
        "baskets": len(pnls),
        "avg_legs": round(statistics.mean(leg_counts), 2) if leg_counts else 0.0,
        "max_lots": round(cfg.max_legs * cfg.leg_volume, 2),
        "exit_reasons": exit_reasons,
        "trades_meta": trades_meta,
    }


def default_m1_basket_configs() -> list[BasketEngineConfig]:
    return [
        BasketEngineConfig(
            name="M1_EXP_BAR_QUICK_1LEG",
            mode="exp_bar", max_legs=1, tp_atr=0.8, sl_atr=1.8, max_hold_bars=12,
            exp_atr_mult=2.0, body_ratio_min=0.55, min_atr_price=0.60,
        ),
        BasketEngineConfig(
            name="M1_PRE15_PB5_REJ_1LEG",
            mode="pre_pb_rej", max_legs=1, tp_atr=0.9, sl_atr=1.8, max_hold_bars=15,
            exp_atr_mult=1.5, body_ratio_min=0.50, trend_lookback=15, pb_lookback=5,
            trend_min_atr=1.5, pb_min_atr=0.6, min_atr_price=0.60,
        ),
        BasketEngineConfig(
            name="M1_PRE15_PB5_BASKET_3LEG",
            mode="pre_pb_rej", max_legs=3, add_step_atr=0.5, scale_window_bars=6,
            tp_atr=0.6, sl_atr=2.2, max_hold_bars=15,
            exp_atr_mult=1.5, body_ratio_min=0.50, trend_lookback=15, pb_lookback=5,
            trend_min_atr=1.5, pb_min_atr=0.6, min_atr_price=0.60,
        ),
        BasketEngineConfig(
            name="M1_EXP_BAR_BASKET_3LEG",
            mode="exp_bar", max_legs=3, add_step_atr=0.5, scale_window_bars=6,
            tp_atr=0.55, sl_atr=2.2, max_hold_bars=12,
            exp_atr_mult=2.2, body_ratio_min=0.55, min_atr_price=0.60,
        ),
        BasketEngineConfig(
            name="M1_TREND_PB_BASKET_3LEG",
            mode="trend_pb_bar", max_legs=3, add_step_atr=0.5, scale_window_bars=6,
            tp_atr=0.6, sl_atr=2.2, max_hold_bars=15, er_min=0.45, min_atr_price=0.60,
        ),
        BasketEngineConfig(
            name="M1_FADE_EXT_BASKET_3LEG",
            mode="fade_ext", max_legs=3, add_step_atr=0.5, scale_window_bars=6,
            tp_atr=0.6, sl_atr=2.4, max_hold_bars=15,
            trend_lookback=10, trend_min_atr=3.0, er_min=0.60, min_atr_price=0.60,
        ),
        BasketEngineConfig(
            name="M1_PRE15_PB5_RUNNER_LDNNY",
            mode="pre_pb_rej", max_legs=1, tp_atr=2.5, sl_atr=1.5, max_hold_bars=25,
            exp_atr_mult=1.5, body_ratio_min=0.50, trend_lookback=15, pb_lookback=5,
            trend_min_atr=1.5, pb_min_atr=0.6, session_hours=(7, 20), trail_at_r=1.0,
            min_atr_price=0.60,
        ),
    ]


def default_m5_basket_configs() -> list[BasketEngineConfig]:
    return [
        BasketEngineConfig(
            name="M5_EXP_BAR_QUICK_1LEG",
            mode="exp_bar", max_legs=1, tp_atr=0.8, sl_atr=1.8, max_hold_bars=10,
            exp_atr_mult=2.0, body_ratio_min=0.55, min_atr_price=1.50,
        ),
        BasketEngineConfig(
            name="M5_PRE15_PB5_REJ_1LEG",
            mode="pre_pb_rej", max_legs=1, tp_atr=0.9, sl_atr=1.8, max_hold_bars=12,
            exp_atr_mult=1.4, body_ratio_min=0.50, trend_lookback=12, pb_lookback=3,
            trend_min_atr=1.5, pb_min_atr=0.6, min_atr_price=1.50,
        ),
        BasketEngineConfig(
            name="M5_PRE15_PB5_BASKET_3LEG",
            mode="pre_pb_rej", max_legs=3, add_step_atr=0.5, scale_window_bars=5,
            tp_atr=0.6, sl_atr=2.2, max_hold_bars=12,
            exp_atr_mult=1.4, body_ratio_min=0.50, trend_lookback=12, pb_lookback=3,
            trend_min_atr=1.5, pb_min_atr=0.6, min_atr_price=1.50,
        ),
        BasketEngineConfig(
            name="M5_EXP_BAR_BASKET_3LEG",
            mode="exp_bar", max_legs=3, add_step_atr=0.5, scale_window_bars=5,
            tp_atr=0.55, sl_atr=2.2, max_hold_bars=10,
            exp_atr_mult=2.2, body_ratio_min=0.55, min_atr_price=1.50,
        ),
        BasketEngineConfig(
            name="M5_TREND_PB_BASKET_3LEG",
            mode="trend_pb_bar", max_legs=3, add_step_atr=0.5, scale_window_bars=5,
            tp_atr=0.6, sl_atr=2.2, max_hold_bars=12, er_min=0.45, min_atr_price=1.50,
        ),
        BasketEngineConfig(
            name="M5_FADE_EXT_BASKET_3LEG",
            mode="fade_ext", max_legs=3, add_step_atr=0.5, scale_window_bars=5,
            tp_atr=0.6, sl_atr=2.4, max_hold_bars=12,
            trend_lookback=8, trend_min_atr=3.0, er_min=0.60, min_atr_price=1.50,
        ),
        BasketEngineConfig(
            name="M5_PRE15_PB5_RUNNER_LDNNY",
            mode="pre_pb_rej", max_legs=1, tp_atr=2.5, sl_atr=1.5, max_hold_bars=20,
            exp_atr_mult=1.4, body_ratio_min=0.50, trend_lookback=12, pb_lookback=3,
            trend_min_atr=1.5, pb_min_atr=0.6, session_hours=(7, 20), trail_at_r=1.0,
            min_atr_price=1.50,
        ),
    ]


def evaluate_pullback_basket_suite(
    *,
    tf_label: str,
    bars: list[list[float]],
    spec: dict[str, Any],
    spread_price: float,
    mc_trials: int = 400,
) -> dict[str, Any]:
    """Evaluate all Pullback/Basket engines on one TF with H1/H2, exclBest, 1.5x stress, MC95 DD."""
    cleaned = clean_ohlcv_bars(bars)
    if len(cleaned) < 120:
        return {"timeframe": tf_label, "bars": len(cleaned), "error": "insufficient_bars", "engines": []}

    basis_name, mult = resolve_pricing_basis(spec, "auto")
    span_days = round((cleaned[-1][0] - cleaned[0][0]) / 86400.0, 2)
    weeks = max(span_days / 7.0, 0.1)
    mid_idx = len(cleaned) // 2
    configs = default_m1_basket_configs() if tf_label.upper() == "M1" else default_m5_basket_configs()

    results: list[dict[str, Any]] = []
    for cfg in configs:
        base_run = simulate_basket_engine(cleaned, cfg, spread_price=spread_price, multiplier=mult)
        stress_run = simulate_basket_engine(cleaned, cfg, spread_price=spread_price * 1.5, multiplier=mult)

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
            "avg_legs": base_run["avg_legs"],
            "trades_per_week": round(base_sum["trades"] / weeks, 1),
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


def make_synthetic_pullback_bars(n_bars: int = 800, bar_seconds: int = 60) -> list[list[float]]:
    """Deterministic synthetic series containing clear trend -> 5-bar pullback -> rejection waves."""
    t0 = 1764028800.0
    bars: list[list[float]] = []
    px = 3500.0
    for i in range(n_bars):
        ts = t0 + i * bar_seconds
        cyc = i % 40
        if cyc < 15:
            # Steady downtrend
            chg = -1.2
            wick = 0.6
        elif cyc < 20:
            # 5-bar sharp counter-pullback UP
            chg = +1.4
            wick = 0.6
        elif cyc == 20:
            # Explosive bearish rejection bar
            chg = -4.2
            wick = 0.8
        elif cyc == 21:
            # Brief adverse bounce (+1.5) to trigger pullback scale-in leg 2
            chg = +1.5
            wick = 0.5
        elif cyc < 28:
            # Follow-through wave down to hit basket TP cleanly
            chg = -1.1
            wick = 0.5
        else:
            # Recovery back up
            chg = +1.4
            wick = 0.5
        o = px
        c = px + chg
        h = max(o, c) + wick
        low = min(o, c) - wick
        bars.append([ts, round(o, 2), round(h, 2), round(low, 2), round(c, 2), 200.0])
        px = c
    return bars
