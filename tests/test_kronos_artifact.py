import json

from trade_mcp.engines.upstream import KRONOS_COMMIT
from trade_mcp.kronos_artifact import load_forecast


def artifact():
    return {'symbol': 'XAUUSD', 'timeframe': 'M1', 'forecast_origin': 6000,
        'source': 'official_KronosPredictor.predict', 'upstream_commit_expected': KRONOS_COMMIT,
        'candles': [[6000+i*60, 100, 102, 99, 101, 0] for i in range(60)],
        'holdout_evaluation': {'metrics': [{'minutes_ahead': h,
            'absolute_error': 1, 'no_change_absolute_error': 2} for h in (15, 30, 60)]}}


def test_accept_fresh_instrument_matched_artifact(tmp_path):
    path = tmp_path/'forecast.json'
    path.write_text(json.dumps(artifact()))
    result = load_forecast(path, 'XAUUSD', 'M1', now=6030)
    assert result['available']
    assert len(result['candles']) == 60
    assert not result['execution_eligible']
    assert not load_forecast(path, 'EURUSD', 'M1', now=6030)['available']
    assert not load_forecast(path, 'XAUUSD', 'M1', now=6300)['available']
    assert not load_forecast(path, 'XAUUSD', 'M15', now=6030)['available']


def test_failed_baseline_and_invalid_ohlc_rejected(tmp_path):
    path = tmp_path/'forecast.json'
    data = artifact()
    data['holdout_evaluation']['metrics'][0]['absolute_error'] = 3
    path.write_text(json.dumps(data))
    assert not load_forecast(path, 'XAUUSD', 'M1', now=6030)['available']
    data = artifact()
    data['candles'][0][2] = 1
    path.write_text(json.dumps(data))
    assert not load_forecast(path, 'XAUUSD', 'M1', now=6030)['available']
