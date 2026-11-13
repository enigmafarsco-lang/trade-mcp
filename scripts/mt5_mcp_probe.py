#!/usr/bin/env python3
"""Talk to MetaTrader 5's built-in MCP servers and print what they offer.

Usage (no third-party packages needed):

    .venv/bin/python scripts/mt5_mcp_probe.py                  # terminal server
    .venv/bin/python scripts/mt5_mcp_probe.py --dump tools.json
    .venv/bin/python scripts/mt5_mcp_probe.py \
        --url https://www.metatrader.com/mcp --url http://127.0.0.1:22346/mcp

MT5 hands out a different API key per server, so name the endpoints and give a
key for each:

    .venv/bin/python scripts/mt5_mcp_probe.py \
        --url terminal=http://127.0.0.1:22346/mcp \
        --token terminal=<terminal key>

The key is also read from $MT5_MCP_TOKEN (default) or $MT5_MCP_TOKEN_<NAME>,
and is asked for with a hidden prompt only when it is still missing.  It is
never printed, so the output is safe to paste.

Useful switches:

    --call TOOL --args '{"symbol": "XAUUSD"}'   call one tool and show the result
    --dump tools.json                           write the full tool schemas
    --timeout 40                                raise the socket timeout
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trade_mcp.mcpclient import (  # noqa: E402
    McpError,
    McpHttpClient,
    is_local_host,
    probe_tcp,
)

TERMINAL_URL = "http://127.0.0.1:22346/mcp"
MARKETDATA_URL = "https://www.metatrader.com/mcp"


def _parse_targets(items: list[str] | None) -> list[tuple[str, str]]:
    """``--url [NAME=]URL`` -> [(name, url)], defaulting to the local terminal."""
    out: list[tuple[str, str]] = []
    for item in items or [f"terminal={TERMINAL_URL}"]:
        head, sep, rest = item.partition("=")
        if sep and head and "/" not in head and ":" not in head:
            name, url = head, rest
        else:
            name, url = "", item
        url = url.strip()
        default_name = "marketdata" if "metatrader.com" in url else (
            "metaeditor" if "22345" in url else "terminal"
        )
        out.append(((name or default_name).strip(), url))
    return out


def _parse_tokens(items: list[str] | None) -> tuple[str, dict[str, str]]:
    """``--token [NAME=]KEY`` (repeatable) -> (bare key, {name: key})."""
    bare = os.environ.get("MT5_MCP_TOKEN", "")
    named: dict[str, str] = {}
    for item in items or []:
        if "=" in item:
            name, key = item.split("=", 1)
            named[name.strip()] = key.strip()
        else:
            bare = item.strip()
    return bare, named


def _short(text: str, width: int = 110) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= width else line[: width - 1] + "…"


def _arg_summary(tool: dict) -> str:
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    bits = []
    for name, spec in props.items():
        kind = (spec or {}).get("type", "any")
        if isinstance(kind, list):
            kind = "|".join(str(k) for k in kind)
        bits.append(f"{name}{'*' if name in required else ''}:{kind}")
    return ", ".join(bits) or "-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect MetaTrader 5 MCP servers")
    parser.add_argument(
        "--url",
        action="append",
        default=None,
        metavar="[NAME=]URL",
        help=f"repeatable; default {TERMINAL_URL}",
    )
    parser.add_argument(
        "--token",
        action="append",
        default=None,
        metavar="[NAME=]KEY",
        help="repeatable; MT5 hands out a different key per server, "
        "so give NAME=KEY for each (or set $MT5_MCP_TOKEN_<NAME>)",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--call", default="", help="tool name to invoke after listing")
    parser.add_argument("--args", default="{}", help="JSON object with the tool arguments")
    parser.add_argument("--dump", default="", help="write the full tool inventory here (JSON)")
    parser.add_argument(
        "--proxy",
        choices=("auto", "never", "env"),
        default="auto",
        help="auto = bypass for 127.0.0.1/localhost (default), never = always bypass, env = honour http_proxy",
    )
    parser.add_argument("--no-list", action="store_true", help="skip tools/list")
    opts = parser.parse_args(argv)

    targets = _parse_targets(opts.url)
    bare_token, named_tokens = _parse_tokens(opts.token)

    def token_for(name: str) -> str:
        env_name = "MT5_MCP_TOKEN_" + name.upper().replace("-", "_").replace(" ", "_")
        return named_tokens.get(name) or os.environ.get(env_name, "") or bare_token

    if not all(token_for(name) for name, _ in targets):
        print("API key:  MT5 -> Tools -> Options -> MCP -> API Key  (per server: terminal / marketdata)")
        print("          tip: --token terminal=<key> --token marketdata=<key>")
        try:
            typed = getpass.getpass("paste the key for the remaining endpoints: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nno key given; the local servers need one", file=sys.stderr)
            typed = ""
        bare_token = bare_token or typed

    try:
        call_args = json.loads(opts.args or "{}")
    except json.JSONDecodeError as exc:
        print(f"--args is not valid JSON: {exc}", file=sys.stderr)
        return 2

    inventory: dict[str, dict] = {}
    failures = 0

    for name, url in targets:
        token = token_for(name)
        print("=" * 78)
        print(f"endpoint : {url}   [{name}]")
        print(f"key      : {'given' if token else 'MISSING'}")
        bypass = {"auto": None, "never": True, "env": False}[opts.proxy]
        local = is_local_host(url)
        if bypass is None:
            state = "bypassed (local endpoint)" if local else "environment setting"
        else:
            state = "bypassed (forced)" if bypass else "honoured from environment"
        print(f"proxy    : {state}")
        for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            value = os.environ.get(var)
            if value:
                print(f"           ${var}={value}")

        blocked = probe_tcp(url)
        if blocked:
            print(f"  [FAIL] {blocked}")
            if "22346" in url:
                print("         - is MetaTrader 5 running?  pgrep -af terminal64")
                print("         - is the internal server enabled?  Tools > Options > MCP")
            failures += 1
            continue

        client = McpHttpClient(url, token, timeout=opts.timeout, bypass_proxy=bypass)
        try:
            info = client.initialize()
        except McpError as exc:
            print(f"  [FAIL] {exc}")
            if exc.status in {401, 403} and len(targets) > 1:
                print("         - MT5 gives each server its own key:  --token terminal=<key>")
                print("         - the metaeditor key (22345) does NOT work on the terminal (22346)")
            if exc.status == 503 and local:
                print("         - try:  --proxy never   (some HTTP proxies answer 503 for local URLs)")
                print("         - and make sure the 22346 endpoint is enabled in Tools > Options > MCP")
            failures += 1
            continue

        server = (info.get("serverInfo") or {}) if isinstance(info, dict) else {}
        print(f"  [ OK ] handshake, protocol={(info or {}).get('protocolVersion')}")
        print(f"         server   : {server.get('name')} {server.get('version')}")
        print(f"         session  : {'yes' if client.session_id else 'none'}")
        if client.server_header:
            print(f"         http     : {client.server_header}")
        caps = (info or {}).get("capabilities") or {}
        print(f"         caps     : {', '.join(sorted(caps)) or '-'}")

        tools: list[dict] = []
        if not opts.no_list:
            try:
                tools = client.list_tools()
            except McpError as exc:
                print(f"  [FAIL] tools/list: {exc}")
                failures += 1

        print(f"  tools    : {len(tools)}")
        for tool in sorted(tools, key=lambda t: str(t.get("name"))):
            name = tool.get("name")
            print(f"    - {name}")
            desc = _short(tool.get("description") or "")
            if desc:
                print(f"        {desc}")
            print(f"        args: {_arg_summary(tool)}")
        inventory[url] = {"serverInfo": server, "tools": tools}

        if opts.call and tools:
            try:
                result = client.call_tool(opts.call, call_args)
            except McpError as exc:
                print(f"  [FAIL] tools/call {opts.call}: {exc}")
                failures += 1
            else:
                print("-" * 78)
                print(f"  result of {opts.call} {json.dumps(call_args, ensure_ascii=False)}:")
                body = McpHttpClient.structured_content(result)
                if body is None:
                    body = result
                text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=2)
                print(text[:4000])
                if result.get("isError"):
                    print("  (the server flagged this call as an error)")

    if opts.dump:
        Path(opts.dump).write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("=" * 78)
        print(f"full inventory written to {opts.dump}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
