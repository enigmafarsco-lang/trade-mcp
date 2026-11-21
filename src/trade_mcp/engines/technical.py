"""Validated OHLCV technical features with explicit implementation provenance."""
import math

from .. import indicators


def validate_ohlcv(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise ValueError('Provide 1 to 10000 OHLCV rows')
    clean = []
    previous = -math.inf
    for row in rows:
        if len(row) != 6 or any(isinstance(v, bool) for v in row):
            raise ValueError('Each OHLCV row must be [timestamp,open,high,low,close,volume]')
        values = [float(v) for v in row]
        t, o, h, lo, c, v = values
        if not all(math.isfinite(x) for x in values):
            raise ValueError('OHLCV values must be finite')
        if t <= previous or t < 0 or min(o, h, lo, c) <= 0 or v < 0:
            raise ValueError('Invalid timestamp, price or volume')
        if h < max(o, c, lo) or lo > min(o, c, h):
            raise ValueError('Invalid candle bounds')
        previous = t
        clean.append(values)
    return clean


def macd(closes):
    fast, slow = indicators.ema(closes, 12), indicators.ema(closes, 26)
    line = [None if a is None or b is None else a-b for a, b in zip(fast, slow, strict=True)]
    defined = [x for x in line if x is not None]
    signal = [None]*(len(line)-len(defined)) + indicators.ema(defined, 9)
    histogram = [None if a is None or b is None else a-b for a, b in zip(line, signal, strict=True)]
    return line, signal, histogram


def supertrend(highs, lows, closes, period=10, multiplier=3.0):
    """Independent Wilder-ATR band implementation; not vendored pandas-ta code."""
    atr = indicators.atr(highs, lows, closes, period)
    band, direction = [None]*len(closes), [None]*len(closes)
    upper = lower = None
    trend = 1
    for i, volatility in enumerate(atr):
        if volatility is None:
            continue
        mid = (highs[i]+lows[i])/2
        bu, bl = mid+multiplier*volatility, mid-multiplier*volatility
        if upper is None:
            upper, lower = bu, bl
        else:
            old_upper, old_lower = upper, lower
            upper = bu if bu < old_upper or closes[i-1] > old_upper else old_upper
            lower = bl if bl > old_lower or closes[i-1] < old_lower else old_lower
            if trend == -1 and closes[i] > old_upper:
                trend = 1
            elif trend == 1 and closes[i] < old_lower:
                trend = -1
        direction[i] = trend
        band[i] = lower if trend == 1 else upper
    return band, direction


def analyze_ohlcv(rows, provider='builtin'):
    rows = validate_ohlcv(rows)
    closes = [r[4] for r in rows]
    highs, lows = [r[2] for r in rows], [r[3] for r in rows]
    if provider == 'pandas_ta':
        # Explicit opt-in. Never silently substitutes a different implementation.
        import pandas as pd
        import pandas_ta as ta
        close, high, low = pd.Series(closes), pd.Series(highs), pd.Series(lows)
        def last(series):
            if series is None or len(series) == 0:
                return None
            value = float(series.iloc[-1])
            return value if math.isfinite(value) else None
        def column(frame, name):
            return last(frame[name]) if frame is not None and name in frame else None
        m = ta.macd(close, fast=12, slow=26, signal=9, talib=False)
        st = ta.supertrend(high, low, close, length=10, multiplier=3.0)
        result = {'rsi14': last(ta.rsi(close, length=14, talib=False)),
                  'macd': column(m, 'MACD_12_26_9'),
                  'macd_signal': column(m, 'MACDs_12_26_9'),
                  'macd_histogram': column(m, 'MACDh_12_26_9'),
                  'supertrend': column(st, 'SUPERT_10_3.0'),
                  'supertrend_direction': column(st, 'SUPERTd_10_3.0')}
        result.update({f'ema{p}': last(ta.ema(close, length=p, talib=False)) for p in (20, 50, 200)})
    elif provider == 'builtin':
        m, sig, hist = macd(closes)
        st, direction = supertrend(highs, lows, closes)
        result = dict(rsi14=indicators.rsi(closes)[-1], macd=m[-1], macd_signal=sig[-1],
                      macd_histogram=hist[-1], supertrend=st[-1], supertrend_direction=direction[-1])
        result.update({f'ema{p}': indicators.ema(closes, p)[-1] for p in (20, 50, 200)})
    else:
        raise ValueError('Unknown indicator provider')
    # Shared dependency-free definitions, explicitly sourced separately when the
    # optional pandas-ta provider supplies the other indicators.
    upper, middle, lower = indicators.bollinger(closes, period=20, mult=2.0)
    result.update(atr14=indicators.atr(highs, lows, closes, 14)[-1],
                  bollinger_upper=upper[-1], bollinger_middle=middle[-1],
                  bollinger_lower=lower[-1])
    return dict(ok=True, provider=provider, bars=len(rows), last_timestamp=rows[-1][0],
                close=closes[-1], indicators=result,
                implementation='direct pandas_ta calls' if provider == 'pandas_ta' else
                'independent built-in formulas, not pandas-ta parity certified',
                additional_indicator_implementation='builtin Wilder ATR14 and population-std Bollinger20/2',
                input_contract='Caller supplies closed bars only; no live freshness claim',
                ready=all(v is not None for v in result.values()))


def candlestick_patterns(rows):
    """Call optional TA-Lib directly; no local model weights or approximation."""
    import numpy as np
    import talib
    data = validate_ohlcv(rows)
    o, h, lo, c = [np.array([r[i] for r in data], dtype=float) for i in (1, 2, 3, 4)]
    return {'ok': True, 'provider': 'TA-Lib', 'patterns': {
        'doji': int(talib.CDLDOJI(o, h, lo, c)[-1]),
        'hammer': int(talib.CDLHAMMER(o, h, lo, c)[-1]),
        'engulfing': int(talib.CDLENGULFING(o, h, lo, c)[-1])},
        'note': 'Signed TA-Lib pattern strength, not win probability'}
