"""Provider usage ledger and the shared web test budget.

Everything here is offline: provider responses are injected, never fetched.
Unknown usage remains ``None``/NULL, cost buckets are never merged across
currencies, and the budget is persisted in the journal rather than in memory.
"""

from __future__ import annotations

import email.message
import io
import json
import threading
import urllib.error

import pytest

import trade_mcp.agents as agents_module
from trade_mcp.agents import AgentEngine, LLMClient, model_signature
from trade_mcp.model_usage import (
    BudgetDenied,
    ProviderError,
    classify_http_error,
    normalise_convention,
    unknown_usage,
    usage_from_response,
)
from trade_mcp.store import Store


def web_cfg(mock_app):
    """A dedicated web-provider config that also has a dev OpenCode configured."""
    return mock_app.cfg.with_overrides(
        llm_base_url="https://provider.example/v1",
        llm_model="web-model",
        llm_api_key="test-key",
        llm_billing_project="web-project",
        llm_usage_convention="openai_inclusive",
        opencode_url="http://127.0.0.1:4096",
        opencode_model="opencode/dev-model",
    )


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = json.dumps(body).encode("utf-8")
        self.status = status

    def read(self, size=-1):
        return self.body if size is None or size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append(request)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def patch_opener(monkeypatch, outcome):
    opener = FakeOpener(outcome)
    monkeypatch.setattr(
        agents_module.urllib.request, "build_opener", lambda *a, **k: opener
    )
    return opener


# ------------------------------------------------------------------ parsing
def test_openai_inclusive_usage_keeps_details_separate():
    usage = usage_from_response(
        {
            "model": "actual-model",
            "choices": [{"message": {"content": "OK"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 40},
                "completion_tokens_details": {"reasoning_tokens": 5},
                "cost": 0.125,
                "currency": "usd",
            },
        },
        "openai_inclusive",
    )
    assert usage == {
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "output_tokens": 20,
        "reasoning_tokens": 5,
        "cost_amount": 0.125,
        "cost_currency": "USD",
        "usage_source": "provider",
        "cost_source": "provider",
        "accounting_convention": "openai_inclusive",
    }
    # No total is computed, so inclusive details cannot be double counted.
    assert "total_tokens" not in usage


def test_deepseek_style_cache_field_is_recognised():
    usage = usage_from_response(
        {"usage": {"prompt_tokens": 10, "completion_tokens": 2,
                   "prompt_cache_hit_tokens": 6}},
        "openai_inclusive",
    )
    assert usage["cached_input_tokens"] == 6
    assert usage["reasoning_tokens"] is None
    assert usage["usage_source"] == "partial"
    assert usage["cost_amount"] is None
    assert usage["cost_source"] == "unknown"


def test_missing_values_stay_null_and_never_become_zero():
    usage = usage_from_response({"choices": []}, "unknown")
    assert usage == unknown_usage("unknown")
    assert all(usage[key] is None for key in (
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_tokens", "cost_amount", "cost_currency",
    ))


def test_cost_without_currency_is_kept_but_not_assigned_one():
    usage = usage_from_response(
        {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.01}},
        "additive",
    )
    assert usage["cost_amount"] == 0.01
    assert usage["cost_currency"] is None
    assert usage["cost_source"] == "provider"
    assert usage["accounting_convention"] == "additive"


def test_invalid_numbers_are_unknown_not_zero():
    usage = usage_from_response(
        {"usage": {"prompt_tokens": -1, "completion_tokens": True,
                   "cost": float("nan"), "currency": "not a currency!"}},
        "nonsense",
    )
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None
    assert usage["cost_amount"] is None
    assert usage["cost_currency"] is None
    assert usage["accounting_convention"] == "unknown"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("openai", "openai_inclusive"),
        ("OpenAI-Compatible", "openai_inclusive"),
        ("inclusive", "openai_inclusive"),
        ("additive", "additive"),
        ("separate", "additive"),
        ("unknown", "unknown"),
        ("something-else", "unknown"),
        (None, "unknown"),
    ],
)
def test_accounting_conventions_are_normalised(raw, expected):
    assert normalise_convention(raw) == expected


def test_http_errors_are_classified_without_echoing_the_body():
    error = classify_http_error(429, "secret prompt and key sk-live")
    assert error.code == "rate_limited"
    assert "secret" not in str(error)
    assert "sk-live" not in error.safe_dict()["message"]

    headers = email.message.Message()
    headers["Retry-After"] = "2"
    bounded = classify_http_error(429, "{}", headers)
    assert bounded.retry_after_ms == 2000


# ------------------------------------------------------------------- ledger
def test_ledger_preserves_null_and_summary_counts_unknowns(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    try:
        store.record_model_usage({
            "request_kind": "model_test", "provider": "provider.example",
            "model": "web-model", "billing_project": "web-project",
            "ok": True, "input_tokens": 10, "cached_input_tokens": None,
            "output_tokens": None, "reasoning_tokens": None,
            "cost_amount": None, "cost_currency": None,
            "usage_source": "partial", "cost_source": "unknown",
            "accounting_convention": "openai_inclusive",
        })
        row = store.list_model_usage(limit=1)[0]
        assert row["input_tokens"] == 10
        assert row["cached_input_tokens"] is None
        assert row["output_tokens"] is None
        assert row["cost_amount"] is None
        assert row["billing_project"] == "web-project"

        summary = store.model_usage_summary()
        assert summary["requests"] == 1
        assert summary["fields"]["input_tokens"] == {
            "known_total": 10, "known_rows": 1, "unknown_rows": 0}
        assert summary["fields"]["output_tokens"] == {
            "known_total": 0, "known_rows": 0, "unknown_rows": 1}
        assert summary["cost_unknown_rows"] == 1
        assert summary["cost"] == []
        assert summary["usage_partial_rows"] == 1
    finally:
        store.close()


def test_summary_never_sums_different_currencies(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    try:
        for amount, currency in ((0.01, "USD"), (0.02, "EUR"), (0.03, "USD")):
            store.record_model_usage({
                "ok": True, "cost_amount": amount, "cost_currency": currency,
                "usage_source": "unknown", "cost_source": "provider",
                "accounting_convention": "unknown",
            })
        store.record_model_usage({"ok": False, "usage_source": "unknown",
                                  "cost_source": "unknown",
                                  "accounting_convention": "unknown"})
        costs = {item["currency"]: item for item in store.model_usage_summary()["cost"]}
        assert costs["USD"]["known_total"] == pytest.approx(0.04)
        assert costs["USD"]["known_rows"] == 2
        assert costs["EUR"]["known_total"] == pytest.approx(0.02)
        assert costs["EUR"]["known_rows"] == 1
        assert None not in costs  # The unknown-cost row is counted, not bucketed.
        assert store.model_usage_summary()["cost_unknown_rows"] == 1
    finally:
        store.close()


def test_ledger_schema_rejects_prompts_responses_and_credentials(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    try:
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(model_usage)")}
        assert not columns & {"prompt", "response", "messages", "api_key", "token"}
        store.record_model_usage({
            "prompt": "PROMPT-SECRET", "response": "RESPONSE-SECRET",
            "api_key": "KEY-SECRET", "ok": True,
            "usage_source": "unknown", "cost_source": "unknown",
            "accounting_convention": "unknown",
        })
        dump = json.dumps(store.list_model_usage(), default=str)
        assert "SECRET" not in dump
    finally:
        store.close()


# ------------------------------------------------------------------- budget
def test_budget_is_denied_until_explicitly_activated(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    try:
        status = store.model_budget_status(now=1000)
        assert status["state"] == "not_activated"
        assert status["allowed"] is False
        assert status["reason"] == "test_budget_not_activated"

        denied = store.reserve_model_budget(now=1000)
        assert denied["allowed"] is False
        assert denied["reserved"] is False
        assert store.model_budget_status(now=1000)["used_attempts"] == 0

        activated = store.activate_model_budget(now=1001)
        assert activated["activated"] is True
        assert activated["allowed"] is True
        assert activated["first_attempt_at"] is None
        assert store.model_usage_summary()["requests"] == 0
    finally:
        store.close()


def test_window_starts_at_first_accepted_attempt_not_activation(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    try:
        store.activate_model_budget(now=1000, window_seconds=300)
        assert store.model_budget_status(now=1500)["state"] == "active"
        assert store.model_budget_status(now=1500)["deadline"] is None

        first = store.reserve_model_budget(now=1500, window_seconds=300)
        assert first["allowed"] is True
        assert first["first_attempt_at"] == 1500
        assert first["deadline"] == 1800

        later = store.reserve_model_budget(now=1799, window_seconds=300)
        assert later["allowed"] is True
        assert later["deadline"] == 1800  # The deadline is not extended.

        expired = store.reserve_model_budget(now=1800, window_seconds=300)
        assert expired["allowed"] is False
        assert expired["reason"] == "test_budget_expired"
    finally:
        store.close()


def test_five_attempts_exhaust_the_window_and_reactivation_resets_it(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    try:
        store.activate_model_budget(now=1000, window_seconds=300, max_attempts=5)
        for index in range(5):
            result = store.reserve_model_budget(now=1000 + index)
            assert result["allowed"] is True
            assert result["attempt_index"] == index + 1
        sixth = store.reserve_model_budget(now=1006)
        assert sixth["allowed"] is False
        assert sixth["reason"] == "test_budget_exhausted"
        assert sixth["used_attempts"] == 5

        status = store.model_budget_status(now=1007)
        assert status["state"] == "exhausted"
        assert status["remaining_attempts"] == 0

        renewed = store.activate_model_budget(now=1008)
        assert renewed["activated"] is True
        assert renewed["used_attempts"] == 0
        assert renewed["first_attempt_at"] is None
    finally:
        store.close()


def test_reactivation_while_active_is_idempotent(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    try:
        first = store.activate_model_budget(now=1000)
        store.reserve_model_budget(now=1010)
        second = store.activate_model_budget(now=1100)
        assert second["already_active"] is True
        assert second["activated"] is False
        assert second["window_id"] == first["window_id"]
        assert second["used_attempts"] == 1
        assert second["first_attempt_at"] == 1010
    finally:
        store.close()


def test_budget_survives_store_reopen_and_read_only_polling(tmp_path):
    path = tmp_path / "journal.sqlite3"
    store = Store(path)
    try:
        store.activate_model_budget(now=2000, window_seconds=300)
        store.reserve_model_budget(now=2001)
        store.reserve_model_budget(now=2002)
    finally:
        store.close()

    reopened = Store(path)
    try:
        # A page refresh or console restart only reads the persisted window.
        for _ in range(5):
            status = reopened.model_budget_status(now=2010)
            assert status["used_attempts"] == 2
            assert status["allowed"] is True
            assert status["first_attempt_at"] == 2001
        third = reopened.reserve_model_budget(now=2011)
        assert third["allowed"] is True
        assert third["used_attempts"] == 3
    finally:
        reopened.close()


def test_concurrent_reservations_never_exceed_the_limit(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    try:
        store.activate_model_budget(now=3000, window_seconds=300, max_attempts=5)
        results = []
        lock = threading.Lock()

        def worker():
            result = store.reserve_model_budget(now=3001)
            with lock:
                results.append(result["allowed"])

        threads = [threading.Thread(target=worker) for _ in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert results.count(True) == 5
        assert results.count(False) == 19
        assert store.model_budget_status(now=3002)["used_attempts"] == 5
    finally:
        store.close()


def test_provider_cooldown_blocks_requests_without_spending_an_attempt(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    try:
        store.activate_model_budget(now=4000, window_seconds=300)
        store.reserve_model_budget(now=4001)
        store.note_model_budget_cooldown(5000, now=4002)
        status = store.model_budget_status(now=4003)
        assert status["allowed"] is False
        assert status["reason"] == "provider_cooldown"
        assert status["retry_after_ms"] == 4000
        denied = store.reserve_model_budget(now=4003)
        assert denied["allowed"] is False
        assert denied["used_attempts"] == 1
        assert store.model_budget_status(now=4008)["allowed"] is True
    finally:
        store.close()


def test_revocation_requires_a_new_explicit_window(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    try:
        store.activate_model_budget(now=5000)
        revoked = store.revoke_model_budget(now=5001)
        assert revoked["state"] == "revoked"
        assert revoked["allowed"] is False
        assert store.reserve_model_budget(now=5002)["reason"] == "test_budget_revoked"
        assert store.activate_model_budget(now=5003)["activated"] is True
    finally:
        store.close()


# --------------------------------------------------------------- web client
def test_web_mode_never_falls_back_to_opencode(mock_app):
    cfg = mock_app.cfg.with_overrides(
        llm_base_url="", llm_model="", llm_api_key="",
        opencode_url="http://127.0.0.1:4096",
        opencode_model="opencode/dev-model",
    )
    web = LLMClient.from_config(cfg, mode="web", store=mock_app.store)
    assert isinstance(web, LLMClient)
    assert web.mode == "web"
    assert web.configured is False
    assert web.model == ""
    with pytest.raises(RuntimeError):
        web.chat([{"role": "user", "content": "hi"}])

    # The development-chat path is untouched and still selects OpenCode.
    auto = LLMClient.from_config(cfg)
    assert type(auto).__name__ == "OpenCodeClient"


def test_web_client_requires_its_own_configuration_even_with_opencode(mock_app):
    cfg = web_cfg(mock_app).with_overrides(llm_base_url="", llm_model="")
    client = LLMClient.from_config(cfg, mode="web", store=mock_app.store)
    assert client.configured is False
    assert model_signature(cfg) == ("", "")


def test_budget_denial_never_reaches_the_provider_or_ledger(mock_app, monkeypatch):
    opener = patch_opener(monkeypatch, AssertionError("provider must not be called"))
    cfg = web_cfg(mock_app)
    client = LLMClient.from_config(cfg, mode="web", store=mock_app.store,
                                   request_kind="model_test")
    with pytest.raises(BudgetDenied) as error:
        client.chat([{"role": "user", "content": "hi"}])
    assert error.value.reason == "test_budget_not_activated"
    assert opener.calls == []
    assert mock_app.store.list_model_usage() == []


def test_invalid_provider_url_fails_before_spending_an_attempt(mock_app):
    cfg = web_cfg(mock_app).with_overrides(llm_base_url="http://provider.example/v1")
    mock_app.store.activate_model_budget()
    client = LLMClient.from_config(cfg, mode="web", store=mock_app.store,
                                   request_kind="model_test")
    with pytest.raises(ProviderError) as error:
        client.chat([{"role": "user", "content": "hi"}])
    assert error.value.code == "connection_failed"
    assert mock_app.store.model_budget_status()["used_attempts"] == 0
    assert mock_app.store.list_model_usage() == []


def test_successful_attempt_records_metadata_and_provider_cost(mock_app, monkeypatch):
    opener = patch_opener(monkeypatch, FakeResponse({
        "model": "provider-returned-model",
        "choices": [{"message": {"content": "OK"}}],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 7},
            "completion_tokens_details": {"reasoning_tokens": 2},
            "cost": 0.0005,
            "currency": "USD",
        },
    }))
    cfg = web_cfg(mock_app)
    mock_app.store.activate_model_budget()
    client = LLMClient.from_config(cfg, mode="web", store=mock_app.store,
                                   request_kind="model_test")
    assert client.chat([{"role": "user", "content": "hi"}]) == "OK"
    assert len(opener.calls) == 1
    assert client.response_model == "provider-returned-model"

    rows = mock_app.store.list_model_usage()
    assert len(rows) == 1
    row = rows[0]
    assert row["request_kind"] == "model_test"
    assert row["attempt_index"] == 1
    assert row["provider"] == "provider.example"
    assert row["model"] == "web-model"
    assert row["billing_project"] == "web-project"
    assert row["ok"] == 1 and row["error_code"] is None
    assert row["input_tokens"] == 11
    assert row["cached_input_tokens"] == 7
    assert row["output_tokens"] == 4
    assert row["reasoning_tokens"] == 2
    assert row["cost_amount"] == pytest.approx(0.0005)
    assert row["cost_currency"] == "USD"
    assert row["usage_source"] == "provider"
    assert row["cost_source"] == "provider"
    assert row["accounting_convention"] == "openai_inclusive"
    # The key and the message text are not in the ledger.
    dump = json.dumps(rows, default=str)
    assert "test-key" not in dump
    assert '"hi"' not in dump


def test_provider_http_error_is_recorded_and_sets_cooldown(mock_app, monkeypatch):
    url = "https://provider.example/v1/chat/completions"
    headers = email.message.Message()
    headers["Retry-After"] = "3"
    failure = urllib.error.HTTPError(
        url, 429, "Too Many Requests", headers,
        io.BytesIO(b'{"error":"rate limit"}'),
    )
    patch_opener(monkeypatch, failure)
    cfg = web_cfg(mock_app)
    mock_app.store.activate_model_budget()
    client = LLMClient.from_config(cfg, mode="web", store=mock_app.store,
                                   request_kind="explain")
    with pytest.raises(ProviderError) as error:
        client.chat([{"role": "user", "content": "hi"}])
    assert error.value.code == "rate_limited"
    assert error.value.retry_after_ms == 3000

    row = mock_app.store.list_model_usage()[0]
    assert row["ok"] == 0
    assert row["error_code"] == "rate_limited"
    assert row["http_status"] == 429
    assert row["input_tokens"] is None
    assert row["cost_amount"] is None
    assert row["retry_after_ms"] == 3000
    budget = mock_app.store.model_budget_status()
    assert budget["reason"] == "provider_cooldown"


def test_agent_engine_fails_closed_on_web_provider_failure(mock_app, monkeypatch):
    patch_opener(monkeypatch, urllib.error.URLError("offline"))
    cfg = web_cfg(mock_app)
    mock_app.store.activate_model_budget()
    agent = mock_app.store.create_agent(
        {"name": "web agent", "symbols": ["XAUUSD"], "mode": "advisory"})
    engine = AgentEngine(
        cfg=cfg, mt5=mock_app.mt5, store=mock_app.store,
        llm=LLMClient.from_config(cfg, mode="web", store=mock_app.store,
                                  request_kind="agent_run"),
    )
    report = engine.run(mock_app.store.get_agent(agent["id"]))
    assert report["used_llm"] is False
    assert report["proposals"] == []
    assert report["decisions"][0]["source"] == "model_error"
    assert report["decisions"][0]["llm_status"] == "failed"
    assert mock_app.mt5.positions() == []
    rows = mock_app.store.list_model_usage()
    assert len(rows) == 1
    assert rows[0]["request_kind"] == "agent_run"
    assert rows[0]["ok"] == 0


def test_agent_engine_stops_on_budget_denial_without_rules_fallback(mock_app, monkeypatch):
    patch_opener(monkeypatch, AssertionError("provider must not be called"))
    cfg = web_cfg(mock_app)
    agent = mock_app.store.create_agent(
        {"name": "web agent", "symbols": ["XAUUSD"], "mode": "advisory"})
    engine = AgentEngine(
        cfg=cfg, mt5=mock_app.mt5, store=mock_app.store,
        llm=LLMClient.from_config(cfg, mode="web", store=mock_app.store,
                                  request_kind="agent_run"),
    )
    report = engine.run(mock_app.store.get_agent(agent["id"]))
    assert report["used_llm"] is False
    assert report["proposals"] == []
    assert report["decisions"][0]["source"] == "model_budget"
    assert report["decisions"][0]["llm_status"] == "blocked"
    assert mock_app.mt5.positions() == []
    assert mock_app.store.list_model_usage() == []


def test_unconfigured_web_agent_fails_closed_without_rules_proposal(mock_app):
    cfg = web_cfg(mock_app).with_overrides(llm_base_url="", llm_model="")
    agent = mock_app.store.create_agent(
        {"name": "unconfigured web agent", "symbols": ["XAUUSD"], "mode": "auto"})
    engine = AgentEngine(
        cfg=cfg, mt5=mock_app.mt5, store=mock_app.store,
        llm=LLMClient.from_config(cfg, mode="web", store=mock_app.store,
                                  request_kind="agent_run"),
    )
    report = engine.run(mock_app.store.get_agent(agent["id"]))
    assert report["used_llm"] is False
    assert report["proposals"] == []
    assert report["decisions"][0]["source"] == "model_not_configured"
    assert report["decisions"][0]["llm_status"] == "blocked"
    assert mock_app.mt5.positions() == []
    assert mock_app.store.list_proposals() == []
    assert mock_app.store.list_model_usage() == []


def test_web_proposals_always_require_human_confirmation(mock_app):
    cfg = web_cfg(mock_app).with_overrides(allow_trading=True)
    client = LLMClient.from_config(cfg, mode="web", store=mock_app.store)
    engine = AgentEngine(cfg=cfg, mt5=mock_app.mt5, store=mock_app.store, llm=client)
    blockers = engine._auto_confirm_blockers(
        {"mode": "auto"}, {"scenario": {"confidence": 90}}, "auto")
    assert blockers == ["dedicated web model: explicit human confirmation required"]


def test_web_decisions_are_never_mislabeled_as_opencode(mock_app, monkeypatch):
    cfg = web_cfg(mock_app)  # This config deliberately also has an OpenCode URL.
    client = LLMClient.from_config(cfg, mode="web", store=mock_app.store)
    engine = AgentEngine(cfg=cfg, mt5=mock_app.mt5, store=mock_app.store, llm=client)
    monkeypatch.setattr(engine.llm, "chat_json",
                        lambda *_: {"action": "wait", "reason": "No setup"})
    decision = engine._decide({}, {})
    assert decision["source"] == "web"
    assert engine.cfg.opencode_url  # The development chat setting is still present.
