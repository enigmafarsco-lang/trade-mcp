import copy

import pytest

from trade_mcp.cost_reconciliation import correlate_deal_costs
from trade_mcp.cost_snapshot import CostSnapshotError


def fixture():
    cost = dict(ticket='10', position_id='20', type=0, entry=0, profit=0.0,
                swap=0.0, commission=-0.2, fee=0.0, costs_complete=True)
    native = dict(ticket=10, position_id=20, type='buy', entry='in', profit=0.0,
                  swap=None, commission=None, fee=None, costs_complete=False)
    return [native], dict(source='mql5_cost_snapshot_v1', generated_at=1000, deals=[cost])


def test_join_does_not_mutate_or_authorize():
    native, snapshot = fixture()
    before = copy.deepcopy(native)
    result = correlate_deal_costs(native, snapshot, now=1001)
    assert native == before
    assert result['deals'][0]['commission'] == -0.2
    assert result['deals'][0]['costs_complete'] is True
    assert result['execution_eligible'] is False
    assert result['lifecycle_complete'] is False


@pytest.mark.parametrize('key,value', [('ticket', 11), ('position_id', 21),
    ('type', 'sell'), ('entry', 'out'), ('profit', 2), ('commission', 0),
    ('profit', float('nan')), ('profit', None)])
def test_conflicts_fail(key, value):
    native, snapshot = fixture()
    native[0][key] = value
    with pytest.raises(CostSnapshotError):
        correlate_deal_costs(native, snapshot, now=1001)


@pytest.mark.parametrize('now', [999, 1031, float('nan')])
def test_freshness(now):
    native, snapshot = fixture()
    with pytest.raises(CostSnapshotError):
        correlate_deal_costs(native, snapshot, now=now)


def test_account_charge_not_allocated():
    native, snapshot = fixture()
    snapshot['deals'].append(dict(snapshot['deals'][0], ticket='11', type=2, position_id='0'))
    with pytest.raises(CostSnapshotError):
        correlate_deal_costs(native, snapshot, now=1001)


def test_extra_rows_are_reported():
    native, snapshot = fixture()
    snapshot['deals'].append(dict(snapshot['deals'][0], ticket='11'))
    assert correlate_deal_costs(native, snapshot, now=1001)['snapshot_only_deals'] == 1
