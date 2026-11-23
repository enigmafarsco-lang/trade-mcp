"""Read-only aggregate v2 history audit; never adopts positions or mutates journal."""
import math

from .account_operations import account_operation_summary
from .cost_snapshot import CostSnapshotError


def audit_lifecycles(snapshot):
    """Compare covered opening/closing volumes with explicit active-position rows.

    Consistency of the observed interval is not proof of full history or final
    broker charges. No monetary values, identifiers or ownership claims are output.
    """
    base = {'execution_eligible': False, 'ownership_verified': False,
            'full_history_verified': False, 'cost_finality_verified': False}
    if snapshot.get('source') != 'mql5_cost_snapshot_v2':
        return {**base, 'available': False, 'reason': 'snapshot_v2_required'}
    operations = account_operation_summary(snapshot)
    if operations['blocking_unallocated_records']:
        return {**base, 'available': False, 'reason': 'unallocated_account_operations'}
    groups = {}
    seen = set()
    for deal in snapshot['deals']:
        if deal['type'] not in (0, 1):
            continue  # Account operations already classified above, never position P/L.
        ticket = int(deal['ticket'])
        if ticket <= 0 or ticket in seen:
            raise CostSnapshotError('Invalid lifecycle deal ticket')
        seen.add(ticket)
        groups.setdefault(int(deal['position_id']), []).append(deal)
    active = {}
    for position in snapshot['positions']:
        pid = int(position['identifier'])
        if pid in active:
            raise CostSnapshotError('Duplicate active lifecycle identifier')
        active[pid] = position
    counts = dict.fromkeys(('positions_evaluated', 'closed_volume_consistent',
                           'open_volume_consistent', 'incomplete_positions',
                           'reversal_positions', 'direction_mismatch_positions',
                           'volume_mismatch_positions', 'active_without_history',
                           'opening_time_mismatch_positions'), 0)
    for pid, items in groups.items():
        counts['positions_evaluated'] += 1
        items = sorted(items, key=lambda d: (int(d['time']), int(d['ticket'])))
        if any(d['entry'] not in (0, 1, 3) for d in items):
            counts['reversal_positions'] += 1
            continue
        if items[0]['entry'] != 0:
            counts['incomplete_positions'] += 1
            continue
        opening_side = items[0]['type']
        if any(d['type'] != (opening_side if d['entry'] == 0 else 1-opening_side) for d in items):
            counts['direction_mismatch_positions'] += 1
            continue
        remaining = 0.0
        invalid = False
        for i, deal in enumerate(items):
            volume = float(deal['volume'])
            if not math.isfinite(volume) or volume <= 0:
                raise CostSnapshotError('Invalid lifecycle volume')
            remaining += volume if deal['entry'] == 0 else -volume
            if remaining < -1e-8 or (i < len(items)-1 and abs(remaining) <= 1e-8):
                invalid = True
        position = active.get(pid)
        if position is not None:
            volume = float(position['volume'])
            if position['type'] != opening_side:
                counts['direction_mismatch_positions'] += 1
                continue
            if int(position['time']) != int(items[0]['time']):
                counts['opening_time_mismatch_positions'] += 1
                continue
            if not math.isfinite(volume) or volume <= 0:
                raise CostSnapshotError('Invalid active volume')
            invalid |= not math.isclose(remaining, volume, abs_tol=1e-8, rel_tol=0)
        else:
            invalid |= not math.isclose(remaining, 0, abs_tol=1e-8, rel_tol=0)
        if invalid:
            counts['volume_mismatch_positions'] += 1
        else:
            counts['open_volume_consistent' if position is not None else 'closed_volume_consistent'] += 1
    counts['active_without_history'] = len(active.keys()-groups.keys())
    problematic = sum(counts[k] for k in ('incomplete_positions', 'reversal_positions',
                      'direction_mismatch_positions', 'volume_mismatch_positions',
                      'active_without_history', 'opening_time_mismatch_positions'))
    return {**base, 'available': True, 'observed_volume_consistent':
            bool(groups) and problematic == 0, 'counts': counts,
            'note': 'bounded_history_diagnostic_not_owned_guardian_acceptance'}
