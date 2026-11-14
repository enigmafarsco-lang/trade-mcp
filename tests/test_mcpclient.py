"""The stdlib MCP client, tested against a stub Streamable-HTTP server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from trade_mcp.mcpclient import (
    McpError,
    McpHttpClient,
    _parse_sse,
    is_local_host,
    probe_tcp,
)

TOKEN = "good-token"
SESSION = "sess-abc123"


class _Stub(BaseHTTPRequestHandler):
    """Implements just enough of the MCP Streamable-HTTP transport."""

    calls: list[dict] = []

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json", extra=None):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls.append(
            {
                "method": payload.get("method"),
                "session": self.headers.get("Mcp-Session-Id"),
                "accept": self.headers.get("Accept"),
                "protocol": self.headers.get("MCP-Protocol-Version"),
                "authorization": self.headers.get("Authorization"),
            }
        )

        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(
                401,
                json.dumps({"error": "unauthorized"}).encode(),
            )
            return

        method = payload.get("method")
        if method == "initialize":
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "MT5-stub", "version": "6316"},
                    },
                }
            ).encode()
            self._send(200, body, extra={"Mcp-Session-Id": SESSION})
            return

        if method == "notifications/initialized":
            self._send(202)
            return

        if self.headers.get("Mcp-Session-Id") != SESSION:
            self._send(400, b'{"error":"missing session"}')
            return

        if method == "tools/list":
            body = (
                "event: message\n"
                "data: "
                + json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "tools": [
                                {
                                    "name": "get_account",
                                    "description": "Account snapshot",
                                    "inputSchema": {"type": "object", "properties": {}},
                                },
                                {
                                    "name": "place_order",
                                    "description": "Send an order",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "symbol": {"type": "string"},
                                            "volume": {"type": "number"},
                                            "side": {"type": "string"},
                                        },
                                        "required": ["symbol", "volume"],
                                    },
                                },
                            ]
                        },
                    }
                )
                + "\n\n"
            ).encode()
            self._send(200, body, ctype="text/event-stream")
            return

        if method == "tools/call":
            name = (payload.get("params") or {}).get("name")
            if name not in {"get_account", "place_order"}:
                self._send(
                    200,
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": payload["id"],
                            "error": {"code": -32602, "message": f"unknown tool {name!r}"},
                        }
                    ).encode(),
                )
                return
            text = json.dumps({"balance": 10000.0, "currency": "USD"})
            body = (
                "event: message\n"
                "data: "
                + json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "content": [{"type": "text", "text": text}],
                            "structuredContent": {"balance": 10000.0, "currency": "USD"},
                        },
                    }
                )
                + "\n\n"
            ).encode()
            self._send(200, body, ctype="text/event-stream")
            return

        self._send(
            200,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {"code": -32601, "message": "no such method"},
                }
            ).encode(),
        )


@pytest.fixture()
def stub():
    _Stub.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def test_handshake_keeps_session_and_protocol_header(stub):
    client = McpHttpClient(stub, TOKEN, timeout=5).connect()
    assert client.session_id == SESSION
    assert client.server_info["name"] == "MT5-stub"

    tools = client.list_tools()
    assert {t["name"] for t in tools} == {"get_account", "place_order"}

    seen = {c["method"]: c for c in _Stub.calls}
    assert seen["notifications/initialized"]["session"] == SESSION
    assert seen["tools/list"]["protocol"] == "2025-06-18"
    assert "text/event-stream" in seen["tools/list"]["accept"]


def test_tool_call_decodes_sse_and_structured_content(stub):
    client = McpHttpClient(stub, TOKEN, timeout=5).connect()
    result = client.call_tool("get_account", {})
    assert client.structured_content(result) == {"balance": 10000.0, "currency": "USD"}
    assert '"balance"' in client.text_content(result)


def test_bad_token_explains_itself(stub):
    with pytest.raises(McpError) as excinfo:
        McpHttpClient(stub, "nope", timeout=5).initialize()
    assert excinfo.value.status == 401
    assert "Tools > Options > MCP" in str(excinfo.value)


def test_missing_session_is_reported(stub):
    client = McpHttpClient(stub, TOKEN, timeout=5)
    client.initialize()
    client.session_id = None  # simulate a server that forgot the header
    with pytest.raises(McpError):
        client.list_tools()


def test_jsonrpc_error_is_raised(stub):
    client = McpHttpClient(stub, TOKEN, timeout=5).connect()
    with pytest.raises(McpError):
        client.call_tool("does_not_exist", {})


def test_probe_tcp_reports_closed_port():
    message = probe_tcp("http://127.0.0.1:1/mcp", timeout=0.5)
    assert message and "nothing is listening" in message
    assert probe_tcp("ftp://example.com/x") is not None


def test_parse_sse_picks_matching_id_and_ignores_comments():
    body = ": keep-alive\n\nevent: message\ndata: {\"id\": 2, \"result\": {}}\n\n"
    assert _parse_sse(body, 2) == {"id": 2, "result": {}}
    assert _parse_sse("nonsense", 1) is None
    assert _parse_sse('data: {"id": 9, "result": {"a": 1}}\n\n', 1) == {
        "id": 9,
        "result": {"a": 1},
    }


def test_is_local_host():
    assert is_local_host("http://127.0.0.1:22346/mcp")
    assert is_local_host("http://localhost:22346/mcp")
    assert is_local_host("http://[::1]:22346/mcp")
    assert not is_local_host("https://www.metatrader.com/mcp")
    assert not is_local_host("http://example.com/mcp")


def test_local_endpoint_ignores_environment_proxy(stub, monkeypatch):
    """A stray http_proxy must not swallow requests to 127.0.0.1 (it did once)."""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    client = McpHttpClient(stub, TOKEN, timeout=5).connect()
    assert {tool["name"] for tool in client.list_tools()} == {"get_account", "place_order"}


def test_remote_endpoint_honours_the_proxy_when_asked(stub, monkeypatch):
    """bypass_proxy=False must keep the environment proxy (deliberately broken here)."""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    client = McpHttpClient(stub, TOKEN, timeout=5, bypass_proxy=False)
    with pytest.raises(McpError):
        client.initialize()


def test_key_fingerprint_never_reveals_the_key():
    from trade_mcp.mcpclient import key_fingerprint

    secret = "0123456789abcdef0123456789abcdef"
    shown = key_fingerprint(secret)
    assert shown.startswith("len=32 ")
    assert secret not in shown
    assert "0123" in shown and "cdef" in shown
    assert key_fingerprint("") == "empty"
    assert "too short" in key_fingerprint("abc")


def test_key_paste_problem_detects_a_pasted_copy_block():
    from trade_mcp.mcpclient import key_paste_problem

    assert key_paste_problem('{"env":{"MT5_MCP_API_KEY":"abc"}}')
    assert key_paste_problem("api_key = abc")
    assert key_paste_problem("Bearer abc")
    assert key_paste_problem(" abc")
    assert key_paste_problem("abc def")
    assert key_paste_problem("0123456789abcdef") is None
    assert key_paste_problem("") is None
