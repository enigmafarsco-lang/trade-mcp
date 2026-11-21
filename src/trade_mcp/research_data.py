"""Redacted historical research export: no accounts, quotes, journal or order methods."""
import math

from .engines.technical import validate_ohlcv


def closed_m1_segment(bars, *, now, minimum=420, maximum=1000):
    if type(minimum) is not int or type(maximum) is not int or not 420 <= minimum <= maximum <= 5000:
        raise ValueError('Require 420..5000 historical M1 bars')
    rows = []
    seen = set()
    for bar in bars:
        stamp = bar.get('time_utc')
        if isinstance(stamp, bool) or stamp is None or not math.isfinite(float(stamp)):
            raise ValueError('Verified UTC history required; never substitute workstation timezone')
        stamp = float(stamp)
        if stamp % 60 or stamp in seen:
            raise ValueError('Non-minute or duplicate history timestamp')
        seen.add(stamp)
        if stamp+60 > now:
            continue
        rows.append([stamp, bar['open'], bar['high'], bar['low'], bar['close'],
                     bar.get('tick_volume', 0)])
    rows = validate_ohlcv(sorted(rows, key=lambda r: r[0]))
    segments = []
    current = []
    for row in rows:
        if current and row[0]-current[-1][0] != 60:
            segments.append(current)
            current = []
        current.append(row)
    segments.append(current)
    eligible = [s for s in segments if len(s) >= minimum]
    if not eligible:
        raise ValueError('No sufficiently long contiguous closed M1 segment; no gap filling')
    # Most recent eligible continuous segment, selected before seeing any forecasts.
    return eligible[-1][-maximum:]
