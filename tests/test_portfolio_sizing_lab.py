"""Hermetic offline tests for multi-capital dynamic lot sizing (0.01..0.10) & combined champions lab."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from trade_mcp.portfolio_sizing_lab import (
    monte_carlo_dynamic_account,
    simulate_dynamic_account,
)


def _sample_unit_trades() -> list[dict]:
    # Mix of tighter ($8 stop) and wider ($20 stop) trades, including simultaneous overlap!
    return [
        # Trade 0 (H1): open t=1000, close t=5000, wide stop=$20, +$50 per 0.01 lot
        {"strategy": "H1", "side": "buy", "opened_at": 1000.0, "closed_at": 5000.0, "entry": 3500.0, "stop_dist_price": 20.0, "pnl_per_001": 50.0},
        # Trade 1 (M5): overlaps Trade 0! open t=2000, close t=3000, tighter stop=$8, +$18 per 0.01 lot
        {"strategy": "M5", "side": "buy", "opened_at": 2000.0, "closed_at": 3000.0, "entry": 3510.0, "stop_dist_price": 8.0, "pnl_per_001": 18.0},
        # Trade 2 (M5): open t=6000, close t=7000, stop=$10, -$10 per 0.01 lot
        {"strategy": "M5", "side": "sell", "opened_at": 6000.0, "closed_at": 7000.0, "entry": 3520.0, "stop_dist_price": 10.0, "pnl_per_001": -10.0},
        # Trade 3 (H1): open t=8000, close t=12000, tight stop=$10, +$30 per 0.01 lot
        {"strategy": "H1", "side": "buy", "opened_at": 8000.0, "closed_at": 12000.0, "entry": 3530.0, "stop_dist_price": 10.0, "pnl_per_001": 30.0},
    ]


def test_dynamic_sizing_scales_lot_with_risk_and_tracks_overlap() -> None:
    """Smaller stop distance scales lot UP within [0.01..0.10] and tracks simultaneous overlap."""
    trades = _sample_unit_trades()

    # On $4,000 at 2% ($80 risk):
    #   Trade 0 (stop $20 => $2000/lot): raw=0.04 lot => +$200
    #   Trade 1 (stop $8  => $800/lot):  raw=0.10 lot (capped at 0.10!) => +$180 (overlaps Trade 0!)
    sim = simulate_dynamic_account(
        trades,
        initial_capital=4000.0,
        risk_pct=0.02,
        min_lot=0.01,
        max_lot=0.10,
        multiplier=100.0,
        contract_size=100.0,
        leverage=500.0,
    )
    assert sim["blown_account"] is False
    assert sim["trades_executed"] == 4
    assert sim["lot_stats"]["min_lot"] == 0.04
    assert sim["lot_stats"]["max_lot"] == 0.10
    assert sim["lot_stats"]["capped_at_010_count"] >= 1
    assert sim["concurrency"]["max_simultaneous_open"] == 2
    assert sim["concurrency"]["overlap_entries"] == 1
    assert sim["final_equity_usd"] > 4500.0


def test_small_capital_100_honest_blowup_and_strict_risk_check() -> None:
    """$100 account honestly flags strict-2% infeasibility and stop-out blowup under losing streak."""
    losing = [
        {"strategy": "H1", "side": "buy", "opened_at": float(i * 1000), "closed_at": float(i * 1000 + 500), "entry": 3500.0, "stop_dist_price": 25.0, "pnl_per_001": -25.0}
        for i in range(6)
    ]
    strict = simulate_dynamic_account(
        losing,
        initial_capital=100.0,
        risk_pct=0.02,
        strict_risk_only=True,
    )
    assert strict["trades_executed"] == 0
    assert strict["skipped"]["strict_risk"] == 6

    floor_sim = simulate_dynamic_account(
        losing,
        initial_capital=100.0,
        risk_pct=0.02,
        strict_risk_only=False,
    )
    assert floor_sim["blown_account"] is True
    assert floor_sim["blown_at_trade"] <= 4

    mc = monte_carlo_dynamic_account(losing, initial_capital=100.0, risk_pct=0.02, trials=50, seed=7)
    assert mc["prob_blown_pct"] == 100.0


def test_dd_governor_activates_after_drawdown() -> None:
    """DD governor halves effective risk once drawdown exceeds threshold."""
    seq = [
        {"strategy": "H1", "side": "buy", "opened_at": 1000.0, "closed_at": 2000.0, "entry": 3500.0, "stop_dist_price": 20.0, "pnl_per_001": -70.0},
        {"strategy": "M5", "side": "buy", "opened_at": 3000.0, "closed_at": 4000.0, "entry": 3500.0, "stop_dist_price": 10.0, "pnl_per_001": 15.0},
    ]
    # On $1,000 at 3% ($30): Trade 0 -> 0.01 lot -> -$70 -> equity $930 (DD=7% < 5% threshold -> triggers governor!)
    sim = simulate_dynamic_account(
        seq,
        initial_capital=1000.0,
        risk_pct=0.03,
        dd_governor_threshold_pct=5.0,
    )
    assert sim["lot_stats"]["dd_governor_trades"] == 1


def test_champions_portfolio_cli_mock_smoke() -> None:
    """Read-only champions portfolio CLI passes offline smoke test with contract_size & 0o600."""
    out_file = Path("logs/_pytest_champions_portfolio_smoke.json")
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/daytrade_champions_portfolio_lab.py",
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
        assert "STARTING CAPITAL: $100" in proc.stdout
        assert "STARTING CAPITAL: $1,000" in proc.stdout
        assert "STARTING CAPITAL: $4,000" in proc.stdout
        assert "BOTH_COMBINED" in proc.stdout
        assert out_file.exists()
        assert (out_file.stat().st_mode & 0o777) == 0o600
    finally:
        if out_file.exists():
            out_file.unlink()
