"""Optional report/backtest bridge. Never imports an order executor."""
from __future__ import annotations

import json
import os
import subprocess
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
