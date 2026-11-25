"""Hermetic offline tests for S1 Velocity Surfer (M1 S/R + 1s Acceleration + Winner Machine-Gun + Decel Exit)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daytrade_s1_velocity_paper as cli  # noqa: E402

from trade_mcp.s1_velocity_surfer import (  # noqa: E402
    S1SurferConfig,
    S1VelocitySurferEngine,
    analyze_m1_sr_context,
    make_synthetic_s1_session,
    replay_recorded_ticks,
)


def test_m1_sr_context_detects_bearish_pullback_resume() -> None:
    m1_bars, ticks = make_synthetic_s1_session()
    ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    assert ctx["bias"] == -1
    assert ctx["label"] in ("BEAR_PB_RESUME", "BEAR_SUP_BREAK", "BEAR_TREND")
    assert ctx["support"] is not None and ctx["resistance"] is not None
    assert ctx["resistance"] > ctx["support"]


def test_s1_machine_gun_fires_winner_legs_locks_stop_and_decel_exits() -> None:
    m1_bars, ticks = make_synthetic_s1_session()
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    eng = S1VelocitySurferEngine(cfg=S1SurferConfig(leg_lot=0.01, max_legs=6))

    events: list[dict[str, object]] = []
    for ts, bid, ask in ticks:
        events.extend(eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx))

    types = [e["type"] for e in events]
    assert "OPEN_L1" in types
    assert types.count("FIRE_ADD") >= 2
    assert "CLOSE_ALL" in types

    assert len(eng.waves_closed) >= 1
    w = eng.waves_closed[0]
    assert w["side"] == "sell"
    assert w["legs_count"] >= 3
    assert w["exit_reason"] in ("decel_stall_exit", "tick_trail_exit")
    assert w["net_pnl_usd"] > 0
    assert w["orders_submitted"] == 0


def test_s1_never_adds_on_adverse_reversal_hits_hard_sl() -> None:
    m1_bars, ticks = make_synthetic_s1_session()
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    eng = S1VelocitySurferEngine(cfg=S1SurferConfig(leg_lot=0.01, max_legs=6, max_stop_price=2.0))

    # Take baseline + first 4s of drop (triggers Leg 1), then violently reverse UP $6
    t_base = ticks[0][0]
    spread = 0.32
    bid = ticks[0][1]
    custom_ticks: list[tuple[float, float, float]] = []
    for step in range(4 * 40):
        sec = step * 0.25
        if sec < 24.0:
            delta = 0.04 if (step % 4 < 2) else -0.04
        elif sec < 25.75:
            delta = -0.18
        else:
            delta = +0.35  # violent immediate V-reversal against SELL right after Leg 1 fills!
        bid = round(bid + delta, 2)
        custom_ticks.append((t_base + sec, bid, round(bid + spread, 2)))

    adds = 0
    for ts, b, a in custom_ticks:
        for ev in eng.on_tick(ts=ts, bid=b, ask=a, m1_context=m1_ctx):
            if ev["type"] == "FIRE_ADD":
                adds += 1

    assert adds == 0
    assert len(eng.waves_closed) >= 1
    first = eng.waves_closed[0]
    assert first["legs_count"] == 1
    assert first["exit_reason"] == "hard_stop_loss"
    assert first["net_pnl_usd"] < 0


def test_s1_tick_record_and_offline_replay_match(tmp_path: Path) -> None:
    m1_bars, ticks = make_synthetic_s1_session()
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])

    rec_file = tmp_path / "ticks.jsonl"
    with rec_file.open("w", encoding="utf-8") as fh:
        for ts, bid, ask in ticks:
            fh.write(json.dumps({"ts": ts, "bid": bid, "ask": ask, "m1": m1_ctx}) + "\n")

    rep = replay_recorded_ticks(rec_file, cfg=S1SurferConfig(leg_lot=0.01, max_legs=6))
    assert rep["ticks_replayed"] == len(ticks)
    assert rep["waves_count"] >= 1
    assert rep["total_legs_executed"] >= 3
    assert rep["realized_pnl_usd"] > 0
    assert rep["orders_submitted"] == 0


def test_s1_cli_mock_and_replay_smoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_PAPER_TRADING", "true")

    rc = cli.main(["--mock"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK=True ORDERS=0 MODE=mock_s1" in out
    assert "[S1 OPEN L1]" in out
    assert "[S1 FIRE +L2]" in out
    assert "[S1 CLOSE ALL]" in out

    m1_bars, ticks = make_synthetic_s1_session()
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    rec = tmp_path / "sample_ticks.jsonl"
    with rec.open("w", encoding="utf-8") as fh:
        for ts, bid, ask in ticks:
            fh.write(json.dumps({"ts": ts, "bid": bid, "ask": ask, "m1": m1_ctx}) + "\n")

    rc2 = cli.main(["--replay", str(rec)])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "S1 VELOCITY REPLAY (ORDERS=0)" in out2
