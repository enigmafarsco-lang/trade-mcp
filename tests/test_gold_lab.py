from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path

from trade_mcp.gold_lab import (
    evaluate_live_m5_snapshot,
    monte_carlo_drawdown,
)
from trade_mcp.m1_momentum import HTFContext, htf_allows_signal

ROOT = Path(__file__).resolve().parents[1]


def test_monte_carlo_drawdown_deterministic_and_bounds():
    pnls = [15.0, -10.0, -10.0, 25.0, -8.0, 30.0, -12.0, -12.0, 18.0, -6.0]
    r1 = monte_carlo_drawdown(pnls, trials=500, seed=42)
    r2 = monte_carlo_drawdown(pnls, trials=500, seed=42)
    assert r1 == r2
    assert r1["trades"] == len(pnls)
    assert r1["p95_max_dd_usd"] >= r1["median_max_dd_usd"] > 0.0
    assert r1["worst_max_dd_usd"] >= r1["p99_max_dd_usd"]

    empty = monte_carlo_drawdown([], trials=100, seed=1)
    assert empty["trades"] == 0 and empty["p95_max_dd_usd"] == 0.0


def test_max_mid_adx_blocks_exhausted_trends():
    m15_ok = HTFContext(close_time=1000, close=3620.0, ema_fast=3618.0, ema_slow=3615.0,
                        adx=26.0, atr=4.0, donchian_pos=0.6, bias="buy")
    h1_exhausted = HTFContext(close_time=1000, close=3620.0, ema_fast=3615.0, ema_slow=3610.0,
                              adx=42.0, atr=10.0, donchian_pos=0.5, bias="buy")
    h1_healthy = HTFContext(close_time=1000, close=3620.0, ema_fast=3615.0, ema_slow=3610.0,
                            adx=27.0, atr=10.0, donchian_pos=0.5, bias="buy")
    h4_ok = HTFContext(close_time=1000, close=3620.0, ema_fast=3610.0, ema_slow=3600.0,
                       adx=28.0, atr=20.0, donchian_pos=0.6, bias="buy")

    ok1, r1 = htf_allows_signal(side="buy", htf_filter="m15_h1_h4", m5=m15_ok, m15=h1_exhausted, h1=h4_ok,
                                min_m5_adx=18.0, min_mid_adx=20.0, max_mid_adx=35.0)
    assert not ok1 and r1 == "htf_h1_exhausted_adx"

    ok2, r2 = htf_allows_signal(side="buy", htf_filter="m15_h1_h4", m5=m15_ok, m15=h1_healthy, h1=h4_ok,
                                min_m5_adx=18.0, min_mid_adx=20.0, max_mid_adx=35.0)
    assert ok2 and r2 is None


def test_evaluate_live_m5_snapshot_returns_all_engines_readonly():
    # Build 120 closed M5 bars in active London/NY session (10:00 UTC)
    t0 = 1795428000  # 2026-11-24T10:00:00Z
    m5 = []
    px = 3600.0
    for i in range(120):
        step = 0.25 if i < 115 else 0.9
        o = px
        c = px + step
        h = max(o, c) + 0.3
        low = min(o, c) - 0.2
        m5.append([float(t0 + i * 300), round(o, 2), round(h, 2), round(low, 2), round(c, 2), 150.0])
        px = c

    snap = evaluate_live_m5_snapshot(m5, symbol="XAUUSD_i", bid=px, ask=round(px + 0.34, 2))
    assert snap["ok"] is True
    assert snap["execution_authorized"] is False
    assert snap["orders_submitted"] == 0
    assert snap["symbol"] == "XAUUSD_i"
    assert set(snap["engines"].keys()) == {
        "m5_momentum_g5",
        "smc_bos_fvg",
        "smc_liquidity_sweep",
        "daytrader_skill_patterns",
    }
    assert "h1_regime" in snap["regime"]
    assert "recommended_action" in snap["consensus"]


def test_gold_lab_365d_cli_mock_and_no_execution_imports(tmp_path, monkeypatch, capsys):
    spec_mod = importlib.util.spec_from_file_location(
        "daytrade_gold_lab_365d", ROOT / "scripts/daytrade_gold_lab_365d.py"
    )
    cli = importlib.util.module_from_spec(spec_mod)
    assert spec_mod and spec_mod.loader
    spec_mod.loader.exec_module(cli)

    paper_spec = importlib.util.spec_from_file_location(
        "daytrade_m1_paper_window", ROOT / "scripts/daytrade_m1_paper_window.py"
    )
    paper_cli = importlib.util.module_from_spec(paper_spec)
    assert paper_spec and paper_spec.loader
    paper_spec.loader.exec_module(paper_cli)

    monkeypatch.setattr(paper_cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "_save_private_json", paper_cli._save_private_json)
    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_PAPER_TRADING", "false")

    rc = cli.main(["--mock", "--mc-trials", "200", "--out", "logs/gold-lab-smoke.json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["paper"] and payload["orders_submitted"] == 0
    bo = payload["bakeoff"]
    ids = [e["id"] for e in bo["engines"]]
    assert ids == [
        "M5_MOMENTUM_G5",
        "M5_MOMENTUM_G5_ADX20_35",
        "SMC_BOS_FVG",
        "SMC_LIQUIDITY_SWEEP",
        "SKILL_BREAKOUT20_MA20",
        "SKILL_RSI_BOUNCE",
    ]
    assert bo["engines"][1]["exploratory_in_sample_flag"] is True
    assert bo["engines"][0]["monte_carlo"]["trials"] == 200

    out_file = tmp_path / "logs" / "gold-lab-smoke.json"
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600

    rc2 = cli.main(["--mock", "--mc-trials", "200", "--summary", "--out", "logs/gold-lab-summary.json"])
    assert rc2 == 0
    txt = capsys.readouterr().out
    assert "HEAD-TO-HEAD 365-DAY GOLDAGENT LAB BAKE-OFF" in txt
    assert "MONTE_CARLO(200)" in txt

    script_text = (ROOT / "scripts/daytrade_gold_lab_365d.py").read_text(encoding="utf-8")
    assert "propose_open" not in script_text and "trade_send_market_order" not in script_text


def test_smc_bos_fvg_and_liquidity_sweep_actually_trigger():
    from trade_mcp.gold_lab import _sim_single_position_engine
    from trade_mcp.m1_momentum import build_htf_index

    spec = {
        "trade_tick_size": 0.01, "trade_tick_value": 0.1, "trade_contract_size": 100.0,
        "currency_profit": "USD", "trade_calc_mode": "CFD Leverage",
        "digits": 2, "volume_step": 0.01, "volume_min": 0.01, "volume_max": 50.0, "point": 0.01,
    }
    t0 = 1795428000  # 2026-11-24T10:00:00Z (London session)
    # Build rising H1/H4 HTF warmup so bullish bias is strictly aligned
    htf_bars = [[float(t0 - (60 - k) * 3600), 3500 + k, 3503 + k, 3499 + k, 3502 + k, 200.0] for k in range(60)]
    htf_idx = (
        build_htf_index(htf_bars, period_seconds=900, ema_fast=9, ema_slow=21),
        build_htf_index(htf_bars, period_seconds=3600, ema_fast=9, ema_slow=21),
        build_htf_index(htf_bars, period_seconds=14400, ema_fast=20, ema_slow=50),
    )

    # 1) Craft clear bullish swing high -> pullback -> BOS + FVG -> FVG retest -> rally
    bars_fvg: list[list[float]] = []
    for k in range(25):
        bars_fvg.append([float(t0 + k * 300), 3600.0, 3601.0, 3599.0, 3600.2, 100.0])
    # Swing high at index 26 (high=3604), flanked by lower highs at 24,25,27,28
    bars_fvg.append([float(t0 + 25 * 300), 3600.2, 3602.0, 3600.0, 3601.5, 110.0])
    bars_fvg.append([float(t0 + 26 * 300), 3601.5, 3604.0, 3601.0, 3603.0, 120.0])
    bars_fvg.append([float(t0 + 27 * 300), 3603.0, 3603.2, 3600.5, 3601.0, 110.0])
    bars_fvg.append([float(t0 + 28 * 300), 3601.0, 3601.5, 3599.5, 3600.0, 100.0])
    # BOS + FVG across 29 (b3), 30 (b2), 31 (b1): b3.high=3601.2 < b1.low=3604.5, b1.close=3608 > swing_high=3604
    bars_fvg.append([float(t0 + 29 * 300), 3600.0, 3601.2, 3599.8, 3601.0, 150.0])
    bars_fvg.append([float(t0 + 30 * 300), 3601.0, 3606.0, 3601.0, 3605.5, 220.0])
    bars_fvg.append([float(t0 + 31 * 300), 3605.5, 3608.5, 3604.5, 3608.0, 240.0])
    # Retest into FVG zone [3601.2, 3604.5] at bar 32 -> rallies to 3620
    bars_fvg.append([float(t0 + 32 * 300), 3607.5, 3608.0, 3603.0, 3607.5, 180.0])
    for k in range(33, 40):
        px = 3607.5 + (k - 32) * 2.0
        bars_fvg.append([float(t0 + k * 300), px, px + 2.5, px - 0.5, px + 2.0, 150.0])

    s_fvg, tr_fvg = _sim_single_position_engine(
        bars_fvg, spec=spec, htf_indices=htf_idx, signal_generator="smc_bos_fvg", spread_price=0.34,
    )
    assert s_fvg["trades"] >= 1 and tr_fvg[0]["side"] == "buy"

    # 2) Craft clear bullish swing low -> wick sweeps below swing low & rejects -> rally
    bars_sw: list[list[float]] = []
    for k in range(25):
        bars_sw.append([float(t0 + k * 300), 3605.0, 3606.0, 3604.0, 3605.0, 100.0])
    # Swing low at index 26 (low=3600.0), flanked by higher lows at 24,25,27,28
    bars_sw.append([float(t0 + 25 * 300), 3605.0, 3605.5, 3602.0, 3603.0, 100.0])
    bars_sw.append([float(t0 + 26 * 300), 3603.0, 3603.5, 3600.0, 3602.5, 130.0])
    bars_sw.append([float(t0 + 27 * 300), 3602.5, 3605.0, 3602.0, 3604.5, 110.0])
    bars_sw.append([float(t0 + 28 * 300), 3604.5, 3605.5, 3603.0, 3604.0, 100.0])
    bars_sw.append([float(t0 + 29 * 300), 3604.0, 3604.5, 3602.5, 3603.0, 100.0])
    # Sweep bar at index 30: sweeps below 3600.0 to 3597.5, closes firmly back above at 3602.5 (huge lower wick!)
    bars_sw.append([float(t0 + 30 * 300), 3603.0, 3603.2, 3597.5, 3602.5, 250.0])
    for k in range(31, 38):
        px = 3602.5 + (k - 30) * 2.0
        bars_sw.append([float(t0 + k * 300), px, px + 2.5, px - 0.5, px + 2.0, 150.0])

    s_sw, tr_sw = _sim_single_position_engine(
        bars_sw, spec=spec, htf_indices=htf_idx, signal_generator="smc_liquidity_sweep", spread_price=0.34,
    )
    assert s_sw["trades"] >= 1 and tr_sw[0]["side"] == "buy"

