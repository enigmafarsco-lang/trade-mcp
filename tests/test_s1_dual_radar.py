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
