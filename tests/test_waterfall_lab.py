"""Hermetic offline tests for Fast Waterfall / Cascade Trend-Acceleration Lab."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from trade_mcp.waterfall_momentum_lab import (
    evaluate_waterfall_suite,
    make_synthetic_waterfall_bars,
)

ROOT = Path(__file__).resolve().parents[1]


def test_waterfall_engine_captures_synthetic_35bar_crash() -> None:
    spec = {
        "point": 0.01,
        "trade_tick_size": 0.01,
        "trade_tick_value": 0.10,
        "trade_contract_size": 100.0,
        "currency_base": "XAU",
        "currency_profit": "USD",
    }
    bars = make_synthetic_waterfall_bars(n_bars=900, bar_seconds=60)
    rep = evaluate_waterfall_suite(tf_label="M1", bars=bars, spec=spec, spread_price=0.32, mc_trials=50)
    by_name = {r["engine"]: r for r in rep["engines"]}
    # Follow waterfall engines must make clear positive net profit on clean 35-bar cascades
    assert by_name["M1_WATERFALL_10B_ER60"]["base"]["trades"] >= 8
    assert by_name["M1_WATERFALL_10B_ER60"]["base"]["net"] > 0
    assert by_name["M1_WATERFALL_15B_ER55"]["base"]["net"] > 0


def test_waterfall_cli_mock_smoke() -> None:
    out_file = ROOT / "logs" / "_pytest_waterfall_smoke.json"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/daytrade_waterfall_lab.py",
                "--mock",
                "--mc-trials", "40",
                "--summary",
                "--out", str(out_file),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ORDERS=0" in proc.stdout
        assert "M1_WATERFALL_10B_ER60" in proc.stdout
        assert out_file.exists() and (out_file.stat().st_mode & 0o777) == 0o600
    finally:
        out_file.unlink(missing_ok=True)
