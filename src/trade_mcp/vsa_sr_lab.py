"""Instagram 2-Candle VSA / Wick-Body / Acceleration & Support-Resistance Empirical Lab.

Tests two popular discretionary/social-media & VSA claims objectively on real Alpari XAUUSD_i
OHLCV data (100 oz/lot CFD Leverage, live spread, zero lookahead, conservative SL-first fills):

  Part 1 — Direct Statistical Predictability Test ("Next 1 & Next 2 Candles"):
    Measures exact out-of-sample hit rates AND average net move AFTER Alpari spread across
    M5, M15, and H1 for:
      - 2C_PLAIN            : 2 consecutive bullish/bearish candles (baseline)
      - 2C_CLEAN_BODY_WICK  : strong body (>=60% range) + tiny opposite wick (<=20% range)
      - 2C_ACCEL_VOL_VSA    : + body acceleration (body2 > body1) + rising tick volume (>1.2x MA20)
      - 2C_ACCEL_VOL_TREND  : + aligned with 1H & 5H trend direction
      - VSA_STOPPING_WICK   : Wyckoff/VSA effort-without-result (high vol >=1.5x MA + long rejection wick)

  Part 2 — Full Causal Backtest of 7 VSA / 2-Candle / Support-Resistance Engines:
      1. INSTA_2C_VSA_HOLD2_M5   : 2-candle VSA accel + 1H/5H trend -> hold strictly 2 M5 bars
      2. INSTA_2C_VSA_HOLD2_M15  : Same exact rule on M15 (hold 2 M15 bars)
      3. INSTA_2C_VSA_HOLD2_H1   : Same exact rule on H1 (5-year audit, hold 2 H1 bars)
      4. VSA_2C_ACCEL_TRAIL_M15  : 2-candle VSA accel + 1H/5H trend on M15 + ATR trailing stop
      5. SR_BREAKOUT_2C_VSA_M15  : Causal swing/PDH-PDL S/R + 2-candle VSA breakout (2R / BE)
      6. SR_BOUNCE_WICK_VSA_M15  : Causal S/R tap + long rejection wick + VSA volume bounce (2R)
      7. SR_BREAKOUT_VSA_H1_5YR  : Causal H1 S/R + VSA volume breakout + Chandelier trail (5yr)
"""

from __future__ import annotations

from typing import Any

from .daytrade_paper import HARD_MAX_LOT, resolve_pricing_basis
from .gold_lab import _iso_utc, _summarize_trades, monte_carlo_drawdown
from .m1_momentum import (
    LONDON_NY_BLOCKED_UTC_HOURS,
    atr_series,
    clean_ohlcv_bars,
    ema_series,
    resample_bars_to_tf,
)


def _candle_metrics(bar: list[float]) -> tuple[int, float, float, float, float, float]:
    """Return (dir [+1/-1/0], body, rng, body_ratio, opp_wick_ratio, tail_wick_ratio)."""
    o, h, low, c = bar[1], bar[2], bar[3], bar[4]
    rng = max(h - low, 1e-9)
    body = abs(c - o)
    body_ratio = body / rng
    if c > o:
        d = 1
        opp_wick = (h - c) / rng   # upper rejection wick opposing bulls
        tail_wick = (o - low) / rng  # lower buying tail
    elif c < o:
        d = -1
        opp_wick = (c - low) / rng  # lower rejection wick opposing bears
        tail_wick = (h - o) / rng   # upper selling tail
    else:
        d = 0
        opp_wick = max(h - c, c - low) / rng
        tail_wick = min(h - c, c - low) / rng
    return d, body, rng, body_ratio, opp_wick, tail_wick


def _rolling_vol_ma(bars: list[list[float]], period: int = 20) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    s = 0.0
    for i, b in enumerate(bars):
        v = float(b[5]) if len(b) >= 6 else 1.0
        s += v
        if i >= period:
            s -= float(bars[i - period][5]) if len(bars[i - period]) >= 6 else 1.0
        if i >= period - 1:
            out[i] = max(s / period, 1e-9)
    return out


def _build_1h_5h_trend_lookup(h1_bars: list[list[float]]) -> tuple[list[int], list[int]]:
    """Strictly causal 1-hour & 5-hour alignment lookup from closed H1 bars.

    For every closed H1 bar closing at `t + 3600`, returns:
      +1 if BOTH last 1H close > 1H open (or > EMA9) AND 5H close > close 5 bars ago (EMA9 > EMA21)
      -1 if BOTH bearish
       0 otherwise (mixed/flat)
    """
    clean = clean_ohlcv_bars(h1_bars)
    if len(clean) < 25:
        return [], []
    closes = [b[4] for b in clean]
    ema9 = ema_series(closes, 9)
    ema21 = ema_series(closes, 21)
    end_times: list[int] = []
    trends: list[int] = []
    for i in range(5, len(clean)):
        b = clean[i]
        e9, e21 = ema9[i], ema21[i]
        delta_1h = b[4] - b[1]
        delta_5h = b[4] - clean[i - 5][4]
        tr = 0
        if e9 is not None and e21 is not None:
            if delta_1h > 0 and delta_5h > 0 and e9 > e21:
                tr = 1
            elif delta_1h < 0 and delta_5h < 0 and e9 < e21:
                tr = -1
        end_times.append(int(b[0]) + 3600)
        trends.append(tr)
    return end_times, trends


def _lookup_trend(end_times: list[int], trends: list[int], ts: int) -> int:
    if not end_times:
        return 0
    lo, hi = 0, len(end_times) - 1
    ans = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if end_times[mid] <= ts:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return trends[ans] if ans >= 0 else 0


def analyze_next_candles_predictability(
    bars: list[list[float]],
    *,
    bar_seconds: int,
    h1_end_times: list[int],
    h1_trends: list[int],
    spread_price: float = 0.33,
) -> dict[str, Any]:
    """Directly measure the Instagram claim: do 2 candles + wick/body + volume + accel predict next 1-2 bars?"""
    clean = clean_ohlcv_bars(bars)
    if len(clean) < 40:
        return {"bars": len(clean), "patterns": {}}
    vol_ma = _rolling_vol_ma(clean, period=20)
    atr_vals = atr_series(clean, period=14)
    blocked = set(LONDON_NY_BLOCKED_UTC_HOURS)

    buckets: dict[str, dict[str, float]] = {}
    names = [
        "2C_PLAIN_LONDON_NY",
        "2C_CLEAN_BODY_WICK",
        "2C_ACCEL_VOL_VSA",
        "2C_ACCEL_VOL_1H5H_TREND",
        "VSA_STOPPING_WICK_REVERSAL",
    ]
    for nm in names:
        buckets[nm] = {
            "n": 0.0,
            "next1_same_color": 0.0,
            "next2_both_same_color": 0.0,
            "next2_net_positive_gross": 0.0,
            "next2_net_positive_after_spread": 0.0,
            "sum_move_1bar": 0.0,
            "sum_move_2bar": 0.0,
        }

    def _record(key: str, pred_dir: int, i: int) -> None:
        b_next1 = clean[i]
        b_next2 = clean[i + 1]
        entry_mid = b_next1[1]
        m1 = (b_next1[4] - entry_mid) * pred_dir
        m2 = (b_next2[4] - entry_mid) * pred_dir
        d1 = 1 if b_next1[4] > b_next1[1] else (-1 if b_next1[4] < b_next1[1] else 0)
        d2 = 1 if b_next2[4] > b_next2[1] else (-1 if b_next2[4] < b_next2[1] else 0)
        bk = buckets[key]
        bk["n"] += 1.0
        if d1 == pred_dir:
            bk["next1_same_color"] += 1.0
        if d1 == pred_dir and d2 == pred_dir:
            bk["next2_both_same_color"] += 1.0
        if m2 > 0:
            bk["next2_net_positive_gross"] += 1.0
        if (m2 - spread_price) > 0:
            bk["next2_net_positive_after_spread"] += 1.0
        bk["sum_move_1bar"] += m1
        bk["sum_move_2bar"] += m2

    for i in range(25, len(clean) - 2):
        # Strictly contiguous next 2 bars so overnight/weekend gaps don't distort stats
        if (clean[i][0] - clean[i - 1][0]) != bar_seconds or (clean[i + 1][0] - clean[i][0]) != bar_seconds:
            continue
        hr = (int(clean[i][0]) // 3600) % 24
        if hr in blocked:
            continue

        b2 = clean[i - 2]
        b1 = clean[i - 1]
        vma1 = vol_ma[i - 1]
        atr1 = atr_vals[i - 1]
        if vma1 is None or atr1 is None or atr1 <= 0:
            continue

        d2, body2, _r2, br2, ow2, _tw2 = _candle_metrics(b2)
        d1, body1, r1, br1, ow1, tw1 = _candle_metrics(b1)
        v2 = float(b2[5]) if len(b2) >= 6 else 1.0
        v1 = float(b1[5]) if len(b1) >= 6 else 1.0

        if d1 != 0 and d1 == d2:
            _record("2C_PLAIN_LONDON_NY", d1, i)
            clean_shape = (br2 >= 0.55 and br1 >= 0.60 and ow2 <= 0.25 and ow1 <= 0.20)
            if clean_shape:
                _record("2C_CLEAN_BODY_WICK", d1, i)
                accel_vsa = (
                    body1 > body2 * 1.05
                    and body1 >= 0.55 * atr1
                    and v1 > v2
                    and v1 >= 1.20 * vma1
                )
                if accel_vsa:
                    _record("2C_ACCEL_VOL_VSA", d1, i)
                    tr = _lookup_trend(h1_end_times, h1_trends, int(clean[i][0]))
                    if tr == d1:
                        _record("2C_ACCEL_VOL_1H5H_TREND", d1, i)

        # Wyckoff / VSA Stopping Volume Exhaustion (high volume + long rejection wick -> predict REVERSAL)
        if v1 >= 1.50 * vma1 and r1 >= 0.90 * atr1 and br1 <= 0.35:
            # Long upper wick -> bearish reversal (-1); long lower wick -> bullish reversal (+1)
            upper_w = (b1[2] - max(b1[1], b1[4])) / r1
            lower_w = (min(b1[1], b1[4]) - b1[3]) / r1
            if upper_w >= 0.50 and upper_w > lower_w * 2.0:
                _record("VSA_STOPPING_WICK_REVERSAL", -1, i)
            elif lower_w >= 0.50 and lower_w > upper_w * 2.0:
                _record("VSA_STOPPING_WICK_REVERSAL", 1, i)

    out_patterns: dict[str, Any] = {}
    for nm, bk in buckets.items():
        n = int(bk["n"])
        if n == 0:
            out_patterns[nm] = {"n": 0}
            continue
        avg_m1 = bk["sum_move_1bar"] / n
        avg_m2 = bk["sum_move_2bar"] / n
        out_patterns[nm] = {
            "n": n,
            "next1_same_dir_pct": round(100.0 * bk["next1_same_color"] / n, 2),
            "next2_both_same_dir_pct": round(100.0 * bk["next2_both_same_color"] / n, 2),
            "next2_win_gross_pct": round(100.0 * bk["next2_net_positive_gross"] / n, 2),
            "next2_win_after_spread_pct": round(100.0 * bk["next2_net_positive_after_spread"] / n, 2),
            "avg_1bar_move_price": round(avg_m1, 4),
            "avg_2bar_move_price": round(avg_m2, 4),
            "avg_2bar_net_after_spread_price": round(avg_m2 - spread_price, 4),
        }
    return {"bars": len(clean), "spread_price": spread_price, "patterns": out_patterns}


def _sim_insta_2c_vsa(
    bars: list[list[float]],
    *,
    bar_seconds: int,
    h1_end_times: list[int],
    h1_trends: list[int],
    spec: dict[str, Any],
    spread_price: float = 0.33,
    commission_per_lot: float = 0.0,
    max_lot: float = 0.01,
    leverage: float = 500.0,
    max_stop_usd: float = 12.0,
    hold_bars_exit: int | None = 2,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Backtest 2-candle clean-body + acceleration + rising VSA volume + 1H/5H trend.

    When hold_bars_exit=2: exact Instagram claim (exit after 2 bars or hard SL).
    When hold_bars_exit=None: trail winners with 1.5x ATR trailing stop once >= 1.0R in profit.
    """
    _, multiplier = resolve_pricing_basis(spec, "auto")
    contract_size = float(spec.get("trade_contract_size") or 100.0)
    vol = min(float(max_lot), HARD_MAX_LOT)
    comm = commission_per_lot * vol
    max_stop_dist = max(spread_price * 4.0, (max_stop_usd - comm) / (vol * multiplier))
    blocked = set(LONDON_NY_BLOCKED_UTC_HOURS)

    clean = clean_ohlcv_bars(bars)
    if len(clean) < 35:
        return _summarize_trades([], peak_margin_usd=0.0, refusals={"insufficient_bars": 1}), []
    vol_ma = _rolling_vol_ma(clean, period=20)
    atr_vals = atr_series(clean, period=14)

    trades: list[dict[str, Any]] = []
    refusals: dict[str, int] = {}
    peak_margin = 0.0
    pos: dict[str, Any] | None = None

    for i in range(25, len(clean)):
        b = clean[i]
        t, o, h, low, c = b[0], b[1], b[2], b[3], b[4]
        bar_end = t + float(bar_seconds)
        hr = (int(t) // 3600) % 24
        atr1 = atr_vals[i - 1]
        vma1 = vol_ma[i - 1]

        opened_now = False
        if pos is None and atr1 is not None and vma1 is not None and hr not in blocked:
            b2 = clean[i - 2]
            b1 = clean[i - 1]
            d2, body2, _r2, br2, ow2, _ = _candle_metrics(b2)
            d1, body1, _r1, br1, ow1, _ = _candle_metrics(b1)
            v2 = float(b2[5]) if len(b2) >= 6 else 1.0
            v1 = float(b1[5]) if len(b1) >= 6 else 1.0
            if (
                d1 != 0
                and d1 == d2
                and br2 >= 0.55
                and br1 >= 0.60
                and ow2 <= 0.25
                and ow1 <= 0.20
                and body1 > body2 * 1.05
                and body1 >= 0.55 * atr1
                and v1 > v2
                and v1 >= 1.20 * vma1
            ):
                tr = _lookup_trend(h1_end_times, h1_trends, int(t))
                if tr == d1:
                    side = "buy" if d1 == 1 else "sell"
                    entry = (o + spread_price) if side == "buy" else o
                    raw_s = (entry - b1[3]) if side == "buy" else (b1[2] + spread_price - entry)
                    sdist = min(max(raw_s, 1.5 * atr1, spread_price * 4.0), max_stop_dist)
                    stop_px = (entry - sdist) if side == "buy" else (entry + sdist)
                    pos = {
                        "side": side,
                        "entry": entry,
                        "stop": stop_px,
                        "sdist": sdist,
                        "opened_at": t,
                        "opened_i": i,
                        "best_px": entry,
                        "trailed": False,
                    }
                    peak_margin = max(peak_margin, vol * contract_size * o / leverage)
                    opened_now = True

        if pos is not None:
            side = pos["side"]
            if side == "buy":
                open_ex = (pos["entry"] - spread_price) if opened_now else o
                hit_sl = low <= pos["stop"]
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
                    "opened_at": pos["opened_at"],
                    "closed_at": bar_end,
                    "side": side,
                    "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                    "exit_reason": "trail_stop" if pos["trailed"] else "stop_loss",
                })
                pos = None
                continue

            bars_held = (i - pos["opened_i"]) + 1
            if hold_bars_exit is not None:
                if bars_held >= hold_bars_exit:
                    diff = (ex_close - pos["entry"]) if side == "buy" else (pos["entry"] - ex_close)
                    trades.append({
                        "opened_at": pos["opened_at"],
                        "closed_at": bar_end,
                        "side": side,
                        "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                        "exit_reason": f"hold_{hold_bars_exit}_bars",
                    })
                    pos = None
            elif not opened_now:
                atr_now = atr_vals[i] or atr1 or pos["sdist"]
                if side == "buy":
                    pos["best_px"] = max(pos["best_px"], h)
                    if (c - pos["entry"]) >= 1.0 * pos["sdist"]:
                        cand = max(pos["stop"], pos["entry"], pos["best_px"] - 1.8 * atr_now)
                        if cand > pos["stop"]:
                            pos["stop"] = cand
                            pos["trailed"] = True
                else:
                    pos["best_px"] = min(pos["best_px"], low)
                    if (pos["entry"] - (c + spread_price)) >= 1.0 * pos["sdist"]:
                        cand = min(pos["stop"], pos["entry"], pos["best_px"] + spread_price + 1.8 * atr_now)
                        if cand < pos["stop"]:
                            pos["stop"] = cand
                            pos["trailed"] = True
                if bars_held >= 48:
                    diff = (ex_close - pos["entry"]) if side == "buy" else (pos["entry"] - ex_close)
                    trades.append({
                        "opened_at": pos["opened_at"],
                        "closed_at": bar_end,
                        "side": side,
                        "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                        "exit_reason": "max_hold",
                    })
                    pos = None

    return _summarize_trades(trades, peak_margin_usd=peak_margin, refusals=refusals), trades


def _causal_sr_levels(
    bars: list[list[float]],
    *,
    i: int,
    swing_k: int = 3,
    lookback: int = 120,
    prev_day_hi: float | None = None,
    prev_day_lo: float | None = None,
) -> tuple[list[float], list[float]]:
    """Return strictly causal resistance & support levels confirmed BEFORE bar i.

    A pivot at index p is ONLY known at bar `p + swing_k` (zero lookahead!).
    """
    res: list[float] = []
    sup: list[float] = []
    if prev_day_hi is not None:
        res.append(prev_day_hi)
    if prev_day_lo is not None:
        sup.append(prev_day_lo)
    lo_idx = max(swing_k, i - lookback)
    hi_idx = i - 1 - swing_k  # strictly confirmed by bar i-1
    for p in range(lo_idx, hi_idx + 1):
        ph = bars[p][2]
        pl = bars[p][3]
        is_sh = all(ph >= bars[p - k][2] and ph >= bars[p + k][2] for k in range(1, swing_k + 1))
        is_sl = all(pl <= bars[p - k][3] and pl <= bars[p + k][3] for k in range(1, swing_k + 1))
        if is_sh:
            res.append(ph)
        if is_sl:
            sup.append(pl)
    return res[-12:], sup[-12:]


def _sim_sr_vsa(
    bars: list[list[float]],
    *,
    bar_seconds: int,
    h1_end_times: list[int],
    h1_trends: list[int],
    spec: dict[str, Any],
    mode: str,
    spread_price: float = 0.33,
    commission_per_lot: float = 0.0,
    max_lot: float = 0.01,
    leverage: float = 500.0,
    max_stop_usd: float = 15.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Causal Support & Resistance + VSA Volume/Wick engine.

    mode == 'breakout':
      2 accelerating clean-body candles break and close through a causal Resistance/Support level
      with rising volume (>=1.2x MA20) aligned with 1H/5H trend -> stop behind level (capped),
      BE at 1.2R, Chandelier trail / 2.5R target.
    mode == 'bounce':
      Bar taps within 0.35*ATR of causal Support/Resistance, prints a long rejection wick
      (>=45% of range) with VSA volume >=1.25x MA20 and closes back inside -> bounce trade (2.0R).
    """
    _, multiplier = resolve_pricing_basis(spec, "auto")
    contract_size = float(spec.get("trade_contract_size") or 100.0)
    vol = min(float(max_lot), HARD_MAX_LOT)
    comm = commission_per_lot * vol
    max_stop_dist = max(spread_price * 4.0, (max_stop_usd - comm) / (vol * multiplier))
    blocked = set(LONDON_NY_BLOCKED_UTC_HOURS)

    clean = clean_ohlcv_bars(bars)
    if len(clean) < 130:
        return _summarize_trades([], peak_margin_usd=0.0, refusals={"insufficient_bars": 1}), []
    vol_ma = _rolling_vol_ma(clean, period=20)
    atr_vals = atr_series(clean, period=14)

    # Track strictly causal Previous Day High/Low
    cur_day = -1
    day_hi: float | None = None
    day_lo: float | None = None
    prev_hi: float | None = None
    prev_lo: float | None = None

    trades: list[dict[str, Any]] = []
    refusals: dict[str, int] = {}
    peak_margin = 0.0
    pos: dict[str, Any] | None = None

    for i in range(len(clean)):
        b = clean[i]
        t, o, h, low, c = b[0], b[1], b[2], b[3], b[4]
        bar_end = t + float(bar_seconds)
        d_id = int(t) // 86400
        if d_id != cur_day:
            if day_hi is not None:
                prev_hi, prev_lo = day_hi, day_lo
            cur_day = d_id
            day_hi, day_lo = h, low
        else:
            day_hi = max(day_hi or h, h)
            day_lo = min(day_lo or low, low)

        if i < 60:
            continue
        hr = (int(t) // 3600) % 24
        atr1 = atr_vals[i - 1]
        vma1 = vol_ma[i - 1]

        opened_now = False
        if pos is None and atr1 is not None and vma1 is not None and hr not in blocked:
            b2 = clean[i - 2]
            b1 = clean[i - 1]
            v1 = float(b1[5]) if len(b1) >= 6 else 1.0
            v2 = float(b2[5]) if len(b2) >= 6 else 1.0
            res_lvls, sup_lvls = _causal_sr_levels(
                clean, i=i, swing_k=3, lookback=100, prev_day_hi=prev_hi, prev_day_lo=prev_lo,
            )
            tr = _lookup_trend(h1_end_times, h1_trends, int(t))

            side: str | None = None
            raw_stop: float | None = None
            if mode == "breakout":
                d2, body2, _r2, br2, ow2, _ = _candle_metrics(b2)
                d1, body1, _r1, br1, ow1, _ = _candle_metrics(b1)
                if d1 != 0 and d1 == d2 and br1 >= 0.55 and ow1 <= 0.25 and body1 >= body2 and v1 >= 1.20 * vma1 and v1 >= v2:
                    if d1 == 1 and tr == 1:
                        broken = [lv for lv in res_lvls if b2[1] < lv < b1[4] and (b1[4] - lv) <= 1.5 * atr1]
                        if broken:
                            side = "buy"
                            raw_stop = min(b1[3], max(broken) - 0.5 * atr1)
                    elif d1 == -1 and tr == -1:
                        broken = [lv for lv in sup_lvls if b1[4] < lv < b2[1] and (lv - b1[4]) <= 1.5 * atr1]
                        if broken:
                            side = "sell"
                            raw_stop = max(b1[2], min(broken) + 0.5 * atr1)
            elif mode == "bounce":
                _d1, _bd1, r1, br1, _ow1, _tw1 = _candle_metrics(b1)
                upper_w = (b1[2] - max(b1[1], b1[4])) / r1
                lower_w = (min(b1[1], b1[4]) - b1[3]) / r1
                if v1 >= 1.25 * vma1 and r1 >= 0.8 * atr1:
                    # Support rejection hammer/pin -> BUY
                    if lower_w >= 0.45 and b1[4] > b1[1] and any(abs(b1[3] - lv) <= 0.40 * atr1 and b1[4] > lv for lv in sup_lvls):
                        side = "buy"
                        raw_stop = b1[3] - 0.25 * atr1
                    # Resistance rejection shooting star/pin -> SELL
                    elif upper_w >= 0.45 and b1[4] < b1[1] and any(abs(b1[2] - lv) <= 0.40 * atr1 and b1[4] < lv for lv in res_lvls):
                        side = "sell"
                        raw_stop = b1[2] + 0.25 * atr1

            if side is not None and raw_stop is not None:
                entry = (o + spread_price) if side == "buy" else o
                raw_s = (entry - raw_stop) if side == "buy" else (raw_stop + spread_price - entry)
                sdist = min(max(raw_s, 1.5 * atr1, spread_price * 4.0), max_stop_dist)
                stop_px = (entry - sdist) if side == "buy" else (entry + sdist)
                tp_px = (entry + 2.5 * sdist) if mode == "breakout" else (entry + 2.0 * sdist)
                if side == "sell":
                    tp_px = (entry - 2.5 * sdist) if mode == "breakout" else (entry - 2.0 * sdist)
                be_trig = (entry + 1.2 * sdist) if side == "buy" else (entry - 1.2 * sdist)
                pos = {
                    "side": side,
                    "entry": entry,
                    "stop": stop_px,
                    "tp": tp_px,
                    "be_trig": be_trig,
                    "be_moved": False,
                    "opened_at": t,
                    "opened_i": i,
                }
                peak_margin = max(peak_margin, vol * contract_size * o / leverage)
                opened_now = True

        if pos is not None:
            side = pos["side"]
            if side == "buy":
                open_ex = (pos["entry"] - spread_price) if opened_now else o
                hit_sl = low <= pos["stop"]
                fill_sl = min(open_ex, pos["stop"])
                hit_tp = h >= pos["tp"]
                hit_be = h >= pos["be_trig"]
            else:
                open_ex = (pos["entry"] + spread_price) if opened_now else (o + spread_price)
                hit_sl = (h + spread_price) >= pos["stop"]
                fill_sl = max(open_ex, pos["stop"])
                hit_tp = (low + spread_price) <= pos["tp"]
                hit_be = (low + spread_price) <= pos["be_trig"]

            if hit_sl:
                diff = (fill_sl - pos["entry"]) if side == "buy" else (pos["entry"] - fill_sl)
                trades.append({
                    "opened_at": pos["opened_at"],
                    "closed_at": bar_end,
                    "side": side,
                    "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                    "exit_reason": "breakeven" if pos["be_moved"] else "stop_loss",
                })
                pos = None
                continue
            if hit_tp:
                diff = (pos["tp"] - pos["entry"]) if side == "buy" else (pos["entry"] - pos["tp"])
                trades.append({
                    "opened_at": pos["opened_at"],
                    "closed_at": bar_end,
                    "side": side,
                    "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                    "exit_reason": "target_hit",
                })
                pos = None
                continue
            if not opened_now and not pos["be_moved"] and hit_be:
                pos["stop"] = pos["entry"]
                pos["be_moved"] = True
            if (i - pos["opened_i"]) >= 72:
                ex = c if side == "buy" else (c + spread_price)
                diff = (ex - pos["entry"]) if side == "buy" else (pos["entry"] - ex)
                trades.append({
                    "opened_at": pos["opened_at"],
                    "closed_at": bar_end,
                    "side": side,
                    "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                    "exit_reason": "max_hold",
                })
                pos = None

    return _summarize_trades(trades, peak_margin_usd=peak_margin, refusals=refusals), trades


def evaluate_vsa_and_sr_lab(
    *,
    m5_bars: list[list[float]],
    m15_bars: list[list[float]],
    h1_bars: list[list[float]],
    spec: dict[str, Any],
    spread_price: float = 0.33,
    max_lot: float = 0.01,
    leverage: float = 500.0,
    mc_trials: int = 1000,
) -> dict[str, Any]:
    """Run both statistical next-2-candle predictability audit AND 7 full causal VSA/SR backtests."""
    clean_m5 = clean_ohlcv_bars(m5_bars)
    clean_m15 = clean_ohlcv_bars(m15_bars) if m15_bars else resample_bars_to_tf(clean_m5, src_seconds=300, dst_seconds=900)
    clean_h1 = clean_ohlcv_bars(h1_bars) if h1_bars else resample_bars_to_tf(clean_m15, src_seconds=900, dst_seconds=3600)
    if len(clean_m5) < 60 or len(clean_h1) < 60:
        raise ValueError("insufficient history for VSA & SR lab")

    h1_end_t, h1_tr = _build_1h_5h_trend_lookup(clean_h1)

    # Part 1: Direct empirical test of Instagram "2 candles -> next 1 & 2 candles" claim
    predictability = {
        "M5": analyze_next_candles_predictability(clean_m5, bar_seconds=300, h1_end_times=h1_end_t, h1_trends=h1_tr, spread_price=spread_price),
        "M15": analyze_next_candles_predictability(clean_m15, bar_seconds=900, h1_end_times=h1_end_t, h1_trends=h1_tr, spread_price=spread_price),
        "H1": analyze_next_candles_predictability(clean_h1, bar_seconds=3600, h1_end_times=h1_end_t, h1_trends=h1_tr, spread_price=spread_price),
    }

    # Part 2: 7 full causal strategy backtests with 4-level cost stress & 1,000-trial MC
    specs_list: list[tuple[str, str, str, Any]] = [
        ("INSTA_2C_VSA_HOLD2_M5", "M5", "Instagram 2-candle VSA accel + 1H/5H trend -> hold 2 M5 bars",
         lambda sp, cm: _sim_insta_2c_vsa(clean_m5, bar_seconds=300, h1_end_times=h1_end_t, h1_trends=h1_tr, spec=spec, spread_price=sp, commission_per_lot=cm, max_lot=max_lot, leverage=leverage, max_stop_usd=12.0, hold_bars_exit=2)),
        ("INSTA_2C_VSA_HOLD2_M15", "M15", "Instagram 2-candle VSA accel + 1H/5H trend -> hold 2 M15 bars",
         lambda sp, cm: _sim_insta_2c_vsa(clean_m15, bar_seconds=900, h1_end_times=h1_end_t, h1_trends=h1_tr, spec=spec, spread_price=sp, commission_per_lot=cm, max_lot=max_lot, leverage=leverage, max_stop_usd=15.0, hold_bars_exit=2)),
        ("INSTA_2C_VSA_HOLD2_H1", "H1", "Instagram 2-candle VSA accel + 1H/5H trend -> hold 2 H1 bars (5yr)",
         lambda sp, cm: _sim_insta_2c_vsa(clean_h1, bar_seconds=3600, h1_end_times=h1_end_t, h1_trends=h1_tr, spec=spec, spread_price=sp, commission_per_lot=cm, max_lot=max_lot, leverage=leverage, max_stop_usd=25.0, hold_bars_exit=2)),
        ("VSA_2C_ACCEL_TRAIL_M15", "M15", "2-candle VSA accel + 1H/5H trend on M15 + 1.8xATR trailing stop",
         lambda sp, cm: _sim_insta_2c_vsa(clean_m15, bar_seconds=900, h1_end_times=h1_end_t, h1_trends=h1_tr, spec=spec, spread_price=sp, commission_per_lot=cm, max_lot=max_lot, leverage=leverage, max_stop_usd=15.0, hold_bars_exit=None)),
        ("SR_BREAKOUT_2C_VSA_M15", "M15", "Causal S/R + 2-candle VSA accelerating breakout (BE 1.2R, TP 2.5R)",
         lambda sp, cm: _sim_sr_vsa(clean_m15, bar_seconds=900, h1_end_times=h1_end_t, h1_trends=h1_tr, spec=spec, mode="breakout", spread_price=sp, commission_per_lot=cm, max_lot=max_lot, leverage=leverage, max_stop_usd=15.0)),
        ("SR_BOUNCE_WICK_VSA_M15", "M15", "Causal S/R tap + long rejection wick + VSA volume bounce (2.0R)",
         lambda sp, cm: _sim_sr_vsa(clean_m15, bar_seconds=900, h1_end_times=h1_end_t, h1_trends=h1_tr, spec=spec, mode="bounce", spread_price=sp, commission_per_lot=cm, max_lot=max_lot, leverage=leverage, max_stop_usd=15.0)),
        ("SR_BREAKOUT_VSA_H1_5YR", "H1", "Causal H1 S/R + 2-candle VSA breakout over full H1 history (5yr)",
         lambda sp, cm: _sim_sr_vsa(clean_h1, bar_seconds=3600, h1_end_times=h1_end_t, h1_trends=h1_tr, spec=spec, mode="breakout", spread_price=sp, commission_per_lot=cm, max_lot=max_lot, leverage=leverage, max_stop_usd=25.0)),
    ]

    engines: list[dict[str, Any]] = []
    for eid, tf, label, fn in specs_list:
        base_sum, base_tr = fn(spread_price, 0.0)
        st_050_c5, _ = fn(max(spread_price, 0.50), 5.0)
        st_070_c7, _ = fn(max(spread_price, 0.70), 7.0)
        mc = monte_carlo_drawdown([float(t["net_pnl_usd"]) for t in base_tr], trials=mc_trials, seed=42)
        dd = float(base_sum["max_dd_usd"])
        p_dd = round(float(base_sum["net_pnl_usd"]) / dd, 2) if dd > 0 else None
        # Split into older half vs recent half
        mid_i = len(base_tr) // 2
        h1_sum = _summarize_trades(base_tr[:mid_i], peak_margin_usd=0.0, refusals={}) if mid_i >= 5 else None
        h2_sum = _summarize_trades(base_tr[mid_i:], peak_margin_usd=0.0, refusals={}) if len(base_tr) - mid_i >= 5 else None
        engines.append({
            "id": eid,
            "tf": tf,
            "label": label,
            "full": {**base_sum, "profit_to_dd_ratio": p_dd},
            "half1": h1_sum,
            "half2": h2_sum,
            "monte_carlo": mc,
            "stress_050_c5": {"net_pnl_usd": st_050_c5["net_pnl_usd"], "pf": st_050_c5["pf"]},
            "stress_070_c7": {"net_pnl_usd": st_070_c7["net_pnl_usd"], "pf": st_070_c7["pf"]},
        })

    return {
        "ok": True,
        "paper": True,
        "executable": False,
        "execution_authorized": False,
        "orders_submitted": 0,
        "ranges": {
            "M5": [_iso_utc(clean_m5[0][0]), _iso_utc(clean_m5[-1][0])],
            "M15": [_iso_utc(clean_m15[0][0]), _iso_utc(clean_m15[-1][0])],
            "H1": [_iso_utc(clean_h1[0][0]), _iso_utc(clean_h1[-1][0])],
        },
        "predictability": predictability,
        "engines": engines,
    }
