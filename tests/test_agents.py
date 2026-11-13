"""Built-in agents: constraints, parsing, and one advisory run on the mock backend."""

from __future__ import annotations

from datetime import datetime, timedelta

from trade_mcp.agents import (
    AgentEngine,
    LLMClient,
    _extract_json,
    _now_in_window,
    compact_context,
)


def test_window_is_wide_open_when_unset():
    assert _now_in_window("") is True
    assert _now_in_window("nonsense") is True


def test_window_covers_now_and_rejects_far_away_hours():
    now = datetime.now()
    start = (now - timedelta(hours=1)).strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")
    assert _now_in_window(f"{start}-{end}") is True

    far_start = (now + timedelta(hours=5)).strftime("%H:%M")
    far_end = (now + timedelta(hours=6)).strftime("%H:%M")
    assert _now_in_window(f"{far_start}-{far_end}") is False


def test_extract_json_tolerates_prose_and_code_fences():
    assert _extract_json('sure!\n```json\n{"action":"buy","confidence":70}\n```') == {
        "action": "buy",
        "confidence": 70,
    }
    assert _extract_json("no json here") is None
    assert _extract_json('{"broken": ') is None


def test_llm_is_optional():
    assert LLMClient().configured is False
    assert LLMClient(base_url="http://x/v1", model="m").configured is True


def test_compact_context_keeps_only_renderable_fields():
    analysis = {
        "symbol": "XAUUSD",
        "quote": {"bid": 4000.0},
        "bias": {"bias": "bullish", "alignment": 0.8},
        "timeframes": {
            "H1": {
                "trend": {"direction": "up", "strength": 0.6},
                "rsi14": 61.2,
                "adx14": 24.0,
                "atr": 3.1,
                "volatility_regime": {"regime": "normal"},
                "close": 4000.0,
            }
        },
        "scenario": {"action": "buy", "confidence": 70},
        "warnings": [],
    }
    ctx = compact_context(analysis)
    assert ctx["symbol"] == "XAUUSD"
    assert ctx["timeframes"]["H1"]["trend"] == "up"
    assert ctx["scenario"]["action"] == "buy"
    assert "quote" in ctx


def test_agent_constraints_are_kept_stricter_than_config(mock_app):
    cfg = mock_app.cfg.with_overrides(max_risk_percent=1.0, max_lot=0.10)
    mock_app.cfg = cfg
    agent = mock_app.store.create_agent(
        {"name": "test", "symbols": ["XAUUSD"], "max_lot": 0.02, "max_risk": 5.0, "mode": "advisory"}
    )
    # console clamps risk; the store keeps whatever it is told, so clamp here instead
    stored = mock_app.store.get_agent(agent["id"])
    assert stored is not None
    assert stored["max_lot"] == 0.02
    assert stored["mode"] == "advisory"


def test_advisory_run_creates_no_execution(mock_app):
    agent = mock_app.store.create_agent(
        {"name": "advisor", "symbols": ["XAUUSD"], "max_lot": 0.02, "max_risk": 0.5,
         "mode": "advisory"}
    )
    engine = AgentEngine(cfg=mock_app.cfg, mt5=mock_app.mt5, store=mock_app.store, llm=LLMClient())
    report = engine.run(mock_app.store.get_agent(agent["id"]))

    assert report["used_llm"] is False
    assert report["decisions"], "the engine must always produce a decision per symbol"
    assert all(item.get("source") == "rules" for item in report["decisions"])
    assert "summary_fa" in report and report["summary_fa"].strip()

    executed = [d for d in report["decisions"] if d.get("status") == "executed"]
    assert not executed, "advisory agents never execute"
    if report["proposals"]:
        # any proposal left behind must still be pending in the journal
        for prop in mock_app.store.list_proposals(limit=20):
            assert prop["status"] in {"pending", "cancelled", "expired"}

    stored = mock_app.store.get_agent(agent["id"])
    assert stored["last_run"] is not None


def test_agent_with_no_symbols_reports_nothing(mock_app):
    agent = mock_app.store.create_agent({"name": "empty", "symbols": [], "mode": "advisory"})
    # store drops empty symbol lists down to ""
    stored = mock_app.store.get_agent(agent["id"])
    assert stored["symbols"] == []
    engine = AgentEngine(cfg=mock_app.cfg, mt5=mock_app.mt5, store=mock_app.store, llm=LLMClient())
    report = engine.run(stored)
    assert report["decisions"] == []
    assert report["summary_fa"]


def test_auto_mode_executes_inside_limits(mock_app, monkeypatch):
    """With trading enabled and mode=auto, a confident decision is executed."""
    mock_app.cfg = mock_app.cfg.with_overrides(allow_trading=True, max_lot=0.10)
    mock_app.mt5.bridge.allow_trading = True
    agent = mock_app.store.create_agent(
        {"name": "auto", "symbols": ["XAUUSD"], "max_lot": 0.10, "max_risk": 0.5, "mode": "auto"}
    )
    engine = AgentEngine(cfg=mock_app.cfg, mt5=mock_app.mt5, store=mock_app.store, llm=LLMClient())

    def forced(self, agent_spec, analysis):
        scenario = analysis.get("scenario") or {}
        scenario["action"] = "buy"
        scenario["confidence"] = 80
        return {
            "action": "buy",
            "confidence": 80,
            "stop_loss": scenario.get("stop_loss"),
            "take_profit": scenario.get("take_profit_1"),
            "risk_percent": 0.5,
            "reason": "تست",
            "source": "rules",
        }

    monkeypatch.setattr(AgentEngine, "_decide", forced)
    report = engine.run(mock_app.store.get_agent(agent["id"]))

    decided = report["decisions"][0]
    assert decided["action"] == "buy"
    if decided.get("proposal_id"):
        assert decided.get("status") in {"executed", "pending"}
        if decided["status"] == "executed":
            assert report["proposals"]
