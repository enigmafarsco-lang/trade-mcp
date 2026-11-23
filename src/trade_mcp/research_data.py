"""Redacted historical research export: no accounts, quotes, journal or order methods."""
import math

from .engines.technical import validate_ohlcv


class HistoryExportError(ValueError):
    """Safe fixed diagnostic; never includes broker payload or exception text."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def closed_m1_segment(bars, *, now, minimum=420, maximum=1000):
    if type(minimum) is not int or type(maximum) is not int or not 420 <= minimum <= maximum <= 5000:
        raise HistoryExportError('INVALID_LIMIT', 'Require 420..5000 historical M1 bars')
    if not isinstance(bars, list) or not bars:
        raise HistoryExportError('EMPTY_HISTORY', 'Broker returned no history rows')
    rows = []
    seen = set()
    for bar in bars:
        if not isinstance(bar, dict):
            raise HistoryExportError('INVALID_ROW', 'History row is not an object')
        stamp = bar.get('time_utc')
        try:
            valid_stamp = not isinstance(stamp, bool) and stamp is not None and math.isfinite(float(stamp))
        except (ValueError, TypeError, OverflowError):
            valid_stamp = False
        if not valid_stamp:
            raise HistoryExportError('UTC_UNVERIFIED', f'Verified UTC history required; received_rows={len(bars)}; never substitute workstation timezone')
        stamp = float(stamp)
        if stamp % 60 or stamp in seen:
            raise HistoryExportError('INVALID_TIMESTAMP', 'Non-minute or duplicate history timestamp')
        seen.add(stamp)
        if stamp+60 > now:
            continue
        if not all(k in bar for k in ('open', 'high', 'low', 'close')):
            raise HistoryExportError('INVALID_ROW', 'Missing OHLC fields')
        rows.append([stamp, bar['open'], bar['high'], bar['low'], bar['close'],
                     bar.get('tick_volume', 0)])
    if not rows:
        raise HistoryExportError('NO_CLOSED_ROWS', f'No closed rows; received_rows={len(bars)}')
    try:
        rows = validate_ohlcv(sorted(rows, key=lambda r: r[0]))
    except (ValueError, TypeError, OverflowError):
        raise HistoryExportError('INVALID_OHLCV', 'Invalid historical OHLCV; raw values withheld') from None
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
        raise HistoryExportError(
            'INSUFFICIENT_CONTIGUOUS_HISTORY',
            f'No sufficiently long contiguous closed M1 segment; received_rows={len(bars)}; '
            f'closed_rows={len(rows)}; longest_segment={max(map(len, segments))}; '
            f'required={minimum}; no gap filling',
        )
    # Most recent eligible continuous segment, selected before seeing any forecasts.
    return eligible[-1][-maximum:]


def broker_time_research_segment(bars, *, minimum=420, maximum=1000):
    """Keep broker wall-clock labels, NOT UTC. Successor proves each kept bar closed.

    No timezone inference and no live freshness claim. Discard latest broker bar;
    reject mixed/malformed/duplicate timestamps before selecting a continuous segment.
    """
    if not isinstance(bars, list) or len(bars) < 2:
        raise HistoryExportError('EMPTY_HISTORY', 'At least two broker bars required')
    converted = []
    seen = set()
    for bar in bars:
        try:
            stamp = bar['time']
            if isinstance(stamp, bool):
                raise ValueError
            stamp = float(stamp)
            if not math.isfinite(stamp) or stamp <= 0 or stamp % 60 or stamp in seen:
                raise ValueError
            seen.add(stamp)
            converted.append(dict(bar, time_utc=stamp))
        except (KeyError, TypeError, ValueError, OverflowError):
            raise HistoryExportError('INVALID_TIMESTAMP', 'Invalid broker wall-clock timestamp') from None
    converted.sort(key=lambda b: b['time_utc'])
    # Internal numeric validation only: these numbers remain broker labels, never UTC.
    return closed_m1_segment(converted[:-1], now=math.inf, minimum=minimum, maximum=maximum)
