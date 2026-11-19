"""Start a read-only web console from an already authenticated MCP application.

No clipboard, credential files or process environments are searched. Credentials
come only from the active app configuration and stay in the child's environment.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

from .opencode import NoRedirect


def journal_identity(path):
    return hashlib.sha256(str(Path(path).expanduser().resolve()).encode()).hexdigest()


def workspace_identity(cfg):
    # The model ID is intentionally mutable from the web console
    # (/api/model-config) for this run only. Base URL, billing label and usage
    # convention are environment-level settings and are part of the identity.
    settings = [cfg.research_python, journal_identity(cfg.journal_path), cfg.mcp_url,
                cfg.llm_base_url, cfg.llm_billing_project, cfg.llm_usage_convention, cfg.llm_fast_advisory,
                cfg.default_symbol, cfg.timeframes, cfg.training_budget, cfg.demo_cash_policy,
                cfg.max_lot, cfg.max_risk_percent, cfg.max_open_positions, cfg.max_spread_points,
                cfg.allowed_symbols, cfg.proposal_ttl, cfg.deviation_points]
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def console_environment(cfg):
    env = os.environ.copy()
    # The web console must not inherit the development chat's OpenCode
    # credentials or an implicit fallback path. Only the dedicated direct
    # provider settings are handed to the child.
    for key in ('OPENCODE_URL', 'OPENCODE_MODEL', 'OPENCODE_VARIANT',
                'OPENCODE_FREE_FALLBACK', 'OPENCODE_PASSWORD'):
        env.pop('TRADE_MCP_' + key, None)
        env.pop(key, None)
    values = {
        'RESEARCH_PYTHON': cfg.research_python,
        'WEB_MODEL_BACKEND': cfg.web_model_backend,
        'BACKEND': 'native', 'MCP_URL': cfg.mcp_url, 'MCP_TOKEN': cfg.mcp_token,
        'LLM_BASE_URL': cfg.llm_base_url, 'LLM_MODEL': cfg.llm_model,
        'LLM_API_KEY': cfg.llm_api_key, 'LLM_BILLING_PROJECT': cfg.llm_billing_project,
        'LLM_USAGE_CONVENTION': cfg.llm_usage_convention,
        'LLM_FAST_ADVISORY': 'true' if cfg.llm_fast_advisory else 'false',
        'DEMO_CASH_POLICY': 'true' if cfg.demo_cash_policy else 'false',
        'ALLOW_TRADING': 'false',
        'JOURNAL': cfg.journal_path, 'SYMBOL': cfg.default_symbol,
        'TIMEFRAMES': ','.join(cfg.timeframes), 'TRAINING_BUDGET': cfg.training_budget,
        'MAX_LOT': cfg.max_lot, 'MAX_RISK_PERCENT': cfg.max_risk_percent,
        'MAX_OPEN_POSITIONS': cfg.max_open_positions, 'MAX_SPREAD_POINTS': cfg.max_spread_points,
        'ALLOWED_SYMBOLS': ','.join(cfg.allowed_symbols), 'PROPOSAL_TTL': cfg.proposal_ttl,
        'DEVIATION_POINTS': cfg.deviation_points,
    }
    env.update({'TRADE_MCP_'+key: str(value) for key, value in values.items()})
    if cfg.web_model_backend == 'opencode-session':
        for key, value in {'OPENCODE_URL': cfg.opencode_url, 'OPENCODE_MODEL': cfg.opencode_model,
                           'OPENCODE_VARIANT': cfg.opencode_variant,
                           'OPENCODE_PASSWORD': cfg.opencode_password}.items():
            env['TRADE_MCP_'+key] = str(value)
    return env


class WorkspaceManager:
    def __init__(self):
        self.process = None
        self.lock = threading.Lock()

    def status(self, cfg):
        url = f'http://127.0.0.1:{cfg.console_port}'
        try:
            with socket.create_connection(('127.0.0.1', cfg.console_port), timeout=1):
                pass
        except OSError:
            return {'ok': True, 'state': 'starting' if self.process is not None and self.process.poll() is None else 'stopped', 'url': url}
        try:
            with urllib.request.build_opener(NoRedirect).open(url+'/api/workspace-status', timeout=3) as response:
                data = json.load(response)
            if (data.get('backend') != 'native' or data.get('trading_enabled') is not False
                    or data.get('workspace_identity') != workspace_identity(cfg)
                    or data.get('model_backend') not in {'opencode-session', 'openai-compatible'}
                    or data.get('model_source') != 'web'
):
                raise ValueError('workspace mismatch')
            return {'ok': True, 'state': 'ready', 'url': url, 'trading_enabled': False,
                    'model_test': data.get('model_test'), 'agents': data.get('agents', []),
                    'message': 'Web console is available; model entitlement is not implied.'}
        except Exception:
            return {'ok': False, 'state': 'port_occupied_or_mismatched', 'url': url,
                    'message': 'The port is occupied or the workspace configuration differs. Nothing was stopped.'}

    def start(self, app, *, open_browser=False):
        with self.lock:
            return self._start(app, open_browser=open_browser)

    def _start(self, app, *, open_browser=False):
        cfg = app.cfg
        if cfg.backend != 'native' or not cfg.mcp_token:
            return {'ok': False, 'error': 'authenticated_native_required'}
        # Verify real terminal identity before reusing credentials for another process.
        status = app.mt5.status()
        if not status.get('ea_connected') or status.get('backend') != 'native':
            return {'ok': False, 'error': 'terminal_not_connected'}
        if app.mt5.account_info(fresh=True).get('trade_mode') != 'demo':
            return {'ok': False, 'error': 'demo_account_required'}
        existing = self.status(cfg)
        if existing['state'] == 'stopped':
            try:
                self.process = subprocess.Popen(
                    [sys.executable, '-m', 'trade_mcp', 'console', '--host', '127.0.0.1',
                     '--console-port', str(cfg.console_port), '--no-browser'],
                    env=console_environment(cfg), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                return {'ok': False, 'error': 'console_launch_failed'}
            result = {'ok': True, 'state': 'starting', 'url': existing['url'], 'trading_enabled': False,
                      'message': 'Console starting; poll web_workspace_status. No model or order requested.'}
        else:
            result = existing
        if open_browser and result.get('ok'):
            # This opens the system browser, not an invented embedded desktop tab.
            try:
                result['browser_open_requested'] = bool(webbrowser.open(result['url'], new=2))
            except Exception:
                result['browser_open_requested'] = False
        return result

    def stop(self):
        with self.lock:
            return self._stop()

    def _stop(self):
        """Stop only the child owned by this manager, never a reused console."""
        if self.process is None or self.process.poll() is not None:
            return {'ok': False, 'error': 'no_owned_console', 'message': 'No other process was stopped.'}
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process = None
        return {'ok': True, 'state': 'stopped'}
