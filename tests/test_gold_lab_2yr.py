"""Hermetic offline tests for 2-year unseen-year OOS & extended GitHub/academic gold lab."""

from __future__ import annotations

import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from trade_mcp.gold_lab_2yr import (
    _sim_h1_donchian50_london,
    _sim_london_open_15bp_orb,
    _sim_m5_vwap_ema200_trail50,
    _sim_m15_chandelier_breakout,
    _sim_m15_ema20_h1_pullback,
    _sim_tpsl_london_cap12,
    evaluate_2yr_oos_and_github_lab,
)
from trade_mcp.m1_momentum import resample_bars_to_tf

SPEC_XAU = {
    "symbol": "XAUUSD_i",
    "point": 0.01,
    "currency_profit": "USD",
    "trade_contract_size": 100.0,
    "trade_tick_size": 0.01,
    "trade_tick_value": 0.10,
    "trade_calc_mode": 4,
    "volume_min": 0.01,
    "volume_max": 50.0,
    "volume_step": 0.01,
    "digits": 2,
}


def _make_m5_trend_series(days: int = 120) -> list[list[float]]:
    start = int(datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp())
    bars: list[list[float]] = []
    px = 2800.0
    for i in range(days * 288):
        t = start + i * 300
        hr = (t // 3600) % 24
        drift = 0.08 if (i % 600) < 380 else -0.05
        if 7 <= hr < 19:
            drift *= 1.5
        o = px
        c = o + drift + math.sin(i / 29.0) * 0.35
        h = max(o, c) + (1.4 if 7 <= hr < 19 else 0.35)
        low = min(o, c) - (1.4 if 7 <= hr < 19 else 0.35)
        bars.append([float(t), round(o, 2), round(h, 2), round(low, 2), round(c, 2), float(40 + (i % 25))])
        px = c
    return bars


def test_tpsl_cap12_and_london_15bp_orb_triggers() -> None:
    """Both capped London box and +/-15bp London-open ORB trigger cleanly and cap loss."""
    base = int(datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc).timestamp())  # Wednesday
    bars: list[list[float]] = []
    for i in range(260):
        t = base + i * 300
        hr = (t // 3600) % 24
        if hr < 5:
            # Athens 03-08 (UTC 00-05 in EEST): clean ~$11 box (~0.35% of $3,100)
            o = 3100.0 + (i % 6) * 1.5
            bars.append([float(t), o, o + 3.0, 3098.0, o + 0.5, 40.0])
        elif hr < 7:
            bars.append([float(t), 3106.0, 3108.0, 3104.0, 3107.0, 45.0])
        else:
            # Strong breakout rally through buy_stop and beyond
            step = (i - 84) * 1.6
            o = 3108.0 + step
            bars.append([float(t), o, o + 2.2, o - 0.6, o + 1.5, 65.0])

    s_cap, tr_cap = _sim_tpsl_london_cap12(bars, spec=SPEC_XAU, spread_price=0.34)
    assert s_cap["trades"] == 1
    assert tr_cap[0]["side"] == "buy"
    assert tr_cap[0]["net_pnl_usd"] > 0

    s_orb, tr_orb = _sim_london_open_15bp_orb(bars, spec=SPEC_XAU, spread_price=0.34)
    assert s_orb["trades"] == 1
    assert tr_orb[0]["side"] == "buy"
    assert tr_orb[0]["net_pnl_usd"] > 0


def test_h1_donchian_and_m15_strategies_and_vwap_trigger() -> None:
    """All 4 new multi-bar strategies fire causally without lookahead or exceptions."""
    m5 = _make_m5_trend_series(days=45)
    m15 = resample_bars_to_tf(m5, src_seconds=300, dst_seconds=900)
    h1 = resample_bars_to_tf(m15, src_seconds=900, dst_seconds=3600)

    s_dc, _ = _sim_h1_donchian50_london(h1, spec=SPEC_XAU, spread_price=0.34)
    assert s_dc["trades"] >= 1

    s_ch, _ = _sim_m15_chandelier_breakout(m15, h1_bars=h1, spec=SPEC_XAU, spread_price=0.34)
    assert s_ch["trades"] >= 1

    s_pb, _ = _sim_m15_ema20_h1_pullback(m15, h1_bars=h1, spec=SPEC_XAU, spread_price=0.34)
    assert s_pb["trades"] >= 1

    s_vw, _ = _sim_m5_vwap_ema200_trail50(m5, spec=SPEC_XAU, spread_price=0.34)
    assert s_vw["trades"] >= 1


def test_evaluate_2yr_oos_and_github_lab_structure() -> None:
    """Full 8-engine + portfolio evaluator splits Y1/Y2 cleanly and submits zero orders."""
    m5 = _make_m5_trend_series(days=120)
    m15 = resample_bars_to_tf(m5, src_seconds=300, dst_seconds=900)
    h1 = resample_bars_to_tf(m15, src_seconds=900, dst_seconds=3600)

    res = evaluate_2yr_oos_and_github_lab(
        m5_bars=m5,
        m15_bars=m15,
        h1_bars=h1,
        spec=SPEC_XAU,
        spread_price=0.34,
        year2_days=60,
        mc_trials=100,
    )
    assert res["ok"] is True
    assert res["orders_submitted"] == 0
    assert res["executable"] is False
    assert res["retention"]["m5"]["has_unseen_y1"] is True
    assert res["retention"]["h1"]["has_unseen_y1"] is True
    ids = [e["id"] for e in res["engines"]]
    assert ids == [
        "M5_MOMENTUM_G5_ADX20_35",
        "TPSL_LONDON_NATIVE",
        "TPSL_LONDON_CAP12",
        "H1_DONCHIAN50_LONDON",
        "M15_CHANDELIER_BREAKOUT",
        "M15_EMA20_H1_PULLBACK",
        "M5_VWAP_EMA200_TRAIL50",
        "M5_LONDON_OPEN_15BP_ORB",
    ]
    for e in res["engines"]:
        assert e["unseen_year1_2024_2025"] is not None
        assert "unseen_year1_positive_and_pf_ge_1_15" in e["gates"]
    assert res["portfolio_combo"]["components"] == ["M5_MOMENTUM_G5_ADX20_35", "TPSL_LONDON_CAP12"]


def test_cli_mock_smoke() -> None:
    """Read-only CLI executes cleanly in --mock --summary mode and writes 0o600 JSON."""
    out_file = Path("logs/_pytest_gold_lab_2yr_smoke.json")
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/daytrade_2yr_oos_and_github_lab.py",
                "--mock",
                "--mc-trials",
                "50",
                "--summary",
                "--out",
                str(out_file),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ORDERS=0" in proc.stdout
        assert "BASIS=contract_size" in proc.stdout
        assert "COMBINED PORTFOLIO" in proc.stdout
        assert out_file.exists()
        assert (out_file.stat().st_mode & 0o777) == 0o600
    finally:
        if out_file.exists():
            out_file.unlink()
