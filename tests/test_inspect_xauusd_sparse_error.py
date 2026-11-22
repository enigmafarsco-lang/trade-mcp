import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def inspector():
    path = Path(__file__).resolve().parents[1] / 'scripts/inspect_xauusd_sparse_error.py'
    spec = importlib.util.spec_from_file_location('offline_error_inspector', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_provider_content_or_secrets_exposed(inspector):
    result = {'isError': True, 'structuredContent': {'ok': False,
              'error': {'message': 'No history for XAUUSD_i; secret token XYZ'}}}
    report = inspector.classify(result)
    assert report['is_error_true']
    assert report['payload_ok_false']
    assert report['payload_error_truthy']
    assert report['phrase_hints_unverified'] == ['history_unavailable_phrase']
    assert 'XYZ' not in json.dumps(report)
    assert 'XAUUSD_i' not in json.dumps(report)
    assert report['root_cause_verified'] is False


def test_mcp_text_json_and_unknown_message(inspector):
    data = {'ok': False, 'error': 'opaque proprietary provider detail'}
    result = {'content': [{'type': 'text', 'text': json.dumps(data)}]}
    report = inspector.classify(result)
    assert report['payload_ok_false'] and report['payload_error_truthy']
    assert report['phrase_hints_unverified'] == []
    assert 'proprietary' not in json.dumps(report)


def test_offline_main_preserves_original_and_exclusive_output(inspector, tmp_path, monkeypatch, capsys):
    raw = tmp_path / 'raw.json'
    original = json.dumps({'isError': True, 'content': [{'type': 'text', 'text': 'invalid argument'}]})
    raw.write_text(original)
    monkeypatch.setattr(inspector, 'RAW', raw)
    output = tmp_path / 'diag.json'
    monkeypatch.setattr(inspector, 'OUTPUT', output)
    assert inspector.main() == 0
    assert raw.read_text() == original
    assert output.stat().st_mode & 0o777 == 0o600
    assert 'invalid argument' not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        inspector.main()
