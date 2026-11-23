"""Offline input validation/network guard. Does not implement a trading simulator."""
import hashlib
import json
import math
import socket
from contextlib import contextmanager
from pathlib import Path

START = 1767052800  # 2025-12-30 UTC, warmup
END = 1775001600  # 2026-04-01 UTC, exclusive


def validate_bundle(value):
    """A provenance declaration is recorded, not authenticated by this validator."""
    if not isinstance(value, dict) or set(value) != {'schema', 'provenance', 'market', 'candles'}:
        raise ValueError('Expected offline bundle schema, provenance, market and candles only')
    if value['schema'] != 'freqtrade-offline-bundle/v1':
        raise ValueError('Unsupported bundle schema')
    provenance = value['provenance']
    if (not isinstance(provenance, dict)
            or set(provenance) != {'exchange', 'source', 'captured_at', 'ccxt_version'}
            or provenance.get('exchange') != 'binance'
            or any(not isinstance(v, str) or not v.strip() for v in provenance.values())):
        raise ValueError('Real Binance source/capture/CCXT version declaration required')
    market = value['market']
    if (not isinstance(market, dict) or market.get('symbol') != 'BTC/USDT'
            or market.get('base') != 'BTC' or market.get('quote') != 'USDT'
            or market.get('spot') is not True or market.get('active') is not True
            or not market.get('id') or market.get('contract') is not False):
        raise ValueError('Require actual active BTC/USDT spot CCXT market snapshot')
    for name in ('amount', 'price'):
        number = market.get('precision', {}).get(name)
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or number <= 0:
            raise ValueError('CCXT tick-size price/amount precision required')
    for name in ('amount', 'cost', 'price'):
        limits = market.get('limits', {}).get(name)
        if not isinstance(limits, dict) or not {'min', 'max'} <= set(limits):
            raise ValueError('Actual market limits required, no invented defaults')
        for number in limits.values():
            if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float))
                                       or not math.isfinite(number) or number < 0):
                raise ValueError('Invalid market limits')
        if limits['min'] is not None and limits['max'] is not None and limits['min'] > limits['max']:
            raise ValueError('Reversed market limits')
    candles = value['candles']
    if not isinstance(candles, dict) or set(candles) != {'1m', '5m'}:
        raise ValueError('Both real UTC 1m and 5m candles required')
    for tf, step in (('1m', 60), ('5m', 300)):
        rows = candles[tf]
        expected_count = (END-START)//step
        if not isinstance(rows, list) or len(rows) != expected_count:
            raise ValueError(f'{tf}: require complete fixed 20251230-20260401 UTC range')
        for i, row in enumerate(rows):
            if (not isinstance(row, list) or len(row) != 6
                    or any(isinstance(n, bool) or not isinstance(n, (int, float))
                           or not math.isfinite(n) for n in row)):
                raise ValueError('Finite numeric OHLCV arrays required')
            t, o, h, lo, c, volume = row
            if (t != START+i*step or min(o, h, lo, c) <= 0 or volume < 0
                    or h < max(o, lo, c) or lo > min(o, h, c)):
                raise ValueError('Gap, invalid timestamp/price/volume or candle bounds')
    for i, row in enumerate(candles['5m']):
        group = candles['1m'][i*5:i*5+5]
        aggregated = [group[0][1], max(r[2] for r in group), min(r[3] for r in group),
                      group[-1][4], sum(r[5] for r in group)]
        if any(not math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-8)
               for a, b in zip(row[1:], aggregated, strict=True)):
            raise ValueError('1m/5m data disagree; do not manufacture replacement candles')
    return value


def load_bundle(path):
    path = Path(path)
    if not 0 < path.stat().st_size <= 80_000_000:
        raise ValueError('Bundle size must be 1..80000000 bytes')
    raw = path.read_bytes()
    value = validate_bundle(json.loads(raw))
    return value, hashlib.sha256(raw).hexdigest()


@contextmanager
def deny_network():
    """Fail closed on Python socket egress, including DNS; not an OS sandbox."""
    names = ('create_connection', 'getaddrinfo')
    original = {name: getattr(socket, name) for name in names}
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def denied(*args, **kwargs):
        raise RuntimeError('OFFLINE_NETWORK_DENIED: no exchange access, fallback or retry permitted')

    try:
        for name in names:
            setattr(socket, name, denied)
        socket.socket.connect = denied
        socket.socket.connect_ex = denied
        yield
    finally:
        for name, func in original.items():
            setattr(socket, name, func)
        socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
