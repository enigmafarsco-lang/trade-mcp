"""Read-only console bootstrap uses the already authenticated application only."""
import json
from types import SimpleNamespace

import pytest

from trade_mcp.workspace import WorkspaceManager, console_environment, workspace_identity


def test_child_environment_preserves_limits_but_never_enables_trading(mock_app):
    cfg = mock_app.cfg.with_overrides(backend='native', allow_trading=True,
                                      mcp_token='private-terminal',
                                      llm_base_url='https://provider.example/v1',
                                      llm_model='web-model', llm_api_key='private-web-key',
                                      llm_billing_project='web-project',
                                      llm_usage_convention='openai_inclusive',
                                      opencode_url='http://localhost:49374',
                                      opencode_password='private-desktop')
    env = console_environment(cfg)
    assert env['TRADE_MCP_BACKEND'] == 'native'
    assert env['TRADE_MCP_ALLOW_TRADING'] == 'false'
    assert env['TRADE_MCP_TRAINING_BUDGET'] == str(cfg.training_budget)
    assert env['TRADE_MCP_LLM_API_KEY'] == 'private-web-key'
    assert env['TRADE_MCP_LLM_BASE_URL'] == 'https://provider.example/v1'
    assert env['TRADE_MCP_LLM_MODEL'] == 'web-model'
    assert env['TRADE_MCP_LLM_BILLING_PROJECT'] == 'web-project'
    assert env['TRADE_MCP_LLM_USAGE_CONVENTION'] == 'openai_inclusive'
    assert 'TRADE_MCP_OPENCODE_PASSWORD' not in env
    assert 'OPENCODE_PASSWORD' not in env
    assert 'TRADE_MCP_OPENCODE_URL' not in env
    assert env['TRADE_MCP_MCP_TOKEN'] == 'private-terminal'
    assert env['TRADE_MCP_JOURNAL'] == cfg.journal_path
    assert 'private' not in workspace_identity(cfg)
    assert workspace_identity(cfg) != workspace_identity(cfg.with_overrides(training_budget=7))


def test_identity_ignores_runtime_model_changes(mock_app):
    base = mock_app.cfg.with_overrides(llm_base_url='https://provider.example/v1',
                                       llm_model='web-model')
    changed = base.with_overrides(llm_model='another-web-model')
    assert workspace_identity(base) == workspace_identity(changed)
    assert workspace_identity(base) != workspace_identity(base.with_overrides(max_lot=base.max_lot + 1))
    assert workspace_identity(base) != workspace_identity(
        base.with_overrides(llm_base_url='https://other.example/v1'))


def test_manager_recognizes_console_after_web_only_model_change(mock_app, monkeypatch):
    from trade_mcp import workspace
    cfg = mock_app.cfg.with_overrides(backend='native', mcp_token='private',
                                      llm_base_url='https://provider.example/v1',
                                      llm_model='web-model')
    running = cfg.with_overrides(llm_model='another-web-model')
    payload = {
        'backend': 'native', 'trading_enabled': False,
        'workspace_identity': workspace_identity(running),
        'model_backend': 'openai-compatible', 'model_source': 'web',
        'model_configured': True, 'model': running.llm_model,
        'model_test': {'state': 'not_tested'}, 'agents': [],
    }
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps(payload).encode()
    class Opener:
        def open(self, url, timeout=3):
            return Response()
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    monkeypatch.setattr(workspace.socket, 'create_connection', lambda *a, **kw: Connection())
    monkeypatch.setattr(workspace.urllib.request, 'build_opener', lambda *a, **kw: Opener())
    result = workspace.WorkspaceManager().status(cfg)
    assert result['ok'] is True
    assert result['state'] == 'ready'


def test_starts_only_verified_demo_with_no_clipboard_or_model_calls(mock_app, monkeypatch):
    from trade_mcp import workspace
    cfg = mock_app.cfg.with_overrides(backend='native', mcp_token='private',
                                      llm_base_url='https://provider.example/v1',
                                      llm_model='web-model')
    app = SimpleNamespace(cfg=cfg, mt5=SimpleNamespace(
        status=lambda: {'ea_connected':True, 'backend':'native'},
        account_info=lambda **kw: {'trade_mode':'demo'}))
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, 'status', lambda _: {'state':'stopped', 'url':'http://127.0.0.1:8790'})
    commands = []
    def spawn(command, **kw):
        commands.append(command)
        assert kw['env']['TRADE_MCP_ALLOW_TRADING'] == 'false'
        assert kw['stdin'] == workspace.subprocess.DEVNULL
        assert kw['stdout'] == workspace.subprocess.DEVNULL
        return SimpleNamespace(poll=lambda:None)
    monkeypatch.setattr(workspace.subprocess, 'Popen', spawn)
    result = manager.start(app)
    assert result['state'] == 'starting'
    assert commands[0][-1] == '--no-browser'
    assert 'private' not in str(result)


@pytest.mark.parametrize('mode', ['real', 'unknown', None])
def test_non_demo_cannot_start_workspace(mock_app, mode):
    cfg = mock_app.cfg.with_overrides(backend='native', mcp_token='private',
                                      llm_base_url='https://provider.example/v1',
                                      llm_model='web-model')
    app = SimpleNamespace(cfg=cfg, mt5=SimpleNamespace(
        status=lambda: {'ea_connected':True, 'backend':'native'}, account_info=lambda **kw:{'trade_mode':mode}))
    assert WorkspaceManager().start(app)['error'] == 'demo_account_required'


def test_mock_cannot_be_promoted_by_changing_config_alone(mock_app):
    mock_app.cfg = mock_app.cfg.with_overrides(
        backend='native', mcp_token='private',
        llm_base_url='https://provider.example/v1', llm_model='web-model')
    assert WorkspaceManager().start(mock_app)['error'] == 'terminal_not_connected'


def test_no_unrelated_console_is_stopped(mock_app, monkeypatch):
    manager = WorkspaceManager()
    assert manager.stop()['error'] == 'no_owned_console'
    cfg = mock_app.cfg.with_overrides(
        backend='native', mcp_token='private',
        llm_base_url='https://provider.example/v1', llm_model='web-model')
    app = SimpleNamespace(cfg=cfg, mt5=SimpleNamespace(
        status=lambda:{'backend':'native','ea_connected':True}, account_info=lambda **kw:{'trade_mode':'demo'}))
    monkeypatch.setattr(manager, 'status', lambda _: {'ok':False, 'state':'port_occupied_or_mismatched'})
    assert manager.start(app)['state'] == 'port_occupied_or_mismatched'
    assert manager.process is None
