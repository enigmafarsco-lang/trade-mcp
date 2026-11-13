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
