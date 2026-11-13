import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('connect_script', Path(__file__).parents[1]/'scripts/mt5_mcp_connect.py')
connect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connect)

KEY = 'example-terminal-secret-only-for-tests'
URL = 'http://127.0.0.1:22346/mcp'


def configuration():
    return json.dumps({'mcpServers': {
        'metaeditor': {'url':'http://127.0.0.1:22345/mcp','headers':{'Authorization':'Bearer other-key'}},
        'terminal': {'url':URL,'headers':{'Authorization':'Bearer '+KEY}},
    }})


def test_terminal_is_selected_from_copy_block():
    assert connect.parse_terminal('## Configuration\n'+configuration()) == (URL, KEY)


def test_toml_terminal_not_metaeditor():
    text = '''[mcp_servers.metaeditor]
url = "http://127.0.0.1:22345/mcp"
http_headers = { "Authorization" = "Bearer wrong-secret" }
[mcp_servers.terminal]
url = "http://127.0.0.1:22346/mcp"
enabled = true
http_headers = { "Authorization" = "Bearer example-terminal-secret-only-for-tests" }
'''
    assert connect.parse_terminal(text) == (URL, KEY)


@pytest.mark.parametrize('text', ['plain-key', '{}', '{"mcpServers":{"metaeditor":{}}}',
    json.dumps({'mcpServers':{'terminal':{'url':'https://attacker.invalid','headers':{'Authorization':'Bearer '+KEY}}}})])
def test_invalid_config_does_not_fallback(text):
    with pytest.raises(ValueError):
        connect.parse_terminal(text)


def test_key_hidden_and_console_not_started_after_401(monkeypatch, capsys):
    monkeypatch.setattr(connect, 'read_clipboard', configuration)
    def run(command, **kw):
        assert KEY not in ' '.join(command)
        assert kw['env']['TRADE_MCP_MCP_TOKEN'] == KEY
        assert kw['env']['TRADE_MCP_ALLOW_TRADING'] == 'false'
        return SimpleNamespace(returncode=1, stdout='mcp key : '+KEY+'\nHTTP 401\n', stderr='')
    monkeypatch.setattr(connect.subprocess, 'run', run)
    monkeypatch.setattr(connect.subprocess, 'call', lambda *a, **kw: pytest.fail('must not start console'))
    assert connect.main(['--console']) == 1
    out = capsys.readouterr().out
    assert KEY not in out
    assert 'HTTP 401' in out


def test_success_starts_console_with_copied_key(monkeypatch):
    monkeypatch.setattr(connect, 'read_clipboard', configuration)
    monkeypatch.setattr(connect.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='doctor: OK', stderr=''))
    calls = []
    def launch(command, **kw):
        calls.append(command)
        assert kw['env']['TRADE_MCP_MCP_TOKEN'] == KEY
        return 0
    monkeypatch.setattr(connect.subprocess, 'call', launch)
    assert connect.main(['--console']) == 0
    assert calls[0][-2:] == ['console', '--no-browser']
