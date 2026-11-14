"""OpenCode v1 server adapter. Each request uses a fresh, deny-all session.

Only bounded market summaries are sent. No MT5 credentials, MCP tools, repository
files or account IDs are supplied. Execution stays in the application's executor.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class OpenCodeClient:
    base_url: str
    model: str = ""
    password: str = field(default="", repr=False)
    timeout: float = 90
    last_session_id: str = ""

    @property
    def configured(self):
        return bool(self.base_url)

    def _request(self, method, path, payload=None):
        parsed = urlparse(self.base_url)
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {'', '/'}):
            raise RuntimeError('OpenCode requires a loopback HTTP server URL without credentials or a path.')
        headers = {'Content-Type': 'application/json'}
        if self.password:
            token = base64.b64encode(('opencode:'+self.password).encode()).decode()
            headers['Authorization'] = 'Basic '+token
        req = urllib.request.Request(self.base_url.rstrip('/')+path,
                                     data=None if payload is None else json.dumps(payload).encode(),
                                     headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=self.timeout) as response:
                return json.load(response)
        except Exception:
            raise RuntimeError('OpenCode request failed. Check server, API version, authentication and model access.') from None

    def health(self):
        result = self._request('GET', '/global/health')
        if not isinstance(result, dict) or result.get('healthy') is not True:
            raise RuntimeError('OpenCode health response is not compatible with the v1 server API.')
        return {'healthy': True, 'version': result.get('version')}

    def chat(self, messages, temperature=0.2):
        del temperature
        self.last_session_id = ''
        session = self._request('POST', '/session', {
            'title': 'trade-mcp bounded analysis',
            'permission': [{'permission': '*', 'pattern': '*', 'action': 'deny'}],
        })
        sid = session.get('id') if isinstance(session, dict) else None
        if not isinstance(sid, str) or not sid:
            raise RuntimeError('OpenCode did not create a session. Requires compatible v1 session API.')
        self.last_session_id = sid
        path = '/session/'+quote(sid, safe='')
        try:
            if session.get('permission') != [{'permission': '*', 'pattern': '*', 'action': 'deny'}]:
                raise RuntimeError('OpenCode did not acknowledge deny-all session permissions.')
            body = {'parts': [{'type': 'text', 'text': '\n\n'.join(
                str(m.get('role', 'user'))+': '+str(m.get('content', '')) for m in messages)}]}
            if self.model:
                provider, separator, model = self.model.partition('/')
                if not separator or not provider or not model:
                    raise RuntimeError('OpenCode model must use provider/model format.')
                body['model'] = {'providerID': provider, 'modelID': model}
            result = self._request('POST', path+'/message', body)
            info = result.get('info') or {}
            if info.get('error'):
                raise RuntimeError('OpenCode model returned an error.')
            text = '\n'.join(p.get('text', '') for p in result.get('parts', []) if p.get('type') == 'text')
            if not text.strip():
                raise RuntimeError('OpenCode returned no text.')
            return text.strip()
        finally:
            # Abort pending work even after a timeout, then remove the market summary.
            for method, suffix, payload in [('POST', '/abort', {}), ('DELETE', '', None)]:
                with suppress(RuntimeError):
                    self._request(method, path+suffix, payload)

    def chat_json(self, messages):
        from .agents import _extract_json
        try:
            return _extract_json(self.chat(messages))
        except (RuntimeError, ValueError, TypeError, AttributeError):
            return None
