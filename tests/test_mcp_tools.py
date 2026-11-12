import json

import pytest


@pytest.fixture()
def mcp_tools(mock_app, monkeypatch):
    """Point the module-level singleton at the mock app and return the tool functions."""
    import trade_mcp.app as app_mod

    monkeypatch.setattr(app_mod, "_APP", mock_app, raising=False)
    from trade_mcp import server

    return server


def _jsonable(payload):
    json.dumps(payload)
    return payload


def test_status_and_account(mcp_tools):
    status = _jsonable(mcp_tools.mt5_status())
    assert status["ok"] is True
    assert status["bridge"]["backend"] == "mock"
    assert status["config"]["allow_trading"] is False

    acct = _jsonable(mcp_tools.account_info())
    assert acct["ok"] is True
    assert acct["account"]["currency"] == "USD"
    assert "balance" in acct["account"]


def test_symbol_tools(mcp_tools):
    found = mcp_tools.find_symbol("gold")
    assert found["ok"] is True and found["count"] >= 1
    spec = mcp_tools.symbol_spec("XAUUSD")
    assert spec["ok"] is True
    assert spec["info"]["digits"] == 2
    assert spec["derived"]["account_currency_loss_per_1_price_unit_per_lot"] > 0
    assert spec["allowed_by_config"] is True


def test_candles_and_snapshot(mcp_tools):
    candles = mcp_tools.get_candles("XAUUSD", "M15", 50)
    assert candles["ok"] is True
    assert candles["count"] == 50
    assert len(candles["data"][0]) == 7

    bad = mcp_tools.get_candles("XAUUSD", "M7", 10)
    assert bad["ok"] is False and bad["error"] == "bad_timeframe"

    snap = _jsonable(mcp_tools.market_snapshot("XAUUSD", "M15,H1", 200))
    assert snap["ok"] is True
    assert set(snap["timeframes"]) == {"M15", "H1"}


def test_analyze_market_tool(mcp_tools):
    out = _jsonable(mcp_tools.analyze_market("XAUUSD", "M15,H1,H4", 300))
    assert out["ok"] is True
    assert out["scenario"]["action"] in {"buy", "sell", "wait"}
    assert out["summary_fa"]


def test_position_size_tool(mcp_tools):
    price = mcp_tools.symbol_spec("XAUUSD")["quote"]["ask"]
    out = mcp_tools.position_size("XAUUSD", 0.5, price, price - 5.0)
    assert out["ok"] is True
    assert out["sizing"]["volume"] > 0
    assert out["applied_risk_percent"] == 0.5
    # risk cap is enforced
    capped = mcp_tools.position_size("XAUUSD", 99, price, price - 5.0)
    assert capped["applied_risk_percent"] == 1.0


def test_positions_history_and_risk(mcp_tools):
    positions = mcp_tools.list_positions()
    assert positions["ok"] is True and positions["count"] == 0

    hist = _jsonable(mcp_tools.trade_history(90))
    assert hist["ok"] is True and hist["deals"] >= 1

    report = _jsonable(mcp_tools.risk_report())
    assert report["ok"] is True
    assert report["limits"]["allow_trading"] is False


def test_proposal_flow_through_tools(mcp_tools):
    price = mcp_tools.symbol_spec("XAUUSD")["quote"]["ask"]
    proposal = _jsonable(
        mcp_tools.propose_order("buy", "XAUUSD", stop_loss=price - 3.0, risk_percent=0.5)
    )
    assert proposal["ok"] is True
    assert proposal["blocked"] is True  # trading disabled by default
    assert "confirm_proposal" in proposal["next_step"] or "BLOCKED" in proposal["next_step"]

    listed = mcp_tools.list_proposals()
    assert listed["count"] == 1 and listed["proposals"][0]["status"] == "pending"

    confirmed = mcp_tools.confirm_proposal(proposal["proposal_id"])
    assert confirmed["ok"] is False and confirmed["error"] == "trading_disabled"

    cancelled = mcp_tools.cancel_proposal(proposal["proposal_id"])
    assert cancelled["ok"] is True


def test_journal_tools(mcp_tools):
    added = mcp_tools.journal_add("test note", symbol="XAUUSD")
    assert added["ok"] is True
    notes = mcp_tools.journal_list(5, "XAUUSD")
    assert notes["count"] == 1
    assert notes["notes"][0]["text"] == "test note"


def test_tools_never_raise(mcp_tools, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection exploded")

    monkeypatch.setattr(mcp_tools.get_app().mt5, "account_info", boom)
    out = mcp_tools.account_info()
    assert out["ok"] is False
    assert out["error"] == "internal_error"
    assert "connection exploded" in out["message"]


def test_tool_registry(mcp_tools):
    import asyncio

    tools = asyncio.run(mcp_tools.mcp.list_tools())
    names = {t.name for t in tools}
    expected = {
        "mt5_status",
        "account_info",
        "find_symbol",
        "symbol_spec",
        "get_candles",
        "market_snapshot",
        "analyze_market",
        "position_size",
        "list_positions",
        "trade_history",
        "risk_report",
        "propose_order",
        "propose_manage_position",
        "confirm_proposal",
        "list_proposals",
        "cancel_proposal",
        "journal_add",
        "journal_list",
    }
    assert expected <= names

    prompts = asyncio.run(mcp_tools.mcp.list_prompts())
    assert {p.name for p in prompts} == {"gold_session_briefing", "trade_review"}
