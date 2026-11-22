import importlib.util
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[1]/'scripts/export_xauusd_tick_sample.py'
    spec = importlib.util.spec_from_file_location('tick_sample', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_tool_name_never_leaks_into_arguments_and_failure_not_retried():
    m = module()
    args = m.request_arguments('XAUUSD_i', '2026-11-20T19:23:00', '2026-11-20T19:28:00',
                               10000, 'broker_wall_clock')
    calls = []

    class Client:
        def list_tools(self):
            return [{'name': m.TOOL, 'inputSchema': {'properties': {k: {} for k in args},
                     'required': ['symbol', 'datetime_from', 'datetime_to'], 'additionalProperties': False}}]

        def call_tool(self, name, arguments):
            calls.append((name, arguments))
            raise RuntimeError('synthetic failure')

    with pytest.raises(RuntimeError):
        m.fetch_once(Client(), args)
    assert len(calls) == 1
    assert calls[0][0] == 'get_chart_ticks_history'
    assert set(calls[0][1]) == {'symbol', 'datetime_from', 'datetime_to', 'limit'}


def test_time_basis_and_bounds_are_explicit():
    m = module()
    for start, end, basis in [('2026-11-20T19:23:00Z', '2026-11-20T19:28:00Z', 'broker_wall_clock'),
                              ('2026-11-20T19:23:00', '2026-11-20T19:28:00', 'UTC'),
                              ('2026-11-20T19:23:00Z', '2026-11-20T19:29:00Z', 'UTC')]:
        with pytest.raises(ValueError):
            m.request_arguments('XAUUSD_i', start, end, 10000, basis)


def test_received_prices_do_not_imply_verified_clock_or_coverage():
    m = module()
    r = m.summarize({'structuredContent': {'ticks': [{'bid': 100, 'ask': 101, 'time': 1}]}},
                    'broker_wall_clock', 10000)
    assert r['valid_two_sided_rows'] == 1
    assert r['response_time_semantics_verified'] is False
    assert r['complete_tick_coverage_verified'] is False
    assert r['backtest_executed'] is False
