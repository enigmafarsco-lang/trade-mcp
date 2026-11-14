"""OpenCode v1 protocol contract and fail-closed web-agent integration."""
import json

import pytest

from trade_mcp.agents import AgentEngine, LLMClient, compact_context
from trade_mcp.opencode import OpenCodeClient


@pytest.mark.parametrize('url', ['https://example.com', 'http://example.com',
                                  'http://user:secret@localhost:4096',
                                  'http://localhost:4096/path'])
def test_only_local_server_is_allowed(url):
    with pytest.raises(RuntimeError, match='loopback'):
        OpenCodeClient(url).health()


def test_session_permissions_model_text_and_cleanup(monkeypatch):
    calls = []
    def request(self, method, path, payload=None):
        calls.append((method, path, payload))
        if path == '/session':
            return {'id': 'ses_test', 'permission': payload['permission']}
        if path.endswith('/message'):
            return {'info': {}, 'parts': [{'type': 'text', 'text': '{"action":"wait"}'}]}
        return {}
    monkeypatch.setattr(OpenCodeClient, '_request', request)
    client = OpenCodeClient('http://localhost:4096', 'opencode/big-pickle')
    assert client.chat_json([{'role': 'user', 'content': 'bounded context'}]) == {'action': 'wait'}
    assert calls[0][2]['permission'] == [{'permission': '*', 'pattern': '*', 'action': 'deny'}]
    assert calls[1][2]['model'] == {'providerID': 'opencode', 'modelID': 'big-pickle'}
    assert calls[-2][:2] == ('POST', '/session/ses_test/abort')
    assert calls[-1][:2] == ('DELETE', '/session/ses_test')


def test_missing_permissions_fail_before_prompt(monkeypatch):
    calls = []
    def request(self, method, path, payload=None):
        calls.append(path)
        return {'id': 'ses_test'} if path == '/session' else {}
    monkeypatch.setattr(OpenCodeClient, '_request', request)
    assert OpenCodeClient('http://localhost:4096').chat_json([]) is None
    assert '/session/ses_test/message' not in calls
    assert calls[-1] == '/session/ses_test'


def test_context_omits_account_identity_and_contains_sizing():
    ctx = compact_context({'account': {'login': 'secret-account', 'balance': 100},
                           'scenario': {'sizing': {'minimum_volume_loss': 1.38}}})
    assert 'secret-account' not in json.dumps(ctx)
    assert ctx['scenario']['sizing']['minimum_volume_loss'] == 1.38


def test_opencode_failure_never_uses_rules_or_claims_success(mock_app, monkeypatch):
    cfg = mock_app.cfg.with_overrides(opencode_url='http://localhost:4096')
    client = LLMClient.from_config(cfg)
    assert isinstance(client, OpenCodeClient)
    monkeypatch.setattr(client, 'chat_json', lambda *_: None)
    engine = AgentEngine(cfg=cfg, mt5=mock_app.mt5, store=mock_app.store, llm=client)
    decision = engine._decide({}, {'scenario': {'action': 'sell', 'confidence': 90}})
    assert decision['action'] == 'wait'
    assert decision['source'] == 'opencode_error'
    assert decision['llm_status'] == 'failed'
    assert engine._auto_confirm_blockers({}, {}, 'auto')
    assert engine._auto_confirm_blockers({}, {}, 'guarded')


def test_opencode_valid_decision_source(mock_app, monkeypatch):
    cfg = mock_app.cfg.with_overrides(opencode_url='http://localhost:4096')
    engine = AgentEngine(cfg=cfg, mt5=mock_app.mt5, store=mock_app.store)
    monkeypatch.setattr(engine.llm, 'chat_json', lambda *_: {'action': 'wait', 'reason': 'No setup'})
    result = engine._decide({}, {})
    assert result['source'] == 'opencode'
    assert result['action'] == 'wait'


def test_mcp_creates_disabled_advisory_agent(mock_app, monkeypatch):
    from trade_mcp import server
    monkeypatch.setattr(server, 'get_app', lambda: mock_app)
    result = server.create_web_agent('Review', ['XAUUSD'], max_risk=20, max_lot=10)
    agent = result['agent']
    assert agent['mode'] == 'advisory'
    assert not agent['enabled']
    assert agent['max_risk'] == mock_app.cfg.max_risk_percent
    assert agent['max_lot'] == mock_app.cfg.max_lot
    assert server.list_web_agents()['agents'][0]['id'] == agent['id']
    assert server.run_web_agent(agent['id'])['error'] == 'opencode_not_configured_in_this_process'
    assert mock_app.mt5.positions() == []


def test_agent_run_failure_reports_no_model_usage(mock_app, monkeypatch):
    cfg = mock_app.cfg.with_overrides(opencode_url='http://localhost:4096')
    engine = AgentEngine(cfg=cfg, mt5=mock_app.mt5, store=mock_app.store)
    monkeypatch.setattr(engine.llm, 'chat_json', lambda *_: None)
    report = engine.run({'name': 'No server', 'symbols': ['XAUUSD'], 'mode': 'auto'})
    assert report['used_llm'] is False
    assert report['model_backend'] == 'opencode'
    assert report['proposals'] == []
    assert report['decisions'][0]['action'] == 'wait'
    assert mock_app.mt5.positions() == []
