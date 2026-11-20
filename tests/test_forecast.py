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


def test_directional_forecast_and_walk_forward():
    import math

    from trade_mcp.forecast import trend_forecast
    rows = [dict(time=i*900, open=100*math.exp(i*.001),
                 high=101*math.exp(i*.001), low=99*math.exp(i*.001),
                 close=100*math.exp(i*.001)) for i in range(100)]
    result = trend_forecast(rows, now=90000)
    assert result['available']
    assert not result['execution_eligible']
    assert [p['minutes_ahead'] for p in result['points']] == [15, 30, 60]
    assert all(p['direction'] == 'up' for p in result['points'])
    assert all(p['mae'] < p['no_change_mae'] for p in result['points'])
    assert all(p['evaluation_samples'] == 20 for p in result['points'])
    assert result['points'][0]['price'] < result['points'][-1]['price']
    rows[-1].update(close=999999, high=999999)
    forming = trend_forecast(rows, now=89999)
    assert forming['anchor_price'] == rows[-2]['close']


def test_trend_missing_stale_gap():
    from trade_mcp.forecast import trend_forecast
    assert not trend_forecast(bars(), now=36000)['available']
    assert not trend_forecast(bars(), 'H1', now=36000)['available']
    rows = [dict(time=i*900, open=100, high=101, low=99, close=100) for i in range(100)]
    assert trend_forecast(rows, now=100000)['reason'] == 'stale_closed_bars'
    rows.pop(-5)
    assert trend_forecast(rows, now=90000)['reason'] == 'non_contiguous_history'


def test_no_skill_forecast_not_drawable():
    from trade_mcp.forecast import trend_forecast
    rows = [dict(time=i*900, open=100, high=101, low=99, close=100)
            for i in range(100)]
    result = trend_forecast(rows, now=90000)
    assert not result['available']
    assert result['reason'] == 'failed_historical_baseline_check'
    assert 'points' not in result
    assert len(result['diagnostics']) == 3
    assert not result['execution_eligible']
