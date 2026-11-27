"""Tests for Symbol-Adaptive Multi-Asset AW_V4 Wrapper & Multi-Symbol Spread Audit (ORDERS=0)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_multi_symbol_waves as msa  # noqa: E402

from trade_mcp.multi_symbol_aw_v4 import (  # noqa: E402
    NormalizedAllWeatherV4,
    compute_risk_parity_lot_scale,
    load_alpari_specs,
)
from trade_mcp.s1_dual_radar import make_synthetic_dual_session  # noqa: E402


def _restore_trade_mcp_env(prev_allow: str | None, prev_paper: str | None) -> None:
    if prev_allow is None:
        os.environ.pop("TRADE_MCP_ALLOW_TRADING", None)
    else:
        os.environ["TRADE_MCP_ALLOW_TRADING"] = prev_allow
    if prev_paper is None:
        os.environ.pop("TRADE_MCP_PAPER_TRADING", None)
    else:
        os.environ["TRADE_MCP_PAPER_TRADING"] = prev_paper


def test_alpari_specs_and_currency_conversion() -> None:
    specs = load_alpari_specs(ROOT / "reports" / "paper_live" / "alpari_wave_symbols_scan.json")
    assert specs["NAS100_i"].contract_size == 1.0
    assert specs["NAS100_i"].currency_profit == "USD"
    assert specs["XAGUSD_i"].contract_size == 5000.0
    assert specs["WTI_i"].contract_size == 1000.0
    assert specs["GER40_i"].currency_profit == "EUR"
    assert abs(specs["GER40_i"].usd_per_price_unit(25000.0, eur_usd=1.10) - 1.10) < 1e-9
    assert specs["USDJPY_i"].currency_profit == "JPY"
    assert abs(specs["USDJPY_i"].usd_per_price_unit(160.0) - 625.0) < 1e-6


def test_risk_parity_lot_scale_handles_parity_and_min_lot_clamp() -> None:
    # NAS100_i: S=10.0 -> SL=28 pts -> $28/lot -> 0.20 lot = $5.60 exact Gold 1x parity
    ls, rlot, sl_usd, clamped = compute_risk_parity_lot_scale(
        price_scale=10.0, usd_per_price_unit=1.0, volume_min=0.01, volume_step=0.01
    )
    assert clamped is False
    assert rlot == 0.20
    assert ls == 10.0
    assert abs(sl_usd - 5.60) < 1e-6

    # XAGUSD_i: S=0.09 -> SL=0.252 -> $1260/lot -> broker 0.02 splittable min = $25.20 SL (flagged!)
    ls_ag, rlot_ag, sl_ag, clamped_ag = compute_risk_parity_lot_scale(
        price_scale=0.09, usd_per_price_unit=5000.0, volume_min=0.01, volume_step=0.01
    )
    assert clamped_ag is True
    assert rlot_ag == 0.02
    assert ls_ag == 1.0
    assert abs(sl_ag - 25.20) < 1e-6

    # Broker with 0.10 volume_min / step (e.g. certain index contracts)
    ls_idx, rlot_idx, _, clamped_idx = compute_risk_parity_lot_scale(
        price_scale=10.0, usd_per_price_unit=1.0, volume_min=0.10, volume_step=0.10
    )
    assert clamped_idx is False
    assert rlot_idx == 0.20
    assert ls_idx == 10.0


def test_normalized_aw_v4_exact_scale_and_pnl_isomorphism() -> None:
    gold_ticks = make_synthetic_dual_session()

    gold = NormalizedAllWeatherV4(
        symbol="XAUUSD_i",
        price_scale=1.0,
        usd_per_price_unit=100.0,
        lot_scale=1.0,
        digits=2,
    )
    # NAS100-scaled stream: prices * 10.0, multiplier $1.0, lot_scale 10.0 -> exact $1.00-for-$1.00 USD PnL!
    nas = NormalizedAllWeatherV4(
        symbol="NAS100_i",
        price_scale=10.0,
        usd_per_price_unit=1.0,
        lot_scale=10.0,
        digits=1,
    )
    # Silver-scaled stream: binary-exact 0.0625 (=1/16), multiplier $5000 -> exact (5000*0.0625/100) = 3.125x USD PnL!
    xag = NormalizedAllWeatherV4(
        symbol="XAGUSD_i",
        price_scale=0.0625,
        usd_per_price_unit=5000.0,
        lot_scale=1.0,
        digits=3,
    )

    for ts, bid, ask, m1_ctx in gold_ticks:
        gold.on_tick(ts, bid, ask, m1_ctx)
        m1_nas = dict(m1_ctx)
        m1_xag = dict(m1_ctx)
        for k in ("support", "resistance", "atr_m1", "last_close"):
            if isinstance(m1_ctx.get(k), (int, float)):
                m1_nas[k] = float(m1_ctx[k]) * 10.0
                m1_xag[k] = float(m1_ctx[k]) * 0.0625
        nas.on_tick(ts, bid * 10.0, ask * 10.0, m1_nas)
        xag.on_tick(ts, bid * 0.0625, ask * 0.0625, m1_xag)

    last_ts, last_bid, last_ask, _ = gold_ticks[-1]
    gold.close_open(last_ts, last_bid, last_ask)
    nas.close_open(last_ts, last_bid * 10.0, last_ask * 10.0)
    xag.close_open(last_ts, last_bid * 0.0625, last_ask * 0.0625)

    assert len(gold.waves_closed) >= 2
    assert len(nas.waves_closed) == len(gold.waves_closed)
    assert len(xag.waves_closed) == len(gold.waves_closed)

    net_gold = sum(float(w["net_pnl_usd"]) for w in gold.waves_closed)
    net_nas = sum(float(w["net_pnl_usd"]) for w in nas.waves_closed)
    net_xag = sum(float(w["net_pnl_usd"]) for w in xag.waves_closed)

    assert abs(net_nas - net_gold) < 1e-6
    assert abs(net_xag - (3.125 * net_gold)) < 1e-4

    # Native entry prices and real lots denormalized accurately
    w0_g, w0_n = gold.waves_closed[0], nas.waves_closed[0]
    assert abs(float(w0_n["avg_entry"]) - (10.0 * float(w0_g["avg_entry"]))) < 0.2
    assert abs(float(w0_n["total_volume"]) - (10.0 * float(w0_g["total_volume"]))) < 0.02


def test_scale_free_m1_context_has_no_gold_atr_floor() -> None:
    # 30 Silver M1 bars with $0.025 range -> true ATR ~0.025 (must NOT clamp to Gold's 0.10!)
    bars = [
        [float(i * 60), 44.000 + i * 0.005, 44.015 + i * 0.005, 43.990 + i * 0.005, 44.008 + i * 0.005, 10.0]
        for i in range(30)
    ]
    ctx = msa._scale_free_m1_context(bars)
    assert 0.01 < float(ctx["atr_m1"]) < 0.05
    assert ctx["bias"] == 1


def test_audit_multi_symbol_waves_mock_cli_runs_cleanly(capsys) -> None:
    prev_allow = os.environ.get("TRADE_MCP_ALLOW_TRADING")
    prev_paper = os.environ.get("TRADE_MCP_PAPER_TRADING")
    try:
        rc = msa.main(["--mock"])
    finally:
        _restore_trade_mcp_env(prev_allow, prev_paper)
    out = capsys.readouterr().out
    assert rc == 0
    assert "SECTION 1 — REAL TICK-BY-TICK SPREAD ANATOMY" in out
    assert "V4_SHIPPED_1.0" in out
    assert "NAS100_i" in out
    assert "XAGUSD_i" in out
    assert "USDJPY_i" in out
    assert "REJECT (SPREAD_HEAVY / DEAD RANGE)" in out
