"""Strict v2 cost-source adapter for the opt-in native demo guardian."""
import copy
import math
from pathlib import Path

from .account_operations import UnallocatedOperationsError, account_operation_summary
from .cost_reconciliation import correlate_deal_costs
from .cost_snapshot import CostSnapshotError, read_cost_snapshot


def load_guard_snapshot(mt5, cfg, account):
    """Configured source is mandatory once selected; never fall back on failure."""
    if account.get('trade_mode') != 'demo' or account.get('currency') != 'USD':
        raise CostSnapshotError('USD demo account required')
    snapshot = read_cost_snapshot(Path(cfg.cost_snapshot_path).expanduser(),
                                  login=str(account['login']), server=account['server'],
                                  currency=account['currency'], max_age=10)
    if snapshot['source'] != 'mql5_cost_snapshot_v2':
        raise CostSnapshotError('Guardian requires snapshot v2')
    after = mt5.account_info(fresh=True)
    if any(str(account[k]) != str(after.get(k)) for k in ('login', 'server', 'currency', 'trade_mode')):
        raise CostSnapshotError('Account changed during cost read')
    operations = account_operation_summary(snapshot)
    if operations['blocking_unallocated_records']:
        raise UnallocatedOperationsError(operations)
    return snapshot


def enrich_owned_history(deals, positions, snapshot, intents):
    """Require exact per-owned-position ticket sets, costs, volumes and opening IDs.

    Unrelated native history outside the snapshot is not used for owned P/L and
    is not adopted. Ownership still belongs exclusively to the durable journal.
    """
    pids = {int(i['position_id']) for i in intents.values() if i.get('position_id')}
    markers = set(intents)
    for d in deals:
        if d.get('entry') == 'in' and d.get('comment') in markers:
            pids.add(int(d['position_id']))
    for pid in pids:
        owners = {marker for marker, intent in intents.items()
                  if int(intent.get('position_id') or 0) == pid or any(
                      int(d.get('position_id') or 0) == pid and d.get('entry') == 'in'
                      and d.get('comment') == marker for d in deals)}
        if len(owners) != 1:
            raise CostSnapshotError('Ambiguous journal ownership')
        marker = next(iter(owners))
        running = 0.0
        group = sorted((d for d in deals if int(d.get('position_id') or 0) == pid),
                       key=lambda d: (float(d['time']), int(d['ticket'])))
        for d in group:
            if d['entry'] not in ('in', 'out', 'out_by'):
                raise CostSnapshotError('Reversal requires review')
            if d['entry'] == 'in' and d.get('comment') != marker:
                raise CostSnapshotError('Unowned addition to reserved position')
            volume = float(d.get('volume', 0))
            if not math.isfinite(volume) or volume <= 0:
                raise CostSnapshotError('Owned volume missing')
            running += volume if d['entry'] == 'in' else -volume
            if running < -1e-8:
                raise CostSnapshotError('Exit precedes covered opening volume')
    owned = [d for d in deals if int(d.get('position_id') or 0) in pids]
    scoped_costs = [d for d in snapshot['deals'] if int(d['position_id']) in pids]
    native_tickets = {str(d['ticket']) for d in owned}
    cost_tickets = {str(d['ticket']) for d in scoped_costs}
    if native_tickets != cost_tickets:
        raise CostSnapshotError('Owned lifecycle ticket coverage differs')
    joined = correlate_deal_costs(owned, snapshot)['deals']
    indexed = {str(d['ticket']): d for d in scoped_costs}
    for row in joined:
        cost = indexed[str(row['ticket'])]
        volume = float(row.get('volume', 0))
        if not math.isfinite(volume) or volume <= 0 or not math.isclose(
                volume, cost['volume'], abs_tol=1e-8, rel_tol=0):
            raise CostSnapshotError('Owned deal volume differs')
        if row.get('entry') == 'in' and row.get('comment') != cost['comment']:
            raise CostSnapshotError('Opening comments differ')
    enriched = {str(d['ticket']): d for d in joined}
    out_deals = [enriched.get(str(d['ticket']), copy.deepcopy(d)) for d in deals]
    snapshot_positions = {int(p['identifier']): p for p in snapshot['positions']}
    out_positions = copy.deepcopy(positions)
    native_active = {int(p.get('identifier') or p['ticket']) for p in positions}
    if {int(p['identifier']) for p in snapshot['positions'] if int(p['identifier']) in pids} != native_active & pids:
        raise CostSnapshotError('Owned active position sets differ')
    for position in out_positions:
        pid = int(position.get('identifier') or position['ticket'])
        if pid not in pids:
            continue
        source = snapshot_positions[pid]
        if (int(position['ticket']) != int(source['ticket']) or
                position['comment'] != source['comment'] or
                position['type'] != ('buy' if source['type'] == 0 else 'sell') or
                not math.isclose(float(position['volume']), source['volume'], abs_tol=1e-8, rel_tol=0)):
            raise CostSnapshotError('Owned floating position identity differs')
        history = [d for d in joined if int(d['position_id']) == pid]
        if any(d['entry'] not in ('in', 'out', 'out_by') for d in history):
            raise CostSnapshotError('Reversal requires review')
        remaining = sum(d['volume'] * (1 if d['entry'] == 'in' else -1) for d in history)
        if remaining <= 0 or not math.isclose(remaining, source['volume'], abs_tol=1e-8, rel_tol=0):
            raise CostSnapshotError('Owned remaining volume differs')
        position.update(profit=source['profit'], swap=source['swap'], pnl_complete=True)
    return out_deals, out_positions
