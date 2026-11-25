"""Hermetic offline tests for the Pullback-Rejection & Bounded Scale-In Basket Lab."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daytrade_pullback_basket_lab as cli  # noqa: E402

from trade_mcp.pullback_basket_lab import (  # noqa: E402
    BasketEngineConfig,
    evaluate_pullback_basket_suite,
    make_synthetic_pullback_bars,
    simulate_basket_engine,
)


def test_synthetic_pullback_suite_executes_bounded_baskets() -> None:
    bars = make_synthetic_pullback_bars(n_bars=800, bar_seconds=60)
    spec = {
        "point": 0.01, "trade_tick_size": 0.01, "trade_tick_value": 0.10,
        "trade_contract_size": 100.0, "currency_base": "XAU", "currency_profit": "USD",
    }
    res = evaluate_pullback_basket_suite(
        tf_label="M1", bars=bars, spec=spec, spread_price=0.31, mc_trials=30,
    )
    assert res["timeframe"] == "M1"
    assert len(res["engines"]) == 7
    by_name = {e["engine"]: e for e in res["engines"]}
    assert by_name["M1_PRE15_PB5_REJ_1LEG"]["base"]["trades"] > 0
    assert by_name["M1_PRE15_PB5_BASKET_3LEG"]["base"]["trades"] > 0
    assert by_name["M1_PRE15_PB5_BASKET_3LEG"]["max_lots"] == 0.03
    assert by_name["M1_PRE15_PB5_BASKET_3LEG"]["avg_legs"] > 1.0
    assert by_name["M1_PRE15_PB5_BASKET_3LEG"]["exit_reasons"].get("tp", 0) > 0
    assert by_name["M1_PRE15_PB5_BASKET_3LEG"]["base"]["net"] > 0


def test_sl_takes_worst_case_priority_over_tp_on_same_bar() -> None:
    """If a single bar spans both SL and TP, hard SL must win (zero optimistic bias)."""
    t0 = 1764028800.0
    bars: list[list[float]] = []
    px = 3500.0
    # 60 calm uptrend bars so EMA9 > EMA21 > EMA50
    for i in range(60):
        o = px
        c = px + 0.5
        bars.append([t0 + i * 60, o, c + 0.3, o - 0.3, c, 100.0])
        px = c
    # Bar 60: big bullish expansion bar triggering BUY on bar 61 open
    o = px
    c = px + 5.0
    bars.append([t0 + 60 * 60, o, c + 0.2, o - 0.2, c, 300.0])
    px = c
    # Bar 61: entry open, whipsaws BOTH down -$20 (SL) and up +$20 (TP) on same bar
    bars.append([t0 + 61 * 60, px, px + 20.0, px - 20.0, px + 10.0, 500.0])
    # Extra tail bar
    bars.append([t0 + 62 * 60, px + 10.0, px + 11.0, px + 9.0, px + 10.0, 100.0])
    # Pad to >= 80 bars
    for k in range(63, 85):
        bars.append([t0 + k * 60, px + 10.0, px + 10.5, px + 9.5, px + 10.0, 100.0])

    cfg = BasketEngineConfig(
        name="TEST_EXP", mode="exp_bar", max_legs=1, tp_atr=0.8, sl_atr=1.8,
        max_hold_bars=5, exp_atr_mult=2.0, body_ratio_min=0.5, min_atr_price=0.2,
    )
    sim = simulate_basket_engine(bars, cfg, spread_price=0.30, multiplier=100.0)
    assert sim["baskets"] >= 1
    first = sim["trades_meta"][0]
    assert first["reason"] == "sl"
    assert first["pnl"] < 0


def test_pullback_basket_cli_mock_smoke(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_PAPER_TRADING", "true")
    out_file = ROOT / "logs" / "_test_pb_basket_mock.json"
    if out_file.exists():
        out_file.unlink()
    try:
        rc = cli.main(["--mock", "--mc-trials", "20", "--summary", "--out", str(out_file)])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "OK=True ORDERS=0" in captured
        assert out_file.exists()
    finally:
        if out_file.exists():
            out_file.unlink()
