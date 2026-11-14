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
    assert calls[0][-1] == 'console'
    assert '--no-browser' not in calls[0]


def test_demo_flag_enables_console_only_after_check(monkeypatch):
    monkeypatch.setattr(connect, 'read_clipboard', configuration)
    commands = []
    def run(command, **kw):
        assert kw['env']['TRADE_MCP_ALLOW_TRADING'] == 'false'
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout='OK', stderr='')
    monkeypatch.setattr(connect.subprocess, 'run', run)
    def launch(command, **kw):
        assert kw['env']['TRADE_MCP_ALLOW_TRADING'] == 'true'
        return 0
    monkeypatch.setattr(connect.subprocess, 'call', launch)
    assert connect.main(['--console','--enable-demo-trading']) == 0
    assert len(commands) == 2
    assert commands[-1][1] == '-c'


def test_demo_flag_rejects_unverified_account(monkeypatch):
    monkeypatch.setattr(connect, 'read_clipboard', configuration)
    results = iter([0,3])
    monkeypatch.setattr(connect.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=next(results),stdout='',stderr=''))
    monkeypatch.setattr(connect.subprocess, 'call', lambda *a, **kw: pytest.fail('must not launch'))
    assert connect.main(['--console','--enable-demo-trading']) == 3


def test_model_setup_verifies_without_printing_key(monkeypatch, capsys):
    from trade_mcp.agents import LLMClient
    answers = iter(['https://provider.example/v1', 'test-model'])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    monkeypatch.setattr(connect.getpass, 'getpass', lambda _: 'private-model-key')
    monkeypatch.setattr(LLMClient, 'chat', lambda self, messages: 'OK')
    env = {}
    assert connect.configure_llm(env)
    assert env['TRADE_MCP_LLM_MODEL'] == 'test-model'
    assert env['TRADE_MCP_LLM_API_KEY'] == 'private-model-key'
    assert 'private-model-key' not in capsys.readouterr().out


def test_model_setup_failure_does_not_leak_or_install_config(monkeypatch, capsys):
    from trade_mcp.agents import LLMClient
    answers = iter(['https://provider.example/v1', 'test-model'])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    monkeypatch.setattr(connect.getpass, 'getpass', lambda _: 'private-model-key')
    def fail(self, messages):
        raise RuntimeError('private-model-key')
    monkeypatch.setattr(LLMClient, 'chat', fail)
    env = {}
    assert not connect.configure_llm(env)
    assert env == {}
    assert 'private-model-key' not in capsys.readouterr().out


def test_model_setup_rejects_remote_plain_http(monkeypatch):
    answers = iter(['http://provider.example/v1', 'test-model'])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    assert not connect.configure_llm({})


def test_opencode_launcher_reuses_server_and_passes_config(monkeypatch):
    from trade_mcp.opencode import OpenCodeClient
    monkeypatch.setattr(connect, 'read_clipboard', configuration)
    monkeypatch.setattr(connect.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='OK', stderr=''))
    monkeypatch.setattr(OpenCodeClient, 'health', lambda self: {'healthy': True})
    def launch(command, **kw):
        assert kw['env']['TRADE_MCP_OPENCODE_URL'] == 'http://127.0.0.1:4096'
        assert kw['env']['TRADE_MCP_OPENCODE_MODEL'] == 'opencode/big-pickle'
        assert kw['env']['TRADE_MCP_ALLOW_TRADING'] == 'false'
        assert kw['env']['TRADE_MCP_MCP_TOKEN'] == KEY
        return 0
    monkeypatch.setattr(connect.subprocess, 'call', launch)
    assert connect.main(['--console', '--opencode', '--opencode-model', 'opencode/big-pickle']) == 0


def test_owned_opencode_does_not_inherit_mt5_credentials(monkeypatch):
    from trade_mcp.opencode import OpenCodeClient
    monkeypatch.setenv('TRADE_MCP_MCP_TOKEN', 'inherited-secret')
    monkeypatch.setenv('MT5_MCP_TOKEN', 'another-secret')
    monkeypatch.setattr(connect, 'read_clipboard', configuration)
    monkeypatch.setattr(connect.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='OK', stderr=''))
    checks = iter([False, True])
    def health(self):
        if not next(checks):
            raise RuntimeError('not running')
        return {'healthy': True}
    monkeypatch.setattr(OpenCodeClient, 'health', health)
    monkeypatch.setattr(connect.shutil, 'which', lambda _: '/usr/bin/opencode')
    stopped = []
    def spawn(command, **kw):
        assert not any(k.startswith(('TRADE_MCP_', 'MT5_MCP_')) for k in kw['env'])
        assert len(kw['env']['OPENCODE_PASSWORD']) >= 32
        assert kw['env']['OPENCODE_PASSWORD'] == kw['env']['OPENCODE_SERVER_PASSWORD']
        assert command == ['opencode', 'serve', '--hostname', '127.0.0.1', '--port', '4096']
        return SimpleNamespace(poll=lambda: None, terminate=lambda: stopped.append(True), wait=lambda **kw: 0)
    monkeypatch.setattr(connect.subprocess, 'Popen', spawn)
    monkeypatch.setattr(connect.subprocess, 'call', lambda *a, **kw: 0)
    assert connect.main(['--console', '--opencode']) == 0
    assert stopped == [True]


def test_console_browser_can_be_disabled_explicitly(monkeypatch):
    monkeypatch.setattr(connect, 'read_clipboard', configuration)
    monkeypatch.setattr(connect.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='OK', stderr=''))
    calls = []
    monkeypatch.setattr(connect.subprocess, 'call', lambda command, **kw: calls.append(command) or 0)
    assert connect.main(['--console', '--no-browser']) == 0
    assert calls[0][-2:] == ['console', '--no-browser']


@pytest.mark.parametrize(('message', 'expected'), [
    ('EADDRINUSE secret-key', 'port 4096 is already occupied'),
    ('ConfigInvalidError secret-key', 'OpenCode configuration was rejected'),
    ('unknown option secret-key', 'the installed OpenCode CLI does not accept the server command'),
    ('arbitrary secret-key', 'server startup or health check failed; the exact cause is not yet known'),
])
def test_startup_diagnostics_do_not_echo_child_secrets(message, expected):
    assert connect.startup_failure_reason(message) == expected
    assert 'secret-key' not in connect.startup_failure_reason(message)
