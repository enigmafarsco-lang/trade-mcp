"""Read-only MT5 tick-history capability check. No account, quotes or tool calls.

Only initialize + tools/list are sent. Never enables trading, installs packages,
restarts Wine or prints connection credentials/raw provider responses.
"""
import argparse
import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.mcpclient import McpHttpClient  # noqa: E402


def describe_tick_history(tools):
    candidates = []
    for tool in tools:
        name = tool.get('name', '')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_]{1,100}', name):
            continue
        lowered = name.lower()
        # A candidate is not evidence of historical bid/ask availability.
        if ('tick' not in lowered and 'quote' not in lowered) or not any(
                word in lowered for word in ('history', 'historical', 'range', 'copy', 'ticks')):
            continue
        if any(word in lowered for word in ('trade', 'order', 'send', 'delete', 'modify', 'write')):
            continue
        schema = tool.get('inputSchema', {})
        required = schema.get('required', [])
        props = schema.get('properties', {})
        fields = []
        for key, value in props.items():
            if not re.fullmatch(r'[A-Za-z0-9_]{1,80}', key) or not isinstance(value, dict):
                continue
            kind = value.get('type')
            if kind not in ('string', 'integer', 'number', 'boolean', 'object', 'array'):
                kind = 'unspecified'
            fields.append({'name': key, 'type': kind, 'required': key in required})
        candidates.append({'name': name, 'parameters': fields})
    return {'schema': 'xauusd-tick-history-capabilities/v1',
            'status': 'candidate_advertised' if candidates else 'no_tick_history_tool_advertised',
            'candidate_tools': candidates, 'historical_bid_ask_verified': False,
            'backtest_executed': False, 'execution_authorized': False,
            'calls_made': ['initialize', 'tools/list'],
            'warning': 'Tool names/schema are discovery only, not proof of tick data or broker acceptance.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Output exists; preserve it and choose a new filename')
    # Keep all library/transport text local and unprinted, including config failures.
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            load_dotenv()
            cfg = Config.from_env()
            if cfg.backend != 'native':
                raise ValueError('native_required')
            client = McpHttpClient(cfg.mcp_url, cfg.mcp_token, timeout=min(cfg.timeout, 30))
            client.connect()
            report = describe_tick_history(client.list_tools())
    except Exception:
        report = {'schema': 'xauusd-tick-history-capabilities/v1', 'status': 'discovery_failed',
                  'historical_bid_ask_verified': False, 'backtest_executed': False,
                  'execution_authorized': False, 'raw_error_withheld': True}
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    print(json.dumps(report, indent=2))
    return 1 if report['status'] == 'discovery_failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
