import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from trade_mcp.console import Handler


def handler(symbols):
    store = MagicMock()
    store.get_agent.return_value = {'id': 'test', 'symbols': symbols, 'mode': 'auto'}
    state = SimpleNamespace(store=store, cfg=object(), app=SimpleNamespace(mt5=object()),
                            lock=threading.Lock(), agent_engine=MagicMock())
    return SimpleNamespace(state=state)


def test_explicit_rules_guarded_has_no_provider_and_preserves_saved_agent():
    h = handler(['XAUUSD'])
    with patch('trade_mcp.console.AgentEngine') as cls:
        cls.return_value.run.return_value = {'source': 'rules'}
        result = Handler._agent_action(h, 'test', {'action': 'run_rules_guarded'})
    assert result['ok']
    client = cls.call_args.kwargs['llm']
    assert not client.configured
    assert client.mode != 'web'
    assert cls.return_value.run.call_args.args[0]['mode'] == 'guarded'
    assert h.state.store.get_agent.return_value['mode'] == 'auto'
    h.state.agent_engine.assert_not_called()
    h.state.store.update_agent.assert_not_called()


def test_multiple_symbols_refused():
    h = handler(['XAUUSD', 'EURUSD'])
    with patch('trade_mcp.console.AgentEngine') as cls:
        result = Handler._agent_action(h, 'test', {'action': 'run_rules_guarded'})
    assert result['error'] == 'rules_requires_one_symbol'
    cls.assert_not_called()


def test_normal_run_remains_web():
    h = handler(['XAUUSD'])
    Handler._agent_action(h, 'test', {'action': 'run'})
    h.state.agent_engine.assert_called_once_with('agent_run')


def test_window_refuses_disabled_execution():
    h = handler(['XAUUSD'])
    h.state.rules_window_lock = threading.Lock()
    h.state.rules_window_stop = threading.Event()
    h.state.cfg = SimpleNamespace(trading_enabled=False, demo_cash_policy=False)
    with patch('trade_mcp.bounded.run_window', side_effect=lambda step, *a, **kw: step()):
        import pytest
        with pytest.raises(ValueError, match='demo_execution_not_enabled'):
            Handler._agent_action(h, 'test', {'action': 'run_rules_window'})
    assert not h.state.rules_window_lock.locked()


def test_window_rejects_parallel_window():
    h = handler(['XAUUSD'])
    h.state.rules_window_lock = threading.Lock()
    h.state.rules_window_lock.acquire()
    result = Handler._agent_action(h, 'test', {'action': 'run_rules_window'})
    assert result['error'] == 'rules_window_already_running'


def test_stop_window_leaves_guardian_untouched():
    h = handler(['XAUUSD'])
    h.state.rules_window_stop = threading.Event()
    assert Handler._agent_action(h, 'test', {'action': 'stop_rules_window'})['ok']
    assert h.state.rules_window_stop.is_set()
