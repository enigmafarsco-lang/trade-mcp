"""USD demo policy tests. All broker operations are fake; no live calls."""
import time
from types import SimpleNamespace

import pytest

from trade_mcp.demo_guard import MAGIC, account_key, inspect_positions, reserve_entry, state_for
from trade_mcp.store import Store


def fixture_broker():
    account = {'login':123,'server':'test-demo','currency':'USD','trade_mode':'demo','equity':99122.7}
    positions, deals, calls = [], [], []
    broker = SimpleNamespace(account_info=lambda **k:account, positions=lambda:positions,
        deals=lambda **k:deals, orders=lambda:[],
        close_position=lambda ticket, **k:calls.append(ticket) or {'retcode':10009})
    return broker, account, positions, deals, calls


def deal(ticket, pid, entry, profit=0, commission=0, fee=0, swap=0, stamp=None):
    return {'ticket':ticket,'position_id':pid,'entry':entry,'profit':profit,'commission':commission,
            'fee':fee,'swap':swap,'volume':.01,'time':stamp or time.time()}


def own(store, account, marker, pid):
    def update(state):
        state['intents'][marker] = {'status':'open','position_id':pid,'started_at':time.time()-100}
    store.cash_guard_update(account_key(account),update)


@pytest.mark.parametrize('net,should_close', [(-9,False),(-10,True),(-11,True),(1.9,False),(2,True)])
def test_only_owned_positions_close_at_cash_thresholds(mock_app, net, should_close):
    broker, account, positions, deals, calls = fixture_broker()
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True, allow_trading=True)
    own(mock_app.store,account,'owned',10)
    positions.extend([{'ticket':10,'magic':MAGIC,'comment':'owned','sl':90,'profit':net+.2,'swap':0},
                      {'ticket':99,'magic':0,'comment':'manual','sl':90,'profit':-100,'swap':0}])
    deals.append({**deal(1,10,'in',commission=-.2),'magic':MAGIC,'comment':'owned'})
    result = inspect_positions(broker,cfg,mock_app.store,execute=True)
    assert result['positions'][0]['net_usd'] == pytest.approx(net)
    assert calls == ([10] if should_close else [])
    inspect_positions(broker,cfg,mock_app.store,execute=True)
    assert calls == ([10] if should_close else [])  # No duplicate close request.


def test_three_closed_losses_latch_survives_restart_and_later_win(mock_app):
    broker, account, positions, deals, calls = fixture_broker()
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True, allow_trading=True)
    for pid in range(1,5):
        own(mock_app.store,account,f'owned-{pid}',pid)
        deals.extend([deal(pid*2,pid,'in',commission=-.1,stamp=pid*10),
                      deal(pid*2+1,pid,'out',profit=-9 if pid < 4 else 2,stamp=pid*10+1)])
    result = inspect_positions(broker,cfg,mock_app.store,execute=True)
    assert result['latched'] and result['review_required']
    reopened = Store(str(mock_app.store.path))
    try:
        assert state_for(reopened,account_key(account))['latched']
        with pytest.raises(ValueError):
            reserve_entry(broker,cfg,reopened)
    finally:
        reopened.close()
    assert calls == []


def test_partial_close_does_not_count_loss_and_unknown_history_blocks(mock_app):
    broker, account, positions, deals, calls = fixture_broker()
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True,allow_trading=False)
    own(mock_app.store,account,'owned',10)
    positions.append({'ticket':10,'magic':MAGIC,'comment':'owned','sl':90,'profit':0,'swap':0})
    deals.extend([deal(1,10,'in'),deal(2,10,'out',profit=-1)])
    assert inspect_positions(broker,cfg,mock_app.store)['loss_streak'] == 0
    positions.clear()
    deals[1]['volume'] = .005
    with pytest.raises(ValueError):
        inspect_positions(broker,cfg,mock_app.store)


def test_trading_disabled_never_sends_cash_exit(mock_app):
    broker, account, positions, deals, calls = fixture_broker()
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True,allow_trading=False)
    own(mock_app.store,account,'owned',10)
    positions.append({'ticket':10,'magic':MAGIC,'comment':'owned','sl':90,'profit':-11,'swap':0})
    deals.append(deal(1,10,'in'))
    assert inspect_positions(broker,cfg,mock_app.store,execute=True)['positions'][0]['exit_due']
    assert calls == []


@pytest.mark.parametrize('field,value', [('trade_mode','real'),('currency','EUR'),('login',None)])
def test_wrong_account_is_rejected(field,value):
    _,account,_,_,_ = fixture_broker()
    account[field] = value
    with pytest.raises(ValueError):
        account_key(account)


def test_reservation_blocks_second_entry_and_is_account_scoped(mock_app):
    broker,account,_,_,_ = fixture_broker()
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True)
    key, marker = reserve_entry(broker,cfg,mock_app.store)
    assert marker.startswith('tmcp:')
    with pytest.raises(ValueError):
        reserve_entry(broker,cfg,mock_app.store)
    account['login'] = 456
    key2, _ = reserve_entry(broker,cfg,mock_app.store)
    assert key != key2


def test_cash_sizing_uses_equity_but_caps_planned_loss(mock_app):
    from trade_mcp.config import sizing_balance
    from trade_mcp.execution import propose_open
    mock_app.cfg = mock_app.cfg.with_overrides(demo_cash_policy=True)
    original = mock_app.mt5.account_info
    mock_app.mt5.account_info = lambda **k:{**original(**k),'currency':'USD','trade_mode':'demo','equity':99122.7}
    account = mock_app.mt5.account_info()
    assert sizing_balance(account,mock_app.cfg) == 99122.7
    tick = mock_app.mt5.tick('XAUUSD')
    result = propose_open(mt5=mock_app.mt5,cfg=mock_app.cfg,store=mock_app.store,symbol='XAUUSD',
        side='buy',stop_loss=tick['ask']-5,comment='agent:fixture')
    assert result['ok'], result
    preview = result['preview']
    assert preview['risk_amount'] <= 10
    assert preview['estimated_profit'] == pytest.approx(2,abs=.1)
    assert mock_app.store.get_proposal(result['proposal_id'])['payload']['demo_cash_policy']


def test_unknown_close_outcome_is_not_retried(mock_app):
    broker,account,positions,deals,calls = fixture_broker()
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True,allow_trading=True)
    own(mock_app.store,account,'owned',10)
    positions.append({'ticket':10,'magic':MAGIC,'comment':'owned','sl':90,'profit':-11,'swap':0})
    deals.append(deal(1,10,'in'))
    def fail(ticket, **kwargs):
        calls.append(ticket)
        raise TimeoutError()
    broker.close_position = fail
    with pytest.raises(TimeoutError):
        inspect_positions(broker,cfg,mock_app.store,execute=True)
    inspect_positions(broker,cfg,mock_app.store,execute=True)
    assert calls == [10]
    assert state_for(mock_app.store,account_key(account))['intents']['owned']['status'] == 'close_requested'


def test_missing_costs_fail_closed_without_counting_or_closing(mock_app):
    broker,account,positions,deals,calls = fixture_broker()
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True,allow_trading=True)
    own(mock_app.store,account,'owned',10)
    positions.append({'ticket':10,'magic':MAGIC,'comment':'owned','sl':90,'profit':-11,'swap':0})
    deals.append(deal(1,10,'in'))
    del deals[0]['fee']
    with pytest.raises(KeyError):
        inspect_positions(broker,cfg,mock_app.store,execute=True)
    assert calls == []


def test_usd_demo_guard_never_sizes_live_equity(mock_app):
    from trade_mcp.config import sizing_balance
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True)
    assert sizing_balance({'trade_mode':'real','currency':'USD','equity':100000},cfg) == 0
    assert sizing_balance({'trade_mode':'demo','currency':'EUR','equity':100000},cfg) == 0


def test_cash_confirmation_tags_only_new_owned_entry(mock_app):
    from trade_mcp.execution import confirm, propose_open
    mock_app.cfg = mock_app.cfg.with_overrides(demo_cash_policy=True,allow_trading=True)
    mock_app.mt5.bridge.allow_trading = True
    original = mock_app.mt5.account_info
    mock_app.mt5.account_info = lambda **k:{**original(**k),'currency':'USD','trade_mode':'demo','equity':99122.7}
    tick = mock_app.mt5.tick('XAUUSD')
    proposal = propose_open(mt5=mock_app.mt5,cfg=mock_app.cfg,store=mock_app.store,symbol='XAUUSD',
                            side='buy',stop_loss=tick['ask']-5,comment='agent:fixture')
    assert proposal['ok'] and not proposal['blocked'], proposal
    result = confirm(mt5=mock_app.mt5,cfg=mock_app.cfg,store=mock_app.store,proposal_id=proposal['proposal_id'])
    assert result['ok'], result
    p = mock_app.mt5.positions()[0]
    assert p['magic'] == MAGIC and p['comment'].startswith('tmcp:')
    state = state_for(mock_app.store,account_key(mock_app.mt5.account_info()))
    assert state['intents'][p['comment']]['status'] == 'awaiting_position'
    assert p['sl'] > 0 and p['tp'] > 0


def test_native_entry_blocked_before_reservation_if_costs_unverified(mock_app):
    broker,account,_,deals,_ = fixture_broker()
    broker.request = lambda *a:{'ownership_supported':True,'ownership_method':'comment_reservation'}
    deals.append({**deal(1,10,'in'),'type':'buy','costs_complete':False})
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True,backend='native')
    with pytest.raises(ValueError):
        reserve_entry(broker,cfg,mock_app.store)
    assert state_for(mock_app.store,account_key(account))['intents'] == {}


def test_native_comment_reservation_without_magic_can_match_only_owned_position(mock_app):
    broker,account,positions,deals,calls = fixture_broker()
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True,allow_trading=True,backend='native')
    broker.request = lambda *a:{'ownership_supported':True,'ownership_method':'comment_reservation'}
    # Unrelated old deals establish transport cost completeness, not ownership.
    deals.append({**deal(1,999,'in'),'type':'buy','costs_complete':True})
    key,marker = reserve_entry(broker,cfg,mock_app.store)
    assert state_for(mock_app.store,key)['intents'][marker]['ownership'] == 'comment_reservation'
    positions.extend([{'ticket':10,'magic':0,'comment':marker,'sl':90,'profit':2,'swap':0},
                      {'ticket':99,'magic':0,'comment':'manual','sl':90,'profit':-100,'swap':0}])
    deals.append({**deal(2,10,'in'),'comment':marker,'magic':0,'type':'buy'})
    inspect_positions(broker,cfg,mock_app.store,execute=True)
    assert calls == [10]


def test_comment_without_journal_reservation_does_not_confer_ownership(mock_app):
    broker,_,positions,_,calls = fixture_broker()
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True,allow_trading=True)
    positions.append({'ticket':10,'magic':MAGIC,'comment':'tmcp:not-reserved','sl':90,'profit':-100,'swap':0})
    inspect_positions(broker,cfg,mock_app.store,execute=True)
    assert not calls


def test_paper_kill_switch_suppresses_direct_guardian_close(mock_app):
    broker, account, positions, deals, calls = fixture_broker()
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True, allow_trading=True, paper_trading=True)
    own(mock_app.store, account, 'owned', 10)
    positions.append({'ticket': 10, 'magic': MAGIC, 'comment': 'owned', 'sl': 90, 'profit': -11, 'swap': 0})
    deals.append(deal(1, 10, 'in'))
    result = inspect_positions(broker, cfg, mock_app.store, execute=True)
    assert result['positions'][0]['exit_due']
    assert calls == []


def test_tick_loss_limit_persists_in_intent_and_survives_default_guardian(mock_app):
    broker, account, positions, deals, calls = fixture_broker()
    cfg = mock_app.cfg.with_overrides(demo_cash_policy=True, allow_trading=True,
                                      demo_cash_loss_usd=2)
    key, marker = reserve_entry(broker, cfg, mock_app.store)
    assert state_for(mock_app.store, key)['intents'][marker]['loss_usd'] == 2
    own(mock_app.store, account, marker, 10)
    mock_app.store.cash_guard_update(key, lambda state: state['intents'][marker].update(loss_usd=2))
    positions.append({'ticket': 10, 'magic': MAGIC, 'comment': marker,
                      'sl': 90, 'profit': -2.1, 'swap': 0})
    deals.append({**deal(1, 10, 'in'), 'magic': MAGIC, 'comment': marker})
    inspect_positions(broker, cfg.with_overrides(demo_cash_loss_usd=10), mock_app.store, execute=True)
    assert calls == [10]
