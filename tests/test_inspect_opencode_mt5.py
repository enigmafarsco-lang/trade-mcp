"""Static inspection uses synthetic configs only; no OpenCode or MT5 is started."""
from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('inspect_opencode_mt5', ROOT / 'scripts/inspect_opencode_mt5.py')
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network is forbidden'))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network is forbidden'))
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('DNS is forbidden'))


def test_jsonc_preserves_urls_comments_inside_strings_and_escaped_quotes():
    value = inspector.parse_jsonc('''{
      // real comment, not the URL or quoted slash
      "url": "http://localhost:22346/mcp", /* block comment */
      "text": "a \\"quote\\" // still string", "values": [1, 2,],
    }''')
    assert value == {'url': 'http://localhost:22346/mcp', 'text': 'a "quote" // still string', 'values': [1, 2]}
    with pytest.raises(ValueError):
        inspector.parse_jsonc('{"a": 1, "a": 2}')
    with pytest.raises(ValueError):
        inspector.parse_jsonc('{"a": 1 /* comment */ 2}')


def test_static_inspection_redacts_credentials_paths_and_unknown_names(tmp_path):
    root, home = tmp_path / 'project', tmp_path / 'home'
    root.mkdir()
    global_dir = home / '.config' / 'opencode'
    global_dir.mkdir(parents=True)
    secret = 'SECRET_PRIVATE_123456'
    private_name = 'private_server_' + secret
    (root / 'opencode.jsonc').write_text('''{
       "mcp": {"servers": {
          "trade": {"type": "local", "command": ["/private/trade-mcp", "serve"],
                    "environment": {"TRADE_MCP_BACKEND": "native",
                                    "TRADE_MCP_ALLOW_TRADING": "false",
                                    "TRADE_MCP_MCP_TOKEN": "''' + secret + '''"}},
          "''' + private_name + '''": {"type": "remote", "url": "http://127.0.0.1:22346/mcp",
                    "headers": {"Authorization": "Bearer ''' + secret + '''"}, "disabled": true},
       }},
    }''')
    (global_dir / 'opencode.json').write_text(json.dumps({'mcp': {
        'terminal': {'type': 'remote', 'url': 'http://127.0.0.1:22346/mcp',
                     'headers': {'Authorization': 'Bearer {env:MY_PRIVATE_NAME}'}, 'enabled': False}}}))
    (root / '.env').write_text('TRADE_MCP_MCP_TOKEN=' + secret + '\nTRADE_MCP_BACKEND=native\n')
    env = {'HOME': str(home), 'PATH': '', 'TRADE_MCP_MCP_TOKEN': secret,
           'OPENCODE_CONFIG': '/private/' + secret, 'OPENCODE_CONFIG_CONTENT': secret}
    snapshots = {p: p.read_bytes() for p in (root / 'opencode.jsonc', global_dir / 'opencode.json', root / '.env')}
    result = inspector.inspect(root, home, env, managed=tmp_path / 'nonexistent-managed')
    encoded = json.dumps(result)
    assert secret not in encoded and private_name not in encoded and str(tmp_path) not in encoded
    assert result['custom_config_env_set'] and result['inline_config_env_set']
    assert result['shell_token_keys_set']['TRADE_MCP_MCP_TOKEN']
    assert result['project_dotenv']['token_keys_set']['TRADE_MCP_MCP_TOKEN']
    assert result['effective_opencode_config'] == 'not_checked'
    assert result['running_opencode'] == 'not_checked' and result['new_network_calls'] == 0
    assert {p: p.read_bytes() for p in snapshots} == snapshots

    global_config = next(x for x in result['config_sources'] if x['scope'] == 'global')
    global_server = global_config['servers'][0]
    assert global_server['shape'] == 'direct' and global_server['name'] == 'terminal'
    assert global_server['endpoint'] == 'loopback_default_terminal'
    assert global_server['authorization_header'] == 'reference' and global_server['enabled'] == 'false'
    project_config = next(x for x in result['config_sources'] if x['scope'] == 'project')
    trade, other = project_config['servers']
    assert trade['shape'] == 'nested' and trade['command'] == 'trade_mcp_launcher'
    assert trade['backend_in_config'] == 'native' and trade['trading_in_config'] == 'false'
    assert trade['token_in_config']['TRADE_MCP_MCP_TOKEN'] == 'literal'
    assert other['name'] == 'other' and other['authorization_header'] == 'literal'
    assert other['disabled'] == 'true'


def test_invalid_json_and_oversized_config_are_not_interpreted(tmp_path):
    root, home = tmp_path / 'project', tmp_path / 'home'
    root.mkdir()
    home.mkdir()
    (root / 'opencode.json').write_text('{"mcp": {}, "mcp": {}}')
    (root / 'opencode.jsonc').write_text(' ' * (inspector.MAX_FILE_BYTES + 1))
    result = inspector.inspect(root, home, {'PATH': ''}, managed=tmp_path / 'empty')
    statuses = {source['format']: source['status'] for source in result['config_sources'] if source['scope'] == 'project'}
    assert statuses == {'json': 'invalid_or_unsupported', 'jsonc': 'too_large'}


def test_symlink_to_process_environment_is_never_read(tmp_path):
    path = tmp_path / 'opencode.json'
    path.symlink_to('/proc/self/environ')
    assert inspector._read(path) == ('excluded', None)


def test_truncation_and_unknown_or_malformed_values_are_explicit():
    config = {'mcp': {'servers': {f'server-{i}': {'type': 'other', 'url': 'https://private.example/'
                                                  + str(i), 'environment': {'TRADE_MCP_MCP_TOKEN': ['private']}}
                                 for i in range(inspector.MAX_SERVERS + 1)}}}
    out = inspector._config(json.dumps(config))
    assert out['servers_truncated'] and len(out['servers']) == inspector.MAX_SERVERS
    assert all(x['name'] == 'other' and x['type'] == 'other' and x['endpoint'] == 'other' for x in out['servers'])
    assert all(x['token_in_config']['TRADE_MCP_MCP_TOKEN'] == 'other' for x in out['servers'])
    assert 'private.example' not in json.dumps(out)
