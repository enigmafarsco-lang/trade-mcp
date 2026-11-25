"""Empirical Japanese Candlestick Pattern & Walk-Forward 1-2 Candle Discovery Lab.

Strictly causal (bar i signals enter at bar i+1 open with real broker spread):
  1. Tests 18 classic named Japanese candlestick patterns (12 Reversal + 6 Continuation)
     from Nison/Morris & Instagram trading charts across M5, M15, H1, H4, and D1.
  2. Runs a strict Train (first 50%) -> Blind OOS Test (second 50%) pattern miner across
     interpretable 1-, 2-, and 3-candle morphological + context signatures to test whether
     Alpari data truly contains a repeatable 1- or 2-candle directional edge after spread.
  3. Runs full 1-position-at-a-time trade backtests (1-bar / 2-bar hold + ATR stop) with
     1.5x spread + $5/lot commission stress and 500-trial Monte Carlo drawdown checks.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Any

from trade_mcp.daytrade_paper import resolve_pricing_basis
from trade_mcp.m1_momentum import clean_ohlcv_bars


def _summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "wr": None, "net": 0.0, "pf": None, "max_dd": 0.0, "p_dd": None, "excl_best": 0.0}
    pnls = [float(t["pnl"]) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    net = sum(pnls)
    pf = (gross_w / gross_l) if gross_l > 0 else (99.0 if gross_w > 0 else None)
    eq = peak = dd = 0.0
    for p in pnls:
        eq += p
        if eq > peak:
            peak = eq
        if peak - eq > dd:
            dd = peak - eq
    excl_best = net - max(pnls) if pnls else 0.0
    p_dd = (net / dd) if dd > 0 else (99.0 if net > 0 else None)
    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "wr": round(100.0 * wins / n, 2),
        "net": round(net, 2),
        "pf": round(pf, 3) if pf is not None else None,
        "max_dd": round(dd, 2),
        "p_dd": round(p_dd, 2) if p_dd is not None else None,
        "excl_best": round(excl_best, 2),
    }


def _mc_p95_dd(trades: list[dict[str, Any]], *, trials: int = 400, seed: int = 42) -> float:
    pnls = [float(t["pnl"]) for t in trades]
    if len(pnls) < 2 or trials <= 0:
        return 0.0
    rng = random.Random(seed)
    dds: list[float] = []
    buf = list(pnls)
    for _ in range(trials):
        rng.shuffle(buf)
        eq = peak = dd = 0.0
        for p in buf:
            eq += p
            if eq > peak:
                peak = eq
            if peak - eq > dd:
                dd = peak - eq
        dds.append(dd)
    dds.sort()
    return round(dds[min(len(dds) - 1, int(0.95 * len(dds)))], 2)


# ---------------------------------------------------------------------------
# Helpers: Resampling H1 -> H4 / D1 & Causal Indicators
# ---------------------------------------------------------------------------

def resample_ohlcv(bars: list[list[float]], bucket_seconds: int) -> list[list[float]]:
    """Causally aggregate lower-TF bars (e.g. H1) into aligned UTC H4 (14400s) or D1 (86400s)."""
    if not bars or bucket_seconds <= 0:
        return []
    out: list[list[float]] = []
    cur_bucket: int | None = None
    o = h = lo = c = v = 0.0
    for b in bars:
        ts = int(b[0])
        bkt = (ts // bucket_seconds) * bucket_seconds
        if cur_bucket is None or bkt != cur_bucket:
            if cur_bucket is not None:
                out.append([float(cur_bucket), o, h, lo, c, v])
            cur_bucket = bkt
            o, h, lo, c, v = float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])
        else:
            if b[2] > h:
                h = float(b[2])
            if b[3] < lo:
                lo = float(b[3])
            c = float(b[4])
            v += float(b[5])
    if cur_bucket is not None:
        out.append([float(cur_bucket), o, h, lo, c, v])
    return out


def _ema(vals: list[float], period: int) -> list[float]:
    if not vals:
        return []
    k = 2.0 / (period + 1.0)
    out = [vals[0]]
    for x in vals[1:]:
        out.append(x * k + out[-1] * (1.0 - k))
    return out


def _atr14(bars: list[list[float]], period: int = 14) -> list[float]:
    n = len(bars)
    if n == 0:
        return []
    trs = [bars[0][2] - bars[0][3]]
    for i in range(1, n):
        hi, lo, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    out = [0.0] * n
    acc = 0.0
    for i, tr in enumerate(trs):
        acc += tr
        if i >= period:
            acc -= trs[i - period]
            out[i] = acc / period
        else:
            out[i] = acc / (i + 1)
    return out


def _bar_geom(b: list[float]) -> tuple[float, float, float, float, int]:
    """Return (rng, body, upper_wick, lower_wick, sign: +1 bull / -1 bear / 0 flat)."""
    o, h, lo, c = b[1], b[2], b[3], b[4]
    rng = max(h - lo, 1e-12)
    body = abs(c - o)
    upper = max(0.0, h - max(o, c))
    lower = max(0.0, min(o, c) - lo)
    sign = 1 if c > o else (-1 if c < o else 0)
    return rng, body, upper, lower, sign


def _bar_shape_label(b: list[float]) -> str:
    """Discrete human-interpretable candle morphology."""
    rng, body, upper, lower, sign = _bar_geom(b)
    br = body / rng
    ur = upper / rng
    lr = lower / rng
    if br >= 0.65:
        return "STRONG_UP" if sign >= 0 else "STRONG_DN"
    if lr >= 0.50 and ur <= 0.20:
        return "LOWER_WICK"
    if ur >= 0.50 and lr <= 0.20:
        return "UPPER_WICK"
    if br <= 0.22:
        return "DOJI"
    return "NORM_UP" if sign >= 0 else "NORM_DN"


# ---------------------------------------------------------------------------
# Part A: 18 Classic Named Japanese Candlestick Patterns (12 Rev + 6 Cont)
# ---------------------------------------------------------------------------

CLASSIC_PATTERN_META: list[tuple[str, str, str]] = [
    ("HAMMER", "reversal", "چکش (صعودی)"),
    ("SHOOTING_STAR", "reversal", "ستاره ثاقب (نزولی)"),
    ("HANGING_MAN", "reversal", "مرد به دار آویخته (نزولی)"),
    ("INVERTED_HAMMER", "reversal", "چکش معکوس (صعودی)"),
    ("PINBAR_REVERSAL", "reversal", "پین‌بار برگشتی"),
    ("DRAGONFLY_GRAVESTONE_DOJI", "reversal", "دوجی سنجاقک/سنگ‌قبر"),
    ("ENGULFING", "reversal", "پوشا / اینگالفینگ"),
    ("HARAMI", "reversal", "هارامی / اینساید بادی"),
    ("PIERCING_DARK_CLOUD", "reversal", "خط نافذ / ابر سیاه"),
    ("TWEEZER_TOP_BOTTOM", "reversal", "انبرک سقف/کف"),
    ("MORNING_EVENING_STAR", "reversal", "ستاره صبحگاهی/عصرگاهی"),
    ("THREE_INSIDE_UP_DOWN", "reversal", "سه کندل داخلی"),
    ("MARUBOZU_CONT", "continuation", "ماروبوزو هم‌روند"),
    ("THREE_SOLDIERS_CROWS", "continuation", "سه سرباز سفید / سه کلاغ سیاه"),
    ("INSIDE_BAR_BREAKOUT", "continuation", "شکست اینساید بار هم‌روند"),
    ("RISING_FALLING_THREE", "continuation", "سه روش صعودی/نزولی"),
    ("OUTSIDE_MOMENTUM_CONT", "continuation", "پوشاننده شتاب هم‌روند"),
    ("TWO_BAR_THRUST_CONT", "continuation", "شتاب ۲ کندلی هم‌روند"),
]


def detect_classic_patterns(
    bars: list[list[float]],
    ema20: list[float],
    atr: list[float],
) -> dict[str, list[tuple[int, int]]]:
    """Return {pattern_name: [(bar_idx, direction +1/-1), ...]} strictly causally."""
    hits: dict[str, list[tuple[int, int]]] = {name: [] for name, _, _ in CLASSIC_PATTERN_META}
    n = len(bars)
    if n < 30:
        return hits

    for i in range(25, n - 2):
        a = atr[i]
        if a <= 0:
            continue
        b0 = bars[i]
        b1 = bars[i - 1]
        b2 = bars[i - 2]
        b3 = bars[i - 3]
        rng0, body0, up0, lo0, s0 = _bar_geom(b0)
        rng1, body1, _up1, _lo1, s1 = _bar_geom(b1)
        rng2, body2, _up2, _lo2, s2 = _bar_geom(b2)
        _rng3, body3, _up3, _lo3, s3 = _bar_geom(b3)

        # Short-term prior trend entering the pattern (strictly <= i-1)
        prior_down = b1[4] < ema20[i - 1] and b1[4] < bars[i - 4][4]
        prior_up = b1[4] > ema20[i - 1] and b1[4] > bars[i - 4][4]
        trend_up_now = b0[4] > ema20[i] and ema20[i] > ema20[i - 3]
        trend_dn_now = b0[4] < ema20[i] and ema20[i] < ema20[i - 3]

        # 1. HAMMER (bullish reversal after decline)
        if prior_down and rng0 >= 0.6 * a and body0 <= 0.35 * rng0 and lo0 >= 2.0 * max(body0, 0.05 * rng0) and up0 <= 0.15 * rng0:
            hits["HAMMER"].append((i, 1))

        # 2. SHOOTING STAR (bearish reversal after rally)
        if prior_up and rng0 >= 0.6 * a and body0 <= 0.35 * rng0 and up0 >= 2.0 * max(body0, 0.05 * rng0) and lo0 <= 0.15 * rng0:
            hits["SHOOTING_STAR"].append((i, -1))

        # 3. HANGING MAN (bearish reversal after rally, long lower shadow)
        if prior_up and rng0 >= 0.6 * a and body0 <= 0.35 * rng0 and lo0 >= 2.0 * max(body0, 0.05 * rng0) and up0 <= 0.15 * rng0:
            hits["HANGING_MAN"].append((i, -1))

        # 4. INVERTED HAMMER (bullish reversal after decline, long upper shadow)
        if prior_down and rng0 >= 0.6 * a and body0 <= 0.35 * rng0 and up0 >= 2.0 * max(body0, 0.05 * rng0) and lo0 <= 0.15 * rng0:
            hits["INVERTED_HAMMER"].append((i, 1))

        # 5. PIN BAR REVERSAL (strong wick >= 62% of range, range >= 0.8 ATR)
        if rng0 >= 0.8 * a:
            if prior_down and lo0 >= 0.62 * rng0 and up0 <= 0.18 * rng0:
                hits["PINBAR_REVERSAL"].append((i, 1))
            elif prior_up and up0 >= 0.62 * rng0 and lo0 <= 0.18 * rng0:
                hits["PINBAR_REVERSAL"].append((i, -1))

        # 6. DRAGONFLY / GRAVESTONE DOJI
        if rng0 >= 0.5 * a and body0 <= 0.12 * rng0:
            if prior_down and lo0 >= 0.65 * rng0:
                hits["DRAGONFLY_GRAVESTONE_DOJI"].append((i, 1))
            elif prior_up and up0 >= 0.65 * rng0:
                hits["DRAGONFLY_GRAVESTONE_DOJI"].append((i, -1))

        # 7. BULLISH / BEARISH ENGULFING
        if body0 >= 0.55 * rng0 and body1 >= 0.25 * a and body0 >= 1.15 * body1:
            if prior_down and s1 < 0 and s0 > 0 and b0[1] <= b1[4] + 0.10 * a and b0[4] >= b1[1]:
                hits["ENGULFING"].append((i, 1))
            elif prior_up and s1 > 0 and s0 < 0 and b0[1] >= b1[4] - 0.10 * a and b0[4] <= b1[1]:
                hits["ENGULFING"].append((i, -1))

        # 8. HARAMI (inside body opposite direction)
        if body1 >= 0.6 * a and body0 > 0 and body0 <= 0.50 * body1:
            hi_b1, lo_b1 = max(b1[1], b1[4]), min(b1[1], b1[4])
            hi_b0, lo_b0 = max(b0[1], b0[4]), min(b0[1], b0[4])
            if hi_b0 <= hi_b1 and lo_b0 >= lo_b1:
                if prior_down and s1 < 0 and s0 > 0:
                    hits["HARAMI"].append((i, 1))
                elif prior_up and s1 > 0 and s0 < 0:
                    hits["HARAMI"].append((i, -1))

        # 9. PIERCING LINE / DARK CLOUD COVER
        if body1 >= 0.6 * a and body0 >= 0.45 * rng0:
            mid1 = 0.5 * (b1[1] + b1[4])
            if prior_down and s1 < 0 and s0 > 0 and b0[1] <= b1[4] + 0.08 * a and mid1 < b0[4] < b1[1]:
                hits["PIERCING_DARK_CLOUD"].append((i, 1))
            elif prior_up and s1 > 0 and s0 < 0 and b0[1] >= b1[4] - 0.08 * a and b1[1] < b0[4] < mid1:
                hits["PIERCING_DARK_CLOUD"].append((i, -1))

        # 10. TWEEZER BOTTOM / TOP
        if body1 >= 0.35 * a and body0 >= 0.35 * a:
            if prior_down and s1 < 0 and s0 > 0 and abs(b0[3] - b1[3]) <= 0.12 * a:
                hits["TWEEZER_TOP_BOTTOM"].append((i, 1))
            elif prior_up and s1 > 0 and s0 < 0 and abs(b0[2] - b1[2]) <= 0.12 * a:
                hits["TWEEZER_TOP_BOTTOM"].append((i, -1))

        # 11. MORNING STAR / EVENING STAR (3-candle reversal)
        if body2 >= 0.6 * a and body1 <= 0.35 * rng1 and body0 >= 0.50 * rng0:
            mid2 = 0.5 * (b2[1] + b2[4])
            prior_dn2 = b2[4] < ema20[i - 2] and b2[4] < bars[i - 5][4]
            prior_up2 = b2[4] > ema20[i - 2] and b2[4] > bars[i - 5][4]
            if prior_dn2 and s2 < 0 and s0 > 0 and b0[4] > mid2:
                hits["MORNING_EVENING_STAR"].append((i, 1))
            elif prior_up2 and s2 > 0 and s0 < 0 and b0[4] < mid2:
                hits["MORNING_EVENING_STAR"].append((i, -1))

        # 12. THREE INSIDE UP / DOWN (confirmed Harami)
        if body2 >= 0.6 * a and body1 <= 0.50 * body2:
            hi_b2, lo_b2 = max(b2[1], b2[4]), min(b2[1], b2[4])
            hi_b1, lo_b1 = max(b1[1], b1[4]), min(b1[1], b1[4])
            if hi_b1 <= hi_b2 and lo_b1 >= lo_b2:
                if s2 < 0 and s1 > 0 and s0 > 0 and b0[4] > b2[1]:
                    hits["THREE_INSIDE_UP_DOWN"].append((i, 1))
                elif s2 > 0 and s1 < 0 and s0 < 0 and b0[4] < b2[1]:
                    hits["THREE_INSIDE_UP_DOWN"].append((i, -1))

        # 13. MARUBOZU CONTINUATION
        if rng0 >= 1.0 * a and body0 >= 0.80 * rng0:
            if s0 > 0 and trend_up_now:
                hits["MARUBOZU_CONT"].append((i, 1))
            elif s0 < 0 and trend_dn_now:
                hits["MARUBOZU_CONT"].append((i, -1))

        # 14. THREE WHITE SOLDIERS / THREE BLACK CROWS
        if body0 >= 0.55 * rng0 and body1 >= 0.55 * rng1 and body2 >= 0.55 * rng2:
            if s2 > 0 and s1 > 0 and s0 > 0 and b0[4] > b1[4] > b2[4] and b0[1] >= b1[1] >= b2[1]:
                hits["THREE_SOLDIERS_CROWS"].append((i, 1))
            elif s2 < 0 and s1 < 0 and s0 < 0 and b0[4] < b1[4] < b2[4] and b0[1] <= b1[1] <= b2[1]:
                hits["THREE_SOLDIERS_CROWS"].append((i, -1))

        # 15. INSIDE BAR BREAKOUT IN TREND
        if b1[2] <= b2[2] and b1[3] >= b2[3] and rng2 >= 0.6 * a:
            if trend_up_now and s0 > 0 and b0[4] > b2[2]:
                hits["INSIDE_BAR_BREAKOUT"].append((i, 1))
            elif trend_dn_now and s0 < 0 and b0[4] < b2[3]:
                hits["INSIDE_BAR_BREAKOUT"].append((i, -1))

        # 16. RISING / FALLING THREE METHODS (4-bar pullback resumption)
        if body3 >= 0.7 * a and body0 >= 0.55 * rng0:
            inside_range = (
                max(b2[2], b1[2]) <= b3[2] + 0.10 * a
                and min(b2[3], b1[3]) >= b3[3] - 0.10 * a
            )
            if inside_range:
                if s3 > 0 and s2 <= 0 and s1 <= 0 and s0 > 0 and b0[4] > b3[4]:
                    hits["RISING_FALLING_THREE"].append((i, 1))
                elif s3 < 0 and s2 >= 0 and s1 >= 0 and s0 < 0 and b0[4] < b3[4]:
                    hits["RISING_FALLING_THREE"].append((i, -1))

        # 17. OUTSIDE MOMENTUM CONTINUATION
        if b0[2] > b1[2] and b0[3] < b1[3] and body0 >= 0.60 * rng0 and rng0 >= 0.8 * a:
            if s0 > 0 and trend_up_now:
                hits["OUTSIDE_MOMENTUM_CONT"].append((i, 1))
            elif s0 < 0 and trend_dn_now:
                hits["OUTSIDE_MOMENTUM_CONT"].append((i, -1))

        # 18. TWO-BAR THRUST CONTINUATION
        if body0 >= 0.65 * rng0 and body1 >= 0.65 * rng1 and rng0 >= 0.6 * a and rng1 >= 0.6 * a:
            if s0 > 0 and s1 > 0 and b0[4] > b1[4] and trend_up_now:
                hits["TWO_BAR_THRUST_CONT"].append((i, 1))
            elif s0 < 0 and s1 < 0 and b0[4] < b1[4] and trend_dn_now:
                hits["TWO_BAR_THRUST_CONT"].append((i, -1))

    return hits


# ---------------------------------------------------------------------------
# Predictive Horizon Evaluator (1-bar & 2-bar Hit Rate, Z-Score, Net After Spread)
# ---------------------------------------------------------------------------

def evaluate_signal_predictions(
    bars: list[list[float]],
    signals: list[tuple[int, int]],
    *,
    spread_px: float,
    split_idx: int,
) -> dict[str, Any]:
    """Evaluate next-1-candle and next-2-candle causal accuracy & net edge (Train vs Blind OOS)."""
    def _bucket_stats(subset: list[tuple[int, int]], horizon: int) -> dict[str, Any]:
        gross_moves: list[float] = []
        net_moves: list[float] = []
        wins_dir = 0
        wins_net = 0
        for idx, direction in subset:
            exit_idx = idx + horizon
            if idx + 1 >= len(bars) or exit_idx >= len(bars):
                continue
            entry = bars[idx + 1][1]
            exit_px = bars[exit_idx][4]
            gross = (exit_px - entry) * direction
            net = gross - spread_px
            gross_moves.append(gross)
            net_moves.append(net)
            if gross > 0:
                wins_dir += 1
            if net > 0:
                wins_net += 1
        n = len(net_moves)
        if n == 0:
            return {
                "n": 0, "hit_dir_pct": None, "hit_net_pct": None,
                "avg_gross": 0.0, "avg_net": 0.0, "z_dir": 0.0, "t_net": 0.0,
            }
        hit_dir = wins_dir / n
        hit_net = wins_net / n
        avg_g = sum(gross_moves) / n
        avg_n = sum(net_moves) / n
        z_dir = (wins_dir - 0.5 * n) / math.sqrt(0.25 * n)
        if n >= 2:
            var_n = sum((x - avg_n) ** 2 for x in net_moves) / (n - 1)
            se_n = math.sqrt(max(var_n, 1e-18) / n)
            t_net = avg_n / se_n if se_n > 0 else 0.0
        else:
            t_net = 0.0
        return {
            "n": n,
            "hit_dir_pct": round(hit_dir * 100.0, 2),
            "hit_net_pct": round(hit_net * 100.0, 2),
            "avg_gross": round(avg_g, 5),
            "avg_net": round(avg_n, 5),
            "z_dir": round(z_dir, 2),
            "t_net": round(t_net, 2),
        }

    train_sigs = [(i, d) for i, d in signals if i < split_idx]
    test_sigs = [(i, d) for i, d in signals if i >= split_idx]
    return {
        "n_total": len(signals),
        "n_train": len(train_sigs),
        "n_test": len(test_sigs),
        "h1_all": _bucket_stats(signals, 1),
        "h2_all": _bucket_stats(signals, 2),
        "h1_train": _bucket_stats(train_sigs, 1),
        "h1_test": _bucket_stats(test_sigs, 1),
        "h2_train": _bucket_stats(train_sigs, 2),
        "h2_test": _bucket_stats(test_sigs, 2),
    }


# ---------------------------------------------------------------------------
# Causal 1-Position Trade Simulator for 1-Bar / 2-Bar Candlestick Signals
# ---------------------------------------------------------------------------

def simulate_pattern_trades(
    bars: list[list[float]],
    atr: list[float],
    signals: list[tuple[int, int]],
    *,
    horizon: int,
    spread_px: float,
    mult: float,
    lot: float = 0.01,
    commission_per_lot: float = 0.0,
    stop_atr_mult: float = 2.0,
    tp_atr_mult: float | None = None,
) -> list[dict[str, Any]]:
    """Simulate non-overlapping causal trades triggered on bar i close, entering bar i+1 open."""
    sig_map: dict[int, int] = {}
    for idx, d in signals:
        if idx not in sig_map:
            sig_map[idx] = d

    trades: list[dict[str, Any]] = []
    half_sp = spread_px * 0.5
    comm_usd = commission_per_lot * lot
    pos: dict[str, Any] | None = None

    for i in range(1, len(bars)):
        ts, o, h, lo, c, _v = bars[i]

        # Open new position on bar i open if bar i-1 fired a signal and we are flat
        if pos is None and (i - 1) in sig_map:
            d = sig_map[i - 1]
            a = atr[i - 1]
            if a > 0:
                entry = (o + half_sp) if d > 0 else (o - half_sp)
                stop_dist = stop_atr_mult * a
                sl = (entry - stop_dist) if d > 0 else (entry + stop_dist)
                tp = None
                if tp_atr_mult is not None and tp_atr_mult > 0:
                    tp = (entry + tp_atr_mult * a) if d > 0 else (entry - tp_atr_mult * a)
                pos = {
                    "side": "BUY" if d > 0 else "SELL",
                    "dir": d,
                    "entry": entry,
                    "entry_ts": ts,
                    "entry_idx": i,
                    "sl": sl,
                    "tp": tp,
                    "risk_usd": stop_dist * mult * lot,
                }

        if pos is None:
            continue

        d = pos["dir"]
        sl_hit = (lo - half_sp <= pos["sl"]) if d > 0 else (h + half_sp >= pos["sl"])
        tp_hit = False
        if pos["tp"] is not None:
            tp_hit = (h - half_sp >= pos["tp"]) if d > 0 else (lo + half_sp <= pos["tp"])

        bars_held = i - pos["entry_idx"] + 1
        exit_px: float | None = None
        reason = ""
        if sl_hit:
            exit_px = pos["sl"]
            reason = "SL"
        elif tp_hit:
            exit_px = pos["tp"]
            reason = "TP"
        elif bars_held >= horizon:
            exit_px = (c - half_sp) if d > 0 else (c + half_sp)
            reason = f"HOLD_{horizon}B"

        if exit_px is not None:
            pnl = (exit_px - pos["entry"]) * d * mult * lot - comm_usd
            trades.append({
                "entry_ts": pos["entry_ts"],
                "exit_ts": ts,
                "side": pos["side"],
                "pnl": round(pnl, 4),
                "risk_usd": round(pos["risk_usd"], 4),
                "reason": reason,
            })
            pos = None

    return trades


# ---------------------------------------------------------------------------
# Part B: Strict Train -> Blind OOS Candlestick Pattern Miner
# ---------------------------------------------------------------------------

def mine_candlestick_patterns_oos(
    bars: list[list[float]],
    ema20: list[float],
    atr: list[float],
    *,
    spread_px: float,
    mult: float,
    split_idx: int,
    min_train_n: int = 40,
    min_test_n: int = 20,
    mc_trials: int = 400,
) -> dict[str, Any]:
    """Exhaustively mine human-readable 1/2/3-candle patterns on Train (first 50%) ONLY,
    then evaluate locked winners BLIND on Test OOS (second 50%).
    """
    n = len(bars)
    if n < 120:
        return {"candidates_tested": 0, "train_passed": 0, "oos_verified": [], "top_train_preview": []}

    shapes = [_bar_shape_label(b) for b in bars]
    geoms = [_bar_geom(b) for b in bars]
    hours = [datetime.fromtimestamp(int(b[0]), tz=timezone.utc).hour for b in bars]
    is_intraday = (len(bars) >= 2) and ((bars[1][0] - bars[0][0]) < 14400)

    # Collect causal candidate signal buckets: key -> [(bar_idx, dir), ...]
    # Every structural pattern is tested in BOTH follow (+1) and fade (-1) semantics via explicit names!
    buckets: dict[str, list[tuple[int, int]]] = {}

    def _add(name: str, idx: int, direction: int) -> None:
        if name not in buckets:
            buckets[name] = []
        buckets[name].append((idx, direction))

    for i in range(25, n - 2):
        a = atr[i]
        if a <= 0:
            continue
        rng0, body0, up0, lo0, s0 = geoms[i]
        rng1, _body1, _up1, _lo1, s1 = geoms[i - 1]
        _rng2, _body2, _up2, _lo2, s2 = geoms[i - 2]

        sh0, sh1 = shapes[i], shapes[i - 1]
        stretch = (bars[i][4] - ema20[i]) / a
        size0 = "BIG" if rng0 >= 1.4 * a else ("TIGHT" if rng0 <= 0.6 * a else "MID")
        hr = hours[i]
        sess = "ASIA" if hr < 7 else ("LDN" if hr < 13 else ("NY" if hr < 20 else "LATE"))

        # --- Family 1: 2-Candle Morphology Pairs (tested as BULL=+1 and BEAR=-1 separately) ---
        pair_key = f"2C:{sh1}->{sh0}"
        _add(f"{pair_key}|BUY", i, 1)
        _add(f"{pair_key}|SELL", i, -1)

        # --- Family 2: Symmetric 2-Candle & 3-Candle Color Streaks (Follow vs Fade) ---
        if s0 != 0 and s1 == s0 and s2 == s0:
            _add("STREAK3_SAME|FOLLOW", i, s0)
            _add("STREAK3_SAME|FADE", i, -s0)
            if rng0 >= 1.2 * a:
                _add("STREAK3_BIG_LAST|FOLLOW", i, s0)
                _add("STREAK3_BIG_LAST|FADE", i, -s0)
            if i >= 26 and geoms[i - 3][4] == s0:
                _add("STREAK4_SAME|FOLLOW", i, s0)
                _add("STREAK4_SAME|FADE", i, -s0)

        # 2 same then 1 opposite flip (micro pullback vs reversal)
        if s1 != 0 and s2 == s1 and s0 == -s1:
            _add("2SAME_1FLIP|FOLLOW_FLIP", i, s0)
            _add("2SAME_1FLIP|FADE_FLIP", i, -s0)

        # --- Family 3: Stretch from EMA20 + Candle Shape (Overextended Mean-Rev vs Momentum) ---
        if stretch >= 1.5:
            _add(f"STRETCH_HI+{sh0}|FADE", i, -1)
            _add(f"STRETCH_HI+{sh0}|FOLLOW", i, 1)
            if size0 == "BIG" and s0 > 0:
                _add("CLIMAX_BIG_UP_STRETCH|FADE", i, -1)
                _add("CLIMAX_BIG_UP_STRETCH|FOLLOW", i, 1)
        elif stretch <= -1.5:
            _add(f"STRETCH_LO+{sh0}|FADE", i, 1)
            _add(f"STRETCH_LO+{sh0}|FOLLOW", i, -1)
            if size0 == "BIG" and s0 < 0:
                _add("CLIMAX_BIG_DN_STRETCH|FADE", i, 1)
                _add("CLIMAX_BIG_DN_STRETCH|FOLLOW", i, -1)

        # Symmetric Combined Stretch Fade/Follow (bull & bear pooled for 2x sample power!)
        if stretch >= 1.5 and sh0 == "UPPER_WICK":
            _add("EXTREME_WICK_REJECT|FADE", i, -1)
        elif stretch <= -1.5 and sh0 == "LOWER_WICK":
            _add("EXTREME_WICK_REJECT|FADE", i, 1)

        if stretch >= 1.5 and sh0 == "STRONG_UP" and size0 == "BIG":
            _add("EXTREME_CLIMAX_BAR|FADE", i, -1)
            _add("EXTREME_CLIMAX_BAR|FOLLOW", i, 1)
        elif stretch <= -1.5 and sh0 == "STRONG_DN" and size0 == "BIG":
            _add("EXTREME_CLIMAX_BAR|FADE", i, 1)
            _add("EXTREME_CLIMAX_BAR|FOLLOW", i, -1)

        if stretch >= 1.2 and s1 > 0 and s0 < 0 and body0 >= 0.55 * rng0:
            _add("STRETCH_REVERSAL_ENGULF_LIKE|FADE", i, -1)
        elif stretch <= -1.2 and s1 < 0 and s0 > 0 and body0 >= 0.55 * rng0:
            _add("STRETCH_REVERSAL_ENGULF_LIKE|FADE", i, 1)

        # --- Family 4: 20-Bar High/Low Breakout vs Fakeout Rejection ---
        hi20 = max(bars[k][2] for k in range(i - 20, i))
        lo20 = min(bars[k][3] for k in range(i - 20, i))
        if bars[i][2] > hi20 and bars[i][4] < hi20 and up0 >= 0.40 * rng0:
            _add("FAKEOUT_20B_SWEEP|FADE", i, -1)
            _add("FAKEOUT_20B_SWEEP|FOLLOW", i, 1)
        elif bars[i][3] < lo20 and bars[i][4] > lo20 and lo0 >= 0.40 * rng0:
            _add("FAKEOUT_20B_SWEEP|FADE", i, 1)
            _add("FAKEOUT_20B_SWEEP|FOLLOW", i, -1)

        if bars[i][4] > hi20 and sh0 == "STRONG_UP":
            _add("CLEAN_20B_BREAKOUT|FOLLOW", i, 1)
            _add("CLEAN_20B_BREAKOUT|FADE", i, -1)
        elif bars[i][4] < lo20 and sh0 == "STRONG_DN":
            _add("CLEAN_20B_BREAKOUT|FOLLOW", i, -1)
            _add("CLEAN_20B_BREAKOUT|FADE", i, 1)

        # --- Family 5: Session-Specific Symmetric Wick / Momentum (intraday M5/M15/H1 only) ---
        if is_intraday:
            if sh0 == "LOWER_WICK" and rng0 >= 0.8 * a:
                _add(f"SESS_{sess}:WICK_REJECT|FADE", i, 1)
            elif sh0 == "UPPER_WICK" and rng0 >= 0.8 * a:
                _add(f"SESS_{sess}:WICK_REJECT|FADE", i, -1)

            if sh0 == "STRONG_UP" and size0 == "BIG":
                _add(f"SESS_{sess}:BIG_MOMENTUM|FOLLOW", i, 1)
                _add(f"SESS_{sess}:BIG_MOMENTUM|FADE", i, -1)
            elif sh0 == "STRONG_DN" and size0 == "BIG":
                _add(f"SESS_{sess}:BIG_MOMENTUM|FOLLOW", i, -1)
                _add(f"SESS_{sess}:BIG_MOMENTUM|FADE", i, 1)

        # --- Family 6: Exhaustion Volume + Small Range (VSA Stopping Bar symmetric) ---
        if i >= 25:
            med_v = sorted(bars[k][5] for k in range(i - 20, i))[10]
            if med_v > 0 and bars[i][5] >= 1.6 * med_v and rng0 <= 0.75 * rng1 and s1 != 0:
                _add("VSA_ABSORB_HIGHVOL_NARROW|FADE_PREV", i, -s1)
                _add("VSA_ABSORB_HIGHVOL_NARROW|FOLLOW_PREV", i, s1)

    # Evaluate every (candidate, horizon in {1, 2}) strictly on TRAIN first
    candidates_tested = 0
    train_winners: list[dict[str, Any]] = []
    oos_verified: list[dict[str, Any]] = []

    for pat_name, sigs in buckets.items():
        if len(sigs) < (min_train_n + min_test_n):
            continue
        ev = evaluate_signal_predictions(bars, sigs, spread_px=spread_px, split_idx=split_idx)
        for h_key, horizon in (("h1", 1), ("h2", 2)):
            candidates_tested += 1
            tr = ev[f"{h_key}_train"]
            te = ev[f"{h_key}_test"]
            if tr["n"] < min_train_n:
                continue
            # Train selection threshold: statistically positive NET after spread
            if tr["avg_net"] > 0 and tr["t_net"] >= 1.80:
                rec: dict[str, Any] = {
                    "pattern": pat_name,
                    "horizon": horizon,
                    "train": tr,
                    "test_oos": te,
                    "all": ev[f"{h_key}_all"],
                    "oos_passed": False,
                }
                # Blind OOS verification
                if te["n"] >= min_test_n and te["avg_net"] > 0 and te["t_net"] >= 1.0:
                    # Run full causal trade sim + stress test!
                    base_tr = simulate_pattern_trades(
                        bars, atr, sigs, horizon=horizon, spread_px=spread_px, mult=mult,
                    )
                    stress_tr = simulate_pattern_trades(
                        bars, atr, sigs, horizon=horizon,
                        spread_px=spread_px * 1.5, mult=mult, commission_per_lot=5.0,
                    )
                    tr_train_trades = [t for t in base_tr if t["entry_ts"] < bars[split_idx][0]]
                    tr_test_trades = [t for t in base_tr if t["entry_ts"] >= bars[split_idx][0]]
                    s_all = _summarize_trades(base_tr)
                    s_train = _summarize_trades(tr_train_trades)
                    s_test = _summarize_trades(tr_test_trades)
                    s_stress = _summarize_trades(stress_tr)
                    s_all["mc_p95_dd"] = _mc_p95_dd(base_tr, trials=mc_trials, seed=42)
                    rec["trades_all"] = s_all
                    rec["trades_train"] = s_train
                    rec["trades_test_oos"] = s_test
                    rec["trades_stress"] = s_stress
                    if (
                        s_test["net"] > 0
                        and s_all["pf"] is not None
                        and s_all["pf"] >= 1.15
                        and s_all["excl_best"] > 0
                        and s_stress["net"] > 0
                    ):
                        rec["oos_passed"] = True
                        oos_verified.append(rec)
                train_winners.append(rec)

    train_winners.sort(key=lambda r: (r["oos_passed"], r["test_oos"]["t_net"], r["train"]["t_net"]), reverse=True)
    oos_verified.sort(
        key=lambda r: (
            r["trades_all"]["p_dd"] if r["trades_all"]["p_dd"] is not None else -999.0,
            r["test_oos"]["t_net"],
        ),
        reverse=True,
    )
    return {
        "candidates_tested": candidates_tested,
        "train_passed": len(train_winners),
        "oos_verified_count": len(oos_verified),
        "oos_verified": oos_verified[:10],
        "top_train_preview": train_winners[:12],
    }


# ---------------------------------------------------------------------------
# Full Timeframe Evaluator (Classic 18 Patterns + OOS Pattern Miner)
# ---------------------------------------------------------------------------

def evaluate_timeframe_candlesticks(
    *,
    tf_label: str,
    bars: list[list[float]],
    spec: dict[str, Any],
    spread_px: float,
    min_train_n: int = 40,
    min_test_n: int = 20,
    mc_trials: int = 400,
) -> dict[str, Any]:
    """Evaluate all 18 classic candlestick patterns + OOS pattern miner on one timeframe."""
    basis_name, mult_val = resolve_pricing_basis(spec, "auto")
    mult = float(mult_val)

    flat_bars = clean_ohlcv_bars(bars)
    if len(flat_bars) < 80:
        return {"timeframe": tf_label, "bars": len(flat_bars), "error": "insufficient_bars"}

    closes = [b[4] for b in flat_bars]
    ema20 = _ema(closes, 20)
    atr = _atr14(flat_bars, 14)
    split_idx = len(flat_bars) // 2
    span_days = (flat_bars[-1][0] - flat_bars[0][0]) / 86400.0

    # Baseline unconditional 1-bar & 2-bar up-rate (for honest comparison vs coin flip / drift)
    up1 = sum(1 for i in range(len(flat_bars) - 1) if flat_bars[i + 1][4] > flat_bars[i + 1][1])
    base_up_pct = round(100.0 * up1 / max(1, len(flat_bars) - 1), 2)
    med_atr = sorted(atr[20:])[len(atr[20:]) // 2]

    # Part A: Classic 18 named patterns
    classic_hits = detect_classic_patterns(flat_bars, ema20, atr)
    classic_rows: list[dict[str, Any]] = []
    for pat_name, kind, fa_label in CLASSIC_PATTERN_META:
        sigs = classic_hits[pat_name]
        ev = evaluate_signal_predictions(flat_bars, sigs, spread_px=spread_px, split_idx=split_idx)

        # Also simulate 1-bar & 2-bar causal trades for every pattern with >= 15 total occurrences
        tr_1b_summary = None
        tr_1b_stress = None
        tr_1b_oos = None
        tr_2b_summary = None
        tr_2b_stress = None
        tr_2b_oos = None
        if len(sigs) >= 15:
            t1 = simulate_pattern_trades(flat_bars, atr, sigs, horizon=1, spread_px=spread_px, mult=mult)
            t1_st = simulate_pattern_trades(
                flat_bars, atr, sigs, horizon=1, spread_px=spread_px * 1.5, mult=mult, commission_per_lot=5.0,
            )
            t1_h1 = [t for t in t1 if t["entry_ts"] < flat_bars[split_idx][0]]
            t1_h2 = [t for t in t1 if t["entry_ts"] >= flat_bars[split_idx][0]]
            t2 = simulate_pattern_trades(flat_bars, atr, sigs, horizon=2, spread_px=spread_px, mult=mult)
            t2_st = simulate_pattern_trades(
                flat_bars, atr, sigs, horizon=2, spread_px=spread_px * 1.5, mult=mult, commission_per_lot=5.0,
            )
            t2_h1 = [t for t in t2 if t["entry_ts"] < flat_bars[split_idx][0]]
            t2_h2 = [t for t in t2 if t["entry_ts"] >= flat_bars[split_idx][0]]
            tr_1b_summary = _summarize_trades(t1)
            tr_1b_stress = _summarize_trades(t1_st)
            tr_1b_oos = {"h1_net": _summarize_trades(t1_h1)["net"], "h2_net": _summarize_trades(t1_h2)["net"]}
            tr_2b_summary = _summarize_trades(t2)
            tr_2b_stress = _summarize_trades(t2_st)
            tr_2b_oos = {"h1_net": _summarize_trades(t2_h1)["net"], "h2_net": _summarize_trades(t2_h2)["net"]}

        classic_rows.append({
            "pattern": pat_name,
            "kind": kind,
            "fa": fa_label,
            "pred": ev,
            "trades_1bar": tr_1b_summary,
            "trades_1bar_stress": tr_1b_stress,
            "trades_1bar_split": tr_1b_oos,
            "trades_2bar": tr_2b_summary,
            "trades_2bar_stress": tr_2b_stress,
            "trades_2bar_split": tr_2b_oos,
        })

    # Sort classic patterns by best 2-bar or 1-bar net expectancy
    classic_rows.sort(
        key=lambda r: max(
            r["pred"]["h1_all"]["t_net"] if r["pred"]["n_total"] >= 15 else -99.0,
            r["pred"]["h2_all"]["t_net"] if r["pred"]["n_total"] >= 15 else -99.0,
        ),
        reverse=True,
    )

    # Part B: Strict Train -> Blind OOS Pattern Miner
    miner = mine_candlestick_patterns_oos(
        flat_bars,
        ema20,
        atr,
        spread_px=spread_px,
        mult=mult,
        split_idx=split_idx,
        min_train_n=min_train_n,
        min_test_n=min_test_n,
        mc_trials=mc_trials,
    )

    return {
        "timeframe": tf_label,
        "bars": len(flat_bars),
        "span_days": round(span_days, 1),
        "median_atr": round(med_atr, 5),
        "spread_px": round(spread_px, 5),
        "spread_pct_of_atr": round(100.0 * spread_px / max(med_atr, 1e-12), 2),
        "baseline_green_bar_pct": base_up_pct,
        "pricing_basis": basis_name,
        "mult": round(mult, 4),
        "classic_patterns": classic_rows,
        "miner": miner,
    }


def make_synthetic_bars_with_embedded_edge(
    *,
    n_bars: int = 1200,
    bar_seconds: int = 3600,
    seed: int = 7,
) -> list[list[float]]:
    """Generate deterministic mock bars with an embedded wick-rejection edge for offline tests."""
    rng = random.Random(seed)
    px = 3000.0
    t0 = 1700000000
    bars: list[list[float]] = []
    next_boost = 0.0
    for i in range(n_bars):
        o = px
        drift = next_boost
        next_boost = 0.0
        # Every ~20 bars inject a clean lower-wick rejection followed by a 2-bar bounce
        if i % 23 == 10:
            lo = o - 12.0
            h = o + 2.0
            c = o + 1.0
            next_boost = 5.5
        elif i % 23 == 20:
            h = o + 12.0
            lo = o - 2.0
            c = o - 1.0
            next_boost = -5.5
        else:
            step = drift + rng.gauss(0.0, 3.2)
            c = o + step
            h = max(o, c) + abs(rng.gauss(1.4, 0.7))
            lo = min(o, c) - abs(rng.gauss(1.4, 0.7))
        bars.append([float(t0 + i * bar_seconds), round(o, 2), round(h, 2), round(lo, 2), round(c, 2), float(100 + (i % 50))])
        px = c
    return bars
