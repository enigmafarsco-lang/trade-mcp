"""File transport: MetaTrader 5 <-> Python through ``MQL5/Files``.

Why this exists
---------------
MT5 gates ``SocketConnect``/``WebRequest`` behind an allow-list that lives in
the terminal's security settings.  On newer builds that list is kept in an
encrypted config file, and under Wine the settings dialog is easy to lose - so
the socket transport can be blocked with ``error 4014`` no matter what you write
into ``Config/common.ini``.

File I/O has no such gate: an Expert Advisor may always read and write inside
its own ``MQL5/Files`` sandbox.  This transport uses that.

Wire format (same JSON contract as the socket bridge)
-----------------------------------------------------
``MQL5/Files/trade-mcp/in/<id>.json``     Python -> EA  request
``MQL5/Files/trade-mcp/out/<id>.json``    EA -> Python  response
``MQL5/Files/trade-mcp/status.json``      EA heartbeat

The EA polls ``in/`` every few hundred milliseconds, and drops a heartbeat into
``status.json`` roughly once a second, which is how ``mt5_status`` and
``trade-mcp doctor`` know that MetaTrader is alive.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .base import BridgeError

DEFAULT_EA_DIR = "trade-mcp"
DEFAULT_POLL = 0.02
DEFAULT_STALE_AFTER = 8.0
PARSE_GRACE = 0.75  # keep re-reading for this long if the JSON is truncated


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def discover_files_dirs(extra_roots: list[str] | None = None) -> list[Path]:
    """Best-effort search for ``MQL5/Files`` directories of Wine/Windows MT5 installs.

    Returns the writable-looking candidates, newest first.  This is only used to
    fill in ``TRADE_MCP_FILES_DIR`` when the user has not set it.
    """
    roots: list[Path] = []
    for raw in extra_roots or []:
        roots.append(Path(raw).expanduser())
    if os.environ.get("WINEPREFIX"):
        roots.append(Path(os.environ["WINEPREFIX"]).expanduser())
    roots.append(Path.home() / ".wine")
    for base in ("/media", "/mnt", "/opt", "/run/media"):
        p = Path(base)
        if p.is_dir():
            roots.append(p)

    found: list[tuple[float, Path]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        try:
            walker = root.glob("**/MQL5/Files")
            for candidate in walker:
                if not candidate.is_dir():
                    continue
                key = str(candidate)
                if key in seen:
                    continue
                seen.add(key)
                # An MT5 sandbox always has a sibling Experts folder.
                if not (candidate.parent / "Experts").is_dir():
                    continue
                try:
                    mtime = candidate.stat().st_mtime
                except OSError:
                    continue
                found.append((mtime, candidate))
        except (OSError, RuntimeError):
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found]


class FileBridge:
    """Drop-in replacement for :class:`trade_mcp.bridge.server.BridgeServer`."""

    def __init__(
        self,
        files_dir: str | os.PathLike[str],
        *,
        ea_dir: str = DEFAULT_EA_DIR,
        timeout: float = 20.0,
        poll: float = DEFAULT_POLL,
        stale_after: float = DEFAULT_STALE_AFTER,
    ):
        self.files_dir = Path(files_dir).expanduser()
        self.root = self.files_dir / ea_dir
        self.inbox = self.root / "in"
        self.outbox = self.root / "out"
        self.status_file = self.root / "status.json"
        self.timeout = float(timeout)
        self.poll = max(float(poll), 0.005)
        self.stale_after = float(stale_after)
        self._requests = 0
        self._errors = 0
        self._last_id: str | None = None

    # ------------------------------------------------------------------ setup
    def ensure_dirs(self) -> None:
        if not self.files_dir.is_dir():
            raise BridgeError(
                f"MT5 files directory not found: {self.files_dir}. "
                "Point TRADE_MCP_FILES_DIR at the terminal's MQL5/Files folder "
                "(Wine: <prefix>/drive_c/Program Files/MetaTrader 5/MQL5/Files).",
                code="files_dir_missing",
            )
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)

    @property
    def connected_hint(self) -> str:
        return f"file:{self.root}"

    # ---------------------------------------------------------------- statuses
    def _read_status(self) -> dict[str, Any] | None:
        for _ in range(3):  # the EA may be mid-write
            try:
                raw = self.status_file.read_text(encoding="utf-8-sig", errors="replace")
            except (FileNotFoundError, OSError):
                return None
            raw = raw.strip()
            if not raw:
                time.sleep(0.02)
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                time.sleep(0.02)
                continue
            if isinstance(data, dict):
                return data
        return None

    def status(self) -> dict[str, Any]:
        data = self._read_status()
        base: dict[str, Any] = {
            "transport": "file",
            "files_dir": str(self.files_dir),
            "ea_dir": str(self.root),
            "ea_connected": False,
            "ea_peer": self.connected_hint,
            "ea_hello": None,
            "pending_requests": len(list(self.inbox.glob("*.json"))) if self.inbox.is_dir() else 0,
            "requests": self._requests,
            "errors": self._errors,
        }
        if not data:
            base["reason"] = "no status.json yet - is TradeMCPFile.mq5 attached to a chart?"
            return base

        updated = float(data.get("updated_at") or 0)
        age = None
        if updated:
            age = time.time() - updated
            if age < -5.0:
                age = None  # EA clock is ahead of ours: trust the file mtime
            else:
                age = max(0.0, age)
        # The EA stamps ``updated_at`` with its own clock.  When that stamp is
        # not plausible (see above) fall back to the file's mtime, which the
        # host kernel writes.
        if age is None:
            with_status: dict[str, Any] = {}
            try:
                with_status["mtime_age"] = time.time() - self.status_file.stat().st_mtime
            except OSError:
                with_status["mtime_age"] = None
            age = with_status["mtime_age"]

        alive = bool(data.get("alive", True))
        connected = bool(alive and age is not None and age <= self.stale_after)
        base.update(
            {
                "ea_connected": connected,
                "age_seconds": None if age is None else round(age, 2),
                "ea_hello": data,
            }
        )
        if not connected:
            seconds = age or 0
            base["reason"] = (
                f"status.json is stale ({seconds:.0f}s old) - terminal closed, EA removed, "
                "or algorithmic trading disabled"
            )
        return base

    # --------------------------------------------------------------- requests
    def request(self, cmd: str, **params: Any) -> Any:
        self.ensure_dirs()
        req_id = uuid.uuid4().hex[:12]
        self._last_id = req_id
        payload = {"id": req_id, "cmd": cmd, "params": params}
        req_path = self.inbox / f"{req_id}.json"
        res_path = self.outbox / f"{req_id}.json"

        _atomic_write(req_path, json.dumps(payload, ensure_ascii=False))
        self._requests += 1

        deadline = time.monotonic() + self.timeout
        first_seen: float | None = None
        data: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                text = res_path.read_text(encoding="utf-8-sig", errors="replace")
            except (FileNotFoundError, OSError):
                time.sleep(self.poll)
                continue
            text = text.strip()
            if not text:
                time.sleep(self.poll)
                continue
            if first_seen is None:
                first_seen = time.monotonic()
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                # A partially flushed file: retry briefly, then give up.
                if time.monotonic() - first_seen > PARSE_GRACE:
                    self._errors += 1
                    self._cleanup(req_path, res_path)
                    raise BridgeError(
                        "EA wrote a response that is not valid JSON", code="bad_response"
                    ) from None
                time.sleep(self.poll)
                continue
            if isinstance(parsed, dict):
                data = parsed
                break
            time.sleep(self.poll)

        if data is None:
            self._errors += 1
            self._cleanup(req_path, res_path)
            status = self.status()
            extra = ""
            if not status.get("ea_connected"):
                extra = f" ({status.get('reason') or 'no heartbeat from the EA'})"
            elif status.get("pending_requests", 0) > 0:
                extra = " (the EA heartbeat is alive but it is not draining the inbox)"
            raise BridgeError(
                f"no answer for '{cmd}' within {self.timeout:.0f}s{extra}. "
                "Is TradeMCPFile.mq5 attached to a chart with algorithmic trading on?",
                code="ea_timeout",
            )

        self._cleanup(req_path, res_path)
        if not data.get("ok", False):
            err = data.get("error") or {}
            if isinstance(err, dict):
                code = str(err.get("code") or "ea_error")
                message = str(err.get("message") or "the EA reported an error")
            else:  # pragma: no cover - defensive
                code, message = "ea_error", str(err)
            raise BridgeError(message, code=code)
        return data.get("data")

    def _cleanup(self, req_path: Path, res_path: Path) -> None:
        for path in (req_path, res_path):
            with contextlib.suppress(OSError):
                path.unlink()

    # ------------------------------------------------------------------ other
    def close(self) -> None:
        """Nothing persistent to release: files are the transport."""

    def wait_for_ea(self, timeout: float = 60.0, interval: float = 0.5) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout)
        status = self.status()
        while time.monotonic() < deadline and not status.get("ea_connected"):
            time.sleep(interval)
            status = self.status()
        return status
