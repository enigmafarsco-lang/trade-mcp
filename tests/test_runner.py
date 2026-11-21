import json
import time
from unittest.mock import Mock, patch

from trade_mcp.paper import PaperLedger
from trade_mcp.runner import paper_cycle


def fake_app(mock_app):
    app = Mock()
    app.cfg = mock_app.cfg
    app.mt5.symbol_info.return_value = mock_app.mt5.symbol_info('XAUUSD')
    app.mt5.tick.return_value = dict(time=time.time(), bid=2650, ask=2650.25)
    app.mt5.resolve_symbol.return_value = 'XAUUSD'
    return app


def test_one_model_decision_one_paper_order_no_broker_side_effect(mock_app, tmp_path):
    app = fake_app(mock_app)
    llm = Mock()
    llm.chat_json.return_value = dict(action='buy', confidence=.9, stop_loss=2649.75, take_profit=2651.25)
    ledger = PaperLedger(tmp_path/'paper.json')
    evidence = dict(ok=True, symbol='XAUUSD', technical={'ready': True})
    with patch('trade_mcp.runner.analyze_broker', return_value=evidence):
        result = paper_cycle(app, llm, ledger, 'XAUUSD')
    assert result['ok'] and result['paper']
    assert llm.chat_json.call_count == 1
    assert ledger.status()['open_position'] is True
    assert all(call[0] in ('tick', 'symbol_info') for call in app.mt5.mock_calls)


def test_model_hold_is_not_an_order(mock_app, tmp_path):
    app = fake_app(mock_app)
    llm = Mock()
    llm.chat_json.return_value = {'action': 'wait', 'confidence': .5}
    ledger = PaperLedger(tmp_path/'paper.json')
    with patch('trade_mcp.runner.analyze_broker', return_value=dict(
            ok=True, symbol='XAUUSD', technical={'ready': True})):
        assert paper_cycle(app, llm, ledger, 'XAUUSD')['action'] == 'HOLD'
    assert not ledger.status()['open_position']


def test_stale_quote_does_not_spend_model_request(mock_app, tmp_path):
    app = fake_app(mock_app)
    app.mt5.tick.return_value['time'] -= 60
    llm = Mock()
    with patch('trade_mcp.runner.analyze_broker', return_value=dict(
            ok=True, symbol='XAUUSD', technical={'ready': True})):
        result = paper_cycle(app, llm, PaperLedger(tmp_path/'paper.json'), 'XAUUSD')
    assert result['error'] == 'stale_paper_quote'
    llm.chat_json.assert_not_called()


def test_comprehensive_tool_clean_text_and_all_indicators(mock_app, monkeypatch):
    import asyncio

    from trade_mcp.server import mcp
    monkeypatch.setattr('trade_mcp.app.get_app', lambda: mock_app)
    output = asyncio.run(mcp.call_tool('get_comprehensive_analysis', {'symbol': 'XAUUSD', 'timeframe': 'M15'}))
    content = output[0] if isinstance(output, tuple) else output
    data = json.loads(content[0].text)
    i = data['technical']['indicators']
    assert all(k in i for k in ('rsi14', 'macd', 'ema20', 'ema50', 'ema200',
                               'atr14', 'supertrend', 'bollinger_upper', 'bollinger_lower'))


def test_runner_does_not_activate_an_exhausted_budget(monkeypatch, capsys):
    from trade_mcp.runner import main
    app = Mock()
    app.store.model_budget_status.return_value = {'allowed': False, 'state': 'exhausted'}
    llm = Mock(configured=True)
    monkeypatch.setenv('TRADE_MCP_PAPER_TRADING', 'true')
    monkeypatch.setattr('sys.argv', ['agent.py', '--consent-model-requests'])
    with patch('trade_mcp.app.build_app', return_value=app), patch(
            'trade_mcp.agents.LLMClient.from_config', return_value=llm):
        assert main() == 2
    assert 'active_model_budget_required' in capsys.readouterr().out
    app.store.activate_model_budget.assert_not_called()
    llm.chat_json.assert_not_called()
    app.close.assert_called_once()
