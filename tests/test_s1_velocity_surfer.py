"""Hermetic offline tests for S1 Velocity Surfer v2 (M1 S/R + C1/C2/C3/C4 S1 Playbook + Winner Machine-Gun + Decel Exit)."""

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
    m1_bars, ticks = make_synthetic_s1_session(mode="normal_c3")
    ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    assert ctx["bias"] == -1
    assert ctx["label"] in ("BEAR_PB_RESUME", "BEAR_SUP_BREAK", "BEAR_TREND")
    assert ctx["support"] is not None and ctx["resistance"] is not None
    assert ctx["resistance"] > ctx["support"]


def test_normal_c3_waits_for_two_s1_candles_then_fires_c3_and_decel_exits() -> None:
    m1_bars, ticks = make_synthetic_s1_session(mode="normal_c3")
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    eng = S1VelocitySurferEngine(cfg=S1SurferConfig(leg_lot=0.01, max_legs=6))

    events: list[dict[str, object]] = []
    for ts, bid, ask in ticks:
        events.extend(eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx))

    opens = [e for e in events if e["type"] == "OPEN_L1"]
    adds = [e for e in events if e["type"] == "FIRE_ADD"]
    closes = [e for e in events if e["type"] == "CLOSE_ALL"]
    assert len(opens) >= 1
    assert opens[0]["regime"] == "NORMAL_C3"
    assert opens[0]["candle_num"] == 3
    assert len(adds) >= 2
    assert len(closes) >= 1

    w = eng.waves_closed[0]
    assert w["side"] == "sell"
    assert w["regime"] == "NORMAL_C3"
    assert w["legs_count"] >= 3
    assert w["exit_reason"] in ("decel_stall_exit", "tick_trail_exit", "profit_floor_hit")
    assert w["net_pnl_usd"] > 0
    assert w["orders_submitted"] == 0


def test_news_steep_c1_probes_confirms_c2_unlocks_c3_and_respects_exhaustion() -> None:
    m1_bars, ticks = make_synthetic_s1_session(mode="news_steep_confirm")
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    eng = S1VelocitySurferEngine(cfg=S1SurferConfig(leg_lot=0.01, max_legs=6))

    events: list[dict[str, object]] = []
    for ts, bid, ask in ticks:
        events.extend(eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx))

    opens = [e for e in events if e["type"] == "OPEN_L1"]
    confirms = [e for e in events if e["type"] == "C2_CONFIRMED"]
    adds = [e for e in events if e["type"] == "FIRE_ADD"]
    assert len(opens) >= 1
    assert opens[0]["regime"] == "NEWS_STEEP_C1"
    assert len(confirms) >= 1
    assert len(adds) >= 2
    assert eng.waves_closed[0]["net_pnl_usd"] > 0
    assert eng.waves_closed[0]["legs_count"] >= 3


def test_news_steep_c1_fakeout_cuts_immediately_on_c2_reversal() -> None:
    m1_bars, ticks = make_synthetic_s1_session(mode="news_steep_fakeout")
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    eng = S1VelocitySurferEngine(cfg=S1SurferConfig(leg_lot=0.01, max_legs=6))

    adds = 0
    for ts, bid, ask in ticks:
        for ev in eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx):
            if ev["type"] == "FIRE_ADD":
                adds += 1

    assert adds == 0
    assert len(eng.waves_closed) >= 1
    w = eng.waves_closed[0]
    assert w["regime"] == "NEWS_STEEP_C1"
    assert w["legs_count"] == 1
    assert w["exit_reason"] == "c2_unconfirmed_cut"


def test_normal_c3_never_averages_down_on_immediate_v_reversal() -> None:
    m1_bars, ticks = make_synthetic_s1_session(mode="normal_c3")
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    eng = S1VelocitySurferEngine(cfg=S1SurferConfig(leg_lot=0.01, max_legs=6))

    adds = 0
    for ts, bid, ask in ticks:
        if eng.basket is not None:
            # Immediately reverse straight up +$2.50 right after L1 opens!
            bid = float(eng.basket["avg_entry"]) + 2.50
            ask = bid + 0.32
        for ev in eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx):
            if ev["type"] == "FIRE_ADD":
                adds += 1
        if eng.waves_closed:
            break

    assert adds == 0
    assert len(eng.waves_closed) == 1
    assert eng.waves_closed[0]["legs_count"] == 1
    assert eng.waves_closed[0]["exit_reason"] == "hard_stop_loss"


def test_s1_tick_record_and_offline_replay_match(tmp_path: Path) -> None:
    m1_bars, ticks = make_synthetic_s1_session(mode="normal_c3")
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
    assert "[S1 OPEN L1" in out
    assert "[S1 FIRE +L2" in out
    assert "[S1 CLOSE ALL" in out

    m1_bars, ticks = make_synthetic_s1_session(mode="news_steep_confirm")
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    rec = tmp_path / "sample_ticks.jsonl"
    with rec.open("w", encoding="utf-8") as fh:
        for ts, bid, ask in ticks:
            fh.write(json.dumps({"ts": ts, "bid": bid, "ask": ask, "m1": m1_ctx}) + "\n")

    rc2 = cli.main(["--replay", str(rec)])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "S1 VELOCITY REPLAY (ORDERS=0)" in out2
