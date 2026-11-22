"""Boundary-management comparisons on synthetic archives, never MT5 or live trading."""
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
    path = Path(__file__).resolve().parents[1] / 'scripts/xauusd_final_boundary_replay.py'
    spec = importlib.util.spec_from_file_location('xauusd_boundary_replay', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helper():
    path = Path(__file__).resolve().parent / 'test_xauusd_offline_next_tests.py'
    spec = importlib.util.spec_from_file_location('offline_next_test_helpers', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def boundary_fixture(start):
    """Synthetic upward quotes; an existing position crosses +$2 in the final minute."""
    start = datetime.fromisoformat(start)
    rows = []
    for index in range(300):
        bid = 100 + index*.005 + (2 if index >= 245 else 0)
        rows.append({'time_ms': (start + timedelta(seconds=index)).isoformat(),
                     'bid': bid, 'ask': bid + .1})
    return rows


@pytest.fixture
def saved(job, helper, tmp_path, monkeypatch):
    archive = tmp_path / 'five'
    archive.mkdir()
    econ = tmp_path / 'economics.json'
    econ.write_text(json.dumps(helper.info()))
    old_rows = boundary_fixture(job.OLD_WINDOW[1])
    old_raw = tmp_path / 'old.json'
    old_raw.write_text(json.dumps(helper.sample(old_rows, text=True)))
    original = replay_cash_scenario(old_rows, helper.info())
    old_cash = tmp_path / 'cash.json'
    old_cash.write_text(json.dumps({'result': {'status': 'gross_scenario_executed_net_costs_unknown', **original}}))
    old_signal = tmp_path / 'signals.json'
    old_signal.write_text(json.dumps({'result': original['signal_replay']}))
    refs, candidates = [], []
    for name, start, end in job.WINDOWS:
        rows = boundary_fixture(start)
        (archive / (name + '-raw-response.json')).write_text(json.dumps(helper.sample(rows)))
        refs.append(helper.v2_reference_window(name, start, end, rows))
        candidates.append(helper.candidate_reference_window(name, rows))
    v2 = tmp_path / 'v2.json'
    v2.write_text(json.dumps({'status': 'completed_5_windows', 'attempted_history_calls': 5, 'windows': refs}))
    prior = tmp_path / 'prior-candidates.json'
    prior.write_text(json.dumps({'result': {'status': 'completed_all_simulations', 'failures': 0,
                                            'windows': candidates}}))
    candidate = replay_cash_scenario(old_rows, helper.info(), consecutive_changes=2, max_spread_cost_usd=1)
    counts = candidate['counts']
    next_ref = tmp_path / 'previous-next-report.json'
    next_ref.write_text(json.dumps({'result': {
        'status': 'completed_all_new_tests', 'new_tests_completed': 25, 'all_baselines_reproduced': True,
        'windows': [{'id': 'additional_sample', 'tests': [{
            'variant': job.CANDIDATE[0], 'entries': counts.get('simulated_entries', 0),
            'profit': counts.get('cash_profit_exits', 0), 'loss': counts.get('cash_loss_exits', 0),
            'gross_total': candidate['gross_total_mark_usd_model']}]}]}}))

    def forbidden(*args, **kwargs):
        pytest.fail('Offline replay must not open a network socket')

    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    return SimpleNamespace(archive=archive, old_raw=old_raw, economics_file=econ, v2_reference=v2,
                           old_cash_report=old_cash, old_signal_report=old_signal,
                           previous_variants=prior, next_report=next_ref)


def test_six_samples_compare_both_models_and_preserve_inputs(job, saved):
    old_bytes = saved.old_raw.read_bytes()
    before = {p.name: p.read_bytes() for p in saved.archive.iterdir()}
    report = job.run_comparisons(**vars(saved))
    assert report['status'] == 'completed_all_comparisons'
    assert report['completed_comparisons'] == report['planned_comparisons'] == 12
    assert len(report['windows']) == 6
    for window in report['windows']:
        assert window['legacy_original_reproduced'] is True
        assert len(window['variants']) == 2
        for entry in window['variants']:
            assert entry['status'] == 'compared'
            old, new = entry['legacy'], entry['boundary_managed']
            assert old['position_open_at_last_interior_quote'] is True
            assert new['position_open_at_last_observed_quote'] is False
            assert new['final_boundary_management']['cash_profit_exits'] == 1
            assert entry['gross_total_delta_vs_legacy_usd_model'] > 0
            assert new['net_pnl'] is None
    assert report['history_calls_made'] == report['network_calls_made'] == report['orders_sent'] == 0
    assert report['execution_authorized'] is False and report['profitability_proven'] is False
    assert report['blind_holdout_or_out_of_sample'] is False
    assert saved.old_raw.read_bytes() == old_bytes
    assert {p.name: p.read_bytes() for p in saved.archive.iterdir()} == before
    result = json.dumps(report, allow_nan=False)
    assert '"bid"' not in result and '"ask"' not in result and '"time_ms"' not in result


def test_missing_sample_skips_only_its_two_scenarios(job, saved):
    (saved.archive / 'w2-raw-response.json').unlink()
    report = job.run_comparisons(**vars(saved))
    assert report['status'] == 'completed_partial_comparisons'
    assert report['completed_comparisons'] == 10
    assert report['windows'][2]['id'] == 'w2'
    assert report['windows'][2]['status'] == 'saved_tick_archive_missing_or_symlink'
    assert report['windows'][3]['id'] == 'w3' and report['windows'][3]['status'] == 'compared'


def test_candidate_reference_missing_does_not_block_other_comparisons(job, saved):
    saved.previous_variants.unlink()
    report = job.run_comparisons(**vars(saved))
    assert report['status'] == 'completed_partial_comparisons'
    assert report['completed_comparisons'] == 7  # six originals plus extra-sample candidate
    for window in report['windows'][1:]:
        assert window['variants'][0]['status'] == 'compared'
        assert window['variants'][1]['status'] == 'not_compared_prior_candidate_reference_unavailable'


def test_wrong_candidate_reference_fails_closed_for_only_that_variant(job, saved):
    report = json.loads(saved.next_report.read_text())
    report['result']['windows'][0]['tests'][0]['gross_total'] = 123456
    saved.next_report.write_text(json.dumps(report))
    result = job.run_comparisons(**vars(saved))
    assert result['status'] == 'completed_partial_comparisons'
    assert result['completed_comparisons'] == 11
    assert result['windows'][0]['variants'][1]['status'] == 'not_compared_prior_candidate_mismatch'
    assert result['windows'][1]['status'] == 'compared'
    assert '123456' not in json.dumps(result)


def test_failed_variant_does_not_stop_next_window_or_leak_error(job, saved, monkeypatch):
    real_replay = job.replay_cash_scenario

    def fail_one(rows, info, **kwargs):
        if (rows[0]['time_ms'] == job.WINDOWS[1][1]
                and kwargs.get('manage_final_boundary') is True
                and kwargs.get('consecutive_changes') == 2):
            raise ValueError('PRIVATE RAW QUOTE ERROR')
        return real_replay(rows, info, **kwargs)

    monkeypatch.setattr(job, 'replay_cash_scenario', fail_one)
    result = job.run_comparisons(**vars(saved))
    assert result['completed_comparisons'] == 11
    assert result['windows'][2]['variants'][1]['status'] == 'offline_replay_failed'
    assert result['windows'][3]['status'] == 'compared'
    assert 'PRIVATE RAW QUOTE ERROR' not in json.dumps(result, allow_nan=False)


def test_invalid_economics_blocks_without_network(job, saved):
    saved.economics_file.write_text('{}')
    report = job.run_comparisons(**vars(saved))
    assert report['status'] == 'blocked_saved_economics_unavailable_or_invalid'
    assert report['completed_comparisons'] == report['network_calls_made'] == 0


def test_main_writes_owner_only_and_never_overwrites(job, saved, tmp_path, monkeypatch, capsys):
    mapping = [('archive', 'ARCHIVE'), ('old_raw', 'OLD_RAW'), ('economics_file', 'ECONOMICS'),
               ('v2_reference', 'REFERENCE'), ('old_cash_report', 'OLD_CASH_REPORT'),
               ('old_signal_report', 'OLD_SIGNAL_REPORT'), ('previous_variants', 'PREVIOUS_VARIANTS'),
               ('next_report', 'NEXT_TEST_REPORT')]
    for key, name in mapping:
        monkeypatch.setattr(job, name, getattr(saved, key))
    output = tmp_path / 'output'
    monkeypatch.setattr(job, 'OUTPUT', output)
    assert job.main() == 0
    summary = output / 'summary.json'
    assert output.stat().st_mode & 0o777 == 0o700
    assert summary.stat().st_mode & 0o777 == 0o600
    original = summary.read_bytes()
    assert json.loads(original)['completed_comparisons'] == 12
    assert job.main() == 1
    assert summary.read_bytes() == original
    assert '"bid"' not in capsys.readouterr().out


def test_real_prior_sanitized_reference_has_one_open_entry(job):
    ref = job.extra_candidate_reference(job.NEXT_TEST_REPORT)
    assert ref['entries'] == 14 and ref['profit_exits'] == 4 and ref['loss_exits'] == 9
    assert ref['gross_total_mark'] == pytest.approx(-10.78)
    assert ref['open_at_end'] is True
