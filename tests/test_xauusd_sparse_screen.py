"""Synthetic tests for bounded history screening, not actual market evidence."""
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def job():
    path = Path(__file__).resolve().parents[1]/'scripts/xauusd_sparse_screen.py'
    spec = importlib.util.spec_from_file_location('sparse_screen_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def info():
    return {'trade_contract_size': 100, 'currency_profit': 'USD', 'trade_calc_mode': 'cfd leverage',
            'volume_min': .01, 'volume_max': 10, 'volume_step': .01}


def valid_result(start):
    start = datetime.fromisoformat(start)
    return {'structuredContent': {'ticks': [
        {'time_ms': (start + timedelta(seconds=i)).isoformat(), 'bid': 100, 'ask': 100.1}
        for i in range(300)]}}


class Client:
    def __init__(self, result=None, error_on=None):
        self.calls = []
        self.result = result
        self.error_on = error_on

    def list_tools(self):
        return [{'name': 'get_chart_ticks_history', 'inputSchema': {
            'properties': {k: {} for k in ('symbol', 'datetime_from', 'datetime_to', 'limit')},
            'required': ['symbol', 'datetime_from', 'datetime_to'], 'additionalProperties': False}}]

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if len(self.calls) == self.error_on:
            raise RuntimeError('private raw error')
        return self.result if self.result is not None else valid_result(args['datetime_from'])


def report():
    return {'status': 'started', 'attempted_history_calls': 0, 'in_progress_window': None, 'windows': []}


def test_six_fixed_distinct_windows_and_separate_scenarios(job, tmp_path):
    client, data = Client(), report()
    job.run_windows(client, info(), tmp_path, data)
    assert len(client.calls) == data['attempted_history_calls'] == 6
    assert [item['id'] for item in data['windows']] == [w[0] for w in job.WINDOWS]
    assert len({call[1]['datetime_from'] for call in client.calls}) == 6
    assert all(call[0] == 'get_chart_ticks_history' and call[1]['limit'] == 2000 for call in client.calls)
    assert all(w['scenario']['net_pnl'] is None for w in data['windows'])
    assert data['status'] == 'all_six_sparse_windows_simulated'
    assert (tmp_path/'eval-1-raw-response.json').stat().st_mode & 0o777 == 0o600


def test_stop_first_error_and_record_attempt_before_call(job, tmp_path):
    client, data = Client(error_on=2), report()
    with pytest.raises(RuntimeError):
        job.run_windows(client, info(), tmp_path, data)
    assert len(client.calls) == 2
    stored = json.loads((tmp_path/'summary.json').read_text())
    assert stored['attempted_history_calls'] == 2
    assert stored['in_progress_window'] == 'eval-2'
    assert len(stored['windows']) == 1


def test_limit_error_stops_before_replay_and_later_calls(job, tmp_path):
    start = job.WINDOWS[0][1]
    result = valid_result(start)
    result['structuredContent']['ticks'] *= 7
    client, data = Client(result=result), report()
    with pytest.raises(job.StopScreen, match='possible_truncation'):
        job.run_windows(client, info(), tmp_path, data)
    assert len(client.calls) == 1
    assert not data['windows']
    assert (tmp_path/'eval-1-raw-response.json').exists()


@pytest.mark.parametrize('mutation', ['outside', 'timezone', 'unsorted', 'empty', 'provider_error'])
def test_bad_data_stops_before_next_call(job, tmp_path, mutation):
    result = valid_result(job.WINDOWS[0][1])
    rows = result['structuredContent']['ticks']
    if mutation == 'outside':
        rows[0]['time_ms'] = '2026-11-17T09:00:00'
    elif mutation == 'timezone':
        rows[0]['time_ms'] += '+00:00'
    elif mutation == 'unsorted':
        rows.reverse()
    elif mutation == 'empty':
        rows.clear()
    else:
        result['isError'] = True
    client, data = Client(result=result), report()
    with pytest.raises(job.StopScreen):
        job.run_windows(client, info(), tmp_path, data)
    assert len(client.calls) == 1
    assert not data['windows']


def test_existing_marker_prevents_all_calls(job, tmp_path, monkeypatch):
    marker = tmp_path/'screen'
    marker.mkdir()
    monkeypatch.setattr(job, 'OUTPUT', marker)
    with pytest.raises(FileExistsError):
        job.run()


def test_invalid_economics_stops_before_connect(job, tmp_path, monkeypatch):
    marker = tmp_path/'screen'
    economics = tmp_path/'economics.json'
    economics.write_text('{}')
    monkeypatch.setattr(job, 'OUTPUT', marker)
    monkeypatch.setattr(job, 'ECONOMICS', economics)
    monkeypatch.setattr(job, 'load_dotenv', lambda: pytest.fail('must stop before network'))
    assert job.run() == 1
    result = json.loads((marker/'summary.json').read_text())
    assert result['attempted_history_calls'] == 0
    assert result['status'] == 'stopped_invalid_saved_economics_file'
