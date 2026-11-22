import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location('research_check', ROOT/'scripts/xauusd_research_check.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_discovery_does_not_confuse_current_quote_or_orders_with_history():
    m = module()
    tools = [{'name': n} for n in ('get_symbol_info', 'get_chart_history', 'trade_send_ticks', 'get_quote')]
    assert m.describe_tick_history(tools)['status'] == 'no_tick_history_tool_advertised'
    tools.append({'name': 'copy_ticks_range', 'inputSchema': {
        'required': ['symbol'], 'properties': {'symbol': {'type': 'string', 'default': 'SECRET'},
                                             'from': {'type': 'integer'}}}})
    r = m.describe_tick_history(tools)
    assert r['status'] == 'candidate_advertised'
    assert r['historical_bid_ask_verified'] is False
    assert 'SECRET' not in json.dumps(r)


def test_script_only_initializes_and_lists_tools(tmp_path, monkeypatch, capsys):
    m = module()
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            calls.append('initialize')

        def list_tools(self):
            calls.append('tools/list')
            print('SECRET_RAW')
            return []

        def call_tool(self, *args, **kwargs):
            raise AssertionError('No business tool call allowed')

    monkeypatch.setattr(m, 'McpHttpClient', Client)
    monkeypatch.setattr(m, 'load_dotenv', lambda: None)
    monkeypatch.setattr(m.Config, 'from_env', lambda: SimpleNamespace(
        backend='native', mcp_url='private', mcp_token='SECRET', timeout=20))
    out = tmp_path/'report.json'
    monkeypatch.setattr(m.sys, 'argv', ['check', str(out)])
    assert m.main() == 0
    assert calls == ['initialize', 'tools/list']
    assert 'SECRET' not in capsys.readouterr().out
    assert out.stat().st_mode & 0o777 == 0o600
    assert json.loads(out.read_text())['backtest_executed'] is False
