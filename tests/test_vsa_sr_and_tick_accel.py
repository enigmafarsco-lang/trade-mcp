"""Hermetic offline tests for Instagram 2-candle VSA/SR lab and live 2-3s tick-accel paper scalper."""

from __future__ import annotations

import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from trade_mcp.m1_momentum import resample_bars_to_tf
from trade_mcp.vsa_sr_lab import (
    analyze_next_candles_predictability,
    evaluate_vsa_and_sr_lab,
)

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


def _make_bars(days: int = 35) -> tuple[list[list[float]], list[list[float]], list[list[float]]]:
    start = int(datetime(2025, 4, 1, 0, 0, tzinfo=timezone.utc).timestamp())
    m5: list[list[float]] = []
    px = 3100.0
    for i in range(days * 288):
        t = start + i * 300
        hr = (t // 3600) % 24
        cycle = i % 160
        drift = 0.22 if cycle < 95 else -0.16
        if 7 <= hr < 19:
            drift *= 1.4
        o = px
        c = o + drift + math.sin(i / 17.0) * 0.45
        h = max(o, c) + (0.45 if (i % 7 != 0) else 2.2)
        low = min(o, c) - (0.45 if (i % 11 != 0) else 2.2)
        vol = 35.0 + (45.0 if cycle % 15 in (3, 4) else (i % 18))
        m5.append([float(t), round(o, 2), round(h, 2), round(low, 2), round(c, 2), vol])
        px = c
    m15 = resample_bars_to_tf(m5, src_seconds=300, dst_seconds=900)
    h1 = resample_bars_to_tf(m15, src_seconds=900, dst_seconds=3600)
    return m5, m15, h1


def test_vsa_predictability_and_7_engines_run_cleanly() -> None:
    m5, m15, h1 = _make_bars(days=35)
    res = evaluate_vsa_and_sr_lab(
        m5_bars=m5,
        m15_bars=m15,
        h1_bars=h1,
        spec=SPEC_XAU,
        spread_price=0.33,
        mc_trials=50,
    )
    assert res["ok"] is True
    assert res["orders_submitted"] == 0
    assert res["executable"] is False
    assert res["predictability"]["M5"]["patterns"]["2C_PLAIN_LONDON_NY"]["n"] > 50
    assert len(res["engines"]) == 7
    assert {e["id"] for e in res["engines"]} == {
        "INSTA_2C_VSA_HOLD2_M5",
        "INSTA_2C_VSA_HOLD2_M15",
        "INSTA_2C_VSA_HOLD2_H1",
        "VSA_2C_ACCEL_TRAIL_M15",
        "SR_BREAKOUT_2C_VSA_M15",
        "SR_BOUNCE_WICK_VSA_M15",
        "SR_BREAKOUT_VSA_H1_5YR",
    }


def test_predictability_accounts_for_spread() -> None:
    m5, _m15, _h1 = _make_bars(days=12)
    stats_low = analyze_next_candles_predictability(m5, bar_seconds=300, h1_end_times=[], h1_trends=[], spread_price=0.0)
    stats_hi = analyze_next_candles_predictability(m5, bar_seconds=300, h1_end_times=[], h1_trends=[], spread_price=0.50)
    p_low = stats_low["patterns"]["2C_PLAIN_LONDON_NY"]
    p_hi = stats_hi["patterns"]["2C_PLAIN_LONDON_NY"]
    assert p_hi["avg_2bar_net_after_spread_price"] < p_low["avg_2bar_net_after_spread_price"]


def test_tick_accel_engine_respects_1h5h_trend_latency_and_profit_lock() -> None:
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import daytrade_tick_accel_paper as tap  # noqa: E402

    # 1) Counter-trend burst must be blocked when 1H/5H trend is bearish (-1)
    eng_blk = tap.TickAccelPaperEngine(min_burst_usd=0.80, latency_sec=0.25)
    t = 1000.0
    px = 3600.0
    for d in [0.05, 0.15, 0.30, 0.45, 0.55]:
        px += d
        eng_blk.on_tick(t, px, px + 0.32, trend_dir=-1)
        t += 0.5
    assert eng_blk.bursts_detected >= 1
    assert eng_blk.bursts_blocked_trend >= 1
    assert len(eng_blk.trades) == 0

    # 2) Aligned bullish burst triggers after latency and locks profit on stall
    eng_ok = tap.TickAccelPaperEngine(min_burst_usd=0.80, latency_sec=0.25)
    tap._run_mock_stream(eng_ok)
    s = eng_ok.summary()
    assert s["trades"] == 1
    assert eng_ok.trades[0]["side"] == "buy"
    assert eng_ok.trades[0]["exit_reason"] == "profit_lock_stall"
    assert eng_ok.trades[0]["net_pnl_usd"] > 0


def test_both_clis_mock_smoke() -> None:
    out_vsa = Path("logs/_pytest_vsa_sr_smoke.json")
    out_tick = Path("logs/_pytest_tick_accel_smoke.json")
    try:
        p1 = subprocess.run(
            [sys.executable, "scripts/daytrade_vsa_sr_lab.py", "--mock", "--mc-trials", "50", "--summary", "--out", str(out_vsa)],
            capture_output=True, text=True, check=False,
        )
        assert p1.returncode == 0, p1.stdout + p1.stderr
        assert "ORDERS=0" in p1.stdout
        assert "BASIS=contract_size" in p1.stdout
        assert out_vsa.exists() and (out_vsa.stat().st_mode & 0o777) == 0o600

        p2 = subprocess.run(
            [sys.executable, "scripts/daytrade_tick_accel_paper.py", "--mock", "--summary", "--out", str(out_tick)],
            capture_output=True, text=True, check=False,
        )
        assert p2.returncode == 0, p2.stdout + p2.stderr
        assert "ORDERS=0" in p2.stdout
        assert "TRADES=1" in p2.stdout
        assert out_tick.exists() and (out_tick.stat().st_mode & 0o777) == 0o600
    finally:
        for f in (out_vsa, out_tick):
            if f.exists():
                f.unlink()
