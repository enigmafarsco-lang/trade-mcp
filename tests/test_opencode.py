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
        if path == '/global/health':
            return {'healthy': True}
        if path == '/api/info':
            return {}
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
        if path == '/global/health':
            return {'healthy': True}
        if path == '/api/info':
            return {}
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


def test_v227_identity_and_stateless_generation(monkeypatch):
    calls = []
    def request(self, method, path, payload=None):
        calls.append((method, path, payload))
        if path == '/api/info':
            return {'version': '2.2.7', 'pid': 123, 'urls': ['http://localhost:4096'], 'paths': {'tmp': '/tmp/test'}}
        if path == '/api/experimental/generate/text':
            return {'data': {'text': '{"action":"wait","reason":"No setup"}'}}
        raise AssertionError('Unexpected endpoint: '+path)
    monkeypatch.setattr(OpenCodeClient, '_request', request)
    client = OpenCodeClient('http://localhost:4096', 'opencode/big-pickle')
    result = client.chat_json([{'role': 'user', 'content': 'bounded summary'}])
    assert result['action'] == 'wait'
    assert client.api_version == 'v2'
    assert client.last_session_id == ''
    assert len(calls) == 2
    assert calls[1][2] == {'prompt': 'user: bounded summary',
                          'model': {'providerID': 'opencode', 'id': 'big-pickle'}}
    assert all('/session' not in path for _, path, _ in calls)


@pytest.mark.parametrize('reply', [{}, {'data': {}}, {'data': {'text': ''}}, {'data': {'text': 5}}])
def test_v2_invalid_generation_fails_closed(reply, monkeypatch):
    client = OpenCodeClient('http://localhost:4096', api_version='v2')
    monkeypatch.setattr(client, '_request', lambda *args: reply)
    assert client.chat_json([]) is None


def test_unknown_server_cannot_generate(monkeypatch):
    calls = []
    def request(self, method, path, payload=None):
        calls.append((method, path))
        return {'version': '3.0.0', 'pid': 123, 'urls': []}
    monkeypatch.setattr(OpenCodeClient, '_request', request)
    assert OpenCodeClient('http://localhost:4096').chat_json([]) is None
    assert all(method == 'GET' for method, _ in calls)


def test_v227_over_http_with_basic_auth():
    import base64
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []
    expected = 'Basic '+base64.b64encode(b'opencode:ephemeral-test-only').decode()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, result):
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            assert self.headers['Authorization'] == expected
            seen.append(self.path)
            assert self.path == '/api/info'
            self.respond({'version': '2.2.7', 'pid': 42, 'urls': [], 'paths': {'tmp': '/tmp/test'}})

        def do_POST(self):
            assert self.headers['Authorization'] == expected
            seen.append(self.path)
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert self.path == '/api/experimental/generate/text'
            assert payload['model'] == {'providerID': 'opencode', 'id': 'big-pickle'}
            assert payload['prompt'] == 'user: test'
            self.respond({'data': {'text': '{"action":"wait"}'}})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = OpenCodeClient(f'http://127.0.0.1:{server.server_port}',
                                'opencode/big-pickle', 'ephemeral-test-only')
        assert client.chat_json([{'role': 'user', 'content': 'test'}]) == {'action': 'wait'}
        assert seen == ['/api/info', '/api/experimental/generate/text']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_private_desktop_registration_is_discovered_without_printing(tmp_path, monkeypatch, capsys):
    from trade_mcp.opencode import discover_desktop
    path = tmp_path/'opencode/service.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'url': 'http://127.0.0.1:49374', 'password': 'private-desktop-secret', 'pid': 123}))
    path.chmod(0o600)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    def health(self):
        assert self.password == 'private-desktop-secret'
        return {'api': 'v2', 'pid': 123}
    monkeypatch.setattr(OpenCodeClient, 'health', health)
    assert discover_desktop() == ('http://127.0.0.1:49374', 'private-desktop-secret')
    assert not capsys.readouterr().out


@pytest.mark.parametrize('case', ['public', 'symlink', 'stale', 'missing'])
def test_desktop_discovery_fails_closed(tmp_path, monkeypatch, case):
    from trade_mcp.opencode import discover_desktop
    path = tmp_path/'opencode/service.json'
    path.parent.mkdir()
    if case != 'missing':
        path.write_text(json.dumps({'url': 'http://127.0.0.1:49374', 'password': 'never-show-me', 'pid': 123}))
        path.chmod(0o644 if case == 'public' else 0o600)
    if case == 'symlink':
        target = path.with_suffix('.target')
        path.rename(target)
        path.symlink_to(target)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    monkeypatch.setattr(OpenCodeClient, 'health', lambda _: {'api': 'v2', 'pid': 456})
    with pytest.raises(RuntimeError) as error:
        discover_desktop()
    assert 'never-show-me' not in str(error.value)


@pytest.mark.parametrize(('status','body','code'), [
    (502, '{"reason":"rate_limit","status":402,"message":"Insufficient balance secret-xyz"}', 'billing_required'),
    (502, 'Free usage limit reached secret-xyz', 'quota_exhausted'),
    (429, 'secret-xyz', 'rate_limited'),
    (401, 'secret-xyz', 'authentication_failed'),
    (404, 'secret-xyz', 'endpoint_unavailable'),
    (500, 'secret-xyz', 'connection_failed'),
])
def test_provider_errors_are_classified_without_echoing_body(status, body, code):
    from trade_mcp.opencode import classify_provider_error
    result = classify_provider_error(status, body).safe_dict()
    assert result['code'] == code
    assert 'secret-xyz' not in json.dumps(result)


def test_billing_failure_is_saved_on_model_client(monkeypatch):
    from trade_mcp.opencode import ModelServiceError
    client = OpenCodeClient('http://localhost:4096', api_version='v2')
    def fail(*args):
        raise ModelServiceError('billing_required', 502)
    monkeypatch.setattr(client, '_request', fail)
    assert client.chat_json([]) is None
    assert client.last_error['code'] == 'billing_required'


def test_auth_failure_is_not_hidden_by_another_health_route(monkeypatch):
    from trade_mcp.opencode import ModelServiceError
    calls = []
    def request(*args):
        calls.append(args)
        raise ModelServiceError('authentication_failed', 401)
    client = OpenCodeClient('http://localhost:4096')
    monkeypatch.setattr(client, '_request', request)
    with pytest.raises(ModelServiceError) as error:
        client.health()
    assert error.value.code == 'authentication_failed'
    assert len(calls) == 1


def test_v2_sends_exact_model_variant(monkeypatch):
    requests = []
    client = OpenCodeClient('http://localhost:49374', 'opencode/deepseek-v4.1-flash-free', api_version='v2', variant='max')
    def request(method, path, payload):
        requests.append(payload)
        return {'data':{'text':'OK'}}
    monkeypatch.setattr(client, '_request', request)
    assert client.chat([{'role':'user','content':'test'}]) == 'OK'
    assert requests[0]['model'] == {'providerID':'opencode', 'id':'deepseek-v4.1-flash-free', 'variant':'max'}


def test_free_fallback_uses_only_bounded_allowlist_and_records_actual_model(monkeypatch):
    from trade_mcp.opencode import ModelServiceError
    calls = []
    def once(self, messages, temperature):
        calls.append((self.model, self.variant))
        if len(calls) == 1:
            raise ModelServiceError('model_unavailable', 502)
        return '{"action":"wait"}'
    monkeypatch.setattr(OpenCodeClient, '_chat_once', once)
    client = OpenCodeClient('http://localhost:49374', 'opencode/deepseek-v4.1-flash-free',
                            variant='max', free_fallback=True)
    assert client.chat_json([]) == {'action':'wait'}
    assert calls == [('opencode/deepseek-v4.1-flash-free','max'), ('opencode/glm-5.3-flash-free','low')]
    assert client.response_model == 'opencode/glm-5.3-flash-free'
    assert client.model == 'opencode/deepseek-v4.1-flash-free'
    assert client.variant == 'max'
    assert client.attempts[0]['error'] == 'model_unavailable'


@pytest.mark.parametrize('code', ['quota_exhausted','billing_required','rate_limited',
                                  'authentication_failed','connection_failed','invalid_response'])
def test_free_fallback_does_not_rotate_on_shared_or_ambiguous_failures(monkeypatch, code):
    from trade_mcp.opencode import ModelServiceError
    calls = []
    def once(self, *args):
        calls.append(self.model)
        raise ModelServiceError(code)
    monkeypatch.setattr(OpenCodeClient, '_chat_once', once)
    client = OpenCodeClient('http://localhost:49374', 'opencode/beam-free', free_fallback=True)
    assert client.chat_json([]) is None
    assert len(calls) == 1
    assert client.last_error['code'] == code


def test_free_fallback_never_exceeds_three_requests(monkeypatch):
    from trade_mcp.opencode import ModelServiceError
    def once(*args):
        raise ModelServiceError('model_unavailable')
    monkeypatch.setattr(OpenCodeClient, '_chat_once', once)
    client = OpenCodeClient('http://localhost:49374', 'opencode/iceman-free', free_fallback=True)
    assert client.chat_json([]) is None
    assert len(client.attempts) == 3
    assert client.model == 'opencode/iceman-free'


@pytest.mark.parametrize('model', ['opencode/big-pickle','opencode/paid','other/beam-free',''])
def test_free_fallback_rejects_unverified_primary_without_request(monkeypatch, model):
    monkeypatch.setattr(OpenCodeClient, '_chat_once', lambda *a: pytest.fail('Must not generate'))
    assert OpenCodeClient('http://localhost:49374', model, free_fallback=True).chat_json([]) is None


def test_model_scoped_error_classification_does_not_override_shared_quota_or_auth():
    from trade_mcp.opencode import classify_provider_error
    assert classify_provider_error(502, '{"code":"model_quota_exceeded"}').code == 'model_unavailable'
    assert classify_provider_error(401, 'model_unavailable').code == 'authentication_failed'
    assert classify_provider_error(502, 'Free usage limit reached model_quota_exceeded').code == 'quota_exhausted'
