"""Notebook wiring tests only: these do not run Freqtrade or claim a real backtest."""
import json
import sys
import types
import zipfile
from pathlib import Path

import pytest

from trade_mcp.engines.upstream import FREQTRADE_COMMIT, STRATEGY001_SHA256

ROOT = Path(__file__).resolve().parents[1]
NB = ROOT/'notebooks/freqtrade_strategy001_backtest_colab.ipynb'


def codes():
    nb = json.loads(NB.read_text())
    return [''.join(c['source']) for c in nb['cells'] if c['cell_type'] == 'code']


def test_notebook_uses_engine_not_signal_return_proxy():
    nb = json.loads(NB.read_text())
    for c in nb['cells']:
        if c['cell_type'] == 'code':
            assert c['execution_count'] is None and c['outputs'] == []
            compile(''.join(c['source']), '<backtest-colab>', 'exec')
    text = '\n'.join(codes())
    assert FREQTRADE_COMMIT in text and STRATEGY001_SHA256 in text
    for required in ["FREQTRADE_VERSION='2025.10'", "'evaluation':'20260101-20260301'",
                     "'final_test':'20260301-20260401'", "ft('backtesting'",
                     "'--timeframe-detail','1m'", "'--cache','none'",
                     "'--export','trades'", "'dry_run':True", "'key':'','secret':''",
                     "'fee':0.001", "'execution_authorized':False", "'is_xauusd_test':False",
                     "command in ('download-data','backtesting')", 'Historical gap; stop, do not fill']:
        assert required in text
    assert 'populate_entry_trend(' not in text
    # Python export remains identical to notebook code, not an independently maintained variant.
    export = (ROOT/'notebooks/freqtrade_strategy001_backtest_colab.py').read_text()
    for code in codes():
        assert code in export


def test_engine_result_reader_preserves_zero_trades_and_exports(tmp_path, monkeypatch):
    results = tmp_path/'results'
    plan = {'evaluation': '20260101-20260301', 'final_test': '20260301-20260401'}
    for split in plan:
        folder = results/split
        folder.mkdir(parents=True)
        with zipfile.ZipFile(folder/'result.zip', 'w') as z:
            z.writestr('result.json', json.dumps({'strategy': {'Strategy001': {
                'timeframe': '5m', 'total_trades': 0, 'profit_total': 0,
                'profit_total_abs': 0, 'profit_factor': None}}}))
    (tmp_path/'coverage.json').write_text('{}')
    (tmp_path/'dependencies.txt').write_text('mock fixture only')
    config = tmp_path/'config.json'
    config.write_text('{}')
    downloads = []
    google = types.ModuleType('google')
    colab = types.ModuleType('google.colab')
    colab.files = types.SimpleNamespace(download=downloads.append)
    monkeypatch.setitem(sys.modules, 'google', google)
    monkeypatch.setitem(sys.modules, 'google.colab', colab)
    env = dict(WORK=tmp_path, RESULTS=results, PLAN=plan, CONFIG=config,
               FREQTRADE_VERSION='2025.10', FREQTRADE_COMMIT=FREQTRADE_COMMIT,
               STRATEGY001_SHA256=STRATEGY001_SHA256, json=json, zipfile=zipfile)
    exec(compile(codes()[-1], '<mock-result-reader>', 'exec'), env)
    report = json.loads((tmp_path/'freqtrade_strategy001_engine_research.json').read_text())
    assert report['splits']['final_test']['total_trades'] == 0
    assert report['splits']['final_test']['profit_factor'] is None
    assert report['execution_authorized'] is False
    assert len(downloads) == 2
    # Missing actual archive must fail, rather than fabricating output.
    (results/'final_test'/'result.zip').unlink()
    with pytest.raises(AssertionError, match='actual engine result'):
        exec(compile(codes()[-1], '<missing-result>', 'exec'), env)
