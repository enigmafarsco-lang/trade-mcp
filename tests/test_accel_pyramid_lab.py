"""Hermetic offline tests for the Volatility-Acceleration & Winner-Only Pyramid Lab."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daytrade_accel_pyramid_lab as cli  # noqa: E402

from trade_mcp.accel_pyramid_lab import (  # noqa: E402
    AccelPyramidConfig,
    evaluate_accel_pyramid_suite,
    make_synthetic_accel_bars,
    simulate_accel_pyramid,
)


def test_winner_only_pyramid_scales_into_acceleration() -> None:
    bars = make_synthetic_accel_bars(n_bars=900, bar_seconds=60)
    spec = {
        "point": 0.01, "trade_tick_size": 0.01, "trade_tick_value": 0.10,
        "trade_contract_size": 100.0, "currency_base": "XAU", "currency_profit": "USD",
    }
    res = evaluate_accel_pyramid_suite(
        tf_label="M1", bars=bars, spec=spec, spread_price=0.32, mc_trials=30,
    )
    assert res["timeframe"] == "M1"
    assert len(res["engines"]) == 7
    by_name = {e["engine"]: e for e in res["engines"]}
    vol_cont = by_name["M1_VOL_EXPL_CONT_PYR3"]
    assert vol_cont["base"]["trades"] > 0
    assert vol_cont["total_legs"] > vol_cont["base"]["trades"]
    assert vol_cont["avg_legs"] > 1.0
    assert vol_cont["base"]["net"] > 0


def test_losing_trade_never_pyramids_or_averages_down() -> None:
    """When a trade moves adversely immediately after entry, legs must remain strictly 1."""
    t0 = 1764054000.0  # 07:00 UTC (inside London session)
    bars: list[list[float]] = []
    px = 3500.0
    for i in range(110):
        o = px
        c = px + 0.4
        bars.append([t0 + i * 60, o, c + 0.2, o - 0.2, c, 100.0])
        px = c
    # Bar 110: bullish shock bar
    bars.append([t0 + 110 * 60, px, px + 4.2, px - 0.1, px + 4.0, 400.0])
    px += 4.0
    # Bar 111..125: immediate straight adverse crash
    for k in range(111, 130):
        o = px
        c = px - 2.5
        bars.append([t0 + k * 60, o, o + 0.1, c - 0.2, c, 300.0])
        px = c

    cfg = AccelPyramidConfig(
        name="TEST_NO_AVG_DOWN", mode="vol_cont", max_legs=3, pyr_step_r=0.8,
        sl_atr=1.5, tp_atr=3.5, trail_atr=1.0, max_hold_bars=15,
        exp_atr_mult=1.1, body_ratio_min=0.5, vol_ratio_min=0.0, min_atr_price=0.2,
    )
    sim = simulate_accel_pyramid(bars, cfg, spread_price=0.30, multiplier=100.0)
    assert sim["baskets"] >= 1
    first = sim["trades_meta"][0]
    assert first["legs"] == 1
    assert first["reason"] == "sl"
    assert first["pnl"] < 0


def test_accel_pyramid_cli_mock_smoke(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_PAPER_TRADING", "true")
    out_file = ROOT / "logs" / "_test_accel_pyr_mock.json"
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
