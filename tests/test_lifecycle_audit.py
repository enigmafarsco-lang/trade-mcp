import copy

import pytest

from trade_mcp.lifecycle_audit import audit_lifecycles


def snapshot():
    return dict(source='mql5_cost_snapshot_v2', deals=[
        dict(ticket='1', position_id='10', type=0, entry=0, time=100, volume=.1),
        dict(ticket='2', position_id='10', type=1, entry=1, time=200, volume=.1)], positions=[])


def test_closed_observed_volume_consistency_is_not_ownership_or_finality():
    data = snapshot()
    before = copy.deepcopy(data)
    result = audit_lifecycles(data)
    assert result['observed_volume_consistent'] is True
    assert result['counts']['closed_volume_consistent'] == 1
    assert not result['ownership_verified'] and not result['full_history_verified']
    assert not result['cost_finality_verified'] and not result['execution_eligible']
    assert data == before
    assert 'ticket' not in str(result) and 'position_id' not in str(result)


def test_partial_exit_open_remaining_volume():
    data = snapshot()
    data['deals'][1]['volume'] = .04
    data['positions'] = [dict(identifier='10', type=0, volume=.06, time=100)]
    result = audit_lifecycles(data)
    assert result['counts']['open_volume_consistent'] == 1
    assert result['observed_volume_consistent']


@pytest.mark.parametrize('change,counter', [
    ('missing_open', 'incomplete_positions'), ('missing_exit', 'volume_mismatch_positions'),
    ('wrong_side', 'direction_mismatch_positions'), ('reversal', 'reversal_positions'),
    ('overclose', 'volume_mismatch_positions'), ('active_missing', 'active_without_history'),
])
def test_incomplete_or_inconsistent_is_not_success(change, counter):
    data = snapshot()
    if change == 'missing_open':
        data['deals'].pop(0)
    elif change == 'missing_exit':
        data['deals'].pop()
    elif change == 'wrong_side':
        data['deals'][1]['type'] = 0
    elif change == 'reversal':
        data['deals'][1]['entry'] = 2
    elif change == 'overclose':
        data['deals'][1]['volume'] = .2
    else:
        data['positions'] = [dict(identifier='99', type=0, volume=.1, time=100)]
    result = audit_lifecycles(data)
    assert result['counts'][counter] == 1
    assert not result['observed_volume_consistent']


def test_old_snapshot_and_empty_history_not_evidence():
    assert not audit_lifecycles(dict(source='mql5_cost_snapshot_v1'))['available']
    assert not audit_lifecycles(dict(source='mql5_cost_snapshot_v2', deals=[], positions=[]))['observed_volume_consistent']
