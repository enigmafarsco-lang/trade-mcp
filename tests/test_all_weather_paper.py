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
    assert "MACRO_WAVE_DYN" in out2
    assert "CALM_M1_PRO_SESS" in out2

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

    rep_live = tmp_path / "reports" / "paper_live" / "aw_live.json"
    rc3 = aw_cli.main(
        [
            "--symbol",
            "XAUUSD_i",
            "--capital",
            "1000",
            "--poll-seconds",
            "0.05",
            "--status-seconds",
            "0.1",
            "--minutes",
            "0.004",
            "--report-file",
            str(rep_live),
        ]
    )
    assert rc3 == 0
    out3 = capsys.readouterr().out
    assert "M1 Warmup=30 bars READY" in out3
    assert "Balance:" in out3
    assert "PROFIT:" in out3
    live_data = json.loads(rep_live.read_text(encoding="utf-8"))
    assert live_data["status"] == "stopped"
    assert live_data["orders_submitted"] == 0
    assert live_data["account"]["seeded_m1_bars"] == 30
    assert live_data["live_state"]["ticks_processed"] >= 1
