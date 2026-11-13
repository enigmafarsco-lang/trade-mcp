"""A tiny MCP client over Streamable HTTP, standard library only.

Why this exists
---------------
Newer MetaTrader 5 builds ship their own MCP servers:

* ``http://127.0.0.1:22346/mcp`` - *terminal*: orders, positions, account data
* ``https://www.metatrader.com/mcp`` - *marketdata*: quotes, news, calendar
* ``http://127.0.0.1:22345/mcp`` - *metaeditor*: coding tools

Talking to them needs no MQL5 code and no compilation, which makes it a very
useful second transport next to the ``MQL5/Files`` bridge.  ``mcp`` (the SDK we
use for our own server) can speak HTTP, but it drags in asyncio and hides the
handshake; this module is deliberately small, synchronous and dependency-free
so it can also be used from a one-off script for discovery.

Wire notes (Streamable HTTP transport)
--------------------------------------
* every call is a ``POST`` with a JSON-RPC 2.0 body;
* the server may answer with ``application/json`` *or* with an SSE stream
  (``text/event-stream``), so both are decoded here;
* the ``initialize`` answer carries an ``Mcp-Session-Id`` header which must be
  echoed on every later request;
* after ``initialize`` the client sends the ``notifications/initialized``
  notification (server answers ``202`` with an empty body);
* authenticated endpoints expect ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import contextlib
import json
import socket
from typing import Any
from urllib import error, request
from urllib.parse import urlparse

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 20.0


class McpError(RuntimeError):
    """Any failure while talking MCP: transport, HTTP, or a JSON-RPC error."""

    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _parse_sse(text: str, want_id: Any = None) -> dict[str, Any] | None:
    """Return the JSON-RPC message carried by an SSE body (last one wins)."""
    blobs: list[str] = []
    pending: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line == "":
            if pending:
                blobs.append("\n".join(pending))
                pending = []
            continue
        if line.startswith(":"):  # keep-alive comment
            continue
        if line.startswith("data:"):
            pending.append(line[5:].lstrip())
    if pending:
        blobs.append("\n".join(pending))

    messages: list[dict[str, Any]] = []
    for blob in blobs:
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            messages.append(parsed)
    if not messages:
        return None
    if want_id is not None:
        for message in messages:
            if message.get("id") == want_id:
                return message
    return messages[-1]


def probe_tcp(url: str, timeout: float = 2.0) -> str | None:
    """Pre-flight check: return an error string when nothing listens on the URL."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    if parsed.scheme not in {"http", "https"}:
        return f"unsupported scheme in {url!r} (expected http or https)"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as exc:
        return f"nothing is listening on {host}:{port} ({exc.__class__.__name__}: {exc})"


class McpHttpClient:
    """Minimal JSON-RPC client for one MCP endpoint."""

    def __init__(
        self,
        url: str,
        token: str = "",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client_name: str = "trade-mcp",
        client_version: str = "0.2.0",
    ):
        self.url = url
        self.token = token
        self.timeout = float(timeout)
        self.client_name = client_name
        self.client_version = client_version
        self.session_id: str | None = None
        self.server_info: dict[str, Any] = {}
        self.protocol_version: str | None = None
        self._next_id = 0
        self._handshaken = False

    # ------------------------------------------------------------------ http
    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"{self.client_name}/{self.client_version}",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self._handshaken:
            headers["MCP-Protocol-Version"] = self.protocol_version or PROTOCOL_VERSION
        return headers

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(self.url, data=body, headers=self._headers(), method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                status = response.status
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                content_type = (response.headers.get("Content-Type") or "").lower()
                raw = response.read()
        except error.HTTPError as exc:
            text = ""
            with contextlib.suppress(OSError):
                text = exc.read()[:600].decode("utf-8", "replace")
            hint = ""
            if exc.code in {401, 403}:
                hint = " - the API key looks wrong; regenerate it in Tools > Options > MCP"
            raise McpError(
                f"HTTP {exc.code} from {self.url}{hint}: {text or exc.reason}",
                status=exc.code,
            ) from None
        except error.URLError as exc:
            raise McpError(f"cannot reach {self.url}: {exc.reason}") from None
        except TimeoutError:
            raise McpError(f"{self.url} did not answer within {self.timeout:.0f}s") from None

        if not raw:
            return None if status == 202 else {}
        text = raw.decode("utf-8", "replace")
        if "text/event-stream" in content_type:
            message = _parse_sse(text, payload.get("id"))
            if message is None:
                raise McpError(f"{self.url} sent an SSE body without a JSON-RPC message")
            return message
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            raise McpError(f"{self.url} sent a non-JSON body: {text[:200]!r}") from None
        return parsed if isinstance(parsed, dict) else {"result": parsed}

    @staticmethod
    def _unwrap(message: dict[str, Any] | None) -> dict[str, Any]:
        if not message:
            return {}
        err = message.get("error")
        if err:
            if isinstance(err, dict):
                raise McpError(
                    f"JSON-RPC error {err.get('code')}: {err.get('message')}",
                    payload=err,
                )
            raise McpError(f"JSON-RPC error: {err}", payload=err)
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------- handshake
    def initialize(self) -> dict[str, Any]:
        message = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": self.client_name, "version": self.client_version},
                },
            }
        )
        result = self._unwrap(message)
        self.server_info = result.get("serverInfo") or {}
        self.protocol_version = result.get("protocolVersion") or PROTOCOL_VERSION
        self._handshaken = True
        # Required by the spec before any other request; the server replies 202.
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._unwrap(
            self._post({"jsonrpc": "2.0", "id": self._id(), "method": "tools/list", "params": {}})
        )
        tools = result.get("tools")
        return [tool for tool in tools or [] if isinstance(tool, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self._unwrap(
            self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._id(),
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                }
            )
        )
        return result

    @staticmethod
    def text_content(result: dict[str, Any]) -> str:
        """Flatten a tool result into plain text (all ``text`` parts joined)."""
        parts = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)

    @staticmethod
    def structured_content(result: dict[str, Any]) -> Any:
        """Best-effort: the tool's structured payload, or its text parsed as JSON."""
        if "structuredContent" in result:
            return result["structuredContent"]
        text = McpHttpClient.text_content(result).strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    # ---------------------------------------------------------------- helpers
    def connect(self) -> McpHttpClient:
        self.initialize()
        return self


def connect(url: str, token: str = "", *, timeout: float = DEFAULT_TIMEOUT) -> McpHttpClient:
    """Open a handshaken client in one call."""
    return McpHttpClient(url, token, timeout=timeout).connect()
