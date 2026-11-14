"""Page-owned analysis never confirms and pauses on loss of lease or model failure."""
import threading
from types import SimpleNamespace

import pytest

from trade_mcp.cycle import PageCycle


@pytest.fixture()
def setup_cycle(mock_app, monkeypatch):
    monkeypatch.setattr(threading.Thread, 'start', lambda _: None)
    cfg = mock_app.cfg.with_overrides(opencode_url='http://127.0.0.1:49374')
    state = SimpleNamespace(cfg=cfg, app=mock_app, store=mock_app.store, lock=threading.Lock(),
                            model_test={'state':'ready', 'model_key':(cfg.opencode_model,cfg.opencode_variant)}, scheduler=SimpleNamespace(running=False))
    cycle = PageCycle(state)
    agent = mock_app.store.create_agent({'name':'Dedicated','symbols':['XAUUSD'],'mode':'auto'})
    yield cycle, state, agent
    cycle.close()


def start(cycle, agent):
    return cycle.command({'action':'start','agent_id':agent['id'],'consent_model_requests':True,'interval':30})


def test_cycle_requires_explicit_consent_and_successful_model_test(setup_cycle):
    cycle, state, agent = setup_cycle
    assert cycle.command({'action':'start','agent_id':agent['id']})['error'] == 'model_usage_consent_required'
    state.model_test = {'state':'failed'}
    assert start(cycle, agent)['error'] == 'successful_model_test_required'


def test_only_page_owner_can_stop_and_heartbeat(setup_cycle):
    cycle, _, agent = setup_cycle
    result = start(cycle, agent)
    assert result['ok']
    assert cycle.command({'action':'stop','lease':'wrong'})['error'] == 'cycle_owner_mismatch'
    assert cycle.status()['active']
    assert cycle.command({'action':'heartbeat','lease':result['lease']})['ok']
    assert cycle.command({'action':'stop','lease':result['lease']})['ok']
    assert not cycle.status()['active']


def test_no_model_calls_after_heartbeat_expiry(setup_cycle, monkeypatch):
    from trade_mcp.agents import AgentEngine
    cycle, _, agent = setup_cycle
    start(cycle, agent)
    cycle.deadline = 0
    monkeypatch.setattr(AgentEngine, 'run', lambda *a: pytest.fail('Lease expired'))
    cycle.tick()
    assert not cycle.status()['active']


def test_cycle_forces_advisory_and_pauses_on_provider_failure(setup_cycle, monkeypatch):
    from trade_mcp.agents import AgentEngine
    cycle, _, agent = setup_cycle
    calls = []
    def run(self, a):
        calls.append(a)
        assert a['mode'] == 'advisory'
        assert a['require_open_market']
        return {'decisions':[{'model_error':{'code':'quota_exhausted','message':'quota unavailable'}}]}
    monkeypatch.setattr(AgentEngine, 'run', run)
    start(cycle, agent)
    cycle.tick()
    assert not cycle.status()['active']
    assert cycle.status()['reason'] == 'quota unavailable'
    cycle.next_run = 0
    cycle.tick()
    assert len(calls) == 1


def test_cycle_waits_for_pending_signal_without_duplicate_requests(setup_cycle, monkeypatch):
    from trade_mcp.agents import AgentEngine
    cycle, state, agent = setup_cycle
    state.store.create_proposal({'symbol':'XAUUSD','kind':'open'})
    monkeypatch.setattr(AgentEngine, 'run', lambda *a: pytest.fail('Do not duplicate pending signals'))
    start(cycle, agent)
    cycle.tick()
    assert cycle.status()['active']
    assert 'پیشنهاد معتبر' in cycle.status()['reason']


def test_cycle_does_not_overlap_runs_or_background_scheduler(setup_cycle, monkeypatch):
    cycle, state, agent = setup_cycle
    state.scheduler.running = True
    assert start(cycle, agent)['error'] == 'stop_background_scheduler_first'
    state.scheduler.running = False
    start(cycle, agent)
    cycle.busy = True
    cycle.tick()
    assert cycle.busy
