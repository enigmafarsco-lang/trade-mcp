import pytest

from trade_mcp.account_operations import account_operation_summary
from trade_mcp.cost_snapshot import CostSnapshotError


def record(kind, **kw):
    return dict(type=kind, position_id='0', profit=123456.78, swap=0,
                commission=0, fee=0, comment='secret comment', **kw)


def test_balance_not_labeled_deposit_or_ignored():
    summary = account_operation_summary({'deals': [record(2)]})
    assert summary['by_deal_type']['balance'] == 1
    assert summary['unallocated_records'] == 1
    assert summary['profit_sign_counts']['positive_profit'] == 1
    assert summary['allocation_verified'] is False
    assert 'deposit' not in str(summary)
    assert '123456.78' not in str(summary)
    assert 'secret comment' not in str(summary)


@pytest.mark.parametrize('kind,name', [(3, 'credit'), (4, 'charge'), (7, 'commission'),
                                      (12, 'interest'), (17, 'tax'), (999, 'unknown')])
def test_known_and_unknown_types(kind, name):
    summary = account_operation_summary({'deals': [record(kind)]})
    assert summary['by_deal_type'][name] == 1
    assert summary['unallocated_records'] == 1


def test_trade_without_position_and_normal_trade_are_distinct():
    summary = account_operation_summary({'deals': [record(0), dict(record(1), position_id='42')]})
    assert summary['by_deal_type']['trade_without_position'] == 1
    assert summary['unallocated_records'] == 1


def test_unknown_amount_cannot_be_classified_as_zero():
    with pytest.raises(CostSnapshotError):
        account_operation_summary({'deals': [dict(record(2), commission=None)]})
