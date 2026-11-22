"""Synthetic validation fixtures ONLY, not financial evidence or an engine backtest."""
import copy
import socket

import pytest

from trade_mcp import freqtrade_offline as offline


@pytest.fixture
def bundle(monkeypatch):
    monkeypatch.setattr(offline, 'START', 0)
    monkeypatch.setattr(offline, 'END', 600)
    return {'schema': 'freqtrade-offline-bundle/v1',
            'provenance': {'exchange': 'binance', 'source': 'SYNTHETIC UNIT TEST ONLY',
                           'captured_at': '2026-01-01T00:00:00Z', 'ccxt_version': 'test'},
            'market': {'symbol': 'BTC/USDT', 'base': 'BTC', 'quote': 'USDT', 'id': 'BTCUSDT',
                       'spot': True, 'active': True, 'contract': False,
                       'precision': {'amount': 0.00001, 'price': 0.01},
                       'limits': {k: {'min': 0.01, 'max': None} for k in ('price', 'amount', 'cost')}},
            'candles': {'1m': [[t, 100, 102, 99, 101, 2] for t in range(0, 600, 60)],
                        '5m': [[t, 100, 102, 99, 101, 10] for t in range(0, 600, 300)]}}


def test_valid_bundle(bundle):
    assert offline.validate_bundle(bundle) is bundle


@pytest.mark.parametrize('defect', ['gap', 'bounds', 'volume', 'metadata', 'credentials', 'milliseconds'])
def test_reject_invalid_bundle(bundle, defect):
    value = copy.deepcopy(bundle)
    if defect == 'gap':
        value['candles']['1m'].pop()
    elif defect == 'bounds':
        value['candles']['1m'][0][2] = 90
    elif defect == 'volume':
        value['candles']['5m'][0][5] = 99
    elif defect == 'metadata':
        del value['market']['limits']
    elif defect == 'credentials':
        value['token'] = 'not-a-real-token'
    else:
        value['candles']['1m'][1][0] *= 1000
    with pytest.raises(ValueError):
        offline.validate_bundle(value)


def test_network_guard_denies_and_restores_without_request():
    original = socket.create_connection
    sock = socket.socket()
    try:
        with offline.deny_network():
            for call in (lambda: socket.create_connection(('example.com', 443)),
                         lambda: socket.getaddrinfo('example.com', 443),
                         lambda: sock.connect(('127.0.0.1', 1)),
                         lambda: sock.connect_ex(('127.0.0.1', 1))):
                with pytest.raises(RuntimeError, match='OFFLINE_NETWORK_DENIED'):
                    call()
        assert socket.create_connection is original
    finally:
        sock.close()


def test_complete_colab_matches_worker_and_validation_sources():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    nb = json.loads((root/'notebooks/freqtrade_strategy001_offline_colab.ipynb').read_text())
    codes = [''.join(c['source']) for c in nb['cells'] if c['cell_type'] == 'code']
    assert len(codes) == 1
    code = codes[0]
    assert code == (root/'notebooks/freqtrade_strategy001_offline_colab.py').read_text()
    compile(code, '<offline-colab>', 'exec')
    import ast
    tree = ast.parse(code)
    literals = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in ('HELPER_SOURCE', 'WORKER_SOURCE')}
    assert literals['HELPER_SOURCE'] == (root/'src/trade_mcp/freqtrade_offline.py').read_text()
    assert literals['WORKER_SOURCE'] == (root/'scripts/freqtrade_offline_worker.py').read_text()
    assert "ft('download-data'" not in code
    assert code.index("validation['validate_bundle'](json.loads(raw))") < code.index('FREQTRADE_VERSION=')
    worker = literals['WORKER_SOURCE']
    assert 'Backtesting(cfg, exchange=exchange)' in worker
    assert 'engine.start()' in worker
    assert 'with deny_network():' in worker
