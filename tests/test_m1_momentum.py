"""Offline hermetic tests for M1 momentum/acceleration + multi-timeframe replay."""
from __future__ import annotations

import importlib.util
import json
import socket
import stat
from pathlib import Path

import pytest

from trade_mcp.m1_momentum import (
    DEFAULT_VALIDATION_CANDIDATES,
    adx_series,
    atr_series,
    build_htf_index,
    compute_htf_coverage,
    detect_m1_impulse,
    htf_allows_signal,
    lookup_htf,
    replay_m1_momentum,
    replay_m1_momentum_matrix,
    split_m1_weekly_windows,
    summarize_sessions_utc,
    validate_m1_momentum_candidates,
)

ROOT = Path(__file__).resolve().parents[1]
ALPARI_SPEC = {
    "trade_tick_size": 0.01,
    "trade_tick_value": 0.1,
    "trade_contract_size": 100.0,
    "currency_profit": "USD",
    "trade_calc_mode": "CFD Leverage",
    "digits": 2,
    "volume_step": 0.01,
    "volume_min": 0.01,
    "volume_max": 50.0,
    "point": 0.01,
    "trade_stops_level": 0.0,
}


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "daytrade_m1_momentum_replay", ROOT / "scripts/daytrade_m1_momentum_replay.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("DNS forbidden"))


def test_htf_index_is_strictly_causal_zero_lookahead():
    # Two M5 bars: [1000..1300] and [1300..1600]
    m5 = [
        [1000 + i * 300, 3600 + i, 3601.5 + i, 3599.5 + i, 3601 + i, 100.0]
        for i in range(40)
    ]
    times, ctxs = build_htf_index(m5, period_seconds=300)
    # Bar 0 opens at 1000 and closes at 1300 -> NOT visible at 1299, visible at 1300
    assert lookup_htf(times, ctxs, 1299) is None
    c0 = lookup_htf(times, ctxs, 1300)
    assert c0 is not None and c0.close_time == 1300
    # At 1540 (inside bar 1), bar 1 (closes 1600) must NOT be visible yet!
    c_mid = lookup_htf(times, ctxs, 1540)
    assert c_mid is not None and c_mid.close_time == 1300
    # Strong steady uptrend -> final ADX is high and bias is buy
    adx = adx_series(m5, 14)
    assert adx[-1] is not None and adx[-1] > 25.0
    assert ctxs[-1].bias == "buy"


def test_detect_m1_impulse_modes_anti_climax_and_spread_filter():
    base = 1700000040
    # 16 quiet bars with ~$1.00 range
    seg = [[float(base + i * 60), 3600.0, 3600.5, 3599.5, 3600.1, 100.0] for i in range(16)]
    # Bar 16: clean bullish acceleration candle (range $2.00 = 2x ATR, body $1.70 = 85%, close near high)
    seg.append([float(base + 16 * 60), 3600.1, 3602.0, 3600.0, 3601.8, 250.0])
    # Bar 17: opening bar (ignored by detector — zero lookahead)
    seg.append([float(base + 17 * 60), 3601.8, 3602.2, 3601.5, 3602.0, 100.0])
    atr = atr_series(seg, 14)

    sig = detect_m1_impulse(seg, atr, 17, entry_mode="impulse_close", spread_price=0.34)
    assert sig is not None and sig["side"] == "buy"
    assert sig["limit_trigger_bid"] is None

    # Pullback mode produces a lower Bid limit inside the impulse bar body
    sig_pb = detect_m1_impulse(seg, atr, 17, entry_mode="impulse_pullback", spread_price=0.34)
    assert sig_pb is not None and sig_pb["limit_trigger_bid"] < 3601.8

    # Excessive spread relative to bar range is rejected
    assert detect_m1_impulse(seg, atr, 17, entry_mode="impulse_close", spread_price=0.80) is None

    # Overextended climax bar (> 3x ATR) is rejected to avoid chasing blow-off tops
    climax_seg = list(seg)
    climax_seg[16] = [float(base + 16 * 60), 3600.1, 3606.0, 3600.0, 3605.8, 500.0]
    atr_c = atr_series(climax_seg, 14)
    assert detect_m1_impulse(climax_seg, atr_c, 17, entry_mode="impulse_close", spread_price=0.34) is None


def test_htf_filter_blocks_chop_and_misaligned_trend():
    from trade_mcp.m1_momentum import HTFContext

    chop_m5 = HTFContext(1000, 3620.0, 3619.0, 3618.0, adx=8.75, atr=4.7, donchian_pos=0.5, bias="buy")
    bull_m15 = HTFContext(1000, 3620.0, 3618.0, 3615.0, adx=22.0, atr=9.0, donchian_pos=0.6, bias="buy")
    bear_h1 = HTFContext(1000, 3620.0, 3625.0, 3640.0, adx=20.0, atr=18.0, donchian_pos=0.3, bias="sell")

    ok, reason = htf_allows_signal(side="buy", htf_filter="m5_m15", m5=chop_m5, m15=bull_m15, h1=bear_h1)
    assert not ok and reason == "htf_m5_low_adx_chop"

    trend_m5 = HTFContext(1000, 3620.0, 3619.0, 3617.0, adx=24.0, atr=5.0, donchian_pos=0.7, bias="buy")
    ok2, _ = htf_allows_signal(side="buy", htf_filter="m5_m15", m5=trend_m5, m15=bull_m15, h1=bear_h1)
    assert ok2 is True

    # Adding H1 filter rejects buying against bearish H1 EMA20 < EMA50
    ok3, reason3 = htf_allows_signal(side="buy", htf_filter="m5_m15_h1", m5=trend_m5, m15=bull_m15, h1=bear_h1)
    assert not ok3 and reason3 == "htf_h1_bias_mismatch"


def test_uncapped_runner_deceleration_exit_and_fast_invalidation_cut():
    base = 1700000040
    # 16 calm bars (~$1 range)
    bars = [[float(base + i * 60), 3600.0, 3600.5, 3599.5, 3600.0, 100.0] for i in range(16)]
    # Bar 16: bullish impulse ($2.00 range) -> triggers BUY at Bar 17 open (3601.8 + 0.34 = 3602.14)
    bars.append([float(base + 16 * 60), 3600.1, 3602.0, 3600.0, 3601.8, 250.0])
    # Bars 17..21: huge runaway wave (+$14!) with strong green bars, NO fixed $2 cap!
    px = 3601.8
    for k in range(5):
        nxt = round(px + 2.8, 2)
        bars.append([float(base + (17 + k) * 60), px, nxt + 0.1, px - 0.1, nxt, 220.0])
        px = nxt
    # Bar 22: bearish deceleration candle -> should close with big uncapped win (>> $2!)
    bars.append([float(base + 22 * 60), px, px + 0.3, px - 1.2, px - 0.8, 180.0])
    bars.append([float(base + 23 * 60), px - 0.8, px - 0.5, px - 2.0, px - 1.5, 120.0])

    res = replay_m1_momentum(
        bars, spec=ALPARI_SPEC, spread_price=0.34, htf_filter="none",
        entry_mode="impulse_close", exit_mode="decel_and_trail",
        stop_usd=5.0, max_lot=0.01, max_legs=1,
    )
    assert res["orders_submitted"] == 0
    assert res["pricing_basis"] == "contract_size" and res["effective_multiplier"] == 100.0
    assert res["wins"] >= 1
    assert res["max_single_win_usd"] > 10.0  # Uncapped runner well above old $2 ceiling!
    assert res["exit_reasons"].get("momentum_deceleration", 0) >= 1

    # Fast invalidation cut test: impulse followed immediately by bearish reversal below impulse midpoint
    fail_bars = [[float(base + i * 60), 3600.0, 3600.5, 3599.5, 3600.0, 100.0] for i in range(17)]
    fail_bars.append([float(base + 17 * 60), 3600.1, 3602.0, 3600.0, 3601.8, 250.0])
    # Next bar reverses sharply to 3600.4 (below impulse mid 3600.95, still above -$5 stop at ~3597.14)
    fail_bars.append([float(base + 18 * 60), 3601.8, 3601.9, 3600.3, 3600.4, 240.0])
    fail_bars.append([float(base + 19 * 60), 3600.4, 3600.5, 3590.0, 3591.0, 300.0])
    res_cut = replay_m1_momentum(
        fail_bars, spec=ALPARI_SPEC, spread_price=0.34, htf_filter="none",
        entry_mode="impulse_close", exit_mode="decel_and_trail",
        stop_usd=5.0, max_lot=0.01, fast_invalidation_cut=True,
    )
    assert res_cut["exit_reasons"].get("fast_invalidation_cut", 0) == 1
    assert -3.0 < res_cut["realized_pnl_usd"] < 0.0  # Cut early well before -$5 stop!


def test_cli_mock_multiday_replay_and_no_execution_imports(tmp_path, monkeypatch, capsys):
    cli = _load_cli()
    paper_spec = importlib.util.spec_from_file_location(
        "daytrade_m1_paper_window", ROOT / "scripts/daytrade_m1_paper_window.py"
    )
    paper_cli = importlib.util.module_from_spec(paper_spec)
    assert paper_spec and paper_spec.loader
    paper_spec.loader.exec_module(paper_cli)

    monkeypatch.setattr(paper_cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "_save_private_json", paper_cli._save_private_json)
    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_PAPER_TRADING", "false")

    rc = cli.main(["--mock", "--out", "logs/m1-momentum-smoke.json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["paper"] and payload["orders_submitted"] == 0
    assert payload["parameters"]["pricing_basis"] == "contract_size"
    assert payload["parameters"]["effective_multiplier"] == 100.0
    assert payload["parameters"]["take_profit_ceiling"] is None
    assert len(payload["matrix"]) == 10
    assert payload["history"]["m1_closed_bars"] == 720
    out_file = tmp_path / "logs" / "m1-momentum-smoke.json"
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600

    # Ensure zero execution imports in m1_momentum.py
    src_text = (ROOT / "src/trade_mcp/m1_momentum.py").read_text(encoding="utf-8")
    assert "from .execution" not in src_text
    assert "propose_open" not in src_text and "trade_send_market_order" not in src_text

    # Sanity-check matrix helper directly with profit-only pyramiding
    spec, quote, m1, m5, m15, h1 = cli._mock_multiday()
    rows = replay_m1_momentum_matrix(
        m1, spec=spec, spread_price=quote["ask"] - quote["bid"],
        m5_bars=m5, m15_bars=m15, h1_bars=h1, stop_usd=5.0, max_lot=0.01,
    )
    assert len(rows) == 10
    assert any(r["max_legs"] == 2 for r in rows)


def _load_validate_cli():
    spec = importlib.util.spec_from_file_location(
        "daytrade_m1_momentum_validate", ROOT / "scripts/daytrade_m1_momentum_validate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_pullback_or_2bar_causal_fallback_when_impulse_runs_away():
    base = 1700000040
    # 16 calm bars (~$1 range)
    bars = [[float(base + i * 60), 3600.0, 3600.5, 3599.5, 3600.0, 100.0] for i in range(16)]
    # Bar 16: bullish impulse ($2.00 range, body $1.70 -> pullback trigger at 3601.8 - 0.51 = 3601.29)
    bars.append([float(base + 16 * 60), 3600.1, 3602.0, 3600.0, 3601.8, 250.0])
    # Bar 17: runs away upward WITHOUT pulling back to 3601.29 (low is 3601.70) -> pure pullback misses it!
    bars.append([float(base + 17 * 60), 3601.8, 3603.4, 3601.7, 3603.2, 260.0])
    # Bar 18..20: continues up then decelerates on bar 20 -> pullback_or_2bar catches bar 18 open via 2-bar fallback!
    bars.append([float(base + 18 * 60), 3603.2, 3606.0, 3603.1, 3605.8, 240.0])
    bars.append([float(base + 19 * 60), 3605.8, 3608.5, 3605.7, 3608.2, 240.0])
    bars.append([float(base + 20 * 60), 3608.2, 3608.4, 3607.0, 3607.2, 200.0])

    pb_only = replay_m1_momentum(
        bars, spec=ALPARI_SPEC, spread_price=0.34, htf_filter="none",
        entry_mode="impulse_pullback", exit_mode="decel_and_trail",
        stop_usd=5.0, max_lot=0.01,
    )
    hybrid = replay_m1_momentum(
        bars, spec=ALPARI_SPEC, spread_price=0.34, htf_filter="none",
        entry_mode="pullback_or_2bar", exit_mode="decel_and_trail",
        stop_usd=5.0, max_lot=0.01,
    )
    # Bar 17 missed the 30% pullback; hybrid catches the runaway via 2-bar fallback on bar 18 open!
    assert hybrid["trades_closed"] >= 1
    assert hybrid["realized_pnl_usd"] > pb_only["realized_pnl_usd"]
    assert hybrid["refusal_counts"].get("pullback_unfilled", 0) >= 1


def test_weekly_splitter_htf_coverage_and_validation_gates():
    cli = _load_validate_cli()
    spec, quote, m1, m5, m15, h1 = cli._mock_multiweek()

    windows = split_m1_weekly_windows(m1, window_days=7, min_bars_per_window=500)
    assert len(windows) == 4
    assert [w["week"] for w in windows] == ["W1", "W2", "W3", "W4"]
    assert windows[0]["start_epoch"] < windows[-1]["start_epoch"]

    htf_indices = (
        build_htf_index(m5, period_seconds=300, ema_fast=9, ema_slow=21),
        build_htf_index(m15, period_seconds=900, ema_fast=9, ema_slow=21),
        build_htf_index(h1, period_seconds=3600, ema_fast=20, ema_slow=50),
    )
    cov = compute_htf_coverage(m1, htf_indices=htf_indices)
    assert cov["m5_pct"] == 100.0 and cov["m15_pct"] == 100.0 and cov["h1_pct"] == 100.0

    # Session summary sanity check
    sess = summarize_sessions_utc([
        {"opened_at": 1790035200 + 8 * 3600, "net_pnl_usd": 4.0},
        {"opened_at": 1790035200 + 13 * 3600, "net_pnl_usd": -2.0},
    ])
    assert sess["london_07_12"]["trades"] == 1 and sess["london_07_12"]["pnl_usd"] == 4.0
    assert sess["ny_overlap_12_16"]["trades"] == 1 and sess["ny_overlap_12_16"]["pnl_usd"] == -2.0

    val = validate_m1_momentum_candidates(
        m1, spec=spec, spread_price=round(quote["ask"] - quote["bid"], 2),
        m5_bars=m5, m15_bars=m15, h1_bars=h1, max_lot=0.02,
    )
    assert val["ok"] and val["orders_submitted"] == 0
    assert len(val["candidates"]) == len(DEFAULT_VALIDATION_CANDIDATES) == 8
    c0 = val["candidates"][0]
    assert c0["weekly_walkforward"]["total_weeks"] == 4
    assert set(c0["cost_stress"].keys()) == {
        "base_live_spread", "stress_spread_050", "stress_spread_050_comm5", "stress_spread_070_comm7"
    }
    assert len(c0["adx_sensitivity"]["sweeps"]) == 4
    assert "overall_pass" in c0 and "gates" in c0
    assert val["verdict"]["status"] in {
        "PASS_READY_FOR_BOUNDED_LIVE_PAPER",
        "FAIL_DO_NOT_PROMOTE_TO_DEMO_OR_LIVE",
    }


def test_validate_cli_mock_4week_and_no_execution_imports(tmp_path, monkeypatch, capsys):
    cli = _load_validate_cli()
    paper_spec = importlib.util.spec_from_file_location(
        "daytrade_m1_paper_window", ROOT / "scripts/daytrade_m1_paper_window.py"
    )
    paper_cli = importlib.util.module_from_spec(paper_spec)
    assert paper_spec and paper_spec.loader
    paper_spec.loader.exec_module(paper_cli)

    monkeypatch.setattr(paper_cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "_save_private_json", paper_cli._save_private_json)
    monkeypatch.setenv("TRADE_MCP_ALLOW_TRADING", "false")
    monkeypatch.setenv("TRADE_MCP_PAPER_TRADING", "false")

    rc = cli.main(["--mock", "--out", "logs/m1-validate-smoke.json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["paper"] and payload["orders_submitted"] == 0
    assert payload["parameters"]["pricing_basis"] == "contract_size"
    assert payload["parameters"]["effective_multiplier"] == 100.0
    assert payload["parameters"]["max_legs"] == 1
    assert payload["history"]["htf_coverage_pct"]["h1_pct"] == 100.0
    assert len(payload["history"]["weekly_windows"]) == 4
    assert len(payload["candidates"]) == 8
    out_file = tmp_path / "logs" / "m1-validate-smoke.json"
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600

    script_text = (ROOT / "scripts/daytrade_m1_momentum_validate.py").read_text(encoding="utf-8")
    assert "execution" not in script_text.replace("execution_authorized", "")
    assert "propose_open" not in script_text and "trade_send_market_order" not in script_text


def test_replay_handles_over_10k_m1_bars_for_30day_validation():
    base = 1790000040
    # 11,000 contiguous M1 bars (> daytrade.m1_rows 10,000 cap)
    bars = [
        [float(base + i * 60), 3600.0, 3600.6, 3599.4, 3600.1, 100.0]
        for i in range(11_000)
    ]
    res = replay_m1_momentum(
        bars, spec=ALPARI_SPEC, spread_price=0.34, htf_filter="none",
        entry_mode="impulse_close", exit_mode="decel_and_trail",
        stop_usd=5.0, max_lot=0.01,
    )
    assert res["ok"] and res["orders_submitted"] == 0


