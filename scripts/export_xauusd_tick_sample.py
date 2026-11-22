"""One explicit bounded read-only tick-history request; no retry or trading.

Tool name is separate from arguments. Dates use the explicitly declared basis;
unknown broker labels are never renamed UTC. Raw response stays owner-only.
"""
import argparse
import contextlib
import io
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from trade_mcp.bridge.native import records  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.mcpclient import McpHttpClient  # noqa: E402

TOOL = 'get_chart_ticks_history'


def request_arguments(symbol, start, end, limit, basis):
    a, b = (datetime.fromisoformat(t.replace('Z', '+00:00')) for t in (start, end))
    if basis not in ('UTC', 'broker_wall_clock'):
        raise ValueError('Explicit time basis required')
    aware = a.utcoffset() is not None and b.utcoffset() is not None
    if basis == 'UTC' and (not aware or a.utcoffset().total_seconds() or b.utcoffset().total_seconds()):
        raise ValueError('UTC bounds must have explicit zero offsets')
    if basis == 'broker_wall_clock' and (a.tzinfo is not None or b.tzinfo is not None):
        raise ValueError('Broker labels must not falsely carry a UTC suffix')
    if not 0 < (b-a).total_seconds() <= 300 or type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError('Maximum five minutes / 10000 rows')
    if not symbol or len(symbol) > 64 or not all(c.isalnum() or c in '._-' for c in symbol):
        raise ValueError('Invalid symbol')
    return {'symbol': symbol, 'datetime_from': start, 'datetime_to': end, 'limit': limit}


def fetch_once(client, arguments):
    tools = client.list_tools()
    matches = [t for t in tools if t.get('name') == TOOL]
    if len(matches) != 1:
        raise ValueError('Verified historical tool not advertised')
    schema = matches[0].get('inputSchema', {})
    properties = schema.get('properties', {})
    if not set(arguments) <= set(properties) or not set(schema.get('required', [])) <= set(arguments):
        raise ValueError('Current tool schema does not match bounded arguments')
    if schema.get('additionalProperties') is not False:
        raise ValueError('Unexpected tool schema; inspect locally before use')
    # Never put "tool" or "name" inside arguments, and never resubmit on error.
    return client.call_tool(TOOL, arguments)


def private_json(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, allow_nan=False)


def summarize(result, basis, limit):
    data = McpHttpClient.structured_content(result)
    rows = records(data, 'ticks', 'quotes', 'history', 'data')
    valid = 0
    timestamped = 0
    for row in rows:
        try:
            bid, ask = row['bid'], row['ask']
            if isinstance(bid, bool) or isinstance(ask, bool):
                continue
            bid, ask = float(bid), float(ask)
            if math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask:
                valid += 1
                timestamped += any(row.get(k) is not None for k in ('time', 'time_msc', 'datetime', 'timestamp'))
        except (KeyError, ValueError, TypeError, OverflowError):
            pass
    return {'schema': 'xauusd-historical-tick-sample/v1',
            'status': 'tool_error' if result.get('isError') else 'response_received',
            'returned_rows': len(rows), 'valid_two_sided_rows': valid,
            'rows_with_time_field': timestamped, 'at_limit_may_be_truncated': len(rows) >= limit,
            'request_time_basis': basis, 'response_time_semantics_verified': False,
            'complete_tick_coverage_verified': False, 'backtest_executed': False,
            'execution_authorized': False, 'tool_call_count': 1}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('output_dir', type=Path)
    p.add_argument('--symbol', required=True)
    p.add_argument('--from', dest='start', required=True)
    p.add_argument('--to', dest='end', required=True)
    p.add_argument('--time-basis', choices=['UTC', 'broker_wall_clock'], required=True)
    p.add_argument('--limit', type=int, default=10000)
    args = p.parse_args()
    arguments = request_arguments(args.symbol, args.start, args.end, args.limit, args.time_basis)
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            load_dotenv()
            cfg = Config.from_env()
            if cfg.backend != 'native':
                raise ValueError('Native backend required')
            client = McpHttpClient(cfg.mcp_url, cfg.mcp_token, timeout=min(30, cfg.timeout))
            client.connect()
            result = fetch_once(client, arguments)
            private_json(args.output_dir/'raw-response.json', result)
            report = summarize(result, args.time_basis, args.limit)
    except Exception:
        report = {'schema': 'xauusd-historical-tick-sample/v1', 'status': 'request_or_validation_failed',
                  'execution_authorized': False, 'backtest_executed': False,
                  'raw_error_withheld': True, 'no_retry_performed': True}
    private_json(args.output_dir/'summary.json', report)
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'response_received' else 1


if __name__ == '__main__':
    raise SystemExit(main())
