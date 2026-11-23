"""Explicit isolated-cloud worker; requires already provisioned upstream packages.

Never installs dependencies, downloads weights, activates budget or sends orders.
Output contains market data; keep it local/owner-only, never commit raw output.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from trade_mcp.engines.upstream import (  # noqa: E402
    KRONOS_COMMIT,
    evaluate_kronos,
    freqtrade_strategy001,
    kronos_predict,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=['kronos', 'freqtrade'])
    parser.add_argument('input', type=Path, help='JSON {rows: closed OHLCV arrays}')
    parser.add_argument('output', type=Path)
    parser.add_argument('--kronos-repo', type=Path)
    parser.add_argument('--tokenizer', type=Path)
    parser.add_argument('--checkpoint', type=Path)
    parser.add_argument('--strategy-source', type=Path)
    args = parser.parse_args()
    if args.input.stat().st_size > 4_000_000:
        raise SystemExit('Input too large')
    request = json.loads(args.input.read_text())
    rows = request['rows']
    period = 60 if args.kind == 'kronos' else 300
    if not rows or any(float(r[0])+period > time.time() for r in rows):
        raise SystemExit('Only closed historical candles accepted')
    if args.kind == 'freqtrade':
        if not args.strategy_source:
            raise SystemExit('Explicit verified Strategy001 source required')
        result = freqtrade_strategy001(rows, args.strategy_source)
    else:
        if not all(p and p.is_dir() for p in (args.kronos_repo, args.tokenizer, args.checkpoint)):
            raise SystemExit('Pre-provisioned cloud repository and local checkpoint directories required')
        head = subprocess.check_output(['git', '-C', str(args.kronos_repo), 'rev-parse', 'HEAD'], text=True).strip()
        dirty = subprocess.check_output(['git', '-C', str(args.kronos_repo), 'status', '--porcelain'], text=True).strip()
        if head != KRONOS_COMMIT or dirty:
            raise SystemExit('Pinned clean Kronos checkout required')
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        sys.path.insert(0, str(args.kronos_repo.resolve()))
        from model import Kronos, KronosPredictor, KronosTokenizer
        predictor = KronosPredictor(Kronos.from_pretrained(str(args.checkpoint.resolve())),
                                    KronosTokenizer.from_pretrained(str(args.tokenizer.resolve())))
        evaluation = evaluate_kronos(rows, predictor)
        result = {**kronos_predict(rows, predictor), 'holdout_evaluation': evaluation,
                  'symbol': request['symbol'], 'timeframe': 'M1',
                  'forecast_origin': rows[-1][0]+60}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(result, stream, allow_nan=False)


if __name__ == '__main__':
    main()
