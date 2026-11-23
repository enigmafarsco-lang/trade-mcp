#!/usr/bin/env python3
"""Inspect possible OpenCode/MT5 config files without running either program.

This is a static, incomplete inspection, not the resolved config of a running
OpenCode instance. Output contains only fixed categories: no config values,
credentials, URLs, file paths, arbitrary server names, or account information.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_FILE_BYTES = 1_000_000
MAX_CONFIGS = 40
MAX_SERVERS = 30
TOKEN_KEYS = ("TRADE_MCP_MCP_TOKEN", "MT5_MCP_TOKEN", "MT5_MCP_TOKEN_TERMINAL")
SAFE_SERVER_NAMES = {"trade", "trade-mcp", "trade_mcp", "mt5", "metatrader", "metatrader5",
                     "terminal", "metaeditor", "marketdata"}
STRING_OR_COMMENT = re.compile(r'("(?:\\.|[^"\\])*")|//[^\r\n]*|/\*[\s\S]*?\*/')
STRING_OR_TRAILING_COMMA = re.compile(r'("(?:\\.|[^"\\])*")|,(?=\s*[}\]])')
REFERENCE = re.compile(r"\{(?:env|file):[^{}]+\}", re.IGNORECASE)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def parse_jsonc(text: str) -> Any:
    # Keep quoted URLs and escaped quotes intact while stripping comments/commas.
    text = STRING_OR_COMMENT.sub(lambda m: m.group(1) if m.group(1) else " ", text)
    text = STRING_OR_TRAILING_COMMA.sub(lambda m: m.group(1) if m.group(1) else "", text)
    return json.loads(text, object_pairs_hook=_unique)


def _read(path: Path) -> tuple[str, str | None]:
    try:
        path = path.resolve(strict=True)
        # In particular, never inspect another process's /proc/.../environ.
        if any(path == Path(p) or Path(p) in path.parents for p in ("/proc", "/sys", "/dev")):
            return "excluded", None
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return "unsupported_file", None
        if info.st_size > MAX_FILE_BYTES:
            return "too_large", None
        return "read", path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "absent", None
    except (OSError, UnicodeError, RuntimeError):
        return "unreadable", None


def _setting(value: Any, allowed: set[str]) -> str:
    if value is None:
        return "unset"
    if isinstance(value, str):
        value = value.strip().lower()
        if value in allowed:
            return value
        if REFERENCE.search(value):
            return "reference"
    return "other"


def _secret_source(value: Any) -> str:
    if value is None:
        return "unset"
    if not isinstance(value, str):
        return "other"
    if not value.strip():
        return "empty"
    return "reference" if REFERENCE.search(value) else "literal"


def _endpoint(value: Any) -> str:
    if value is None:
        return "unset"
    if not isinstance(value, str):
        return "other"
    if REFERENCE.search(value):
        return "reference"
    try:
        url = urlsplit(value)
        if (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
                and not url.username and not url.password and not url.query and not url.fragment
                and url.path == "/mcp"):
            return "loopback_default_terminal" if url.port == 22346 else "loopback_other_port"
    except ValueError:
        pass
    return "other"


def _command(value: Any) -> str:
    if value is None:
        return "unset"
    parts = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    if not parts or not all(isinstance(part, str) for part in parts):
        return "other"
    if any(Path(part).name in {"trade-mcp", "trade_mcp", "mt5_mcp_connect.py"} for part in parts):
        return "trade_mcp_launcher"
    return "other"


def _server(name: Any, value: Any, shape: str) -> dict[str, Any]:
    name = name.lower() if isinstance(name, str) else "other"
    server = value if isinstance(value, dict) else {}
    env = server.get("environment")
    env = env if isinstance(env, dict) else {}
    headers = server.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    auth = [v for k, v in headers.items() if isinstance(k, str) and k.lower() == "authorization"]
    auth_source = "ambiguous" if len(auth) > 1 else _secret_source(auth[0]) if auth else "unset"
    kind = _setting(server.get("type"), {"local", "remote"})
    return {
        "shape": shape,
        "name": name if name in SAFE_SERVER_NAMES else "other",
        "type": kind,
        "command": _command(server.get("command")),
        "endpoint": _endpoint(server.get("url")),
        "backend_in_config": _setting(env.get("TRADE_MCP_BACKEND"), {"native", "mcp", "bridge", "file", "mock"}),
        "trading_in_config": _setting(env.get("TRADE_MCP_ALLOW_TRADING"), {"true", "false", "1", "0", "yes", "no", "on", "off"}),
        "token_in_config": {key: _secret_source(env.get(key)) for key in TOKEN_KEYS},
        "authorization_header": auth_source,
        "enabled": _setting(server.get("enabled"), set()) if not isinstance(server.get("enabled"), bool)
                   else str(server["enabled"]).lower(),
        "disabled": _setting(server.get("disabled"), set()) if not isinstance(server.get("disabled"), bool)
                    else str(server["disabled"]).lower(),
    }


def _config(text: str) -> dict[str, Any]:
    try:
        data = parse_jsonc(text)
        if not isinstance(data, dict):
            raise ValueError("root is not an object")
        if "mcp" not in data:
            return {"status": "inspected", "mcp": "absent", "servers": [], "servers_truncated": False}
        mcp = data["mcp"]
        if not isinstance(mcp, dict):
            raise ValueError("mcp is not an object")
        servers: list[dict[str, Any]] = []
        nested = mcp.get("servers")
        if nested is not None:
            if not isinstance(nested, dict):
                raise ValueError("servers is not an object")
            servers.extend(_server(name, value, "nested") for name, value in nested.items())
        servers.extend(_server(name, value, "direct") for name, value in mcp.items()
                       if name != "servers" and isinstance(value, dict))
        return {"status": "inspected", "mcp": "present", "servers": servers[:MAX_SERVERS],
                "servers_truncated": len(servers) > MAX_SERVERS}
    except (TypeError, ValueError, RecursionError):
        return {"status": "invalid_or_unsupported"}


def _dotenv(path: Path) -> dict[str, Any]:
    status, text = _read(path)
    if status != "read" or text is None:
        return {"status": status}
    # Match the project's basic .env loader without exporting anything.
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key in (*TOKEN_KEYS, "TRADE_MCP_BACKEND", "TRADE_MCP_ALLOW_TRADING"):
            values.setdefault(key, value)
    return {
        "status": "inspected",
        "token_keys_set": {key: bool(values.get(key)) for key in TOKEN_KEYS},
        "backend": _setting(values.get("TRADE_MCP_BACKEND"), {"native", "mcp", "bridge", "file", "mock"}),
        "trading": _setting(values.get("TRADE_MCP_ALLOW_TRADING"), {"true", "false", "1", "0", "yes", "no", "on", "off"}),
    }


def inspect(root: Path, home: Path, environ: Mapping[str, str], managed: Path = Path("/etc/opencode")) -> dict[str, Any]:
    root = root.resolve()
    global_dir = Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"
    locations: list[tuple[str, int | None, bool, Path]] = []
    for ext in ("json", "jsonc"):
        locations.append(("global", None, False, global_dir / f"opencode.{ext}"))
        locations.append(("managed", None, False, managed / f"opencode.{ext}"))
    for depth, parent in enumerate((root, *root.parents)):
        for hidden in (False, True):
            for ext in ("json", "jsonc"):
                base = parent / ".opencode" if hidden else parent
                locations.append(("project" if depth == 0 else "ancestor", depth, hidden,
                                  base / f"opencode.{ext}"))

    found: list[dict[str, Any]] = []
    truncated = False
    for scope, depth, hidden, path in locations:
        status, text = _read(path)
        if status == "absent":
            continue
        if len(found) == MAX_CONFIGS:
            truncated = True
            break
        result = _config(text) if status == "read" and text is not None else {"status": status}
        found.append({"scope": scope, "depth": depth, "hidden_dir": hidden,
                      "format": "jsonc" if path.suffix == ".jsonc" else "json", **result})

    return {
        "status": "inspected",
        "opencode_on_current_path": shutil.which("opencode", path=environ.get("PATH", "")) is not None,
        "opencode_version": "not_checked",
        "config_sources": found,
        "sources_truncated": truncated,
        "custom_config_env_set": bool(environ.get("OPENCODE_CONFIG")),
        "custom_config_dir_env_set": bool(environ.get("OPENCODE_CONFIG_DIR")),
        "inline_config_env_set": bool(environ.get("OPENCODE_CONFIG_CONTENT")),
        "shell_token_keys_set": {key: bool(environ.get(key)) for key in TOKEN_KEYS},
        "shell_backend": _setting(environ.get("TRADE_MCP_BACKEND"), {"native", "mcp", "bridge", "file", "mock"}),
        "shell_trading": _setting(environ.get("TRADE_MCP_ALLOW_TRADING"), {"true", "false", "1", "0", "yes", "no", "on", "off"}),
        "dotenv_skipped_by_shell": bool(environ.get("TRADE_MCP_SKIP_DOTENV")),
        "project_dotenv": _dotenv(root / ".env"),
        "effective_opencode_config": "not_checked",
        "running_opencode": "not_checked",
        "new_network_calls": 0,
    }


def main() -> int:
    try:
        result = inspect(Path.cwd(), Path.home(), os.environ)
    except Exception:
        # Never surface an exception that might contain a private path or value.
        result = {"status": "blocked", "reason": "static_inspection_unavailable", "new_network_calls": 0}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["status"] == "inspected" else 1


if __name__ == "__main__":
    raise SystemExit(main())
