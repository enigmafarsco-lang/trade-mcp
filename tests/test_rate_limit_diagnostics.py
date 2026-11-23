import json

import pytest

from trade_mcp.model_usage import classify_http_error


@pytest.mark.parametrize('body,origin,reasons', [
    ('{}', 'unknown', []),
    ('not json', 'unknown', []),
    (json.dumps({'error':{'message':'Temporarily rate-limited upstream'}}),
     'upstream_reported', ['upstream_rate_limit_reported']),
    (json.dumps({'error':{'message':'Daily request limit reached'}}),
     'unknown', ['daily_limit_reported']),
    (json.dumps({'error':{'message':'requests per minute exceeded'}}),
     'unknown', ['per_minute_limit_reported']),
])
def test_rate_limit_retains_only_fixed_safe_claims(body, origin, reasons):
    error = classify_http_error(429, body)
    safe = error.safe_dict()
    assert error.code == 'rate_limited'
    diagnostic = safe['rate_limit_diagnostic']
    assert diagnostic['origin'] == origin
    assert diagnostic['reported_reasons'] == reasons
    assert diagnostic['independently_verified'] is False


def test_metadata_secrets_and_provider_identity_are_never_echoed():
    body = json.dumps({'error':{'message':'secret-key', 'metadata':{
        'raw':'private-prompt: temporarily rate-limited upstream',
        'provider_name':'private-provider', 'headers':{'Authorization':'private-token'}}}})
    safe = json.dumps(classify_http_error(429, body).safe_dict())
    for value in ('secret-key','private-prompt','private-provider','private-token'):
        assert value not in safe
    assert 'upstream_reported' in safe


def test_status_alone_and_unrelated_metadata_do_not_attribute_origin():
    body = json.dumps({'error':{'metadata':{'provider_name':'Upstream rate limit'}}})
    assert classify_http_error(429, body).safe_dict()['rate_limit_diagnostic']['origin'] == 'unknown'
    assert 'rate_limit_diagnostic' not in classify_http_error(503, '{}').safe_dict()
