"""Optional report/backtest bridge. Never imports an order executor."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from pathlib import Path


def research_status(cfg):
    path = cfg.research_python
    ready = bool(path and Path(path).is_absolute() and Path(path).is_file() and os.access(path, os.X_OK))
    return {'ok': True, 'configured': ready,
            'scope': 'TradingAgents tool-free analyst and backtesting.py; report-only',
            'dependencies_verified': False,
            'message': 'Separate Python 3.11+ environment required. No automatic installation or generation.'}


def run_worker(cfg, payload):
    if not research_status(cfg)['configured']:
        return {'ok': False, 'error': 'research_not_configured'}
    script = Path(__file__).with_name('research_worker.py')
    # Allowlist only basic OS environment. Never forward API, MT5 or OpenCode
    # credentials. The worker uses its own venv; no live-project PYTHONPATH.
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT') if k in os.environ}
    env['PYTHONNOUSERSITE'] = '1'
    try:
        result = subprocess.run([cfg.research_python, str(script)],
            input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=30,
            check=False, cwd=str(script.parent))
        if result.returncode or len(result.stdout) > 200000:
            return {'ok': False, 'error': 'research_worker_failed'}
        output = json.loads(result.stdout)
        if not isinstance(output, dict) or not isinstance(output.get('ok'), bool):
            raise ValueError('Invalid worker output')
        return output
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'research_worker_timeout'}
    except (OSError, ValueError):
        return {'ok': False, 'error': 'research_worker_failed'}


def validated_messages(result):
    messages = result.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 12:
        raise ValueError('Invalid research messages')
    if any(not isinstance(m, dict) or m.get('role') not in {'system','user'}
           or not isinstance(m.get('content'), str) for m in messages):
        raise ValueError('Invalid research message')
    if sum(len(m['content']) for m in messages) > 100000:
        raise ValueError('Research prompt too large')
    return [{'role':m['role'], 'content':m['content']} for m in messages]


def snapshot_record(bars):
    """Canonical reproducible input; reject malformed OHLC rather than repair it."""
    if not isinstance(bars, list) or not 60 <= len(bars) <= 499:
        raise ValueError('Invalid snapshot size')
    clean = []
    for bar in bars:
        row = {}
        for key in ('open', 'high', 'low', 'close'):
            value = bar.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid price')
            row[key] = float(value)
        if row['low'] > min(row['open'], row['close']) or row['high'] < max(row['open'], row['close']):
            raise ValueError('Invalid OHLC range')
        # Preserve source bar time when supplied, without inventing dates.
        value = bar.get('time')
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            row['time'] = value
        clean.append(row)
    times = [bar.get('time') for bar in clean]
    supplied = [value for value in times if value is not None]
    if supplied and (len(supplied) != len(clean) or any(b <= a for a, b in zip(supplied, supplied[1:], strict=False))):
        raise ValueError('Missing, duplicate or unordered candle times')
    encoded = json.dumps(clean, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return {'sha256': hashlib.sha256(encoded).hexdigest(), 'bars': clean,
            'count': len(clean), 'first_time': clean[0].get('time'), 'last_time': clean[-1].get('time'),
            'time_check': {'reference':'host_clock', 'checked_at':time.time(),
                'status':'unknown' if not supplied else 'future' if supplied[-1] > time.time()+300 else 'not_future',
                'warning':'Historical input; this is not a freshness or clock-synchronization guarantee.'}}
