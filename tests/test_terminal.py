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


def test_opencode_web_model_test_and_redacted_settings(mock_app, monkeypatch):
    from trade_mcp.opencode import OpenCodeClient
    mock_app.cfg = mock_app.cfg.with_overrides(opencode_url='http://127.0.0.1:4096',
                                               opencode_password='never-show-this')
    calls = []
    def chat(self, messages):
        calls.append(messages)
        return 'OK'
    monkeypatch.setattr(OpenCodeClient, 'chat', chat)
    with console(mock_app) as base:
        with urlopen(base+'/api/settings') as r:
            raw = r.read().decode()
        assert 'never-show-this' not in raw
        assert json.loads(raw)['llm']['backend'] == 'opencode'
        req = Request(base+'/api/model-test', data=b'{}', headers={'Content-Type': 'application/json'})
        with urlopen(req) as r:
            result = json.load(r)
            assert result['ok'] and result['backend'] == 'opencode'
        assert len(calls) == 1
        assert mock_app.mt5.positions() == []


def test_workspace_shows_external_saved_report_expiry_and_billing(mock_app, monkeypatch):
    from trade_mcp.opencode import ModelServiceError, OpenCodeClient
    mock_app.cfg = mock_app.cfg.with_overrides(opencode_url='http://127.0.0.1:4096',
                                               opencode_password='never-echo')
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
        raise ModelServiceError('billing_required', 502)
    monkeypatch.setattr(OpenCodeClient, 'chat', fail)
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
    from trade_mcp.opencode import OpenCodeClient
    mock_app.cfg.allow_trading = True
    mock_app.cfg.opencode_url = 'http://127.0.0.1:4096'
    price = mock_app.mt5.tick('XAUUSD')['ask']
    def chat(self, messages):
        return json.dumps({'action':'buy', 'confidence':80, 'stop_loss':price-5,
                           'take_profit':price+10, 'risk_percent':.5, 'reason':'Mock-only test'})
    monkeypatch.setattr(OpenCodeClient, 'chat', chat)
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
        assert decision['source'] == 'opencode'
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


def test_model_variant_can_be_explicitly_configured_without_generation(mock_app, monkeypatch):
    from trade_mcp.opencode import OpenCodeClient
    mock_app.cfg.opencode_url = 'http://localhost:49374'
    calls = []
    def chat(self, messages):
        calls.append((self.model, self.variant))
        return 'OK'
    monkeypatch.setattr(OpenCodeClient, 'chat', chat)
    with console(mock_app) as base:
        def post(path, data):
            with urlopen(Request(base+path, data=json.dumps(data).encode(), headers={'Content-Type':'application/json'})) as r:
                return json.load(r)
        result = post('/api/model-config', {'model':'opencode/deepseek-v4.1-flash-free','variant':'max'})
        assert result['ok'] and not result['persisted']
        assert calls == []
        assert not mock_app.cfg.allow_trading
        post('/api/model-test', {})
        assert calls == [('opencode/deepseek-v4.1-flash-free','max')]
        with urlopen(base+'/api/workspace-status') as r:
            status = json.load(r)
        assert status['model_variant'] == 'max'
        assert status['model_test']['state'] == 'ready'
        assert not status['page_cycle']['active']


def test_cross_origin_posts_are_rejected(mock_app):
    with console(mock_app) as base:
        req = Request(base+'/api/model-test', data=b'{}', headers={'Origin':'http://foreign.example','Content-Type':'application/json'})
        with pytest.raises(HTTPError) as error:
            urlopen(req)
        assert error.value.code == 403
