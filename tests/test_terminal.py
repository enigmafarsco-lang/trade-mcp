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
            assert json.load(r) == {'ok': True, 'backend': 'opencode'}
        assert len(calls) == 1
        assert mock_app.mt5.positions() == []
