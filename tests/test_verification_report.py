"""Machine-generated counts must not silently turn failures/skips into passes."""
import runpy
from pathlib import Path

import pytest

SUMMARY = runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/verify_research.py'))['junit_summary']


@pytest.mark.parametrize('wrapper', [True, False])
def test_junit_counts_are_read_from_xml(tmp_path, wrapper):
    text = '<testsuite tests="7" failures="1" errors="1" skipped="2"/>'
    if wrapper:
        text = '<testsuites>'+text+'</testsuites>'
    path = tmp_path/'results.xml'
    path.write_text(text)
    assert SUMMARY(path) == {'tests':7,'failures':1,'errors':1,'skipped':2,'passed':3}
