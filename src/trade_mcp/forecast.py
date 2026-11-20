"""Read-only volatility scenarios, not calibrated forecasts or order signals."""
import math
import time

from .engines.technical import validate_ohlcv

STEPS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600}


def hourly_scenarios(bars, timeframe="M15", *, now=None):
    now = time.time() if now is None else now
    if timeframe not in STEPS:
        return {"available": False, "reason": "use_M1_to_H1", "execution_eligible": False}
    step = STEPS[timeframe]
    rows = validate_ohlcv([[b['time'], b['open'], b['high'], b['low'],
                            b['close'], b.get('tick_volume', 0)] for b in bars])
    closed = [r for r in rows if r[0] + step <= now]
    base = {"execution_eligible": False, "calibrated": False,
            "method": "independent_zero_drift_log_volatility_scenarios",
            "warning": "سناریوی نوسان؛ نه پیش‌بینی قطعی، احتمال کالیبره‌شده یا سیگنال سفارش"}
    if len(closed) < 31:
        return {**base, "available": False, "reason": "insufficient_closed_bars"}
    last = closed[-1]
    if now - (last[0] + step) > 2 * step:
        return {**base, "available": False, "reason": "stale_closed_bars"}
    sample = closed[-31:]
    returns = [math.log(b[4] / a[4]) for a, b in zip(sample, sample[1:], strict=False)]
    mean = sum(returns) / len(returns)
    sigma = math.sqrt(sum((r - mean)**2 for r in returns) / (len(returns) - 1))
    points = []
    for minutes in (15, 30, 45, 60):
        width = min(sigma * math.sqrt(minutes * 60 / step), 50)
        points.append({"time": now + minutes * 60, "minutes_ahead": minutes,
                       "center": last[4], "lower": last[4] * math.exp(-width),
                       "upper": last[4] * math.exp(width)})
    return {**base, "available": True, "as_of": now,
            "last_closed_bar_time": last[0], "points": points}
