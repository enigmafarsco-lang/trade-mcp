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


def _trend_price(closes, horizon):
    """32-bar OLS log-price slope, anchored to the last observed close."""
    values = [math.log(v) for v in closes[-32:]]
    mid = (len(values)-1)/2
    slope = sum((i-mid)*v for i, v in enumerate(values)) / sum(
        (i-mid)**2 for i in range(len(values)))
    exponent = slope*horizon
    if abs(exponent) > 2:
        raise ValueError('Unstable trend extrapolation')
    return closes[-1]*math.exp(exponent)


def trend_forecast(bars, timeframe='M15', *, now=None):
    """Experimental point forecast with rolling-origin errors, never an order signal."""
    now = time.time() if now is None else now
    base = {'available': False, 'execution_eligible': False,
            'method': 'independent_32_bar_log_ols_anchored_trend',
            'source': 'closed_broker_bars_not_llm', 'validated_for_trading': False}
    if timeframe not in {'M1', 'M5', 'M15'}:
        return {**base, 'reason': 'use_M1_M5_M15_for_hourly_forecast'}
    step = STEPS[timeframe]
    rows = validate_ohlcv([[b['time'], b['open'], b['high'], b['low'],
                            b['close'], b.get('tick_volume', 0)] for b in bars])
    rows = [r for r in rows if r[0]+step <= now]
    max_horizon = 3600//step
    if len(rows) < 32+max_horizon+20:
        return {**base, 'reason': 'insufficient_walk_forward_history'}
    if now-(rows[-1][0]+step) > 2*step:
        return {**base, 'reason': 'stale_closed_bars'}
    # Gaps invalidate equal-spacing extrapolation, rather than inventing bars.
    sample = rows[-(32+max_horizon+20):]
    if any(b[0]-a[0] != step for a, b in zip(sample, sample[1:], strict=False)):
        return {**base, 'reason': 'non_contiguous_history'}
    closes = [r[4] for r in rows]
    points = []
    try:
        for minutes in (15, 30, 60):
            horizon = minutes*60//step
            errors, baseline, hits = [], [], []
            for origin in range(len(closes)-horizon-20, len(closes)-horizon):
                prediction = _trend_price(closes[:origin+1], horizon)
                actual, current = closes[origin+horizon], closes[origin]
                errors.append(abs(prediction-actual))
                baseline.append(abs(current-actual))
                hits.append((prediction-current)*(actual-current) > 0)
            prediction = _trend_price(closes, horizon)
            points.append({'minutes_ahead': minutes,
                           'time': rows[-1][0]+step+minutes*60,
                           'price': prediction,
                           'direction': 'up' if prediction > closes[-1] else
                           'down' if prediction < closes[-1] else 'flat',
                           'evaluation_samples': 20, 'mae': sum(errors)/20,
                           'no_change_mae': sum(baseline)/20,
                           'direction_accuracy': sum(hits)/20})
    except (ValueError, OverflowError):
        return {**base, 'reason': 'unstable_extrapolation'}
    # A failed historical baseline comparison must not look like a usable forecast.
    # Passing this small diagnostic is still NOT trading validation.
    if any(p['mae'] >= p['no_change_mae'] or p['direction_accuracy'] <= .5 for p in points):
        return {**base, 'reason': 'failed_historical_baseline_check',
                'diagnostics': [{k: p[k] for k in ('minutes_ahead', 'evaluation_samples',
                    'mae', 'no_change_mae', 'direction_accuracy')} for p in points]}
    return {**base, 'available': True, 'as_of': rows[-1][0]+step,
            'last_closed_bar_time': rows[-1][0], 'anchor_price': closes[-1],
            'points': points, 'warning': 'پیش‌بینی آماری آزمایشی؛ خطا دارد و سیگنال سفارش نیست'}
