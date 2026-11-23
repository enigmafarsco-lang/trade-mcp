"""Synthetic local archives; no network or trading."""
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def job():
    path = Path(__file__).resolve().parents[1]/'scripts/replay_existing_xauusd_archives.py'
    spec = importlib.util.spec_from_file_location('existing_xauusd_archives', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def sample(day=9):
    start = datetime(2026, 11, day, 9)
    rows = [{'time_ms': (start+timedelta(seconds=i)).isoformat(), 'bid': 100, 'ask': 100.1}
            for i in range(300)]
    return {'content': [{'type': 'text', 'text': json.dumps({'ok': True, 'symbol': 'XAUUSD_i',
                                                             'period': 'tick', 'history': rows})}]}


def econ():
    return {'trade_contract_size': 100, 'volume_min': .01, 'volume_max': 10,
            'volume_step': .01, 'currency_profit': 'USD', 'trade_calc_mode': 'cfd leverage'}


def save(root, name, data):
    path = root/name/'raw-response.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_offline_two_days_automatic_replay_and_no_raw_prices(job, tmp_path):
    save(tmp_path, 'a', sample(9))
    save(tmp_path, 'b', sample(10))
    economics = tmp_path/'econ.json'
    economics.write_text(json.dumps(econ()))
    report = job.evaluate(tmp_path, economics)
    assert report['status'] == 'existing_samples_replayed'
    assert report['distinct_broker_label_dates'] == 2
    assert report['unique_usable_nonoverlapping_samples'] == 2
    assert report['history_calls_made'] == 0 and report['new_network_calls'] == 0
    assert report['net_pnl'] is None and report['profitability_proven'] is False
    assert all(w['scenario']['net_pnl'] is None for w in report['windows'])
    assert all(w['scenario']['schema'] == 'xauusd-next-quote-cash-scenario/v1' for w in report['windows'])
    assert all('final_boundary_management' not in w['scenario'] for w in report['windows'])
    assert '100.1' not in json.dumps(report)


def test_duplicate_overlap_and_wrong_provenance_excluded(job, tmp_path):
    first = sample()
    save(tmp_path, 'a', first)
    save(tmp_path, 'b', first)
    overlap = sample()
    payload = json.loads(overlap['content'][0]['text'])
    payload['history'][1]['bid'] = 100.01
    overlap['content'][0]['text'] = json.dumps(payload)
    save(tmp_path, 'c', overlap)
    payload['symbol'] = 'OTHER'
    overlap['content'][0]['text'] = json.dumps(payload)
    save(tmp_path, 'd', overlap)
    selected, reasons = job.collect(job.candidate_paths(tmp_path))
    assert len(selected) == 1
    assert reasons == {'duplicate_file': 1, 'unverified_symbol_period_or_success': 1, 'overlap_excluded': 1}


def test_ignore_symlinks_and_unrelated_files(job, tmp_path):
    external = tmp_path/'external'
    external.mkdir()
    save(external, 'other', sample())
    root = tmp_path/'root'
    root.mkdir()
    (root/'linked').symlink_to(external, target_is_directory=True)
    (root/'other.json').write_text('{}')
    assert job.candidate_paths(root) == []


def test_input_caps_fail_closed(job, tmp_path, monkeypatch):
    save(tmp_path, 'a', sample())
    monkeypatch.setattr(job, 'MAX_FILES', 0)
    with pytest.raises(ValueError, match='inventory_file_cap_exceeded'):
        job.candidate_paths(tmp_path)


def test_main_output_is_private_and_does_not_overwrite(job, tmp_path, monkeypatch):
    save(tmp_path, 'a', sample())
    info = tmp_path/'economics.json'
    info.write_text(json.dumps(econ()))
    output = tmp_path/'result.json'
    monkeypatch.setattr(job, 'ROOT', tmp_path)
    monkeypatch.setattr(job, 'ECONOMICS', info)
    monkeypatch.setattr(job, 'OUTPUT', output)
    assert job.main() == 0
    assert output.stat().st_mode & 0o777 == 0o600
    assert job.main() == 1
