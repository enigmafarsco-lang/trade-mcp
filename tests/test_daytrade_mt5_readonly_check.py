"""The live probe's mechanics are exercised only against synthetic MT5 responses."""
from __future__ import annotations

import importlib.util
import json
import socket
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('daytrade_live_probe', ROOT / 'scripts/daytrade_mt5_readonly_check.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
REAL_CONNECT = socket.socket.connect
SYMBOL = {
    'name': 'XAUUSD_i', 'visible': True, 'bid': 2700.0, 'ask': 2700.1,
    'trade_tick_size': .01, 'trade_tick_value': 1, 'point': .01,
    'volume_min': .01, 'volume_max': 100, 'volume_step': .01, 'trade_stops_level': 0,
}
PROPS = {
    'terminal_get_info': [], 'get_time_information': [],
    'get_marketwatch_symbols': ['symbol', 'limit', 'include_hidden'],
    'get_chart_history': ['symbol', 'period', 'limit', 'datetime_from', 'datetime_to'],
}


def fake_bars(count=300, *, naive=False):
    result = []
    for i in range(count):
        dt = NOW - timedelta(minutes=count - 1 - i)
        stamp = dt.strftime('%Y-%m-%dT%H:%M:%S' if naive else '%Y-%m-%dT%H:%M:%SZ')
        price = 2600.0 + i * .01
        result.append({'time': stamp, 'open': price, 'high': price + 1, 'low': price - 1,
                       'close': price, 'tick_volume': 100})
    return result


class FakeClient:
    def __init__(self, *, bars=None, unchanged=False, application_error=False, missing_schema=None):
        self.bars = fake_bars() if bars is None else bars
        self.unchanged = unchanged
        self.application_error = application_error
        self.missing_schema = missing_schema
        self.calls = []

    def connect(self):
        return self

    def list_tools(self):
        return [{'name': name, 'inputSchema': {'type': 'object',
                    'properties': {p: {} for p in fields if p != self.missing_schema},
                    'required': fields}} for name, fields in PROPS.items()]

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == 'terminal_get_info':
            body = {'terminal': {'connected': True}}
        elif name == 'get_time_information':
            body = {'utc_time': NOW.isoformat(), 'trade_server_time': NOW.replace(tzinfo=None).isoformat()}
        elif name == 'get_marketwatch_symbols':
            body = {'symbols': [dict(SYMBOL)]}
            if len([n for n, _ in self.calls if n == name]) == 2 and not self.unchanged:
                body['symbols'][0].update(bid=2700.2, ask=2700.3)
        elif name == 'get_chart_history':
            body = {'ok': False, 'error': 'SECRET-RAW-ERROR'} if self.application_error else {'candles': self.bars}
        else:
            raise AssertionError('Forbidden native tool')
        return {'structuredContent': body, 'isError': False}


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setenv('TRADE_MCP_SKIP_DOTENV', '1')
    monkeypatch.setenv('TRADE_MCP_DEMO_CASH_POLICY', 'false')
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('no real network'))
    from trade_mcp import app
    assert app._APP is None


def run(tmp_path, client, *, use_http=False):
    runner = probe.Runner(tmp_path, 'a' * 40, 'XAUUSD_i')
    runner.discover_and_fixtures()
    cfg = probe.Config(backend='native', mcp_url='http://127.0.0.1:22346/mcp', mcp_token='secret-token')
    waits = []
    runner.live(cfg, lambda *a, **k: client, waits.append, lambda: NOW.timestamp())
    if use_http:
        runner.exercise_http()
    result = runner.finish()
    return result, waits


def test_all_live_adapters_run_on_mock_data_only_with_bounded_calls_and_private_logs(tmp_path):
    client = FakeClient()
    report, waits = run(tmp_path, client)
    assert waits == [5]
    assert [name for name, _ in client.calls] == [
        'terminal_get_info', 'get_time_information', 'get_marketwatch_symbols',
        'get_marketwatch_symbols', 'get_chart_history']
    assert client.calls[-1][1]['period'] == 'M1' and client.calls[-1][1]['limit'] == 1500
    assert set(client.calls[-1][1]) == set(PROPS['get_chart_history'])
    for check in ('daytrade_mcp_discovery', 'daytrade_mt5_terminal', 'daytrade_mt5_symbol',
                  'daytrade_mt5_quote', 'daytrade_mt5_quote_update', 'daytrade_mt5_m1',
                  'daytrade_mt5_m1_freshness', *probe.QUOTE_CHECKS, *probe.BAR_CHECKS,
                  *probe.FIXTURE_CHECKS):
        assert report['checks'][check] == 'pass', (check, report['reasons'].get(check))
    assert report['checks']['daytrade_mt5_session'] == 'blocked'
    assert report['checks']['daytrade_mt5_quote_freshness'] == 'blocked'
    assert report['counts']['daytrade_mt5_m1_bars'] == 300
    assert report['counts']['daytrade_mt5_segments'] == 20
    assert report['counts']['daytrade_mt5_bootstrap_paths'] == 20
    encoded = json.dumps(report)
    assert '2700' not in encoded and 'XAUUSD_i' not in encoded and 'secret-token' not in encoded
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in tmp_path.iterdir())
    assert json.loads((tmp_path / 'native-call-counts.json').read_text()) == probe.RAW_LIMITS
    # Results above are synthetic. The test is not evidence of an MT5 connection.


def test_real_mcp_http_transport_against_a_strict_synthetic_terminal(tmp_path, monkeypatch):
    fake = FakeClient()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):  # noqa: N802 - http.server protocol
            assert self.headers.get('Authorization') == 'Bearer synthetic-token'
            length = int(self.headers['Content-Length'])
            message = json.loads(self.rfile.read(length))
            method = message['method']
            if method == 'notifications/initialized':
                self.send_response(202)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if method == 'initialize':
                result = {'protocolVersion': '2025-06-18', 'serverInfo': {'name': 'mock-only'}}
            elif method == 'tools/list':
                result = {'tools': fake.list_tools()}
            elif method == 'tools/call':
                result = fake.call_tool(message['params']['name'], message['params']['arguments'])
            else:
                raise AssertionError('Unexpected method')
            body = json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Mcp-Session-Id', 'synthetic-session')
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    addr = ('127.0.0.1', httpd.server_port)

    def only_stub(sock, address):
        if address != addr:
            pytest.fail('unexpected network destination')
        return REAL_CONNECT(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', only_stub)
    try:
        runner = probe.Runner(tmp_path, 'a' * 40, 'XAUUSD_i')
        runner.discover_and_fixtures()
        cfg = probe.Config(backend='native', mcp_url=f'http://127.0.0.1:{httpd.server_port}/mcp',
                           mcp_token='synthetic-token')
        runner.live(cfg, probe.McpHttpClient, lambda _: None, lambda: NOW.timestamp())
        report = runner.finish()
        assert report['checks']['daytrade_mt5_terminal'] == 'pass'
        assert report['checks']['daytrade_mt5_m1'] == 'pass'
        assert report['checks']['daytrade_live_entry_buy'] == 'pass'
        assert len(fake.calls) == 5
        assert all(name in probe.RAW_LIMITS for name, _ in fake.calls)
        assert 'synthetic-token' not in json.dumps(report)
    finally:
        httpd.shutdown()
        thread.join(timeout=3)
        httpd.server_close()


def test_isolated_mock_console_consumes_broker_shaped_data_without_a_broker_or_journal(tmp_path, monkeypatch):
    # Only the driver's ephemeral loopback server is permitted by its own guard.
    monkeypatch.setattr(socket.socket, 'connect', REAL_CONNECT)
    report, _ = run(tmp_path, FakeClient(), use_http=True)
    assert report['checks']['daytrade_broker_input_http'] == 'pass'
    assert report['counts']['daytrade_http_cases'] >= 11
    assert report['checks'].get('daytrade_live_console_http', 'not_run') == 'not_run'
    assert not list(tmp_path.glob('*.sqlite3'))
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in tmp_path.iterdir())


def test_wide_spread_can_touch_hypothetical_stops_without_becoming_a_test_failure(tmp_path, monkeypatch):
    monkeypatch.setitem(SYMBOL, 'ask', 2750.0)
    report, _ = run(tmp_path, FakeClient(unchanged=True))
    assert report['checks']['daytrade_live_exit_plan'] == 'pass'
    assert report['checks']['daytrade_live_alert_snapshot'] == 'pass'
    buy = json.loads((tmp_path / 'advisory-live-alert-buy.json').read_text())['response']
    sell = json.loads((tmp_path / 'advisory-live-alert-sell.json').read_text())['response']
    assert buy['status'] == sell['status'] == 'stop_touched'
    assert buy['observed_exit_side'] == 'bid' and sell['observed_exit_side'] == 'ask'


def test_unchanged_quotes_and_naive_times_never_prove_freshness_or_market_session(tmp_path):
    report, _ = run(tmp_path, FakeClient(unchanged=True, bars=fake_bars(45, naive=True)))
    assert report['checks']['daytrade_mt5_quote'] == 'pass'
    assert report['checks']['daytrade_mt5_quote_update'] == 'blocked'
    assert report['reasons']['daytrade_mt5_quote_update'] == 'unchanged_quotes'
    assert report['checks']['daytrade_mt5_m1'] == 'pass'
    assert report['checks']['daytrade_mt5_m1_freshness'] == 'blocked'
    assert report['reasons']['daytrade_mt5_m1_freshness'] == 'timestamp_unverified'
    assert report['checks']['daytrade_live_time_machine'] == 'blocked'
    assert report['checks']['daytrade_mt5_session'] == 'blocked'


@pytest.mark.parametrize(('payload', 'expected', 'reason'), [
    ({'ok': True, 'terminal': {'connection': {'state': 'synchronized',
                                               'last_connection_lost_at': None},
                               'mql5_community_connected': True}}, True, None),
    ({'terminal': {'connected': True}}, True, None),
    ({'terminal': {'connection': {'state': 'disconnected'}}}, False, 'terminal_disconnected'),
    ({'terminal': {'connection': {'state': 'synchronized'}, 'connected': False}}, False, 'invalid_data'),
    ({'terminal': {'connection': {'state': 'unrecognized'}}}, False, 'invalid_data'),
    ({'terminal': {'mql5_community_connected': True,
                   'connection': {'last_connection_lost_at': None}}}, False, 'invalid_data'),
])
def test_connect_reads_native_connection_state_without_extra_calls(tmp_path, payload, expected, reason):
    class TerminalClient(FakeClient):
        def call_tool(self, name, args):
            assert name == 'terminal_get_info' and args == {}
            self.calls.append((name, args))
            return {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(payload)}]}

    client = TerminalClient()
    runner = probe.Runner(tmp_path, 'a' * 40, 'XAUUSD_i')
    cfg = probe.Config(backend='native', mcp_url='http://127.0.0.1:22346/mcp', mcp_token='synthetic-token')
    assert runner.connect(cfg, lambda *args, **kwargs: client) is expected
    assert runner.report['checks']['daytrade_mt5_terminal'] == ('pass' if expected else 'blocked')
    assert runner.report['reasons'].get('daytrade_mt5_terminal') == reason
    assert client.calls == [('terminal_get_info', {})]
    assert runner.calls == {**{name: 0 for name in probe.RAW_LIMITS}, 'terminal_get_info': 1}


def test_native_application_error_is_not_success_or_exposed(tmp_path):
    client = FakeClient(application_error=True)
    report, _ = run(tmp_path, client)
    assert report['checks']['daytrade_mt5_m1'] == 'blocked'
    assert report['reasons']['daytrade_mt5_m1'] == 'tool_error'
    assert report['checks']['daytrade_live_entry_buy'] == 'blocked'
    assert 'SECRET-RAW-ERROR' not in json.dumps(report)
    assert 'SECRET-RAW-ERROR' in (tmp_path / 'native-m1.json').read_text()


def test_missing_schema_blocks_call_instead_of_guessing_arguments(tmp_path):
    client = FakeClient(missing_schema='datetime_from')
    report, _ = run(tmp_path, client)
    assert report['checks']['daytrade_mt5_m1'] == 'blocked'
    assert report['reasons']['daytrade_mt5_m1'] == 'schema_mismatch'
    assert 'get_chart_history' not in [name for name, _ in client.calls]


def test_no_remote_endpoint_no_quote_no_data_requests(tmp_path):
    runner = probe.Runner(tmp_path, 'a' * 40, 'XAUUSD_i')
    runner.discover_and_fixtures()
    cfg = probe.Config(backend='native', mcp_url='https://example.test/mcp', mcp_token='secret')
    runner.live(cfg, lambda *a, **k: pytest.fail('must not connect'), lambda _: None, lambda: NOW.timestamp())
    report = runner.finish()
    assert report['checks']['daytrade_mt5_terminal'] == 'blocked'
    assert report['reasons']['daytrade_mt5_terminal'] == 'client_unavailable'
    assert report['checks']['daytrade_fixture_news'] == 'pass'


def test_second_quote_error_is_not_misreported_as_unchanged(tmp_path):
    class SecondFails(FakeClient):
        def call_tool(self, name, args):
            if name == 'get_marketwatch_symbols' and any(n == name for n, _ in self.calls):
                self.calls.append((name, args))
                raise probe.McpError('secret raw error', status=403)
            return super().call_tool(name, args)

    report, _ = run(tmp_path, SecondFails())
    assert report['counts']['daytrade_mt5_quote_samples'] == 1
    assert report['checks']['daytrade_mt5_quote'] == 'pass'
    assert report['checks']['daytrade_mt5_quote_update'] == 'blocked'
    assert report['reasons']['daytrade_mt5_quote_update'] == 'authentication_failed'
    assert 'secret raw error' not in json.dumps(report)


def test_native_network_guard_rejects_nonloopback_even_if_a_hostname_resolves_there():
    with probe.loopback_native():
        sock = socket.socket()
        try:
            with pytest.raises(RuntimeError, match='resolve to loopback'):
                sock.connect(('192.0.2.1', 80))
        finally:
            sock.close()


def test_isolated_http_guard_rejects_all_non_test_destinations():
    with probe.only_loopback_port(9876):
        sock = socket.socket()
        try:
            with pytest.raises(RuntimeError, match='isolated test server'):
                sock.connect(('127.0.0.1', 9877))
            with pytest.raises(RuntimeError, match='isolated test server'):
                socket.getaddrinfo('example.com', 443)
        finally:
            sock.close()


def test_all_redirects_are_rejected_before_any_follow_up_request():
    assert probe.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.test/') is None


def test_output_directory_is_private_exclusive_and_restricted_to_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, 'ROOT', tmp_path)
    out = probe.new_private_output(Path('logs/check-1'))
    assert (out.stat().st_mode & 0o777) == 0o700
    with pytest.raises(FileExistsError):
        probe.new_private_output(Path('logs/check-1'))
    with pytest.raises(ValueError):
        probe.new_private_output(Path('not-logs/check-2'))
    with pytest.raises(ValueError):
        probe.new_private_output(Path('logs/nested/check-3'))


def test_native_allowlist_and_attempt_budget_are_hard_limits(tmp_path):
    runner = probe.Runner(tmp_path, 'a' * 40, 'XAUUSD_i')
    runner.client = FakeClient()
    runner.tools = {x['name']: x for x in runner.client.list_tools()}
    with pytest.raises(probe.Blocked, match='unsafe_operation_excluded'):
        runner.native('trade_send_market_order', {}, 'forbidden')
    runner.native('terminal_get_info', {}, 'terminal')
    with pytest.raises(probe.Blocked, match='unsafe_operation_excluded'):
        runner.native('terminal_get_info', {}, 'terminal-again')
    assert [n for n, _ in runner.client.calls] == ['terminal_get_info']


def test_timestamp_freshness_distinguishes_stale_unknown_and_clock_skew():
    clock = {'utc_time': NOW.isoformat()}
    assert probe.freshness(clock, NOW.isoformat(), NOW.timestamp(), 200) == ('pass', None)
    assert probe.freshness(clock, (NOW - timedelta(minutes=3)).isoformat(), NOW.timestamp(), 200) == ('fail', 'stale_data')
    assert probe.freshness(clock, '2026-11-23T10:00:00', NOW.timestamp(), 200) == ('blocked', 'timestamp_unverified')
    assert probe.freshness(clock, NOW.isoformat(), NOW.timestamp() + 60, 200) == ('blocked', 'clock_mismatch')
    assert probe.freshness(clock, NOW.isoformat(), NOW.timestamp(), 1500) == ('blocked', 'timestamp_unverified')


def test_bars_reject_mixed_timestamp_bases_invalid_ohlc_and_duplicates():
    bars = fake_bars(2)
    bars[0]['time'] = '2026-11-23T09:59:00'
    with pytest.raises(probe.Blocked, match='timestamp_unverified'):
        probe.compact_bars({'candles': bars})
    bars = fake_bars(2)
    bars[1]['high'] = bars[1]['low'] - 1
    with pytest.raises(probe.Blocked, match='invalid_data'):
        probe.compact_bars({'candles': bars})
    bars = fake_bars(2)
    bars[1]['time'] = bars[0]['time']
    with pytest.raises(probe.Blocked, match='invalid_data'):
        probe.compact_bars({'candles': bars})


@pytest.mark.parametrize('url', ['http://127.0.0.1:22346/mcp', 'http://localhost:22346/mcp', 'http://[::1]:2/mcp'])
def test_loopback_url_accepted(url):
    assert probe.local_terminal_url(url)


@pytest.mark.parametrize('url', ['http://192.168.1.4/mcp', 'https://localhost/mcp',
                                 'http://secret@localhost/mcp', 'http://localhost/mcp?token=secret'])
def test_remote_or_credential_url_rejected(url):
    assert not probe.local_terminal_url(url)
