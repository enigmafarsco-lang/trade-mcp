"""OpenCode server adapter: v1 deny-all sessions, v2 stateless text generation.

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
    api_version: str = ""

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
        self.api_version = ''
        try:
            result = self._request('GET', '/api/info')
        except RuntimeError:
            result = None
        if (isinstance(result, dict) and str(result.get('version', '')).lstrip('v').startswith('2.')
                and isinstance(result.get('pid'), int) and isinstance(result.get('urls'), list)):
            self.api_version = 'v2'
            return {'healthy': True, 'version': result['version'], 'api': 'v2'}
        result = self._request('GET', '/global/health')
        if not isinstance(result, dict) or result.get('healthy') is not True:
            raise RuntimeError('OpenCode did not return a supported v1/v2 server identity.')
        self.api_version = 'v1'
        return {'healthy': True, 'version': result.get('version'), 'api': 'v1'}

    def chat(self, messages, temperature=0.2):
        del temperature
        self.last_session_id = ''
        if not self.api_version:
            self.health()
        if self.api_version == 'v2':
            # The v2 stateless generation route has no tools or session agent loop.
            body = {'prompt': '\n\n'.join(str(m.get('role', 'user'))+': '+str(m.get('content', ''))
                                           for m in messages)}
            if self.model:
                provider, separator, model = self.model.partition('/')
                if not separator or not provider or not model:
                    raise RuntimeError('OpenCode model must use provider/model format.')
                body['model'] = {'providerID': provider, 'id': model}
            result = self._request('POST', '/api/experimental/generate/text', body)
            data = result.get('data') if isinstance(result, dict) else None
            text = data.get('text') if isinstance(data, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError('OpenCode v2 returned no valid text.')
            return text.strip()
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
