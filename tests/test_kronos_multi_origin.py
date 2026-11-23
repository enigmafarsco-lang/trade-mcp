import math

from trade_mcp.engines import upstream


def make_rows(n, start=1_700_000_000, start_price=2000.0, step=0.1):
    rows = []
    price = start_price
    for i in range(n):
        o = price
        c = price + step
        h = max(o, c) + 0.5
        lo = min(o, c) - 0.5
        rows.append([start + i * 60, o, h, lo, c, 10.0])
        price = c
    return rows


def fake_predictor_factory(pred_close_offset=0.0, ohlc_adjustments=None, volume_adjustments=None):
    class Pred:
        def predict(self, **kwargs):
            raise AssertionError('raw predict should not be called in this fake kronos_predict patch')
    return Pred()


def fake_kronos_predict(training_rows, predictor):
    last = training_rows[-1]
    origin = last[0] + 60
    candles = []
    for i in range(60):
        o = last[4] + i * 0.1
        c = o + 0.05
        candles.append([origin + i * 60, o, o + 0.2, o - 0.2, c, 1.0])
    post = {
        'volume_policy': 'negative_to_zero_with_raw_audit',
        'ohlc_policy': 'envelope_preserve_open_close_with_raw_audit',
        'prices_modified': False,
        'ohlc_adjustments': [],
        'volume_adjustments': [],
    }
    # deterministic correction metadata for aggregation tests
    if len(training_rows) % 120 == 0:
        post['ohlc_adjustments'] = [{'minute': 1, 'raw': {'open': 1, 'high': 1, 'low': 1, 'close': 1},
                                      'used': {'open': 1, 'high': 1.5, 'low': 1, 'close': 1}}]
        post['volume_adjustments'] = [{'minute': 2, 'raw_volume': -0.2, 'used_volume': 0.0}]
        post['prices_modified'] = True
    return {
        'candles': candles,
        'source': 'official_KronosPredictor.predict',
        'upstream_commit_expected': upstream.KRONOS_COMMIT,
        'execution_authorized': False,
        'postprocessing': post,
        'validated_for_trading': False,
    }


def test_fixed_multi_origin_splits_are_deterministic_and_no_leakage():
    rows = make_rows(600)
    splits = upstream.fixed_multi_origin_splits(rows, origins=4, horizon=60, min_context=120, step=60, final_test_origins=1)
    assert len(splits) == 4
    assert [s['split'] for s in splits] == ['evaluation', 'evaluation', 'evaluation', 'final_test']
    idxs = [s['origin_index'] for s in splits]
    assert idxs == sorted(idxs)
    assert all(idxs[i] - idxs[i - 1] >= 60 for i in range(1, len(idxs)))
    for s in splits:
        assert s['origin_time'] == rows[s['origin_index']][0]
        assert s['origin_index'] >= 119
        assert s['origin_index'] + 60 < len(rows)
    assert splits == upstream.fixed_multi_origin_splits(rows, origins=4, horizon=60, min_context=120, step=60, final_test_origins=1)


def test_simple_trend_baseline_uses_only_training_rows():
    rows = make_rows(200, start_price=100.0, step=1.0)
    pred = upstream.simple_trend_close_baseline(rows[:120], 15, lookback=15)
    # linear rising closes -> positive slope projection
    assert pred > rows[119][4]
    pred2 = upstream.simple_trend_close_baseline(rows[:120], 15, lookback=15)
    assert pred == pred2


def test_multi_origin_evaluation_reports_baselines_and_postprocessing(monkeypatch):
    rows = make_rows(600)
    monkeypatch.setattr(upstream, 'kronos_predict', fake_kronos_predict)
    report = upstream.evaluate_kronos_multi_origin(rows, predictor=object(), origins=4, horizons=(15, 30, 60),
                                                   lookback=15, final_test_origins=1, step=60)
    assert report['schema'] == 'kronos-multi-origin-evaluation/v1'
    assert report['final_test_used_for_tuning'] is False
    assert report['origins'] == 4
    assert report['final_test_origins'] == 1
    assert set(report['split_summary'].keys()) == {'evaluation', 'final_test'}
    assert len(report['overall_summary']) == 3
    for row in report['overall_summary']:
        assert row['minutes_ahead'] in (15, 30, 60)
        for k in ['kronos_mae', 'no_change_mae', 'simple_trend_mae']:
            assert isinstance(row[k], float) and math.isfinite(row[k])
    # postprocessing frequency/size
    ps = report['postprocessing_summary']
    assert ps['ohlc_corrections_total'] >= 1
    assert ps['volume_corrections_total'] >= 1
    assert ps['origins_with_ohlc_corrections'] >= 1
    assert ps['origins_with_volume_corrections'] >= 1
    assert ps['ohlc_correction_size_mean_of_origin_means'] >= 0
    detail = report['origins_detail']
    assert len(detail) == 4
    assert detail[-1]['split'] == 'final_test'
    for origin in detail:
        assert len(origin['metrics']) == 3
        for m in origin['metrics']:
            assert 'absolute_error' in m
            assert 'no_change_absolute_error' in m
            assert 'simple_trend_absolute_error' in m
            assert 'ratio_vs_no_change' in m
            assert 'ratio_vs_simple_trend' in m


def test_multi_origin_rejects_tuning_on_final_test_by_structure():
    rows = make_rows(600)
    # final test must be < origins and reported separately
    splits = upstream.fixed_multi_origin_splits(rows, origins=3, horizon=60, min_context=120, step=60, final_test_origins=1)
    assert splits[-1]['split'] == 'final_test'
    assert all(s['split'] == 'evaluation' for s in splits[:-1])