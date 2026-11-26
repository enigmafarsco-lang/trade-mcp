"""Hermetic tests for RapidBurstAccelEngine, TickRobustZScoreEngine, and sweep CLI (ORDERS=0)."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from trade_mcp.quant_github_accel_lab import (  # noqa: E402
    RapidBurstAccelConfig,
    RapidBurstAccelEngine,
    TickRobustZScoreConfig,
    TickRobustZScoreEngine,
)


def _make_accel_wave_ticks() -> list[tuple[float, float, float]]:
    """Synthetic 1s stream with calm base followed by true accelerating downward explosion + deceleration."""
    t0 = 1795704000.0  # 14:40:00Z (active London/NY hour)
    ticks: list[tuple[float, float, float]] = []

    def add_seg(p0: float, p1: float, secs: int) -> None:
        nonlocal t0
        for i in range(1, secs + 1):
            mid = p0 + (p1 - p0) * (i / secs)
            ticks.append((t0 + i, round(mid - 0.20, 2), round(mid + 0.20, 2)))
        t0 += secs

    add_seg(3570.0, 3570.2, 60)   # calm flat base
    add_seg(3570.2, 3569.2, 10)   # mild velocity (-$0.10/s)
    add_seg(3569.2, 3565.2, 10)   # TRUE ACCELERATION (-$0.40/s -> accel = -$0.30/s!)
    add_seg(3565.2, 3557.0, 25)   # steep continuation for rapid slope stacking!
    add_seg(3557.0, 3559.5, 20)   # deceleration + bounce -> triggers decel_peak_book / ratchet
    return ticks


def _make_night_reversion_ticks() -> list[tuple[float, float, float]]:
    """Synthetic 2s night-session stream (02:00 SRV) oscillating around 3560 then stretching Z < -2.2 and reverting."""
    t0 = 1795658400.0  # 02:00:00Z (Asian/Night hour)
    ticks: list[tuple[float, float, float]] = []
    # 30 minutes of calm +/- $2.0 oscillation around 3560 (builds SMA20, StdDev~$1.4, low ADX)
    for s in range(0, 1800, 2):
        mid = 3560.0 + 2.0 * math.sin(s / 90.0)
        ticks.append((t0 + s, round(mid - 0.18, 2), round(mid + 0.18, 2)))
    cur_t = t0 + 1800
    # 2 minutes sharp stretch down to 3555.0 (Z < -2.5)
    for s in range(2, 122, 2):
        mid = 3560.0 - 5.0 * (s / 120.0)
        ticks.append((cur_t + s, round(mid - 0.18, 2), round(mid + 0.18, 2)))
    cur_t += 120
    # 1 minute turn-confirmation bar (small +$0.25 hammer bounce to 3555.25, still stretched Z < -2.0 -> BUY!)
    for s in range(2, 62, 2):
        mid = 3555.0 + 0.25 * (s / 60.0)
        ticks.append((cur_t + s, round(mid - 0.18, 2), round(mid + 0.18, 2)))
    cur_t += 60
    # 8 minutes smooth reversion back to 3560.5 -> hits Limit TP at SMA mean!
    for s in range(2, 482, 2):
        mid = 3555.25 + 5.25 * (s / 480.0)
        ticks.append((cur_t + s, round(mid - 0.18, 2), round(mid + 0.18, 2)))
    return ticks


def test_rapid_burst_accel_scales_lot_and_stacks_by_slope() -> None:
    ticks = _make_accel_wave_ticks()
    eng_fixed = RapidBurstAccelEngine(
        cfg=RapidBurstAccelConfig(
            name="FIXED_1L",
            vel_win_sec=10.0,
            min_vel_per_sec=0.16,
            require_accel=True,
            min_accel_per_sec=0.05,
            min_60s_move=3.0,
            variable_l1_lot=False,
            enable_slope_stacking=False,
        )
    )
    eng_stack = RapidBurstAccelEngine(
        cfg=RapidBurstAccelConfig(
            name="VARLOT_STACK",
            vel_win_sec=10.0,
            min_vel_per_sec=0.16,
            require_accel=True,
            min_accel_per_sec=0.05,
            min_60s_move=3.0,
            variable_l1_lot=True,
            max_l1_lot=0.06,
            enable_slope_stacking=True,
            max_legs=6,
            add_step_price=1.20,
            add_interval_sec=3.0,
        )
    )
    for ts, bid, ask in ticks:
        eng_fixed.on_tick(ts=ts, bid=bid, ask=ask)
        eng_stack.on_tick(ts=ts, bid=bid, ask=ask)
    eng_fixed.force_close_open(ts=ticks[-1][0], bid=ticks[-1][1], ask=ticks[-1][2])
    eng_stack.force_close_open(ts=ticks[-1][0], bid=ticks[-1][1], ask=ticks[-1][2])

    assert len(eng_fixed.waves_closed) >= 1
    assert len(eng_stack.waves_closed) >= 1
    assert eng_stack.waves_closed[0]["total_volume"] > eng_fixed.waves_closed[0]["total_volume"]
    assert eng_stack.waves_closed[0]["legs_count"] >= 2
    assert eng_stack.realized_pnl_usd > eng_fixed.realized_pnl_usd > 0.0
    assert all(w["orders_submitted"] == 0 for w in eng_stack.waves_closed)


def test_tick_robust_zscore_reverts_profitably_during_night_session() -> None:
    ticks = _make_night_reversion_ticks()
    eng = TickRobustZScoreEngine(
        cfg=TickRobustZScoreConfig(
            name="NIGHT_MR",
            bar_sec=60,
            entry_z=2.0,
            adx_max=35.0,
            min_sd_cost_mult=2.0,
            min_tp_cost_mult=3.0,
            active_server_hours=(0, 1, 2, 3, 4, 5),
        )
    )
    for ts, bid, ask in ticks:
        eng.on_tick(ts=ts, bid=bid, ask=ask)
    eng.force_close_open(ts=ticks[-1][0], bid=ticks[-1][1], ask=ticks[-1][2])

    assert len(eng.waves_closed) >= 1
    assert eng.waves_closed[0]["side"] == "buy"
    assert eng.realized_pnl_usd > 0.0
    assert eng.waves_closed[0]["orders_submitted"] == 0


def test_run_quant_accel_sweep_cli_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import run_quant_accel_sweep as sweep_cli

    monkeypatch.setattr(sweep_cli, "ROOT", tmp_path)
    (tmp_path / "logs").mkdir(parents=True)
    f1 = tmp_path / "logs" / "s1_ticks_XAUUSD_i_20261126.jsonl"
    ticks = _make_night_reversion_ticks() + _make_accel_wave_ticks()
    with f1.open("w", encoding="utf-8") as fh:
        for ts, bid, ask in ticks:
            fh.write(json.dumps({"ts": ts, "bid": bid, "ask": ask}) + "\n")

    rc = sweep_cli.main(["--save"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PART 0:" in out
    assert "PART 1:" in out
    assert "PART 2: NIGHT / QUIET HOURS ONLY" in out
    assert "PART 4: 24-HOUR AW_V3 COMBINED PORTFOLIOS" in out
    saved = json.loads((tmp_path / "reports" / "paper_live" / "quant_accel_sweep.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "quant_accel_sweep_v2"
    assert saved["orders_submitted"] == 0
    assert "RB_V3_RFSTACK_SESS" in saved["all_stats"]
    assert "GH_Z_M1_Z22_24H" in saved["all_stats"]
    assert "QR_BOX30_NIGHT" in saved["all_stats"]


def test_quiet_range_bounce_reverts_night_box() -> None:
    from trade_mcp.quant_github_accel_lab import QuietRangeBounceConfig, QuietRangeBounceEngine

    ticks = _make_night_reversion_ticks()
    eng = QuietRangeBounceEngine(
        cfg=QuietRangeBounceConfig(
            name="QR_TEST",
            adx_max=35.0,
            min_box_range=3.00,
            max_box_range=15.00,
            edge_frac=0.20,
            min_tp_cost_mult=2.5,
            active_server_hours=(0, 1, 2, 3, 4, 5),
        )
    )
    for ts, bid, ask in ticks:
        eng.on_tick(ts=ts, bid=bid, ask=ask)
    eng.force_close_open(ts=ticks[-1][0], bid=ticks[-1][1], ask=ticks[-1][2])
    assert len(eng.waves_closed) >= 1
    assert eng.realized_pnl_usd > 0.0
    from trade_mcp.s1_impulse_audit import combine_portfolio_waves

    st = combine_portfolio_waves("QR_TEST", [eng.waves_closed])
    assert st["net_usd"] > 0.0
    assert eng.waves_closed[0]["orders_submitted"] == 0


def test_run_champions_24h_evolution_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import run_champions_24h_evolution as evo

    monkeypatch.setattr(evo, "ROOT", tmp_path)
    (tmp_path / "logs").mkdir(parents=True)
    f1 = tmp_path / "logs" / "s1_ticks_XAUUSD_i_20261126.jsonl"
    ticks = _make_night_reversion_ticks() + _make_accel_wave_ticks()
    with f1.open("w", encoding="utf-8") as fh:
        for ts, bid, ask in ticks:
            fh.write(json.dumps({"ts": ts, "bid": bid, "ask": ask}) + "\n")

    rc = evo.main(["--save"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PART 1:" in out
    assert "PART 2: HEAD-TO-HEAD AW_V3 PORTFOLIOS" in out
    saved = json.loads((tmp_path / "reports" / "paper_live" / "champions_24h_evolution.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "champions_24h_evolution_v1"
    assert saved["orders_submitted"] == 0
    assert "AW_V3_24H_PRO_VARLOT" in saved["portfolios"]


def test_inspect_drop_window_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import inspect_drop_window as drop_cli

    monkeypatch.setattr(drop_cli, "ROOT", tmp_path)
    (tmp_path / "logs").mkdir(parents=True)
    f1 = tmp_path / "logs" / "s1_ticks_XAUUSD_i_20261126.jsonl"
    ticks = _make_accel_wave_ticks()
    with f1.open("w", encoding="utf-8") as fh:
        for ts, bid, ask in ticks:
            fh.write(json.dumps({"ts": ts, "bid": bid, "ask": ask}) + "\n")

    rc = drop_cli.main(["--since-hour", "14"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PART 1: FORENSIC DROP MICROSCOPE" in out
    assert "PART 2: 5s/15s VERTICAL-CLIFF A/B" in out
    assert "CLIFF_10S_28_24H" in out


def test_donchian_breakout_and_latest_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import inspect_latest_and_test_breakout as brk_cli

    monkeypatch.setattr(brk_cli, "ROOT", tmp_path)
    (tmp_path / "logs").mkdir(parents=True)
    f1 = tmp_path / "logs" / "s1_ticks_XAUUSD_i_20261126.jsonl"
    # Build 35 minutes of gentle range followed by a clean downward structural breakdown!
    t0 = 1795708800.0  # 2026-11-26 16:00:00 UTC
    ticks: list[tuple[float, float, float]] = []
    for m in range(35):
        base = 3580.0 - 0.12 * m + (1.4 if m % 2 == 0 else -1.4)
        for s in range(0, 60, 4):
            mid = base + 0.3 * math.sin(s / 10.0)
            ticks.append((t0 + m * 60 + s, round(mid - 0.18, 2), round(mid + 0.18, 2)))
    # Sharp structural breakdown -$9 over 3 minutes
    cur_t = t0 + 35 * 60
    for s in range(0, 180, 2):
        mid = 3574.0 - 9.0 * (s / 180.0)
        ticks.append((cur_t + s, round(mid - 0.18, 2), round(mid + 0.18, 2)))
    with f1.open("w", encoding="utf-8") as fh:
        for ts, bid, ask in ticks:
            fh.write(json.dumps({"ts": ts, "bid": bid, "ask": ask}) + "\n")

    rc = brk_cli.main(["--since-hour", "16"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PART 1: TRUE LATEST STITCHED TIMELINE" in out
    assert "GH_DONCH_M1_30_24H" in out


def test_reverse_engineer_manual_edge_enriches_ticks() -> None:
    import reverse_engineer_manual_edge as rev

    ticks = _make_accel_wave_ticks()
    times = [t for t, _, _ in ticks]
    mids = [0.5 * (b + a) for _, b, a in ticks]
    t_entry = times[75]
    t_exit = times[95]
    sample_trades = [
        {
            "side": "sell",
            "volume": 0.05,
            "entry_price": 3566.5,
            "exit_price": 3560.0,
            "opened_at": t_entry,
            "closed_at": t_exit,
            "hold_seconds": int(t_exit - t_entry),
            "net_usd": 32.5,
        }
    ]
    xray = rev.enrich_trades_with_tick_physics(sample_trades, times, mids)
    assert xray["orders_submitted"] == 0
    assert xray["all"]["trades"] == 1
    assert xray["all"]["net_usd"] == 32.5
    assert xray["all"]["matched_ticks"] == 1
    assert xray["trades"][0]["phys"]["with_60s_trend"] is True

