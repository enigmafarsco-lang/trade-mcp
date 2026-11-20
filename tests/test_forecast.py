import pytest

from trade_mcp.forecast import hourly_scenarios


def bars():
    return [dict(time=i*900, open=100, high=102, low=98, close=100+i*.01)
            for i in range(40)]


def test_bounded_non_execution_scenarios():
    result = hourly_scenarios(bars(), now=36000)
    assert result['available']
    assert not result['execution_eligible']
    assert not result['calibrated']
    assert [p['minutes_ahead'] for p in result['points']] == [15, 30, 45, 60]
    assert all(p['lower'] <= p['center'] <= p['upper'] for p in result['points'])


def test_forming_candle_excluded():
    rows = bars()
    rows[-1].update(high=9999, close=9999)
    result = hourly_scenarios(rows, now=35500)
    assert result['points'][0]['center'] == rows[-2]['close']


def test_stale_and_short_and_unsupported():
    assert hourly_scenarios(bars(), now=40000)['reason'] == 'stale_closed_bars'
    assert not hourly_scenarios(bars()[:10], now=9000)['available']
    assert not hourly_scenarios(bars(), 'D1')['available']


def test_invalid_prices_rejected():
    rows = bars()
    rows[-1]['close'] = float('nan')
    with pytest.raises(ValueError):
        hourly_scenarios(rows, now=36000)
