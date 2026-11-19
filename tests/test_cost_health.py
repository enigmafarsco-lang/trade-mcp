from types import SimpleNamespace
from unittest.mock import Mock, patch

from trade_mcp.cost_health import cost_health


def test_not_configured_does_not_call_mt5():
    mt5 = Mock()
    assert cost_health(mt5, SimpleNamespace(cost_snapshot_path=''))['ok'] is False
    assert not mt5.mock_calls


def test_error_details_not_leaked():
    mt5 = Mock()
    mt5.account_info.side_effect = ValueError('secret account and token')
    result = cost_health(mt5, SimpleNamespace(cost_snapshot_path='/private', backend='native'))
    assert result['stage'] == 'account'
    assert 'secret' not in str(result)
    assert '/private' not in str(result)


def test_account_switch_blocks_success():
    mt5 = Mock()
    account = dict(login=1, server='Demo', currency='USD', trade_mode='demo')
    mt5.account_info.side_effect = [account, dict(account, login=2)]
    mt5.deals.return_value = [{'ticket': 1}]
    with patch('trade_mcp.cost_health.read_cost_snapshot', return_value={}), patch(
            'trade_mcp.cost_health.correlate_deal_costs', return_value={}):
        result = cost_health(mt5, SimpleNamespace(cost_snapshot_path='file', backend='native'))
    assert result['error'] == 'account_changed'
    assert result['execution_eligible'] is False


def test_correlation_reason_is_sanitized_and_specific():
    from trade_mcp.cost_reconciliation import CostCorrelationError

    mt5 = Mock()
    mt5.account_info.return_value = dict(login=1, server='Demo', currency='USD', trade_mode='demo')
    mt5.deals.return_value = [{'ticket': 1}]
    with patch('trade_mcp.cost_health.read_cost_snapshot', return_value={}), patch(
            'trade_mcp.cost_health.correlate_deal_costs',
            side_effect=CostCorrelationError('unallocated_account_charge')):
        result = cost_health(mt5, SimpleNamespace(cost_snapshot_path='file', backend='native'))
    assert result['reason'] == 'unallocated_account_charge'
    assert result['stage'] == 'correlation'
    assert result['execution_eligible'] is False
    assert 'login' not in result
