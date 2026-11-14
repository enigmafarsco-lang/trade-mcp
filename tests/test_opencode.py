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
