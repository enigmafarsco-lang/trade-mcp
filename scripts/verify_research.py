"""Offline deployment verification with machine-derived, timestamped JSON reports."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


def junit_summary(path):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == 'testsuite' else list(root.iter('testsuite'))
    totals = {key: 0 for key in ('tests', 'failures', 'errors', 'skipped')}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, 0))
    totals['passed'] = totals['tests'] - sum(totals[k] for k in ('failures','errors','skipped'))
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--research-python', required=True, type=Path)
    parser.add_argument('--output-dir', default='logs', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    interpreter = args.research_python.absolute()
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        parser.error('Research interpreter must be an executable file')
    destination = args.output_dir.absolute()
    # Reports may contain local paths. Refuse tracked/unignored output inside repo.
    if destination.is_relative_to(root):
        relative = (destination / 'research-verification.json').relative_to(root)
        checked = subprocess.run(['git','check-ignore','--quiet',str(relative)], cwd=root, check=False)
        if checked.returncode != 0:
            parser.error('Output directory inside repository must be Git-ignored')
    destination.mkdir(parents=True, exist_ok=True)
    start = datetime.now(timezone.utc)
    commit = subprocess.run(['git','rev-parse','HEAD'], cwd=root, capture_output=True,
                            text=True, check=True).stdout.strip()
    sources = ['tests/test_research.py', 'tests/test_cloud_access.py',
               'src/trade_mcp/research_worker.py', 'src/trade_mcp/cloud_access.py']
    hashes = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in sources}
    results = []
    env = dict(os.environ)
    env['RESEARCH_TEST_PYTHON'] = str(interpreter)
    # Test fixtures perform offline HTTP mocks and upstream prompt/backtest checks.
    # Do not print or persist the environment, stdout, stderr or credential data.
    with tempfile.TemporaryDirectory(prefix='trade-research-verify-') as temp:
        for index, suite in enumerate(sources[:2]):
            xml = Path(temp)/f'{index}.xml'
            command = [sys.executable, '-m', 'pytest', suite, '-q', f'--junitxml={xml}']
            try:
                result = subprocess.run(command, cwd=root, env=env, capture_output=True,
                                        timeout=180, check=False)
                row = {'suite':suite, 'exit_code':result.returncode,
                       'summary':junit_summary(xml) if xml.is_file() else None}
            except subprocess.TimeoutExpired:
                row = {'suite':suite, 'exit_code':None, 'summary':None, 'error':'test_timeout'}
            except (ET.ParseError, ValueError):
                row = {'suite':suite, 'exit_code':None, 'summary':None, 'error':'invalid_test_report'}
            results.append(row)
    ok = all(r['exit_code'] == 0 and r['summary'] and r['summary']['tests'] > 0
             and r['summary']['skipped'] == 0 and r['summary']['failures'] == 0
             and r['summary']['errors'] == 0 for r in results)
    report = {'ok':ok, 'started_at':start.isoformat(),
              'finished_at':datetime.now(timezone.utc).isoformat(), 'commit':commit,
              'test_python':sys.executable, 'research_python':str(interpreter),
              'source_sha256':hashes, 'suites':results,
              'scope':'Offline source verification only; no live console or cloud access check.'}
    path = destination / ('research-verification-'+start.strftime('%Y%m%dT%H%M%S%fZ')+'.json')
    with path.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    print(json.dumps({'ok':ok, 'report_path':str(path), 'suites':results}, indent=2))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
