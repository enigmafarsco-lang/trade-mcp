"""Read broker candles into the deterministic engine; never send an order."""
import math
import time

from ..config import TF_SECONDS
from .strategy import strategy_signal


def analyze_broker(mt5, cfg, symbol='', timeframe='M15', provider='builtin', now=None):
    timeframe = timeframe.upper()
    if timeframe not in TF_SECONDS or timeframe == 'MN1':
        raise ValueError('Unsupported fixed-duration timeframe')
    resolved = mt5.resolve_symbol(symbol or cfg.default_symbol)
    rates = mt5.candles(resolved, timeframe, count=401)
    if len(rates) > 401:
        raise ValueError('Broker returned excessive candles')
    now = time.time() if now is None else float(now)
    if not math.isfinite(now):
        raise ValueError('Invalid clock')
    duration = TF_SECONDS[timeframe]
    # Exclude the latest returned candle even if local time suggests closure:
    # terminal timezone/clock assumptions are not sufficient to trust that bar.
    timestamps = [float(row['time']) for row in rates]
    if any(not math.isfinite(t) for t in timestamps) or any(
            b <= a for a, b in zip(timestamps, timestamps[1:], strict=False)):
        raise ValueError('Broker candles must be ordered and unique')
    closed = [row for row in rates[:-1] if float(row['time'])+duration <= now]
    if not closed:
        return dict(ok=False, error='closed_candles_unavailable', execution_authorized=False)
    rows = [[row['time'], row['open'], row['high'], row['low'], row['close'],
             row['tick_volume']] for row in closed]
    result = strategy_signal(rows, provider)
    result.update(symbol=resolved, timeframe=timeframe, backend=cfg.backend,
                  source='mock_synthetic' if cfg.backend == 'mock' else 'broker_candles',
                  last_closed_bar_time=rows[-1][0],
                  closed_bar_age_seconds=max(0, now-(float(rows[-1][0])+duration)),
                  latest_returned_bar_excluded=True,
                  freshness_validated_for_execution=False)
    return result
