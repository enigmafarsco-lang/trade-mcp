"""Research integration boundary tests: no upstream network or order execution."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trade_mcp.research import research_status, run_worker, validated_messages


def test_missing_worker_is_explicit(mock_app):
    assert not research_status(mock_app.cfg)['configured']
    assert run_worker(mock_app.cfg, {'operation':'prepare'})['error'] == 'research_not_configured'


def test_worker_does_not_receive_secrets(mock_app, monkeypatch):
    mock_app.cfg.research_python = sys.executable
    monkeypatch.setenv('TRADE_MCP_MCP_TOKEN','broker-secret')
    monkeypatch.setenv('TRADE_MCP_OPENCODE_PASSWORD','chat-secret')
    monkeypatch.setenv('OPENAI_API_KEY','model-secret')
    monkeypatch.setenv('PYTHONPATH','untrusted-path')
    def run(argv, **kwargs):
        assert argv[0] == sys.executable
        assert not any(k in kwargs['env'] for k in ('TRADE_MCP_MCP_TOKEN','OPENAI_API_KEY',
            'TRADE_MCP_OPENCODE_PASSWORD','PYTHONPATH'))
        assert kwargs['timeout'] == 30
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"messages":[]}', stderr='')
    monkeypatch.setattr('trade_mcp.research.subprocess.run', run)
    assert run_worker(mock_app.cfg, {'operation':'prepare'})['ok']


@pytest.mark.parametrize('messages', [[], [{'role':'tool','content':'x'}],
    [{'role':'system','content':{}}], [{'role':'user','content':'x'*100001}]])
def test_invalid_research_prompts_rejected(messages):
    with pytest.raises(ValueError):
        validated_messages({'messages':messages})


def test_research_api_uses_budgeted_client_and_never_proposes(mock_app, monkeypatch):
    from urllib.request import Request, urlopen

    from test_terminal import console

    from trade_mcp.agents import LLMClient
    mock_app.cfg.research_python = sys.executable
    mock_app.cfg.llm_base_url = 'http://localhost:9999/v1'
    mock_app.cfg.llm_model = 'fixture'
    agent = mock_app.store.create_agent({'name':'Research','symbols':['XAUUSD']})
    worker = Mock(return_value={'ok':True,'messages':[{'role':'user','content':'snapshot'}], 'engine':'fixture'})
    monkeypatch.setattr('trade_mcp.research.run_worker', worker)
    before = mock_app.store.list_proposals()
    with console(mock_app) as base:
        req = Request(base+'/api/research/analyze', data=json.dumps({
            'agent_id':agent['id'],'consent_model_request':True}).encode(),
            headers={'Content-Type':'application/json'})
        from urllib.error import HTTPError
        with pytest.raises(HTTPError) as exc:
            urlopen(req)
        assert json.load(exc.value)['error'] == 'test_budget_not_activated'
        assert mock_app.store.list_model_usage() == []
        mock_app.store.activate_model_budget()
        def response(self, req):
            assert self.request_kind == 'research_tradingagents'
            return {'choices':[{'message':{'content':'Research only'}}],
                    'usage':{'prompt_tokens':42,'completion_tokens':8}}, 200
        monkeypatch.setattr(LLMClient, '_response_data', response)
        with urlopen(req) as r:
            report = json.load(r)
        assert report['report'] == 'Research only' and report['executable'] is False
        assert mock_app.store.list_model_usage()[0]['input_tokens'] == 42
    assert mock_app.store.list_proposals() == before


def test_backtest_route_does_not_use_model_or_proposals(mock_app, monkeypatch):
    from urllib.request import Request, urlopen

    from test_terminal import console

    from trade_mcp.agents import LLMClient
    mock_app.cfg.research_python = sys.executable
    agent = mock_app.store.create_agent({'name':'Backtest','symbols':['XAUUSD']})
    monkeypatch.setattr(LLMClient, 'chat', lambda *a: pytest.fail('Backtest must not call a model'))
    def worker(cfg, work):
        assert work['operation'] == 'backtest'
        assert 60 <= len(work['bars']) <= 499
        return {'ok':True,'stats':{'# Trades':2}}
    monkeypatch.setattr('trade_mcp.research.run_worker', worker)
    before = mock_app.store.list_proposals()
    with console(mock_app) as base:
        req = Request(base+'/api/research/backtest', data=json.dumps({'agent_id':agent['id']}).encode(),
                      headers={'Content-Type':'application/json'})
        with urlopen(req) as r:
            report = json.load(r)
    assert report['backend'] == 'mock' and report['executable'] is False
    assert mock_app.store.list_model_usage() == []
    assert mock_app.store.list_proposals() == before


def test_real_pinned_worker_offline_when_configured(mock_app):
    import math
    import os
    interpreter = os.environ.get('RESEARCH_TEST_PYTHON')
    if not interpreter:
        pytest.skip('Optional isolated upstream environment not selected')
    mock_app.cfg.research_python = interpreter
    prepared = run_worker(mock_app.cfg, {'operation':'prepare', 'date':'2026-11-12',
        'symbol':'XAUUSD', 'context':{'data_backend':'mock','quote':{'bid':2673.5}}})
    assert prepared['ok'], prepared
    assert len(validated_messages(prepared)) >= 2
    bars = []
    for i in range(150):
        p = 100+5*math.sin(i/10)
        bars.append({'open':p,'high':p+1,'low':p-1,'close':p+.2})
    result = run_worker(mock_app.cfg, {'operation':'backtest','bars':bars})
    assert result['ok'], result
    assert result['stats']['# Trades'] > 0


@pytest.mark.parametrize('strategy', ['sma_cross','rsi_reversion','donchian_breakout'])
def test_strategy_signals_do_not_look_ahead(strategy):
    import math

    from trade_mcp.research_strategies import strategy_signals
    bars = [{'close':100+10*math.sin(i/8), 'high':101+10*math.sin(i/8),
             'low':99+10*math.sin(i/8)} for i in range(150)]
    full = strategy_signals(bars, strategy)
    assert full[:30] == [0]*30
    assert set(full) <= {-1,0,1}
    assert any(full)
    for end in (31,60,100):
        assert strategy_signals(bars[:end], strategy) == full[:end]


def test_unknown_strategy_rejected():
    from trade_mcp.research_strategies import strategy_signals
    with pytest.raises(ValueError):
        strategy_signals([], 'untrusted-import')
