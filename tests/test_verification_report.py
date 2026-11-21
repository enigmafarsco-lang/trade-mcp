"""Machine-generated counts must not silently turn failures/skips into passes."""
import runpy
from pathlib import Path

import pytest

SUMMARY = runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/verify_research.py'))['junit_summary']


@pytest.mark.parametrize('wrapper', [True, False])
def test_junit_counts_are_read_from_xml(tmp_path, wrapper):
    text = '<testsuite tests="7" failures="1" errors="1" skipped="2"/>'
    if wrapper:
        text = '<testsuites>'+text+'</testsuites>'
    path = tmp_path/'results.xml'
    path.write_text(text)
    assert SUMMARY(path) == {'tests':7,'failures':1,'errors':1,'skipped':2,'passed':3}


@pytest.mark.parametrize('url', ['https://example.com','http://127.0.0.1@evil.test','http://localhost:8790/private'])
def test_live_verifier_refuses_nonlocal_or_credential_urls(url):
    verify = runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/verify_role_previews.py'))['verify']
    with pytest.raises(ValueError):
        verify(url, 'existing-agent')


def test_live_verifier_only_prepares_and_redacts_prompts(monkeypatch):
    import json

    verify = runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/verify_role_previews.py'))['verify']
    calls = []
    status = {'backend':'native', 'trading_enabled':False,
              'model_budget':{'state':'revoked','used_attempts':1},
              'model_usage':{'requests':5}, 'scheduler':{'running':False},
              'page_cycle':{'active':False,'busy':False}, 'agents':[{'id':'existing-agent'}],
              'proposals':[{'id':'historical'}]}
    class Response:
        def __init__(self, data):
            self.data = data
            self.status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *args):
            return json.dumps(self.data).encode()
    class Opener:
        def open(self, req, **kwargs):
            calls.append((req.method, req.full_url))
            if req.method == 'GET':
                return Response(status)
            assert req.full_url.endswith('/api/research/prepare')
            role = json.loads(req.data)['role']
            return Response({'ok':True,'role':role,'generation_requested':False,'model_response':False,
                             'executable':False,'usage':None,'messages':[{'content':'private-prompt'}],
                             'characters':14})
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: Opener())
    result = verify('http://127.0.0.1:8790', 'existing-agent')
    assert result['ok'] and result['boundaries_unchanged']
    assert len(calls) == 5
    assert 'private-prompt' not in json.dumps(result)
