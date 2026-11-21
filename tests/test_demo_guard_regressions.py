"""Regression tests for durable demo-guard reconciliation (redacted incident fixtures)."""
import copy
import time
from types import SimpleNamespace

import pytest

from trade_mcp.demo_guard import DemoGuardian, account_key, inspect_positions, state_for
from trade_mcp.store import Store


def _broker(account, positions, deals, orders=None, close_retcode=10009):
    calls = []
    return SimpleNamespace(
        account_info=lambda **k: account,
        positions=lambda: list(positions),
        deals=lambda **k: list(deals),
        orders=lambda: list(orders or []),
        close_position=lambda ticket, **k: calls.append(int(ticket)) or {'retcode': close_retcode},
        request=lambda *a, **k: {'ownership_supported': True, 'ownership_method': 'comment_reservation'},
        close=lambda: None,
        calls=calls,
    )


def _intent(marker, status='awaiting_position', position_id=None, ownership='comment_reservation', started_at=None):
    return {marker: {'status': status, 'position_id': position_id,
                     'started_at': started_at if started_at is not None else time.time()-60,
                     'ownership': ownership}}


def _deal(ticket, pid, entry, profit=0.0, commission=-0.1, fee=0.0, swap=0.0,
          volume=0.01, stamp=0, comment='tmcp:incident', side='sell', costs_complete=True):
    return {'ticket': ticket, 'position_id': pid, 'entry': entry, 'type': side,
            'profit': profit, 'commission': commission, 'fee': fee, 'swap': swap,
            'volume': volume, 'time': stamp, 'comment': comment, 'costs_complete': costs_complete}


def _seed(store, account, intents):
    store.cash_guard_update(account_key(account), lambda state: state['intents'].update(copy.deepcopy(intents)))


def test_incident_closed_between_polls_unparseable_deal_time_reconciles(mock_app):
    """Accepted reserved intent; position closed between polls; opening deal time
    unavailable in normalized history; unique marker identity and complete costs
    must reconcile without adopting unrelated trades or fabricating costs."""
    account = {'login': 123, 'server': 'test-demo', 'currency': 'USD', 'trade_mode': 'demo', 'equity': 1000.0}
    marker = 'tmcp:incident-redacted'
    intents = _intent(marker)
    # Closed position: no live positions/orders, full in/out history with time=0 (unparseable)
    deals = [
        _deal(101, 9001, 'in', profit=0.0, commission=-0.2, stamp=0, comment=marker),
        _deal(102, 9001, 'out', side='buy', profit=2.5, commission=0.0, stamp=0, comment=marker),
    ]
    broker = _broker(account, positions=[], deals=deals)
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=False, allow_trading=False)
    _seed(mock_app.store, account, intents)

    result = inspect_positions(broker, cfg, mock_app.store, execute=False, reconcile_existing=True)

    assert result['ok'] is True
    assert result['unresolved_intent_count'] == 0
    assert result['blocker_code'] is None
    state = state_for(mock_app.store, account_key(account))
    item = state['intents'][marker]
    assert item['status'] == 'closed'
    assert item['position_id'] == 9001
    assert item['net_usd'] == pytest.approx(2.3)
    assert state['streak'] == 0 and state['latched'] is False

    # repeated reconciliation/process-restart style: idempotent, no double count/reset
    reopened = Store(str(mock_app.store.path))
    try:
        again = inspect_positions(broker, cfg, reopened, execute=False, reconcile_existing=True)
        state2 = state_for(reopened, account_key(account))
        assert again['unresolved_intent_count'] == 0
        assert state2['intents'][marker]['status'] == 'closed'
        assert state2['intents'][marker]['net_usd'] == pytest.approx(2.3)
        assert state2['streak'] == 0 and state2['latched'] is False
    finally:
        reopened.close()


def test_accepted_order_observed_while_still_open_reconciles_to_open(mock_app):
    account = {'login': 123, 'server': 'test-demo', 'currency': 'USD', 'trade_mode': 'demo', 'equity': 1000.0}
    marker = 'tmcp:open-redacted'
    intents = _intent(marker)
    positions = [{'ticket': 7001, 'identifier': 7001, 'magic': 0, 'comment': marker,
                  'sl': 1.0, 'profit': 0.0, 'swap': 0.0, 'pnl_complete': True, 'volume': 0.01, 'type': 'sell'}]
    deals = [_deal(201, 7001, 'in', profit=0.0, stamp=time.time()-30, comment=marker)]
    broker = _broker(account, positions=positions, deals=deals)
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=False, allow_trading=False)
    _seed(mock_app.store, account, intents)

    result = inspect_positions(broker, cfg, mock_app.store, execute=False, reconcile_existing=True)
    state = state_for(mock_app.store, account_key(account))
    assert state['intents'][marker]['status'] == 'open'
    assert state['intents'][marker]['position_id'] == 7001
    # Ownership/identity reconciled to the live position, but lifecycle remains
    # open and must still block new entries.
    assert result['unresolved_intent_count'] == 1
    assert result['blocker_code'] == 'unresolved_intent'
    assert not any(row.get('state') == 'unresolved' for row in result.get('positions', []))


def test_missing_or_changed_comment_stays_unresolved_and_entry_blocking(mock_app):
    account = {'login': 123, 'server': 'test-demo', 'currency': 'USD', 'trade_mode': 'demo', 'equity': 1000.0}
    marker = 'tmcp:reserved-redacted'
    intents = _intent(marker)
    # history belongs to different comment/identity -> must not be adopted
    deals = [
        _deal(301, 8001, 'in', stamp=0, comment='tmcp:other'),
        _deal(302, 8001, 'out', side='buy', profit=1.0, stamp=0, comment='tmcp:other'),
    ]
    broker = _broker(account, positions=[], deals=deals)
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=False, allow_trading=False)
    _seed(mock_app.store, account, intents)

    result = inspect_positions(broker, cfg, mock_app.store, execute=False, reconcile_existing=True)
    assert result['unresolved_intent_count'] == 1
    assert result['blocker_code'] == 'unresolved_intent'
    state = state_for(mock_app.store, account_key(account))
    assert state['intents'][marker]['status'] == 'awaiting_position'


def test_unknown_costs_preserve_unresolved_and_do_not_mark_zero(mock_app):
    account = {'login': 123, 'server': 'test-demo', 'currency': 'USD', 'trade_mode': 'demo', 'equity': 1000.0}
    marker = 'tmcp:costs-redacted'
    intents = _intent(marker)
    deals = [
        _deal(401, 8101, 'in', stamp=0, comment=marker, costs_complete=False, commission=None),
        _deal(402, 8101, 'out', side='buy', profit=3.0, stamp=0, comment=marker, costs_complete=False, commission=None),
    ]
    broker = _broker(account, positions=[], deals=deals)
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=False, allow_trading=False)
    _seed(mock_app.store, account, intents)

    with pytest.raises(Exception) as caught:
        inspect_positions(broker, cfg, mock_app.store, execute=False, reconcile_existing=True)
    assert getattr(caught.value, 'code', str(caught.value)) == 'explicit_cost_fields_required'
    state = state_for(mock_app.store, account_key(account))
    assert state['intents'][marker]['status'] == 'awaiting_position'
    assert 'net_usd' not in state['intents'][marker]


def test_partial_or_incomplete_closed_history_remains_blocking(mock_app):
    account = {'login': 123, 'server': 'test-demo', 'currency': 'USD', 'trade_mode': 'demo', 'equity': 1000.0}
    marker = 'tmcp:partial-redacted'
    intents = _intent(marker)
    # out volume does not cover opened volume
    deals = [
        _deal(501, 8201, 'in', volume=0.02, stamp=0, comment=marker),
        _deal(502, 8201, 'out', side='buy', volume=0.01, profit=1.0, stamp=0, comment=marker),
    ]
    broker = _broker(account, positions=[], deals=deals)
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=False, allow_trading=False)
    _seed(mock_app.store, account, intents)

    with pytest.raises(Exception) as caught:
        inspect_positions(broker, cfg, mock_app.store, execute=False, reconcile_existing=True)
    assert getattr(caught.value, 'code', str(caught.value)) == 'incomplete_closed_position_volume'
    state = state_for(mock_app.store, account_key(account))
    assert state['intents'][marker]['status'] == 'awaiting_position'


def test_guardian_reconcile_now_sanitized_health_in_no_order_mode(mock_app):
    account = {'login': 123, 'server': 'test-demo', 'currency': 'USD', 'trade_mode': 'demo', 'equity': 1000.0}
    marker = 'tmcp:health-redacted'
    intents = _intent(marker)
    deals = [
        _deal(601, 8301, 'in', profit=0.0, commission=-0.1, stamp=0, comment=marker),
        _deal(602, 8301, 'out', side='buy', profit=1.0, commission=0.0, stamp=0, comment=marker),
    ]
    broker = _broker(account, positions=[], deals=deals)
    mock_app.mt5 = broker
    mock_app.cfg = mock_app.cfg.with_overrides(demo_cash_policy=False, allow_trading=False)
    _seed(mock_app.store, account, intents)
    guardian = DemoGuardian(mock_app)

    health, result = guardian.reconcile_now(execute=False)
    assert health['ok'] is True
    assert health['enabled'] is False
    assert health['unresolved_intent_count'] == 0
    assert health['current_blocker_code'] is None
    assert health['last_poll_at'] is not None
    assert health['last_successful_reconciliation_at'] is not None
    assert result['reconciled' if False else 'ok'] is True
    state = state_for(mock_app.store, account_key(account))
    assert state['intents'][marker]['status'] == 'closed'
    assert guardian.health()['unresolved_intent_count'] == 0
