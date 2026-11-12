import json

from trade_mcp import analysis
from trade_mcp.bridge.mock import MockBridge


def _bars(mock: MockBridge, count=300, tf="M15", symbol="XAUUSD"):
    return mock.request("rates", symbol=symbol, timeframe=tf, count=count)["rates"]


def test_mock_bars_are_stable_and_ordered():
    mock = MockBridge()
    first = _bars(mock, 50)
    second = _bars(mock, 50)
    assert [b["time"] for b in first] == [b["time"] for b in second]
    assert all(a["time"] < b["time"] for a, b in zip(first, first[1:], strict=False))
    assert all(b["low"] <= b["open"] <= b["high"] for b in first)
    assert all(b["low"] <= b["close"] <= b["high"] for b in first)


def test_profile_has_expected_fields():
    mock = MockBridge()
    prof = analysis.profile(_bars(mock), "M15", 2)
    assert prof["ok"] is True
    for key in ("ema", "rsi14", "atr14", "adx14", "donchian", "trend", "structure"):
        assert key in prof
    assert prof["trend"]["direction"] in {"up", "down", "range"}
    assert -1.0 <= prof["trend"]["score"] <= 1.0


def test_analyze_symbol_end_to_end(mock_app):
    result = analysis.analyze_symbol(
        mt5=mock_app.mt5,
        cfg=mock_app.cfg,
        timeframes=("M15", "H1", "H4"),
        bars_count=300,
    )
    assert result["ok"] is True
    assert result["symbol"] == "XAUUSD"
    assert set(result["timeframes"]) == {"M15", "H1", "H4"}
    assert result["bias"]["bias"] in {"long", "short", "neutral"}
    scenario = result["scenario"]
    assert scenario["action"] in {"buy", "sell", "wait"}
    if scenario["action"] in {"buy", "sell"}:
        low, high = scenario["entry_zone"]
        assert low < high
        assert scenario["reward_risk_1"] and scenario["reward_risk_1"] > 1.0
        assert scenario["reward_risk_2"] > scenario["reward_risk_1"]
        assert 0 < scenario["confidence"] <= 92
        assert scenario["sizing"]["volume"] > 0
        # stop must be on the correct side of the entry
        if scenario["action"] == "buy":
            assert scenario["stop_loss"] < scenario["entry_reference"]
        else:
            assert scenario["stop_loss"] > scenario["entry_reference"]
    assert result["summary"] and result["summary_fa"]
    json.dumps(result)  # must be serialisable for the MCP transport


def test_scenario_sizing_respects_risk_cap(mock_app):
    result = analysis.analyze_symbol(
        mt5=mock_app.mt5, cfg=mock_app.cfg, timeframes=("M15", "H1"), bars_count=300
    )
    sizing = result["scenario"].get("sizing")
    if sizing:
        assert sizing["risk_percent"] <= mock_app.cfg.max_risk_percent
        assert sizing["volume"] <= mock_app.cfg.max_lot


def test_history_stats():
    deals = [
        {"profit": 100, "swap": 0, "commission": -1, "symbol": "XAUUSD"},
        {"profit": -50, "swap": 0, "commission": -1, "symbol": "XAUUSD"},
        {"profit": 200, "swap": 0, "commission": -1, "symbol": "EURUSD"},
    ]
    stats = analysis.history_stats(deals, days=30)
    assert stats["deals"] == 3
    assert stats["wins"] == 2 and stats["losses"] == 1
    assert stats["win_rate_percent"] == round(2 / 3 * 100, 1)
    assert stats["profit_factor"] and stats["profit_factor"] > 1
    assert set(stats["by_symbol"]) == {"XAUUSD", "EURUSD"}


def test_symbol_resolution_handles_broker_suffix(mock_app):
    assert mock_app.mt5.resolve_symbol("XAUUSD") == "XAUUSD"
    assert mock_app.mt5.resolve_symbol("xauusd.a") == "XAUUSD.a"
    hits = mock_app.mt5.find_symbol("gold")
    assert any(h["symbol"].startswith("XAUUSD") for h in hits)
