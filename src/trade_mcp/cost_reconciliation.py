"""Read-only correlation of fresh MQL5 cost records with native deal history.

Does not authorize execution, infer ownership, convert terminal timestamps, or
claim full position lifecycle coverage. Caller must use read_cost_snapshot with
independently obtained live identity immediately before calling this function.
"""
from __future__ import annotations

import copy
import math
import time

from .cost_snapshot import CostSnapshotError

FIELDS = ('profit', 'swap', 'commission', 'fee')
ENTRIES = {0: 'in', 1: 'out', 2: 'inout', 3: 'out_by'}


def _number(value):
    if isinstance(value, bool) or value is None:
        raise CostSnapshotError('Missing financial value')
    result = float(value)
    if not math.isfinite(result):
        raise CostSnapshotError('Nonfinite financial value')
    return result


def correlate_deal_costs(native_deals, snapshot, *, now=None):
    """Return a diagnostic copy with costs joined by exact ticket and position ID.

    Non-trade account charges in the snapshot block correlation because allocation
    is unknown. Native price profit must match; known costs must never be replaced
    by conflicting data. Snapshot-only rows are reported, not silently discarded.
    Native rows outside the bounded snapshot cause failure rather than zero costs.
    """
    now = time.time() if now is None else now
    if snapshot.get('source') != 'mql5_cost_snapshot_v1':
        raise CostSnapshotError('Unsupported cost source')
    if not 0 <= _number(now) - _number(snapshot.get('generated_at')) <= 30:
        raise CostSnapshotError('Stale cost source')
    indexed = {}
    for row in snapshot['deals']:
        ticket = str(row['ticket'])
        if ticket in indexed or int(ticket) <= 0:
            raise CostSnapshotError('Duplicate or invalid cost ticket')
        if row['type'] not in (0, 1) or int(row['position_id']) <= 0:
            raise CostSnapshotError('Unallocated account charges require review')
        if row.get('costs_complete') is not True:
            raise CostSnapshotError('Incomplete cost record')
        for key in FIELDS:
            _number(row[key])
        indexed[ticket] = row
    result = []
    seen = set()
    for native in native_deals:
        ticket = str(native['ticket'])
        if ticket in seen or ticket not in indexed:
            raise CostSnapshotError('Missing or duplicate native ticket')
        seen.add(ticket)
        cost = indexed[ticket]
        if (int(native['position_id']) != int(cost['position_id']) or
                native['type'] != ('buy' if cost['type'] == 0 else 'sell') or
                native['entry'] != ENTRIES.get(cost['entry'])):
            raise CostSnapshotError('Deal identity mismatch')
        # Do not compare naive terminal-time epochs to UTC without a validated
        # conversion. Exact globally unique deal IDs are mandatory for this join.
        for key in FIELDS:
            supplied = native.get(key)
            if (key == 'profit' or supplied is not None) and not math.isclose(
                    _number(supplied), _number(cost[key]), abs_tol=1e-7, rel_tol=0):
                raise CostSnapshotError('Financial source mismatch')
        item = copy.deepcopy(native)
        item.update({key: cost[key] for key in FIELDS})
        item.update(costs_complete=True, cost_source='mql5_cost_snapshot_v1')
        result.append(item)
    return {'deals': result, 'matched': len(result),
            'snapshot_only_deals': len(indexed.keys() - seen),
            'execution_eligible': False, 'lifecycle_complete': False}
