"""Hermetic offline tests for ShockRadar, v2.5_SHOCK_* profiles, Pullback-Dip V2, and Dual Radar Live CLI."""

from __future__ import annotations

import contextlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daytrade_s1_dual_radar_live as dual_cli  # noqa: E402

from trade_mcp.s1_dual_radar import (  # noqa: E402
    ShockRadar,
    ShockRadarConfig,
    make_synthetic_dual_session,
)
from trade_mcp.s1_impulse_audit import (  # noqa: E402
    ALPARI_LONDON_NY_HOURS,
    PullbackDipEngine,
    build_pullback_dip_profile_config,
    combine_portfolio_waves,
)
from trade_mcp.s1_velocity_surfer import (  # noqa: E402
    S1VelocitySurferEngine,
    build_s1_profile_config,
)


def test_v25_shock_profiles_resolve_and_sleep_through_quiet_drift() -> None:
    c8 = build_s1_profile_config("v2.5_SHOCK_8", leg_lot=0.01, max_legs=6)
    c10 = build_s1_profile_config("v2.5_SHOCK_10", leg_lot=0.01, max_legs=6)
    cr = build_s1_profile_config("v2.5_SHOCK_10_RUNNER", leg_lot=0.01, max_legs=6)

    assert c8.enable_news_c1 is False and c8.min_60s_move_price == 8.0 and c8.min_60s_efficiency == 0.45
    assert c10.enable_news_c1 is False and c10.min_60s_move_price == 10.0 and c10.min_60s_efficiency == 0.50
    assert cr.enable_news_c1 is False and cr.min_60s_move_price == 10.0 and cr.trail_min_pullback > c10.trail_min_pullback

    # Calm +$5/60s drift + 3s burst must keep v2.5_SHOCK_10 completely asleep!
    t0 = float(datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3440.0, "resistance": 3600.0}
    eng = S1VelocitySurferEngine(cfg=c10)
    bid = 3500.00
    for step in range(4 * 70):
        sec = step * 0.25
        if sec < 40.0:
            bid = round(3500.00 + (0.03 if step % 4 < 2 else -0.03), 2)
        elif sec < 62.0:
            bid = round(bid + 0.05, 2)  # only ~$4.40/22s -> well below $10/60s!
        else:
            bid = round(bid + 0.14, 2)
        eng.on_tick(ts=t0 + sec, bid=bid, ask=round(bid + 0.36, 2), m1_context=bull_ctx)
    assert eng.basket is None
    assert len(eng.waves_closed) == 0


def test_pullback_dip_v2_pivot_stop_cuts_loss_below_v1_fixed_stop() -> None:
    """V2 causal pivot SL cuts failed dips at ~$2.00-$3.00 instead of V1's -$4.50 fixed stop."""
    t0 = float(datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3450.0, "resistance": 3600.0}
    eng_v1 = PullbackDipEngine(cfg=build_pullback_dip_profile_config("PULLBACK_DIP_5M", leg_lot=0.01, max_legs=4))
    eng_v2 = PullbackDipEngine(cfg=build_pullback_dip_profile_config("PB_DIP_V2_24H", leg_lot=0.01, max_legs=4))

    spread = 0.36
    bid = 3480.00
    for step in range(4 * 365):
        sec = step * 0.25
        if sec < 300.0:
            bid = round(3480.00 + (12.00 * sec / 300.0), 2)  # +$12 smooth trend to 3492
        elif sec < 320.0:
            bid = round(3492.00 - (3.10 * (sec - 300.0) / 20.0), 2)  # -$3.10 dip to 3488.90
        elif sec < 324.0:
            bid = round(3488.90 + (1.30 * (sec - 320.0) / 4.0), 2)  # +$0.98/3s bounce (dip depth $2.12) -> triggers BUY
        else:
            # Immediate breakdown -$7.00 -> stops both V1 and V2
            bid = round(3490.20 - (7.00 * (sec - 324.0) / 41.0), 2)
        eng_v1.on_tick(ts=t0 + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx)
        eng_v2.on_tick(ts=t0 + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx)

    assert len(eng_v1.waves_closed) == 1
    assert len(eng_v2.waves_closed) == 1
    loss_v1 = eng_v1.waves_closed[0]["net_pnl_usd"]
    loss_v2 = eng_v2.waves_closed[0]["net_pnl_usd"]
    assert loss_v1 <= -4.40
    assert -3.15 <= loss_v2 < 0.0
    assert abs(loss_v2) < abs(loss_v1) - 1.20


def test_pullback_dip_v2_efficiency_gate_blocks_whipsaw_trend() -> None:
    """Wild sawtooth swings with net +$8 but low 15s efficiency (<0.30) must be rejected by V2."""
    t0 = float(datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3450.0, "resistance": 3600.0}
    eng_v2 = PullbackDipEngine(cfg=build_pullback_dip_profile_config("PB_DIP_V2_24H", leg_lot=0.01, max_legs=4))

    spread = 0.36
    # Build violent sawtooth path (+12, -10, +12, -10, +12, -6 -> net +10, path ~62 -> eff ~ 0.16!)
    waypoints = [
        (0.0, 3480.0),
        (50.0, 3492.0),
        (100.0, 3482.0),
        (150.0, 3494.0),
        (200.0, 3484.0),
        (250.0, 3496.0),
        (285.0, 3488.0),
        (300.0, 3492.5),  # net +12.50 over 300s, huge path!
        (320.0, 3489.5),  # -$3.00 pullback
        (324.0, 3490.8),  # +$1.30 bounce -> would trigger V1, blocked by V2 efficiency gate!
    ]

    def _interp(sec: float) -> float:
        for i in range(len(waypoints) - 1):
            s0, p0 = waypoints[i]
            s1, p1 = waypoints[i + 1]
            if s0 <= sec <= s1:
                frac = (sec - s0) / max(1e-9, s1 - s0)
                return round(p0 + frac * (p1 - p0), 2)
        return waypoints[-1][1]

    for step in range(4 * 326):
        sec = step * 0.25
        bid = _interp(sec)
        eng_v2.on_tick(ts=t0 + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx)

    assert eng_v2.basket is None
    assert len(eng_v2.waves_closed) == 0
    assert eng_v2.signals_blocked_efficiency >= 1


def test_pullback_dip_v2_profit_lock_preserves_gain_above_be_scratch() -> None:
    """When V2 reaches +$3.90 and reverses without hitting trail, profit-lock floor preserves >= +$1.20."""
    t0 = float(datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3450.0, "resistance": 3600.0}
    eng = PullbackDipEngine(cfg=build_pullback_dip_profile_config("PB_DIP_V2_24H", leg_lot=0.01, max_legs=1))
    spread = 0.36
    for step in range(4 * 370):
        sec = step * 0.25
        if sec < 300.0:
            bid = round(3480.00 + (12.00 * sec / 300.0), 2)
        elif sec < 320.0:
            bid = round(3492.00 - (3.00 * (sec - 300.0) / 20.0), 2)
        elif sec < 324.0:
            bid = round(3489.00 + (1.20 * (sec - 320.0) / 4.0), 2)  # fills ~3490.56
        elif sec < 340.0:
            # Rally +$4.00 above entry to ~3494.50 (clears profit_lock_arm=3.80, below trail_arm=4.20)
            bid = round(3490.20 + (4.25 * (sec - 324.0) / 16.0), 2)
        else:
            # Reverse back to entry -> must hit locked profit floor (~+$1.40), NOT +$0.08 BE!
            bid = round(3494.45 - (5.00 * (sec - 340.0) / 30.0), 2)
        eng.on_tick(ts=t0 + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx)

    assert len(eng.waves_closed) == 1
    w = eng.waves_closed[0]
    assert w["exit_reason"] == "profit_floor_hit"
    assert w["net_pnl_usd"] >= 1.20


def test_pullback_dip_v2_session_filter_manages_open_trade_past_close_and_blocks_asia() -> None:
    """Trade opened at 19:58 server time still exits cleanly after 20:00; new signal at 03:00 is blocked."""
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3450.0, "resistance": 3600.0}
    eng = PullbackDipEngine(cfg=build_pullback_dip_profile_config("PB_DIP_V2_SESS", leg_lot=0.01, max_legs=4))
    assert eng.cfg.active_server_hours == ALPARI_LONDON_NY_HOURS

    # Start at 19:53:00 -> 5m trend finishes at 19:58:00 -> dip & entry at ~19:58:24 -> exits after 20:00:00!
    t_late = float(datetime(2026, 11, 26, 19, 53, 0, tzinfo=timezone.utc).timestamp())
    spread = 0.36
    for step in range(4 * 460):  # runs to 20:00:40 server time
        sec = step * 0.25
        if sec < 300.0:
            bid = round(3480.00 + (12.00 * sec / 300.0), 2)
        elif sec < 320.0:
            bid = round(3492.00 - (3.00 * (sec - 300.0) / 20.0), 2)
        elif sec < 324.0:
            bid = round(3489.00 + (1.25 * (sec - 320.0) / 4.0), 2)
        elif sec < 430.0:
            bid = round(3490.25 + (9.00 * (sec - 324.0) / 106.0), 2)  # crosses 20:00 while in trade!
        else:
            bid = round(3499.25 - (4.00 * (sec - 430.0) / 30.0), 2)
        eng.on_tick(ts=t_late + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx)

    assert len(eng.waves_closed) == 1
    assert eng.waves_closed[0]["closed_utc"].startswith("2026-11-26T20:")
    assert eng.waves_closed[0]["net_pnl_usd"] > 3.0

    # Now jump to 03:00 server time (Asia off-session): same pattern MUST be blocked!
    t_asia = float(datetime(2026, 11, 27, 3, 0, 0, tzinfo=timezone.utc).timestamp())
    for step in range(4 * 340):
        sec = step * 0.25
        if sec < 300.0:
            bid = round(3510.00 + (12.00 * sec / 300.0), 2)
        elif sec < 320.0:
            bid = round(3522.00 - (3.00 * (sec - 300.0) / 20.0), 2)
        else:
            bid = round(3519.00 + (1.30 * (sec - 320.0) / 4.0), 2)
        eng.on_tick(ts=t_asia + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx)

    assert len(eng.waves_closed) == 1  # zero new trades in Asia!
    assert eng.signals_blocked_session >= 1


def test_pullback_dip_v2_consecutive_loss_cooldown() -> None:
    """Two back-to-back stop-outs engage 180s consec_loss_cooldown_sec."""
    t0 = float(datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3450.0, "resistance": 3600.0}
    eng = PullbackDipEngine(cfg=build_pullback_dip_profile_config("PB_DIP_V2_24H", leg_lot=0.01, max_legs=4))
    spread = 0.36

    # Run two separate failed-dip waves separated by 90s gap-free trend rebuild
    for wave_idx in range(2):
        base_t = t0 + wave_idx * 450.0
        base_px = 3480.0 + wave_idx * 20.0
        for step in range(4 * 365):
            sec = step * 0.25
            if sec < 300.0:
                bid = round(base_px + (12.00 * sec / 300.0), 2)
            elif sec < 320.0:
                bid = round(base_px + 12.00 - (3.10 * (sec - 300.0) / 20.0), 2)
            elif sec < 324.0:
                bid = round(base_px + 8.90 + (1.30 * (sec - 320.0) / 4.0), 2)
            else:
                bid = round(base_px + 10.20 - (6.00 * (sec - 324.0) / 41.0), 2)
            eng.on_tick(ts=base_t + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx)

    assert len(eng.waves_closed) == 2
    assert eng.consecutive_losses == 2
    assert eng.cooldown_until >= (eng.waves_closed[-1]["closed_at"] + 179.0)


def test_shock_radar_escalates_cleanly_and_ignores_reconnect_gaps() -> None:
    """ShockRadar sleeps in chop, ignores reconnect/spread spikes, escalates cleanly, and catches reversals."""
    radar = ShockRadar(cfg=ShockRadarConfig(alert_cooldown_sec=60.0))
    t0 = float(datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    ctx = {"bias": 1, "label": "BULL_TREND", "support": 3480.0, "resistance": 3600.0}
    alerts: list[dict[str, Any]] = []

    # 1) 40s calm chop -> 0 alerts, state == SLEEP
    bid = 3500.00
    for step in range(4 * 40):
        sec = step * 0.25
        bid = round(3500.00 + (0.05 if step % 4 < 2 else -0.05), 2)
        al = radar.on_tick(ts=t0 + sec, bid=bid, ask=round(bid + 0.36, 2), m1_context=ctx)
        if al:
            alerts.append(al)
    assert alerts == []
    assert radar.state == "SLEEP"

    # 2) Simulate 25-second MT5 disconnect + $15 price jump -> MUST NOT false-alert!
    t_gap = t0 + 65.0
    for k in range(8):
        al = radar.on_tick(ts=t_gap + k * 0.25, bid=3515.0 + k * 0.02, ask=3515.36 + k * 0.02, m1_context=ctx)
        if al:
            alerts.append(al)
    assert alerts == []

    # 3) Wide rollover spread ($2.00) during a fast move -> MUST NOT alert!
    t_wide = t_gap + 5.0
    bid = 3515.0
    for step in range(4 * 35):
        sec = step * 0.25
        bid = round(bid + 0.09, 2)
        al = radar.on_tick(ts=t_wide + sec, bid=bid, ask=round(bid + 2.00, 2), m1_context=ctx)
        if al:
            alerts.append(al)
    assert alerts == []

    # 4) Clean straight-line acceleration after gap reset -> escalates PRE_SHOCK -> SHOCK_8 -> SHOCK_10 (3 alerts, zero spam!)
    t_clean = t_wide + 60.0
    bid = 3520.0
    for step in range(4 * 50):
        sec = step * 0.25
        bid = round(bid + 0.075, 2)  # +$0.30/s -> +$15 in 50s, eff ~ 1.0
        al = radar.on_tick(ts=t_clean + sec, bid=bid, ask=round(bid + 0.36, 2), m1_context=ctx)
        if al:
            alerts.append(al)

    tiers = [a["tier"] for a in alerts]
    assert tiers == ["PRE_SHOCK", "SHOCK_8", "SHOCK_10"]
    assert all(a["side"] == "BUY" for a in alerts)
    assert alerts[1]["escalated"] is True
    assert alerts[2]["escalated"] is True

    # 5) Sharp opposite-direction shock (-$13/35s) -> fires reversal SELL alert immediately!
    t_rev = t_clean + 50.0
    rev_alerts: list[dict[str, Any]] = []
    for step in range(4 * 45):
        sec = step * 0.25
        bid = round(bid - 0.14, 2)
        al = radar.on_tick(ts=t_rev + sec, bid=bid, ask=round(bid + 0.36, 2), m1_context=ctx)
        if al:
            rev_alerts.append(al)
    assert any(a["side"] == "SELL" and a["tier"] in ("SHOCK_8", "SHOCK_10") for a in rev_alerts)


def test_dual_radar_cli_mock_replay_and_live_wiring(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """End-to-end verification of --mock, --replay, path guard, and stubbed live native MT5 wiring (ORDERS=0)."""
    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_PAPER_TRADING", "true")
    monkeypatch.setattr(dual_cli, "ROOT", tmp_path)
    (tmp_path / "reports" / "paper_live").mkdir(parents=True)
    (tmp_path / "logs").mkdir(parents=True)

    # Path guard rejects outside reports/ or logs/
    with pytest.raises(ValueError, match="inside reports/ or logs/"):
        dual_cli._ensure_in_reports_or_logs(tmp_path / "escape.json")

    # 1) --mock with --report-file
    mock_rep = tmp_path / "reports" / "paper_live" / "dual_mock.json"
    rc = dual_cli.main(["--mock", "--no-bell", "--report-file", str(mock_rep)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK=True ORDERS=0 MODE=mock_dual_radar" in out
    assert "PB_SESS_BLOCKED_OFFSESS=" in out
    loaded = json.loads(mock_rep.read_text(encoding="utf-8"))
    assert loaded["schema"] == "s1_dual_radar_live_v1"
    assert loaded["orders_submitted"] == 0
    assert loaded["radar"]["total_alerts"] >= 3
    assert loaded["engines"]["v2.5_SHOCK_10"]["summary"]["realized_pnl_usd"] > 0.0
    assert loaded["engines"]["PB_DIP_V2_SESS"]["summary"]["realized_pnl_usd"] > 0.0
    assert (
        loaded["engines"]["PB_DIP_V2_24H"]["summary"]["waves_count"]
        > loaded["engines"]["PB_DIP_V2_SESS"]["summary"]["waves_count"]
    )

    # 2) --replay on recorded JSONL
    ticks = make_synthetic_dual_session()
    rec_path = tmp_path / "logs" / "dual_sample.jsonl"
    with rec_path.open("w", encoding="utf-8") as fh:
        for ts, b, a, ctx in ticks:
            fh.write(json.dumps({"ts": ts, "bid": b, "ask": a, "m1": ctx}) + "\n")
    rc2 = dual_cli.main(["--replay", str(rec_path), "--no-bell"])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "DUAL RADAR + 5-ENGINE REPLAY" in out2
    assert "COMBO(RUN10+PB_SESS)" in out2

    # 3) Live wiring smoke with stubbed ReadOnlyNativeFeed (0 broker orders, atomic report write)
    class _StubCfg:
        backend = "native"
        mcp_url = "http://127.0.0.1:22346/mcp"
        mcp_token = "stub"

    class _StubFeed:
        def __init__(self, url: str, token: str, symbol: str, timeout: float = 15.0) -> None:
            self.symbol = symbol
            self._n = 0

        def connect(self) -> tuple[dict[str, Any], dict[str, float]]:
            return (
                {
                    "contract_size": 100.0,
                    "point": 0.01,
                    "trade_tick_size": 0.01,
                    "trade_tick_value": 1.0,
                    "currency_profit": "USD",
                },
                {"bid": 3500.00, "ask": 3500.36},
            )

        def fetch_quote(self) -> dict[str, float]:
            self._n += 1
            return {"bid": round(3500.00 + 0.05 * self._n, 2), "ask": round(3500.36 + 0.05 * self._n, 2)}

    monkeypatch.setattr(dual_cli, "load_dotenv", lambda _p: None)
    monkeypatch.setattr(dual_cli.Config, "from_env", classmethod(lambda cls: _StubCfg()))
    monkeypatch.setattr(dual_cli, "loopback_native", lambda: contextlib.nullcontext())
    monkeypatch.setattr(dual_cli, "ReadOnlyNativeFeed", _StubFeed)
    monkeypatch.setattr(
        dual_cli,
        "_server_now",
        lambda _feed: (
            float(datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc).timestamp()),
            datetime(2026, 11, 26, 14, 0, 0),
        ),
    )
    monkeypatch.setattr(dual_cli, "_fetch_m1_bars", lambda _feed, now_label, limit=200: [])

    live_rep = tmp_path / "reports" / "paper_live" / "dual_live_smoke.json"
    rc3 = dual_cli.main(
        [
            "--symbol",
            "XAUUSD_i",
            "--poll-seconds",
            "0.05",
            "--status-seconds",
            "0.1",
            "--minutes",
            "0.004",
            "--no-bell",
            "--report-file",
            str(live_rep),
            "--report-seconds",
            "10",
        ]
    )
    assert rc3 == 0
    live_data = json.loads(live_rep.read_text(encoding="utf-8"))
    assert live_data["status"] == "stopped"
    assert live_data["orders_submitted"] == 0
    assert live_data["live_state"]["ticks_processed"] >= 1


def test_combine_portfolio_waves_chronological_dd() -> None:
    w1 = [{"closed_at": 100.0, "closed_utc": "2026-11-26T14:01:40Z", "legs_count": 1, "net_pnl_usd": 5.0}]
    w2 = [
        {"closed_at": 120.0, "closed_utc": "2026-11-26T14:02:00Z", "legs_count": 1, "net_pnl_usd": -2.5},
        {"closed_at": 150.0, "closed_utc": "2026-11-26T14:02:30Z", "legs_count": 2, "net_pnl_usd": 8.0},
    ]
    res = combine_portfolio_waves("COMBO", [w1, w2])
    assert res["waves_count"] == 3
    assert res["total_legs"] == 4
    assert res["net_usd"] == 10.5
    assert res["max_dd_usd"] == 2.5
    assert res["pf"] == 5.2


def _emit_s10_bar(
    eng: Any,
    bar_start_ts: float,
    mids: list[float],
    *,
    spread: float = 0.36,
    m1_ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    """Feed a sequence of mid-price ticks evenly spaced inside one 10-second bucket."""
    events: list[dict[str, Any]] = []
    n = len(mids)
    step = 9.0 / max(1, n)
    half = spread * 0.5
    for i, m in enumerate(mids):
        bid = round(m - half, 2)
        ask = round(m + half, 2)
        events.extend(eng.on_tick(ts=bar_start_ts + i * step, bid=bid, ask=ask, m1_context=m1_ctx))
    return events


def test_s10_sr_sweep_vsa_trades_stop_hunt_respects_session_and_blocks_vertical_trend() -> None:
    """SR_SWEEP_VSA_24H catches liquidity sweep + VSA tail to TP, respects session filter, and rejects vertical trends."""
    from trade_mcp.s10_hybrid_engines import S10HybridEngine, build_s10_hybrid_profile_config

    eng_24h = S10HybridEngine(cfg=build_s10_hybrid_profile_config("SR_SWEEP_VSA_24H", leg_lot=0.01))
    eng_sess = S10HybridEngine(cfg=build_s10_hybrid_profile_config("SR_SWEEP_VSA_SESS", leg_lot=0.01))
    rng_ctx = {"bias": 0, "label": "RANGE_NEUTRAL", "support": 3498.0, "resistance": 3502.5}

    # 1) Active London/NY hour (14:00 server): 40 realistic quiet 10s bars slowly oscillating inside [3498.0, 3502.5]
    # (~$0.50 effort/bar vs $2.70 effort on the liquidity sweep spike!)
    t0 = float(datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    box_cycle = [
        3500.0, 3499.5, 3499.0, 3498.5, 3498.0, 3498.5, 3499.0, 3499.5, 3500.0, 3500.5,
        3501.0, 3501.5, 3502.0, 3502.5, 3502.0, 3501.5, 3501.0, 3500.5, 3500.0, 3499.5,
    ]
    for i in range(40):
        c0 = box_cycle[i % len(box_cycle)]
        c1 = box_cycle[(i + 1) % len(box_cycle)]
        mids = [c0, round(0.5 * (c0 + c1), 2), c1]
        _emit_s10_bar(eng_24h, t0 + i * 10.0, mids, m1_ctx=rng_ctx)
        _emit_s10_bar(eng_sess, t0 + i * 10.0, mids, m1_ctx=rng_ctx)

    # Bar 40: bullish stop-hunt wick below 3498.0 -> pokes to 3497.2 (-$0.80), surges effort ($2.70 vs ~$0.50 MA), closes 3498.6!
    sweep_mids = [3498.5, 3498.2, 3497.9, 3497.6, 3497.2, 3497.4, 3497.7, 3498.0, 3498.2, 3498.4, 3498.5, 3498.6]
    _emit_s10_bar(eng_24h, t0 + 400.0, sweep_mids, m1_ctx=rng_ctx)
    _emit_s10_bar(eng_sess, t0 + 400.0, sweep_mids, m1_ctx=rng_ctx)

    # Bar 41..43: close bar 40 -> fills BUY after 0.25s latency -> price reverts upward +$4.00 to hit take_profit_hit!
    rebound = [3498.6, 3498.7, 3499.2, 3499.9, 3500.6, 3501.4, 3502.2, 3503.0]
    _emit_s10_bar(eng_24h, t0 + 410.0, rebound, m1_ctx=rng_ctx)
    _emit_s10_bar(eng_sess, t0 + 410.0, rebound, m1_ctx=rng_ctx)

    assert len(eng_24h.waves_closed) == 1
    assert len(eng_sess.waves_closed) == 1
    assert eng_24h.waves_closed[0]["side"] == "buy"
    assert eng_24h.waves_closed[0]["exit_reason"] == "take_profit_hit"
    assert eng_24h.waves_closed[0]["net_pnl_usd"] >= 2.0

    # 2) Jump to 03:00 Asia off-session (gap reset): SR_SWEEP_VSA_SESS must block while SR_SWEEP_VSA_24H trades!
    t_asia = float(datetime(2026, 11, 27, 3, 0, 0, tzinfo=timezone.utc).timestamp())
    for i in range(40):
        c0 = box_cycle[i % len(box_cycle)]
        c1 = box_cycle[(i + 1) % len(box_cycle)]
        mids = [c0, round(0.5 * (c0 + c1), 2), c1]
        _emit_s10_bar(eng_24h, t_asia + i * 10.0, mids, m1_ctx=rng_ctx)
        _emit_s10_bar(eng_sess, t_asia + i * 10.0, mids, m1_ctx=rng_ctx)
    _emit_s10_bar(eng_24h, t_asia + 400.0, sweep_mids, m1_ctx=rng_ctx)
    _emit_s10_bar(eng_sess, t_asia + 400.0, sweep_mids, m1_ctx=rng_ctx)
    _emit_s10_bar(eng_24h, t_asia + 410.0, rebound, m1_ctx=rng_ctx)
    _emit_s10_bar(eng_sess, t_asia + 410.0, rebound, m1_ctx=rng_ctx)

    assert len(eng_24h.waves_closed) == 2
    assert len(eng_sess.waves_closed) == 1
    assert eng_sess.signals_blocked_session >= 1

    # 3) Runaway vertical downtrend (eff_5m ~ 1.0 > 0.58) -> sweep fade MUST be blocked by regime guard!
    eng_trend = S10HybridEngine(cfg=build_s10_hybrid_profile_config("SR_SWEEP_VSA_24H", leg_lot=0.01))
    t_tr = float(datetime(2026, 11, 27, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    px = 3520.0
    for i in range(40):
        mids = [round(px - 0.10 * k, 2) for k in range(5)]
        px = mids[-1]
        _emit_s10_bar(eng_trend, t_tr + i * 10.0, mids, m1_ctx=rng_ctx)
    # Hammer tail at bottom of runaway crash
    crash_hammer = [px, px - 0.3, px - 0.7, px - 1.1, px - 1.4, px - 0.9, px - 0.4, px + 0.15, px + 0.25]
    _emit_s10_bar(eng_trend, t_tr + 400.0, crash_hammer, m1_ctx=rng_ctx)
    _emit_s10_bar(eng_trend, t_tr + 410.0, [px + 0.3, px + 0.4, px + 0.5], m1_ctx=rng_ctx)
    assert eng_trend.basket is None
    assert len(eng_trend.waves_closed) == 0
    assert eng_trend.signals_blocked_regime >= 1


def test_s10_sr_break_vsa_captures_two_candle_breakout_and_resets_on_gap() -> None:
    """SR_BREAK_VSA_S10 catches 2 clean accelerating VSA candles breaking 15m Resistance and rides runner."""
    from trade_mcp.s10_hybrid_engines import S10HybridEngine, build_s10_hybrid_profile_config

    eng = S10HybridEngine(cfg=build_s10_hybrid_profile_config("SR_BREAK_VSA_S10", leg_lot=0.01, max_legs=3))
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3498.0, "resistance": 3502.0}
    t0 = float(datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc).timestamp())

    # 38 calm consolidation bars strictly inside [3498.5, 3502.0] (~$0.50 effort/bar)
    box_seq = [
        3500.0, 3499.5, 3499.0, 3498.5, 3499.0, 3499.5, 3500.0, 3500.5,
        3501.0, 3501.5, 3502.0, 3501.5, 3501.0, 3500.5,
    ]
    for i in range(38):
        c0 = box_seq[i % len(box_seq)]
        c1 = box_seq[(i + 1) % len(box_seq)]
        mids = [c0, round(0.5 * (c0 + c1), 2), c1]
        _emit_s10_bar(eng, t0 + i * 10.0, mids, m1_ctx=bull_ctx)

    # Bar 38 (B1) + Bar 39 (B2): 2 clean accelerating bullish VSA candles clearing 3502.0 Resistance
    b1 = [3501.2, 3501.4, 3501.6, 3501.8, 3502.0, 3502.15]  # body +0.95, clean
    b2 = [3502.15, 3502.3, 3502.5, 3502.7, 3502.9, 3503.1, 3503.3, 3503.5]  # body +1.35, effort ~$1.35 (~2.7x MA!)
    _emit_s10_bar(eng, t0 + 380.0, b1, m1_ctx=bull_ctx)
    _emit_s10_bar(eng, t0 + 390.0, b2, m1_ctx=bull_ctx)

    # Bar 40..42: open triggers on bar 39 close + 0.25s latency -> +$8.00 rally -> -$2.60 pullback triggers profit exit
    _emit_s10_bar(eng, t0 + 400.0, [3503.5, 3503.7, 3504.5, 3505.5, 3506.5], m1_ctx=bull_ctx)
    _emit_s10_bar(eng, t0 + 410.0, [3507.2, 3508.4, 3509.6, 3510.8, 3511.6], m1_ctx=bull_ctx)
    _emit_s10_bar(eng, t0 + 420.0, [3510.8, 3509.8, 3509.0, 3508.4], m1_ctx=bull_ctx)

    assert len(eng.waves_closed) == 1
    w = eng.waves_closed[0]
    assert w["side"] == "buy"
    assert w["exit_reason"] in ("tick_trail_exit", "profit_floor_hit")
    assert w["legs_count"] >= 2
    assert w["net_pnl_usd"] > 3.0

    # Verify >60s disconnect gap resets internal S10 history cleanly
    eng.on_tick(ts=t0 + 600.0, bid=3520.0, ask=3520.36, m1_context=bull_ctx)
    assert len(eng._s10_bars) == 0


def test_s10_ema_reclaim_catches_medium_trend_pullback_and_does_not_chase() -> None:
    """EMA_RECLAIM_S10 buys a pullback-to-EMA18 reclaim inside a medium +$5.50/5m bull trend, without chasing."""
    from trade_mcp.s10_hybrid_engines import S10HybridEngine, build_s10_hybrid_profile_config

    eng = S10HybridEngine(cfg=build_s10_hybrid_profile_config("EMA_RECLAIM_S10", leg_lot=0.01, max_legs=3))
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3480.0, "resistance": 3560.0}
    t0 = float(datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc).timestamp())

    # 1) 56 steady upward 10s bars: +$0.20/bar -> +$6.00/5m (below Pullback-Dip's $8 gate, above EMA_RECLAIM's $4!)
    # Because price rises steadily far above EMA18 without pulling back, engine MUST NOT chase during warmup!
    px = 3490.0
    for i in range(56):
        mids = [round(px, 2), round(px + 0.07, 2), round(px + 0.14, 2), round(px + 0.20, 2)]
        px = mids[-1]
        _emit_s10_bar(eng, t0 + i * 10.0, mids, m1_ctx=bull_ctx)

    assert eng.basket is None
    assert len(eng.waves_closed) == 0  # anti-chase check passed!

    # 2) Pullback 2 bars down to tap causal EMA18 (ef)
    ef_now = float(eng._ema_fast or px)
    _emit_s10_bar(eng, t0 + 560.0, [px, px - 0.45, px - 0.90, round(ef_now + 0.15, 2)], m1_ctx=bull_ctx)
    _emit_s10_bar(
        eng,
        t0 + 570.0,
        [round(ef_now + 0.15, 2), round(ef_now - 0.10, 2), round(ef_now - 0.20, 2), round(ef_now - 0.05, 2)],
        m1_ctx=bull_ctx,
    )

    # 3) Clean bullish VSA reclaim bar closing firmly back above EMA18 with higher tick activity
    ef_pb = float(eng._ema_fast or ef_now)
    reclaim_mids = [
        round(ef_pb - 0.10, 2),
        round(ef_pb + 0.05, 2),
        round(ef_pb + 0.25, 2),
        round(ef_pb + 0.45, 2),
        round(ef_pb + 0.70, 2),
        round(ef_pb + 0.95, 2),
        round(ef_pb + 1.15, 2),
    ]
    _emit_s10_bar(eng, t0 + 580.0, reclaim_mids, m1_ctx=bull_ctx)

    # 4) Continuation +$7.00 then -$2.50 pullback -> pyramids and exits profitably on trail!
    top = reclaim_mids[-1]
    _emit_s10_bar(eng, t0 + 590.0, [top, top + 0.4, top + 1.4, top + 2.6, top + 3.8], m1_ctx=bull_ctx)
    _emit_s10_bar(eng, t0 + 600.0, [top + 4.4, top + 5.4, top + 6.4, top + 7.2], m1_ctx=bull_ctx)
    _emit_s10_bar(eng, t0 + 610.0, [top + 6.4, top + 5.4, top + 4.6, top + 4.2], m1_ctx=bull_ctx)

    assert len(eng.waves_closed) == 1
    w = eng.waves_closed[0]
    assert w["side"] == "buy"
    assert w["exit_reason"] in ("tick_trail_exit", "profit_floor_hit")
    assert w["legs_count"] >= 2
    assert w["net_pnl_usd"] > 2.5


def test_dual_radar_cli_include_hybrids_runs_all_nine_engines_readonly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """--include-hybrids wires all 4 S10 VSA/SR/EMA engines cleanly alongside the 5 core engines (ORDERS=0)."""
    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_PAPER_TRADING", "true")
    monkeypatch.setattr(dual_cli, "ROOT", tmp_path)
    (tmp_path / "reports" / "paper_live").mkdir(parents=True)
    rep = tmp_path / "reports" / "paper_live" / "dual_hybrids_mock.json"

    rc = dual_cli.main(["--mock", "--include-hybrids", "--no-bell", "--report-file", str(rep)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SR_SWEEP_VSA_24H" in out
    assert "SR_SWEEP_VSA_SESS" in out
    assert "SR_BREAK_VSA_S10" in out
    assert "EMA_RECLAIM_S10" in out
    data = json.loads(rep.read_text(encoding="utf-8"))
    assert data["orders_submitted"] == 0
    assert len(data["engines"]) == 9


def test_dynamic_accel_duration_lot_scaler_invariants() -> None:
    """Verify Acceleration x Duration lot scaling and strict house-money risk-free SL cushion invariant."""
    from trade_mcp.s1_velocity_surfer import compute_dynamic_add_lot, compute_dynamic_l1_lot

    # 1) L1 scales up monotonically with tape acceleration and respects max_l1_lot cap
    calm = compute_dynamic_l1_lot(base_lot=0.02, max_l1_lot=0.06, max_total_lot=0.50, abs_move_60s=2.0, abs_move_5m=5.0)
    med = compute_dynamic_l1_lot(base_lot=0.02, max_l1_lot=0.06, max_total_lot=0.50, abs_move_60s=7.0, abs_move_5m=10.0)
    news = compute_dynamic_l1_lot(base_lot=0.02, max_l1_lot=0.06, max_total_lot=0.50, abs_move_60s=12.0, abs_move_5m=16.0, abs_move_15m=24.0)
    assert calm == 0.02
    assert calm < med <= news == 0.06

    # 2) In loss or without breathing cushion -> ZERO add lot!
    no_add = compute_dynamic_add_lot(
        side="sell",
        bid=3482.5,
        ask=3482.9,
        fill_px=3482.5,
        old_vol=0.06,
        old_avg=3483.1,
        breathing_room=1.50,
        base_lot=0.02,
        max_leg_lot=0.10,
        max_total_lot=0.50,
        dir_move_60s=10.0,
        dir_move_5m=15.0,
        hold_sec=60.0,
    )
    assert no_add == 0.0

    # 3) Deep in profit: add lot increases with BOTH acceleration AND duration, and post-add avg stays risk-free!
    short_slow = compute_dynamic_add_lot(
        side="sell",
        bid=3474.0,
        ask=3474.4,
        fill_px=3474.0,
        old_vol=0.12,
        old_avg=3481.0,
        breathing_room=1.50,
        base_lot=0.02,
        max_leg_lot=0.10,
        max_total_lot=0.50,
        dir_move_60s=1.0,
        dir_move_5m=3.0,
        hold_sec=5.0,
    )
    sustained_fast = compute_dynamic_add_lot(
        side="sell",
        bid=3474.0,
        ask=3474.4,
        fill_px=3474.0,
        old_vol=0.12,
        old_avg=3481.0,
        breathing_room=1.50,
        base_lot=0.02,
        max_leg_lot=0.10,
        max_total_lot=0.50,
        dir_move_60s=10.0,
        dir_move_5m=16.0,
        hold_sec=90.0,
    )
    assert 0.01 <= short_slow < sustained_fast <= 0.10
    # Verify ironclad gap-proof house-money invariant: new_avg leaves >= breathing_room + 0.45 above live ask!
    new_vol = round(0.12 + sustained_fast, 2)
    new_avg = (3481.0 * 0.12 + 3474.0 * sustained_fast) / new_vol
    assert (new_avg - 0.45 + 1e-6) >= (3474.4 + 1.50)


def test_dynamic_runners_scale_winners_cut_losers_and_run_in_dual_radar_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Dynamic-lot runners compound winners on house money, never add to losers, and wire cleanly via --include-dynamic."""
    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_PAPER_TRADING", "true")
    monkeypatch.setattr(dual_cli, "ROOT", tmp_path)
    (tmp_path / "reports" / "paper_live").mkdir(parents=True)
    rep = tmp_path / "reports" / "paper_live" / "dual_dyn_mock.json"

    rc = dual_cli.main(["--mock", "--include-dynamic", "--no-bell", "--report-file", str(rep)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SHOCK10_DYN_RUN" in out
    assert "PB_V2_DYN_SESS" in out
    assert "MACRO_WAVE_DYN" in out

    data = json.loads(rep.read_text(encoding="utf-8"))
    assert data["orders_submitted"] == 0
    assert len(data["engines"]) == 8
    assert "COMBO_DYN(SHK10D+PB)" in out
    assert data["combined_dynamic"]["net_usd"] > data["combined_primary"]["net_usd"] * 3.0

    flat_pb_pnl = data["engines"]["PB_DIP_V2_SESS"]["summary"]["realized_pnl_usd"]
    dyn_pb_pnl = data["engines"]["PB_V2_DYN_SESS"]["summary"]["realized_pnl_usd"]
    macro_pnl = data["engines"]["MACRO_WAVE_DYN"]["summary"]["realized_pnl_usd"]
    dyn_pb_max_vol = max(w["total_volume"] for w in data["engines"]["PB_V2_DYN_SESS"]["waves"])
    assert dyn_pb_max_vol >= 0.15  # compounded well beyond flat 0.04 lot!
    assert dyn_pb_pnl > flat_pb_pnl * 4.0
    assert macro_pnl > 200.0  # 55% multi-leg profit-lock ratchet captures big pyramided surges!

    # Verify failed-dip loser: PB_V2_DYN_SESS MUST NOT add a single leg in loss and cuts strictly at pivot SL!
    t0 = float(datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    bull_ctx = {"bias": 1, "label": "BULL_TREND", "support": 3450.0, "resistance": 3600.0}
    eng_fail = PullbackDipEngine(cfg=build_pullback_dip_profile_config("PB_V2_DYN_SESS", leg_lot=0.01, max_legs=4))
    spread = 0.36
    for step in range(4 * 365):
        sec = step * 0.25
        if sec < 300.0:
            bid = round(3480.00 + (12.00 * sec / 300.0), 2)
        elif sec < 320.0:
            bid = round(3492.00 - (3.10 * (sec - 300.0) / 20.0), 2)
        elif sec < 324.0:
            bid = round(3488.90 + (1.30 * (sec - 320.0) / 4.0), 2)
        else:
            bid = round(3490.20 - (15.00 * (sec - 324.0) / 41.0), 2)  # -$15 crash right after fill!
        eng_fail.on_tick(ts=t0 + sec, bid=bid, ask=round(bid + spread, 2), m1_context=bull_ctx)

    assert len(eng_fail.waves_closed) == 1
    w = eng_fail.waves_closed[0]
    assert w["legs_count"] == 1  # zero adds to loser!
    assert w["exit_reason"] == "hard_stop_loss"
    assert w["net_pnl_usd"] >= -8.0  # defensive 0.02 probe caps hard SL tightly (never -$18 or -$171!)


def _emit_bar_ticks(
    eng: Any,
    *,
    t_bar: float,
    bar_sec: int,
    o: float,
    h: float,
    low: float,
    c: float,
    spread: float = 0.36,
    ctx: dict[str, Any] | None = None,
) -> None:
    """Emit causal intra-bar OHLC ticks for a single multi-minute bar (t_bar .. t_bar + bar_sec - 1)."""
    m1_ctx = ctx or {"bias": 0, "label": "CALM"}
    pts = [
        (0.5, o),
        (bar_sec * 0.25, low if c >= o else h),
        (bar_sec * 0.65, h if c >= o else low),
        (bar_sec - 1.0, c),
    ]
    for dt, mid in pts:
        bid = round(mid - 0.5 * spread, 4)
        ask = round(mid + 0.5 * spread, 4)
        eng.on_tick(ts=t_bar + dt, bid=bid, ask=ask, m1_context=m1_ctx)


def test_calm_regime_trend_reclaim_rides_steady_drift_and_pyramids() -> None:
    """CALM_M3_SWING_24H catches steady calm pullback-reclaim drifts and compounds risk-free on house money."""
    from trade_mcp.calm_regime_engines import CalmRegimeEngine, build_calm_regime_profile_config

    eng = CalmRegimeEngine(cfg=build_calm_regime_profile_config("CALM_M3_SWING_24H", leg_lot=0.02, max_legs=8))
    t0 = 1764000000.0 - (1764000000.0 % 180.0)
    bar_sec = 180
    px = 3500.0
    bar_idx = 0

    # 14 steady bullish M3 bars (+$1.50/bar -> calm drift, NOT a $10/60s shock!)
    for _ in range(14):
        _emit_bar_ticks(eng, t_bar=t0 + bar_idx * bar_sec, bar_sec=bar_sec, o=px, h=px + 2.2, low=px - 0.8, c=px + 1.5)
        px += 1.5
        bar_idx += 1

    # Pullback bar into EMA8 zone, then bullish reclaim close back above EMA8
    _emit_bar_ticks(eng, t_bar=t0 + bar_idx * bar_sec, bar_sec=bar_sec, o=px, h=px + 0.4, low=px - 3.6, c=px - 2.6)
    px -= 2.6
    bar_idx += 1
    _emit_bar_ticks(eng, t_bar=t0 + bar_idx * bar_sec, bar_sec=bar_sec, o=px, h=px + 3.2, low=px - 0.6, c=px + 2.6)
    px += 2.6
    bar_idx += 1

    # Next bar starts (closes reclaim bar -> queues open -> fills +0.25s later!) and drifts smoothly +$18 then pulls back -$7
    t_open = t0 + bar_idx * bar_sec
    for step in range(4 * 420):
        sec = step * 0.25
        if sec < 300.0:
            mid = px + 18.0 * (sec / 300.0)
        else:
            mid = px + 18.0 - 8.5 * ((sec - 300.0) / 120.0)
        eng.on_tick(ts=t_open + sec, bid=round(mid - 0.18, 4), ask=round(mid + 0.18, 4), m1_context={"bias": 1, "label": "BULL_TREND"})

    assert len(eng.waves_closed) >= 1
    w0 = eng.waves_closed[0]
    assert w0["side"] == "buy"
    assert w0["legs_count"] >= 2
    assert w0["total_volume"] >= 0.03
    assert w0["net_pnl_usd"] > 20.0
    assert eng.summary()["orders_submitted"] == 0


def test_calm_regime_m5_range_bb_mean_reversion_and_anti_trend_guard() -> None:
    """CALM_M5_RANGE_24H fades lower-BB rejection in genuine sideways range and blocks fading strong trends."""
    from trade_mcp.calm_regime_engines import CalmRegimeEngine, build_calm_regime_profile_config

    eng = CalmRegimeEngine(cfg=build_calm_regime_profile_config("CALM_M5_RANGE_24H", leg_lot=0.02))
    t0 = 1764000000.0 - (1764000000.0 % 300.0)
    bar_sec = 300
    # 16 calm sideways M5 bars oscillating around 3565 (+/- $3.5)
    seq = [3564.0, 3567.0, 3563.5, 3566.5, 3564.0, 3567.5, 3563.0, 3566.0, 3564.5, 3567.0, 3563.5, 3566.5, 3564.0, 3567.0, 3564.5, 3566.0]
    bar_idx = 0
    for c_val in seq:
        _emit_bar_ticks(
            eng,
            t_bar=t0 + bar_idx * bar_sec,
            bar_sec=bar_sec,
            o=3565.0,
            h=max(3565.0, c_val) + 1.2,
            low=min(3565.0, c_val) - 1.2,
            c=c_val,
        )
        bar_idx += 1

    # Lower BB sweep + strong bullish rejection close back inside band (~3562.0)
    _emit_bar_ticks(
        eng,
        t_bar=t0 + bar_idx * bar_sec,
        bar_sec=bar_sec,
        o=3561.5,
        h=3562.4,
        low=3556.5,
        c=3562.2,
    )
    bar_idx += 1

    # Next bar starts (triggers fill) and mean-reverts back up to 3567.5 -> mean_tp_hit!
    t_fill = t0 + bar_idx * bar_sec
    for step in range(4 * 90):
        sec = step * 0.25
        mid = 3562.2 + 5.5 * (sec / 90.0)
        eng.on_tick(ts=t_fill + sec, bid=round(mid - 0.18, 4), ask=round(mid + 0.18, 4), m1_context={"bias": 0, "label": "CHOP_NEUTRAL"})

    assert len(eng.waves_closed) == 1
    assert eng.waves_closed[0]["side"] == "buy"
    assert eng.waves_closed[0]["exit_reason"] == "mean_tp_hit"
    assert eng.waves_closed[0]["net_pnl_usd"] > 5.0

    # Strict anti-trend guard: steady strong downtrend must NOT be faded!
    eng_trend = CalmRegimeEngine(cfg=build_calm_regime_profile_config("CALM_M5_RANGE_24H", leg_lot=0.02))
    px = 3600.0
    for i in range(22):
        _emit_bar_ticks(
            eng_trend,
            t_bar=t0 + i * bar_sec,
            bar_sec=bar_sec,
            o=px,
            h=px + 0.6,
            low=px - 4.2,
            c=px - 3.0,
        )
        px -= 3.0
    assert eng_trend.basket is None
    assert len(eng_trend.waves_closed) == 0



