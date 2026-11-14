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

MODEL_ERROR_MESSAGES = {
    'billing_required': 'اعتبار سرویس مدل کافی نیست؛ ایجنت متوقف است و پیشنهادی ساخته نشد.',
    'quota_exhausted': 'سهمیهٔ سرویس مدل تمام شده است؛ ایجنت متوقف است.',
    'rate_limited': 'سرویس مدل موقتاً محدودیت تعداد درخواست دارد؛ پاسخ معتبر دریافت نشد.',
    'authentication_failed': 'احراز هویت سرور OpenCode ناموفق بود.',
    'endpoint_unavailable': 'مسیر API مدل در این نسخه در دسترس نیست.',
    'connection_failed': 'ارتباط با سرور مدل برقرار نشد یا پاسخ نامعتبر بود.',
    'invalid_response': 'مدل پاسخ تصمیم معتبر نداد؛ هیچ پیشنهاد جدیدی ساخته نشد.',
}


class ModelServiceError(RuntimeError):
    def __init__(self, code, http_status=None):
        self.code = code
        self.http_status = http_status
        super().__init__(MODEL_ERROR_MESSAGES.get(code, MODEL_ERROR_MESSAGES['connection_failed']))

    def safe_dict(self):
        return {'code': self.code, 'message': str(self), 'http_status': self.http_status}


def classify_provider_error(status, body):
    # Inspect a bounded response only for known markers. Never return its contents.
    text = body.lower()
    if status == 402 or 'insufficient balance' in text or '\"status\": 402' in text or '\"status\":402' in text:
        code = 'billing_required'
    elif any(marker in text for marker in ('free usage limit', 'quota exceeded', 'quota_exceeded', 'insufficient_quota')):
        code = 'quota_exhausted'
    elif status == 429 or 'rate_limit' in text:
        code = 'rate_limited'
    elif status in (401, 403):
        code = 'authentication_failed'
    elif status == 404:
        code = 'endpoint_unavailable'
    else:
        code = 'connection_failed'
    return ModelServiceError(code, status)


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
    last_error: dict | None = field(default=None, init=False)

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
        except urllib.error.HTTPError as exc:
            body = exc.read(32768).decode('utf-8', 'replace')
            raise classify_provider_error(exc.code, body) from None
        except Exception:
            raise ModelServiceError('connection_failed') from None

    def health(self):
        self.api_version = ''
        try:
            result = self._request('GET', '/api/info')
        except ModelServiceError as exc:
            if exc.code == 'authentication_failed':
                raise
            result = None
        except RuntimeError:
            result = None
        if (isinstance(result, dict) and str(result.get('version', '')).lstrip('v').startswith('2.')
                and isinstance(result.get('pid'), int) and isinstance(result.get('urls'), list)):
            self.api_version = 'v2'
            return {'healthy': True, 'version': result['version'], 'api': 'v2', 'pid': result['pid']}
        result = self._request('GET', '/global/health')
        if not isinstance(result, dict) or result.get('healthy') is not True:
            raise RuntimeError('OpenCode did not return a supported v1/v2 server identity.')
        self.api_version = 'v1'
        return {'healthy': True, 'version': result.get('version'), 'api': 'v1'}

    def chat(self, messages, temperature=0.2):
        del temperature
        self.last_session_id = ''
        self.last_error = None
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
        self.last_error = None
        try:
            result = _extract_json(self.chat(messages))
            if not result:
                self.last_error = ModelServiceError('invalid_response').safe_dict()
            return result
        except ModelServiceError as exc:
            self.last_error = exc.safe_dict()
            return None
        except (RuntimeError, ValueError, TypeError, AttributeError):
            self.last_error = ModelServiceError('connection_failed').safe_dict()
            return None


def discover_desktop():
    """Read the user's private v2 service registration and verify its identity.

    No process is spawned. Secrets are returned in memory only, never logged.
    """
    import os
    import stat
    from pathlib import Path

    path = Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local/state')/'opencode/service.json'
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'r') as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o077 or metadata.st_size > 65536):
                raise ValueError('unsafe registration')
            record = json.loads(stream.read(65537))
        url, password, pid = record.get('url'), record.get('password'), record.get('pid')
        if (not isinstance(url, str) or not isinstance(password, str) or not password
                or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
            raise ValueError('invalid registration')
        client = OpenCodeClient(url, password=password, timeout=5)
        health = client.health()
        if health.get('api') != 'v2' or health.get('pid') != pid:
            raise ValueError('stale registration')
        return url, password
    except Exception:
        raise RuntimeError('Cannot verify the private OpenCode desktop registration. Open the desktop app; expected a current-user-only service.json under XDG_STATE_HOME/opencode or ~/.local/state/opencode. No alternate server was started.') from None
