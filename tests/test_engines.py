import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from trade_mcp.engines.risk import size_position
from trade_mcp.engines.strategy import strategy_signal
from trade_mcp.engines.technical import analyze_ohlcv, validate_ohlcv
from trade_mcp.text_tools import clean_tool


def bars(n=240):
    return [[i, 100+i, 102+i, 99+i, 101+i, 10] for i in range(n)]


def test_technical_warmup_and_ema():
    result = analyze_ohlcv(bars())
    assert result['ready']
    assert result['indicators']['ema20'] == pytest.approx(330.5)
    assert result['indicators']['ema200'] == pytest.approx(240.5)
    assert analyze_ohlcv(bars(10))['indicators']['ema200'] is None
    assert not analyze_ohlcv(bars(10))['ready']
    assert result['indicators']['macd'] == pytest.approx(7)
    assert result['indicators']['supertrend_direction'] == 1


@pytest.mark.parametrize('rows', [[], [[1, 1, 2, 0, 1, 3]],
    [[1, 1, 2, 1, 3, 0]], [[1, 1, 2, 1, 1, -1]],
    [[1, True, 2, 1, 1, 0]], bars(2)[::-1], [[1, 1, 2, 1, float('nan'), 0]]])
def test_invalid_ohlcv(rows):
    with pytest.raises(ValueError):
        validate_ohlcv(rows)


def test_no_lookahead_prefix():
    data = bars()
    before = analyze_ohlcv(data[:220])
    data[230][4] += 0.5
    assert analyze_ohlcv(data[:220]) == before


def test_signal_is_not_probability_or_authorization():
    assert strategy_signal(bars(30))['signal'] == 'HOLD'
    result = strategy_signal(bars())
    assert result['execution_authorized'] is False
    assert result['confidence_kind'] == 'uncalibrated_rule_agreement_not_probability'


def test_clean_string_hides_provider_error():
    @clean_tool
    def fail() -> str:
        raise RuntimeError('secret-key')
    assert 'secret-key' not in fail()
    assert json.loads(fail())['ok'] is False


def test_actual_mcp_tools_are_text():
    from trade_mcp.server import mcp
    result = asyncio.run(mcp.call_tool('technical_analysis_text', {'ohlcv': bars()}))
    # FastMCP versions may return (content, structured) or content only.
    content = result[0] if isinstance(result, tuple) else result
    assert content[0].type == 'text'
    assert json.loads(content[0].text)['ready'] is True


def test_demo_risk_cap():
    from trade_mcp.config import Config
    mt5 = Mock()
    mt5.account_info.return_value = dict(login=1, server='Demo', trade_mode='demo',
                                        currency='USD', equity=100000)
    mt5.resolve_symbol.return_value = 'XAUUSD'
    mt5.symbol_info.return_value = dict(trade_tick_size=1, trade_tick_value=1,
                                       volume_min=0.01, volume_step=0.01, volume_max=100)
    result = size_position(mt5, Config(demo_cash_policy=True), 'XAUUSD', 100, 90, 1)
    assert result['sizing']['actual_risk'] <= 10
    assert result['sizing']['risk_amount'] == 10


def test_ccxt_sandbox_is_first_and_no_order():
    from trade_mcp.engines.exchange import quote
    client = Mock()
    client.fetch_ticker.return_value = {'bid': 1, 'ask': 2}
    library = SimpleNamespace(exchanges=['example'], example=Mock(return_value=client))
    with patch('trade_mcp.engines.exchange.importlib.import_module', return_value=library):
        assert quote('example', 'A/B')['sandbox'] is True
    assert client.mock_calls[0][0] == 'set_sandbox_mode'
    assert not client.create_order.called


def test_text_only_main_has_only_string_tools():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location('text_main', Path(__file__).resolve().parents[1] / 'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.list_tools())
    assert len(tools) >= 12
    assert all(t.name.endswith('_text') or t.name in {
        'get_comprehensive_analysis', 'get_full_technical_summary'} for t in tools)
    assert 'mt5_status_text' in [t.name for t in tools]


def test_downward_supertrend_and_macd():
    rows = [[i, 400-i, 402-i, 399-i, 401-i, 10] for i in range(240)]
    result = analyze_ohlcv(rows)
    assert result['indicators']['supertrend_direction'] == -1
    assert result['indicators']['macd'] == pytest.approx(-7)


def test_ccxt_never_falls_back_when_sandbox_unsupported():
    from trade_mcp.engines.exchange import quote
    client = Mock()
    client.set_sandbox_mode.side_effect = RuntimeError('sandbox unavailable')
    library = SimpleNamespace(exchanges=['example'], example=Mock(return_value=client))
    with patch('trade_mcp.engines.exchange.importlib.import_module', return_value=library), pytest.raises(RuntimeError):
        quote('example', 'A/B')
    assert not client.fetch_ticker.called
