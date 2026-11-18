"""Explicit v2 session transport for web-only usage; never adopts a chat session."""
import json
import re
from contextlib import suppress

from .agents import LLMClient
from .model_usage import ProviderError
from .opencode import FREE_MODELS, ModelServiceError, OpenCodeClient

ADAPTER_REVISION = "session-default-alias-v3"


class WebOpenCodeClient(LLMClient):
    @classmethod
    def from_web_config(cls, cfg, store, request_kind):
        client = cls(base_url=cfg.opencode_url, model=cfg.opencode_model,
                     mode='web', store=store, request_kind=request_kind,
                     usage_convention='unknown')
        client.web_variant = cfg.opencode_variant
        client.oc = OpenCodeClient(cfg.opencode_url, cfg.opencode_model,
                                  cfg.opencode_password, timeout=60, variant=cfg.opencode_variant)
        return client

    @property
    def provider(self):
        return 'opencode-session'

    @property
    def configured(self):
        return bool(self.base_url and self.model in FREE_MODELS)

    def _endpoint(self):
        # Validates loopback configuration without network or budget consumption.
        from urllib.parse import urlparse
        p = urlparse(self.base_url)
        if (p.scheme != 'http' or p.hostname not in {'localhost', '127.0.0.1', '::1'}
                or p.username or p.password or p.query or p.fragment or p.path not in {'', '/'}):
            raise RuntimeError('Loopback OpenCode URL required')
        return self.base_url.rstrip('/') + '/api/session'

    def chat(self, messages, temperature=0.2):
        self.transport_diagnostics = {'stage': 'before_request', 'generation_submitted': False}
        try:
            return super().chat(messages, temperature)
        except ProviderError as exc:
            exc.transport = dict(self.transport_diagnostics)
            self.last_error = exc.safe_dict()
            raise

    def check_session(self):
        """Create/validate/delete our own session, never invoke generate."""
        self._endpoint()
        self.transport_diagnostics = {'stage':'before_request', 'generation_submitted':False}
        self.probe_only = True
        try:
            self._response_data(None)
            return {'ok':True, 'adapter_revision':ADAPTER_REVISION, 'transport':dict(self.transport_diagnostics), 'generation_requested':False}
        except ProviderError as exc:
            exc.transport = dict(self.transport_diagnostics)
            return {'ok':False, 'adapter_revision':ADAPTER_REVISION, **exc.safe_dict()}
        finally:
            self.probe_only = False

    def _response_data(self, req):
        permissions = [{'action': '*', 'resource': '*', 'effect': 'deny'}]
        provider, _, model = self.model.partition('/')
        selection = {'providerID': provider, 'id': model}
        if self.oc.variant:
            selection['variant'] = self.oc.variant
        sid = None
        try:
            self.transport_diagnostics['stage'] = 'server_identity'
            if self.oc.health().get('api') != 'v2':
                raise ProviderError('endpoint_unavailable')
            self.transport_diagnostics['stage'] = 'session_create'
            result = self.oc._request('POST', '/api/session', {
                'title': 'trade-mcp web isolated analysis', 'model': selection,
                'permissions': permissions,
            })
            self.transport_diagnostics['stage'] = 'session_validation'
            session = result.get('data') or {}
            sid = session.get('id')
            if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', sid):
                sid = None
                raise ProviderError('invalid_response')
            # Refuse inference if the server did not acknowledge isolation/model.
            def normalized_variant(value):
                # OpenCode 2.3 returns the literal 'default' for unspecified variants.
                return '' if value in (None, '', 'default') else value

            def model_matches(info):
                actual = info.get('model') or {}
                return (isinstance(actual, dict)
                    and actual.get('providerID') == selection['providerID']
                    and actual.get('id') == selection['id']
                    and normalized_variant(actual.get('variant')) == normalized_variant(selection.get('variant')))

            self.transport_diagnostics.update(permissions_acknowledged=session.get('permissions') == permissions,
                                              model_acknowledged=model_matches(session))
            if session.get('permissions') != permissions:
                raise ProviderError('invalid_response')
            if not model_matches(session):
                # Some servers do not return the selected model on creation.
                # Explicitly select it only on OUR new session, then read back.
                self.transport_diagnostics['stage'] = 'session_model_select'
                self.oc._request('POST', '/api/session/'+sid+'/model', {'model': selection})
                self.transport_diagnostics['stage'] = 'session_model_readback'
                result = self.oc._request('GET', '/api/session/'+sid)
                verified = result.get('data') or {}
                same_session = verified.get('id') == sid
                self.transport_diagnostics.update(
                    permissions_acknowledged=same_session and verified.get('permissions') == permissions,
                    model_acknowledged=same_session and model_matches(verified))
                if not all(self.transport_diagnostics[key] for key in ('permissions_acknowledged', 'model_acknowledged')):
                    raise ProviderError('invalid_response')
            if getattr(self, 'probe_only', False):
                return {'checked':True}, 200
            messages = json.loads(req.data)['messages']
            prompt = '\n\n'.join(m['role']+': '+m['content'] for m in messages)
            self.transport_diagnostics.update(stage='session_generate', generation_submitted=True)
            data = self.oc._request('POST', '/api/session/'+sid+'/generate', {'prompt': prompt}).get('data') or {}
            self.transport_diagnostics['stage'] = 'generation_response'
            tokens = data.get('tokens') or {}
            cache = tokens.get('cache') or {}
            # Keep the returned categories unmodified. Inclusion semantics are
            # unknown here; never add cache/reasoning to another category.
            return {'choices': [{'message': {'content': data.get('text')}}],
                    'model': self.model,
                    'usage': {'input_tokens': tokens.get('input'),
                              'output_tokens': tokens.get('output'),
                              'cached_tokens': cache.get('read'),
                              'reasoning_tokens': tokens.get('reasoning')}}, 200
            # OpenCode's cost is locally calculated from its catalogue, not a
            # provider invoice. Deliberately leave provider-reported cost unknown.
        except ModelServiceError as exc:
            if exc.retry_after_ms is not None:
                self.store.note_model_budget_cooldown(exc.retry_after_ms, reason=exc.code)
            raise ProviderError(exc.code, exc.http_status, retry_after_ms=exc.retry_after_ms) from None
        finally:
            if sid:
                # Delete only the ephemeral session created by this request.
                with suppress(RuntimeError, ValueError):
                    self.oc._request('DELETE', '/api/session/'+sid)
