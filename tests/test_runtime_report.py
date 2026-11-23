import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'runtime_report', Path(__file__).resolve().parents[1] / 'scripts/publish_runtime_report.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_report_unknown_is_not_success():
    report = module.sanitize({'commit': 'a' * 40, 'checks': {'compile': 'pass'}})
    assert report['checks']['order'] == 'not_run'
    assert report['counts']['orders_submitted'] is None
    assert report['execution_authorized_by_report'] is False


@pytest.mark.parametrize('extra', [
    {'account': 'private'}, {'checks': {'compile': 'secret'}},
    {'counts': {'orders_submitted': True}}, {'counts': {'deals': -1}},
    {'checks': {'raw_error': 'private'}}, {'commit': 'private'},
])
def test_private_or_invalid_data_rejected(extra):
    with pytest.raises(ValueError):
        module.sanitize({'commit': 'a' * 40, **extra})


def test_engine_evidence_is_separate_from_cloud_analysis():
    result = module.sanitize({'commit': 'a' * 40, 'checks': {'technical_builtin': 'pass',
                              'broker_engine': 'blocked', 'web_engine': 'pass'}})
    assert result['checks']['analysis'] == 'not_run'
    assert result['checks']['technical_builtin'] == 'pass'
    assert result['execution_authorized_by_report'] is False


def daytrade_evidence():
    return {'commit': 'a' * 40, 'checks': {'daytrade_offline_tests': 'pass'}, 'counts': {
        'daytrade_pytest_passed': 53, 'daytrade_pytest_skipped': 1,
        'daytrade_pytest_failed': 0, 'daytrade_pytest_errors': 0,
        'daytrade_pytest_total': 54, 'daytrade_pytest_exit_code': 0}}


def test_sanitized_daytrade_report_preserves_skips_and_does_not_claim_live_validation():
    result = module.sanitize(daytrade_evidence())
    assert result['checks']['daytrade_offline_tests'] == 'pass'
    assert result['counts']['daytrade_pytest_passed'] == 53
    assert result['counts']['daytrade_pytest_skipped'] == 1
    assert result['counts']['daytrade_pytest_total'] == 54
    assert result['checks']['broker_engine'] == 'not_run'
    assert result['checks']['web_engine'] == 'not_run'
    assert result['checks']['order'] == 'not_run'
    assert result['counts']['orders_submitted'] is None
    assert result['execution_authorized_by_report'] is False


@pytest.mark.parametrize(('field', 'value'), [
    ('daytrade_pytest_passed', 0), ('daytrade_pytest_failed', 1),
    ('daytrade_pytest_errors', 1), ('daytrade_pytest_exit_code', 1),
    ('daytrade_pytest_total', 55), ('daytrade_pytest_skipped', True),
    ('daytrade_pytest_failed', -1),
])
def test_sanitizer_rejects_inconsistent_or_invalid_daytrade_pass(field, value):
    evidence = daytrade_evidence()
    evidence['counts'][field] = value
    with pytest.raises(ValueError):
        module.sanitize(evidence)


def test_daytrade_unknown_counts_are_null_and_never_invented():
    evidence = daytrade_evidence()
    del evidence['counts']['daytrade_pytest_skipped']
    with pytest.raises(ValueError, match='Incomplete'):
        module.sanitize(evidence)
    evidence['checks']['daytrade_offline_tests'] = 'blocked'
    report = module.sanitize(evidence)
    assert report['counts']['daytrade_pytest_skipped'] is None
    evidence['checks'].pop('daytrade_offline_tests')
    with pytest.raises(ValueError, match='measured check'):
        module.sanitize(evidence)


def test_daytrade_report_writer_does_not_overwrite_or_copy_raw_input(tmp_path):
    source, output = tmp_path / 'input.json', tmp_path / 'report.json'
    source.write_text(json.dumps(daytrade_evidence()))
    command = [sys.executable, str(Path(module.__file__)), str(source), str(output)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    saved = output.read_bytes()
    report = json.loads(saved)
    assert report['counts']['daytrade_pytest_skipped'] == 1
    assert report['checks']['daytrade_offline_tests'] == 'pass'
    assert 'source' not in report and 'raw' not in report
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert output.read_bytes() == saved
