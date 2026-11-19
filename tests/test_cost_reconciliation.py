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


@pytest.mark.parametrize('mutation,reason', [
    ('missing', 'native_ticket_outside_snapshot'),
    ('duplicate', 'native_ticket_duplicate'),
    ('profit', 'financial_source_mismatch'),
    ('charge', 'unallocated_account_charge'),
])
def test_specific_safe_reasons(mutation, reason):
    from trade_mcp.cost_reconciliation import CostCorrelationError

    native, snapshot = fixture()
    if mutation == 'missing':
        native[0]['ticket'] = 99
    elif mutation == 'duplicate':
        native.append(dict(native[0]))
    elif mutation == 'profit':
        native[0]['profit'] = 99
    else:
        snapshot['deals'][0]['type'] = 2
    with pytest.raises(CostCorrelationError) as caught:
        correlate_deal_costs(native, snapshot, now=1001)
    assert caught.value.code == reason


def test_coverage_counts_expose_no_raw_values():
    from trade_mcp.cost_reconciliation import coverage_summary

    native, snapshot = fixture()
    native.append(dict(native[0], ticket=99999999999))
    snapshot['deals'].append(dict(snapshot['deals'][0], ticket='88888888888'))
    report = coverage_summary(native, snapshot)
    assert report['ticket_overlap'] == 1
    assert report['native_only_deals'] == 1
    assert report['snapshot_only_deals'] == 1
    assert '99999999999' not in str(report)
    assert '88888888888' not in str(report)
    assert all(type(v) is int for v in report.values())


def test_v2_join_preserves_source_label():
    native, snapshot = fixture()
    snapshot['source'] = 'mql5_cost_snapshot_v2'
    result = correlate_deal_costs(native, snapshot, now=1001)
    assert result['deals'][0]['cost_source'] == 'mql5_cost_snapshot_v2'
    assert result['execution_eligible'] is False


def test_positive_balance_not_added_to_any_trade_profit():
    native, snapshot = fixture()
    snapshot['deals'].append(dict(ticket='999', position_id='0', type=2, entry=0,
                                  volume=0, profit=10000, swap=0, commission=0, fee=0,
                                  costs_complete=True))
    original = copy.deepcopy(snapshot)
    result = correlate_deal_costs(native, snapshot, now=1001)
    assert result['deals'][0]['profit'] == 0
    assert result['deals'][0]['commission'] == -.2
    assert result['excluded_positive_balance_records'] == 1
    assert result['matched'] == 1
    assert snapshot == original  # Raw balance record remains preserved.


def test_matching_native_balance_is_explicitly_excluded():
    native, snapshot = fixture()
    balance = dict(ticket='999', position_id='0', type=2, entry=0, volume=0,
                   profit=10000, swap=0, commission=0, fee=0, costs_complete=True)
    snapshot['deals'].append(balance)
    native.append(dict(balance, type='balance', entry='in'))
    result = correlate_deal_costs(native, snapshot, now=1001)
    assert result['excluded_native_balance_records'] == 1
    assert len(result['deals']) == 1


@pytest.mark.parametrize('change', [{'profit': -100}, {'fee': -.1}, {'volume': .1},
                                   {'type': 7}, {'costs_complete': False}])
def test_unqualified_balance_still_blocks_correlation(change):
    native, snapshot = fixture()
    balance = dict(ticket='999', position_id='0', type=2, entry=0, volume=0,
                   profit=10000, swap=0, commission=0, fee=0, costs_complete=True)
    snapshot['deals'].append(dict(balance, **change))
    with pytest.raises(CostSnapshotError):
        correlate_deal_costs(native, snapshot, now=1001)


@pytest.mark.parametrize('field,value,reason', [
    ('position_id', 21, 'deal_position_id_mismatch'),
    ('type', 'sell', 'deal_side_mismatch'),
    ('entry', 'out', 'deal_entry_mismatch'),
])
def test_identity_reasons_are_field_specific(field, value, reason):
    from trade_mcp.cost_reconciliation import CostCorrelationError
    native, snapshot = fixture()
    native[0][field] = value
    with pytest.raises(CostCorrelationError) as caught:
        correlate_deal_costs(native, snapshot, now=1001)
    assert caught.value.code == reason


def test_aggregate_identity_mismatch_preserves_privacy():
    from trade_mcp.cost_reconciliation import identity_summary
    native, snapshot = fixture()
    native[0].update(position_id=0, type='private raw enum', entry='private entry')
    result = identity_summary(native, snapshot)
    assert result['counts']['position_id_mismatches'] == 1
    assert result['counts']['side_mismatches'] == 1
    assert result['counts']['entry_mismatches'] == 1
    assert result['native_side_counts']['other'] == 1
    assert result['native_entry_counts']['other'] == 1
    assert 'private' not in str(result)


def test_aggregate_identity_success_has_no_false_mismatches():
    from trade_mcp.cost_reconciliation import identity_summary
    native, snapshot = fixture()
    result = identity_summary(native, snapshot)
    assert result['counts']['compared_trade_records'] == 1
    assert result['counts']['position_id_mismatches'] == 0
    assert result['counts']['side_mismatches'] == 0
    assert result['counts']['entry_mismatches'] == 0
