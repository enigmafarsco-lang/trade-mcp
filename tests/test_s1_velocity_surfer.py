"""Hermetic offline tests for S1 Velocity Surfer v2 (M1 S/R + C1/C2/C3/C4 S1 Playbook + Winner Machine-Gun + Decel Exit)."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daytrade_s1_velocity_paper as cli  # noqa: E402

from trade_mcp.bridge.native import to_epoch  # noqa: E402
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


def test_server_now_naive_broker_clock_matches_compact_bars_in_non_utc_timezone() -> None:
    """Regression: naive broker wall-clock times must NOT shift by host OS timezone (e.g. Asia/Tehran +03:30)."""
    class _StubFeed:
        def _call(self, tool: str, args: dict[str, object]) -> dict[str, object]:
            assert tool == "get_time_information"
            return {"trade_server_time": "2026-11-25 17:15:30"}

    old_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Asia/Tehran"
        if hasattr(time, "tzset"):
            time.tzset()
        srv_ts, srv_dt = cli._server_now(_StubFeed())  # type: ignore[arg-type]
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        if hasattr(time, "tzset"):
            time.tzset()

    # Build 40 M1 bars using to_epoch() on naive broker timestamps just like compact_bars() does
    bars: list[list[float]] = []
    px = 3500.0
    for i in range(40):
        t_dt = srv_dt.replace(second=0) - timedelta(minutes=(40 - i))
        ts = float(to_epoch(t_dt.strftime("%Y-%m-%d %H:%M:%S")))
        o = px
        c = px - 0.6
        bars.append([ts, o, o + 0.3, c - 0.3, c, 100.0])
        px = c

    ctx = analyze_m1_sr_context(bars, server_now_ts=srv_ts)
    assert ctx["label"] != "INSUFFICIENT_M1"
    assert ctx["bias"] == -1
    assert ctx["support"] is not None and ctx["resistance"] is not None


def test_live_m1_support_break_unlocks_sell_from_chop_neutral() -> None:
    """When closed M1 is CHOP_NEUTRAL, live breakdown below closed M1 support unlocks SELL immediately."""
    _, ticks = make_synthetic_s1_session(mode="normal_c3")
    chop_ctx = {
        "bias": 0,
        "label": "CHOP_NEUTRAL",
        "support": ticks[0][1] - 0.40,
        "resistance": ticks[0][1] + 5.00,
        "atr_m1": 2.0,
        "pullback_resume": False,
        "last_close": ticks[0][1],
    }
    eng = S1VelocitySurferEngine(cfg=S1SurferConfig(leg_lot=0.01, max_legs=6))
    opens: list[dict[str, object]] = []
    for ts, bid, ask in ticks:
        for ev in eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=chop_ctx):
            if ev["type"] == "OPEN_L1":
                opens.append(ev)

    assert len(opens) >= 1
    assert opens[0]["side"] == "sell"
    assert opens[0]["m1_label"] == "LIVE_M1_SUP_BREAK"
    assert eng.waves_closed[0]["net_pnl_usd"] > 0


def test_replay_rebuilds_causal_m1_when_recording_had_insufficient_m1(tmp_path: Path) -> None:
    """Reproduces the live recording with INSUFFICIENT_M1 labels and verifies causal M1 rebuild catches the crash."""
    t0 = 1764083600.0
    rec = tmp_path / "broken_m1_ticks.jsonl"
    bid = 3463.00
    spread = 0.39
    rows = 0
    with rec.open("w", encoding="utf-8") as fh:
        # 3 minutes of calm sideways range (3462..3465) @ 2 Hz, then sharp crash down!
        for step in range(2 * 220):  # 220 seconds
            sec = step * 0.5
            if sec < 180.0:
                delta = 0.08 if (step % 8 < 4) else -0.08
            elif sec < 182.0:
                delta = -0.25  # breaks below support + 2 bearish S1 candles
            elif sec < 192.0:
                delta = -0.35  # crash wave!
            else:
                delta = +0.03  # stalls
            bid = round(bid + delta, 2)
            fh.write(
                json.dumps(
                    {
                        "ts": round(t0 + sec, 3),
                        "bid": bid,
                        "ask": round(bid + spread, 2),
                        "m1": {"bias": 0, "label": "INSUFFICIENT_M1"},
                    }
                )
                + "\n"
            )
            rows += 1

    rep = replay_recorded_ticks(rec, cfg=S1SurferConfig(leg_lot=0.01, max_legs=6))
    assert rep["ticks_replayed"] == rows
    assert rep["rebuilt_m1_ticks"] == rows
    assert rep["waves_count"] >= 1
    assert rep["realized_pnl_usd"] > 0
    assert rep["orders_submitted"] == 0


def test_v21_fast_s1_reversal_cut_and_loss_cooldown() -> None:
    """v2.1: 1L trade that reverses on the next completed S1 candle cuts quickly (<=2.5s) & applies loss cooldown."""
    m1_bars, ticks = make_synthetic_s1_session(mode="normal_c3")
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    eng = S1VelocitySurferEngine(
        cfg=S1SurferConfig(leg_lot=0.01, max_legs=6, s1_reversal_cut_body=0.30, loss_cooldown_sec=20.0)
    )

    rev_step = 0
    for ts, bid, ask in ticks:
        if eng.basket is not None:
            rev_step += 1
            # Ramp smoothly up +$0.12/0.25s (a real +$0.48/s opposite green S1 candle, still inside $1.50 hard SL)
            bid = round(float(eng.basket["avg_entry"]) + 0.12 * rev_step, 2)
            ask = round(bid + 0.32, 2)
        eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        if eng.waves_closed:
            break

    assert len(eng.waves_closed) == 1
    w = eng.waves_closed[0]
    assert w["legs_count"] == 1
    assert w["exit_reason"] == "s1_reversal_cut"
    assert w["hold_seconds"] <= 2.5
    assert eng.consecutive_losses == 1
    assert eng.cooldown_until >= (w["closed_at"] + 19.5)


def test_replay_calibration_sweep_cli_smoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_PAPER_TRADING", "true")
    m1_bars, ticks = make_synthetic_s1_session(mode="news_steep_confirm")
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    rec = tmp_path / "sweep_ticks.jsonl"
    with rec.open("w", encoding="utf-8") as fh:
        for ts, bid, ask in ticks:
            fh.write(json.dumps({"ts": ts, "bid": bid, "ask": ask, "m1": m1_ctx}) + "\n")

    rc = cli.main(["--replay-sweep", str(rec)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "S1 VELOCITY CALIBRATION SWEEP (ORDERS=0)" in out
    assert "v2.0_LEGACY_5S" in out
    assert "v2.1_DEFAULT" in out
    assert "BEST PROFILE WAVES:" in out


