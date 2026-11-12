"""Common bridge types."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class BridgeError(RuntimeError):
    """Raised when the EA cannot be reached, times out, or reports an error."""

    def __init__(self, message: str, code: str = "bridge_error", details: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out


@runtime_checkable
class Bridge(Protocol):
    """Everything the MCP tools need from the MT5 connection."""

    def request(self, cmd: str, **params: Any) -> Any: ...

    def status(self) -> dict[str, Any]: ...

    def close(self) -> None: ...
