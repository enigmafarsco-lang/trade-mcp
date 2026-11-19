import importlib.util
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
