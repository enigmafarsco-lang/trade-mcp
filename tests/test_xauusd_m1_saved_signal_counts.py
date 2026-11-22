"""Read-only signal extraction against synthetic reports, never real tick archives."""
import copy
import importlib.util
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest


def import_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def extractor():
    path = Path(__file__).resolve().parents[1] / 'scripts/xauusd_m1_saved_signal_counts.py'
    return import_file('m1_saved_signal_counts', path)


@pytest.fixture
def synthetic(extractor, tmp_path, monkeypatch):
    """Create synthetic old-job output with the production schema, then never replay it."""
    root = Path(__file__).resolve().parents[1]
    old_job = import_file('m1_previous_synthetic', root / 'scripts/xauusd_m1_previous_direction.py')
    fixtures = import_file('m1_previous_fixture_helpers', root / 'tests/test_xauusd_m1_previous_direction.py')
    archive = tmp_path / 'five'
    archive.mkdir()
    metadata = tmp_path / 'economics.json'
    metadata.write_text(json.dumps(fixtures.info()))
    old_raw = tmp_path / 'old.json'
    old_rows = fixtures.rows_for(old_job.OLD_WINDOW[1])
    old_raw.write_text(json.dumps(fixtures.response(old_rows)))
    comparisons = [fixtures.reference_comparison(old_job.OLD_WINDOW[0], m, old_rows) for m in old_job.MODELS]
    for name, start, _end in old_job.WINDOWS:
        rows = fixtures.rows_for(start)
        (archive / (name + '-raw-response.json')).write_text(json.dumps(fixtures.response(rows)))
        comparisons.extend(fixtures.reference_comparison(name, model, rows) for model in old_job.MODELS)
    boundary = tmp_path / 'boundary.json'
    boundary.write_text(json.dumps({
        'schema': 'xauusd-research-check-report/v1', 'report_id': old_job.BOUNDARY_REPORT_ID,
        'raw_data_committed': False, 'net_pnl': None,
        'result': {'schema': 'xauusd-final-boundary-replay/v1', 'status': 'completed_all_comparisons',
                   'history_calls_made': 0, 'planned_comparisons': 12, 'completed_comparisons': 12,
                   'failures': 0, 'all_legacies_reproduced': True, 'comparisons': comparisons},
    }))

    def forbidden(*args, **kwargs):
        pytest.fail('Synthetic aggregate extraction must not connect to a network')

    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    summary = old_job.run_experiment(archive=archive, old_raw=old_raw, economics_file=metadata,
                                     boundary_reference=boundary)
    assert summary['completed_paired_comparisons'] == 12
    synthetic_counts = {}
    for window in summary['windows']:
        original = window['pairs'][0]['previous_available_only']['signal_counts']
        synthetic_counts[window['id']] = (window['returned_rows'], original['buy'], original['sell'])
    monkeypatch.setattr(extractor, 'PUBLISHED_WINDOW_COUNTS', synthetic_counts)

    def published_side(side):
        return {'entries': side['simulated_entries'], 'profit': side['cash_profit_exits'],
                'loss': side['cash_loss_exits'], 'gross_total_mark': side['gross_total_mark_usd_model'],
                'open_at_end': side['position_open_at_sample_end'],
                'latch': side['simulated_three_consecutive_loss_latch']}

    recorded = {key: copy.deepcopy(value) for key, value in summary.items() if key != 'windows'}
    recorded['windows'] = []
    for window in summary['windows']:
        pairs = []
        for pair in window['pairs']:
            pairs.append({'model': pair['model_id'], 'baseline_matched': pair['baseline_aggregate_matched'],
                          'aligned_minus_marked': pair['aligned_minus_matched_gross_total_mark_usd_model'],
                          'pav': published_side(pair['previous_available_only']),
                          'pda': published_side(pair['previous_direction_aligned'])})
        recorded['windows'].append({'id': window['id'], 'status': window['status'], 'pairs': pairs})
    published = {'schema': 'xauusd-research-check-report/v1',
                 'report_id': '20261122T130500Z-xauusd-m1-previous-direction',
                 'head_at_execution': '8f684993ffac34bb4c8a36ca0aebda35677ef531',
                 'execution_count': 1, 'raw_data_committed': False, 'result': recorded}
    # Arbitrary unexpected fields must never be copied to the public output.
    summary['extra_private_field'] = {'bid': 123456, 'secret': 'PRIVATE_RAW_VALUE', 'time_ms': 'PRIVATE_TIME'}
    summary_path, published_path = tmp_path / 'summary.json', tmp_path / 'published.json'
    summary_path.write_text(json.dumps(summary, allow_nan=False))
    published_path.write_text(json.dumps(published, allow_nan=False))
    return SimpleNamespace(summary=summary, published=published, summary_path=summary_path,
                           published_path=published_path)


def test_published_fixed_counts_match_prior_sanitized_reports(extractor):
    # This reads only tracked aggregate reports, not the workstation or ticks.
    reports = extractor.ROOT / 'reports/research'
    extra = json.loads((reports / '20261122T073857Z-xauusd-signal-replay.json').read_text())['result']
    windows = json.loads((reports / '20261122T110500Z-xauusd-sparse-screen-v2-5win.json').read_text())['windows']
    expected = {'additional_sample': (extra['input_rows'], extra['signal_counts'].get('buy', 0),
                                      extra['signal_counts'].get('sell', 0))}
    expected.update({window['id']: (window['returned_rows'], window['signal_counts'].get('buy', 0),
                                    window['signal_counts'].get('sell', 0)) for window in windows})
    assert expected == extractor.PUBLISHED_WINDOW_COUNTS
    assert sum(item[0] for item in expected.values()) == 4827
    assert sum(item[1] for item in expected.values()) == 107
    assert sum(item[2] for item in expected.values()) == 66


def test_decision_review_totals_match_published_aggregate(extractor):
    reference = json.loads(extractor.REFERENCE.read_text())['result']
    pairs = [pair for window in reference['windows'] for pair in window['pairs']]
    review = extractor.REFERENCE.with_suffix('.md').read_text()
    models = (('original_3_changes', 'Three changes, no spread cap'),
              ('2_changes_spread_cap_1_00', 'Two changes, $1 spread cap'))
    for model_id, label in models:
        for mode, display in (('pav', 'Pav'), ('pda', 'Pda')):
            sides = [pair[mode] for pair in pairs if pair['model'] == model_id]
            counts = [sum(side[key] for side in sides) for key in ('entries', 'profit', 'loss', 'open_at_end')]
            assert counts[0] == sum(counts[1:])
            row = f'| {label} | {display} | ' + ' | '.join(map(str, counts)) + ' |'
            assert row in review
    filtered = [pair['pda']['gross_total_mark'] for pair in pairs]
    assert sum(value < -1e-8 for value in filtered) == 11
    assert sum(value > 1e-8 for value in filtered) == 1


def test_published_bounds_are_labeled_and_follow_existing_reports(extractor):
    reports = extractor.ROOT / 'reports/research'
    m1 = json.loads(extractor.REFERENCE.read_text())['result']
    extra = json.loads((reports / '20261122T073857Z-xauusd-signal-replay.json').read_text())['result']
    prior_windows = json.loads((reports / '20261122T110500Z-xauusd-sparse-screen-v2-5win.json').read_text())['windows']
    prior = {'additional_sample': extra, **{window['id']: window for window in prior_windows}}
    review = extractor.REFERENCE.with_suffix('.md').read_text()
    assert 'lower bounds, not recovered raw signal counts' in review
    assert 'If the M1 experiment used those same saved quote rows' in review
    for window in m1['windows']:
        model = {pair['model']: pair for pair in window['pairs']}
        three = model['original_3_changes']
        two = model['2_changes_spread_cap_1_00']
        assert prior[window['id']]['interior_minutes_seen'] == 3
        raw_total = sum(prior[window['id']]['signal_counts'].values())
        assert 0 < three['pda']['entries'] <= raw_total
        assert two['pda']['entries'] > 0
        assert three['pav']['entries'] <= raw_total
        row = (f"| {window['id']} | ≥{three['pav']['entries']} | ≥{three['pda']['entries']} | "
               f"≥{two['pav']['entries']} | ≥{two['pda']['entries']} |")
        assert row in review


def test_extracts_per_window_and_model_counts_without_leaking_inputs(extractor, synthetic):
    before_summary = synthetic.summary_path.read_bytes()
    before_reference = synthetic.published_path.read_bytes()
    result = extractor.extract(extractor.load_json(synthetic.summary_path),
                               extractor.load_json(synthetic.published_path))
    assert result['status'] == 'completed_aggregate_extraction_no_replay'
    assert result['matched_scenario_pairs'] == 12 and len(result['windows']) == 6
    assert result['tick_replays'] == result['network_calls_made'] == result['orders_sent'] == 0
    assert result['commission'] is result['fees'] is result['swap'] is result['net_pnl'] is None
    assert result['blind_holdout_or_out_of_sample'] is False and result['execution_authorized'] is False
    for saved, published, window in zip(synthetic.summary['windows'], synthetic.published['result']['windows'],
                                        result['windows'], strict=True):
        assert window['id'] == saved['id'] == published['id']
        assert window['returned_rows'] == saved['returned_rows'] == 300
        assert window['eligible_current_minute_buckets'] == 2
        assert window['previous_observed_bid_directions'] == {'up': 1, 'down': 1, 'flat': 0}
        for model, pair in zip(window['models'], saved['pairs'], strict=True):
            assert model['model_id'] == pair['model_id']
            raw = pair['previous_available_only']['raw_signals_before_all_entry_filters']
            assert model['raw_signals_before_all_entry_filters']['previous_available'] == raw['previous_available']
            assert model['raw_signals_before_all_entry_filters']['previous_available_and_direction_aligned'] == 0
            assert model['signals_blocked_at_entry_decision']['previous_direction_aligned_mode'] > 0
            assert model['simulated_entries']['previous_direction_aligned'] == 0
    output = json.dumps(result, allow_nan=False)
    assert all(x not in output for x in ('PRIVATE_RAW_VALUE', 'PRIVATE_TIME', '"bid"', '"ask"', '"time_ms"',
                                         'trade_contract_size', 'gross_total_mark'))
    assert synthetic.summary_path.read_bytes() == before_summary
    assert synthetic.published_path.read_bytes() == before_reference


@pytest.mark.parametrize('mutation', [
    'wrong_delta', 'wrong_gross', 'wrong_entry', 'wrong_exit', 'wrong_latch', 'wrong_open',
    'unknown_mode', 'unknown_model', 'wrong_spread_cap', 'duplicate_window', 'duplicate_pair',
    'wrong_schema', 'wrong_status', 'wrong_reference_status', 'missing_side', 'missing_pair',
    'claims_utc_verified', 'claims_selection_was_not_posthoc', 'claims_baseline_was_hash',
    'wrong_reference_revision', 'nonzero_history', 'unknown_cost',
    'missing_net', 'boolean_count', 'nonfinite', 'unequal_context', 'cross_model_context_mismatch',
    'inconsistent_signal_total', 'too_many_aligned', 'invalid_direction_block', 'raw_signals_exceed_rows',
    'historical_rows_mismatch', 'historical_raw_signal_mismatch',
])
def test_inconsistent_or_untrusted_reports_are_rejected(extractor, synthetic, mutation):
    summary, reference = copy.deepcopy(synthetic.summary), copy.deepcopy(synthetic.published)
    window = summary['windows'][0]
    pair = window['pairs'][0]
    pav, pda = pair['previous_available_only'], pair['previous_direction_aligned']
    raw = pav['raw_signals_before_all_entry_filters']
    if mutation == 'wrong_delta':
        pair['aligned_minus_matched_gross_total_mark_usd_model'] += 1
    elif mutation == 'wrong_gross':
        reference['result']['windows'][0]['pairs'][0]['pav']['gross_total_mark'] += 1
    elif mutation == 'wrong_entry':
        pav['simulated_entries'] += 1
    elif mutation == 'wrong_exit':
        pda['cash_profit_exits'] += 1
    elif mutation == 'wrong_latch':
        pda['simulated_three_consecutive_loss_latch'] = not pda['simulated_three_consecutive_loss_latch']
    elif mutation == 'wrong_open':
        pav['position_open_at_sample_end'] = not pav['position_open_at_sample_end']
    elif mutation == 'unknown_mode':
        pav['m1_entry_context'] = 'none'
    elif mutation == 'unknown_model':
        pair['model_id'] = 'unknown'
    elif mutation == 'wrong_spread_cap':
        window['pairs'][1]['previous_available_only']['max_entry_spread_cost_usd_model'] = .5
    elif mutation == 'duplicate_window':
        summary['windows'][1]['id'] = summary['windows'][0]['id']
    elif mutation == 'duplicate_pair':
        window['pairs'][1]['model_id'] = window['pairs'][0]['model_id']
    elif mutation == 'wrong_schema':
        summary['schema'] = 'unknown'
    elif mutation == 'wrong_status':
        summary['status'] = 'completed_partial_paired_comparisons'
    elif mutation == 'wrong_reference_status':
        reference['result']['windows'][0]['status'] = 'some_pairs_not_compared'
    elif mutation == 'missing_side':
        del pair['previous_direction_aligned']
    elif mutation == 'missing_pair':
        window['pairs'].pop()
    elif mutation == 'claims_utc_verified':
        summary['utc_verified'] = True
    elif mutation == 'claims_selection_was_not_posthoc':
        summary['prior_hypothesis_selection_after_results'] = False
    elif mutation == 'claims_baseline_was_hash':
        summary['baseline_check_is_metrics_not_raw_archive_hash'] = False
    elif mutation == 'wrong_reference_revision':
        reference['head_at_execution'] = 'UNKNOWN'
    elif mutation == 'nonzero_history':
        summary['history_calls_made'] = 1
    elif mutation == 'unknown_cost':
        summary['commission'] = 0
    elif mutation == 'missing_net':
        del summary['net_pnl']
    elif mutation == 'boolean_count':
        raw['previous_available'] = True
    elif mutation == 'nonfinite':
        pav['gross_total_mark_usd_model'] = float('nan')
    elif mutation == 'unequal_context':
        pda['eligible_current_minute_buckets'] = 0
    elif mutation == 'cross_model_context_mismatch':
        for mode in ('previous_available_only', 'previous_direction_aligned'):
            window['pairs'][1][mode]['previous_observed_bid_directions'] = {'up': 2, 'down': 0, 'flat': 0}
    elif mutation == 'inconsistent_signal_total':
        raw['previous_unavailable'] += 1
    elif mutation == 'too_many_aligned':
        raw['previous_available_and_direction_aligned'] = raw['previous_available'] + 1
    elif mutation == 'invalid_direction_block':
        pav['signals_blocked_by_previous_interior_direction'] = 1
    elif mutation == 'historical_rows_mismatch':
        window['returned_rows'] += 1
    elif mutation == 'historical_raw_signal_mismatch':
        for side in (pav, pda):
            side['signal_counts']['buy'] += 1
            side['raw_signals_before_all_entry_filters']['previous_unavailable'] += 1
    else:
        pav['signal_counts']['buy'] = 2000
    with pytest.raises((ValueError, TypeError, KeyError)):
        extractor.extract(summary, reference)


@pytest.mark.parametrize('mode', ['available', 'aligned'])
def test_entries_cannot_exceed_their_raw_opportunities(extractor, synthetic, mode):
    summary, reference = copy.deepcopy(synthetic.summary), copy.deepcopy(synthetic.published)
    pair = summary['windows'][0]['pairs'][0]
    pav, pda = pair['previous_available_only'], pair['previous_direction_aligned']
    if mode == 'available':
        assert pav['simulated_entries'] > 0
        for side in (pav, pda):
            raw = side['raw_signals_before_all_entry_filters']
            raw['previous_unavailable'] += raw['previous_available']
            raw['previous_available'] = 0
            raw['previous_available_and_direction_aligned'] = 0
            side['signals_blocked_by_previous_interior_direction'] = 0
    else:
        assert pda['raw_signals_before_all_entry_filters']['previous_available_and_direction_aligned'] == 0
        # Keep published trade accounting consistent so this tests the raw-opportunity bound.
        pda.update(simulated_entries=1, cash_profit_exits=1, cash_loss_exits=0,
                   position_open_at_sample_end=False)
        reference['result']['windows'][0]['pairs'][0]['pda'].update(
            entries=1, profit=1, loss=0, open_at_end=False)
    with pytest.raises(extractor.AggregateMismatch, match='pair/additional_sample/original_3_changes'):
        extractor.extract(summary, reference)


def test_input_files_are_bounded_and_refuse_symlinks(extractor, synthetic, tmp_path):
    link = tmp_path / 'summary-link.json'
    link.symlink_to(synthetic.summary_path)
    with pytest.raises(ValueError):
        extractor.load_json(link)
    parent_link = tmp_path / 'parent-link'
    parent_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        extractor.load_json(parent_link / synthetic.summary_path.name)
    huge = tmp_path / 'large.json'
    huge.write_bytes(b' ' * (extractor.MAX_BYTES + 1))
    with pytest.raises(ValueError):
        extractor.load_json(huge)
    bad = tmp_path / 'bad.json'
    for payload in ('{"value": NaN}', '{"value": 1, "value": 2}'):
        bad.write_text(payload)
        with pytest.raises(ValueError):
            extractor.load_json(bad)


@pytest.mark.skipif(not hasattr(os, 'mkfifo') or not hasattr(os, 'O_NONBLOCK'), reason='POSIX FIFO required')
def test_input_special_file_is_refused_without_blocking(extractor, tmp_path):
    fifo = tmp_path / 'summary-fifo'
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        extractor.load_json(fifo)


def set_paths(extractor, synthetic, tmp_path, monkeypatch):
    output = tmp_path / 'published-signal-counts.json'
    monkeypatch.setattr(extractor, 'SUMMARY', synthetic.summary_path)
    monkeypatch.setattr(extractor, 'REFERENCE', synthetic.published_path)
    monkeypatch.setattr(extractor, 'OUTPUT', output)
    monkeypatch.setattr(extractor.sys, 'argv', ['aggregate-extractor'])
    return output


def test_main_writes_exclusive_private_aggregate_and_does_not_repeat(extractor, synthetic, tmp_path,
                                                                     monkeypatch, capsys):
    output = set_paths(extractor, synthetic, tmp_path, monkeypatch)
    assert extractor.main() == 0
    assert output.stat().st_mode & 0o777 == 0o600
    first = output.read_bytes()
    data = json.loads(first)
    assert data['matched_scenario_pairs'] == 12
    assert b'PRIVATE_RAW_VALUE' not in first and b'PRIVATE_TIME' not in first
    assert extractor.main() == 1
    assert output.read_bytes() == first
    captured = capsys.readouterr()
    assert 'PRIVATE_RAW_VALUE' not in captured.out + captured.err


def test_main_missing_input_or_inconsistency_writes_nothing_and_hides_errors(extractor, synthetic, tmp_path,
                                                                             monkeypatch, capsys):
    output = set_paths(extractor, synthetic, tmp_path, monkeypatch)
    synthetic.summary_path.unlink()
    assert extractor.main() == 1
    assert not output.exists()
    captured = capsys.readouterr()
    assert 'Saved M1 summary is missing' in captured.err
    assert str(tmp_path) not in captured.err and not captured.out


def test_missing_reference_has_a_distinct_safe_status(extractor, synthetic, tmp_path, monkeypatch, capsys):
    output = set_paths(extractor, synthetic, tmp_path, monkeypatch)
    synthetic.published_path.unlink()
    assert extractor.main() == 1
    assert not output.exists()
    captured = capsys.readouterr()
    assert 'Published M1 reference is missing' in captured.err
    assert str(tmp_path) not in captured.err and not captured.out


def test_pair_mismatch_reports_only_fixed_scope_and_writes_nothing(extractor, synthetic, tmp_path,
                                                                   monkeypatch, capsys):
    output = set_paths(extractor, synthetic, tmp_path, monkeypatch)
    pair = synthetic.summary['windows'][0]['pairs'][0]
    pair['previous_direction_aligned']['simulated_entries'] += 1
    synthetic.summary_path.write_text(json.dumps(synthetic.summary))
    assert extractor.main() == 1
    assert not output.exists()
    captured = capsys.readouterr()
    assert 'pair/additional_sample/original_3_changes/pda' in captured.err
    assert 'no output written' in captured.err
    assert str(tmp_path) not in captured.err and 'PRIVATE_RAW_VALUE' not in captured.err and not captured.out


def test_symlink_output_directory_stops_before_reading_inputs(extractor, synthetic, tmp_path, monkeypatch, capsys):
    set_paths(extractor, synthetic, tmp_path, monkeypatch)
    target = tmp_path / 'actual-directory'
    target.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(extractor, 'OUTPUT', alias / 'counts.json')

    def forbidden(*args, **kwargs):
        pytest.fail('Must refuse a symlink output directory before reading inputs')

    monkeypatch.setattr(extractor, 'load_json', forbidden)
    assert extractor.main() == 1
    assert not (target / 'counts.json').exists()
    assert 'output directory is unsafe' in capsys.readouterr().err


def test_interrupted_output_write_is_reported_without_deleting_or_overwriting(extractor, synthetic, tmp_path,
                                                                                 monkeypatch, capsys):
    output = set_paths(extractor, synthetic, tmp_path, monkeypatch)
    real_fdopen = os.fdopen

    class FailedWriter:
        def __init__(self, fd, *args, **kwargs):
            self.stream = real_fdopen(fd, 'w', *args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, value):
            self.stream.write(value[:10])
            raise OSError('PRIVATE_WRITE_FAILURE')

    def fdopen(fd, mode, *args, **kwargs):
        if mode == 'w':
            return FailedWriter(fd, *args, **kwargs)
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(extractor.os, 'fdopen', fdopen)
    assert extractor.main() == 1
    assert output.exists() and output.read_bytes()
    captured = capsys.readouterr()
    assert 'output may be incomplete' in captured.err
    assert 'PRIVATE_WRITE_FAILURE' not in captured.err and not captured.out


def test_existing_or_symlink_output_stops_before_any_input_read(extractor, synthetic, tmp_path, monkeypatch):
    target = tmp_path / 'existing.json'
    target.write_text('do not overwrite')
    output = set_paths(extractor, synthetic, tmp_path, monkeypatch)
    output.symlink_to(target)

    def forbidden(*args, **kwargs):
        pytest.fail('Must check exclusive output before reading saved inputs')

    monkeypatch.setattr(extractor, 'load_json', forbidden)
    assert extractor.main() == 1
    assert target.read_text() == 'do not overwrite'
