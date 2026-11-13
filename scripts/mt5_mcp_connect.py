#!/usr/bin/env python3
"""Read MT5's Copy configuration locally. Never print/store keys or paste into Bash."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse


def parse_terminal(text: str) -> tuple[str, str]:
    """Extract only terminal, never silently substitute metaeditor/marketdata."""
    config = None
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{', text):
        try:
            candidate, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get('mcpServers'), dict):
            config = candidate['mcpServers'].get('terminal')
            break
    if config is not None:
        if not isinstance(config, dict):
            raise ValueError('Invalid terminal configuration.')
        url = config.get('url', '')
        headers = config.get('headers') or {}
        auth = next((v for k, v in headers.items() if k.lower() == 'authorization'), '')
    else:
        section = re.search(r'^\s*\[mcp_servers\.terminal\]\s*$(.*?)(?=^\s*\[|\Z)', text, re.M | re.S)
        if not section:
            raise ValueError('No terminal section found. Use the MT5 MCP Copy button, not the API Key field.')
        body = section.group(1)
        def quoted(pattern):
            m = re.search(pattern, body, re.M | re.I)
            return m.group(1) if m else ''
        url = quoted(r'''^\s*url\s*=\s*["']([^"']+)["']''')
        auth = quoted(r'''["']Authorization["']\s*=\s*["']([^"']+)["']''')
    if not isinstance(url, str) or not isinstance(auth, str):
        raise ValueError('Invalid terminal URL or authorization header.')
    parsed = urlparse(url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.username or parsed.password:
        raise ValueError('Only a local HTTP terminal endpoint is accepted.')
    if not auth.lower().startswith('bearer '):
        raise ValueError('Missing terminal Bearer authorization header.')
    token = auth[7:].strip()
    if len(token) < 12 or any(c.isspace() or c in '\"\'{}[]' for c in token):
        raise ValueError('Invalid terminal key. Copy the saved MCP configuration again.')
    return url, token


def read_clipboard() -> str:
    commands = []
    if os.environ.get('WAYLAND_DISPLAY'):
        commands.append(['wl-paste', '--no-newline'])
    commands.extend([['xclip', '-selection', 'clipboard', '-o'], ['xsel', '--clipboard', '--output']])
    for command in commands:
        if not shutil.which(command[0]):
            continue
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()
    except Exception:
        raise ValueError('Cannot read clipboard. On Ubuntu/X11 install xclip: sudo apt install xclip') from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--console', action='store_true', help='start read-only console after doctor succeeds')
    args = parser.parse_args(argv)
    if sys.stdin.isatty():
        print('Open MT5 > Tools > Options > MCP and click Copy now.')
        print('Return here and press Enter only. Do NOT paste the configuration.')
        try:
            input('Press Enter after copying from MT5: ')
        except (EOFError, KeyboardInterrupt):
            print('Cancelled.')
            return 2
    try:
        url, token = parse_terminal(read_clipboard())
    except (ValueError, TypeError, AttributeError):
        # Clipboard content and parser exceptions can contain secrets. Do not log them.
        print('Cannot read a valid local terminal configuration from the clipboard.')
        print('Use MT5 > Tools > Options > MCP > Copy. Do not paste it into Bash or chat.')
        print('On Ubuntu/X11, install a clipboard reader if needed: sudo apt install xclip')
        return 2
    env = os.environ.copy()
    env.update(TRADE_MCP_BACKEND='native', TRADE_MCP_MCP_URL=url,
               TRADE_MCP_MCP_TOKEN=token, TRADE_MCP_ALLOW_TRADING='false',
               TRADE_MCP_TRAINING_BUDGET='100')
    # doctor normally prints a partial key; run it with redacted output here.
    print('Testing the terminal configuration copied from MT5. Trading is disabled.', flush=True)
    result = subprocess.run([sys.executable, '-m', 'trade_mcp', 'doctor', '--wait', '0'],
                            env=env, capture_output=True, text=True)
    for line in (result.stdout + result.stderr).splitlines():
        if 'mcp key' in line:
            print('  mcp key     : loaded from clipboard (hidden)')
        else:
            print(line.replace(token, '[REDACTED]'))
    if result.returncode:
        print('Console not started. If HTTP 401 persists, the saved terminal key was rejected by the running server.')
        return result.returncode
    if args.console:
        return subprocess.call([sys.executable, '-m', 'trade_mcp', 'console', '--no-browser'], env=env)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
