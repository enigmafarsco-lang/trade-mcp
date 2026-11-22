"""Offline M1 comparison on six synthetic archives; no real or broker data."""
import importlib.util
import json
import math
import socket
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trade_mcp.mcpclient import McpHttpClient
from trade_mcp.tick_cash_replay import replay_cash_scenario


@pytest.fixture
def job():
    path = Path(__file__).resolve().parents[1] / 'scripts/xauusd_m1_previous_direction.py'
    spec = importlib.util.spec_from_file_location('xauusd_m1_previous_direction_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def info():
    return {'trade_contract_size': 100, 'currency_profit': 'USD', 'trade_calc_mode': 'cfd leverage',
            'volume_min': .01, 'volume_max': 10, 'volume_step': .01}


def rows_for(start):
    start = datetime.fromisoformat(start)
    rows = []
    for i in range(300):
        if i < 60:
            bid = 100
        elif i < 120:
            bid = 100 + (i - 60)*.01
        elif i < 180:
            bid = 100.6 - (i - 120)*.01
        elif i < 240:
            bid = 100 + (i - 180)*.01
        else:
            bid = 100.6 + (2 if i >= 245 else 0)
        rows.append({'time_ms': (start + timedelta(seconds=i)).isoformat(), 'bid': bid, 'ask': bid + .1})
    return rows


def response(rows):
    return {'structuredContent': {'symbol': 'XAUUSD_i', 'period': 'tick', 'ok': True, 'history': rows}}


def managed_ref_for(model, rows):
    _, consecutive, cap = model
    baseline = replay_cash_scenario(rows, info(), consecutive_changes=consecutive, max_spread_cost_usd=cap)
    counts = baseline['counts']
    return {'entries': counts.get('simulated_entries', 0), 'profit_exits': counts.get('cash_profit_exits', 0),
            'loss_exits': counts.get('cash_loss_exits', 0),
            'gross_total_usd_model': baseline['gross_total_mark_usd_model'],
            'open': baseline['position_open_at_sample_end'], 'latch': baseline['simulated_three_consecutive_loss_latch']}


def reference_comparison(name, model, rows):
    model_id, consecutive, cap = model
    kwargs = {'consecutive_changes': consecutive, 'max_spread_cost_usd': cap}
    old = replay_cash_scenario(rows, info(), manage_final_boundary=False, **kwargs)
    new = replay_cash_scenario(rows, info(), manage_final_boundary=True, **kwargs)

    def saved_side(scenario, open_field):
        counts = scenario['counts']
        return {'entries': counts.get('simulated_entries', 0), 'profit': counts.get('cash_profit_exits', 0),
                'loss': counts.get('cash_loss_exits', 0), 'gross_total': scenario['gross_total_mark_usd_model'],
                open_field: scenario['position_open_at_sample_end'],
                'latch': scenario['simulated_three_consecutive_loss_latch']}

    return {'sample': name, 'variant': model_id, 'legacy': saved_side(old, 'open_at_interior'),
            'managed': saved_side(new, 'open_at_last_observed'),
            'delta_vs_legacy': new['gross_total_mark_usd_model'] - old['gross_total_mark_usd_model']}


@pytest.fixture
def saved(job, tmp_path, monkeypatch):
    # Only the synthetic fixture opens the completed job's in-memory guard.
    monkeypatch.setattr(job, 'M1_RESEARCH_RUN_CLOSED', False)
    archive = tmp_path / 'five'
    archive.mkdir()
    econ = tmp_path / 'economics.json'
    econ.write_text(json.dumps(info()))
    old_raw = tmp_path / 'old.json'
    old_rows = rows_for(job.OLD_WINDOW[1])
    old_raw.write_text(json.dumps(response(old_rows)))
    comparisons = [reference_comparison(job.OLD_WINDOW[0], model, old_rows) for model in job.MODELS]
    for name, start, _end in job.WINDOWS:
        rows = rows_for(start)
        (archive / (name + '-raw-response.json')).write_text(json.dumps(response(rows)))
        comparisons.extend(reference_comparison(name, model, rows) for model in job.MODELS)
    ref = tmp_path / 'reference.json'
    ref.write_text(json.dumps({
        'schema': 'xauusd-research-check-report/v1', 'report_id': job.BOUNDARY_PATH.stem,
        'raw_data_committed': False, 'net_pnl': None,
        'result': {'schema': 'xauusd-final-boundary-replay/v1', 'status': 'completed_all_comparisons',
                   'history_calls_made': 0, 'planned_comparisons': 12, 'completed_comparisons': 12,
                   'failures': 0, 'all_legacies_reproduced': True, 'comparisons': comparisons},
    }))

    def forbidden(*args, **kwargs):
        pytest.fail('Offline comparison must not open sockets, call MCP or place orders')

    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(McpHttpClient, 'connect', forbidden)
    monkeypatch.setattr(McpHttpClient, 'call_tool', forbidden)
    return SimpleNamespace(archive=archive, old_raw=old_raw, economics_file=econ, boundary_reference=ref)


def test_real_published_reference_shape_only(job):
    # Reading a sanitized Git report is not replaying any real tick archive.
    refs = job.published_managed_references(job.BOUNDARY_PATH)
    assert len(refs) == 12
    assert refs[('w1', job.CANDIDATE)]['gross_total_usd_model'] == pytest.approx(-.20)
    assert refs[('additional_sample', job.ORIGINAL)]['gross_total_usd_model'] == pytest.approx(-6.24)


def test_published_aggregate_deltas_are_filtered_minus_matched_not_reverse(job):
    # Validate the sanitized report only; this never reads or replays archived quotes.
    path = job.ROOT / 'reports/research/20261122T130500Z-xauusd-m1-previous-direction.json'
    data = json.loads(path.read_text())
    result = data['result']
    assert 'pda.gross_total_mark minus pav.gross_total_mark' in data['delta_definition']
    assert result['status'] == 'completed_all_paired_comparisons'
    assert result['planned_paired_comparisons'] == result['completed_paired_comparisons'] == 12
    assert result['baseline_checks_attempted'] == result['baseline_checks_matched'] == 12
    assert all(result[key] is None for key in ('commission', 'fees', 'swap', 'net_pnl'))
    seen, pairs = set(), {}
    for window in result['windows']:
        for pair in window['pairs']:
            key = (window['id'], pair['model'])
            assert key not in seen and pair['baseline_matched'] is True
            seen.add(key)
            pairs[key] = pair
            for mode in ('pav', 'pda'):
                side = pair[mode]
                assert type(side['open_at_end']) is bool
                assert side['entries'] == side['profit'] + side['loss'] + int(side['open_at_end'])
                assert math.isfinite(side['gross_total_mark'])
            delta = pair['pda']['gross_total_mark'] - pair['pav']['gross_total_mark']
            assert math.isclose(delta, pair['aligned_minus_marked'], rel_tol=0, abs_tol=1e-8)
    assert seen == {(sample, model[0]) for sample in job.SAMPLES for model in job.MODELS}
    md = path.with_suffix('.md').read_text()
    assert '| Pda − Pav |' in md
    shown = set()
    for line in md.splitlines():
        columns = [part.strip() for part in line.strip('|').split('|')]
        if len(columns) != 9 or columns[0] not in job.SAMPLES:
            continue
        sample, model, pav_counts, pav_gross, pav_open, pda_counts, pda_gross, pda_open, delta = columns
        key = sample, model
        assert key in pairs and key not in shown
        shown.add(key)
        pair = pairs[key]
        for mode, counts, gross, opened in (('pav', pav_counts, pav_gross, pav_open),
                                             ('pda', pda_counts, pda_gross, pda_open)):
            side = pair[mode]
            assert tuple(map(int, counts.split('/'))) == tuple(side[k] for k in ('entries', 'profit', 'loss'))
            assert math.isclose(float(gross), side['gross_total_mark'], rel_tol=0, abs_tol=.0051)
            assert opened in ('بله', 'خیر') and (opened == 'بله') is side['open_at_end']
        assert math.isclose(float(delta), pair['aligned_minus_marked'], rel_tol=0, abs_tol=.0051)
    assert shown == seen


def test_six_synthetic_samples_run_matched_pairs_and_leave_inputs_unchanged(job, saved):
    paths = [saved.old_raw, saved.economics_file, saved.boundary_reference, *saved.archive.iterdir()]
    before = {path: path.read_bytes() for path in paths}
    report = job.run_experiment(**vars(saved))
    assert report['status'] == 'completed_all_paired_comparisons'
    assert report['completed_paired_comparisons'] == report['planned_paired_comparisons'] == 12
    assert report['baseline_checks_attempted'] == report['baseline_checks_matched'] == 12
    assert len(report['windows']) == 6
    for window in report['windows']:
        assert window['status'] == 'compared'
        for pair in window['pairs']:
            matched, aligned = pair['previous_available_only'], pair['previous_direction_aligned']
            assert pair['baseline_aggregate_matched'] is True
            assert matched['eligible_current_minute_buckets'] == aligned['eligible_current_minute_buckets'] == 2
            assert matched['previous_observed_bid_directions'] == {'up': 1, 'down': 1, 'flat': 0}
            assert matched['raw_signals_before_all_entry_filters'] == aligned['raw_signals_before_all_entry_filters']
            assert matched['simulated_entries'] > 0 and aligned['simulated_entries'] == 0
            assert pair['interpretation'] == 'direction_aligned_mode_had_no_simulated_entries'
            assert aligned['net_pnl'] is None and matched['net_pnl'] is None
    assert report['history_calls_made'] == report['network_calls_made'] == report['orders_sent'] == 0
    assert report['execution_authorized'] is False and report['profitability_proven'] is False
    assert report['blind_holdout_or_out_of_sample'] is False
    assert report['commission'] is report['fees'] is report['swap'] is report['net_pnl'] is None
    inventory = report['input_presence_only_not_validation']
    assert inventory['economics'] == inventory['published_boundary_reference'] == 'present_unverified'
    assert set(inventory['saved_samples'].values()) == {'present_unverified'}
    assert all(path.read_bytes() == contents for path, contents in before.items())
    serialized = json.dumps(report, allow_nan=False)
    assert all(secret not in serialized for secret in ('"bid"', '"ask"', '"time_ms"', 'trade_contract_size'))


def test_each_pair_uses_one_baseline_verification_and_two_new_managed_paths(job, saved, monkeypatch):
    real = job.replay_cash_scenario
    calls = []

    def recording(rows, metadata, **kwargs):
        calls.append((kwargs['consecutive_changes'], kwargs['max_spread_cost_usd'],
                      kwargs['manage_final_boundary'], kwargs.get('m1_context', 'none')))
        return real(rows, metadata, **kwargs)

    monkeypatch.setattr(job, 'replay_cash_scenario', recording)
    report = job.run_experiment(**vars(saved))
    assert report['completed_paired_comparisons'] == 12
    assert calls == [(changes, cap, True, mode) for _ in range(6) for _, changes, cap in job.MODELS
                     for mode in ('none', *job.CONTEXT_MODES)]


def test_missing_one_archive_only_skips_that_window(job, saved):
    (saved.archive / 'w2-raw-response.json').unlink()
    report = job.run_experiment(**vars(saved))
    assert report['status'] == 'completed_partial_paired_comparisons'
    assert report['completed_paired_comparisons'] == 10
    assert report['windows'][2]['id'] == 'w2'
    assert report['windows'][2]['status'] == 'saved_tick_archive_missing_or_symlink'
    assert report['windows'][3]['status'] == 'compared'


def test_symlinked_archive_is_not_followed(job, saved, tmp_path):
    path = saved.archive / 'w2-raw-response.json'
    data = path.read_bytes()
    path.unlink()
    other = tmp_path / 'external.json'
    other.write_bytes(data)
    path.symlink_to(other)
    report = job.run_experiment(**vars(saved))
    assert report['completed_paired_comparisons'] == 10
    assert report['windows'][2]['status'] == 'saved_tick_archive_missing_or_symlink'
    assert report['windows'][3]['status'] == 'compared'


def test_invalid_saved_window_does_not_leak_data_or_stop_others(job, saved):
    path = saved.archive / 'w2-raw-response.json'
    raw = json.loads(path.read_text())
    raw['structuredContent']['history'][10]['bid'] = 'PRIVATE_SAMPLE_VALUE'
    path.write_text(json.dumps(raw))
    report = job.run_experiment(**vars(saved))
    assert report['windows'][2]['status'] == 'saved_tick_archive_invalid_or_out_of_bounds'
    assert report['windows'][3]['status'] == 'compared'
    assert 'PRIVATE_SAMPLE_VALUE' not in json.dumps(report)


def test_valid_but_different_input_blocks_mismatched_baselines_without_affecting_other_windows(job, saved):
    path = saved.archive / 'w2-raw-response.json'
    raw = json.loads(path.read_text())
    for row in raw['structuredContent']['history']:
        row['bid'], row['ask'] = 100, 100.1
    path.write_text(json.dumps(raw))
    report = job.run_experiment(**vars(saved))
    assert report['completed_paired_comparisons'] == 10
    pairs = report['windows'][2]['pairs']
    assert all(pair['status'] == 'prior_managed_aggregate_mismatch' for pair in pairs)
    assert all(pair['baseline_aggregate_matched'] is False for pair in pairs)
    assert report['windows'][3]['status'] == 'compared'
    assert report['baseline_check_is_metrics_not_raw_archive_hash'] is True


def test_inconsistent_published_reference_blocks_all_without_replay(job, saved, monkeypatch):
    raw = json.loads(saved.boundary_reference.read_text())
    raw['result']['comparisons'][0]['delta_vs_legacy'] = 123456
    saved.boundary_reference.write_text(json.dumps(raw))

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid reference must fail before cash replay')

    monkeypatch.setattr(job, 'replay_cash_scenario', forbidden)
    report = job.run_experiment(**vars(saved))
    assert report['status'] == 'blocked_published_reference_unavailable_or_invalid'
    assert report['completed_paired_comparisons'] == report['baseline_checks_attempted'] == 0
    assert '123456' not in json.dumps(report)


def test_missing_economics_blocks_before_reference_or_replay(job, saved, monkeypatch):
    saved.economics_file.unlink()
    (saved.archive / 'w2-raw-response.json').unlink()

    def forbidden(*args, **kwargs):
        pytest.fail('Missing economics must fail before baseline/reference or replay')

    monkeypatch.setattr(job, 'published_managed_references', forbidden)
    report = job.run_experiment(**vars(saved))
    assert report['status'] == 'blocked_saved_economics_unavailable_or_invalid'
    assert report['completed_paired_comparisons'] == report['network_calls_made'] == 0
    inventory = report['input_presence_only_not_validation']
    assert inventory['economics'] == 'missing_or_not_regular'
    assert inventory['saved_samples']['w2'] == 'missing_or_not_regular'
    assert inventory['saved_samples']['w1'] == 'present_unverified'


def test_no_signal_or_entry_is_not_treated_as_a_positive_delta(job):
    rows = rows_for(job.OLD_WINDOW[1])
    for row in rows:
        row['bid'], row['ask'] = 100, 100.1
    model = job.MODELS[0]
    pair = job.compare_pair(model, rows, info(), managed_ref_for(model, rows))
    assert pair['status'] == 'compared'
    assert pair['interpretation'] == 'no_raw_signals_in_eligible_minutes'
    assert pair['aligned_minus_matched_gross_total_mark_usd_model'] is None


def test_no_eligible_minute_and_no_entry_status_are_explicit(job):
    original = rows_for(job.OLD_WINDOW[1])
    one_interior_minute = original[:120] + original[240:]
    model = job.MODELS[0]
    pair = job.compare_pair(model, one_interior_minute, info(), managed_ref_for(model, one_interior_minute))
    assert pair['status'] == 'compared'
    assert pair['interpretation'] == 'no_eligible_adjacent_previous_minute'
    assert pair['aligned_minus_matched_gross_total_mark_usd_model'] is None

    too_wide = [dict(row, ask=row['bid'] + 2) for row in original]
    model = job.MODELS[1]  # Previous $1 research cap blocks this synthetic spread.
    pair = job.compare_pair(model, too_wide, info(), managed_ref_for(model, too_wide))
    assert pair['status'] == 'compared'
    assert pair['previous_available_only']['raw_signals_before_all_entry_filters']['previous_available'] > 0
    assert pair['interpretation'] == 'no_simulated_entries_in_either_mode'
    assert pair['aligned_minus_matched_gross_total_mark_usd_model'] is None


def test_context_failure_is_sanitized_and_independent_pairs_continue(job, saved, monkeypatch):
    real = job.replay_cash_scenario

    def fail_one(rows, metadata, **kwargs):
        if (rows[0]['time_ms'] == job.WINDOWS[1][1]
                and kwargs.get('m1_context') == 'prior_interior_aligned'
                and kwargs.get('consecutive_changes') == 2):
            raise ValueError('PRIVATE_TICK_OR_ACCOUNT_CONTENT')
        return real(rows, metadata, **kwargs)

    monkeypatch.setattr(job, 'replay_cash_scenario', fail_one)
    report = job.run_experiment(**vars(saved))
    assert report['completed_paired_comparisons'] == 11
    assert report['windows'][2]['pairs'][1]['status'] == 'offline_comparison_failed'
    assert report['baseline_checks_attempted'] == report['baseline_checks_matched'] == 12
    assert report['windows'][3]['status'] == 'compared'
    assert 'PRIVATE_TICK_OR_ACCOUNT_CONTENT' not in json.dumps(report, allow_nan=False)


@pytest.mark.parametrize('existing_marker', [False, True])
def test_completed_job_refuses_even_without_workstation_marker(job, tmp_path, monkeypatch, capsys, existing_marker):
    assert job.M1_RESEARCH_RUN_CLOSED is True
    output = tmp_path / 'fixed-output'
    if existing_marker:
        output.mkdir()
        (output / 'summary.json').write_text('saved result')
    monkeypatch.setattr(job, 'OUTPUT', output)

    def forbidden(*args, **kwargs):
        pytest.fail('Completed job must stop before inspecting inputs or replaying')

    monkeypatch.setattr(job, 'run_experiment', forbidden)
    assert job.main() == 1
    assert output.exists() is existing_marker
    if existing_marker:
        assert (output / 'summary.json').read_text() == 'saved result'
    assert 'already completed' in capsys.readouterr().out


def test_main_writes_private_exclusive_output_and_does_not_repeat(job, saved, tmp_path, monkeypatch, capsys):
    for key, name in [('archive', 'ARCHIVE'), ('old_raw', 'OLD_RAW'), ('economics_file', 'ECONOMICS'),
                      ('boundary_reference', 'BOUNDARY_PATH')]:
        monkeypatch.setattr(job, name, getattr(saved, key))
    output = tmp_path / 'new-output'
    monkeypatch.setattr(job, 'OUTPUT', output)
    assert job.main() == 0
    summary = output / 'summary.json'
    assert output.stat().st_mode & 0o777 == 0o700
    assert summary.stat().st_mode & 0o777 == 0o600
    original = summary.read_bytes()
    assert json.loads(original)['completed_paired_comparisons'] == 12

    def forbidden(*args, **kwargs):
        pytest.fail('An existing output must be checked before any replay')

    monkeypatch.setattr(job, 'run_experiment', forbidden)
    assert job.main() == 1
    assert summary.read_bytes() == original
    assert '"bid"' not in capsys.readouterr().out


def test_main_rejects_symlink_marker_before_any_replay(job, tmp_path, monkeypatch):
    monkeypatch.setattr(job, 'M1_RESEARCH_RUN_CLOSED', False)  # Synthetic no-overwrite test only.
    output = tmp_path / 'output-link'
    target = tmp_path / 'existing'
    target.mkdir()
    output.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(job, 'OUTPUT', output)

    def forbidden(*args, **kwargs):
        pytest.fail('Must not replay or follow an existing output marker')

    monkeypatch.setattr(job, 'run_experiment', forbidden)
    assert job.main() == 1
    assert list(target.iterdir()) == []


def test_main_does_not_create_one_shot_marker_when_saved_inputs_are_missing(job, saved, tmp_path, monkeypatch):
    saved.economics_file.unlink()
    monkeypatch.setattr(job, 'ECONOMICS', saved.economics_file)
    output = tmp_path / 'not-created'
    monkeypatch.setattr(job, 'OUTPUT', output)
    assert job.main() == 1
    assert not output.exists()


def test_main_does_not_create_marker_if_no_saved_archives_remain(job, saved, tmp_path, monkeypatch):
    saved.old_raw.unlink()
    for path in saved.archive.iterdir():
        path.unlink()
    for key, name in [('archive', 'ARCHIVE'), ('old_raw', 'OLD_RAW'), ('economics_file', 'ECONOMICS'),
                      ('boundary_reference', 'BOUNDARY_PATH')]:
        monkeypatch.setattr(job, name, getattr(saved, key))
    output = tmp_path / 'not-created'
    monkeypatch.setattr(job, 'OUTPUT', output)
    assert job.main() == 1
    assert not output.exists()


def test_failed_private_write_keeps_marker_and_does_not_repeat(job, saved, tmp_path, monkeypatch, capsys):
    for key, name in [('archive', 'ARCHIVE'), ('old_raw', 'OLD_RAW'), ('economics_file', 'ECONOMICS'),
                      ('boundary_reference', 'BOUNDARY_PATH')]:
        monkeypatch.setattr(job, name, getattr(saved, key))
    output = tmp_path / 'failed-write'
    monkeypatch.setattr(job, 'OUTPUT', output)
    with monkeypatch.context() as patch:
        def no_write(*args, **kwargs):
            raise OSError('PRIVATE_ERROR')

        patch.setattr(job.os, 'open', no_write)
        assert job.main() == 1
    assert output.is_dir() and not (output / 'summary.json').exists()
    assert job.main() == 1
    assert 'PRIVATE_ERROR' not in capsys.readouterr().err


def test_main_partial_results_are_not_overwritten(job, saved, tmp_path, monkeypatch):
    (saved.archive / 'w2-raw-response.json').unlink()
    for key, name in [('archive', 'ARCHIVE'), ('old_raw', 'OLD_RAW'), ('economics_file', 'ECONOMICS'),
                      ('boundary_reference', 'BOUNDARY_PATH')]:
        monkeypatch.setattr(job, name, getattr(saved, key))
    output = tmp_path / 'partial-output'
    monkeypatch.setattr(job, 'OUTPUT', output)
    assert job.main() == 1
    summary = output / 'summary.json'
    assert json.loads(summary.read_text())['completed_paired_comparisons'] == 10
    old = summary.read_bytes()
    assert job.main() == 1 and summary.read_bytes() == old
