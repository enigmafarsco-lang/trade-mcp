"""Hermetic offline tests for the Live 2-Engine Champions Paper Forward-Tester."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from trade_mcp.champions_paper_engine import (
    ChampionsPaperEngine,
    save_state_atomic,
)

ROOT = Path(__file__).resolve().parents[1]


def _gold_spec() -> dict[str, object]:
    return {
        "point": 0.01,
        "trade_tick_size": 0.01,
        "trade_tick_value": 0.10,
        "trade_contract_size": 100.0,
        "currency_base": "XAU",
        "currency_profit": "USD",
    }


def test_dynamic_lot_sizing_and_dd_governor() -> None:
    eng = ChampionsPaperEngine(
        spec=_gold_spec(), initial_capital_usd=4000.0, risk_pct=2.0, min_lot=0.01, max_lot=0.10, dd_governor_pct=12.0,
    )
    # $4000 * 2% = $80 risk; stop_dist = $10 -> $1000/lot risk -> 0.08 lot
    vol, risk = eng.compute_position_lot(stop_dist_price=10.0, price=3500.0, bid=3500.0, ask=3500.32)
    assert vol == 0.08
    assert risk == 80.0

    # Trigger 15% DD (> 12% governor) -> risk halves from 2% to 1%
    eng.peak_equity_usd = 4000.0
    eng.realized_pnl_usd = -600.0  # equity = $3400 (15% DD) -> 1% = $34 -> 0.03 lot at $10 stop
    vol_dd, _ = eng.compute_position_lot(stop_dist_price=10.0, price=3500.0, bid=3500.0, ask=3500.32)
    assert vol_dd == 0.03

    # Fixed lot override ignores risk_pct
    eng_fix = ChampionsPaperEngine(spec=_gold_spec(), initial_capital_usd=4000.0, fixed_lot=0.01)
    vol_f, _ = eng_fix.compute_position_lot(stop_dist_price=10.0, price=3500.0, bid=3500.0, ask=3500.32)
    assert vol_f == 0.01


def test_h1_donchian_open_idempotent_trail_and_state_roundtrip() -> None:
    eng = ChampionsPaperEngine(spec=_gold_spec(), initial_capital_usd=1000.0, risk_pct=1.5)
    t_base = 1764028800
    h1: list[list[float]] = []
    px = 3500.0
    for i in range(56):
        c = 3500.0 + (3.0 if i % 2 == 0 else -3.0)
        h1.append([float(t_base + i * 3600), px, max(px, c) + 5.0, min(px, c) - 5.0, c, 500.0])
        px = c
    ts = int(h1[-1][0]) + 3600
    while ((ts + 3600) // 3600) % 24 not in range(8, 16):
        h1.append([float(ts), 3500.0, 3506.0, 3494.0, 3501.0, 500.0])
        ts += 3600
    h1.append([float(ts), 3502.0, 3527.0, 3501.0, 3525.0, 900.0])

    evs = eng.on_bars(m5_bars=[], m15_bars=[], h1_bars=h1, bid=3525.0, ask=3525.32, server_now_ts=float(ts + 3605))
    assert any(e["type"] == "OPEN" for e in evs)
    assert eng.ENGINE_H1 in eng.positions
    orig_tid = eng.positions[eng.ENGINE_H1]["trade_id"]

    # Calling on_bars again with same closed bar MUST NOT open a duplicate trade
    evs_dup = eng.on_bars(m5_bars=[], m15_bars=[], h1_bars=h1, bid=3525.0, ask=3525.32, server_now_ts=float(ts + 3610))
    assert evs_dup == []
    assert eng.positions[eng.ENGINE_H1]["trade_id"] == orig_tid

    # Save & reload state atomically inside logs/
    st_file = ROOT / "logs" / "_pytest_champions_state.json"
    try:
        save_state_atomic(st_file, eng.to_state_dict())
        assert (st_file.stat().st_mode & 0o777) == 0o600
        eng2 = ChampionsPaperEngine(spec=_gold_spec(), initial_capital_usd=1000.0, risk_pct=1.5)
        eng2.load_state_dict(json.loads(st_file.read_text(encoding="utf-8")))
        assert eng2.ENGINE_H1 in eng2.positions
        assert eng2.last_h1_closed_ts == eng.last_h1_closed_ts
    finally:
        st_file.unlink(missing_ok=True)


def test_m5_pending_pullback_fills_on_live_tick_and_stops_out() -> None:
    eng = ChampionsPaperEngine(spec=_gold_spec(), initial_capital_usd=1000.0, fixed_lot=0.01)
    eng.pending_m5_pullback = {
        "side": "buy",
        "limit_trigger_bid": 3510.0,
        "impulse_low": 3504.0,
        "impulse_high": 3514.0,
        "impulse_range": 10.0,
        "impulse_mid": 3509.0,
        "impulse_body": 8.0,
        "atr_m1": 5.0,
        "signal_bar_ts": 1764060000,
        "active_bar_ts": 1764060300,
        "expires_at": 1764060600.0,
    }
    # Above trigger -> no fill
    assert eng.on_tick(bid=3511.5, ask=3511.82, ts=1764060350.0) == []
    assert eng.ENGINE_M5 not in eng.positions

    # Touches trigger -> fills at live ask!
    evs_open = eng.on_tick(bid=3509.8, ask=3510.12, ts=1764060360.0)
    assert any(e["type"] == "OPEN" for e in evs_open)
    pos = eng.positions[eng.ENGINE_M5]
    assert pos["entry"] == 3510.12

    # Price drops below SL -> closes immediately on live tick
    evs_close = eng.on_tick(bid=pos["stop"] - 0.20, ask=pos["stop"] + 0.12, ts=1764060400.0)
    assert any(e["type"] == "CLOSE" for e in evs_close)
    assert eng.ENGINE_M5 not in eng.positions
    assert len(eng.closed_trades) == 1
    assert eng.closed_trades[0]["orders_submitted"] == 0


def test_champions_paper_live_cli_mock_smoke() -> None:
    st = ROOT / "logs" / "_pytest_live_paper_state.json"
    jn = ROOT / "logs" / "_pytest_live_paper_trades.jsonl"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/daytrade_champions_paper_live.py",
                "--mock",
                "--reset-state",
                "--capital", "1000",
                "--state-file", str(st),
                "--journal-file", str(jn),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ORDERS=0" in proc.stdout
        assert "[PAPER OPEN]" in proc.stdout
        assert "[PAPER TRAIL]" in proc.stdout
        assert "[PAPER CLOSE]" in proc.stdout
        assert st.exists() and (st.stat().st_mode & 0o777) == 0o600
        assert jn.exists() and (jn.stat().st_mode & 0o777) == 0o600
        trade_line = json.loads(jn.read_text(encoding="utf-8").strip().splitlines()[0])
        assert trade_line["net_pnl_usd"] > 0
        assert trade_line["orders_submitted"] == 0
    finally:
        st.unlink(missing_ok=True)
        jn.unlink(missing_ok=True)
