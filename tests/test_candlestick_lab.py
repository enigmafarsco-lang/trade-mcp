"""Hermetic offline tests for Japanese Candlestick & Walk-Forward 1-2 Bar Discovery Lab."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from trade_mcp.candlestick_lab import (
    _atr14,
    _ema,
    detect_classic_patterns,
    evaluate_timeframe_candlesticks,
    make_synthetic_bars_with_embedded_edge,
    resample_ohlcv,
)


def _gold_spec() -> dict[str, object]:
    return {
        "point": 0.01,
        "trade_tick_size": 0.01,
        "trade_tick_value": 0.10,
        "trade_contract_size": 100.0,
        "currency_base": "XAU",
        "currency_profit": "USD",
        "volume_min": 0.01,
        "volume_max": 50.0,
        "volume_step": 0.01,
        "trade_stops_level": 0.0,
    }


def test_resample_h1_to_h4_ohlcv() -> None:
    # 4 aligned H1 bars starting at epoch multiple of 14400
    t0 = 1700006400  # divisible by 14400
    bars = [
        [float(t0 + 0), 100.0, 105.0, 99.0, 102.0, 10.0],
        [float(t0 + 3600), 102.0, 108.0, 101.0, 104.0, 20.0],
        [float(t0 + 7200), 104.0, 106.0, 97.0, 100.0, 30.0],
        [float(t0 + 10800), 100.0, 103.0, 98.0, 101.5, 40.0],
    ]
    h4 = resample_ohlcv(bars, 14400)
    assert len(h4) == 1
    assert h4[0] == [float(t0), 100.0, 108.0, 97.0, 101.5, 100.0]


def test_detect_classic_hammer_and_engulfing_causally() -> None:
    t0 = 1700000000
    bars: list[list[float]] = []
    px = 2600.0
    # 30 bars steady downtrend so prior_down is True
    for i in range(30):
        o = px
        c = px - 3.0
        bars.append([float(t0 + i * 3600), o, o + 0.8, c - 0.8, c, 100.0])
        px = c
    # Bar 30: textbook Hammer (long lower wick >= 2*body, tiny upper wick, small body)
    o = px
    bars.append([float(t0 + 30 * 3600), o, o + 0.2, o - 12.0, o + 1.0, 150.0])
    # Bar 31: red continuation
    bars.append([float(t0 + 31 * 3600), o + 1.0, o + 1.2, o - 3.0, o - 2.5, 120.0])
    # Bar 32: textbook Bullish Engulfing
    bars.append([float(t0 + 32 * 3600), o - 2.6, o + 4.5, o - 2.8, o + 4.0, 200.0])
    # Padding 3 bars for causal exit window
    for k in range(33, 36):
        bars.append([float(t0 + k * 3600), o + 4.0, o + 5.0, o + 3.0, o + 4.5, 100.0])

    ema20 = _ema([b[4] for b in bars], 20)
    atr = _atr14(bars, 14)
    hits = detect_classic_patterns(bars, ema20, atr)
    assert (30, 1) in hits["HAMMER"]
    assert (32, 1) in hits["ENGULFING"]


def test_miner_recovers_synthetic_embedded_edge_in_blind_oos() -> None:
    bars = make_synthetic_bars_with_embedded_edge(n_bars=1500, bar_seconds=3600, seed=11)
    rep = evaluate_timeframe_candlesticks(
        tf_label="H1",
        bars=bars,
        spec=_gold_spec(),
        spread_px=0.34,
        min_train_n=15,
        min_test_n=8,
        mc_trials=60,
    )
    assert rep["miner"]["oos_verified_count"] >= 1
    top = rep["miner"]["oos_verified"][0]
    assert top["oos_passed"] is True
    assert top["trades_test_oos"]["net"] > 0
    assert top["trades_stress"]["net"] > 0


def test_candlestick_cli_mock_smoke() -> None:
    out_file = Path("logs/_pytest_candlestick_smoke.json")
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/daytrade_candlestick_lab.py",
                "--mock",
                "--mc-trials",
                "40",
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
        assert "OOS!" in proc.stdout
        assert out_file.exists() and (out_file.stat().st_mode & 0o777) == 0o600
    finally:
        if out_file.exists():
            out_file.unlink()
