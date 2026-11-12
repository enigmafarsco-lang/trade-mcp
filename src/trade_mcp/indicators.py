"""Small, dependency-free technical indicators.

All functions take plain Python lists and return full-length lists aligned with
the input, using ``None`` where an indicator is not yet defined. That makes
"take the last value" and "compare with the value N bars ago" trivial.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

Number = float


def _clean(values: Iterable[float]) -> list[float]:
    return [float(v) for v in values]


def sma(values: Sequence[float], period: int) -> list[float | None]:
    vals = _clean(values)
    out: list[float | None] = [None] * len(vals)
    if period <= 0:
        return out
    window = 0.0
    for i, v in enumerate(vals):
        window += v
        if i >= period:
            window -= vals[i - period]
        if i >= period - 1:
            out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    vals = _clean(values)
    out: list[float | None] = [None] * len(vals)
    if period <= 0 or not vals:
        return out
    k = 2.0 / (period + 1.0)
    prev: float | None = None
    seed_sum = 0.0
    for i, v in enumerate(vals):
        if i < period - 1:
            seed_sum += v
            continue
        if prev is None:
            seed_sum += v
            prev = seed_sum / period
        else:
            prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def true_ranges(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    trs: list[float] = []
    prev_close: float | None = None
    for h, lo, c in zip(highs, lows, closes, strict=False):
        if prev_close is None:
            trs.append(float(h) - float(lo))
        else:
            trs.append(max(h - lo, abs(h - prev_close), abs(lo - prev_close)))
        prev_close = c
    return trs


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> list[float | None]:
    """Wilder's Average True Range."""
    trs = true_ranges(highs, lows, closes)
    out: list[float | None] = [None] * len(trs)
    if period <= 0 or len(trs) < period:
        return out
    seed = sum(trs[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    vals = _clean(values)
    out: list[float | None] = [None] * len(vals)
    if len(vals) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = vals[i] - vals[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(vals)):
        delta = vals[i] - vals[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    return out


def adx(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Wilder ADX with +DI / -DI."""
    n = len(closes)
    plus_dm: list[float] = [0.0] * n
    minus_dm: list[float] = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
    trs = true_ranges(highs, lows, closes)

    def wilder(series: Sequence[float]) -> list[float]:
        out = [0.0] * n
        if len(series) < period:
            return out
        acc = sum(series[1:period + 1])
        out[period] = acc
        for i in range(period + 1, n):
            acc = acc - acc / period + series[i]
            out[i] = acc
        return out

    tr_s = wilder(trs)
    plus_s = wilder(plus_dm)
    minus_s = wilder(minus_dm)
    plus_di: list[float | None] = [None] * n
    minus_di: list[float | None] = [None] * n
    dx: list[float | None] = [None] * n
    for i in range(n):
        if tr_s[i] > 0:
            pdi = 100.0 * plus_s[i] / tr_s[i]
            mdi = 100.0 * minus_s[i] / tr_s[i]
            plus_di[i] = pdi
            minus_di[i] = mdi
            total = pdi + mdi
            dx[i] = 100.0 * abs(pdi - mdi) / total if total else 0.0
    out: list[float | None] = [None] * n
    seed_idx = 2 * period - 1
    if seed_idx < n:
        window = [d for d in dx[period:seed_idx + 1] if d is not None]
        if window:
            prev = sum(window) / len(window)
            out[seed_idx] = prev
            for i in range(seed_idx + 1, n):
                if dx[i] is None:
                    continue
                prev = (prev * (period - 1) + dx[i]) / period
                out[i] = prev
    return out, plus_di, minus_di


def stdev(values: Sequence[float], period: int) -> list[float | None]:
    vals = _clean(values)
    out: list[float | None] = [None] * len(vals)
    for i in range(period - 1, len(vals)):
        window = vals[i - period + 1: i + 1]
        mean = sum(window) / period
        out[i] = math.sqrt(sum((v - mean) ** 2 for v in window) / period)
    return out


def bollinger(
    values: Sequence[float], period: int = 20, mult: float = 2.0
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    mid = sma(values, period)
    sd = stdev(values, period)
    upper = [None if (m is None or s is None) else m + mult * s for m, s in zip(mid, sd, strict=False)]
    lower = [None if (m is None or s is None) else m - mult * s for m, s in zip(mid, sd, strict=False)]
    return upper, mid, lower


def donchian(
    highs: Sequence[float], lows: Sequence[float], period: int = 20
) -> tuple[list[float | None], list[float | None]]:
    n = len(highs)
    upper: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    for i in range(period - 1, n):
        upper[i] = max(highs[i - period + 1: i + 1])
        lower[i] = min(lows[i - period + 1: i + 1])
    return upper, lower


def swings(
    highs: Sequence[float], lows: Sequence[float], left: int = 2, right: int = 2
) -> tuple[list[int], list[int]]:
    """Fractal swing points: indices of confirmed swing highs and lows."""
    sh: list[int] = []
    sl: list[int] = []
    n = len(highs)
    for i in range(left, n - right):
        window_h = highs[i - left: i + right + 1]
        window_l = lows[i - left: i + right + 1]
        if highs[i] == max(window_h) and highs[i] > highs[i - 1]:
            sh.append(i)
        if lows[i] == min(window_l) and lows[i] < lows[i - 1]:
            sl.append(i)
    return sh, sl


def slope(values: Sequence[float], period: int = 20) -> list[float | None]:
    """Normalised least-squares slope, expressed as % of price per bar."""
    vals = _clean(values)
    out: list[float | None] = [None] * len(vals)
    if period < 2:
        return out
    xs = list(range(period))
    mean_x = sum(xs) / period
    denom = sum((x - mean_x) ** 2 for x in xs)
    for i in range(period - 1, len(vals)):
        window = vals[i - period + 1: i + 1]
        mean_y = sum(window) / period
        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, window, strict=False))
        s = num / denom if denom else 0.0
        out[i] = (s / mean_y * 100.0) if mean_y else 0.0
    return out


def last_defined(series: Sequence[float | None], default: float | None = None) -> float | None:
    for value in reversed(series):
        if value is not None:
            return value
    return default


def percentile_rank(history: Sequence[float], value: float) -> float:
    """Where ``value`` sits inside ``history`` (0..1)."""
    clean = [h for h in history if h is not None]
    if not clean:
        return 0.5
    below = sum(1 for h in clean if h <= value)
    return below / len(clean)


def pct_change(a: float, b: float) -> float:
    if not b:
        return 0.0
    return (a - b) / b * 100.0
