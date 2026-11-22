"""Offline synthetic coverage of the completed one-shot v2 screen.

The fake client cannot contact MT5, place orders, or access provider services.
These tests are not evidence about the historical responses or profitability.
"""
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trade_mcp.mcpclient import McpHttpClient


@pytest.fixture
def job():
    path = Path(__file__).resolve().parents[1] / 'scripts/xauusd_sparse_screen_v2.py'
    spec = importlib.util.spec_from_file_location('sparse_screen_v2_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def economics():
    return {'trade_contract_size': 100, 'currency_profit': 'USD',
            'trade_calc_mode': 'cfd leverage', 'volume_min': .01,
            'volume_max': 10, 'volume_step': .01}


def empty_payload(**overrides):
    payload = {'symbol': 'XAUUSD_i', 'period': 'tick', 'ok': False, 'history': [],
               'data_available_from': '2026-11-01T00:00:00'}
    payload.update(overrides)
    return payload


def loss_response(start):
    """Artificial rising quotes and a wide spread generate three simulated losses."""
    first = datetime.fromisoformat(start)
    rows = [{'time_ms': (first + timedelta(seconds=i)).isoformat(),
             'bid': 100 + i * .01, 'ask': 102 + i * .01} for i in range(300)]
    return {'structuredContent': {'symbol': 'XAUUSD_i', 'period': 'tick',
                                  'ok': True, 'history': rows}}


def window(job):
    return job.WINDOWS[0][1:]


@pytest.mark.parametrize('diagnostic', [None, 'no data', 'No data available', 'no_history'])
def test_exact_no_data_continues_without_inventing_cause(job, diagnostic):
    payload = empty_payload()
    if diagnostic is not None:
        payload['message'] = diagnostic
    result = {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(payload)}]}
    assert job.validate_rows(result, *window(job)) == {'kind': 'no_usable_data_cause_unknown'}


@pytest.mark.parametrize('extra', [
    {'error': 'provider problem'},
    {'errors': [{'code': 123, 'message': 'provider problem'}]},
    {'meta': {'error_code': 25}},
    {'error_message': 'provider problem'},
    {'meta': {'provider_error': 'failure'}},
    {'status': 'failed'},
    {'status': 429},
    {'statusCode': '503'},
    {'message': 'rate limit reached'},
    {'meta': {'reason': 'permission denied'}},
])
def test_explicit_service_error_never_continues_as_empty_data(job, extra):
    with pytest.raises(job.StopScreen, match='explicit_provider_error'):
        job.validate_rows({'structuredContent': empty_payload(**extra)}, *window(job))


def test_mcp_error_flag_stops_before_parsing_even_bad_content(job):
    with pytest.raises(job.StopScreen, match='explicit_provider_error'):
        job.validate_rows({'isError': True, 'content': 7}, *window(job))


def test_explicit_error_overrides_even_nonempty_success_history(job):
    result = loss_response(window(job)[0])
    result['structuredContent']['metadata'] = {'error_message': 'provider problem'}
    with pytest.raises(job.StopScreen, match='explicit_provider_error'):
        job.validate_rows(result, *window(job))


def test_unknown_provider_diagnostic_is_not_assumed_empty_history(job):
    result = {'structuredContent': empty_payload(reason='temporarily unknown')}
    with pytest.raises(job.StopScreen, match='ambiguous_provider_diagnostic'):
        job.validate_rows(result, *window(job))


@pytest.mark.parametrize('change', [
    'missing_history', 'null_history', 'wrong_symbol', 'wrong_period', 'missing_ok',
    'wrong_ok_type', 'alternate_rows', 'ok_false_with_rows', 'empty_success',
    'mixed_rows', 'out_of_order', 'outside_window', 'aware_timestamp', 'invalid_quote',
])
def test_malformed_or_ambiguous_data_never_becomes_no_data(job, change):
    result = loss_response(window(job)[0])
    data = result['structuredContent']
    rows = data['history']
    if change == 'missing_history':
        del data['history']
        data['ok'] = False
    elif change == 'null_history':
        data['history'], data['ok'] = None, False
    elif change == 'wrong_symbol':
        data['symbol'], data['ok'], data['history'] = 'OTHER', False, []
    elif change == 'wrong_period':
        data['period'], data['ok'], data['history'] = 'M1', False, []
    elif change == 'missing_ok':
        del data['ok']
    elif change == 'wrong_ok_type':
        data['ok'] = 'false'
    elif change == 'alternate_rows':
        data['history'], data['ok'], data['ticks'] = [], False, rows
    elif change == 'ok_false_with_rows':
        data['ok'] = False
    elif change == 'empty_success':
        data['history'] = []
    elif change == 'mixed_rows':
        rows[4] = 'not an object'
    elif change == 'out_of_order':
        rows[5], rows[6] = rows[6], rows[5]
    elif change == 'outside_window':
        rows[1]['time_ms'] = '2026-11-11T09:00:00'
    elif change == 'aware_timestamp':
        rows[1]['time_ms'] += '+00:00'
    elif change == 'invalid_quote':
        rows[1]['ask'] = 0
    with pytest.raises(job.StopScreen, match='malformed_response'):
        job.validate_rows(result, *window(job))


def test_actual_array_length_enforces_cap_before_filtering_bad_rows(job):
    result = loss_response(window(job)[0])
    data = result['structuredContent']
    data['history'] = (data['history'] * 7)[:job.LIMIT]
    data['history'][0] = 'bad row that must not be silently dropped'
    with pytest.raises(job.StopScreen, match='at_or_above_limit_possible_truncation'):
        job.validate_rows(result, *window(job))


@pytest.fixture
def fake_job(job, tmp_path, monkeypatch):
    """Exercise run() with only local temp files and a deterministic fake MCP client."""
    calls, events = [], []
    output = tmp_path / 'screen'
    old_screen = tmp_path / 'v1'
    old_screen.mkdir()
    (old_screen / 'marker').write_text('do not touch')
    saved_economics = tmp_path / 'economics.json'
    saved_economics.write_text(json.dumps(economics()))

    class FakeClient:
        structured_content = staticmethod(McpHttpClient.structured_content)

        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            return self

        def list_tools(self):
            return [{'name': 'get_chart_ticks_history', 'inputSchema': {
                'properties': {key: {} for key in ('symbol', 'datetime_from', 'datetime_to', 'limit')},
                'required': ['symbol', 'datetime_from', 'datetime_to'],
                'additionalProperties': False}}]

        def call_tool(self, name, arguments):
            calls.append((name, dict(arguments)))
            event = events[len(calls) - 1]
            if isinstance(event, Exception):
                raise event
            if event == 'empty':
                return {'structuredContent': empty_payload()}
            if event == 'loss':
                return loss_response(arguments['datetime_from'])
            return event

    class FakeConfig:
        @staticmethod
        def from_env():
            return SimpleNamespace(backend='native', mcp_url='http://127.0.0.1:22346/mcp',
                                   mcp_token='', timeout=1)

    monkeypatch.setattr(job, 'OUTPUT', output)
    monkeypatch.setattr(job, 'V1_OUTPUT', old_screen)
    monkeypatch.setattr(job, 'ECONOMICS', saved_economics)
    monkeypatch.setattr(job, 'McpHttpClient', FakeClient)
    monkeypatch.setattr(job, 'Config', FakeConfig)
    monkeypatch.setattr(job, 'load_dotenv', lambda: None)
    # Only the fake client is used when the already-completed flow is exercised.
    monkeypatch.setattr(job, 'V2_RUN_CLOSED', False)
    return SimpleNamespace(job=job, events=events, calls=calls, output=output, old_screen=old_screen)


def test_empty_and_losing_windows_continue_once_each_without_real_latch(fake_job, capsys):
    h = fake_job
    h.events.extend(['empty', 'loss', 'loss', 'empty', 'loss'])
    assert h.job.run() == 0
    report = json.loads((h.output / 'summary.json').read_text())
    assert report['status'] == 'completed_5_windows'
    assert report['attempted_history_calls'] == len(h.calls) == 5
    assert report['stop_reason'] is None
    assert [w['status'] for w in report['completed_windows']] == [
        'no_usable_data_cause_unknown', 'simulated', 'simulated',
        'no_usable_data_cause_unknown', 'simulated']
    for item in report['completed_windows']:
        if item['status'] == 'simulated':
            scenario = item['scenario']
            assert scenario['counts']['cash_loss_exits'] == 3
            assert scenario['simulated_three_consecutive_loss_latch'] is True
            assert scenario['execution_authorized'] is False and scenario['net_pnl'] is None
    assert [(call[1]['datetime_from'], call[1]['datetime_to']) for call in h.calls] == [
        (start, end) for _, start, end in h.job.WINDOWS]
    assert all(name == 'get_chart_ticks_history' and args['limit'] == 2000
               for name, args in h.calls)
    assert (h.output / 'summary.json').stat().st_mode & 0o777 == 0o600
    assert (h.old_screen / 'marker').read_text() == 'do not touch'
    with pytest.raises(FileExistsError):
        h.job.run()
    assert len(h.calls) == 5  # Fixed output marker: no second attempt.
    assert 'do not touch' not in capsys.readouterr().out


@pytest.mark.parametrize(('event', 'reason'), [
    ({'structuredContent': empty_payload(message='rate limit reached PRIVATE-DETAIL')},
     'explicit_provider_error'),
    ({'structuredContent': {'symbol': 'XAUUSD_i', 'period': 'tick', 'ok': False}},
     'malformed_response'),
    (RuntimeError('private network connection detail PRIVATE-DETAIL'),
     'transport_or_auth_or_rate_limit_error'),
])
def test_real_errors_stop_partial_run_and_do_not_leak_raw_details(fake_job, capsys, event, reason):
    h = fake_job
    h.events.extend(['loss', event, 'loss', 'loss', 'loss'])
    assert h.job.run() == 1
    report = json.loads((h.output / 'summary.json').read_text())
    assert report['status'] == 'stopped_' + reason
    assert report['stop_reason'] == reason
    assert report['stopped_window'] == 'w2'
    assert report['attempted_history_calls'] == len(h.calls) == 2
    assert len(report['completed_windows']) == 1
    assert 'PRIVATE-DETAIL' not in capsys.readouterr().out
    assert 'PRIVATE-DETAIL' not in json.dumps(report)


def test_schema_mismatch_stops_before_any_history_call(fake_job, monkeypatch):
    h = fake_job
    monkeypatch.setattr(h.job.McpHttpClient, 'list_tools', lambda self: [])
    assert h.job.run() == 1
    report = json.loads((h.output / 'summary.json').read_text())
    assert report['status'] == 'stopped_history_tool_missing_or_ambiguous'
    assert report['attempted_history_calls'] == len(h.calls) == 0


def test_invalid_economics_stops_before_connect(fake_job, monkeypatch):
    h = fake_job
    h.job.ECONOMICS.write_text('{}')
    monkeypatch.setattr(h.job, 'load_dotenv', lambda: pytest.fail('must stop before settings or network'))
    assert h.job.run() == 1
    report = json.loads((h.output / 'summary.json').read_text())
    assert report['status'] == 'stopped_input_transport_or_replay_error'
    assert report['attempted_history_calls'] == len(h.calls) == 0


def test_already_completed_v2_is_closed_even_without_local_marker(job, tmp_path, monkeypatch):
    output = tmp_path / 'no-previous-marker'
    monkeypatch.setattr(job, 'OUTPUT', output)
    monkeypatch.setattr(job, 'load_dotenv', lambda: pytest.fail('must not read settings'))
    monkeypatch.setattr(job, 'McpHttpClient', lambda *a, **kw: pytest.fail('must not connect'))
    assert job.V2_RUN_CLOSED is True
    assert job.run() == 1
    assert not output.exists()
