#!/usr/bin/env python3
"""Read MT5's Copy configuration locally. Never print/store keys or paste into Bash."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
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


def configure_llm(env: dict[str, str]) -> bool:
    """Prompt locally and verify a real response; do not persist or print secrets."""
    from trade_mcp.agents import LLMClient

    try:
        base = input('OpenAI-compatible base URL (include /v1 if required): ').strip()
        model = input('Exact model ID from your provider: ').strip()
        parsed = urlparse(base)
        local = parsed.hostname in {'localhost', '127.0.0.1', '::1'}
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment or (parsed.scheme != 'https' and not (local and parsed.scheme == 'http'))):
            print('Use HTTPS for remote providers or HTTP for a local model. No credentials in the URL.')
            return False
        if not model:
            print('A model ID is required.')
            return False
        key = getpass.getpass('Model API key (hidden; Enter for a keyless local server): ').strip()
        print('Testing one model request. Provider usage charges may apply.', flush=True)
        client = LLMClient(base_url=base, api_key=key, model=model, timeout=30)
        response = client.chat([{'role': 'user', 'content': 'Reply with OK only.'}])
        if not response:
            print('Model returned an empty response. Console not started.')
            return False
    except (EOFError, KeyboardInterrupt):
        print('Model setup cancelled.')
        return False
    except Exception:
        # Provider errors may echo credentials, request bodies or account information.
        print('Model test failed. Check the provider URL, model ID, key, credit and network. Console not started.')
        return False
    env.update(TRADE_MCP_LLM_BASE_URL=base, TRADE_MCP_LLM_MODEL=model, TRADE_MCP_LLM_API_KEY=key)
    print('Model response verified. Settings stay in this process only; agent JSON decisions can still fail validation.')
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-browser', action='store_true', help='do not open the console in a browser')
    parser.add_argument('--console', action='store_true', help='start read-only console after doctor succeeds')
    parser.add_argument('--enable-demo-trading', action='store_true',
                        help='enable execution only after a fresh demo-account check; requires --console')
    parser.add_argument('--setup-llm', action='store_true',
                        help='prompt for an OpenAI-compatible model and verify a response; no keys saved')
    parser.add_argument('--opencode', action='store_true',
                        help='use a local OpenCode v1 server, starting it if needed')
    parser.add_argument('--opencode-model', default='', help='optional provider/model ID')
    args = parser.parse_args(argv)
    if args.opencode and (not args.console or args.setup_llm):
        parser.error('--opencode requires --console and cannot be combined with --setup-llm')
    if args.setup_llm and (not args.console or not sys.stdin.isatty()):
        parser.error('--setup-llm requires --console and an interactive terminal')
    model_env = {}
    if args.setup_llm and not configure_llm(model_env):
        return 4
    if args.enable_demo_trading and not args.console:
        parser.error('--enable-demo-trading requires --console')
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
    env.update(model_env)
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
    if args.enable_demo_trading:
        check = subprocess.run([sys.executable, '-c',
            'from trade_mcp.app import build_app; from trade_mcp.config import Config; '
            'a=build_app(Config.from_env()); '
            'account=a.mt5.account_info(fresh=True); a.close(); '
            'raise SystemExit(0 if account.get("trade_mode")=="demo" else 3)'],
            env=env, capture_output=True, text=True)
        if check.returncode:
            print('Demo account could not be verified. Trading remains disabled; console not started.')
            return 3
        env['TRADE_MCP_ALLOW_TRADING'] = 'true'
        print('Demo account verified. Execution enabled; each web order still requires confirmation.', flush=True)
    if args.console:
        owned = None
        if args.opencode:
            from trade_mcp.opencode import OpenCodeClient
            endpoint = 'http://127.0.0.1:4096'
            client = OpenCodeClient(endpoint, password=os.environ.get('OPENCODE_SERVER_PASSWORD', ''), timeout=2)
            try:
                client.health()
            except RuntimeError:
                if not shutil.which('opencode'):
                    print('OpenCode CLI not found. Install it and sign in to your provider first.')
                    return 4
                # The OpenCode process must never inherit the terminal token.
                child_env = {k: v for k, v in os.environ.items()
                             if not k.startswith(('TRADE_MCP_', 'MT5_MCP_'))}
                owned = subprocess.Popen(['opencode', 'serve', '--hostname', '127.0.0.1', '--port', '4096'],
                                         env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                ready = False
                for _ in range(40):
                    if owned.poll() is not None:
                        break
                    try:
                        client.health()
                        ready = True
                        break
                    except RuntimeError:
                        time.sleep(.25)
                if not ready:
                    if owned.poll() is None:
                        owned.terminate()
                        owned.wait(timeout=10)
                    print('OpenCode v1 server did not start. Check port 4096 and your OpenCode installation.')
                    return 4
            env['TRADE_MCP_OPENCODE_URL'] = endpoint
            env['TRADE_MCP_OPENCODE_MODEL'] = args.opencode_model
            print('OpenCode connected. Web agents require human confirmation, including existing auto agents.')
            print('Model credentials stay in OpenCode. Use the web model test to verify provider access.')
        try:
            command = [sys.executable, '-m', 'trade_mcp', 'console']
            if args.no_browser:
                command.append('--no-browser')
            return subprocess.call(command, env=env)
        finally:
            if owned is not None and owned.poll() is None:
                owned.terminate()
                try:
                    owned.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    owned.kill()
                    owned.wait()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
