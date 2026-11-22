"""One-shot bounded, pre-registered sparse historical screen; NEVER trades.

Six fixed five-minute history requests at most. The fixed output directory acts
as a durable no-repeat marker even after interruption. No automatic retries.
"""
import contextlib
import hashlib
import io
import ipaddress
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from trade_mcp.bridge.native import records  # noqa: E402
from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.mcpclient import McpHttpClient  # noqa: E402
from trade_mcp.tick_cash_replay import economics, replay_cash_scenario  # noqa: E402
from trade_mcp.tick_replay import replay_signals  # noqa: E402

SYMBOL = 'XAUUSD_i'
LIMIT = 2000
WINDOWS = [
    ('eval-1', '2026-11-09T09:00:00', '2026-11-09T09:05:00'),
    ('eval-2', '2026-11-09T13:00:00', '2026-11-09T13:05:00'),
    ('eval-3', '2026-11-09T19:00:00', '2026-11-09T19:05:00'),
    ('final-1', '2026-11-10T09:00:00', '2026-11-10T09:05:00'),
    ('final-2', '2026-11-10T13:00:00', '2026-11-10T13:05:00'),
    ('final-3', '2026-11-10T19:00:00', '2026-11-10T19:05:00'),
]
ECONOMICS = Path('logs/research-inputs/xauusd-cash-research-20261122T075312Z/symbol-economics.json')
OUTPUT = Path('logs/research-inputs/xauusd-sparse-screen-20261109-10-v1')
FIELDS = {'trade_contract_size', 'currency_profit', 'trade_calc_mode',
          'volume_min', 'volume_max', 'volume_step'}


class StopScreen(Exception):
    """Safe, fixed reason only, not a raw provider error."""


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, indent=2, allow_nan=False)


def save_progress(directory, report):
    """Replace progress atomically with owner-only permissions."""
    temp = directory / '.summary.tmp'
    private_json(temp, report)
    os.replace(temp, directory / 'summary.json')


def validate_schema(client):
    tools = [t for t in client.list_tools() if t.get('name') == 'get_chart_ticks_history']
    if len(tools) != 1:
        raise StopScreen('history_tool_missing_or_ambiguous')
    schema = tools[0].get('inputSchema', {})
    expected = {'symbol', 'datetime_from', 'datetime_to', 'limit'}
    if (not expected <= set(schema.get('properties', {}))
            or not set(schema.get('required', [])) <= expected
            or schema.get('additionalProperties') is not False):
        raise StopScreen('history_schema_mismatch')


def validate_rows(result, start, end):
    if not isinstance(result, dict) or result.get('isError'):
        raise StopScreen('history_provider_error')
    data = McpHttpClient.structured_content(result)
    if isinstance(data, dict) and (data.get('ok') is False or data.get('error')):
        raise StopScreen('history_provider_error')
    rows = records(data, 'ticks', 'quotes', 'history', 'data')
    if not rows:
        raise StopScreen('empty_or_unrecognized_history')
    if len(rows) >= LIMIT:
        raise StopScreen('at_or_above_limit_possible_truncation')
    a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
    try:
        for row in rows:
            stamp = datetime.fromisoformat(row['time_ms'])
            if stamp.tzinfo is not None or not a <= stamp <= b:
                raise ValueError('timestamp outside requested naive labels')
        replay_signals(rows)
    except (ValueError, KeyError, TypeError, OverflowError):
        raise StopScreen('invalid_unordered_out_of_bounds_or_insufficient_rows') from None
    return rows


def run_windows(client, info, out, report):
    validate_schema(client)
    for name, start, end in WINDOWS:
        report['attempted_history_calls'] += 1
        report['in_progress_window'] = name
        save_progress(out, report)  # durable attempt recorded BEFORE call
        args = {'symbol': SYMBOL, 'datetime_from': start, 'datetime_to': end, 'limit': LIMIT}
        result = client.call_tool('get_chart_ticks_history', args)
        private_json(out / (name + '-raw-response.json'), result)
        rows = validate_rows(result, start, end)
        scenario = replay_cash_scenario(rows, info)
        private_json(out / (name + '-scenario.json'), scenario)
        report['windows'].append({'id': name, 'start': start, 'end': end,
                                  'returned_rows': len(rows), 'scenario': scenario})
        report['in_progress_window'] = None
        save_progress(out, report)
    report['status'] = 'all_six_sparse_windows_simulated'


def run():
    # A fixed one-shot directory blocks reruns after success, error or interruption.
    OUTPUT.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {'schema': 'xauusd-sparse-screen/v1', 'status': 'started',
              'symbol': SYMBOL, 'time_basis': 'broker_wall_clock_labels_utc_unverified',
              'request_limit': LIMIT, 'max_history_calls': len(WINDOWS),
              'attempted_history_calls': 0, 'in_progress_window': None, 'windows': [],
              'complete_tick_coverage_verified': False, 'net_pnl': None,
              'execution_authorized': False, 'profitability_proven': False,
              'window_independence': 'each window starts flat with a fresh hypothetical latch; not continuous',
              'limitations': ['Sparse five-minute windows, not complete multi-day backtest',
                              'Initial position and latch state unknown, not a live guard replay',
                              'Fixed current contract snapshot may differ historically',
                              'Fill and cash-exit assumptions are not broker validated',
                              'No verified commission, swap, slippage, account-risk or UTC']}
    save_progress(OUTPUT, report)
    try:
        raw = ECONOMICS.read_bytes()
        if not 0 < len(raw) <= 100000:
            raise StopScreen('invalid_saved_economics_file')
        info = json.loads(raw)
        if not isinstance(info, dict) or set(info) != FIELDS:
            raise StopScreen('invalid_saved_economics_file')
        economics(info)  # fail before any request
        report['economics_sha256'] = hashlib.sha256(raw).hexdigest()
        report['economics_symbol_binding_independently_verified'] = False
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            load_dotenv()
            cfg = Config.from_env()
            host = urlparse(cfg.mcp_url).hostname
            try:
                local = host == 'localhost' or ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = False
            if cfg.backend != 'native' or not local:
                raise StopScreen('native_loopback_endpoint_required')
            client = McpHttpClient(cfg.mcp_url, cfg.mcp_token, timeout=min(30, cfg.timeout))
            client.connect()
            run_windows(client, info, OUTPUT, report)
    except StopScreen as exc:
        report['status'] = 'stopped_' + str(exc)
    except Exception:
        # Avoid exposing credentials, provider payload, prices or local paths.
        report['status'] = 'stopped_input_transport_or_replay_error'
        report['raw_error_withheld'] = True
    save_progress(OUTPUT, report)
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'all_six_sparse_windows_simulated' else 1


if __name__ == '__main__':
    raise SystemExit(run())
