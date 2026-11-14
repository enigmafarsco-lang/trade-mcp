"""NativeBridge: our command contract on top of MetaTrader's own MCP tools."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from trade_mcp.bridge.client import MT5
from trade_mcp.bridge.native import NativeBridge, find, records, to_epoch

TOKEN = "terminal-key"
SESSION = "sess-native-1"

# Payloads deliberately mimic the nested shapes a server may use, so the
# field lookup (recursive, case/underscore-insensitive) is really exercised.
ACCOUNT = {
    "ok": True,
    "account": {
        "login": "10013594880",
        "server": "MetaQuotes-Demo",
        "currency": "USD",
        "leverage": 100,
        "balance": 10000.0,
        "equity": 10012.5,
        "margin": 100.0,
        "margin_free": 9912.5,
        "margin_level": 10012.5,
        "profit": 12.5,
        "credit": 0.0,
        "trade_allowed": True,
        "trade_mode": "demo",
    },
}

SYMBOLS = {
    "count": 1,
    "symbols": [
        {
            "name": "XAUUSD",
            "description": "Gold vs US Dollar",
            "path": "Metals\\Gold",
            "digits": 2,
            "point": 0.01,
            "spread": 25,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
            "trade_contract_size": 100.0,
            "trade_tick_value": 1.0,
            "trade_stops_level": 0,
            "bid": 2650.24,
            "ask": 2650.49,
            "visible": True,
        }
    ],
}

CANDLES = {
    "count": 3,
    "candles": [
        {"time": "2026-11-12T09:30:00Z", "open": 2649.0, "high": 2650.0, "low": 2648.5, "close": 2649.8, "volume": 120},
        {"time": "2026-11-12T09:45:00Z", "open": 2649.8, "high": 2651.0, "low": 2649.2, "close": 2650.5, "volume": 140},
        {"time": "2026-11-12T10:00:00Z", "open": 2650.5, "high": 2651.4, "low": 2650.0, "close": 2650.9, "volume": 90},
    ],
}

OPEN_POSITIONS = {
    "positions": [
        {
            "ticket": "500123",
            "symbol": "XAUUSD",
            "type": "buy",
            "volume": 0.05,
            "price_open": 2650.0,
            "sl": 2640.0,
            "tp": 2670.0,
            "price_current": 2652.0,
            "profit": 10.0,
            "swap": -0.4,
            "magic": "20251112",
            "comment": "trade-mcp",
            "time": "2026-11-12T08:00:00Z",
        }
    ],
    "orders": [
        {
            "ticket": "600001",
            "symbol": "XAUUSD",
            "type": "buy_limit",
            "volume": 0.02,
            "price": 2600.0,
            "sl": 2590.0,
            "tp": 2620.0,
            "magic": "20251112",
            "time_setup": "2026-11-12T07:00:00Z",
            "comment": "pending",
        }
    ],
}

HISTORY = {
    "deals": [
        {
            "ticket": "400001",
            "position_id": "300001",
            "order": "300001",
            "symbol": "XAUUSD",
            "type": "sell",
            "entry": "out",
            "volume": 0.05,
            "price": 2660.0,
            "profit": 50.0,
            "swap": 0.0,
            "commission": -0.7,
            "magic": "20251112",
            "time": "2026-11-11T15:00:00Z",
            "comment": "tp",
        }
    ]
}

SUCCESS = {"ok": True, "retcode": 10009, "order": "500124", "deal": "500125", "volume": 0.05, "price": 2650.5}

RESPONSES: dict[str, dict] = {
    "terminal_get_info": {
        "terminal": {
            "build": 6316,
            "company": "MetaQuotes Software Corp.",
            "data_path": "C:\\Users\\joshua\\AppData\\Roaming\\MetaQuotes\\Terminal\\ABC",
            "connected": True,
            "trade_allowed": True,
        }
    },
    "get_time_information": {"utc_time": "2026-11-12T10:00:00Z", "local_utc_offset_minutes": 0},
    "get_trading_account_info": ACCOUNT,
    "get_marketwatch_symbols": SYMBOLS,
    "get_chart_history": CANDLES,
    "get_trading_open_positions": OPEN_POSITIONS,
    "get_trading_history_orders": HISTORY,
    "trade_order_calc_margin": {"margin": 132.5},
    "trade_send_market_order": SUCCESS,
    "trade_modify_sl_tp": {"ok": True, "retcode": 10009},
    "trade_close_single_position": {"ok": True, "retcode": 10009, "deal": "500126"},
    "add_marketwatch_symbol": {"ok": True},
    "remove_marketwatch_symbol": {"ok": True},
}


class _StubMCP(BaseHTTPRequestHandler):
    calls: list[tuple[str, dict]] = []

    def log_message(self, *args):
        pass

    def _send(self, status: int, payload: dict | None = None, extra=None):
        body = json.dumps(payload).encode() if payload else b""
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        message = json.loads(self.rfile.read(length) or b"{}")
        method = message.get("method")

        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"error": "unauthorized"})
            return
        if method == "initialize":
            self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "MetaTrader 5 MCP", "version": "1.0"},
                    },
                },
                extra={"Mcp-Session-Id": SESSION},
            )
            return
        if method == "notifications/initialized":
            self._send(202)
            return
        if self.headers.get("Mcp-Session-Id") != SESSION:
            self._send(400, {"error": "missing session"})
            return

        if method == "tools/list":
            self._send(
                200,
                {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": [
                    {"name": name, "inputSchema": {"type": "object", "properties": {}}}
                    for name in RESPONSES
                ]}},
            )
            return

        if method == "tools/call":
            params = message.get("params") or {}
            name = str(params.get("name"))
            args = params.get("arguments") or {}
            type(self).calls.append((name, args))
            if name not in RESPONSES:
                self._send(200, {"jsonrpc": "2.0", "id": message["id"],
                                 "error": {"code": -32602, "message": f"unknown tool {name}"}})
                return
            payload = RESPONSES[name]
            self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(payload)}],
                        "structuredContent": payload,
                    },
                },
            )
            return

        self._send(200, {"jsonrpc": "2.0", "id": message.get("id"),
                         "error": {"code": -32601, "message": "no such method"}})


@pytest.fixture()
def stub():
    _StubMCP.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubMCP)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def bridge(url: str) -> NativeBridge:
    return NativeBridge(url, TOKEN, timeout=5)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def test_find_is_case_and_underscore_insensitive():
    payload = {"Account": {"MarginFree": 12.5, "nested": {"equity": 99}}}
    assert find(payload, "margin_free") == 12.5
    assert find(payload, "equity") == 99
    assert find(payload, "nothing_here") is None


def test_records_falls_back_to_the_first_list_of_objects():
    payload = {"count": 1, "result": {"items": [{"a": 1}]}}
    assert records(payload) == [{"a": 1}]
    assert records({"deals": [{"x": 1}], "orders": [{"y": 2}]}, "deals") == [{"x": 1}]


def test_to_epoch_accepts_iso_epoch_and_milliseconds():
    assert to_epoch("2026-11-12T10:00:00Z") == 1794477600
    assert to_epoch(1794477600) == 1794477600
    assert to_epoch(1794477600000) == 1794477600
    assert to_epoch("") == 0
    assert to_epoch("not a date") == 0


# --------------------------------------------------------------------------- #
# command contract                                                             #
# --------------------------------------------------------------------------- #
def test_account_and_terminal_shapes(stub):
    native = bridge(stub)
    account = native.request("account_info")
    assert account["login"] == 10013594880
    assert account["balance"] == 10000.0
    assert account["margin_free"] == 9912.5
    assert account["trade_mode"] == "demo"
    assert account["leverage"] == 100

    terminal = native.request("terminal_info")
    assert terminal["build"] == 6316
    assert terminal["connected"] is True

    ping = native.request("ping")
    assert ping["pong"] is True
    assert ping["epoch"] == 1794477600


def test_symbol_and_tick_shapes(stub):
    native = bridge(stub)
    info = native.request("symbol_info", symbol="XAUUSD")
    assert info["digits"] == 2 and info["point"] == 0.01
    assert info["symbol"] == "XAUUSD"
    assert info["volume_min"] == 0.01
    assert info["bid"] == 2650.24

    tick = native.request("tick", symbol="XAUUSD")
    assert tick["ask"] == 2650.49
    assert tick["spread_points"] == 25.0

    listed = native.request("symbols_list", pattern="gold", limit=10)
    assert [row["symbol"] for row in listed] == ["XAUUSD"]


def test_rates_are_sorted_and_capped(stub):
    native = bridge(stub)
    data = native.request("rates", symbol="XAUUSD", timeframe="M15", count=2)
    rates = data["rates"]
    assert data["count"] == 2
    assert [bar["close"] for bar in rates] == [2650.5, 2650.9]
    assert rates[-1]["time"] > rates[0]["time"]
    assert rates[0]["tick_volume"] == 140

    name, arguments = _StubMCP.calls[-1]
    assert name == "get_chart_history"
    assert arguments["period"] == "M15"
    assert arguments["symbol"] == "XAUUSD"


def test_positions_orders_and_history(stub):
    native = bridge(stub)
    positions = native.request("positions")
    assert positions[0]["ticket"] == 500123  # string ticket -> int
    assert positions[0]["type"] == "buy"
    assert positions[0]["profit"] == 10.0

    orders = native.request("orders")
    assert orders[0]["ticket"] == 600001
    assert orders[0]["type"] == "buy_limit"

    deals = native.request("history_deals", date_from=0, date_to=0)
    assert deals[0]["commission"] == -0.7
    assert deals[0]["entry"] == "out"
    assert deals[0]["type"] == "sell"


def test_calc_order_uses_ask_when_no_price_given(stub):
    native = bridge(stub)
    calc = native.request("calc_order", symbol="XAUUSD", side="buy", volume=0.05)
    assert calc["margin"] == 132.5
    assert calc["price"] == 2650.49
    assert calc["free_margin_after"] == round(9912.5 - 132.5, 2)

    margin_calls = [args for name, args in _StubMCP.calls if name == "trade_order_calc_margin"]
    assert margin_calls[-1] == {"symbol": "XAUUSD", "type": "buy", "volume": 0.05, "price": 2650.49}


def test_trade_open_modify_close(stub):
    native = bridge(stub)
    opened = native.request("trade_open", symbol="XAUUSD", side="buy", volume=0.05, sl=2640.0)
    assert opened["retcode"] == 10009
    assert opened["deal"] == 500125
    name, arguments = _StubMCP.calls[-1]
    assert name == "trade_send_market_order"
    assert arguments["type"] == "buy" and arguments["sl"] == 2640.0

    modified = native.request("trade_modify", ticket=500123, sl=2645.0)
    assert modified["retcode"] == 10009
    name, arguments = _StubMCP.calls[-1]
    assert name == "trade_modify_sl_tp"
    assert arguments == {"symbol": "XAUUSD", "position_ticket": "500123", "sl": 2645.0}

    closed = native.request("trade_close", ticket=500123)
    assert closed["deal"] == 500126
    assert _StubMCP.calls[-1][0] == "trade_close_single_position"


def test_partial_close_is_refused_instead_of_closing_everything(stub):
    from trade_mcp.bridge.base import BridgeError

    native = bridge(stub)
    with pytest.raises(BridgeError) as excinfo:
        native.request("trade_close", ticket=500123, volume=0.01)
    assert excinfo.value.code == "partial_close_unsupported"


def test_missing_position_is_reported(stub):
    from trade_mcp.bridge.base import BridgeError

    native = bridge(stub)
    with pytest.raises(BridgeError) as excinfo:
        native.request("trade_close", ticket=999)
    assert excinfo.value.code == "not_found"


def test_status_reports_the_server(stub):
    native = bridge(stub)
    status = native.status()
    assert status["ea_connected"] is True
    assert status["transport"] == "native"
    assert status["ea_hello"]["build"] == 6316


def test_status_explains_an_unreachable_server():
    native = NativeBridge("http://127.0.0.1:1/mcp", TOKEN, timeout=1)
    status = native.status()
    assert status["ea_connected"] is False
    assert "failed" in status["reason"]


def test_unknown_command_is_explicit(stub):
    from trade_mcp.bridge.base import BridgeError

    with pytest.raises(BridgeError) as excinfo:
        bridge(stub).request("trade_close_all")
    assert excinfo.value.code == "unsupported_command"


# --------------------------------------------------------------------------- #
# integration with the typed client                                            #
# --------------------------------------------------------------------------- #
def test_typed_client_round_trip(stub):
    mt5 = MT5(bridge(stub), "XAUUSD")
    assert mt5.resolve_symbol("XAUUSD") == "XAUUSD"  # no Market Watch add needed
    assert mt5.account_info()["equity"] == 10012.5
    assert len(mt5.candles("XAUUSD", "M15", 3)) == 3
    assert mt5.positions()[0]["volume"] == 0.05
    assert mt5.calc_order("XAUUSD", "sell", 0.02)["margin"] == 132.5
    assert mt5.open_order("XAUUSD", "buy", 0.01)["retcode"] == 10009


# --------------------------------------------------------------------------- #
# application wiring                                                           #
# --------------------------------------------------------------------------- #
def test_build_app_wires_the_native_backend(stub, tmp_path):
    from trade_mcp.app import build_app
    from trade_mcp.config import Config

    cfg = Config(
        backend="native",
        mcp_url=stub,
        mcp_token=TOKEN,
        journal_path=str(tmp_path / "journal.sqlite3"),
    )
    app = build_app(cfg, backend="native")
    try:
        assert app.mt5.status()["ea_connected"] is True
        assert app.mt5.account_info()["balance"] == 10000.0
        assert app.mt5.candles("XAUUSD", "M15", 3)[-1]["close"] == 2650.9
    finally:
        app.mt5.close()


def test_native_backend_requires_a_token():
    from trade_mcp.app import build_app
    from trade_mcp.bridge.base import BridgeError
    from trade_mcp.config import Config

    with pytest.raises(BridgeError) as excinfo:
        build_app(Config(backend="native", mcp_token=""), backend="native")
    assert excinfo.value.code == "mcp_token_missing"


def test_snapshot_script_prints_payload_shapes(stub, capsys):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "mt5_mcp_snapshot.py"
    spec = importlib.util.spec_from_file_location("mt5_mcp_snapshot", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    code = module.main(
        ["--url", f"terminal={stub}", "--token", f"terminal={TOKEN}", "--symbol", "XAUUSD"]
    )
    out = capsys.readouterr().out
    assert "balance = 10000.0" in out
    assert "get_chart_history" in out
    assert "no trading tool was called" in out or "tool call(s) failed" in out
    assert code in (0, 1)  # the stub does not implement list_open_charts


def test_env_selects_the_native_backend(monkeypatch):
    from trade_mcp.config import Config

    monkeypatch.setenv("TRADE_MCP_BACKEND", "native")
    monkeypatch.setenv("TRADE_MCP_MCP_URL", "http://127.0.0.1:22346/mcp")
    monkeypatch.delenv("TRADE_MCP_MCP_TOKEN", raising=False)
    monkeypatch.setenv("MT5_MCP_TOKEN", "from-the-alias")
    cfg = Config.from_env()
    assert cfg.backend == "native"
    assert cfg.mcp_url == "http://127.0.0.1:22346/mcp"
    assert cfg.mcp_token == "from-the-alias"

    monkeypatch.setenv("TRADE_MCP_BACKEND", "mcp")
    assert Config.from_env().backend == "native"
    monkeypatch.setenv("TRADE_MCP_BACKEND", "nonsense")
    assert Config.from_env().backend == "bridge"


def test_build_6318_position_field_names():
    row = NativeBridge._position_record({
        "position_id": 152898489594, "action": "sell", "symbol": "EURUSD",
        "volume": 9.01, "price_open": 1.10793, "price_last": 1.10528,
        "swaps": -6.31, "create_time": "2026-11-12T16:47:16",
    })
    assert row["ticket"] == 152898489594
    assert row["type"] == "sell"
    assert row["price_current"] == 1.10528
    assert row["swap"] == -6.31
    assert row["time"] > 0


def test_build_6318_account_without_leverage(monkeypatch):
    monkeypatch.setattr(NativeBridge, "call", lambda *a, **kw: {
        "account": {"type": "demo", "login": 123, "balance": 100000},
        "terminal": {"build": 6318},
    })
    native = NativeBridge.__new__(NativeBridge)
    native.last_raw = {}
    account = native._cmd_account_info()
    assert account["trade_mode"] == "demo"
    assert account["leverage"] is None


def test_forward_limited_history_covers_latest_bars(monkeypatch):
    from datetime import datetime, timezone

    native = NativeBridge('http://unused', 'test')
    calls = []

    def call(tool, args=None):
        calls.append((tool, args))
        if tool == 'get_time_information':
            return {'utc_time': '2026-11-13T10:00:00Z',
                    'trade_server_last_known_time': '2026-11-13T12:00:00'}
        start = int(datetime.fromisoformat(args['datetime_from']).replace(tzinfo=timezone.utc).timestamp())
        end = int(datetime.fromisoformat(args['datetime_to']).replace(tzinfo=timezone.utc).timestamp())
        # Simulate a tool returning the FIRST `limit` records, not the last.
        history = [dict(time=t, open=100, high=102, low=99, close=101)
                   for t in range(start, end+1, 900)]
        return {'history': history[:args['limit']]}

    monkeypatch.setattr(native, 'call', call)
    data = native._cmd_rates(symbol='XAUUSD', timeframe='M15', count=3)
    args = calls[-1][1]
    end = int(datetime.fromisoformat(args['datetime_to']).replace(tzinfo=timezone.utc).timestamp())
    assert len(data['rates']) == 3
    assert data['rates'][-1]['time'] >= end-900
    assert abs(end-datetime.now(timezone.utc).timestamp()-(7200+900)) < 3
    native._cmd_rates(symbol='XAUUSD', timeframe='M15', count=3)
    assert len([c for c in calls if c[0] == 'get_time_information']) == 1


def test_trading_permission_error_is_distinct(monkeypatch):
    from types import SimpleNamespace

    from trade_mcp.bridge.base import BridgeError

    native = NativeBridge('http://unused', 'test')
    client = SimpleNamespace(call_tool=lambda *args: {
        'isError': True, 'content': [{'type':'text','text':'trading is not permitted'}]})
    monkeypatch.setattr(native, '_connect', lambda: client)
    with pytest.raises(BridgeError) as exc:
        native.call('trade_send_market_order', {})
    assert exc.value.code == 'mcp_trading_not_permitted'


def test_terminal_permission_uses_mcp_flag(monkeypatch):
    native = NativeBridge('http://unused', 'test')
    monkeypatch.setattr(native, 'call', lambda *args: {'terminal': {
        'mcp_trade_allowed': False, 'experts_trade_allowed': True}})
    data = native._cmd_terminal_info()
    assert data['mcp_trade_allowed'] is False
    assert data['trade_allowed'] is False
