"""Read-only correlation of fresh MQL5 cost records with native deal history.

Does not authorize execution, infer ownership, convert terminal timestamps, or
claim full position lifecycle coverage. Caller must use read_cost_snapshot with
independently obtained live identity immediately before calling this function.
"""
from __future__ import annotations

import copy
import math
import time

from .account_operations import is_positive_balance_operation
from .cost_snapshot import CostSnapshotError


class CostCorrelationError(CostSnapshotError):
    """Fixed diagnostic reason, never raw broker data."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


FIELDS = ('profit', 'swap', 'commission', 'fee')
ENTRIES = {0: 'in', 1: 'out', 2: 'inout', 3: 'out_by'}


def _number(value):
    if isinstance(value, bool) or value is None:
        raise CostCorrelationError('financial_value_missing')
    result = float(value)
    if not math.isfinite(result):
        raise CostCorrelationError('financial_value_nonfinite')
    return result


def correlate_deal_costs(native_deals, snapshot, *, now=None):
    """Return a diagnostic copy with costs joined by exact ticket and position ID.

    Non-trade account charges in the snapshot block correlation because allocation
    is unknown. Native price profit must match; known costs must never be replaced
    by conflicting data. Snapshot-only rows are reported, not silently discarded.
    Native rows outside the bounded snapshot cause failure rather than zero costs.
    """
    now = time.time() if now is None else now
    if snapshot.get('source') not in {'mql5_cost_snapshot_v1', 'mql5_cost_snapshot_v2'}:
        raise CostCorrelationError('unsupported_cost_source')
    if not 0 <= _number(now) - _number(snapshot.get('generated_at')) <= 30:
        raise CostCorrelationError('snapshot_stale')
    indexed, balance_operations = {}, {}
    snapshot_tickets = set()
    for row in snapshot['deals']:
        ticket = str(row['ticket'])
        if ticket in snapshot_tickets or int(ticket) <= 0:
            raise CostCorrelationError('snapshot_ticket_invalid_or_duplicate')
        snapshot_tickets.add(ticket)
        if is_positive_balance_operation(row):
            balance_operations[ticket] = row
            continue
        if row['type'] not in (0, 1) or int(row['position_id']) <= 0:
            raise CostCorrelationError('unallocated_account_charge')
        if row.get('costs_complete') is not True:
            raise CostCorrelationError('snapshot_costs_incomplete')
        for key in FIELDS:
            _number(row[key])
        indexed[ticket] = row
    result = []
    seen = set()
    excluded_native = 0
    for native in native_deals:
        ticket = str(native['ticket'])
        if ticket in seen:
            raise CostCorrelationError('native_ticket_duplicate')
        seen.add(ticket)
        if ticket in balance_operations:
            credit = balance_operations[ticket]
            if (int(native.get('position_id', -1)) != 0 or
                    native.get('type') not in {'balance', '2', 'deal_type_balance'} or
                    native.get('entry') != 'in' or _number(native.get('volume')) != 0):
                raise CostCorrelationError('deal_identity_mismatch')
            for key in FIELDS:
                supplied = native.get(key)
                if (key == 'profit' or supplied is not None) and not math.isclose(
                        _number(supplied), _number(credit[key]), abs_tol=1e-7, rel_tol=0):
                    raise CostCorrelationError('financial_source_mismatch')
            excluded_native += 1
            continue
        if ticket not in indexed:
            raise CostCorrelationError('native_ticket_outside_snapshot')
        cost = indexed[ticket]
        if int(native['position_id']) != int(cost['position_id']):
            raise CostCorrelationError('deal_position_id_mismatch')
        if native['type'] != ('buy' if cost['type'] == 0 else 'sell'):
            raise CostCorrelationError('deal_side_mismatch')
        if native['entry'] != ENTRIES.get(cost['entry']):
            raise CostCorrelationError('deal_entry_mismatch')
        # Do not compare naive terminal-time epochs to UTC without a validated
        # conversion. Exact globally unique deal IDs are mandatory for this join.
        for key in FIELDS:
            supplied = native.get(key)
            if (key == 'profit' or supplied is not None) and not math.isclose(
                    _number(supplied), _number(cost[key]), abs_tol=1e-7, rel_tol=0):
                raise CostCorrelationError('financial_source_mismatch')
        item = copy.deepcopy(native)
        item.update({key: cost[key] for key in FIELDS})
        item.update(costs_complete=True, cost_source=snapshot['source'])
        result.append(item)
    return {'deals': result, 'matched': len(result),
            'snapshot_only_deals': len(indexed.keys() - seen),
            'excluded_positive_balance_records': len(balance_operations),
            'excluded_native_balance_records': excluded_native,
            'execution_eligible': False, 'lifecycle_complete': False}


def coverage_summary(native_deals, snapshot):
    """Return counts only, with no assumption that the two time windows agree."""
    native_ids = [str(row['ticket']) for row in native_deals]
    snapshot_ids = [str(row['ticket']) for row in snapshot['deals']]
    native_set, snapshot_set = set(native_ids), set(snapshot_ids)
    return {
        'native_deals': len(native_ids),
        'snapshot_deals': len(snapshot_ids),
        'ticket_overlap': len(native_set & snapshot_set),
        'native_only_deals': len(native_set - snapshot_set),
        'snapshot_only_deals': len(snapshot_set - native_set),
        'native_duplicate_tickets': len(native_ids) - len(native_set),
        'snapshot_duplicate_tickets': len(snapshot_ids) - len(snapshot_set),
        'unallocated_snapshot_deals': sum(
            row['type'] not in (0, 1) or int(row['position_id']) <= 0
            for row in snapshot['deals']),
    }


def identity_summary(native_deals, snapshot):
    """Aggregate field-specific identity mismatches for shared trading tickets.

    No IDs, raw enum strings, comments, dates or amounts leave this function.
    This is diagnosis, not a relaxed join; missing fields count as mismatches.
    """
    indexed = {str(row['ticket']): row for row in snapshot['deals']
               if row['type'] in (0, 1) and int(row['position_id']) > 0}
    counts = dict.fromkeys(('compared_trade_records', 'position_id_mismatches',
                           'side_mismatches', 'entry_mismatches',
                           'native_position_id_missing', 'native_side_unknown',
                           'native_entry_unknown'), 0)
    sides = dict.fromkeys(('buy', 'sell', 'other'), 0)
    entries = dict.fromkeys(('in', 'out', 'out_by', 'inout', 'other'), 0)
    for native in native_deals:
        source = indexed.get(str(native['ticket']))
        if source is None:
            continue
        counts['compared_trade_records'] += 1
        position = int(native.get('position_id') or 0)
        side, entry = native.get('type'), native.get('entry')
        counts['native_position_id_missing'] += int(position <= 0)
        counts['native_side_unknown'] += int(side not in ('buy', 'sell'))
        counts['native_entry_unknown'] += int(entry not in ENTRIES.values())
        counts['position_id_mismatches'] += int(position != int(source['position_id']))
        counts['side_mismatches'] += int(side != ('buy' if source['type'] == 0 else 'sell'))
        counts['entry_mismatches'] += int(entry != ENTRIES.get(source['entry']))
        sides[side if side in ('buy', 'sell') else 'other'] += 1
        entries[entry if entry in ('in', 'out', 'out_by', 'inout') else 'other'] += 1
    return dict(counts=counts, native_side_counts=sides, native_entry_counts=entries)
