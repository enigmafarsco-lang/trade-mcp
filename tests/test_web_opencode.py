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


def test_default_variant_normalization_and_safe_diagnostics(mock_app, monkeypatch):
    c = client(mock_app)
    c.oc.variant = ''
    def request(self, method, path, payload=None):
        if path == '/api/info':
            return {'version':'2.2.7','pid':123,'urls':[]}
        if path == '/api/session':
            return {'data': {**payload, 'model':{**payload['model'],'variant':''}, 'id':'ses_web'}}
        if path.endswith('/generate'):
            return {'data': {'text':'OK','tokens':{}}}
        return {}
    monkeypatch.setattr(OpenCodeClient, '_request', request)
    mock_app.store.activate_model_budget()
    assert c.chat([]) == 'OK'
    assert c.transport_diagnostics['generation_submitted'] is True
    assert c.transport_diagnostics['stage'] == 'generation_response'


def test_rejected_acknowledgement_explains_no_generation(mock_app, monkeypatch):
    c = client(mock_app)
    def request(self, method, path, payload=None):
        if path == '/api/info':
            return {'version':'2.2.7','pid':123,'urls':[]}
        if path == '/api/session':
            return {'data': {**payload, 'permissions':[], 'id':'ses_web'}}
        assert not path.endswith('/generate')
        return {}
    monkeypatch.setattr(OpenCodeClient, '_request', request)
    mock_app.store.activate_model_budget()
    with pytest.raises(ProviderError) as exc:
        c.chat([])
    diagnostic = exc.value.safe_dict()['transport']
    assert diagnostic == {'stage':'session_validation', 'generation_submitted':False,
                          'permissions_acknowledged':False, 'model_acknowledged':True}


def test_session_probe_never_generates_or_reserves_budget(mock_app, monkeypatch):
    calls = []
    def request(self, method, path, payload=None):
        calls.append((method,path))
        if path == '/api/info':
            return {'version':'2.2.7','pid':123,'urls':[]}
        if path == '/api/session':
            return {'data':{**payload,'id':'ses_probe'}}
        assert method == 'DELETE'
        return {}
    monkeypatch.setattr(OpenCodeClient, '_request', request)
    before = mock_app.store.model_budget_status()
    result = client(mock_app).check_session()
    assert result['ok'] and result['generation_requested'] is False
    assert result['transport']['model_acknowledged'] is True
    assert calls[-1] == ('DELETE','/api/session/ses_probe')
    assert mock_app.store.model_budget_status() == before
    assert mock_app.store.list_model_usage() == []


def test_model_mismatch_uses_official_switch_then_verified_readback(mock_app, monkeypatch):
    calls = []
    expected = {}
    def request(self, method, path, payload=None):
        calls.append((method,path))
        if path == '/api/info':
            return {'version':'2.2.7','pid':123,'urls':[]}
        if path == '/api/session':
            expected.update(payload)
            return {'data': {**payload,'id':'ses_owned','model':{}}}
        if path.endswith('/model'):
            assert payload['model'] == expected['model']
            return {}  # HTTP 204
        if method == 'GET':
            return {'data':{**expected,'id':'ses_owned'}}
        assert method == 'DELETE'  # Probe never invokes generate.
        return {}
    monkeypatch.setattr(OpenCodeClient, '_request', request)
    result = client(mock_app).check_session()
    assert result['ok']
    assert result['transport']['model_acknowledged'] is True
    assert result['transport']['generation_submitted'] is False
    assert ('POST','/api/session/ses_owned/model') in calls
    assert ('GET','/api/session/ses_owned') in calls
    assert mock_app.store.list_model_usage() == []


@pytest.mark.parametrize('mismatch', ['model','permissions','id'])
def test_model_switch_never_bypasses_failed_readback(mock_app, monkeypatch, mismatch):
    expected = {}
    def request(self, method, path, payload=None):
        if path == '/api/info':
            return {'version':'2.2.7','pid':123,'urls':[]}
        if path == '/api/session':
            expected.update(payload)
            return {'data':{**payload,'model':{},'id':'ses_owned'}}
        if path.endswith('/model'):
            return {}
        if method == 'GET':
            result = {**expected,'id':'ses_owned'}
            result[mismatch] = {'model':{},'permissions':[],'id':'ses_unrelated'}[mismatch]
            return {'data':result}
        assert not path.endswith('/generate')
        return {}
    monkeypatch.setattr(OpenCodeClient, '_request', request)
    result = client(mock_app).check_session()
    assert not result['ok']
    assert result['transport']['generation_submitted'] is False
    assert result['transport']['stage'] == 'session_model_readback'


def test_opencode_no_content_response_is_success(monkeypatch):
    class Response:
        status = 204
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *args):
            pytest.fail('204 response must not be parsed as JSON')
    class Opener:
        def open(self, *args, **kwargs):
            return Response()
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    assert OpenCodeClient('http://localhost:49374')._request('POST','/api/session/ses_owned/model',{}) == {}
