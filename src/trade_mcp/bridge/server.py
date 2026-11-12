"""TCP server that the MQL5 Expert Advisor connects to.

Design
------
* Python is the **server** (default 127.0.0.1:8787).
* The EA (running inside MetaTrader 5, which may itself run under Wine) is the
  **client** and reconnects automatically when Python restarts.
* Wire format is newline-delimited JSON, one object per line:

      EA  -> Python   {"type":"hello","ea":"TradeMCPConnector","version":"1.00", ...}
      Py  -> EA       {"id":"<uuid>","cmd":"account_info","params":{...}}
      EA  -> Python   {"id":"<uuid>","ok":true,"data":{...}}
      EA  -> Python   {"id":"<uuid>","ok":false,"error":{"code":"...","message":"..."}}

Why this shape: it keeps the Windows-only bits (MQL5, OrderSend) inside the
terminal, while the whole Python/AI stack stays native Linux. No Windows
Python, no `MetaTrader5` pip package, no DLL under Wine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from typing import Any

from .base import BridgeError

LOG = logging.getLogger(__name__)

MAX_LINE_BYTES = 8 * 1024 * 1024


class BridgeServer:
    """Thread-backed asyncio TCP server with a synchronous ``request()`` API."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8787, timeout: float = 20.0):
        self.host = host
        self.port = port
        self.timeout = timeout

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._server: asyncio.AbstractServer | None = None
        self._start_error: BaseException | None = None

        self._writer: asyncio.StreamWriter | None = None
        self._peer: str | None = None
        self._hello: dict[str, Any] = {}
        self._connected_at: float | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._last_error: str | None = None
        self._connections = 0

    # ------------------------------------------------------------------ life
    def start(self, wait: float = 5.0) -> BridgeServer:
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="trade-mcp-bridge", daemon=True)
        self._thread.start()
        if not self._ready.wait(wait):
            raise BridgeError(f"bridge server did not start within {wait}s", code="start_timeout")
        if self._start_error is not None:
            raise BridgeError(
                f"cannot listen on {self.host}:{self.port}: {self._start_error}",
                code="bind_failed",
            )
        LOG.info("bridge listening on %s:%s", self.host, self.port)
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:  # pragma: no cover - defensive
            LOG.exception("bridge loop crashed: %s", exc)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    async def _serve(self) -> None:
        try:
            self._server = await asyncio.start_server(
                self._handle, self.host, self.port, limit=MAX_LINE_BYTES
            )
        except OSError as exc:
            self._start_error = exc
            self._ready.set()
            return
        self._ready.set()
        async with self._server:
            await self._server.serve_forever()

    def close(self) -> None:
        loop, server, writer = self._loop, self._server, self._writer
        if loop is not None and not loop.is_closed():
            if server is not None:
                loop.call_soon_threadsafe(server.close)
            if writer is not None:
                loop.call_soon_threadsafe(writer.close)
        self._thread = None
        self._loop = None

    # -------------------------------------------------------------- handlers
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self._connections += 1
        self._writer = writer
        self._peer = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)
        self._connected_at = time.time()
        self._hello = {}
        LOG.info("EA connected from %s", self._peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    LOG.warning("dropping malformed EA message: %r", line[:200])
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "hello" or "id" not in msg:
                    self._hello = {k: v for k, v in msg.items() if k != "type"}
                    LOG.info("EA hello: %s", self._hello.get("version") or self._hello)
                    continue
                fut = self._pending.get(str(msg.get("id")))
                if fut is None or fut.done():
                    continue
                if msg.get("ok"):
                    fut.set_result(msg.get("data"))
                else:
                    err = msg.get("error") or {}
                    fut.set_exception(
                        BridgeError(
                            str(err.get("message") or "EA returned an error"),
                            code=str(err.get("code") or "ea_error"),
                            details=err,
                        )
                    )
        except (asyncio.IncompleteReadError, ConnectionResetError) as exc:
            LOG.info("EA connection reset: %s", exc)
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except BaseException as exc:  # noqa: BLE001 - never kill the socket thread
            LOG.warning("EA connection failed: %s", exc)
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(BridgeError("EA disconnected", code="disconnected"))
            self._pending.clear()
            if self._writer is writer:
                self._writer = None
                self._peer = None
                self._connected_at = None
            with contextlib.suppress(Exception):  # closing a dead socket must not raise
                writer.close()
            LOG.info("EA disconnected")

    async def _request_coro(self, cmd: str, params: dict[str, Any]) -> Any:
        writer = self._writer
        if writer is None:
            raise BridgeError(
                "no MetaTrader EA is connected. Attach TradeMCPConnector to a chart "
                "and check the Experts log.",
                code="not_connected",
            )
        rid = uuid.uuid4().hex
        fut = self._loop.create_future()  # type: ignore[union-attr]
        self._pending[rid] = fut
        try:
            payload = json.dumps(
                {"id": rid, "cmd": cmd, "params": params},
                separators=(",", ":"),
                default=str,
            )
            writer.write(payload.encode("utf-8") + b"\n")
            await writer.drain()
            return await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._last_error = f"timeout after {self.timeout}s on '{cmd}'"
            raise BridgeError(
                f"EA did not answer '{cmd}' within {self.timeout}s",
                code="timeout",
            ) from None
        finally:
            self._pending.pop(rid, None)

    # ------------------------------------------------------------------ API
    def request(self, cmd: str, **params: Any) -> Any:
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            raise BridgeError("bridge server is not running", code="not_running")
        if self._writer is None:
            raise BridgeError(
                "no MetaTrader EA is connected. Attach TradeMCPConnector to a chart "
                "and check the Experts log.",
                code="not_connected",
            )
        fut = asyncio.run_coroutine_threadsafe(self._request_coro(cmd, params), loop)
        try:
            return fut.result(timeout=self.timeout + 5)
        except TimeoutError:
            fut.cancel()
            raise BridgeError(f"bridge timed out on '{cmd}'", code="timeout") from None

    def status(self) -> dict[str, Any]:
        connected = self._writer is not None
        return {
            "ok": True,
            "backend": "bridge",
            "listen": f"{self.host}:{self.port}",
            "ea_connected": connected,
            "ea_peer": self._peer,
            "ea_hello": self._hello or None,
            "connected_for_seconds": (
                round(time.time() - self._connected_at, 1) if self._connected_at else None
            ),
            "connections_total": self._connections,
            "pending_requests": len(self._pending),
            "last_error": self._last_error,
        }
