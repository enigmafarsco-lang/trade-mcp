import copy
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from trade_mcp.cost_snapshot import CostSnapshotError
from trade_mcp.demo_guard import account_key, inspect_positions
from trade_mcp.guard_costs import enrich_owned_history, load_guard_snapshot


def evidence():
    stamp = int(time.time())
    native = dict(ticket=1, position_id=20, type='buy', entry='in', volume=.1,
                  profit=0, swap=None, commission=None, fee=None, costs_complete=False,
                  time=stamp-30, comment='tmcp:owned')
    cost = dict(ticket='1', position_id='20', type=0, entry=0, volume=.1, profit=0,
                swap=0, commission=-.2, fee=0, costs_complete=True,
                time=stamp-30, comment='tmcp:owned')
    position = dict(ticket=21, identifier=20, type='buy', volume=.1, profit=None,
                    swap=None, comment='tmcp:owned', sl=90, pnl_complete=False)
    floating = dict(ticket='21', identifier='20', type=0, volume=.1, profit=-9.9,
                    swap=0, comment='tmcp:owned', sl=90, tp=110)
    snapshot = dict(source='mql5_cost_snapshot_v2', generated_at=stamp,
                    deals=[cost], positions=[floating])
    intents = {'tmcp:owned': dict(position_id=20, status='open',
                                 started_at=stamp-60, ownership='comment_reservation')}
    return [native], [position], snapshot, intents


def test_scopes_costs_without_adopting_unrelated_history():
    deals, positions, snapshot, intents = evidence()
    unrelated = dict(deals[0], ticket=999, position_id=999, comment='manual')
    deals.append(unrelated)
    before = copy.deepcopy((deals, positions, snapshot, intents))
    result, floating = enrich_owned_history(deals, positions, snapshot, intents)
    assert result[0]['commission'] == -.2
    assert result[1]['commission'] is None
    assert floating[0]['profit'] == -9.9
    assert (deals, positions, snapshot, intents) == before


@pytest.mark.parametrize('change', ['missing_ticket', 'extra_ticket', 'volume', 'comment',
                                    'floating_volume', 'floating_ticket', 'missing_position'])
def test_inconsistent_owned_evidence_blocks(change):
    deals, positions, snapshot, intents = evidence()
    if change == 'missing_ticket':
        snapshot['deals'] = []
    elif change == 'extra_ticket':
        snapshot['deals'].append(dict(snapshot['deals'][0], ticket='2'))
    elif change == 'volume':
        snapshot['deals'][0]['volume'] = .2
    elif change == 'comment':
        snapshot['deals'][0]['comment'] = 'manual'
    elif change == 'floating_volume':
        snapshot['positions'][0]['volume'] = .2
    elif change == 'floating_ticket':
        snapshot['positions'][0]['ticket'] = '999'
    else:
        snapshot['positions'] = []
    with pytest.raises(CostSnapshotError):
        enrich_owned_history(deals, positions, snapshot, intents)


def test_loader_requires_v2_and_account_stability():
    _, _, snapshot, _ = evidence()
    account = dict(login=1, server='Demo', currency='USD', trade_mode='demo')
    mt5 = Mock()
    mt5.account_info.return_value = dict(account, login=2)
    cfg = SimpleNamespace(cost_snapshot_path='snapshot')
    with patch('trade_mcp.guard_costs.read_cost_snapshot', return_value=snapshot) as reader:
        with pytest.raises(CostSnapshotError):
            load_guard_snapshot(mt5, cfg, account)
        assert reader.call_args.kwargs['max_age'] == 10
    snapshot['source'] = 'mql5_cost_snapshot_v1'
    with patch('trade_mcp.guard_costs.read_cost_snapshot', return_value=snapshot), pytest.raises(CostSnapshotError):
        load_guard_snapshot(mt5, cfg, account)


def test_guard_closes_from_owned_v2_costs_only_once(mock_app):
    deals, positions, snapshot, intents = evidence()
    account = dict(login=1, server='Demo', currency='USD', trade_mode='demo')
    mt5 = Mock()
    mt5.account_info.return_value = account
    mt5.positions.return_value = positions
    mt5.deals.return_value = deals
    mt5.close_position.return_value = {'retcode': 10009}
    mock_app.store.cash_guard_update(account_key(account), lambda state: state['intents'].update(intents))
    cfg = mock_app.cfg.with_overrides(backend='native', cost_snapshot_path='snapshot',
                                      demo_cash_policy=True, allow_trading=True)
    with patch('trade_mcp.guard_costs.read_cost_snapshot', return_value=snapshot):
        result = inspect_positions(mt5, cfg, mock_app.store, execute=True)
        inspect_positions(mt5, cfg, mock_app.store, execute=True)
    assert result['positions'][0]['net_usd'] == pytest.approx(-10.1)
    mt5.close_position.assert_called_once()
    assert mt5.close_position.call_args.args == (21,)


def test_guard_dry_run_never_closes(mock_app):
    deals, positions, snapshot, intents = evidence()
    account = dict(login=1, server='Demo', currency='USD', trade_mode='demo')
    mt5 = Mock()
    mt5.account_info.return_value = account
    mt5.positions.return_value = positions
    mt5.deals.return_value = deals
    mock_app.store.cash_guard_update(account_key(account), lambda state: state['intents'].update(intents))
    cfg = mock_app.cfg.with_overrides(backend='native', cost_snapshot_path='snapshot',
                                      demo_cash_policy=True, allow_trading=True)
    with patch('trade_mcp.guard_costs.read_cost_snapshot', return_value=snapshot):
        result = inspect_positions(mt5, cfg, mock_app.store, execute=False)
    assert result['positions'][0]['exit_due']
    mt5.close_position.assert_not_called()


def test_unowned_addition_blocks_instead_of_adopting():
    deals, positions, snapshot, intents = evidence()
    deals.append(dict(deals[0], ticket=2, comment='manual'))
    snapshot['deals'].append(dict(snapshot['deals'][0], ticket='2', comment='manual'))
    with pytest.raises(CostSnapshotError):
        enrich_owned_history(deals, positions, snapshot, intents)


def test_v2_closed_losses_use_real_costs_and_persist_latch(mock_app):
    from trade_mcp.demo_guard import state_for
    from trade_mcp.store import Store
    account = dict(login=1, server='Demo', currency='USD', trade_mode='demo')
    deals, costs, intents = [], [], {}
    for index in range(3):
        native, _, snap, intent = evidence()
        pid, marker = 20+index, f'tmcp:owned-{index}'
        stamp = int(time.time())-30+index
        opened = dict(native[0], ticket=1+index*2, position_id=pid, comment=marker, time=stamp)
        exited = dict(opened, ticket=2+index*2, entry='out', type='sell', profit=-1, time=stamp+1)
        deals.extend([opened, exited])
        c = dict(snap['deals'][0], ticket=str(opened['ticket']), position_id=str(pid), comment=marker)
        costs.extend([c, dict(c, ticket=str(exited['ticket']), entry=1, type=1, profit=-1)])
        intents[marker] = dict(intent['tmcp:owned'], position_id=pid)
    snapshot = dict(source='mql5_cost_snapshot_v2', generated_at=int(time.time()),
                    deals=costs, positions=[])
    mt5 = Mock()
    mt5.account_info.return_value = account
    mt5.positions.return_value = []
    mt5.deals.return_value = deals
    mock_app.store.cash_guard_update(account_key(account), lambda state: state['intents'].update(intents))
    cfg = mock_app.cfg.with_overrides(backend='native', cost_snapshot_path='snapshot',
                                      demo_cash_policy=True, allow_trading=True)
    with patch('trade_mcp.guard_costs.read_cost_snapshot', return_value=snapshot):
        result = inspect_positions(mt5, cfg, mock_app.store, execute=True)
    assert result['latched'] and result['loss_streak'] == 3
    reopened = Store(str(mock_app.store.path))
    try:
        assert state_for(reopened, account_key(account))['latched'] is True
    finally:
        reopened.close()
    mt5.close_position.assert_not_called()


def test_balance_record_stays_blocked_with_private_safe_inventory():
    from trade_mcp.account_operations import UnallocatedOperationsError
    _, _, snapshot, _ = evidence()
    snapshot['deals'].append(dict(snapshot['deals'][0], ticket='999', position_id='0',
                                  type=2, profit=987654.32, comment='private'))
    account = dict(login=1, server='Demo', currency='USD', trade_mode='demo')
    mt5 = Mock()
    mt5.account_info.return_value = account
    cfg = SimpleNamespace(cost_snapshot_path='snapshot')
    with patch('trade_mcp.guard_costs.read_cost_snapshot', return_value=snapshot), pytest.raises(
            UnallocatedOperationsError) as caught:
        load_guard_snapshot(mt5, cfg, account)
    assert caught.value.code == 'unallocated_account_operations'
    assert caught.value.summary['by_deal_type']['balance'] == 1
    assert caught.value.summary['allocation_verified'] is False
    assert '987654' not in str(caught.value.summary)
    assert 'private' not in str(caught.value.summary)
