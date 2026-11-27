"""Hermetic tests for AllWeatherPaperSuite and daytrade_all_weather_live.py (ORDERS=0)."""

from __future__ import annotations

import contextlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import daytrade_all_weather_live as aw_cli  # noqa: E402

from trade_mcp.all_weather_paper import AllWeatherPaperSuite  # noqa: E402
from trade_mcp.s1_dual_radar import make_synthetic_dual_session  # noqa: E402


def test_all_weather_suite_seed_and_account_accounting() -> None:
    """seed_m1_bars skips forming bar, warms CALM_M1_PRO_SESS, and tracks combined Balance/Equity/Profit."""
    suite = AllWeatherPaperSuite(initial_capital_usd=1000.0)
    t_now = float(datetime(2026, 11, 26, 14, 30, 20, tzinfo=timezone.utc).timestamp())
    cur_bar_start = float(int(t_now // 60) * 60)

    # 30 closed M1 bars + 1 currently forming bar (which must be ignored!)
    bars: list[list[float]] = []
    px = 3500.0
    for i in range(30, 0, -1):
        ts_bar = cur_bar_start - i * 60.0
        bars.append([ts_bar, px, px + 1.8, px - 0.5, px + 1.0, 100.0])
        px += 1.0
    bars.append([cur_bar_start, px, px + 5.0, px - 5.0, px + 2.0, 10.0])  # forming bar

    seeded = suite.seed_m1_bars(bars, server_now_ts=t_now, spread=0.36)
    assert seeded == 30
    assert len(suite.calm_engine._bars) == 30
    assert suite.account_summary()["orders_submitted"] == 0
    assert suite.account_summary()["balance_usd"] == 1000.0

    # Feed synthetic dual session and confirm accounting consistency
    ticks = make_synthetic_dual_session()
    last_ts, last_bid, last_ask = 0.0, 0.0, 0.0
    for ts, bid, ask, m1_ctx in ticks:
        suite.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        last_ts, last_bid, last_ask = ts, bid, ask
    suite.close_all_open(ts=last_ts, bid=last_bid, ask=last_ask, reason="eof_test")

    acct = suite.account_summary(last_bid, last_ask)
    assert acct["paper_only"] is True
    assert acct["orders_submitted"] == 0
    assert acct["trades_count"] >= 1
    assert acct["balance_usd"] == round(1000.0 + acct["realized_pnl_usd"], 2)
    assert acct["equity_usd"] == acct["balance_usd"]
    assert acct["net_profit_usd"] == acct["realized_pnl_usd"]


def test_all_weather_cli_mock_replay_and_live_smoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Verify --mock, --replay, and stubbed live monitor produce MT5 panel & 0 broker orders."""
    monkeypatch.setattr(aw_cli, "ROOT", tmp_path)
    (tmp_path / "logs").mkdir(parents=True)
    (tmp_path / "reports" / "paper_live").mkdir(parents=True)

    # 1) --mock + --report-file
    rep_mock = tmp_path / "reports" / "paper_live" / "aw_mock.json"
    rc1 = aw_cli.main(["--mock", "--capital", "1000", "--report-file", str(rep_mock)])
    assert rc1 == 0
    out1 = capsys.readouterr().out
    assert "ALL-WEATHER PAPER MOCK SUMMARY" in out1
    assert "PROFIT:" in out1
    data1 = json.loads(rep_mock.read_text(encoding="utf-8"))
    assert data1["schema"] == "all_weather_pro_m1_paper_v1"
    assert data1["orders_submitted"] == 0
    assert data1["account"]["portfolio"] == "COMBO_AW_V3_24H"
    assert data1["account"]["trades_count"] >= 1

    # 2) --replay on two JSONL files (verifies multi-file cumulative balance)
    ticks = make_synthetic_dual_session()
    f1 = tmp_path / "logs" / "sample1.jsonl"
    with f1.open("w", encoding="utf-8") as fh:
        for ts, bid, ask, m1_ctx in ticks:
            fh.write(json.dumps({"ts": ts, "bid": bid, "ask": ask, "m1": m1_ctx}) + "\n")
    rc2 = aw_cli.main(["--replay", str(f1), "--capital", "1000"])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "ALL-WEATHER PAPER REPLAY" in out2
    assert "SHOCK10_DYN_RUN" in out2
    assert "VSNAP6_24H" in out2
    assert "MACRO_WAVE_DYN" in out2
    assert "ZSCORE_MR_24H" in out2

    # 3) Live stub smoke test with M1 warmup + MT5 panel + atomic report
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
                {"bid": 3550.00, "ask": 3550.36},
            )

        def fetch_quote(self) -> dict[str, float]:
            self._n += 1
            return {"bid": round(3550.00 + 0.04 * self._n, 2), "ask": round(3550.36 + 0.04 * self._n, 2)}

    t_srv = float(datetime(2026, 11, 26, 14, 25, 10, tzinfo=timezone.utc).timestamp())
    bar_start = float(int(t_srv // 60) * 60)
    stub_bars = [[bar_start - i * 60.0, 3540.0 + 0.2 * (30 - i), 3541.5 + 0.2 * (30 - i), 3539.5 + 0.2 * (30 - i), 3540.5 + 0.2 * (30 - i), 50.0] for i in range(30, 0, -1)]

    monkeypatch.setattr(aw_cli, "load_dotenv", lambda _p: None)
    monkeypatch.setattr(aw_cli.Config, "from_env", classmethod(lambda cls: _StubCfg()))
    monkeypatch.setattr(aw_cli, "loopback_native", lambda: contextlib.nullcontext())
    monkeypatch.setattr(aw_cli, "ReadOnlyNativeFeed", _StubFeed)
    monkeypatch.setattr(aw_cli, "_server_now", lambda _feed: (t_srv, datetime(2026, 11, 26, 14, 25, 10)))
    monkeypatch.setattr(aw_cli, "_fetch_m1_bars", lambda _feed, now_label, limit=200: stub_bars)

    # Omit --report-file to verify live mode auto-writes reports/paper_live/all_weather_live.json
    default_rep = tmp_path / "reports" / "paper_live" / "all_weather_live.json"
    rc3 = aw_cli.main(
        [
            "--symbol",
            "XAUUSD_i",
            "--capital",
            "1000",
            "--calm-profile",
            "CALM_M1_PRO_150",
            "--poll-seconds",
            "0.05",
            "--status-seconds",
            "0.1",
            "--minutes",
            "0.004",
        ]
    )
    assert rc3 == 0
    out3 = capsys.readouterr().out
    assert "M1 Warmup=30 bars READY" in out3
    assert "Balance:" in out3
    assert "PROFIT:" in out3
    assert "Status:" in out3
    live_data = json.loads(default_rep.read_text(encoding="utf-8"))
    assert live_data["status"] == "stopped"
    assert live_data["orders_submitted"] == 0
    assert live_data["account"]["seeded_m1_bars"] == 30
    assert live_data["market_status"]["calm_profile"] == "CALM_M1_PRO_150"
    assert "headline" in live_data["market_status"]
    assert live_data["live_state"]["ticks_processed"] >= 1


def test_inspect_live_sessions_audits_windows_and_tick_chart(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """inspect_live_sessions summarizes reports, trade logs, and 15m tick chart timeline + --save."""
    import inspect_live_sessions as insp

    monkeypatch.setattr(insp, "ROOT", tmp_path)
    (tmp_path / "logs").mkdir(parents=True)
    (tmp_path / "reports" / "paper_live").mkdir(parents=True)

    # Create sample live report JSON
    (tmp_path / "reports" / "paper_live" / "dual_live.json").write_text(
        json.dumps(
            {
                "schema": "s1_dual_radar_live_v1",
                "status": "running",
                "orders_submitted": 0,
                "started_utc": "2026-11-26T09:00:00+00:00",
                "updated_utc": "2026-11-26T12:00:00+00:00",
                "elapsed_minutes": 180.0,
                "live_state": {"ticks_processed": 36000, "reconnect_count": 0, "consecutive_errors": 0},
                "engines": {"SHOCK10_DYN_RUN": {"waves_count": 0, "wins": 0, "losses": 0, "realized_pnl_usd": 0.0}},
            }
        ),
        encoding="utf-8",
    )

    # Create sample trade log + tick JSONL
    (tmp_path / "logs" / "all_weather_paper_trades.jsonl").write_text(
        json.dumps(
            {
                "closed_utc": "2026-11-26T10:49:31+00:00",
                "engine_label": "CALM_M1_PRO",
                "side": "buy",
                "total_volume": 0.03,
                "avg_entry": 3571.44,
                "exit_price": 3573.47,
                "net_pnl_usd": 6.08,
                "exit_reason": "profit_floor_hit",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ticks = make_synthetic_dual_session()
    tfile = tmp_path / "logs" / "s1_ticks_XAUUSD_i_20261126.jsonl"
    with tfile.open("w", encoding="utf-8") as fh:
        for ts, bid, ask, m1_ctx in ticks:
            fh.write(json.dumps({"ts": ts, "bid": bid, "ask": ask, "m1": m1_ctx}) + "\n")

    rc = insp.main(["--save"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1) LIVE PAPER WINDOWS" in out
    assert "2) RECORDED TRADE & ALERT LOGS" in out
    assert "3) TICK-BY-SECOND CHART TIMELINE" in out
    assert "4) LAST 240 MIN MICROSCOPE" in out
    assert "SAVED_COMPACT_AUDIT=" in out
    saved = json.loads((tmp_path / "reports" / "paper_live" / "live_audit_summary.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "live_audit_summary_v1"
    assert saved["orders_submitted"] == 0
    assert len(saved["live_reports"]) == 1
    assert len(saved["tick_charts"]) == 1
    assert len(saved["tick_charts"][0]["buckets_15m"]) >= 1
    assert saved["recent_microscope"] is not None
    assert len(saved["recent_microscope"]["minutes"]) >= 1


def test_all_weather_risk_multiplier_scales_lots_and_pnl_cleanly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """risk_multiplier linearly scales lots & PnL across all 4 engines while keeping identical signals & PF."""
    import daytrade_all_weather_live as aw_live

    with pytest.raises(ValueError):
        AllWeatherPaperSuite(risk_multiplier=0.0)
    with pytest.raises(ValueError):
        AllWeatherPaperSuite(risk_multiplier=6.0)

    ticks = make_synthetic_dual_session()
    s1 = AllWeatherPaperSuite(initial_capital_usd=1000.0, risk_multiplier=1.0)
    s2 = AllWeatherPaperSuite(initial_capital_usd=1000.0, risk_multiplier=2.0)

    # Verify all 4 engines scaled their lot configs by 2x
    assert s2.shock_engine.cfg.leg_lot == round(s1.shock_engine.cfg.leg_lot * 2.0, 2)
    assert s2.shock_engine.cfg.dyn_max_leg_lot == round(s1.shock_engine.cfg.dyn_max_leg_lot * 2.0, 2)
    assert s2.fast_shock_engine is not None and s1.fast_shock_engine is not None
    assert s2.fast_shock_engine.cfg.leg_lot == round(s1.fast_shock_engine.cfg.leg_lot * 2.0, 2)
    assert s2.macro_engine.cfg.leg_lot == round(s1.macro_engine.cfg.leg_lot * 2.0, 2)
    assert s2.calm_engine.cfg.leg_lot == round(s1.calm_engine.cfg.leg_lot * 2.0, 2)
    assert s2.calm_engine.cfg.max_lot == round(s1.calm_engine.cfg.max_lot * 2.0, 2)

    for s in (s1, s2):
        for ts, bid, ask, m1_ctx in ticks:
            s.on_tick(ts=ts, bid=bid, ask=ask, m1_context=m1_ctx)
        s.close_all_open(ts=ticks[-1][0], bid=ticks[-1][1], ask=ticks[-1][2], reason="eof")

    a1 = s1.account_summary(ticks[-1][1], ticks[-1][2])
    a2 = s2.account_summary(ticks[-1][1], ticks[-1][2])
    assert a1["trades_count"] == a2["trades_count"] > 0
    assert a1["wins"] == a2["wins"]
    assert a1["losses"] == a2["losses"]
    assert a2["risk_multiplier"] == 2.0
    ratio = a2["net_profit_usd"] / a1["net_profit_usd"]
    assert 1.99 <= ratio <= 2.01

    rc = aw_live.main(["--mock", "--risk-mult", "2.0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "RiskMult: 2x" in out


def test_multi_symbol_wave_scanner_and_recorder_mock(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify Alpari wave-symbol discovery, ATR/Spread grading, and --mock CLI (ORDERS=0)."""
    import record_multi_ticks_live as rml

    rows = [
        {"name": "XAUUSD_i", "bid": 3570.0, "ask": 3570.4, "digits": 2, "point": 0.01, "visible": True},
        {"name": "USTEC", "bid": 20980.0, "ask": 20983.0, "digits": 1, "point": 0.1, "visible": False},
        {"name": "USTEC_i", "bid": 20980.0, "ask": 20981.5, "digits": 1, "point": 0.1, "visible": True},
        {"name": "GBPJPY_i", "bid": 195.100, "ask": 195.120, "digits": 3, "point": 0.001, "visible": True},
        {"name": "XAGUSD_i", "bid": 31.400, "ask": 31.430, "digits": 3, "point": 0.001, "visible": False},
    ]
    picked = rml.discover_wave_symbols(rows, include_gold=False, max_symbols=6)
    syms = [p["symbol"] for p in picked]
    assert "USTEC_i" in syms  # prefers Alpari _i suffix
    assert "GBPJPY_i" in syms
    assert "XAGUSD_i" in syms
    assert "XAUUSD_i" not in syms

    bars = [[float(1700000000 + i * 60), 100.0, 102.0, 99.0, 101.0, 10.0] for i in range(25)]
    wm = rml._compute_m1_wave_metrics(bars, spread_price=0.40)
    assert wm["atr_m1"] == 3.0
    assert wm["atr_to_spread"] == 7.5
    assert wm["wave_grade"] == "A_EXCELLENT"

    rc = rml.main(["--mock"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ORDERS=0" in out
    assert "USTEC_i" in out


def test_audit_and_fix_gold_bugs_mock(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify day-by-day deduplicated Gold bug-fix audit lab runs cleanly in --mock and --phase2 modes (ORDERS=0)."""
    import audit_and_fix_gold_bugs as ab

    rc = ab.main(["--mock"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "GOLD DAY-BY-DAY DEDUPLICATED BUG-FIX AUDIT" in out
    assert "SECTION 1" in out
    assert "SECTION 2" in out
    assert "SECTION 3" in out
    assert "SECTION 4" in out
    assert "AW_V3_SHIPPED_BASE" in out
    assert "VSNAP6_FIX12_BREATHE" in out
    assert "CALM_M5_TREND_24H" in out

    rc2 = ab.main(["--mock", "--phase2"])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "VSNAP6_WIDE_ADD22" in out2
    assert "MACRO_24H_TREND10" in out2
    assert "AW_V4_ADD22_M5_M60" in out2
    assert "PER-ENGINE x PER-DAY BREAKDOWN" in out2
    assert "COMPLETE LAST-NIGHT TRADE-BY-TRADE AUDIT" in out2

    rc3 = ab.main(["--mock", "--phase3"])
    assert rc3 == 0
    out3 = capsys.readouterr().out
    assert "M5_DONCHIAN12_L02" in out3
    assert "AW_V4_BASE_E42_02" in out3
    assert "AW_V4_BASE_DC12" in out3

    rc4 = ab.main(["--mock", "--phase4"])
    assert rc4 == 0
    out4 = capsys.readouterr().out
    assert "VSNAP6_PARTIAL_26" in out4
    assert "DC_SAFE_16_M60" in out4
    assert "AW_V4_PARTIAL_26" in out4




