"""Synthetic archived responses only; this test suite never contacts MT5."""
import importlib.util
import json
import socket
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trade_mcp.tick_cash_replay import replay_cash_scenario


@pytest.fixture
def job():
    path = Path(__file__).resolve().parents[1] / 'scripts/compare_xauusd_v2_offline_variants.py'
    spec = importlib.util.spec_from_file_location('xauusd_offline_comparisons_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def info():
    return {'trade_contract_size': 100, 'currency_profit': 'USD', 'trade_calc_mode': 'cfd leverage',
            'volume_min': .01, 'volume_max': 10, 'volume_step': .01}


def rows_for(start):
    origin = datetime.fromisoformat(start)
    # Artificially wide spread demonstrates why a rejected-entry variant can
    # avoid hypothetical losses, without claiming real-market profitability.
    return [{'time_ms': (origin + timedelta(seconds=i)).isoformat(),
             'bid': 100 + i*.01, 'ask': 102 + i*.01} for i in range(300)]


def prior_window(name, start, end, rows):
    scenario = replay_cash_scenario(rows, info())
    counts = scenario['counts']
    signal = scenario['signal_replay']
    return {'id': name, 'start': start, 'end': end, 'status': 'simulated',
            'returned_rows': len(rows), 'replayed_rows': signal['replayed_rows'],
            'interior_minutes_seen': signal['interior_minutes_seen'],
            'signal_counts': signal['signal_counts'],
            'simulated_entries': counts.get('simulated_entries'),
            'cash_loss_exits': counts.get('cash_loss_exits'),
            'cash_profit_exits': counts.get('cash_profit_exits'),
            'signals_blocked_by_simulated_loss_latch': counts.get('signals_blocked_by_simulated_loss_latch'),
            'gross_realized_usd_model': scenario['gross_realized_usd_model'],
            'gross_open_mark_usd_model': scenario['gross_open_mark_usd_model'],
            'gross_total_mark_usd_model': scenario['gross_total_mark_usd_model'],
            'position_open_at_sample_end': scenario['position_open_at_sample_end'],
            'simulated_three_consecutive_loss_latch': scenario['simulated_three_consecutive_loss_latch'],
            'net_pnl': None}


@pytest.fixture
def archives(job, tmp_path, monkeypatch):
    archive = tmp_path / 'v2-archive'
    archive.mkdir()
    economics = tmp_path / 'economics.json'
    economics.write_text(json.dumps(info()))
    reference = tmp_path / 'prior-sanitized.json'
    windows = []
    for name, start, end in job.WINDOWS:
        rows = rows_for(start)
        response = {'structuredContent': {'ok': True, 'symbol': 'XAUUSD_i',
                                          'period': 'tick', 'history': rows}}
        (archive / (name + '-raw-response.json')).write_text(json.dumps(response))
        windows.append(prior_window(name, start, end, rows))
    reference.write_text(json.dumps({'status': 'completed_5_windows',
                                     'attempted_history_calls': 5, 'windows': windows}))

    def forbidden(*args, **kwargs):
        pytest.fail('Offline comparisons must not connect or create a socket')

    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    return archive, economics, reference


def test_all_variants_continue_after_losses_using_only_local_data(job, archives):
    report = job.compare(*archives)
    assert report['status'] == 'completed_all_simulations'
    assert report['completed_simulations'] == report['max_simulations'] == 20
    assert len(report['windows']) == 5
    for window in report['windows']:
        assert window['baseline_reproduced'] is True
        assert len(window['variants']) == 4
        original, *alternatives = window['variants']
        assert original['gross_realized_usd_model'] == pytest.approx(-12)
        assert original['cash_loss_exits'] == 3
        assert original['simulated_three_consecutive_loss_latch'] is True
        assert all(variant['status'] == 'simulated' for variant in alternatives)
        assert all(variant['simulated_entries'] == 0 for variant in alternatives)
        assert all(variant['signals_blocked_by_spread'] > 0 for variant in alternatives)
    assert report['history_calls_made'] == report['network_calls_made'] == report['orders_sent'] == 0
    assert report['execution_authorized'] is False and report['profitability_proven'] is False
    assert report['blind_holdout_or_out_of_sample'] is False
    assert report['commission'] is None and report['net_pnl'] is None
    serialized = json.dumps(report)
    assert '"bid"' not in serialized and '"ask"' not in serialized and '"time_ms"' not in serialized


def test_saved_json_text_content_is_supported_without_changing_archives(job, archives):
    archive, _, _ = archives
    path = archive / 'w1-raw-response.json'
    data = json.loads(path.read_text())['structuredContent']
    data['data_available_from'] = '2026-11-01T00:00:00'
    path.write_text(json.dumps({'isError': False,
                                'content': [{'type': 'text', 'text': json.dumps(data)}]}))
    before = {p.name: p.read_bytes() for p in archive.iterdir()}
    report = job.compare(*archives)
    assert report['status'] == 'completed_all_simulations'
    assert report['windows'][0]['baseline_reproduced'] is True
    assert {p.name: p.read_bytes() for p in archive.iterdir()} == before


def test_bad_local_window_is_skipped_and_next_window_still_runs(job, archives):
    archive, _, _ = archives
    (archive / 'w2-raw-response.json').unlink()
    report = job.compare(*archives)
    assert report['status'] == 'completed_partial_simulations'
    assert report['completed_simulations'] == 16
    assert report['windows'][1]['status'] == 'saved_window_unavailable_invalid_or_unreplayable'
    assert all(item['status'].startswith('not_simulated_') for item in report['windows'][1]['variants'])
    assert report['windows'][2]['baseline_reproduced'] is True
    assert all(item['status'] == 'simulated' for item in report['windows'][4]['variants'])


def test_baseline_mismatch_is_not_misreported_and_remaining_windows_continue(job, archives):
    _, _, path = archives
    ref = json.loads(path.read_text())
    ref['windows'][1]['gross_realized_usd_model'] = 123456
    path.write_text(json.dumps(ref))
    report = job.compare(*archives)
    assert report['status'] == 'completed_partial_simulations'
    assert report['completed_simulations'] == 16
    assert report['windows'][1]['status'] == 'prior_baseline_mismatch'
    assert report['windows'][1]['baseline_reproduced'] is False
    assert report['windows'][2]['baseline_reproduced'] is True
    assert '123456' not in json.dumps(report)


def test_unexpected_baseline_failure_does_not_stop_next_window(job, archives, monkeypatch):
    real_replay = job.replay_cash_scenario

    def broken_one_window(rows, metadata, **kwargs):
        if rows[0]['time_ms'] == job.WINDOWS[1][1]:
            raise RuntimeError('PRIVATE BASELINE DETAIL')
        return real_replay(rows, metadata, **kwargs)

    monkeypatch.setattr(job, 'replay_cash_scenario', broken_one_window)
    report = job.compare(*archives)
    assert report['completed_simulations'] == 16
    assert report['windows'][1]['status'] == 'saved_window_unavailable_invalid_or_unreplayable'
    assert report['windows'][2]['baseline_reproduced'] is True
    assert 'PRIVATE BASELINE DETAIL' not in json.dumps(report)


def test_broken_variant_does_not_stop_next_variant_or_leak_error(job, archives, monkeypatch):
    real_replay = job.replay_cash_scenario

    def broken_one(rows, metadata, **kwargs):
        if kwargs.get('max_spread_cost_usd') == .5:
            raise ValueError('PRIVATE RAW PROVIDER DETAIL')
        return real_replay(rows, metadata, **kwargs)

    monkeypatch.setattr(job, 'replay_cash_scenario', broken_one)
    report = job.compare(*archives)
    assert report['completed_simulations'] == 15
    assert report['status'] == 'completed_partial_simulations'
    for window in report['windows']:
        assert [v['status'] for v in window['variants']] == [
            'simulated', 'offline_simulation_failed', 'simulated', 'simulated']
    assert 'PRIVATE RAW PROVIDER DETAIL' not in json.dumps(report)


def test_nonfinite_variant_is_recorded_as_failure_and_next_variant_runs(job, archives, monkeypatch):
    real_replay = job.replay_cash_scenario

    def nonfinite_one(rows, metadata, **kwargs):
        scenario = real_replay(rows, metadata, **kwargs)
        if kwargs.get('max_spread_cost_usd') == .5:
            scenario['gross_total_mark_usd_model'] = float('nan')
        return scenario

    monkeypatch.setattr(job, 'replay_cash_scenario', nonfinite_one)
    report = job.compare(*archives)
    assert report['completed_simulations'] == 15
    assert report['windows'][0]['variants'][1]['status'] == 'offline_simulation_failed'
    assert report['windows'][0]['variants'][2]['status'] == 'simulated'
    json.dumps(report, allow_nan=False)


def test_symlinked_raw_input_is_not_followed(job, archives, tmp_path):
    archive, _, _ = archives
    raw = archive / 'w3-raw-response.json'
    target = tmp_path / 'foreign.json'
    target.write_text(raw.read_text())
    raw.unlink()
    raw.symlink_to(target)
    report = job.compare(*archives)
    assert report['completed_simulations'] == 16
    assert report['windows'][2]['status'] == 'saved_window_unavailable_invalid_or_unreplayable'
    assert report['windows'][3]['baseline_reproduced'] is True


def test_missing_economics_or_reference_fails_without_connections(job, archives):
    archive, econ, reference = archives
    econ.unlink()
    result = job.compare(archive, econ, reference)
    assert result['status'] == 'blocked_saved_economics_unavailable_or_invalid'
    econ.write_text(json.dumps(info()))
    reference.unlink()
    result = job.compare(archive, econ, reference)
    assert result['status'] == 'blocked_prior_sanitized_reference_unavailable_or_invalid'
    assert result['history_calls_made'] == result['orders_sent'] == 0


def test_main_writes_private_sanitized_output_without_overwriting(job, archives, tmp_path, monkeypatch, capsys):
    archive, economics, reference = archives
    output = tmp_path / 'results'
    monkeypatch.setattr(job, 'ARCHIVE', archive)
    monkeypatch.setattr(job, 'ECONOMICS', economics)
    monkeypatch.setattr(job, 'REFERENCE', reference)
    monkeypatch.setattr(job, 'OUTPUT', output)
    assert job.main() == 0
    summary = output / 'summary.json'
    assert output.stat().st_mode & 0o777 == 0o700
    assert summary.stat().st_mode & 0o777 == 0o600
    first = summary.read_text()
    assert json.loads(first)['completed_simulations'] == 20
    assert job.main() == 1
    assert summary.read_text() == first
    out = capsys.readouterr().out
    assert '"bid"' not in out and '"ask"' not in out and '"time_ms"' not in out


def test_public_reference_matches_five_fixed_windows(job):
    refs = job.valid_reference(job.read_json(job.REFERENCE))
    assert list(refs) == [name for name, _, _ in job.WINDOWS]
    assert len(job.VARIANTS) == 4
    assert len({name for name, _, _ in job.VARIANTS}) == 4
