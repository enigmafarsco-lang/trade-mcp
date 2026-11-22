"""Offline SIGNAL replay of existing TickMomentum; not an order/PnL simulator.

Broker wall-clock labels are mapped to a relative numeric axis solely for
TickMomentum's time-difference checks. No UTC or complete-feed claim is made.
"""
import math
from collections import Counter
from datetime import datetime

from .tick_momentum import TickMomentum


def replay_signals(rows, *, consecutive=3):
    if not isinstance(rows, list) or not 2 <= len(rows) <= 10000:
        raise ValueError('Require 2..10000 existing historical quote rows')
    clean = []
    for row in rows:
        stamp = row.get('time_ms')
        if not isinstance(stamp, str):
            raise ValueError('Require actual ISO-like time_ms strings; no timestamp guessing')
        dt = datetime.fromisoformat(stamp)
        if dt.tzinfo is not None:
            raise ValueError('This replay explicitly accepts naive broker labels only')
        bid, ask = row['bid'], row['ask']
        if isinstance(bid, bool) or isinstance(ask, bool):
            raise ValueError('Invalid bid/ask')
        bid, ask = float(bid), float(ask)
        if not math.isfinite(bid) or not math.isfinite(ask) or not 0 < bid <= ask:
            raise ValueError('Invalid bid/ask')
        if clean and dt <= clean[-1][0]:
            raise ValueError('Strictly ordered unique rows required; never silently sort/deduplicate')
        clean.append((dt, bid, ask))
    first_minute = clean[0][0].replace(second=0, microsecond=0)
    last_minute = clean[-1][0].replace(second=0, microsecond=0)
    if (clean[-1][0]-clean[0][0]).total_seconds() > 300:
        raise ValueError('Only the bounded five-minute research sample is supported')
    strategy = TickMomentum(consecutive=consecutive)
    reasons, signals = Counter(), Counter()
    seen_minutes = set()
    minute, opened = None, None
    used = skipped = 0
    for dt, bid, ask in clean:
        start = dt.replace(second=0, microsecond=0)
        # Boundary minutes may be partial; never invent their opens or close state.
        if start in (first_minute, last_minute):
            skipped += 1
            continue
        if start != minute:
            minute, opened = start, bid
        elapsed = (dt-first_minute).total_seconds()
        bar_start = (start-first_minute).total_seconds()
        signal = strategy.observe(
            {'time_msc': elapsed*1000, 'bid': bid, 'ask': ask,
             'time_source': 'historical_broker_labels'},
            {'time': bar_start, 'time_utc': bar_start, 'open': opened,
             'time_source': 'relative_replay_axis_not_utc'}, now=elapsed)
        used += 1
        seen_minutes.add(start)
        if signal['action'] == 'wait':
            reasons[signal['reason']] += 1
        else:
            signals[signal['action']] += 1
    if not used:
        raise ValueError('No interior minute after discarding potentially partial boundary minutes')
    return {'schema': 'xauusd-offline-momentum-signal-replay/v1',
            'source': 'existing_TickMomentum.observe', 'consecutive_changes': consecutive,
            'input_rows': len(clean), 'replayed_rows': used, 'boundary_rows_discarded': skipped,
            'interior_minutes_seen': len(seen_minutes), 'signal_counts': dict(signals),
            'wait_reason_counts': dict(reasons), 'time_basis': 'relative_broker_wall_clock_not_utc',
            'candle_open_source': 'first_returned_bid_in_each_interior_broker_minute',
            'complete_tick_coverage_verified': False, 'utc_verified': False,
            'signal_replay_executed': True, 'profitability_backtest_executed': False,
            'execution_guards_replayed': False, 'execution_authorized': False,
            'pnl': None, 'fees': None,
            'limitations': ['Tiny historical sample, not representative trading evidence',
                            'Missing ticks cannot be excluded; sampled minute open may differ from true open',
                            'No fills, positions, sizing, broker guards or cash exits simulated',
                            'Internal time_utc argument holds relative offsets only, never UTC instants']}
