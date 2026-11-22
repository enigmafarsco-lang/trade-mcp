"""Six synthetic archives; no MT5, network, models, orders or real latch."""
import importlib.util
import json
import socket
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trade_mcp.tick_cash_replay import replay_cash_scenario


@pytest.fixture
def job():
    path = Path(__file__).resolve().parents[1] / 'scripts/xauusd_offline_next_tests.py'
    spec = importlib.util.spec_from_file_location('xauusd_next_tests', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def info():
    return {'trade_contract_size': 100, 'currency_profit': 'USD', 'trade_calc_mode': 'cfd leverage',
            'volume_min': .01, 'volume_max': 10, 'volume_step': .01}


def rows_for(start):
    start = datetime.fromisoformat(start)
    return [{'time_ms': (start + timedelta(seconds=i)).isoformat(),
             'bid': 100 + i*.01, 'ask': 102 + i*.01} for i in range(300)]


def sample(rows, *, text=False):
    payload = {'symbol': 'XAUUSD_i', 'period': 'tick', 'ok': True, 'history': rows}
    if text:
        return {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(payload)}]}
    return {'structuredContent': payload}


def v2_reference_window(name, start, end, rows):
    model = replay_cash_scenario(rows, info(), manage_final_boundary=False)
    signal, counts = model['signal_replay'], model['counts']
    return {'id': name, 'start': start, 'end': end, 'status': 'simulated',
            'returned_rows': len(rows), 'replayed_rows': signal['replayed_rows'],
            'interior_minutes_seen': signal['interior_minutes_seen'],
            'signal_counts': signal['signal_counts'],
            'simulated_entries': counts.get('simulated_entries'),
            'cash_loss_exits': counts.get('cash_loss_exits'),
            'cash_profit_exits': counts.get('cash_profit_exits'),
            'signals_blocked_by_simulated_loss_latch': counts.get('signals_blocked_by_simulated_loss_latch'),
            'gross_realized_usd_model': model['gross_realized_usd_model'],
            'gross_open_mark_usd_model': model['gross_open_mark_usd_model'],
            'gross_total_mark_usd_model': model['gross_total_mark_usd_model'],
            'position_open_at_sample_end': model['position_open_at_sample_end'],
            'simulated_three_consecutive_loss_latch': model['simulated_three_consecutive_loss_latch'],
            'net_pnl': None}


def candidate_reference_window(name, rows):
    candidate = replay_cash_scenario(rows, info(), consecutive_changes=2, max_spread_cost_usd=1,
                                     manage_final_boundary=False)
    count = candidate['counts']
    return {'id': name, 'baseline_reproduced': True, 'variants': [{
        'variant_id': '2_changes_spread_cap_1_00',
        'entries': count.get('simulated_entries', 0), 'profit_exits': count.get('cash_profit_exits', 0),
        'loss_exits': count.get('cash_loss_exits', 0), 'gross_total_mark': candidate['gross_total_mark_usd_model'],
        'open_at_end': candidate['position_open_at_sample_end'],
        'latch': candidate['simulated_three_consecutive_loss_latch']}]}


@pytest.fixture
def saved(job, tmp_path, monkeypatch):
    archive = tmp_path / 'five'
    archive.mkdir()
    econ = tmp_path / 'economics.json'
    econ.write_text(json.dumps(info()))
    old_raw = tmp_path / 'old.json'
    old_rows = rows_for(job.OLD_WINDOW[1])
    old_raw.write_text(json.dumps(sample(old_rows, text=True)))
    old_model = replay_cash_scenario(old_rows, info(), manage_final_boundary=False)
    cash = tmp_path / 'old-cash-report.json'
    cash.write_text(json.dumps({'result': {'status': 'gross_scenario_executed_net_costs_unknown', **old_model}}))
    signal = tmp_path / 'old-signal-report.json'
    signal.write_text(json.dumps({'result': old_model['signal_replay']}))
    v2_refs, candidates = [], []
    for name, start, end in job.WINDOWS:
        rows = rows_for(start)
        (archive / (name + '-raw-response.json')).write_text(json.dumps(sample(rows)))
        v2_refs.append(v2_reference_window(name, start, end, rows))
        candidates.append(candidate_reference_window(name, rows))
    v2 = tmp_path / 'prior-v2.json'
    v2.write_text(json.dumps({'status': 'completed_5_windows', 'attempted_history_calls': 5, 'windows': v2_refs}))
    previous = tmp_path / 'prior-candidates.json'
    previous.write_text(json.dumps({'result': {'status': 'completed_all_simulations', 'failures': 0,
                                                'windows': candidates}}))

    def forbidden(*args, **kwargs):
        pytest.fail('Must not open any network socket')

    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    return SimpleNamespace(archive=archive, old_raw=old_raw, economics_file=econ, v2_reference=v2,
                           old_cash_report=cash, old_signal_report=signal, previous_variants=previous)


def args(saved):
    return vars(saved)


def test_all_new_tests_complete_and_losses_do_not_stop_later_tests(job, saved):
    before = {p.name: p.read_bytes() for p in saved.archive.iterdir()}
    old_before = saved.old_raw.read_bytes()
    report = job.run_campaign(**args(saved))
    assert report['status'] == 'completed_all_new_tests'
    assert report['new_tests_planned'] == report['new_tests_completed'] == 25
    assert report['baseline_checks_attempted'] == 6
    assert len(report['windows']) == 6
    additional = report['windows'][0]
    assert additional['id'] == 'additional_sample'
    assert additional['baseline_reproduced'] is True
    assert additional['variants'][0]['variant_id'] == '2_changes_spread_cap_1_00'
    assert len(additional['variants']) == 5
    for window in report['windows'][1:]:
        assert window['baseline_reproduced'] is True
        assert len(window['variants']) == 4
        no_cap = window['variants'][0]
        assert no_cap['variant_id'] == '2_changes_no_spread_cap'
        assert no_cap['cash_loss_exits'] == 3
        assert no_cap['same_reported_metrics_as_previous_2_cap_1'] is False
        assert all(v['status'] == 'simulated' for v in window['variants'])
    assert report['history_calls_made'] == report['network_calls_made'] == report['orders_sent'] == 0
    assert report['profitability_proven'] is False and report['execution_authorized'] is False
    assert report['prior_sample_baseline_was_known'] and report['not_blind_or_unbiased_holdout']
    assert report['net_pnl'] is None and report['commission'] is None
    assert {p.name: p.read_bytes() for p in saved.archive.iterdir()} == before
    assert saved.old_raw.read_bytes() == old_before
    serialized = json.dumps(report, allow_nan=False)
    assert '"bid"' not in serialized and '"ask"' not in serialized and '"time_ms"' not in serialized


def test_archived_campaign_explicitly_uses_legacy_horizon(job, saved, monkeypatch):
    real_replay = job.replay_cash_scenario
    modes = []

    def record_mode(rows, metadata, **kwargs):
        modes.append(kwargs.get('manage_final_boundary'))
        return real_replay(rows, metadata, **kwargs)

    monkeypatch.setattr(job, 'replay_cash_scenario', record_mode)
    report = job.run_campaign(**args(saved))
    assert report['status'] == 'completed_all_new_tests'
    assert len(modes) == 31 and all(mode is False for mode in modes)


def test_additional_sample_uses_observed_naive_labels_not_invented_utc(job, saved):
    response = json.loads(saved.old_raw.read_text())
    data = json.loads(response['content'][0]['text'])
    for row in data['history']:
        stamp = datetime.fromisoformat(row['time_ms']) + timedelta(hours=1)
        row['time_ms'] = stamp.isoformat()
    response['content'][0]['text'] = json.dumps(data)
    saved.old_raw.write_text(json.dumps(response))
    report = job.run_campaign(**args(saved))
    assert report['status'] == 'completed_all_new_tests'
    assert report['windows'][0]['baseline_reproduced'] is True
    assert report['additional_sample_requested_bounds_independently_verified'] is False
    assert report['utc_verified'] is False


def test_additional_sample_overlapping_v2_labels_is_skipped(job, saved):
    response = json.loads((saved.archive / 'w1-raw-response.json').read_text())
    saved.old_raw.write_text(json.dumps(response))
    report = job.run_campaign(**args(saved))
    assert report['windows'][0]['status'] == 'saved_tick_archive_invalid_or_out_of_bounds'
    assert report['new_tests_completed'] == 20
    assert report['windows'][1]['status'] == 'simulated'


def test_missing_additional_sample_does_not_prevent_next_20_tests(job, saved):
    saved.old_raw.unlink()
    report = job.run_campaign(**args(saved))
    assert report['status'] == 'completed_partial_new_tests'
    assert report['new_tests_completed'] == 20
    assert report['windows'][0]['status'] == 'saved_tick_archive_missing_or_symlink'
    assert all(v['status'] == 'not_simulated_saved_tick_archive_missing_or_symlink'
               for v in report['windows'][0]['variants'])
    assert all(w['status'] == 'simulated' for w in report['windows'][1:])


def test_missing_one_v2_input_skips_only_that_window(job, saved):
    (saved.archive / 'w2-raw-response.json').unlink()
    report = job.run_campaign(**args(saved))
    assert report['new_tests_completed'] == 21
    assert report['windows'][2]['id'] == 'w2'
    assert report['windows'][2]['status'] == 'saved_tick_archive_missing_or_symlink'
    assert report['windows'][3]['id'] == 'w3' and report['windows'][3]['status'] == 'simulated'


def test_old_baseline_mismatch_skips_variants_but_not_other_samples(job, saved):
    old = json.loads(saved.old_cash_report.read_text())
    old['result']['gross_total_mark_usd_model'] = 123456
    saved.old_cash_report.write_text(json.dumps(old))
    report = job.run_campaign(**args(saved))
    assert report['windows'][0]['status'] == 'prior_baseline_mismatch'
    assert report['new_tests_completed'] == 20
    assert report['windows'][1]['baseline_reproduced'] is True
    assert '123456' not in json.dumps(report)


def test_v2_reference_unavailable_does_not_prevent_earlier_sample(job, saved):
    saved.v2_reference.unlink()
    report = job.run_campaign(**args(saved))
    assert report['status'] == 'completed_partial_new_tests'
    assert report['new_tests_completed'] == 5
    assert report['windows'][0]['status'] == 'simulated'
    assert all(w['status'] == 'prior_baseline_reference_unavailable' for w in report['windows'][1:])


def test_prior_candidate_report_is_optional_for_new_simulations(job, saved):
    saved.previous_variants.unlink()
    report = job.run_campaign(**args(saved))
    assert report['status'] == 'completed_all_new_tests'
    assert report['new_tests_completed'] == 25
    assert all(v['prior_candidate_comparison'] == 'unavailable'
               for w in report['windows'][1:] for v in w['variants'])


def test_failure_or_nonfinite_one_variant_continues_others(job, saved, monkeypatch):
    real_replay = job.replay_cash_scenario

    def broken(rows, metadata, **kwargs):
        if kwargs.get('max_spread_cost_usd') == .75 and kwargs.get('consecutive_changes') == 2:
            raise ValueError('PRIVATE RAW ERROR')
        result = real_replay(rows, metadata, **kwargs)
        if kwargs.get('max_spread_cost_usd') == .5:
            result['gross_total_mark_usd_model'] = float('nan')
        return result

    monkeypatch.setattr(job, 'replay_cash_scenario', broken)
    report = job.run_campaign(**args(saved))
    assert report['new_tests_completed'] == 13
    assert report['status'] == 'completed_partial_new_tests'
    for window in report['windows']:
        assert window['status'] == 'some_variants_failed'
        assert window['variants'][-1]['variant_id'] == '3_changes_spread_cap_0_75'
        assert window['variants'][-1]['status'] == 'simulated'
    assert 'PRIVATE RAW ERROR' not in json.dumps(report, allow_nan=False)


def test_saved_economics_missing_blocks_without_network(job, saved):
    saved.economics_file.unlink()
    report = job.run_campaign(**args(saved))
    assert report['status'] == 'blocked_saved_economics_unavailable_or_invalid'
    assert report['new_tests_completed'] == 0 and report['history_calls_made'] == 0


def test_main_output_private_and_no_overwrite(job, saved, tmp_path, monkeypatch, capsys):
    for key, name in [('archive', 'ARCHIVE'), ('old_raw', 'OLD_RAW'), ('economics_file', 'ECONOMICS'),
                      ('v2_reference', 'REFERENCE'), ('old_cash_report', 'OLD_CASH_REPORT'),
                      ('old_signal_report', 'OLD_SIGNAL_REPORT'), ('previous_variants', 'PREVIOUS_VARIANTS')]:
        monkeypatch.setattr(job, name, getattr(saved, key))
    output = tmp_path / 'output'
    monkeypatch.setattr(job, 'OUTPUT', output)
    assert job.main() == 0
    summary = output / 'summary.json'
    assert output.stat().st_mode & 0o777 == 0o700
    assert summary.stat().st_mode & 0o777 == 0o600
    original = summary.read_bytes()
    assert json.loads(original)['new_tests_completed'] == 25
    assert job.main() == 1
    assert summary.read_bytes() == original
    assert '"bid"' not in capsys.readouterr().out


def test_published_old_reports_have_fields_needed_for_comparison(job):
    cash = job.read_json(job.OLD_CASH_REPORT)
    signal = job.read_json(job.OLD_SIGNAL_REPORT)
    # Check the reference shape only; this does not execute or verify real ticks.
    historical_report_shape = dict(cash['result'], signal_replay=signal['result'])
    assert job.previous_original_matches(historical_report_shape, cash, signal)


def test_real_sanitized_reference_has_prior_candidate_for_all_five_windows(job):
    candidates = job.read_previous_candidates(job.PREVIOUS_VARIANTS)
    assert set(candidates) == {name for name, _, _ in job.WINDOWS}
    assert candidates['w1']['gross_total_mark'] == pytest.approx(1.22)
    assert candidates['w4']['gross_total_mark'] == pytest.approx(-6.32)
