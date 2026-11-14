"""Read-only console bootstrap uses the already authenticated application only."""
from types import SimpleNamespace

import pytest

from trade_mcp.workspace import WorkspaceManager, console_environment, workspace_identity


def test_child_environment_preserves_limits_but_never_enables_trading(mock_app):
    cfg = mock_app.cfg.with_overrides(backend='native', allow_trading=True,
                                      mcp_token='private-terminal', opencode_url='http://localhost:49374',
                                      opencode_password='private-desktop')
    env = console_environment(cfg)
    assert env['TRADE_MCP_BACKEND'] == 'native'
    assert env['TRADE_MCP_ALLOW_TRADING'] == 'false'
    assert env['TRADE_MCP_TRAINING_BUDGET'] == str(cfg.training_budget)
    assert env['TRADE_MCP_OPENCODE_PASSWORD'] == 'private-desktop'
    assert env['TRADE_MCP_MCP_TOKEN'] == 'private-terminal'
    assert env['TRADE_MCP_JOURNAL'] == cfg.journal_path
    assert 'private' not in workspace_identity(cfg)
    assert workspace_identity(cfg) != workspace_identity(cfg.with_overrides(training_budget=7))


def test_starts_only_verified_demo_with_no_clipboard_or_model_calls(mock_app, monkeypatch):
    from trade_mcp import workspace
    cfg = mock_app.cfg.with_overrides(backend='native', mcp_token='private',
                                      opencode_url='http://localhost:49374')
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
    cfg = mock_app.cfg.with_overrides(backend='native', mcp_token='private', opencode_url='http://localhost:49374')
    app = SimpleNamespace(cfg=cfg, mt5=SimpleNamespace(
        status=lambda: {'ea_connected':True, 'backend':'native'}, account_info=lambda **kw:{'trade_mode':mode}))
    assert WorkspaceManager().start(app)['error'] == 'demo_account_required'


def test_mock_cannot_be_promoted_by_changing_config_alone(mock_app):
    mock_app.cfg = mock_app.cfg.with_overrides(backend='native', mcp_token='private', opencode_url='http://localhost:49374')
    assert WorkspaceManager().start(mock_app)['error'] == 'terminal_not_connected'


def test_no_unrelated_console_is_stopped(mock_app, monkeypatch):
    manager = WorkspaceManager()
    assert manager.stop()['error'] == 'no_owned_console'
    cfg = mock_app.cfg.with_overrides(backend='native', mcp_token='private', opencode_url='http://localhost:49374')
    app = SimpleNamespace(cfg=cfg, mt5=SimpleNamespace(
        status=lambda:{'backend':'native','ea_connected':True}, account_info=lambda **kw:{'trade_mode':'demo'}))
    monkeypatch.setattr(manager, 'status', lambda _: {'ok':False, 'state':'port_occupied_or_mismatched'})
    assert manager.start(app)['state'] == 'port_occupied_or_mismatched'
    assert manager.process is None
