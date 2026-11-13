#!/usr/bin/env python3
"""Dump the *shape* of the answers of MetaTrader's read-only MCP tools.

The native backend (``TRADE_MCP_BACKEND=native``) has to read fields out of
payloads whose schema MT5 does not publish.  This script prints the keys and
scalar values of each read-only tool answer, which is all we need to check or
correct the mapping in ``src/trade_mcp/bridge/native.py`` - and it never calls
a trading tool.

    read -rsp 'terminal key: ' TK; echo
    .venv/bin/python scripts/mt5_mcp_snapshot.py --token "terminal=$TK"
    unset TK

Use ``--json FILE`` to keep the full payloads for offline work.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mt5_mcp_probe import _parse_targets, _parse_tokens, _short  # noqa: E402

from trade_mcp.mcpclient import McpError, McpHttpClient, is_local_host  # noqa: E402

TERMINAL_URL = "http://127.0.0.1:22346/mcp"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _tree(value, depth: int = 0, max_depth: int = 3, limit: int = 30) -> list[str]:
    lines: list[str] = []
    if isinstance(value, dict):
        for index, (key, item) in enumerate(value.items()):
            if index >= limit:
                lines.append("  " * depth + "...")
                break
            if isinstance(item, (dict, list)):
                size = len(item)
                lines.append("  " * depth + f"{key}: {type(item).__name__}({size})")
                if depth < max_depth:
                    lines.extend(_tree(item, depth + 1, max_depth, limit))
            else:
                lines.append("  " * depth + f"{key} = {_short(str(item), 60)}")
    elif isinstance(value, list):
        if not value:
            lines.append("  " * depth + "(empty list)")
        else:
            lines.append("  " * depth + f"[0] of {len(value)}")
            lines.extend(_tree(value[0], depth + 1, max_depth, limit))
    return lines


def _read_calls(symbol: str) -> list[tuple[str, dict]]:
    now = datetime.now(timezone.utc)
    return [
        ("terminal_get_info", {}),
        ("get_time_information", {}),
        ("get_trading_account_info", {}),
        ("get_marketwatch_symbols", {"symbol": symbol}),
        ("get_marketwatch_symbols", {"limit": 5}),
        (
            "get_chart_history",
            {
                "datetime_from": _iso(now - timedelta(days=3)),
                "datetime_to": _iso(now),
                "symbol": symbol,
                "period": "M15",
                "limit": 3,
            },
        ),
        ("get_trading_open_positions", {}),
        (
            "get_trading_history_orders",
            {
                "datetime_from": _iso(now - timedelta(days=30)),
                "datetime_to": _iso(now),
                "include_orders": True,
                "include_deals": True,
                "limit": 5,
            },
        ),
        ("list_open_charts", {}),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the payload shape of MT5's MCP tools")
    parser.add_argument("--url", action="append", default=None, metavar="[NAME=]URL")
    parser.add_argument("--token", action="append", default=None, metavar="[NAME=]KEY")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--json", default="", help="write the full payloads to this file")
    parser.add_argument("--tool", default="", help="only this tool")
    parser.add_argument("--args", default="{}", help="arguments for --tool (JSON object)")
    opts = parser.parse_args(argv)

    targets = _parse_targets(opts.url)
    bare_token, named_tokens = _parse_tokens(opts.token)
    name = targets[0][0]
    url = targets[0][1] if opts.url else TERMINAL_URL

    def token_for(label: str) -> str:
        env_name = "MT5_MCP_TOKEN_" + label.upper().replace("-", "_").replace(" ", "_")
        return named_tokens.get(label) or os.environ.get(env_name, "") or bare_token

    token = token_for(name)
    if not token:
        print("key  : MT5 -> Tools -> Options -> MCP -> the 'terminal' endpoint -> Copy")
        try:
            token = getpass.getpass("paste the terminal key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nno key given", file=sys.stderr)
            return 2
    if not token:
        print("no key given", file=sys.stderr)
        return 2

    calls = _read_calls(opts.symbol)
    if opts.tool:
        try:
            args = json.loads(opts.args or "{}")
        except json.JSONDecodeError as exc:
            print(f"--args is not valid JSON: {exc}", file=sys.stderr)
            return 2
        calls = [(opts.tool, args)]

    client = McpHttpClient(url, token, timeout=opts.timeout, bypass_proxy=True if is_local_host(url) else None)
    try:
        client.connect()
    except McpError as exc:
        print(f"[FAIL] handshake with {url}: {exc}", file=sys.stderr)
        if exc.status == 401:
            print("       401 => this is another endpoint's key (terminal != metaeditor)", file=sys.stderr)
        elif exc.status == 503:
            print("       503 => endpoint disabled, or the environment proxy is in the way", file=sys.stderr)
        return 1
    server = client.server_info or {}
    print(f"server : {server.get('name')} {server.get('version')}".rstrip())
    print(f"url    : {url}")
    print("")

    dump: dict[str, object] = {}
    failures = 0
    for tool, args in calls:
        label = tool if not args else f"{tool}({', '.join(f'{k}' for k in args)})"
        try:
            result = client.call_tool(tool, args)
        except McpError as exc:
            print(f"--- {label}\n    [FAIL] {_short(str(exc), 120)}")
            failures += 1
            continue
        if result.get("isError"):
            print(f"--- {label}\n    [FAIL] {_short(client.text_content(result) or 'tool error', 120)}")
            failures += 1
            continue
        body = client.structured_content(result)
        if body is None:
            text = client.text_content(result)
            try:
                body = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                body = {"text": text}
        dump[label] = body
        print(f"--- {label}")
        for line in _tree(body):
            print("    " + line)
        print("")

    if opts.json:
        Path(opts.json).expanduser().write_text(json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"full payloads: {opts.json}")
    if failures:
        print(f"{failures} tool call(s) failed - the rest is above")
        return 1
    print("done - no trading tool was called")
    return 0


if __name__ == "__main__":
    sys.exit(main())
