import json
import urllib.error

from trade_mcp.cloud_access import check_openrouter


def test_read_only_check_without_key_has_no_network(monkeypatch):
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert check_openrouter('')['error'] == 'api_key_required'


def test_read_only_check_allowlists_output_and_never_posts(monkeypatch):
    calls = []
    class Response:
        def __init__(self, data):
            self.data = data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            return json.dumps(self.data).encode()
    class Opener:
        def open(self, req, timeout):
            assert req.method == 'GET'
            calls.append(req.full_url)
            if req.full_url.endswith('/key'):
                return Response({'data':{'label':'private', 'free_model_daily_requests':
                    {'used':1,'limit':50,'remaining':49}}})
            return Response({'data':[
                {'id':'vendor/model:free','pricing':{'prompt':'0','completion':'0'}},
                {'id':'vendor/paid:free','pricing':{'prompt':'1','completion':'0'}},
                {'id':'vendor/missing:free','pricing':{}},
            ]})
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: Opener())
    result = check_openrouter('secret-key')
    assert result['ok']
    assert result['catalogue_free_model_ids'] == ['vendor/model:free']
    assert result['free_daily_requests']['remaining'] == 49
    assert 'secret-key' not in json.dumps(result) and 'private' not in json.dumps(result)
    assert len(calls) == 2


def test_read_only_error_does_not_echo_body_or_key(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError('https://openrouter.ai',401,'private',{},None)
    monkeypatch.setattr('urllib.request.build_opener', lambda *a: Opener())
    result = check_openrouter('secret-key')
    assert result['http_status'] == 401
    assert 'private' not in json.dumps(result)
