from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from trade_mcp.engines.live import analyze_broker


def setup():
    mt5 = Mock()
    mt5.resolve_symbol.return_value = 'XAUUSD.a'
    mt5.candles.return_value = [dict(time=i*900, open=100+i, high=102+i,
                                    low=99+i, close=101+i, tick_volume=10)
                               for i in range(241)]
    return mt5, SimpleNamespace(default_symbol='XAUUSD', backend='native')


def test_broker_closed_bars_and_no_side_effect():
    mt5, cfg = setup()
    # Latest bar is still forming. Its extreme close must not enter indicators.
    mt5.candles.return_value[-1]['close'] = 999999
    result = analyze_broker(mt5, cfg, now=240*900+30)
    assert result['ok'] and result['technical']['bars'] == 240
    assert result['technical']['close'] == 340
    assert result['symbol'] == 'XAUUSD.a'
    assert result['latest_returned_bar_excluded']
    assert not result['execution_authorized']
    assert not result['freshness_validated_for_execution']
    assert [call[0] for call in mt5.mock_calls] == ['resolve_symbol', 'candles']


def test_synthetic_source_not_claimed_live():
    mt5, cfg = setup()
    cfg.backend = 'mock'
    assert analyze_broker(mt5, cfg, now=300000)['source'] == 'mock_synthetic'


def test_unsorted_broker_data_rejected():
    mt5, cfg = setup()
    mt5.candles.return_value.reverse()
    with pytest.raises(ValueError):
        analyze_broker(mt5, cfg)


def test_calendar_month_is_not_a_fixed_duration():
    mt5, cfg = setup()
    with pytest.raises(ValueError):
        analyze_broker(mt5, cfg, timeframe='MN1')
    assert not mt5.mock_calls


def test_empty_broker_data_is_not_hold_signal():
    mt5, cfg = setup()
    mt5.candles.return_value = []
    assert analyze_broker(mt5, cfg)['error'] == 'closed_candles_unavailable'
