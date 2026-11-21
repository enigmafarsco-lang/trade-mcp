"""Request shape and failure semantics only; no real cloud calls."""
import json

import pytest

from trade_mcp.agents import LLMClient, model_signature
from trade_mcp.model_usage import ProviderError
from trade_mcp.workspace import console_environment, workspace_identity


def configure(cfg):
    cfg.llm_base_url = 'https://openrouter.ai/api/v1'
    cfg.llm_model = 'inclusionai/ling-3.2-flash:free'
    cfg.web_model_backend = 'direct'
    cfg.llm_fast_advisory = True


@pytest.mark.parametrize('kind,active', [('agent_run',True), ('model_test',True),
                                        ('research_tradingagents',False), ('explain',False)])
def test_profile_is_only_for_short_explicit_web_calls(mock_app, kind, active):
    configure(mock_app.cfg)
    client = LLMClient.from_config(mock_app.cfg, mode='web', store=mock_app.store, request_kind=kind)
    payload = client._request_payload([{'role':'user','content':'fixture'}], .2)
    assert ('max_tokens' in payload) is active
    assert ('reasoning' in payload) is active
    if active:
        assert payload['max_tokens'] == 512
        assert payload['reasoning'] == {'enabled':False}
        assert payload['provider'] == {'require_parameters':True}
    assert payload['model'] == mock_app.cfg.llm_model
    assert 'models' not in payload


@pytest.mark.parametrize('change', ['disabled','other_model','other_host','non_web'])
def test_profile_never_leaks_to_other_routes(mock_app, change):
    configure(mock_app.cfg)
    if change == 'disabled':
        mock_app.cfg.llm_fast_advisory = False
    if change == 'other_model':
        mock_app.cfg.llm_model = 'another/model:free'
    if change == 'other_host':
        mock_app.cfg.llm_base_url = 'https://other.example/v1'
    client = LLMClient.from_config(mock_app.cfg, mode='auto' if change == 'non_web' else 'web', store=mock_app.store)
    assert client.request_profile == 'provider-default'
    assert 'reasoning' not in client._request_payload([], .2)


def test_profile_changes_test_and_workspace_identity_and_launcher_env(mock_app):
    configure(mock_app.cfg)
    before = model_signature(mock_app.cfg), workspace_identity(mock_app.cfg)
    assert console_environment(mock_app.cfg)['TRADE_MCP_LLM_FAST_ADVISORY'] == 'true'
    mock_app.cfg.llm_fast_advisory = False
    assert model_signature(mock_app.cfg) != before[0]
    assert workspace_identity(mock_app.cfg) != before[1]
    assert console_environment(mock_app.cfg)['TRADE_MCP_LLM_FAST_ADVISORY'] == 'false'


def test_truncated_output_is_rejected_but_actual_usage_retained(mock_app, monkeypatch):
    configure(mock_app.cfg)
    client = LLMClient.from_config(mock_app.cfg, mode='web', store=mock_app.store)
    requests = []
    def response(self, req):
        requests.append(json.loads(req.data))
        return {'choices':[{'message':{'content':'{"action":"buy"}'},'finish_reason':'length'}],
                'usage':{'prompt_tokens':10,'completion_tokens':512}}, 200
    monkeypatch.setattr(LLMClient, '_response_data', response)
    mock_app.store.activate_model_budget()
    with pytest.raises(ProviderError) as exc:
        client.chat([])
    assert exc.value.code == 'invalid_response'
    assert len(requests) == 1
    assert requests[0]['reasoning'] == {'enabled':False}
    row = mock_app.store.list_model_usage()[0]
    assert row['ok'] == 0 and row['input_tokens'] == 10 and row['output_tokens'] == 512
    assert mock_app.store.model_budget_status()['used_attempts'] == 1
