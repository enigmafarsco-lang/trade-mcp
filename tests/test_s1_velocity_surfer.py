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
    build_s1_profile_config,
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

    # Multi-file combined sweep
    rc2 = cli.main(["--replay-sweep", str(rec), str(rec)])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "COMBINED S1 CALIBRATION SWEEP (2 FILES," in out2


def test_news_c1_blocks_whipsaw_bounce_with_low_3s_velocity() -> None:
    """Reproduces live Trade #5: 1s down-up whipsaw (vel=0.6x, +0.23/3s) must NOT trigger NEWS_STEEP_C1."""
    t0 = 1764090000.0
    eng = S1VelocitySurferEngine(cfg=S1SurferConfig(leg_lot=0.01, max_legs=6))
    bull_ctx = {"bias": 1, "label": "BULL_RES_BREAK", "support": 3445.0, "resistance": 3455.0}
    bid = 3460.00
    spread = 0.40
    opens = 0
    for step in range(4 * 32):
        sec = step * 0.25
        if sec < 24.0:
            delta = 0.04 if (step % 4 < 2) else -0.04
        elif sec < 25.0:
            delta = -0.15  # 1s dip -$0.60
        elif sec < 26.0:
            delta = +0.18  # 1s whipsaw bounce +$0.72 -> net 3s move only +$0.12!
        else:
            delta = 0.0
        bid = round(bid + delta, 2)
        for ev in eng.on_tick(ts=t0 + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx):
            if ev["type"] == "OPEN_L1":
                opens += 1
    assert opens == 0


def test_sr_wall_guard_blocks_buying_right_under_m1_resistance() -> None:
    """Reproduces live Trades #1/#2: never BUY right inside M1 Resistance ceiling wall before clean break."""
    _, ticks = make_synthetic_s1_session(mode="normal_c3")
    # Flip synthetic bear ticks into a bull surge that runs straight into Resistance ceiling
    base_px = 3452.00
    bull_ticks: list[tuple[float, float, float]] = []
    for ts, bid, _ask in ticks:
        inv_bid = round(base_px + (ticks[0][1] - bid), 2)
        bull_ticks.append((ts, inv_bid, round(inv_bid + 0.39, 2)))

    # Place closed M1 Resistance just above the end of C3 so the whole C3 push runs right into the wall
    res_wall = round(bull_ticks[4 * 27 - 1][1] + 0.35, 2)
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3445.0, "resistance": res_wall}
    eng = S1VelocitySurferEngine(cfg=S1SurferConfig(leg_lot=0.01, max_legs=6, sr_wall_buffer_price=0.80))

    opens_at_wall = 0
    for ts, bid, ask in bull_ticks[: 4 * 27]:
        for ev in eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=bull_ctx):
            if ev["type"] == "OPEN_L1":
                opens_at_wall += 1

    assert opens_at_wall == 0
    assert eng.signals_blocked_m1 >= 1


def test_min_60s_move_gate_blocks_quiet_chop_and_allows_real_rip() -> None:
    """Ripping-tape gate (min_60s_move_price) keeps S1 quiet in calm drift and unlocks on real >=$4/min moves."""
    m1_bars, ticks = make_synthetic_s1_session(mode="normal_c3")
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])

    # Require $5/60s move: our normal_c3 only moves ~$1.50 before entry -> blocked as quiet!
    eng_quiet = S1VelocitySurferEngine(cfg=S1SurferConfig(leg_lot=0.01, max_legs=6, min_60s_move_price=5.0))
    for ts, bid, ask in ticks[: 4 * 27]:
        eng_quiet.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
    assert eng_quiet.basket is None
    assert eng_quiet.signals_blocked_quiet >= 1

    # Now prepend 35s of real downward trend (-$6.00 over 35s) before the burst -> unlocks immediately!
    t_first = ticks[0][0]
    lead_ticks: list[tuple[float, float, float]] = []
    start_bid = ticks[0][1] + 6.00
    for k in range(35 * 4):
        lt = t_first - (35.0 - k * 0.25)
        lb = round(start_bid - (6.00 * k / (35 * 4)), 2)
        lead_ticks.append((lt, lb, round(lb + 0.32, 2)))

    eng_rip = S1VelocitySurferEngine(cfg=S1SurferConfig(leg_lot=0.01, max_legs=6, min_60s_move_price=4.0))
    opens = 0
    for ts, bid, ask in lead_ticks + ticks:
        for ev in eng_rip.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx):
            if ev["type"] == "OPEN_L1":
                opens += 1
    assert opens >= 1
    assert eng_rip.waves_closed[0]["net_pnl_usd"] > 0


def test_v23_wave_rider_survives_stair_step_breaths_and_pyramids() -> None:
    """Reproduces live Nov 25 bull rally: Wave-Rider survives -$0.60 breaths without $0.20 SL choke-outs."""
    t0 = 1_764_090_000.0
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3440.0, "resistance": 3510.0}
    cfg = S1SurferConfig(
        leg_lot=0.01,
        max_legs=4,
        vel_mult=3.0,
        min_burst_price=1.20,
        min_60s_move_price=4.0,
        add_interval_sec=2.5,
        add_min_profit_price=1.00,
        min_stop_price=2.20,
        max_stop_price=3.50,
        disable_decel_exit=True,
        candle_cap_adds=False,
        sl_min_breathing_room=1.20,
        s1_reversal_cut_body=0.0,
        trail_arm_price=1.80,
        trail_min_pullback=1.40,
        trail_giveback_frac=0.35,
        max_hold_sec=180.0,
    )
    eng = S1VelocitySurferEngine(cfg=cfg)
    spread = 0.40
    bid = 3450.00
    # 35s quiet ramp (+$5.00) so 60s tape move >= $4.00 and baseline is calm,
    # then a C1-C2-C3 surge followed by a normal -$0.65 breath, then +$8 stair-step rally, then -$2.20 reversal!
    for step in range(4 * 75):
        sec = step * 0.25
        if sec < 35.0:
            bid = round(3450.00 + (5.00 * sec / 35.0), 2)
        elif sec < 38.0:
            # 3s burst +$2.40 -> triggers BUY entry
            bid = round(bid + 0.20, 2)
        elif sec < 40.0:
            # 2s normal stair-step breath (-$0.64) — v2.2 panicked here, v2.3 MUST hold!
            bid = round(bid - 0.08, 2)
        elif sec < 55.0:
            # 15s steady stair-step rally (+$9.00) -> pyramids winner legs
            bid = round(bid + 0.15, 2)
        else:
            # Real reversal (-$2.50+) -> triggers clean tick_trail_exit in solid profit
            bid = round(bid - 0.20, 2)
        prev_sl = eng.basket["stop"] if eng.basket is not None else None
        eng.on_tick(ts=t0 + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx)
        # Breathing room invariant: whenever SL ratchets upward, it must keep >= sl_min_breathing_room below live bid!
        if (
            eng.basket is not None
            and eng.basket["stop_locked"]
            and prev_sl is not None
            and eng.basket["stop"] > prev_sl
        ):
            assert eng.basket["stop"] <= bid - cfg.sl_min_breathing_room + 1e-6

    assert len(eng.waves_closed) >= 1
    w0 = eng.waves_closed[0]
    assert w0["side"] == "buy"
    assert w0["hold_seconds"] >= 10.0  # survived the 2s breath instead of exiting at 1.8s!
    assert w0["legs_count"] >= 2  # pyramided winner legs across the multi-second wave
    assert w0["exit_reason"] == "tick_trail_exit"
    assert w0["net_pnl_usd"] > 5.0


def test_v24_safe_pyramid_never_turns_winner_into_multi_leg_loser() -> None:
    """Risk-free pyramid gate + 1L BE arm + pullback cap + loss-only fast cut all hold mathematically."""
    t0 = 1_764_095_000.0
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3440.0, "resistance": 3530.0}
    cfg = S1SurferConfig(
        leg_lot=0.01,
        max_legs=4,
        vel_mult=3.0,
        min_burst_price=1.20,
        min_60s_move_price=4.0,
        add_interval_sec=2.0,
        add_min_profit_price=1.00,
        min_stop_price=1.80,
        max_stop_price=2.60,
        decel_loss_cut_sec=3.0,
        disable_decel_exit=True,
        decel_only_in_loss_1l=True,
        candle_cap_adds=False,
        sl_min_breathing_room=1.10,
        be_arm_price=1.40,
        require_be_on_add=True,
        s1_reversal_cut_body=0.0,
        trail_arm_price=1.80,
        trail_min_pullback=1.20,
        trail_max_pullback_price=2.80,
        trail_giveback_frac=0.32,
        multi_leg_giveback_frac=0.18,
        max_hold_sec=240.0,
    )
    # Case 1: +$1.60 pop followed by immediate -$2.00 reversal -> MUST stay 1L (no premature L2!) & exit >= $0 via BE!
    eng1 = S1VelocitySurferEngine(cfg=cfg)
    spread = 0.40
    bid = 3450.00
    for step in range(4 * 52):
        sec = step * 0.25
        if sec < 35.0:
            bid = round(3450.00 + (5.00 * sec / 35.0), 2)
        elif sec < 38.0:
            bid = round(bid + 0.16, 2)  # triggers L1 around ~3456.8
        elif sec < 41.0:
            bid = round(bid + 0.16, 2)  # pops +$1.80 above L1 -> arms 1L BE, NOT enough for risk-free L2 ($2.65)!
        else:
            bid = round(bid - 0.12, 2)  # reverses back down
        eng1.on_tick(ts=t0 + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx)
        if eng1.basket is not None and len(eng1.basket["legs"]) >= 2:
            # Invariant: ANY multi-leg basket MUST already have SL >= avg_entry + 0.04!
            assert eng1.basket["stop"] >= eng1.basket["avg_entry"] + 0.04
    assert len(eng1.waves_closed) >= 1
    assert eng1.waves_closed[0]["legs_count"] == 1
    assert eng1.waves_closed[0]["net_pnl_usd"] >= 0.0

    # Case 2: Big +$20 trend run followed by -$3.00 pullback -> pyramids risk-free AND caps giveback <= $2.80!
    eng2 = S1VelocitySurferEngine(cfg=cfg)
    bid = 3450.00
    for step in range(4 * 80):
        sec = step * 0.25
        if sec < 35.0:
            bid = round(3450.00 + (5.00 * sec / 35.0), 2)
        elif sec < 60.0:
            bid = round(bid + 0.22, 2)  # big +$22 wave
        else:
            bid = round(bid - 0.20, 2)  # sharp reversal -> capped pullback trail exits near top!
        eng2.on_tick(ts=t0 + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx)
        if eng2.basket is not None and len(eng2.basket["legs"]) >= 2:
            assert eng2.basket["stop"] >= eng2.basket["avg_entry"] + 0.04
    assert len(eng2.waves_closed) >= 1
    w2 = eng2.waves_closed[0]
    assert w2["legs_count"] >= 2
    assert w2["exit_reason"] == "tick_trail_exit"
    assert w2["net_pnl_usd"] > 20.0


def test_build_s1_profile_config_and_shallow_breath_survives_loss_cut() -> None:
    """Canonical --profile loader resolves v2.4_GOLD_* and loss_cut_min_loss_price ignores -$0.35 shallow breaths."""
    cfg = build_s1_profile_config("v2.4_GOLD_RIP4", leg_lot=0.01, max_legs=6)
    assert cfg.require_be_on_add is True
    assert cfg.loss_cut_min_loss_price == 0.85
    assert cfg.decel_loss_cut_sec == 6.0

    with pytest.raises(ValueError, match="Unknown S1 profile"):
        build_s1_profile_config("DOES_NOT_EXIST")

    # Shallow -$0.35 breath at hold=7s must NOT trigger stalled_loss_cut when loss_cut_min_loss_price=0.85!
    t0 = 1_764_099_000.0
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3440.0, "resistance": 3530.0}
    eng = S1VelocitySurferEngine(cfg=cfg)
    spread = 0.40
    bid = 3450.00
    for step in range(4 * 60):
        sec = step * 0.25
        if sec < 35.0:
            bid = round(3450.00 + (5.00 * sec / 35.0), 2)
        elif sec < 38.0:
            bid = round(bid + 0.18, 2)  # triggers BUY L1
        elif sec < 45.0:
            # Flat/shallow dip around -$0.30 net of entry for 7s -> MUST survive!
            bid = round(bid + (-0.01 if (step % 4 == 0) else 0.005), 2)
        elif sec < 54.0:
            bid = round(bid + 0.20, 2)  # wave launches!
        else:
            bid = round(bid - 0.25, 2)
        eng.on_tick(ts=t0 + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx)

    assert len(eng.waves_closed) >= 1
    assert eng.waves_closed[0]["exit_reason"] != "stalled_loss_cut"
    assert eng.waves_closed[0]["net_pnl_usd"] > 3.0


def test_live_report_snapshot_writes_atomic_git_safe_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Periodic --report-file helper validates reports/ path, rejects traversal, and writes atomic JSON."""
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    (tmp_path / "reports" / "paper_live").mkdir(parents=True)

    # Reject outside reports/ or logs/, and reject non-.json
    with pytest.raises(ValueError, match="inside reports/ or logs/"):
        cli._ensure_in_reports_or_logs(tmp_path / "outside.json")
    with pytest.raises(ValueError, match=r"end with \.json"):
        cli._ensure_in_reports_or_logs(tmp_path / "reports" / "paper_live" / "bad.txt")

    rep_path = cli._ensure_in_reports_or_logs(tmp_path / "reports" / "paper_live" / "s1_test.json")
    m1_bars, ticks = make_synthetic_s1_session(mode="normal_c3")
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    eng = S1VelocitySurferEngine(cfg=build_s1_profile_config("v2.1_DEFAULT", leg_lot=0.01, max_legs=6))
    for ts, bid, ask in ticks:
        eng.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)

    payload = cli._build_live_report_payload(
        eng=eng,
        symbol="XAUUSD_i",
        profile_label="v2.4_GOLD_RIP5.5",
        status="running",
        started_utc="2026-11-25T17:00:00+00:00",
        elapsed_minutes=12.5,
        m1_ctx=m1_ctx,
        last_bid=ticks[-1][1],
        last_ask=ticks[-1][2],
        ticks_processed=len(ticks),
        ticks_recorded=len(ticks),
        reconnect_count=0,
        consecutive_errors=0,
    )
    cli._write_atomic_json(rep_path, payload)
    loaded = json.loads(rep_path.read_text(encoding="utf-8"))
    assert loaded["schema"] == "s1_velocity_paper_live_v1"
    assert loaded["orders_submitted"] == 0
    assert loaded["profile"] == "v2.4_GOLD_RIP5.5"
    assert loaded["summary"]["waves_count"] >= 1
    assert len(loaded["hourly_summary"]) >= 1
    assert len(loaded["waves"]) == loaded["summary"]["waves_count"]


def test_live_tick_recorder_daily_path_and_replay_compat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """24/7 tick recorder produces replay-compatible JSONL under logs/ and sanitizes symbol names."""
    import record_ticks_live as rec

    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_PAPER_TRADING", "true")
    p = rec._daily_tick_path("XAUUSD_i", "20261126", logs_dir=tmp_path)
    assert p.parent == tmp_path.resolve()
    assert p.name == "s1_ticks_XAUUSD_i_20261126.jsonl"

    # Traversal attempt inside symbol name gets sanitized safely inside logs_dir
    p2 = rec._daily_tick_path("../../evil", "20261126", logs_dir=tmp_path)
    assert p2.parent == tmp_path.resolve()

    # Written lines parse cleanly in replay_recorded_ticks()
    m1_bars, ticks = make_synthetic_s1_session(mode="normal_c3")
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=ticks[0][0])
    fh = rec._open_private_append(p)
    try:
        for ts, bid, ask in ticks:
            fh.write(rec._format_tick_line(ts, bid, ask, m1_ctx))
    finally:
        fh.close()

    res = replay_recorded_ticks(p, cfg=build_s1_profile_config("v2.1_DEFAULT", leg_lot=0.01, max_legs=6))
    assert res["ticks_replayed"] == len(ticks)
    assert res["waves_count"] >= 1


def test_shock_gates_and_pullback_dip_audit_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Verify causal shock gates (no news C1, 60s efficiency, 15m breakout), PullbackDipEngine, and audit CLI."""
    import daytrade_s1_impulse_audit as audit_cli

    from trade_mcp.s1_impulse_audit import (
        PullbackDipConfig,
        PullbackDipEngine,
        analyze_tape_physics,
        load_tick_stream,
        run_structural_benchmark,
    )

    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_PAPER_TRADING", "true")

    # 1) enable_news_c1=False blocks NEWS_STEEP_C1 single-candle probes
    m1_bars, news_ticks = make_synthetic_s1_session(mode="news_steep_fakeout")
    m1_ctx = analyze_m1_sr_context(m1_bars, server_now_ts=news_ticks[0][0])
    eng_no_news = S1VelocitySurferEngine(cfg=S1SurferConfig(enable_news_c1=False))
    for ts, bid, ask in news_ticks:
        eng_no_news.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
    assert len(eng_no_news.waves_closed) == 0

    # 2) PullbackDipEngine buys a -$3.00 dip inside a +$10 5-min bull trend and rides continuation
    t0 = 1_764_100_000.0
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3440.0, "resistance": 3560.0}
    dip_ticks: list[tuple[float, float, float, dict[str, object]]] = []
    bid = 3480.00
    spread = 0.40
    for step in range(4 * 390):  # 390s = 6.5 min
        sec = step * 0.25
        if sec < 300.0:
            bid = round(3480.00 + (12.00 * sec / 300.0), 2)  # +$12 5m bull trend to 3492
        elif sec < 320.0:
            bid = round(3492.00 - (3.00 * (sec - 300.0) / 20.0), 2)  # -$3.00 pullback dip to 3489
        elif sec < 324.0:
            bid = round(3489.00 + (1.20 * (sec - 320.0) / 4.0), 2)  # +$1.20 S1 bounce -> triggers BUY!
        elif sec < 370.0:
            bid = round(3490.20 + (10.00 * (sec - 324.0) / 46.0), 2)  # +$10 continuation to 3500.20
        else:
            bid = round(3500.20 - (4.00 * (sec - 370.0) / 20.0), 2)  # -$4 reversal -> triggers trail exit!
        dip_ticks.append((t0 + sec, bid, round(bid + spread, 2), bull_ctx))

    deng = PullbackDipEngine(cfg=PullbackDipConfig(leg_lot=0.01, max_legs=4))
    for ts, b, a, ctx in dip_ticks:
        deng.on_tick(ts=ts, bid=b, ask=a, m1_context=ctx)
    assert len(deng.waves_closed) >= 1
    assert deng.waves_closed[0]["side"] == "buy"
    assert deng.waves_closed[0]["net_pnl_usd"] > 3.0

    # 3) Write JSONL and run tape physics + structural benchmark + CLI smoke
    jsonl = tmp_path / "dip_sample.jsonl"
    with jsonl.open("w", encoding="utf-8") as fh:
        for ts, b, a, ctx in dip_ticks:
            fh.write(json.dumps({"ts": ts, "bid": b, "ask": a, "m1": ctx}) + "\n")

    loaded = load_tick_stream(jsonl)
    loaded_rebuilt = load_tick_stream(jsonl, rebuild_m1=True)
    assert len(loaded_rebuilt) == len(dip_ticks)
    phys = analyze_tape_physics(loaded)
    assert phys["ticks"] == len(dip_ticks)
    assert len(phys["clean_waves"]) >= 1
    rows = run_structural_benchmark(loaded)
    assert len(rows) == 23
    names = {r["strategy"] for r in rows}
    assert {
        "SHOCK_10_RUNNER",
        "SHOCK10_DYN_RUN",
        "PB_DIP_V2_24H",
        "PB_DIP_V2_SESS",
        "PB_DIP_V2_SAFE",
        "PB_V2_DYN_SESS",
        "MACRO_WAVE_DYN",
        "SR_SWEEP_VSA_24H",
        "SR_SWEEP_VSA_SESS",
        "SR_BREAK_VSA_S10",
        "EMA_RECLAIM_S10",
        "CALM_M1_EMA_24H",
        "CALM_M3_SWING_24H",
        "CALM_M5_TREND_24H",
        "CALM_M5_RANGE_24H",
    }.issubset(names)
    rc = audit_cli.main([str(jsonl)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PART 1: TAPE PHYSICS" in out
    assert "PULLBACK_DIP_5M" in out
    assert "PB_DIP_V2_SESS" in out
    assert "PB_V2_DYN_SESS" in out
    assert "MACRO_WAVE_DYN" in out
    assert "SHOCK10_DYN_RUN" in out
    assert "CALM_M3_SWING_24H" in out
    assert "CALM_M5_TREND_24H" in out
    assert "CALM_M5_RANGE_24H" in out
    assert "COMBO_SHK10_PBV2" in out
    assert "COMBO_DYN_SUITE" in out
    assert "COMBO_DYN_MACRO" in out
    assert "COMBO_AW_M3" in out
    assert "COMBO_AW_M5" in out










