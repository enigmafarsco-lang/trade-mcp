"""Isolated session usage contract, no live OpenCode/provider calls."""
import pytest

from trade_mcp.agents import LLMClient
from trade_mcp.model_usage import BudgetDenied, ProviderError
from trade_mcp.opencode import OpenCodeClient


def client(mock_app):
    cfg = mock_app.cfg.with_overrides(web_model_backend='opencode-session',
        opencode_url='http://localhost:49374', opencode_model='opencode/deepseek-v4.1-flash-free',
        opencode_variant='low')
    return LLMClient.from_config(cfg, mode='web', store=mock_app.store)


def test_isolated_session_usage_and_cleanup(mock_app, monkeypatch):
    calls = []
    def request(self, method, path, payload=None):
        calls.append((method,path,payload))
        if path == '/api/info':
            return {'version':'2.2.7','pid':123,'urls':[]}
        if path == '/api/session':
            assert payload['permissions'] == [{'action':'*','resource':'*','effect':'deny'}]
            assert payload['model']['variant'] == 'low'
            return {'data': {**payload,'id':'ses_web'}}
        if path.endswith('/generate'):
            return {'data': {'text':'{"action":"wait"}', 'tokens': {
                'input':120,'output':20,'reasoning':5,'cache':{'read':80,'write':0}}, 'cost':0}}
        return {}
    monkeypatch.setattr(OpenCodeClient, '_request', request)
    c = client(mock_app)
    with pytest.raises(BudgetDenied):
        c.chat([])
    assert calls == []
    mock_app.store.activate_model_budget()
    assert c.chat_json([{'role':'user','content':'Market snapshot'}]) == {'action':'wait'}
    row = mock_app.store.list_model_usage()[0]
    assert row['input_tokens'] == 120
    assert row['cached_input_tokens'] == 80
    assert row['output_tokens'] == 20
    assert row['reasoning_tokens'] == 5
    assert row['cost_amount'] is None
    assert row['accounting_convention'] == 'unknown'
    assert row['provider'] == 'opencode-session'
    assert calls[-1][:2] == ('DELETE','/api/session/ses_web')
    assert not any('development' in str(p) for _,_,p in calls)


def test_session_refuses_unacknowledged_permissions(mock_app, monkeypatch):
    def request(self, method, path, payload=None):
        if path == '/api/info':
            return {'version':'2.2.7','pid':123,'urls':[]}
        if path == '/api/session':
            return {'data': {'id':'ses_web','model':payload['model']}}
        assert not path.endswith('/generate')
        return {}
    monkeypatch.setattr(OpenCodeClient, '_request', request)
    mock_app.store.activate_model_budget()
    with pytest.raises(ProviderError):
        client(mock_app).chat([])
    assert mock_app.store.list_model_usage()[0]['ok'] == 0


def test_session_quota_failure_records_unknown_usage_and_cooldown(mock_app, monkeypatch):
    from trade_mcp.opencode import ModelServiceError
    def request(self, method, path, payload=None):
        if path == '/api/info':
            return {'version':'2.2.7','pid':123,'urls':[]}
        if path == '/api/session':
            return {'data': {**payload,'id':'ses_web'}}
        if path.endswith('/generate'):
            raise ModelServiceError('quota_exhausted', 502, retry_after_ms=60000)
        return {}
    monkeypatch.setattr(OpenCodeClient, '_request', request)
    mock_app.store.activate_model_budget()
    c = client(mock_app)
    with pytest.raises(ProviderError):
        c.chat([])
    row = mock_app.store.list_model_usage()[0]
    assert row['input_tokens'] is None
    assert row['error_code'] == 'quota_exhausted'
    assert not mock_app.store.model_budget_status()['allowed']
