import pytest

from trade_mcp import analysis
from trade_mcp.bridge.base import BridgeError


def _proposal(app, **kwargs):
    from trade_mcp import execution

    params = dict(
        mt5=app.mt5,
        cfg=app.cfg,
        store=app.store,
        symbol="XAUUSD",
        side="buy",
        stop_loss=None,
        risk_percent=0.5,
    )
    params.update(kwargs)
    return execution.propose_open(**params)


def test_proposal_requires_stop_or_volume(mock_app):
    out = _proposal(mock_app)
    assert out["ok"] is False
    assert out["error"] == "missing_stop_loss"


def test_proposal_is_blocked_when_trading_disabled(mock_app):
    price = mock_app.mt5.tick("XAUUSD")["ask"]
    out = _proposal(mock_app, stop_loss=price - 3.0)
    assert out["ok"] is True
    assert out["blocked"] is True
    assert any("trading_disabled" in b for b in out["blockers"])
    assert out["proposal_id"]

    from trade_mcp import execution

    confirm = execution.confirm(
        mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store, proposal_id=out["proposal_id"]
    )
    assert confirm["ok"] is False
    assert confirm["error"] == "trading_disabled"
    # nothing must have been opened
    assert mock_app.mt5.positions() == []


def test_confirm_executes_and_journals(mock_app):
    mock_app.cfg.allow_trading = True
    price = mock_app.mt5.tick("XAUUSD")["ask"]
    out = _proposal(mock_app, stop_loss=price - 3.0, risk_percent=0.5)
    assert out["blocked"] is False, out["blockers"]

    from trade_mcp import execution

    result = execution.confirm(
        mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store, proposal_id=out["proposal_id"]
    )
    assert result["ok"] is True, result
    assert result["result"]["retcode"] == 10009

    positions = mock_app.mt5.positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "XAUUSD"

    prop = mock_app.store.get_proposal(out["proposal_id"])
    assert prop["status"] == "executed"
    assert mock_app.store.list_trades(limit=5)[0]["symbol"] == "XAUUSD"

    # a proposal cannot be confirmed twice
    again = execution.confirm(
        mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store, proposal_id=out["proposal_id"]
    )
    assert again["ok"] is False
    assert again["error"] == "not_pending"


def test_confirm_refuses_stale_price(mock_app):
    mock_app.cfg.allow_trading = True
    price = mock_app.mt5.tick("XAUUSD")["ask"]
    out = _proposal(mock_app, stop_loss=price - 8.0)
    # simulate a large market move after the proposal
    payload = mock_app.store.get_proposal(out["proposal_id"])["payload"]
    payload["reference_price"] = payload["reference_price"] - 20.0
    mock_app.store._conn.execute(
        "UPDATE proposals SET payload = ? WHERE id = ?",
        (__import__("json").dumps(payload), out["proposal_id"]),
    )
    mock_app.store._conn.commit()

    from trade_mcp import execution

    result = execution.confirm(
        mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store, proposal_id=out["proposal_id"]
    )
    assert result["ok"] is False
    assert result["error"] == "price_moved"
    assert mock_app.mt5.positions() == []


def test_manage_position_close_and_modify(mock_app):
    mock_app.cfg.allow_trading = True
    price = mock_app.mt5.tick("XAUUSD")["ask"]
    out = _proposal(mock_app, stop_loss=price - 4.0)
    from trade_mcp import execution

    execution.confirm(
        mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store, proposal_id=out["proposal_id"]
    )
    ticket = mock_app.mt5.positions()[0]["ticket"]

    # tightening the stop is allowed
    mod = execution.propose_manage(
        mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store,
        action="modify", ticket=ticket, sl=price - 2.0,
    )
    assert mod["ok"] is True and mod["blocked"] is False
    execution.confirm(
        mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store, proposal_id=mod["proposal_id"]
    )
    assert mock_app.mt5.positions()[0]["sl"] == pytest.approx(price - 2.0, abs=0.01)

    # widening the stop is refused
    widen = execution.propose_manage(
        mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store,
        action="modify", ticket=ticket, sl=price - 9.0,
    )
    assert widen["blocked"] is True
    assert any(i["code"] == "stop_widening" for i in widen["issues"])

    close = execution.propose_manage(
        mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store, action="close", ticket=ticket
    )
    assert close["ok"] is True and close["blocked"] is False
    done = execution.confirm(
        mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store, proposal_id=close["proposal_id"]
    )
    assert done["ok"] is True
    assert mock_app.mt5.positions() == []


def test_manage_unknown_ticket(mock_app):
    from trade_mcp import execution

    out = execution.propose_manage(
        mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store, action="close", ticket=999
    )
    assert out["ok"] is False
    assert out["error"] == "position_not_found"


def test_cancel_proposal(mock_app):
    price = mock_app.mt5.tick("XAUUSD")["ask"]
    out = _proposal(mock_app, stop_loss=price - 3.0)
    from trade_mcp import execution

    cancelled = execution.cancel(store=mock_app.store, proposal_id=out["proposal_id"])
    assert cancelled["ok"] is True
    mock_app.cfg.allow_trading = True
    result = execution.confirm(
        mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store, proposal_id=out["proposal_id"]
    )
    assert result["error"] == "not_pending"


def test_unknown_symbol_raises_bridge_error(mock_app):
    with pytest.raises(BridgeError):
        mock_app.mt5.symbol_info("NOTAREALSYMBOL")


def test_analysis_scenario_is_actionable(mock_app):
    result = analysis.analyze_symbol(
        mt5=mock_app.mt5, cfg=mock_app.cfg, timeframes=("M15", "H1"), bars_count=300
    )
    assert result["scenario"]["action"] in {"buy", "sell", "wait"}
    assert result["warnings"]
