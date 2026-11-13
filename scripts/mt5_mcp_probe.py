#!/usr/bin/env python3
"""Talk to MetaTrader 5's built-in MCP servers and print what they offer.

Usage (no third-party packages needed):

    .venv/bin/python scripts/mt5_mcp_probe.py                  # terminal server
    .venv/bin/python scripts/mt5_mcp_probe.py --dump tools.json
    .venv/bin/python scripts/mt5_mcp_probe.py \
        --url https://www.metatrader.com/mcp --url http://127.0.0.1:22346/mcp

The API key is asked for with a hidden prompt (or taken from $MT5_MCP_TOKEN).
Nothing about the key is ever printed, so the output is safe to paste.

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

from trade_mcp.mcpclient import McpError, McpHttpClient, probe_tcp  # noqa: E402

TERMINAL_URL = "http://127.0.0.1:22346/mcp"
MARKETDATA_URL = "https://www.metatrader.com/mcp"


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
    parser.add_argument("--url", action="append", default=None, help=f"repeatable, default {TERMINAL_URL}")
    parser.add_argument("--token", default=os.environ.get("MT5_MCP_TOKEN", ""))
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--call", default="", help="tool name to invoke after listing")
    parser.add_argument("--args", default="{}", help="JSON object with the tool arguments")
    parser.add_argument("--dump", default="", help="write the full tool inventory here (JSON)")
    parser.add_argument("--no-list", action="store_true", help="skip tools/list")
    opts = parser.parse_args(argv)

    urls = opts.url or [TERMINAL_URL]
    token = opts.token
    if not token:
        print("API key:  MT5 -> Tools -> Options -> MCP -> API Key (Copy)")
        try:
            token = getpass.getpass("paste it (input is hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nno key given; local servers need one", file=sys.stderr)
            token = ""

    try:
        call_args = json.loads(opts.args or "{}")
    except json.JSONDecodeError as exc:
        print(f"--args is not valid JSON: {exc}", file=sys.stderr)
        return 2

    inventory: dict[str, dict] = {}
    failures = 0

    for url in urls:
        print("=" * 78)
        print(f"endpoint : {url}")
        blocked = probe_tcp(url)
        if blocked:
            print(f"  [FAIL] {blocked}")
            if "22346" in url:
                print("         - is MetaTrader 5 running?  pgrep -af terminal64")
                print("         - is the internal server enabled?  Tools > Options > MCP")
            failures += 1
            continue

        client = McpHttpClient(url, token, timeout=opts.timeout)
        try:
            info = client.initialize()
        except McpError as exc:
            print(f"  [FAIL] {exc}")
            failures += 1
            continue

        server = (info.get("serverInfo") or {}) if isinstance(info, dict) else {}
        print(f"  [ OK ] handshake, protocol={(info or {}).get('protocolVersion')}")
        print(f"         server   : {server.get('name')} {server.get('version')}")
        print(f"         session  : {'yes' if client.session_id else 'none'}")
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
