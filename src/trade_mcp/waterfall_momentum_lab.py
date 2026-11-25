"""Fast Waterfall / Cascade Trend-Acceleration Lab (M1 45d & M5 517d).

Tests fast, zero-H4-lag, uncapped-ADX directional cascade engines directly modeling
unbroken 5-to-30-minute waterfall drops & vertical surges (high Kaufman Efficiency Ratio,
dominant same-color bar streaks, and accelerating EMA slope), plus an honest Fade control.
"""

from __future__ import annotations

from typing import Any

from .candlestick_lab import _mc_p95_dd, _summarize_trades
from .daytrade_paper import resolve_pricing_basis
from .m1_momentum import (
    LONDON_NY_BLOCKED_UTC_HOURS,
    atr_series,
    clean_ohlcv_bars,
    ema_series,
)


def _efficiency_ratio(bars: list[list[float]], end_idx: int, window: int) -> tuple[float, float, float]:
    """Return (kaufman_er, signed_net_move, same_color_frac) over closed bars [end_idx-window+1 .. end_idx]."""
    start_idx = end_idx - window + 1
    if start_idx < 1 or window < 2:
        return 0.0, 0.0, 0.0
    net = bars[end_idx][4] - bars[start_idx - 1][4]
    path = 0.0
    pos_bars = 0
    neg_bars = 0
    for k in range(start_idx, end_idx + 1):
        step = abs(bars[k][4] - bars[k - 1][4])
        path += step
        if bars[k][4] > bars[k][1]:
            pos_bars += 1
        elif bars[k][4] < bars[k][1]:
            neg_bars += 1
    er = (abs(net) / path) if path > 1e-12 else 0.0
    same_frac = (pos_bars / window) if net > 0 else ((neg_bars / window) if net < 0 else 0.0)
    return er, net, same_frac


def _sim_waterfall_engine(
    bars: list[list[float]],
    *,
    spec: dict[str, Any],
    spread_price: float,
    window: int,
    min_er: float,
    min_move_px: float,
    min_same_color: float,
    breakout_lookback: int,
    stop_atr_mult: float,
    max_stop_usd: float,
    trail_bars: int,
    max_hold_bars: int,
    mode: str = "follow",  # "follow" | "fade" | "ema_accel"
    blocked_utc_hours: frozenset[int] = frozenset(),
    commission_per_lot: float = 0.0,
    lot: float = 0.01,
    cooldown_bars: int = 3,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Strictly causal 1-position simulator for fast M1/M5 waterfall/cascade strategies."""
    _, mult = resolve_pricing_basis(spec, "auto")
    clean = clean_ohlcv_bars(bars)
    if len(clean) < max(window, breakout_lookback, 60) + 5:
        return _summarize_trades([]), []

    closes = [b[4] for b in clean]
    atr_vals = atr_series(clean, period=14)
    ema9 = ema_series(closes, 9)
    ema21 = ema_series(closes, 21)
    ema50 = ema_series(closes, 50)

    comm = commission_per_lot * lot
    max_stop_dist = max(spread_price * 3.0, (max_stop_usd - comm) / max(lot * mult, 1e-9))

    trades: list[dict[str, Any]] = []
    pos: dict[str, Any] | None = None
    last_exit_i = -999
    start_i = max(window + 2, breakout_lookback + 2, 55)

    for i in range(start_i, len(clean)):
        b = clean[i]
        t, o, h, lo, c = b[0], b[1], b[2], b[3], b[4]
        hr = (int(t) // 3600) % 24
        atr1 = atr_vals[i - 1]

        # 1) Entry check on strictly closed bars <= i-1 -> enter at bar i open
        opened_now = False
        if (
            pos is None
            and (i - last_exit_i) >= cooldown_bars
            and atr1 is not None
            and atr1 > 0
            and not (blocked_utc_hours and hr in blocked_utc_hours)
        ):
            sig_side: str | None = None
            if mode == "ema_accel":
                e9, e21, e50 = ema9[i - 1], ema21[i - 1], ema50[i - 1]
                e9_prev = ema9[i - 6]
                e9_prev2 = ema9[i - 11]
                if all(x is not None for x in (e9, e21, e50, e9_prev, e9_prev2)):
                    slope_now = float(e9) - float(e9_prev)
                    slope_old = float(e9_prev) - float(e9_prev2)
                    # 5 consecutive closed bars strictly on trending side of EMA9 + accelerating slope
                    all_below = all(clean[k][4] < float(ema9[k] or 0.0) for k in range(i - 5, i))
                    all_above = all(clean[k][4] > float(ema9[k] or 0.0) for k in range(i - 5, i))
                    if all_below and float(e9) < float(e21) < float(e50) and slope_now < -min_move_px * 0.35 and slope_now < slope_old * 1.25:
                        sig_side = "sell"
                    elif all_above and float(e9) > float(e21) > float(e50) and slope_now > min_move_px * 0.35 and slope_now > slope_old * 1.25:
                        sig_side = "buy"
            else:
                er, net_move, same_frac = _efficiency_ratio(clean, i - 1, window)
                if er >= min_er and abs(net_move) >= min_move_px and same_frac >= min_same_color:
                    raw_dir = "buy" if net_move > 0 else "sell"
                    # Breakout confirmation on strictly prior bars before bar i-1
                    bo_ok = True
                    if breakout_lookback > 0:
                        prev_slice = clean[i - 1 - breakout_lookback : i - 1]
                        if raw_dir == "buy":
                            bo_ok = clean[i - 1][4] >= max(x[2] for x in prev_slice) - 0.1 * atr1
                        else:
                            bo_ok = clean[i - 1][4] <= min(x[3] for x in prev_slice) + 0.1 * atr1
                    if bo_ok:
                        if mode == "fade":
                            sig_side = "sell" if raw_dir == "buy" else "buy"
                        else:
                            sig_side = raw_dir

            if sig_side is not None:
                entry = (o + spread_price) if sig_side == "buy" else o
                sdist = min(max(stop_atr_mult * float(atr1), spread_price * 3.0), max_stop_dist)
                stop_px = (entry - sdist) if sig_side == "buy" else (entry + sdist)
                pos = {
                    "side": sig_side,
                    "entry": entry,
                    "stop": stop_px,
                    "sdist": sdist,
                    "opened_i": i,
                    "entry_ts": t,
                    "trailed": False,
                }
                opened_now = True

        # 2) Intrabar stop check (strictly conservative: SL before trail)
        if pos is not None:
            side = pos["side"]
            if side == "buy":
                open_ex = (pos["entry"] - spread_price) if opened_now else o
                hit_sl = lo <= pos["stop"]
                fill_sl = min(open_ex, pos["stop"])
                ex_close = c
            else:
                open_ex = (pos["entry"] + spread_price) if opened_now else (o + spread_price)
                hit_sl = (h + spread_price) >= pos["stop"]
                fill_sl = max(open_ex, pos["stop"])
                ex_close = c + spread_price

            if hit_sl:
                diff = (fill_sl - pos["entry"]) if side == "buy" else (pos["entry"] - fill_sl)
                trades.append({
                    "entry_ts": pos["entry_ts"],
                    "exit_ts": t,
                    "side": side.upper(),
                    "pnl": round(diff * lot * mult - comm, 4),
                    "reason": "trail_sl" if pos["trailed"] else "sl",
                })
                pos = None
                last_exit_i = i
                continue

            bars_held = i - pos["opened_i"] + 1
            if bars_held >= max_hold_bars:
                diff = (ex_close - pos["entry"]) if side == "buy" else (pos["entry"] - ex_close)
                trades.append({
                    "entry_ts": pos["entry_ts"],
                    "exit_ts": t,
                    "side": side.upper(),
                    "pnl": round(diff * lot * mult - comm, 4),
                    "reason": "max_hold",
                })
                pos = None
                last_exit_i = i
                continue

            # EMA exit for ema_accel mode once price closes back across EMA9
            if mode == "ema_accel" and ema9[i] is not None:
                e9_now = float(ema9[i])
                if (side == "buy" and c < e9_now) or (side == "sell" and c > e9_now):
                    diff = (ex_close - pos["entry"]) if side == "buy" else (pos["entry"] - ex_close)
                    trades.append({
                        "entry_ts": pos["entry_ts"],
                        "exit_ts": t,
                        "side": side.upper(),
                        "pnl": round(diff * lot * mult - comm, 4),
                        "reason": "ema9_cross",
                    })
                    pos = None
                    last_exit_i = i
                    continue

            # Tight N-bar swing / chandelier trail once in profit >= 0.6R
            atr_now = float(atr_vals[i] or atr1 or pos["sdist"] / 2.0)
            cur_profit = (ex_close - pos["entry"]) if side == "buy" else (pos["entry"] - ex_close)
            if cur_profit >= 0.6 * pos["sdist"] and i >= trail_bars:
                buf = 0.25 * atr_now
                recent = clean[i - trail_bars + 1 : i + 1]
                if side == "buy":
                    cand = max(pos["stop"], pos["entry"] + 0.05, min(x[3] for x in recent) - buf)
                    cand = min(cand, c - spread_price)
                    if cand > pos["stop"]:
                        pos["stop"] = cand
                        pos["trailed"] = True
                else:
                    cand = min(pos["stop"], pos["entry"] - 0.05, max(x[2] for x in recent) + spread_price + buf)
                    cand = max(cand, c + spread_price)
                    if cand < pos["stop"]:
                        pos["stop"] = cand
                        pos["trailed"] = True

    return _summarize_trades(trades), trades


def evaluate_waterfall_suite(
    *,
    tf_label: str,
    bars: list[list[float]],
    spec: dict[str, Any],
    spread_price: float,
    mc_trials: int = 400,
) -> dict[str, Any]:
    """Run all fast Waterfall/Cascade momentum specs + Fade control on M1 or M5."""
    clean = clean_ohlcv_bars(bars)
    if len(clean) < 120:
        return {"timeframe": tf_label, "bars": len(clean), "error": "insufficient_bars"}
    split_ts = clean[len(clean) // 2][0]
    span_days = round((clean[-1][0] - clean[0][0]) / 86400.0, 1)

    if tf_label == "M1":
        specs = [
            ("M1_FAST_5B_ER70_TRAIL", dict(window=5, min_er=0.70, min_move_px=5.0, min_same_color=0.80, breakout_lookback=20, stop_atr_mult=2.0, max_stop_usd=6.0, trail_bars=3, max_hold_bars=45, mode="follow")),
            ("M1_WATERFALL_10B_ER60", dict(window=10, min_er=0.60, min_move_px=6.0, min_same_color=0.70, breakout_lookback=30, stop_atr_mult=2.2, max_stop_usd=8.0, trail_bars=4, max_hold_bars=60, mode="follow")),
            ("M1_WATERFALL_15B_ER55", dict(window=15, min_er=0.55, min_move_px=8.0, min_same_color=0.65, breakout_lookback=45, stop_atr_mult=2.5, max_stop_usd=10.0, trail_bars=5, max_hold_bars=90, mode="follow")),
            ("M1_WATERFALL_20B_ER55", dict(window=20, min_er=0.55, min_move_px=10.0, min_same_color=0.65, breakout_lookback=60, stop_atr_mult=2.5, max_stop_usd=12.0, trail_bars=5, max_hold_bars=90, mode="follow")),
            ("M1_WATERFALL_30B_ER50", dict(window=30, min_er=0.50, min_move_px=12.0, min_same_color=0.60, breakout_lookback=60, stop_atr_mult=2.5, max_stop_usd=12.0, trail_bars=6, max_hold_bars=120, mode="follow")),
            ("M1_WATERFALL_15B_LDN_NY", dict(window=15, min_er=0.55, min_move_px=8.0, min_same_color=0.65, breakout_lookback=45, stop_atr_mult=2.5, max_stop_usd=10.0, trail_bars=5, max_hold_bars=90, mode="follow", blocked_utc_hours=LONDON_NY_BLOCKED_UTC_HOURS)),
            ("M1_EMA9_21_SLOPE_ACCEL", dict(window=10, min_er=0.0, min_move_px=5.0, min_same_color=0.0, breakout_lookback=0, stop_atr_mult=2.2, max_stop_usd=8.0, trail_bars=4, max_hold_bars=60, mode="ema_accel")),
            ("M1_WATERFALL_15B_FADE_CTRL", dict(window=15, min_er=0.55, min_move_px=8.0, min_same_color=0.65, breakout_lookback=45, stop_atr_mult=2.5, max_stop_usd=10.0, trail_bars=5, max_hold_bars=45, mode="fade")),
        ]
    else:
        specs = [
            ("M5_CASCADE_3B_15MIN_ALL", dict(window=3, min_er=0.65, min_move_px=7.0, min_same_color=1.0, breakout_lookback=12, stop_atr_mult=2.0, max_stop_usd=12.0, trail_bars=2, max_hold_bars=36, mode="follow")),
            ("M5_CASCADE_3B_15MIN_LDN_NY", dict(window=3, min_er=0.65, min_move_px=7.0, min_same_color=1.0, breakout_lookback=12, stop_atr_mult=2.0, max_stop_usd=12.0, trail_bars=2, max_hold_bars=36, mode="follow", blocked_utc_hours=LONDON_NY_BLOCKED_UTC_HOURS)),
            ("M5_CASCADE_4B_20MIN_ALL", dict(window=4, min_er=0.60, min_move_px=9.0, min_same_color=0.75, breakout_lookback=18, stop_atr_mult=2.2, max_stop_usd=12.0, trail_bars=3, max_hold_bars=48, mode="follow")),
            ("M5_CASCADE_6B_30MIN_ALL", dict(window=6, min_er=0.55, min_move_px=12.0, min_same_color=0.66, breakout_lookback=24, stop_atr_mult=2.5, max_stop_usd=15.0, trail_bars=3, max_hold_bars=60, mode="follow")),
            ("M5_CASCADE_6B_30MIN_LDN_NY", dict(window=6, min_er=0.55, min_move_px=12.0, min_same_color=0.66, breakout_lookback=24, stop_atr_mult=2.5, max_stop_usd=15.0, trail_bars=3, max_hold_bars=60, mode="follow", blocked_utc_hours=LONDON_NY_BLOCKED_UTC_HOURS)),
            ("M5_EMA9_21_SLOPE_ACCEL", dict(window=6, min_er=0.0, min_move_px=8.0, min_same_color=0.0, breakout_lookback=0, stop_atr_mult=2.2, max_stop_usd=12.0, trail_bars=3, max_hold_bars=48, mode="ema_accel")),
            ("M5_CASCADE_3B_FADE_CTRL", dict(window=3, min_er=0.65, min_move_px=7.0, min_same_color=1.0, breakout_lookback=12, stop_atr_mult=2.0, max_stop_usd=12.0, trail_bars=2, max_hold_bars=24, mode="fade")),
            ("M5_CASCADE_6B_FADE_CTRL", dict(window=6, min_er=0.55, min_move_px=12.0, min_same_color=0.66, breakout_lookback=24, stop_atr_mult=2.5, max_stop_usd=15.0, trail_bars=3, max_hold_bars=24, mode="fade")),
        ]

    rows: list[dict[str, Any]] = []
    for name, kw in specs:
        base_sum, base_tr = _sim_waterfall_engine(clean, spec=spec, spread_price=spread_price, **kw)
        st_sum, _ = _sim_waterfall_engine(
            clean, spec=spec, spread_price=spread_price * 1.5, commission_per_lot=5.0, **kw,
        )
        h1_sum = _summarize_trades([t for t in base_tr if t["entry_ts"] < split_ts])
        h2_sum = _summarize_trades([t for t in base_tr if t["entry_ts"] >= split_ts])
        mc95 = _mc_p95_dd(base_tr, trials=mc_trials, seed=42)
        passed = bool(
            base_sum["trades"] >= 20
            and base_sum["net"] > 0
            and (base_sum["pf"] or 0.0) >= 1.20
            and base_sum["excl_best"] > 0
            and h1_sum["net"] > 0
            and h2_sum["net"] > 0
            and st_sum["net"] > 0
        )
        rows.append({
            "engine": name,
            "passed": passed,
            "trades_per_week": round(base_sum["trades"] / max(span_days / 7.0, 0.1), 1),
            "base": base_sum,
            "h1_net": h1_sum["net"],
            "h2_net": h2_sum["net"],
            "stress_net": st_sum["net"],
            "mc_p95_dd": mc95,
        })

    rows.sort(key=lambda r: (r["passed"], r["base"]["p_dd"] if r["base"]["p_dd"] is not None else -999.0, r["base"]["net"]), reverse=True)
    return {
        "timeframe": tf_label,
        "bars": len(clean),
        "span_days": span_days,
        "spread_price": spread_price,
        "engines": rows,
    }


def make_synthetic_waterfall_bars(*, n_bars: int = 900, bar_seconds: int = 60) -> list[list[float]]:
    """Deterministic synthetic bars containing repeated clean waterfall trends for offline testing."""
    t0 = 1760000000
    px = 3500.0
    bars: list[list[float]] = []
    for i in range(n_bars):
        phase = i % 90
        if 20 <= phase < 55:
            # Clean waterfall crash for 35 bars straight (-$1.1/bar -> -$38 drop!)
            o = px
            c = o - 1.1
            h = o + 0.15
            lo = c - 0.15
        elif 55 <= phase < 75:
            # Clean vertical rally (+$1.8/bar)
            o = px
            c = o + 1.8
            h = c + 0.15
            lo = o - 0.15
        else:
            # Quiet chop
            o = px
            c = o + (0.3 if i % 2 == 0 else -0.3)
            h = max(o, c) + 0.4
            lo = min(o, c) - 0.4
        bars.append([float(t0 + i * bar_seconds), round(o, 2), round(h, 2), round(lo, 2), round(c, 2), 100.0])
        px = c
    return bars
