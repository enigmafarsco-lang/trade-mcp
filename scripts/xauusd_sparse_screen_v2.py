"""New bounded 5-window XAUUSD sparse screen. No rerun/modify of v1.

At most ONE read-only tick-history request per window, sequentially, 2000 rows max.
No retries, no alternate provider, no pagination. Fixed output dir is a no-repeat marker.
The five-window run is complete: this historical job is closed and must not run again.
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

from trade_mcp.config import Config, load_dotenv  # noqa: E402
from trade_mcp.mcpclient import McpHttpClient  # noqa: E402
from trade_mcp.tick_cash_replay import economics, replay_cash_scenario  # noqa: E402
from trade_mcp.tick_replay import replay_signals  # noqa: E402

SYMBOL = 'XAUUSD_i'
LIMIT = 2000
WINDOWS = [
    ('w1', '2026-11-09T13:00:00', '2026-11-09T13:05:00'),
    ('w2', '2026-11-09T19:00:00', '2026-11-09T19:05:00'),
    ('w3', '2026-11-10T09:00:00', '2026-11-10T09:05:00'),
    ('w4', '2026-11-10T13:00:00', '2026-11-10T13:05:00'),
    ('w5', '2026-11-10T19:00:00', '2026-11-10T19:05:00'),
]
ECONOMICS = Path('logs/research-inputs/xauusd-cash-research-20261122T075312Z/symbol-economics.json')
OUTPUT = Path('logs/research-inputs/xauusd-sparse-screen-20261109-10-v2-5win')
V1_OUTPUT = Path('logs/research-inputs/xauusd-sparse-screen-20261109-10-v1')
# This experiment already ran once; keep the guard even in a fresh checkout
# that lacks the original machine's local output-directory marker.
V2_RUN_CLOSED = True
FIELDS = {'trade_contract_size', 'currency_profit', 'trade_calc_mode',
          'volume_min', 'volume_max', 'volume_step'}
STOP_STATUSES = {
    'transport_or_auth_or_rate_limit_error',
    'malformed_response',
    'explicit_provider_error',
    'ambiguous_provider_diagnostic',
    'at_or_above_limit_possible_truncation',
    'history_schema_mismatch',
    'history_tool_missing_or_ambiguous',
    'input_transport_or_replay_error',
}


class StopScreen(Exception):
    """Safe fixed reason only."""


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, indent=2, allow_nan=False)


def save_progress(directory, report):
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


def has_value(value):
    """Empty diagnostic values are not evidence of an explicit error."""
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and value == 0:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list)):
        return bool(value)
    return True


def metadata_fields(data):
    """Inspect bounded metadata, never quote rows or private error text."""
    pending = [data]
    visited = 0
    while pending:
        item = pending.pop()
        visited += 1
        if visited > 1000:
            raise StopScreen('malformed_response')
        if isinstance(item, dict):
            for key, value in item.items():
                name = ''.join(c for c in str(key).lower() if c.isalnum())
                if name in {'history', 'ticks', 'quotes'}:
                    continue
                yield name, value
                if isinstance(value, (dict, list)):
                    pending.append(value)
        elif isinstance(item, list):
            pending.extend(value for value in item if isinstance(value, (dict, list)))


def is_explicit_service_error(fields, result):
    if result.get('isError'):
        return True
    error_fields = {'retcode', 'fault', 'faultcode', 'faultstring', 'failure'}
    error_statuses = {'error', 'failed', 'failure', 'unauthorized', 'forbidden',
                      'ratelimited', 'timeout', 'unavailable'}
    service_hints = ('rate limit', 'rate-limit', 'rate_limit', 'too many requests',
                     'quota', 'unauthorized', 'forbidden', 'permission denied',
                     'access denied', 'timeout', 'timed out', 'throttl',
                     'authentication failed', 'service unavailable', 'provider error')
    for key, value in fields:
        is_error_field = (key in error_fields or key.startswith(('error', 'lasterror', 'exception'))
                          or key.endswith('error'))
        if is_error_field and has_value(value):
            return True
        if key in {'status', 'message', 'reason', 'detail', 'description'} and isinstance(value, str):
            text = value.lower().strip()
            if key == 'status' and ''.join(c for c in text if c.isalnum()) in error_statuses:
                return True
            if any(hint in text for hint in service_hints):
                return True
        if key in {'status', 'statuscode', 'code'}:
            number = value if type(value) is int else int(value) if isinstance(value, str) and value.isdigit() else 0
            if 400 <= number <= 599:
                return True
    return False


def harmless_no_data_diagnostic(key, value):
    if not has_value(value):
        return True
    if key not in {'status', 'message', 'reason', 'detail', 'description'} or not isinstance(value, str):
        return False
    words = ' '.join(value.lower().replace('_', ' ').split())
    return words in {'no data', 'no data available', 'no history', 'empty history',
                     'history is empty', 'no ticks', 'no ticks available'}


def classify_exception(exc):
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    text = name + ' ' + msg
    if any(s in text for s in ['timeout', 'connection', 'transport', 'refused', 'reset', 'dns', 'socket', 'network']):
        return 'transport_or_auth_or_rate_limit_error'
    if any(s in text for s in ['401', '403', 'auth', 'unauthorized', 'forbidden', 'token']):
        return 'transport_or_auth_or_rate_limit_error'
    if any(s in text for s in ['429', 'rate limit', 'too many', 'throttl']):
        return 'transport_or_auth_or_rate_limit_error'
    if any(s in text for s in ['malformed', 'json', 'schema', 'decode', 'parse']):
        return 'malformed_response'
    return None


def validate_rows(result, start, end):
    if not isinstance(result, dict):
        raise StopScreen('malformed_response')
    if result.get('isError'):
        raise StopScreen('explicit_provider_error')
    try:
        data = McpHttpClient.structured_content(result)
        fields = list(metadata_fields(data))
    except (AttributeError, TypeError, ValueError):
        raise StopScreen('malformed_response') from None
    if is_explicit_service_error(fields, result):
        raise StopScreen('explicit_provider_error')
    # A missing history field is not proof of an empty array. Only the exact
    # observed empty-response shape may continue; ambiguous diagnostics stop.
    if (not isinstance(data, dict) or data.get('symbol') != SYMBOL
            or data.get('period') != 'tick' or type(data.get('ok')) is not bool
            or not isinstance(data.get('history'), list)):
        raise StopScreen('malformed_response')
    rows = data['history']  # Do not silently filter non-object rows or choose another array.
    if data['ok'] is False and rows == []:
        diagnostic_keys = {'status', 'statuscode', 'message', 'reason', 'code', 'detail', 'description'}
        if any(key in diagnostic_keys and not harmless_no_data_diagnostic(key, value)
               for key, value in fields):
            raise StopScreen('ambiguous_provider_diagnostic')
        if any(has_value(data.get(key)) for key in ('ticks', 'quotes', 'data')):
            raise StopScreen('malformed_response')
        return {'kind': 'no_usable_data_cause_unknown'}
    if data['ok'] is not True or not rows:
        raise StopScreen('malformed_response')
    if len(rows) >= LIMIT:
        raise StopScreen('at_or_above_limit_possible_truncation')
    if any(not isinstance(row, dict) for row in rows):
        raise StopScreen('malformed_response')
    a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
    try:
        for row in rows:
            stamp = datetime.fromisoformat(row['time_ms'])
            if stamp.tzinfo is not None or not a <= stamp <= b:
                raise ValueError('timestamp outside requested naive labels')
        replay_signals(rows)
    except (ValueError, KeyError, TypeError, OverflowError):
        raise StopScreen('malformed_response') from None
    return {'kind': 'rows', 'rows': rows}


def run():
    if V2_RUN_CLOSED:
        print('This v2 screen already ran; no history requests will be made.')
        return 1
    if V1_OUTPUT.resolve() == OUTPUT.resolve():
        raise SystemExit('refusing to use v1 output dir')
    OUTPUT.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {
        'schema': 'xauusd-sparse-screen-v2/v1',
        'status': 'started',
        'symbol': SYMBOL,
        'time_basis': 'broker_wall_clock_labels_utc_unverified',
        'request_limit': LIMIT,
        'max_history_calls': len(WINDOWS),
        'attempted_history_calls': 0,
        'completed_windows': [],
        'stopped_window': None,
        'stop_reason': None,
        'complete_tick_coverage_verified': False,
        'net_pnl': None,
        'execution_authorized': False,
        'profitability_proven': False,
        'window_independence': 'each window starts flat with a fresh hypothetical latch; not continuous',
        'limitations': [
            'Sparse five-minute windows, not complete multi-day backtest',
            'Initial position and latch state unknown, not a live guard replay',
            'Fixed current contract snapshot may differ historically',
            'Fill and cash-exit assumptions are not broker validated',
            'No verified commission, swap, slippage, account-risk or UTC',
        ],
    }
    save_progress(OUTPUT, report)
    try:
        raw = ECONOMICS.read_bytes()
        if not 0 < len(raw) <= 100000:
            raise StopScreen('input_transport_or_replay_error')
        info = json.loads(raw)
        if not isinstance(info, dict) or set(info) != FIELDS:
            raise StopScreen('input_transport_or_replay_error')
        economics(info)
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
                raise StopScreen('input_transport_or_replay_error')
            client = McpHttpClient(cfg.mcp_url, cfg.mcp_token, timeout=min(30, cfg.timeout))
            client.connect()
            validate_schema(client)
        for name, start, end in WINDOWS:
            report['attempted_history_calls'] += 1
            report['in_progress_window'] = name
            save_progress(OUTPUT, report)
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    args = {'symbol': SYMBOL, 'datetime_from': start, 'datetime_to': end, 'limit': LIMIT}
                    result = client.call_tool('get_chart_ticks_history', args)
            except Exception as exc:
                cls = classify_exception(exc)
                private_json(OUTPUT / f'{name}-error.json', {'status': cls or 'transport_or_auth_or_rate_limit_error'})
                report['stopped_window'] = name
                report['stop_reason'] = cls or 'transport_or_auth_or_rate_limit_error'
                report['status'] = 'stopped_' + report['stop_reason']
                report['in_progress_window'] = None
                save_progress(OUTPUT, report)
                print(json.dumps(report, indent=2))
                return 1
            private_json(OUTPUT / f'{name}-raw-response.json', result)
            try:
                outcome = validate_rows(result, start, end)
            except StopScreen as exc:
                report['stopped_window'] = name
                report['stop_reason'] = str(exc)
                report['status'] = 'stopped_' + str(exc)
                report['in_progress_window'] = None
                save_progress(OUTPUT, report)
                print(json.dumps(report, indent=2))
                return 1
            if outcome['kind'] == 'no_usable_data_cause_unknown':
                report['completed_windows'].append({
                    'id': name, 'start': start, 'end': end,
                    'status': 'no_usable_data_cause_unknown',
                    'returned_rows': 0,
                })
                report['in_progress_window'] = None
                save_progress(OUTPUT, report)
                continue
            rows = outcome['rows']
            try:
                scenario = replay_cash_scenario(rows, info)
            except Exception:
                report['stopped_window'] = name
                report['stop_reason'] = 'input_transport_or_replay_error'
                report['status'] = 'stopped_input_transport_or_replay_error'
                report['in_progress_window'] = None
                save_progress(OUTPUT, report)
                print(json.dumps(report, indent=2))
                return 1
            private_json(OUTPUT / f'{name}-scenario.json', scenario)
            win = {
                'id': name, 'start': start, 'end': end,
                'status': 'simulated',
                'returned_rows': len(rows),
                'scenario': scenario,
            }
            # If simulation loses money, record loss and CONTINUE (already recorded in scenario).
            report['completed_windows'].append(win)
            report['in_progress_window'] = None
            save_progress(OUTPUT, report)
        report['status'] = 'completed_all_windows_or_stopped_by_rule'
        if report['stop_reason'] is None:
            report['status'] = 'completed_5_windows'
    except StopScreen as exc:
        report['status'] = 'stopped_' + str(exc)
        report['stop_reason'] = str(exc)
    except Exception:
        report['status'] = 'stopped_input_transport_or_replay_error'
        report['stop_reason'] = 'input_transport_or_replay_error'
        report['raw_error_withheld'] = True
    report['in_progress_window'] = None
    save_progress(OUTPUT, report)
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'completed_5_windows' else 1


if __name__ == '__main__':
    raise SystemExit(run())