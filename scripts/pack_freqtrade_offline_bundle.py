"""Package EXISTING legally obtained real local data. Never downloads or fabricates.

markets.json must contain an actual CCXT BTC/USDT market object (or markets map).
Candle JSON files must contain UTC second OHLCV arrays, not CCXT milliseconds.
Source/capture/version fields are declarations, not independent authentication.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from trade_mcp.freqtrade_offline import validate_bundle  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--market', required=True, type=Path)
    p.add_argument('--m1', required=True, type=Path)
    p.add_argument('--m5', required=True, type=Path)
    p.add_argument('--source', required=True, help='Actual lawful data source description, no credentials')
    p.add_argument('--captured-at', required=True, help='Real snapshot capture time, ISO UTC')
    p.add_argument('--ccxt-version', required=True)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    market = json.loads(a.market.read_text())
    if 'BTC/USDT' in market:
        market = market['BTC/USDT']
    value = {'schema': 'freqtrade-offline-bundle/v1', 'market': market,
             'provenance': {'exchange': 'binance', 'source': a.source,
                            'captured_at': a.captured_at, 'ccxt_version': a.ccxt_version},
             'candles': {'1m': json.loads(a.m1.read_text()), '5m': json.loads(a.m5.read_text())}}
    validate_bundle(value)
    a.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, allow_nan=False)
    print('Validated local research bundle written; no exchange request made.')


if __name__ == '__main__':
    main()
