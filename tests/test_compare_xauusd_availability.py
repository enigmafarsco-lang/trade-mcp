import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def script():
    path = Path(__file__).resolve().parents[1] / 'scripts/compare_xauusd_availability.py'
    spec = importlib.util.spec_from_file_location('availability_comparison_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def response(date):
    payload = {'symbol': 'XAUUSD_i', 'period': 'tick', 'ok': False, 'history': [],
               'data_available_from': date}
    return {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(payload)}]}


@pytest.mark.parametrize(('date', 'first'), [
    ('2026-11-08T00:00:00', 'availability_label_at_or_before_window_start'),
    ('2026-11-09T09:02:00', 'availability_label_inside_window'),
    ('2026-11-09T09:05:00', 'availability_label_at_or_after_window_end'),
    ('2026-11-11T00:00:00', 'availability_label_at_or_after_window_end'),
])
def test_comparison_without_printing_raw_value(script, date, first):
    report = script.compare(response(date))
    assert report['window_relationships']['eval-1'] == first
    assert date not in json.dumps(report)
    assert report['provider_root_cause_verified'] is False
    assert report['utc_verified'] is False
    assert report['history_calls_made'] == 0


@pytest.mark.parametrize('value', ['2026-11-09T09:00:00Z', 'not a date', None, 12345])
def test_no_conversion_or_timestamp_guessing(script, value):
    report = script.compare(response(value))
    assert report['status'] == 'not_comparable'
    assert not report['window_relationships']


def test_refuse_wrong_symbol_or_nonempty_history(script):
    result = {'structuredContent': {'symbol': 'OTHER', 'period': 'tick', 'ok': False,
                                    'history': [], 'data_available_from': '2026-11-09T09:00:00'}}
    assert script.compare(result)['blocker'] == 'saved_payload_does_not_match_failed_request'


def test_main_uses_local_file_and_owner_only_exclusive_output(script, tmp_path, monkeypatch):
    source, output = tmp_path/'in.json', tmp_path/'out.json'
    saved = json.dumps(response('2026-11-11T00:00:00'))
    source.write_text(saved)
    monkeypatch.setattr(script, 'RAW', source)
    monkeypatch.setattr(script, 'OUTPUT', output)
    assert script.main() == 0
    assert source.read_text() == saved
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        script.main()
