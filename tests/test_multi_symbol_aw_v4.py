"""Tests for Symbol-Adaptive Multi-Asset AW_V4 Wrapper & Multi-Symbol Spread Audit (ORDERS=0)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_multi_symbol_waves as msa  # noqa: E402

from trade_mcp.multi_symbol_aw_v4 import (  # noqa: E402
    NormalizedAllWeatherV4,
    compute_risk_parity_lot_scale,
    load_alpari_specs,
)
from trade_mcp.s1_dual_radar import make_synthetic_dual_session  # noqa: E402


def _restore_trade_mcp_env(prev_allow: str | None, prev_paper: str | None) -> None:
    if prev_allow is None:
        os.environ.pop("TRADE_MCP_ALLOW_TRADING", None)
    else:
        os.environ["TRADE_MCP_ALLOW_TRADING"] = prev_allow
    if prev_paper is None:
        os.environ.pop("TRADE_MCP_PAPER_TRADING", None)
    else:
        os.environ["TRADE_MCP_PAPER_TRADING"] = prev_paper


def test_alpari_specs_and_currency_conversion() -> None:
    specs = load_alpari_specs(ROOT / "reports" / "paper_live" / "alpari_wave_symbols_scan.json")
    assert specs["NAS100_i"].contract_size == 1.0
    assert specs["NAS100_i"].currency_profit == "USD"
    assert specs["XAGUSD_i"].contract_size == 5000.0
    assert specs["WTI_i"].contract_size == 1000.0
    assert specs["GER40_i"].currency_profit == "EUR"
    assert abs(specs["GER40_i"].usd_per_price_unit(25000.0, eur_usd=1.10) - 1.10) < 1e-9
    assert specs["USDJPY_i"].currency_profit == "JPY"
    assert abs(specs["USDJPY_i"].usd_per_price_unit(160.0) - 625.0) < 1e-6


def test_risk_parity_lot_scale_handles_parity_and_min_lot_clamp() -> None:
    # NAS100_i: S=10.0 -> SL=28 pts -> $28/lot -> 0.20 lot = $5.60 exact Gold 1x parity
    ls, rlot, sl_usd, clamped = compute_risk_parity_lot_scale(
        price_scale=10.0, usd_per_price_unit=1.0, volume_min=0.01, volume_step=0.01
    )
    assert clamped is False
    assert rlot == 0.20
    assert ls == 10.0
    assert abs(sl_usd - 5.60) < 1e-6

    # XAGUSD_i: S=0.09 -> SL=0.252 -> $1260/lot -> broker 0.02 splittable min = $25.20 SL (flagged!)
    ls_ag, rlot_ag, sl_ag, clamped_ag = compute_risk_parity_lot_scale(
        price_scale=0.09, usd_per_price_unit=5000.0, volume_min=0.01, volume_step=0.01
    )
    assert clamped_ag is True
    assert rlot_ag == 0.02
    assert ls_ag == 1.0
    assert abs(sl_ag - 25.20) < 1e-6

    # Broker with 0.10 volume_min / step (e.g. certain index contracts)
    ls_idx, rlot_idx, _, clamped_idx = compute_risk_parity_lot_scale(
        price_scale=10.0, usd_per_price_unit=1.0, volume_min=0.10, volume_step=0.10
    )
    assert clamped_idx is False
    assert rlot_idx == 0.20
    assert ls_idx == 10.0


def test_normalized_aw_v4_exact_scale_and_pnl_isomorphism() -> None:
    gold_ticks = make_synthetic_dual_session()

    gold = NormalizedAllWeatherV4(
        symbol="XAUUSD_i",
        price_scale=1.0,
        usd_per_price_unit=100.0,
        lot_scale=1.0,
        digits=2,
    )
    # NAS100-scaled stream: prices * 10.0, multiplier $1.0, lot_scale 10.0 -> exact $1.00-for-$1.00 USD PnL!
    nas = NormalizedAllWeatherV4(
        symbol="NAS100_i",
        price_scale=10.0,
        usd_per_price_unit=1.0,
        lot_scale=10.0,
        digits=1,
    )
    # Silver-scaled stream: binary-exact 0.0625 (=1/16), multiplier $5000 -> exact (5000*0.0625/100) = 3.125x USD PnL!
    xag = NormalizedAllWeatherV4(
        symbol="XAGUSD_i",
        price_scale=0.0625,
        usd_per_price_unit=5000.0,
        lot_scale=1.0,
        digits=3,
    )

    for ts, bid, ask, m1_ctx in gold_ticks:
        gold.on_tick(ts, bid, ask, m1_ctx)
        m1_nas = dict(m1_ctx)
        m1_xag = dict(m1_ctx)
        for k in ("support", "resistance", "atr_m1", "last_close"):
            if isinstance(m1_ctx.get(k), (int, float)):
                m1_nas[k] = float(m1_ctx[k]) * 10.0
                m1_xag[k] = float(m1_ctx[k]) * 0.0625
        nas.on_tick(ts, bid * 10.0, ask * 10.0, m1_nas)
        xag.on_tick(ts, bid * 0.0625, ask * 0.0625, m1_xag)

    last_ts, last_bid, last_ask, _ = gold_ticks[-1]
    gold.close_open(last_ts, last_bid, last_ask)
    nas.close_open(last_ts, last_bid * 10.0, last_ask * 10.0)
    xag.close_open(last_ts, last_bid * 0.0625, last_ask * 0.0625)

    assert len(gold.waves_closed) >= 2
    assert len(nas.waves_closed) == len(gold.waves_closed)
    assert len(xag.waves_closed) == len(gold.waves_closed)

    net_gold = sum(float(w["net_pnl_usd"]) for w in gold.waves_closed)
    net_nas = sum(float(w["net_pnl_usd"]) for w in nas.waves_closed)
    net_xag = sum(float(w["net_pnl_usd"]) for w in xag.waves_closed)

    assert abs(net_nas - net_gold) < 1e-6
    assert abs(net_xag - (3.125 * net_gold)) < 1e-4

    # Native entry prices and real lots denormalized accurately
    w0_g, w0_n = gold.waves_closed[0], nas.waves_closed[0]
    assert abs(float(w0_n["avg_entry"]) - (10.0 * float(w0_g["avg_entry"]))) < 0.2
    assert abs(float(w0_n["total_volume"]) - (10.0 * float(w0_g["total_volume"]))) < 0.02


def test_scale_free_m1_context_has_no_gold_atr_floor() -> None:
    # 30 Silver M1 bars with $0.025 range -> true ATR ~0.025 (must NOT clamp to Gold's 0.10!)
    bars = [
        [float(i * 60), 44.000 + i * 0.005, 44.015 + i * 0.005, 43.990 + i * 0.005, 44.008 + i * 0.005, 10.0]
        for i in range(30)
    ]
    ctx = msa._scale_free_m1_context(bars)
    assert 0.01 < float(ctx["atr_m1"]) < 0.05
    assert ctx["bias"] == 1


def test_normalized_aw_v4_blocks_market_reopen_gap_after_freeze() -> None:
    nas = NormalizedAllWeatherV4(
        symbol="NAS100_i",
        price_scale=10.0,
        usd_per_price_unit=1.0,
        lot_scale=10.0,
        digits=1,
        stale_freeze_sec=90.0,
        reopen_warmup_sec=120.0,
    )
    m1_bull = {"bias": 1, "label": "BULL_TREND", "support": 25950.0, "resistance": 26050.0, "atr_m1": 15.0, "pullback_resume": False, "last_close": 26000.0}
    ts = 1_795_700_000.0
    # 1) Normal moving market for 60s
    for i in range(60):
        px = 26000.0 + (i % 5) * 0.5
        nas.on_tick(ts + i, px, px + 3.8, m1_bull)
    # 2) Market closes & freezes identically for 400s
    for j in range(1, 401):
        nas.on_tick(ts + 60 + j, 26002.0, 26008.0, m1_bull)
    # 3) Market reopens with massive +220 pt gap + volatile spikes over 60s
    for k in range(60):
        px = 26220.0 + k * 2.0
        nas.on_tick(ts + 461 + k, px, px + 4.0, m1_bull)

    assert nas.frozen_ticks_skipped >= 300
    assert nas.reopen_resets == 1
    assert len(nas.waves_closed) == 0
    assert nas._has_open_position() is False


def test_load_deduplicated_symbol_segments_strips_frozen_market_hours(tmp_path: Path) -> None:
    import json

    p = tmp_path / "s1_ticks_NAS100_i_20261126.jsonl"
    ts0 = 1_795_700_000.0
    rows: list[str] = []
    # Segment 1: 40 live moving ticks
    for i in range(40):
        b = 26000.0 + i * 0.5
        rows.append(json.dumps({"ts": ts0 + i, "bid": b, "ask": b + 4.0}))
    # 300s frozen closed market (identical bid/ask)
    for j in range(1, 301):
        rows.append(json.dumps({"ts": ts0 + 40 + j, "bid": 26019.5, "ask": 26025.5}))
    # Segment 2: reopen with +200 pt gap & 40 live moving ticks
    for k in range(40):
        b = 26220.0 + k * 0.5
        rows.append(json.dumps({"ts": ts0 + 341 + k, "bid": b, "ask": b + 4.0}))
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")

    segs, meta = msa.load_deduplicated_symbol_segments([p], freeze_split_sec=90.0)
    assert len(segs) == 2
    assert meta["freeze_events"] == 1
    assert meta["frozen_ticks"] == 299
    assert meta["active_ticks"] == 81


def test_anti_teleport_guard_blocks_single_tick_flash_spike() -> None:
    """Reproduces the 05:16:34 -> 05:17:03 single-tick +147 pt NAS100 flash-spike & V-collapse."""
    m1_bull = {
        "bias": 1,
        "label": "BULL_TREND",
        "support": 26550.0,
        "resistance": 26650.0,
        "atr_m1": 15.0,
        "pullback_resume": False,
        "last_close": 26602.0,
    }
    ts = 1_795_756_500.0
    ticks: list[tuple[float, float, float]] = []
    # 70s of calm normal ticks around 26602
    for i in range(70):
        b = 26600.0 + (i % 4) * 0.5
        ticks.append((ts + i, b, b + 4.0))
    # Single-tick +147 pt teleport! Holds ~25s then collapses straight back -147 pts
    for j in range(25):
        b = 26746.0 + (j % 2) * 0.5
        ticks.append((ts + 70 + j, b, b + 4.0))
    for k in range(20):
        b = 26600.0 - k * 0.2
        ticks.append((ts + 95 + k, b, b + 4.0))

    unguarded = NormalizedAllWeatherV4(
        symbol="NAS100_i",
        price_scale=9.75,
        usd_per_price_unit=1.0,
        lot_scale=10.0,
        digits=1,
        max_single_tick_jump_norm=0.0,
    )
    guarded = NormalizedAllWeatherV4(
        symbol="NAS100_i",
        price_scale=9.75,
        usd_per_price_unit=1.0,
        lot_scale=10.0,
        digits=1,
        max_single_tick_jump_norm=4.50,
        teleport_cooldown_sec=90.0,
    )

    for t, b, a in ticks:
        unguarded.on_tick(t, b, a, m1_bull)
        guarded.on_tick(t, b, a, m1_bull)

    # Without guard, VSNAP6 fires into the +147 pt spike and suffers a huge stop loss; WITH guard -> ZERO trades!
    assert len(unguarded.waves_closed) >= 1
    assert sum(float(w["net_pnl_usd"]) for w in unguarded.waves_closed) < -20.0
    assert guarded.teleport_blocks >= 1
    assert len(guarded.waves_closed) == 0
    assert guarded._has_open_position() is False


def test_record_multi_ticks_live_wave_top4_enters_record_mode() -> None:
    import argparse

    import record_multi_ticks_live as rml

    def _ns(**kw: object) -> argparse.Namespace:
        defaults: dict[str, object] = {
            "scan": False,
            "auto_wave": False,
            "wave_top3": False,
            "wave_top4": False,
            "symbols": [],
            "include_gold": False,
        }
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    assert rml.resolve_recorder_mode(_ns()) == ("scan", [])
    assert rml.resolve_recorder_mode(_ns(scan=True, wave_top4=True)) == ("scan", [])
    assert rml.resolve_recorder_mode(_ns(wave_top4=True)) == ("record", ["NAS100_i", "GER40_i", "US500_i", "XAGUSD_i"])
    assert rml.resolve_recorder_mode(_ns(wave_top4=True, include_gold=True)) == (
        "record",
        ["XAUUSD_i", "NAS100_i", "GER40_i", "US500_i", "XAGUSD_i"],
    )
    assert rml.resolve_recorder_mode(_ns(wave_top3=True)) == ("record", ["NAS100_i", "XAGUSD_i", "GER40_i"])
    assert rml.resolve_recorder_mode(_ns(symbols=["NAS100_i", "US500_i"])) == ("record", ["NAS100_i", "US500_i"])
    assert rml.resolve_recorder_mode(_ns(auto_wave=True)) == ("record", [])


def test_audit_multi_symbol_waves_mock_cli_runs_cleanly(capsys) -> None:
    prev_allow = os.environ.get("TRADE_MCP_ALLOW_TRADING")
    prev_paper = os.environ.get("TRADE_MCP_PAPER_TRADING")
    try:
        rc = msa.main(["--mock"])
    finally:
        _restore_trade_mcp_env(prev_allow, prev_paper)
    out = capsys.readouterr().out
    assert rc == 0
    assert "SECTION 1 — ACTIVE-MARKET SPREAD ANATOMY" in out
    assert "SECTION 1B — SINGLE-TICK TELEPORT / FLASH-SPIKE FORENSICS" in out
    assert "V4_SHIPPED_1.0" in out
    assert "V4_GUARD_1.0" in out
    assert "V4_GUARD_24H" in out
    assert "NAS100_i" in out
    assert "XAGUSD_i" in out
    assert "USDJPY_i" in out
    assert "REJECT (SPREAD_HEAVY / DEAD RANGE)" in out


def test_fast_snap_and_macro_htf_60m_counter_trend_guards() -> None:
    """Verify causal 60m HTF guards block dead-cat counter-trend bounces even after 15m/30m decay."""
    from trade_mcp.all_weather_paper import AllWeatherPaperSuite

    # Build a synthetic 52m timeline:
    # - Minutes 0..15: steady selloff from 3662 -> 3642 (-$20)
    # - Minutes 15..51: sideways consolidation around 3642.5 (so 15m & 30m net move ~ 0!)
    # - Minute 51: sharp +$6.8 bounce in 25s (3642.5 -> 3649.3) -> triggers VSNAP6 BUY if only 15m/30m active,
    #              but 60m move is still ~3649.3 - 3660.8 = -$11.5 <= -10.0, so block_counter_60m=10 blocks it!
    base_ts = 1795788000.0  # 14:00:00
    ticks: list[tuple[float, float, float]] = []
    px = 3662.0
    for sec in range(0, 900, 5):  # 0..15m drop -$20
        px = 3662.0 - 20.0 * (sec / 900.0)
        ticks.append((base_ts + sec, round(px - 0.15, 2), round(px + 0.15, 2)))
    for sec in range(900, 3060, 5):  # 15m..51m flat around 3642.5
        px = 3642.5 + 0.3 * ((sec // 30) % 2)
        ticks.append((base_ts + sec, round(px - 0.15, 2), round(px + 0.15, 2)))
    # Sharp +$6.80 bounce in 25s (3642.5 -> 3649.3); 60m net move is still <= -$11.0!
    for i in range(1, 26):
        px = 3642.5 + 6.8 * (i / 25.0)
        ticks.append((base_ts + 3060 + i, round(px - 0.15, 2), round(px + 0.15, 2)))

    s_ctrl = AllWeatherPaperSuite(engine_version="v4", risk_multiplier=2.0)
    s_guard = AllWeatherPaperSuite(engine_version="v4", risk_multiplier=2.0)
    assert s_ctrl.fast_shock_engine is not None and s_guard.fast_shock_engine is not None
    s_guard.fast_shock_engine.cfg.block_counter_60m = 10.0

    for ts, b, a in ticks:
        s_ctrl.fast_shock_engine.on_tick(ts=ts, bid=b, ask=a)
        s_guard.fast_shock_engine.on_tick(ts=ts, bid=b, ask=a)

    assert s_ctrl.fast_shock_engine.basket is not None or s_ctrl.fast_shock_engine.pending is not None
    assert s_guard.fast_shock_engine.basket is None and s_guard.fast_shock_engine.pending is None
    assert s_guard.fast_shock_engine.signals_blocked_htf >= 1

    # Also verify PullbackDipEngine (MACRO_WAVE) seed_htf_bars + 60m counter-trend guard
    s_macro = AllWeatherPaperSuite(engine_version="v4", risk_multiplier=1.0)
    bars = [{"bar_id": float(int((base_ts - (60 - k) * 60) // 60)), "c": 3665.0 - 0.30 * k} for k in range(60)]
    seeded = s_macro.macro_engine.seed_htf_bars(bars)
    assert seeded == 60
    assert s_macro.macro_engine._htf_net_move(int(base_ts), 3645.0, 3600) <= -18.0


def test_ab_structural_risk_variants_and_mock_cli(capsys) -> None:
    """Verify structural variant mutator preserves Leg 2+ adds and ab_test_structural_risk --mock succeeds."""
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import ab_test_structural_risk as ab_sr

    from trade_mcp.all_weather_paper import AllWeatherPaperSuite

    # Check Fix-L1 caps Leg 1 to base leg_lot at 2x risk while keeping Leg 2+ pyramid caps intact
    suite = AllWeatherPaperSuite(engine_version="v4", risk_multiplier=2.0)
    assert suite.fast_shock_engine is not None
    orig_max_leg = suite.fast_shock_engine.cfg.dyn_max_leg_lot
    orig_max_tot = suite.fast_shock_engine.cfg.dyn_max_total_lot
    b3 = next(v for v in ab_sr.VARIANTS if v.name == "B3_FIX_L1+HTF60")
    ab_sr.apply_structural_variant(suite, b3)
    assert suite.fast_shock_engine.cfg.dyn_max_l1_lot == suite.fast_shock_engine.cfg.leg_lot == 0.04
    assert suite.fast_shock_engine.cfg.dyn_max_leg_lot == orig_max_leg
    assert suite.fast_shock_engine.cfg.dyn_max_total_lot == orig_max_tot
    assert suite.fast_shock_engine.cfg.block_counter_60m == 10.0
    assert suite.macro_engine.cfg.dyn_max_l1_lot == suite.macro_engine.cfg.leg_lot
    assert suite.macro_engine.cfg.block_counter_30m == 8.0
    assert suite.macro_engine.cfg.block_counter_60m == 10.0

    # Check B7 (1.5x L1 cap) and B6 (unchanged dynamic L1)
    suite_b7 = AllWeatherPaperSuite(engine_version="v4", risk_multiplier=2.0)
    b7 = next(v for v in ab_sr.VARIANTS if v.name == "B7_HTF120_CAP15")
    ab_sr.apply_structural_variant(suite_b7, b7)
    assert suite_b7.fast_shock_engine is not None
    assert suite_b7.fast_shock_engine.cfg.dyn_max_l1_lot == 0.06
    assert suite_b7.fast_shock_engine.cfg.block_counter_120m == 16.0

    # Check trailing weekend frozen quote trimmer (keeps first tick of closing quote, strips 39 frozen duplicates)
    dummy_seg = [(1000.0 + i, round(3600.0 + i * 0.1, 2), round(3600.3 + i * 0.1, 2), {}) for i in range(5)]
    dummy_seg += [(1005.0 + i * 10.0, 3600.4, 3600.7, {}) for i in range(40)]  # 390s frozen tail
    trimmed, n_rem = ab_sr._trim_frozen_segment_tails([dummy_seg], freeze_tail_sec=300.0)
    assert n_rem == 40
    assert len(trimmed[0]) == 5

    # End-to-end --mock CLI run
    prev_allow = os.environ.get("TRADE_MCP_ALLOW_TRADING")
    prev_paper = os.environ.get("TRADE_MCP_PAPER_TRADING")
    try:
        rc = ab_sr.main(["--mock", "--risk-mult", "2.0"])
    finally:
        _restore_trade_mcp_env(prev_allow, prev_paper)
    out = capsys.readouterr().out
    assert rc == 0
    assert "A0_PROD_V4" in out
    assert "B3_FIX_L1+HTF60" in out
    assert "B6_HTF120_DYN_L1" in out
    assert "B7_HTF120_CAP15" in out
    assert "SCIENTIFIC ANTI-OVERFIT VERDICT" in out


def test_multi_symbol_aw_v5_scale_free_m1_seed_and_live_mock(tmp_path: Path, monkeypatch, capsys) -> None:
    """Verify NormalizedAllWeatherV5, scale-free Silver M1 context, M1 seeding, and daytrade_multi_symbol_live --mock."""
    from trade_mcp.multi_symbol_aw_v4 import (
        NormalizedAllWeatherV5,
        analyze_scale_free_m1_context,
    )

    # 1) Scale-free M1 analyzer accurately measures Silver ATR (~0.03) without Gold's 0.10 floor bug
    base_ts = 1795860000.0
    silver_bars = [
        [base_ts + i * 60.0, 44.200 + i * 0.015, 44.225 + i * 0.015, 44.195 + i * 0.015, 44.215 + i * 0.015, 10.0]
        for i in range(40)
    ]
    ctx = analyze_scale_free_m1_context(silver_bars, server_now_ts=base_ts + 45 * 60.0)
    assert ctx["bias"] == 1
    assert 0.020 <= ctx["atr_m1"] <= 0.045  # Gold analyzer would falsely clamp to 0.10!
    assert ctx["support"] < ctx["resistance"]

    # 2) NormalizedAllWeatherV5 wires engine_version=v5, 60m/120m HTF, 4.5x teleport, and disables inner double-counting
    eng = NormalizedAllWeatherV5(
        symbol="NAS100_i",
        price_scale=9.75,
        usd_per_price_unit=1.0,
        lot_scale=10.0,
        digits=1,
    )
    assert eng.engine_version == "v5"
    assert eng.suite.engine_version == "v5"
    assert eng.suite.stale_freeze_sec == 0.0
    assert eng.suite.max_single_tick_jump_price == 0.0
    assert eng.suite.fast_shock_engine is not None
    assert eng.suite.fast_shock_engine.cfg.block_counter_60m == 12.0
    assert eng.suite.fast_shock_engine.cfg.block_counter_120m == 16.0
    assert eng.suite.macro_engine.cfg.block_counter_120m == 16.0

    # Verify normalized seed_m1_bars warms up 120m HTF cleanly
    nas_bars = [
        [base_ts + i * 60.0, 25000.0 + i * 2.0, 25010.0 + i * 2.0, 24995.0 + i * 2.0, 25002.0 + i * 2.0, 10.0]
        for i in range(130)
    ]
    seeded = eng.seed_m1_bars(nas_bars, server_now_ts=base_ts + 135 * 60.0, spread=3.9)
    assert seeded == 120
    assert len(eng.suite.fast_shock_engine._htf_seed) == 125
    assert len(eng.suite.macro_engine._htf_seed) == 125
    assert eng.suite.fast_shock_engine._htf_net_move(int(base_ts + 130 * 60), 25260.0 / 9.75, 7200) > 20.0
    assert eng.suite.macro_engine._htf_net_move(int(base_ts + 130 * 60), 25260.0 / 9.75, 7200) > 20.0

    # 3) End-to-end daytrade_multi_symbol_live.py --mock --preset top4 --report-file
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import daytrade_multi_symbol_live as ms_live

    monkeypatch.setattr(ms_live, "ROOT", tmp_path)
    (tmp_path / "reports" / "paper_live").mkdir(parents=True)
    rep = tmp_path / "reports" / "paper_live" / "ms_v5_mock.json"

    prev_allow = os.environ.get("TRADE_MCP_ALLOW_TRADING")
    prev_paper = os.environ.get("TRADE_MCP_PAPER_TRADING")
    try:
        rc = ms_live.main(["--mock", "--preset", "top4", "--report-file", str(rep)])
    finally:
        _restore_trade_mcp_env(prev_allow, prev_paper)
    out = capsys.readouterr().out
    assert rc == 0
    assert "MULTI-SYMBOL ALL-WEATHER V5 PAPER" in out
    assert "NAS100_i" in out and "XAGUSD_i" in out and "US500_i" in out and "GER40_i" in out

    snap = json.loads(rep.read_text(encoding="utf-8"))
    assert snap["schema"] == "multi_symbol_aw_v5_live_v1"
    assert snap["orders_submitted"] == 0
    assert snap["engine_version"] == "v5"
    assert snap["proven_portfolio"]["symbols"] == ["NAS100_i", "XAGUSD_i"]
    assert snap["watchlist_summary"]["symbols"] == ["US500_i", "GER40_i"]
    for sym in ("NAS100_i", "XAGUSD_i", "US500_i", "GER40_i"):
        assert snap["symbols"][sym]["account"]["teleport_blocks"] >= 1
        assert snap["symbols"][sym]["account"]["portfolio"] == "COMBO_AW_V5_24H"


