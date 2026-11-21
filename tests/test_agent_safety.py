"""Post-response entry safety: offline only, no providers or orders."""
from datetime import datetime, timezone

import pytest

from trade_mcp.agent_safety import post_response_check


def inputs(side='buy'):
    return dict(quote={'bid':100,'ask':100.1,'tick_time':datetime.fromtimestamp(1000, timezone.utc).isoformat()},
                tick={'bid':100,'ask':100.1,'time':1000}, info={'point':.01},
                decision={'action':side,'stop_loss':99 if side == 'buy' else 101,
                          'take_profit':102 if side == 'buy' else 98},
                elapsed=1, now=1001, max_spread_points=60)


@pytest.mark.parametrize('side', ['buy','sell'])
def test_fresh_entry_on_both_sides_passes_without_modifying_levels(side):
    data = inputs(side)
    original = dict(data['decision'])
    assert post_response_check(**data)['ok']
    assert data['decision'] == original


@pytest.mark.parametrize('fault,reason', [
    ('slow','analysis_expired'), ('old_tick','fresh_quote_time_invalid_or_stale'),
    ('future','fresh_quote_time_invalid_or_stale'), ('spread','spread_too_wide_after_response'),
    ('drift','entry_drift_exceeded'), ('nan','decision_levels_invalid'),
    ('crossed','decision_levels_crossed_or_wrong_side'),
])
def test_rejects_changed_or_stale_conditions(fault, reason):
    data = inputs()
    if fault == 'slow':
        data['elapsed'] = 80
    elif fault == 'old_tick':
        data['tick']['time'] = 980
    elif fault == 'future':
        data['tick']['time'] = 1010
    elif fault == 'spread':
        data['tick']['ask'] = 101
    elif fault == 'drift':
        data['tick'].update(bid=100.4, ask=100.5)
    elif fault == 'nan':
        data['decision']['stop_loss'] = float('nan')
    else:
        data['decision']['take_profit'] = 100.05
    result = post_response_check(**data)
    assert not result['ok'] and reason in result['reasons']


@pytest.mark.parametrize('side', ['buy','sell'])
@pytest.mark.parametrize('slow', [False, True])
def test_engine_proposes_only_after_post_response_checks(mock_app, monkeypatch, side, slow):
    from trade_mcp.agents import AgentEngine, LLMClient
    clock = [0.0]
    monkeypatch.setattr('trade_mcp.agents.time.monotonic', lambda: clock[0])
    agent = mock_app.store.create_agent({'name':'Safety','symbols':['XAUUSD'], 'mode':'advisory',
                                        'max_lot':.1, 'max_risk':.5})
    engine = AgentEngine(cfg=mock_app.cfg, mt5=mock_app.mt5, store=mock_app.store, llm=LLMClient())
    levels = {}
    def decide(agent, analysis):
        quote = analysis['quote']
        entry = float(quote['ask'] if side == 'buy' else quote['bid'])
        levels.update(stop_loss=entry-10 if side == 'buy' else entry+10,
                      take_profit=entry+20 if side == 'buy' else entry-20)
        clock[0] = 80 if slow else 1
        return {'action':side, 'confidence':80, 'source':'web', 'risk_percent':.1, **levels}
    monkeypatch.setattr(engine, '_decide', decide)
    before = mock_app.store.list_proposals()
    report = engine.run(agent)
    assert not report['errors'], report
    decision = report['decisions'][0]
    assert report['used_llm']  # Guard doesn't erase real-model provenance.
    if slow:
        assert decision['action'] == 'wait' and decision['model_action'] == side
        assert 'analysis_expired' in decision['post_response_check']['reasons']
        assert mock_app.store.list_proposals() == before
    else:
        assert decision['post_response_check']['ok']
        assert decision.get('proposal_id'), decision
        assert decision['stop_loss'] == round(levels['stop_loss'], 2)
        assert decision['take_profit'] == round(levels['take_profit'], 2)
        assert decision['status'] != 'executed'


def test_guard_expiring_during_margin_check_creates_nothing(mock_app, monkeypatch):
    from trade_mcp.execution import propose_open
    calls = []
    def guard(tick, info):
        calls.append(tick)
        return {'ok':len(calls) == 1, 'reasons':[] if len(calls) == 1 else ['analysis_expired']}
    tick = mock_app.mt5.tick('XAUUSD')
    before = mock_app.store.list_proposals()
    result = propose_open(mt5=mock_app.mt5,cfg=mock_app.cfg,store=mock_app.store,
                          symbol='XAUUSD',side='buy',stop_loss=tick['bid']-10,
                          take_profit=tick['ask']+20,volume=.01,entry_guard=guard)
    assert result['error'] == 'post_response_check_failed'
    assert len(calls) == 2 and calls[0] is calls[1]
    assert mock_app.store.list_proposals() == before


def test_sizing_refusal_has_structured_status_and_no_proposal(mock_app, monkeypatch):
    from trade_mcp.agents import AgentEngine, LLMClient
    agent = mock_app.store.create_agent({'name':'Minimum lot','symbols':['XAUUSD'],
                                        'mode':'advisory','max_risk':.01,'max_lot':.01})
    engine = AgentEngine(cfg=mock_app.cfg,mt5=mock_app.mt5,store=mock_app.store,llm=LLMClient())
    def decide(agent, analysis):
        entry = float(analysis['quote']['bid'])
        return {'action':'sell','source':'web','confidence':75,'risk_percent':.01,
                'stop_loss':entry+15,'take_profit':entry-20}
    monkeypatch.setattr(engine,'_decide',decide)
    before = mock_app.store.list_proposals()
    report = engine.run(agent)
    assert not report['errors'], report
    result = report['decisions'][0]
    assert result['post_response_check']['ok']
    # The refused decision stays visible, but the execution action is WAIT.
    assert result['decision_action'] == 'sell' and result['source'] == 'web'
    assert result['action'] == 'wait' and result['no_trade'] is True
    assert result['status'] == 'rejected' and result['blocked'] is True
    error = result['proposal_error']
    assert error['error'] == 'sizing_failed'
    assert error['sizing']['minimum_volume_loss'] > error['sizing']['risk_amount']
    assert error['checked_entry'] > 0 and error['effective_sizing_balance'] > 0
    assert not result.get('proposal_id')
    assert mock_app.store.list_proposals() == before
    assert mock_app.store.list_model_usage() == []
