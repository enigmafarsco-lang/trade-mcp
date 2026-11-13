"""The file transport, tested against a fake Expert Advisor (no MT5 needed)."""

from __future__ import annotations

import json
import threading
import time

import pytest

from trade_mcp.bridge.base import BridgeError
from trade_mcp.bridge.file import FileBridge


def make_bridge(tmp_path, **kwargs) -> FileBridge:
    files = tmp_path / "MQL5-Files"
    files.mkdir()
    return FileBridge(files, timeout=kwargs.pop("timeout", 1.0), poll=0.01, **kwargs)


class FakeEA(threading.Thread):
    """Plays the EA side of the contract: drain ``in/``, write ``out/``."""

    def __init__(self, bridge: FileBridge, handler):
        super().__init__(daemon=True)
        self.bridge = bridge
        self.handler = handler
        self.seen: list[dict] = []
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            for req in sorted(self.bridge.inbox.glob("*.json")):
                try:
                    payload = json.loads(req.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                self.seen.append(payload)
                response = self.handler(payload)
                (self.bridge.outbox / req.name).write_text(
                    json.dumps(response), encoding="utf-8"
                )
                req.unlink(missing_ok=True)
            time.sleep(0.01)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=1)


def test_status_without_heartbeat(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.ensure_dirs()
    status = bridge.status()
    assert status["ea_connected"] is False
    assert status["transport"] == "file"
    assert "status.json" in status["reason"]


def test_status_fresh_and_stale(tmp_path):
    bridge = make_bridge(tmp_path, stale_after=5.0)
    bridge.ensure_dirs()

    bridge.status_file.write_text(
        json.dumps({"alive": True, "updated_at": time.time(), "symbol": "XAUUSD"}),
        encoding="utf-8",
    )
    fresh = bridge.status()
    assert fresh["ea_connected"] is True
    assert fresh["ea_hello"]["symbol"] == "XAUUSD"

    bridge.status_file.write_text(
        json.dumps({"alive": True, "updated_at": time.time() - 60}), encoding="utf-8"
    )
    stale = bridge.status()
    assert stale["ea_connected"] is False
    assert "stale" in stale["reason"]


def test_request_roundtrip(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.ensure_dirs()
    ea = FakeEA(bridge, lambda payload: {"id": payload["id"], "ok": True, "data": {"pong": True}})
    ea.start()
    try:
        assert bridge.request("ping") == {"pong": True}
    finally:
        ea.stop()

    assert ea.seen[0]["cmd"] == "ping"
    assert list(bridge.inbox.glob("*.json")) == []
    assert list(bridge.outbox.glob("*.json")) == []


def test_params_reach_the_ea(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.ensure_dirs()
    ea = FakeEA(bridge, lambda payload: {"id": payload["id"], "ok": True, "data": payload["params"]})
    ea.start()
    try:
        data = bridge.request("rates", symbol="XAUUSD", timeframe="H1", count=50)
    finally:
        ea.stop()
    assert data == {"symbol": "XAUUSD", "timeframe": "H1", "count": 50}


def test_error_response_raises(tmp_path):
    bridge = make_bridge(tmp_path)
    bridge.ensure_dirs()
    ea = FakeEA(
        bridge,
        lambda payload: {
            "id": payload["id"],
            "ok": False,
            "error": {"code": "unknown_symbol", "message": "no such symbol"},
        },
    )
    ea.start()
    try:
        with pytest.raises(BridgeError) as info:
            bridge.request("symbol_info", symbol="NOPE")
    finally:
        ea.stop()
    assert info.value.code == "unknown_symbol"
    assert "no such symbol" in str(info.value)


def test_timeout_when_no_ea_and_inbox_is_cleaned(tmp_path):
    bridge = make_bridge(tmp_path, timeout=0.3)
    bridge.ensure_dirs()
    with pytest.raises(BridgeError) as info:
        bridge.request("ping")
    assert info.value.code == "ea_timeout"
    assert "no answer" in str(info.value)
    # a stale request must not be left behind for a late-arriving EA
    assert list(bridge.inbox.glob("*.json")) == []


def test_missing_files_dir_is_explicit(tmp_path):
    bridge = FileBridge(tmp_path / "does-not-exist", timeout=0.2)
    with pytest.raises(BridgeError) as info:
        bridge.request("ping")
    assert info.value.code == "files_dir_missing"


def test_partial_json_response_is_retried(tmp_path):
    bridge = make_bridge(tmp_path, timeout=2.0)
    bridge.ensure_dirs()

    def handler(payload):
        return {"id": payload["id"], "ok": True, "data": {"late": True}}

    ea = FakeEA(bridge, handler)
    # Simulate a torn write: the first file content is truncated on purpose.
    original = ea.handler

    def torn(payload):
        req = bridge.outbox / f"{payload['id']}.json"
        req.write_text('{"id": "' + payload["id"] + '", "ok": tr', encoding="utf-8")
        time.sleep(0.05)
        return original(payload)

    ea.handler = torn
    ea.start()
    try:
        assert bridge.request("ping") == {"late": True}
    finally:
        ea.stop()
