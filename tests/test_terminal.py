import json
import threading
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from trade_mcp.console import ConsoleServer


@contextmanager
def console(app):
    server = ConsoleServer(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.httpd.server_port}'
    finally:
        server.stop()
        thread.join(timeout=3)


def test_terminal_snapshot_assets_and_watch(mock_app):
    with console(mock_app) as base:
        for path, content_type in [('/terminal.js', 'text/javascript'), ('/terminal.css', 'text/css')]:
            with urlopen(base+path) as r:
                assert content_type in r.headers['Content-Type']
                assert len(r.read()) > 100
        with urlopen(base+'/api/terminal?symbol=XAUUSD&tf=H1&selected=ema,macd,rsi') as r:
            data = json.load(r)
        assert data['ok'] and data['timeframe'] == 'H1'
        assert len(data['bars']) == 300
        assert set(data['series']) == {'ema', 'macd', 'rsi'}
        assert data['training_budget'] == 10000  # fixture explicitly opts in
        assert data['orders'] == []
        with urlopen(base+'/api/marketwatch') as r:
            rows = json.load(r)['symbols']
        assert rows[0]['bid'] > 0
        assert mock_app.mt5.positions() == []
        with pytest.raises(HTTPError) as e:
            urlopen(base+'/api/terminal?tf=BAD')
        assert e.value.code == 400


def test_explanation_requires_config_and_redacts_identity(mock_app, monkeypatch):
    mock_app.cfg.llm_base_url = ''
    mock_app.cfg.llm_model = ''
    with console(mock_app) as base:
        request = Request(base+'/api/terminal/explain', data=b'{"symbol":"XAUUSD"}',
                          headers={'Content-Type': 'application/json'})
        with pytest.raises(HTTPError) as e:
            urlopen(request)
        assert e.value.code == 409
        mock_app.cfg.llm_base_url = 'http://model.invalid/v1'
        mock_app.cfg.llm_model = 'test-model'
        mock_app.cfg.llm_api_key = 'private-api-key'
        mock_app.store.activate_model_budget()

        def chat(client, messages):
            payload = json.loads(messages[-1]['content'])
            assert set(payload) == {'symbol', 'quote', 'bias', 'scenario', 'warnings'}
            assert 'login' not in payload
            assert 'private-api-key' not in json.dumps(messages)
            return 'سناریو تضمین سود ندارد.'

        monkeypatch.setattr('trade_mcp.console.LLMClient.chat', chat)
        with urlopen(request) as r:
            result = json.load(r)
        assert result['used_llm'] is True
        assert result['executable'] is False
        assert result['model'] == 'test-model'
        assert mock_app.mt5.positions() == []


def test_default_workspace_and_package_assets():
    from pathlib import Path

    import trade_mcp.console as module

    web = Path(module.WEB_DIR)
    html = (web/'index.html').read_text()
    assert '<section id="tab-terminal" class="active">' in html
    assert '/terminal.js' in html and '/terminal.css' in html
    assert 'desk-llm' in html
    assert '/api/terminal' in (web/'terminal.js').read_text()
    assert 'localStorage' in (web/'terminal.js').read_text()


def test_incremental_chart_and_fast_quotes(mock_app, monkeypatch):
    calls = []
    original = mock_app.mt5.candles

    def candles(symbol, tf, count):
        calls.append(count)
        return original(symbol, tf, count)

    monkeypatch.setattr(mock_app.mt5, 'candles', candles)
    with console(mock_app) as base:
        for _ in range(2):
            with urlopen(base+'/api/chart?symbol=XAUUSD&tf=M15&selected=ema,rsi') as r:
                data = json.load(r)
            assert len(data['bars']) == 300
            assert len(data['series']['ema']['20']) == 300
        assert calls == [300, 3]
        with urlopen(base+'/api/quotes?symbols=XAUUSD,XAUUSD,EURUSD') as r:
            prices = json.load(r)
        assert set(prices['quotes']) == {'XAUUSD', 'EURUSD'}
        assert prices['quotes']['XAUUSD']['bid'] > 0
        with urlopen(base+'/api/account-state') as r:
            state = json.load(r)
        assert state['account']['trade_mode'] == 'demo'
        assert mock_app.mt5.positions() == []


def test_manual_demo_preview_no_execution(mock_app):
    mock_app.cfg.training_budget = 10000
    quote = mock_app.mt5.tick('XAUUSD')
    body = {'action': 'open', 'symbol': 'XAUUSD', 'side': 'buy',
            'sl': quote['ask']-3, 'tp': quote['ask']+6, 'risk': .5}
    with console(mock_app) as base:
        request = Request(base+'/api/manual/propose', data=json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request) as r:
            data = json.load(r)
        assert data['ok'] and data['blocked']
        assert data['proposal_id']
        assert data['preview']['type'] == 'market'
        assert mock_app.mt5.positions() == []
        confirmation = Request(base+'/api/proposals/'+data['proposal_id']+'/confirm',
                               data=b'{}', headers={'Content-Type': 'application/json'})
        with urlopen(confirmation) as r:
            assert json.load(r)['error'] == 'trading_disabled'
        assert mock_app.mt5.positions() == []


def test_manual_rejects_real_account(mock_app, monkeypatch):
    monkeypatch.setattr(mock_app.mt5, 'account_info', lambda **kw: {'trade_mode': 'real'})
    with console(mock_app) as base:
        for path in ['/api/manual/propose', '/api/proposals/anything/confirm']:
            with pytest.raises(HTTPError) as exc:
                urlopen(Request(base+path, data=b'{}', headers={'Content-Type': 'application/json'}))
            assert exc.value.code == 403


def test_manual_demo_confirm_mock_only(mock_app):
    mock_app.cfg.allow_trading = True
    mock_app.cfg.training_budget = 10000
    quote = mock_app.mt5.tick('XAUUSD')
    with console(mock_app) as base:
        def post(path, payload):
            with urlopen(Request(base+path, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})) as r:
                return json.load(r)

        proposal = post('/api/manual/propose', {'action':'open', 'symbol':'XAUUSD',
                         'side':'buy', 'sl':quote['ask']-3, 'tp':quote['ask']+6, 'risk':.5})
        assert not proposal['blocked']
        result = post('/api/proposals/'+proposal['proposal_id']+'/confirm', {})
        assert result['ok']
        assert len(mock_app.mt5.positions()) == 1
        position = mock_app.mt5.positions()[0]
        modify = post('/api/manual/propose', {'action':'modify','ticket':str(position['ticket']),
                                            'tp':quote['ask']+9})
        assert modify['preview']['new_sl'] == position['sl']
        assert len(mock_app.mt5.positions()) == 1
        close = post('/api/manual/propose', {'action':'close', 'ticket':str(position['ticket'])})
        assert close['preview']['close_volume'] == position['volume']
        assert not close['blocked']
        done = post('/api/proposals/'+close['proposal_id']+'/confirm', {})
        assert done['executed'] is True
        assert mock_app.mt5.positions() == []


def test_watch_selected_and_automatic_stop_preview(mock_app):
    mock_app.cfg.training_budget = 10000
    with console(mock_app) as base:
        with urlopen(base+'/api/marketwatch') as r:
            selected = json.load(r)['symbols']
        assert all(s['visible'] for s in selected)
        assert 'XAUUSD.a' not in [s['symbol'] for s in selected]
        with urlopen(base+'/api/marketwatch?all=1') as r:
            assert any(not s['visible'] for s in json.load(r)['symbols'])
        with urlopen(base+'/api/order-ticket?symbol=XAUUSD') as r:
            assert json.load(r)['spec']['volume_min'] == .01
        req = Request(base+'/api/manual/propose', data=json.dumps({
            'symbol':'XAUUSD','side':'buy','volume':.01,'auto_sl':True}).encode(),
            headers={'Content-Type':'application/json'})
        with urlopen(req) as r:
            d = json.load(r)
        assert d['auto_sl'] and d['ok'] and d['blocked']
        assert d['preview']['stop_loss'] > 0
        assert mock_app.mt5.positions() == []


def test_web_model_test_and_redacted_settings(mock_app, monkeypatch):
    from trade_mcp.agents import LLMClient
    mock_app.cfg = mock_app.cfg.with_overrides(
        llm_base_url='https://provider.example/v1', llm_model='web-model',
        llm_api_key='never-show-this', llm_billing_project='web-billing',
        llm_usage_convention='openai_inclusive',
        opencode_url='http://127.0.0.1:4096',
        opencode_password='never-show-opencode')
    mock_app.store.activate_model_budget()
    calls = []
    def chat(self, messages):
        calls.append(messages)
        self.last_usage = {'input_tokens': 3, 'cached_input_tokens': 0,
                           'output_tokens': 1, 'reasoning_tokens': None,
                           'cost_amount': None, 'cost_currency': None,
                           'usage_source': 'partial', 'cost_source': 'unknown',
                           'accounting_convention': 'openai_inclusive'}
        return 'OK'
    monkeypatch.setattr(LLMClient, 'chat', chat)
    with console(mock_app) as base:
        with urlopen(base+'/api/settings') as r:
            raw = r.read().decode()
        assert 'never-show-this' not in raw
        assert 'never-show-opencode' not in raw
        settings = json.loads(raw)
        assert settings['llm']['backend'] == 'openai-compatible'
        assert settings['llm']['mode'] == 'web'
        assert settings['llm']['api_key_configured'] is True
        assert settings['llm']['billing_project'] == 'web-billing'
        assert settings['llm']['usage_convention'] == 'openai_inclusive'
        req = Request(base+'/api/model-test', data=b'{}', headers={'Content-Type': 'application/json'})
        with urlopen(req) as r:
            result = json.load(r)
        assert result['ok'] and result['backend'] == 'openai-compatible'
        assert result['usage']['input_tokens'] == 3
        assert len(calls) == 1
        assert mock_app.mt5.positions() == []


def test_model_test_requires_explicit_budget(mock_app, monkeypatch):
    from trade_mcp.agents import LLMClient
    mock_app.cfg = mock_app.cfg.with_overrides(
        llm_base_url='https://provider.example/v1', llm_model='web-model')
    calls = []
    def chat(self, messages):
        calls.append(messages)
        return 'OK'
    monkeypatch.setattr(LLMClient, 'chat', chat)
    with console(mock_app) as base:
        req = Request(base+'/api/model-test', data=b'{}',
                      headers={'Content-Type': 'application/json'})
        with pytest.raises(HTTPError) as error:
            urlopen(req)
        assert error.value.code == 429
        payload = json.loads(error.value.read())
        assert payload['error'] == 'test_budget_not_activated'
        assert payload['budget']['state'] == 'not_activated'
        assert calls == []
    assert mock_app.mt5.positions() == []


def test_workspace_shows_external_saved_report_expiry_and_billing(mock_app, monkeypatch):
    from trade_mcp.agents import LLMClient
    from trade_mcp.model_usage import ProviderError
    from trade_mcp.opencode import ModelServiceError
    mock_app.cfg = mock_app.cfg.with_overrides(
        llm_base_url='https://provider.example/v1', llm_model='web-model',
        llm_api_key='never-echo', opencode_password='never-echo-opencode')
    mock_app.store.activate_model_budget()
    a = mock_app.store.create_agent({'name': 'External agent', 'symbols': ['XAUUSD']})
    mock_app.store.mark_agent_run(a['id'], {'used_llm': False, 'decisions': [
        {'symbol': 'XAUUSD', 'action': 'wait', 'source': 'opencode_error',
         'model_error': ModelServiceError('billing_required', 502).safe_dict()}]})
    p = mock_app.store.create_proposal({'symbol': 'XAUUSD', 'volume': .01})
    mock_app.store._conn.execute('UPDATE proposals SET expires_at=1 WHERE id=?', (p['id'],))
    mock_app.store._conn.commit()
    count = []
    def fail(self, messages):
        count.append(1)
        raise ProviderError('billing_required', 502)
    monkeypatch.setattr(LLMClient, 'chat', fail)
    with console(mock_app) as base:
        with urlopen(base+'/api/workspace-status') as r:
            d = json.load(r)
        assert not count  # Read polling never invokes the provider.
        assert d['agents'][0]['report']['decisions'][0]['model_error']['code'] == 'billing_required'
        assert d['proposals'][0]['status'] == 'expired'
        assert not d['proposals'][0]['confirmable']
        assert mock_app.store.get_proposal(p['id'])['status'] == 'pending'  # Display-only expiry.
        req = Request(base+'/api/model-test', data=b'{}', headers={'Content-Type':'application/json'})
        with urlopen(req) as r:
            error = json.load(r)
        assert error['error'] == 'billing_required'
        with urlopen(base+'/api/workspace-status') as r:
            status = json.load(r)
        assert status['model_test']['state'] == 'failed'
        assert status['model_test']['code'] == 'billing_required'
        assert 'never-echo' not in json.dumps(status)
        assert len(count) == 1
        with urlopen(base+'/workspace.js') as r:
            assert 'javascript' in r.headers['Content-Type']
    assert mock_app.mt5.positions() == []


def test_model_test_surfaces_provider_retry_window(mock_app, monkeypatch):
    from trade_mcp.agents import LLMClient
    from trade_mcp.model_usage import ProviderError
    mock_app.cfg = mock_app.cfg.with_overrides(
        llm_base_url='https://provider.example/v1', llm_model='web-model')
    mock_app.store.activate_model_budget()
    def fail(self, messages):
        raise ProviderError('quota_exhausted', 502, retry_after_ms=49741000)
    monkeypatch.setattr(LLMClient, 'chat', fail)
    with console(mock_app) as base:
        req = Request(base+'/api/model-test', data=b'{}', headers={'Content-Type':'application/json'})
        with urlopen(req) as r:
            result = json.load(r)
        assert result['ok'] is False
        assert result['code'] == 'quota_exhausted'
        assert result['retry_after_ms'] == 49741000
        with urlopen(base+'/api/workspace-status') as r:
            status = json.load(r)
        assert status['model_test']['state'] == 'failed'
        assert status['model_test']['retry_after_ms'] == 49741000
    assert mock_app.mt5.positions() == []


def test_expired_proposal_confirmation_is_rejected_server_side(mock_app):
    p = mock_app.store.create_proposal({'symbol': 'XAUUSD', 'volume': .01})
    mock_app.store._conn.execute('UPDATE proposals SET expires_at=1 WHERE id=?', (p['id'],))
    mock_app.store._conn.commit()
    with console(mock_app) as base:
        req = Request(base+'/api/proposals/'+p['id']+'/confirm', data=b'{}',
                      headers={'Content-Type':'application/json'})
        with urlopen(req) as r:
            result = json.load(r)
        assert result['ok'] is False
        assert result['error'] == 'expired'
    assert mock_app.mt5.positions() == []


def test_workspace_advisory_action_does_not_use_saved_auto_mode(mock_app, monkeypatch):
    from trade_mcp.agents import AgentEngine
    agent = mock_app.store.create_agent({'name':'Auto', 'symbols':['XAUUSD'], 'mode':'auto'})
    seen = []
    def run(self, a):
        seen.append(a['mode'])
        return {'used_llm':False, 'proposals':[]}
    monkeypatch.setattr(AgentEngine, 'run', run)
    with console(mock_app) as base:
        req = Request(base+'/api/agents/'+agent['id'], data=b'{"action":"run_advisory"}',
                      headers={'Content-Type':'application/json'})
        with urlopen(req) as r:
            assert json.load(r)['ok']
    assert seen == ['advisory']
    assert mock_app.store.get_agent(agent['id'])['mode'] == 'auto'


def test_complete_model_to_web_preview_to_separate_confirmation_mock_only(mock_app, monkeypatch):
    from trade_mcp.agents import LLMClient
    mock_app.cfg.allow_trading = True
    mock_app.cfg.llm_base_url = 'https://provider.example/v1'
    mock_app.cfg.llm_model = 'web-model'
    mock_app.store.activate_model_budget()
    price = mock_app.mt5.tick('XAUUSD')['ask']
    def chat(self, messages):
        return json.dumps({'action':'buy', 'confidence':80, 'stop_loss':price-5,
                           'take_profit':price+10, 'risk_percent':.5, 'reason':'Mock-only test'})
    monkeypatch.setattr(LLMClient, 'chat', chat)
    agent = mock_app.store.create_agent({'name':'Cycle test','symbols':['XAUUSD'],
                                        'mode':'auto','max_lot':.1,'max_risk':.5})
    with console(mock_app) as base:
        def post(path, payload):
            req = Request(base+path, data=json.dumps(payload).encode(),
                          headers={'Content-Type':'application/json'})
            with urlopen(req) as r:
                return json.load(r)
        run = post('/api/agents/'+agent['id'], {'action':'run_advisory'})
        assert run['report']['used_llm']
        decision = run['report']['decisions'][0]
        assert decision['source'] == 'web'
        assert decision['status'] == 'pending'
        assert decision['preview']['volume'] > 0
        assert decision['preview']['risk_amount'] > 0
        assert decision['preview']['estimated_profit'] > 0
        assert mock_app.mt5.positions() == []
        with urlopen(base+'/api/workspace-status') as r:
            visible = json.load(r)
        assert visible['agents'][0]['report']['used_llm']
        pid = decision['proposal_id']
        assert visible['proposals'][0]['id'] == pid
        assert visible['proposals'][0]['confirmable']
        assert mock_app.mt5.positions() == []  # Reading preview never executes.
        done = post('/api/proposals/'+pid+'/confirm', {})
        assert done['executed'] is True
        assert len(mock_app.mt5.positions()) == 1
        again = post('/api/proposals/'+pid+'/confirm', {})
        assert again['error'] == 'not_pending'
        assert len(mock_app.mt5.positions()) == 1


def test_model_can_be_explicitly_configured_without_generation(mock_app, monkeypatch):
    from trade_mcp.agents import LLMClient
    mock_app.cfg = mock_app.cfg.with_overrides(
        llm_base_url='https://provider.example/v1', llm_model='web-model')
    mock_app.store.activate_model_budget()
    calls = []
    def chat(self, messages):
        calls.append(self.model)
        return 'OK'
    monkeypatch.setattr(LLMClient, 'chat', chat)
    with console(mock_app) as base:
        def post(path, data):
            with urlopen(Request(base+path, data=json.dumps(data).encode(), headers={'Content-Type':'application/json'})) as r:
                return json.load(r)
        result = post('/api/model-config', {'model':'web-model-2'})
        assert result['ok'] and not result['persisted']
        assert result['backend'] == 'openai-compatible'
        assert calls == []
        assert not mock_app.cfg.allow_trading
        post('/api/model-test', {})
        assert calls == ['web-model-2']
        with urlopen(base+'/api/workspace-status') as r:
            status = json.load(r)
        assert status['model'] == 'web-model-2'
        assert status['model_backend'] == 'openai-compatible'
        assert status['model_test']['state'] == 'ready'
        assert not status['page_cycle']['active']


def test_cross_origin_posts_are_rejected(mock_app):
    with console(mock_app) as base:
        req = Request(base+'/api/model-test', data=b'{}', headers={'Origin':'http://foreign.example','Content-Type':'application/json'})
        with pytest.raises(HTTPError) as error:
            urlopen(req)
        assert error.value.code == 403


def test_offline_token_estimate_uses_shared_messages_without_side_effects(mock_app, monkeypatch):
    from trade_mcp.agents import LLMClient, analysis_messages
    captured = []
    import trade_mcp.console as module
    original = module.analysis_messages
    def capture(agent, analysis):
        messages = original(agent, analysis)
        assert messages == analysis_messages(agent, analysis)
        captured.append(messages)
        return messages
    monkeypatch.setattr(module, 'analysis_messages', capture)
    monkeypatch.setattr(LLMClient, 'chat', lambda *a, **kw: pytest.fail('No provider call allowed'))
    agent = mock_app.store.create_agent({'name':'Offline','symbols':['XAUUSD']})
    before = (mock_app.store.list_proposals(), mock_app.store.list_model_usage(),
              mock_app.store.agent_run_history(), mock_app.store.model_budget_status())
    with console(mock_app) as base:
        req = Request(base+'/api/estimate-analysis-tokens', data=json.dumps({'agent_id':agent['id']}).encode(),
                      headers={'Content-Type':'application/json'})
        with urlopen(req) as response:
            data = json.load(response)
        with urlopen(base+'/api/order-ticket?symbol=XAUUSD') as response:
            assert json.load(response)['backend'] == 'mock'
    assert data['ok'] and data['backend'] == 'mock'
    assert data['characters'] == sum(len(m['content']) for m in captured[0])
    assert data['estimated_input_tokens'] == (data['characters']+3)//4
    assert data['exact'] is False and data['tokenizer'] is None
    assert data['provider_attempted'] is False
    assert all(data[k] is None for k in ('output_tokens','reasoning_tokens','cached_input_tokens','cost'))
    after = (mock_app.store.list_proposals(), mock_app.store.list_model_usage(),
             mock_app.store.agent_run_history(), mock_app.store.model_budget_status())
    assert before == after


@pytest.mark.parametrize('payload,error', [({},'unknown_agent'),
    ({'symbol':'EURUSD'},'symbol_not_in_agent')])
def test_offline_token_estimate_rejects_invalid_selection(mock_app, payload, error):
    agent = mock_app.store.create_agent({'name':'Offline','symbols':['XAUUSD']})
    if payload:
        payload = {**payload,'agent_id':agent['id']}
    with console(mock_app) as base:
        req = Request(base+'/api/estimate-analysis-tokens', data=json.dumps(payload).encode(),
                      headers={'Content-Type':'application/json'})
        with pytest.raises(HTTPError) as exc:
            urlopen(req)
        assert exc.value.code == 400
        assert json.load(exc.value)['error'] == error


def test_session_model_change_no_generation_and_no_budget_reset(mock_app, monkeypatch):
    from trade_mcp.opencode import OpenCodeClient
    monkeypatch.setattr(OpenCodeClient, '_request', lambda *a, **kw: pytest.fail('No OpenCode network on configuration'))
    mock_app.cfg.web_model_backend = 'opencode-session'
    mock_app.cfg.opencode_url = 'http://localhost:49374'
    mock_app.cfg.opencode_model = 'opencode/deepseek-v4.1-flash-free'
    mock_app.cfg.opencode_variant = 'low'
    mock_app.store.activate_model_budget()
    mock_app.store.reserve_model_budget()
    budget = mock_app.store.model_budget_status()
    with console(mock_app) as base:
        req = Request(base+'/api/model-config', data=json.dumps({
            'model':'opencode/mimo-v2.6-flash-free'}).encode(),
            headers={'Content-Type':'application/json'})
        with urlopen(req) as r:
            data = json.load(r)
        assert data['ok'] and data['variant'] == ''
        with urlopen(base+'/api/workspace-status') as r:
            state = json.load(r)
        assert state['model'] == 'opencode/mimo-v2.6-flash-free'
        assert state['model_variant'] == ''
        assert state['model_test']['state'] == 'not_tested'
    after = mock_app.store.model_budget_status()
    assert {k:v for k,v in after.items() if k != "seconds_remaining"} == {k:v for k,v in budget.items() if k != "seconds_remaining"}
    assert mock_app.store.list_model_usage() == []


@pytest.mark.parametrize('model,variant', [('opencode/big-pickle',''),
    ('opencode/paid',''), ('opencode/mimo-v2.6-flash-free','low')])
def test_session_model_config_rejects_nonfree_and_inherited_variant(mock_app, model, variant):
    mock_app.cfg.web_model_backend = 'opencode-session'
    original = mock_app.cfg.opencode_model
    with console(mock_app) as base:
        req = Request(base+'/api/model-config', data=json.dumps({'model':model,'variant':variant}).encode(),
                      headers={'Content-Type':'application/json'})
        with pytest.raises(HTTPError) as exc:
            urlopen(req)
        assert exc.value.code == 400
    assert mock_app.cfg.opencode_model == original


def test_explicit_desktop_connection_configures_only_web_without_generation(mock_app, monkeypatch):
    from trade_mcp.opencode import OpenCodeClient
    from trade_mcp.workspace import workspace_identity
    monkeypatch.setattr('trade_mcp.opencode.discover_desktop', lambda: ('http://127.0.0.1:49374','private-desktop-password'))
    monkeypatch.setattr(OpenCodeClient, '_request', lambda *a, **kw: pytest.fail('No generation from configuration'))
    identity = workspace_identity(mock_app.cfg)
    budget = mock_app.store.model_budget_status()
    with console(mock_app) as base:
        req = Request(base+'/api/model-connect-opencode', data=json.dumps({
            'model':'opencode/mimo-v2.6-flash-free','consent_connect':True}).encode(),
            headers={'Content-Type':'application/json'})
        with urlopen(req) as r:
            data = json.load(r)
        assert data['ok'] and data['generation_requested'] is False
        with urlopen(base+'/api/workspace-status') as r:
            raw = r.read().decode()
        assert 'private-desktop-password' not in raw
        state = json.loads(raw)
        assert state['model_configured']
        assert state['model_backend'] == 'opencode-session'
        assert state['model'] == 'opencode/mimo-v2.6-flash-free'
        assert state['model_variant'] == ''
        assert state['model_test']['state'] == 'not_tested'
    assert workspace_identity(mock_app.cfg) == identity
    assert mock_app.store.model_budget_status() == budget
    assert mock_app.store.list_model_usage() == []
    assert mock_app.cfg.opencode_password == 'private-desktop-password'


def test_desktop_discovery_failure_does_not_modify_configuration(mock_app, monkeypatch):
    def fail():
        raise RuntimeError('raw secret must not be exposed')
    monkeypatch.setattr('trade_mcp.opencode.discover_desktop', fail)
    before = (mock_app.cfg.web_model_backend, mock_app.cfg.opencode_model, mock_app.cfg.opencode_url)
    with console(mock_app) as base:
        req = Request(base+'/api/model-connect-opencode', data=json.dumps({
            'model':'opencode/mimo-v2.6-flash-free','consent_connect':True}).encode(),
            headers={'Content-Type':'application/json'})
        with pytest.raises(HTTPError) as exc:
            urlopen(req)
        assert exc.value.code == 409
        raw = exc.value.read().decode()
        assert 'raw secret' not in raw
        assert json.loads(raw)['error'] == 'desktop_discovery_failed'
    assert before == (mock_app.cfg.web_model_backend, mock_app.cfg.opencode_model, mock_app.cfg.opencode_url)


@pytest.mark.parametrize('payload', [{'model':'opencode/mimo-v2.6-flash-free'},
                                    {'model':'opencode/paid','consent_connect':True}])
def test_desktop_connect_requires_consent_and_free_model(mock_app, monkeypatch, payload):
    monkeypatch.setattr('trade_mcp.opencode.discover_desktop', lambda: pytest.fail('Must not discover'))
    with console(mock_app) as base:
        req = Request(base+'/api/model-connect-opencode', data=json.dumps(payload).encode(),
                      headers={'Content-Type':'application/json'})
        with pytest.raises(HTTPError) as exc:
            urlopen(req)
        assert exc.value.code == 400


def test_status_proves_live_runtime_revisions(mock_app):
    import os

    with console(mock_app) as base:
        with urlopen(base+'/api/workspace-status') as r:
            runtime = json.load(r)['runtime']
        assert runtime['pid'] == os.getpid()
        assert runtime['started_at'] > 0
        assert runtime['console_revision'] == 'runtime-proof-v1'
        assert runtime['session_adapter_revision'] == 'session-model-readback-v2'
