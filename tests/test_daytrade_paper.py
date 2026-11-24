"""Offline tests for the Day Trader M1 live-feed paper simulator."""
from __future__ import annotations

import importlib.util
import json
import socket
import stat
from pathlib import Path

import pytest

from trade_mcp.daytrade_paper import (
    DaytradePaperSession,
    evaluate_m1_direction,
    plan_paper_trade,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = {
    "trade_tick_size": 0.01,
    "trade_tick_value": 1.0,
    "volume_step": 0.01,
    "volume_min": 0.01,
    "volume_max": 10.0,
    "point": 0.01,
    "trade_stops_level": 5.0,
}


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "daytrade_m1_paper_window", ROOT / "scripts/daytrade_m1_paper_window.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _bars(closes: list[float], *, start: int = 1700000040, half: float = 0.25) -> list[list[float]]:
    rows = []
    for i, close in enumerate(closes):
        t = start + i * 60
        open_p = closes[i - 1] if i > 0 else close
        high = max(open_p, close) + half
        low = min(open_p, close) - half
        rows.append([t, open_p, high, low, close, 100.0])
    return rows


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("DNS forbidden"))


def test_evaluate_m1_direction_buy_sell_and_wait():
    # 3 range bars [2650.0..2651.0], 4th closed bar breaks above 2651.0, 5th forming bar excluded
    buy_bars = [
        [1700000040, 2650.4, 2650.9, 2650.1, 2650.5, 100.0],
        [1700000100, 2650.5, 2651.0, 2650.2, 2650.6, 110.0],
        [1700000160, 2650.6, 2650.9, 2650.0, 2650.4, 105.0],
        [1700000220, 2650.4, 2651.4, 2650.3, 2651.2, 140.0],
        [1700000280, 2651.2, 2651.3, 2648.0, 2648.1, 20.0],  # forming bar (ignored!)
    ]
    sig = evaluate_m1_direction(buy_bars, opening_range_bars=3)
    assert sig["action"] == "buy"
    assert sig["closed_bar_time"] == 1700000220
    assert sig["range_edge"] == 2650.0

    # Sell breakdown on 4th closed bar below 2650.0
    sell_bars = [
        [1700000040, 2650.4, 2650.9, 2650.1, 2650.5, 100.0],
        [1700000100, 2650.5, 2651.0, 2650.2, 2650.6, 110.0],
        [1700000160, 2650.6, 2650.9, 2650.0, 2650.4, 105.0],
        [1700000220, 2650.4, 2650.5, 2649.6, 2649.8, 140.0],
        [1700000280, 2649.8, 2655.0, 2649.7, 2654.0, 20.0],  # forming bar (ignored!)
    ]
    sig_sell = evaluate_m1_direction(sell_bars, opening_range_bars=3)
    assert sig_sell["action"] == "sell"
    assert sig_sell["range_edge"] == 2651.0

    # Inside range -> wait
    wait_bars = [
        *buy_bars[:3],
        [1700000220, 2650.4, 2650.8, 2650.2, 2650.5, 90.0],
        [1700000280, 2650.5, 2655.0, 2650.4, 2655.0, 20.0],
    ]
    assert evaluate_m1_direction(wait_bars, opening_range_bars=3)["action"] == "wait"


def test_evaluate_m1_direction_handles_gap_without_filling():
    # Gap before last 3 bars leaves fewer than 5 contiguous bars
    gapped = [
        [1700000040, 2650.0, 2651.0, 2649.5, 2650.5, 100.0],
        [1700000160, 2650.5, 2651.0, 2650.0, 2650.6, 100.0],  # 120s gap
        [1700000220, 2650.6, 2651.5, 2650.5, 2651.4, 120.0],
        [1700000280, 2651.4, 2651.5, 2651.0, 2651.3, 40.0],
    ]
    sig = evaluate_m1_direction(gapped, opening_range_bars=3)
    assert sig["action"] == "wait"
    assert sig["reason"] == "insufficient_contiguous_m1"


def test_plan_paper_trade_respects_budget_lot_cap_and_spread():
    # Normal gold buy: spread $0.20, stop_usd=$10, target_usd=$2, max_lot=0.10
    plan = plan_paper_trade(
        side="buy", bid=2650.00, ask=2650.20, range_edge=2649.50,
        spec=SPEC, stop_usd=10.0, target_usd=2.0, max_lot=0.10,
    )
    assert plan["ok"] and plan["paper"] and not plan["executable"] and not plan["execution_authorized"]
    assert plan["entry"] == 2650.20
    assert plan["volume"] == 0.10
    assert plan["planned_stop_usd"] <= 10.0
    assert plan["planned_target_usd"] >= 2.0
    assert plan["stop"] == 2649.20
    assert plan["target"] == 2650.40
    assert plan["entry_spread_usd"] == 2.0

    # Wide range ($2.00) scales lot down to 0.05 to keep planned risk <= $10
    wide = plan_paper_trade(
        side="sell", bid=2650.00, ask=2650.20, range_edge=2652.00,
        spec=SPEC, stop_usd=10.0, target_usd=5.0, max_lot=0.10,
    )
    assert wide["ok"]
    assert wide["volume"] == 0.05
    assert wide["planned_stop_usd"] <= 10.0
    assert wide["planned_target_usd"] >= 5.0
    assert wide["entry"] == 2650.00
    assert wide["target"] == 2649.00

    # Huge stop distance where even 0.01 lot exceeds $10 -> refused
    too_wide = plan_paper_trade(
        side="buy", bid=2650.00, ask=2650.20, range_edge=2635.00,
        spec=SPEC, stop_usd=10.0, target_usd=2.0, max_lot=0.10,
    )
    assert not too_wide["ok"] and too_wide["reason"] == "volume_below_minimum"

    # Excessive spread relative to stop distance -> refused
    wide_spread = plan_paper_trade(
        side="buy", bid=2650.00, ask=2650.60, range_edge=2649.90,
        spec=SPEC, stop_usd=10.0, target_usd=2.0, max_lot=0.10, max_spread_ratio=0.40,
    )
    assert not wide_spread["ok"] and wide_spread["reason"] == "spread_too_wide"


def test_session_lifecycle_target_hit_commission_and_dedup():
    session = DaytradePaperSession(
        symbol="XAUUSD_i", spec=SPEC, duration_seconds=600,
        target_usd=3.0, stop_usd=10.0, opening_range_bars=3,
        max_lot=0.10, commission_per_lot=5.0, started_at=1000.0,
    )
    bars = [
        [1700000040, 2650.4, 2650.9, 2650.1, 2650.5, 100.0],
        [1700000100, 2650.5, 2651.0, 2650.2, 2650.6, 110.0],
        [1700000160, 2650.6, 2650.9, 2650.0, 2650.4, 105.0],
        [1700000220, 2650.4, 2651.4, 2650.3, 2651.2, 140.0],
        [1700000280, 2651.2, 2651.3, 2651.1, 2651.2, 20.0],
    ]
    opened = session.on_bars(bars=bars, bid=2651.20, ask=2651.40, now=1010.0)
    assert opened["opened"] is True
    assert opened["trade"]["side"] == "buy"
    # Entry 2651.40 vs range_low 2650.00 -> $1.40/pt + $5/lot = $145/lot -> floor(10/145)=0.06 lot
    assert opened["trade"]["volume"] == 0.06
    assert opened["trade"]["commission_usd"] == 0.3

    # Same closed bar does not re-evaluate
    again = session.on_bars(bars=bars, bid=2651.20, ask=2651.40, now=1015.0)
    assert again["action"] == "already_evaluated"
    assert session.m1_bars_evaluated == 1

    # Not yet at target (+$3 net at 0.06 lot requires Bid >= 2651.40 + 3.30/6 = 2651.95)
    m1 = session.mark_quote(bid=2651.80, ask=2652.00, now=1020.0)
    assert m1["open"] and not m1["closed"]
    assert m1["unrealized_pnl_usd"] == pytest.approx(2.1, abs=1e-4)

    # Target hit at observed Bid 2652.00 -> gross +$3.60, commission $0.30 -> net +$3.30
    m2 = session.mark_quote(bid=2652.00, ask=2652.20, now=1030.0)
    assert m2["closed"] is True
    assert m2["trade"]["exit_reason"] == "target_hit"
    assert m2["trade"]["net_pnl_usd"] == pytest.approx(3.3, abs=1e-4)
    assert m2["trade"]["hold_seconds"] == 20.0

    summary = session.summary(ended_at=1600.0)
    assert summary["orders_submitted"] == 0
    assert summary["wins"] == 1 and summary["losses"] == 0
    assert summary["win_rate_pct"] == 100.0
    assert summary["realized_pnl_usd"] == pytest.approx(3.3, abs=1e-4)
    assert not summary["open_position_at_end"]


def test_session_three_loss_latch_gap_loss_and_open_at_end():
    session = DaytradePaperSession(
        symbol="XAUUSD_i", spec=SPEC, duration_seconds=600,
        target_usd=2.0, stop_usd=10.0, opening_range_bars=3,
        max_lot=0.10, max_consecutive_losses=3, started_at=1000.0,
    )
    t = 1700000040
    for i in range(3):
        b = [
            [t + 0, 2650.4, 2650.9, 2650.1, 2650.5, 100.0],
            [t + 60, 2650.5, 2651.0, 2650.2, 2650.6, 100.0],
            [t + 120, 2650.6, 2650.9, 2650.0, 2650.4, 100.0],
            [t + 180, 2650.4, 2651.4, 2650.3, 2651.2, 140.0],
            [t + 240, 2651.2, 2651.3, 2651.1, 2651.2, 20.0],
        ]
        ev = session.on_bars(bars=b, bid=2651.20, ask=2651.40, now=1010.0 + i * 60)
        assert ev["opened"] is True
        assert ev["trade"]["volume"] == 0.07
        # Gap drop to 2649.90 (-$1.50 from 2651.40 at 0.07 lot -> -$10.50 real gap loss, not fake -$10)
        m = session.mark_quote(bid=2649.90, ask=2650.10, now=1020.0 + i * 60)
        assert m["closed"] is True
        assert m["trade"]["exit_reason"] == "stop_hit"
        assert m["trade"]["net_pnl_usd"] == pytest.approx(-10.5, abs=1e-4)
        t += 300

    assert session.latched is True
    # 4th breakout is blocked by 3-loss latch
    b4 = [
        [t + 0, 2650.4, 2650.9, 2650.1, 2650.5, 100.0],
        [t + 60, 2650.5, 2651.0, 2650.2, 2650.6, 100.0],
        [t + 120, 2650.6, 2650.9, 2650.0, 2650.4, 100.0],
        [t + 180, 2650.4, 2651.4, 2650.3, 2651.2, 140.0],
        [t + 240, 2651.2, 2651.3, 2651.1, 2651.2, 20.0],
    ]
    blocked = session.on_bars(bars=b4, bid=2651.20, ask=2651.40, now=1300.0)
    assert blocked["opened"] is False and blocked["reason"] == "loss_latch_active"
    assert session.refusal_counts["loss_latch_active"] == 1

    # Separate session ending with an open position keeps it marked open honestly
    open_session = DaytradePaperSession(
        symbol="XAUUSD_i", spec=SPEC, duration_seconds=600,
        target_usd=2.0, stop_usd=10.0, started_at=1000.0,
    )
    assert open_session.on_bars(bars=b4, bid=2651.20, ask=2651.40, now=1010.0)["opened"]
    open_session.mark_quote(bid=2651.10, ask=2651.30, now=1590.0)
    s = open_session.summary(ended_at=1600.0)
    assert s["open_position_at_end"] is True
    assert s["open_position"]["closed_at_window_end"] is False
    # 0.07 lot * (2651.10 - 2651.40) * 100 = -$2.10 unrealized
    assert s["unrealized_pnl_usd"] == pytest.approx(-2.1, abs=1e-4)
    assert s["realized_pnl_usd"] == 0.0
    assert s["total_marked_pnl_usd"] == pytest.approx(-2.1, abs=1e-4)


def test_cli_mock_loop_and_private_log(tmp_path, monkeypatch, capsys):
    cli = _load_cli()
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    now = [0.0]

    def fake_clock():
        return now[0]

    def fake_sleep(dt):
        now[0] += dt

    feed = cli.MockFeed("XAUUSD_i")
    spec, _ = feed.connect()
    session = DaytradePaperSession(
        symbol="XAUUSD_i", spec=spec, duration_seconds=12,
        target_usd=2.0, stop_usd=10.0, opening_range_bars=3,
        started_at=1000.0,
    )
    summary = cli.run_window_loop(
        feed=feed, session=session, poll_seconds=2.0, bars_poll_seconds=2.0,
        clock=fake_clock, wall=lambda: 1000.0 + now[0], sleep=fake_sleep, quiet=True,
    )
    assert summary["ok"] and summary["paper"] and summary["orders_submitted"] == 0
    assert summary["trades_opened"] == 1 and summary["trades_closed"] == 1
    assert summary["wins"] == 1 and summary["realized_pnl_usd"] >= 2.0

    out_path = tmp_path / "logs" / "paper-smoke.json"
    cli._save_private_json(out_path, {"summary": summary, "events": session.events})
    assert stat.S_IMODE(out_path.stat().st_mode) == 0o600
    saved = json.loads(out_path.read_text(encoding="utf-8"))
    assert saved["summary"]["wins"] == 1
    assert len(saved["events"]) == 2


def test_sell_lifecycle_single_position_and_readonly_boundaries():
    session = DaytradePaperSession(
        symbol="XAUUSD_i", spec=SPEC, duration_seconds=600,
        target_usd=2.0, stop_usd=10.0, opening_range_bars=3,
        max_lot=0.10, started_at=1000.0,
    )
    sell_bars = [
        [1700000040, 2650.4, 2650.9, 2650.1, 2650.5, 100.0],
        [1700000100, 2650.5, 2651.0, 2650.2, 2650.6, 100.0],
        [1700000160, 2650.6, 2650.9, 2650.0, 2650.4, 100.0],
        [1700000220, 2650.4, 2650.5, 2649.6, 2649.8, 140.0],
        [1700000280, 2649.8, 2649.9, 2649.7, 2649.8, 20.0],
    ]
    opened = session.on_bars(bars=sell_bars, bid=2649.80, ask=2650.00, now=1010.0)
    assert opened["opened"] is True
    assert opened["trade"]["side"] == "sell"
    assert opened["trade"]["entry"] == 2649.80
    # Range high 2651.00 - entry 2649.80 = 1.20 -> floor(10/120)=0.08 lot
    assert opened["trade"]["volume"] == 0.08

    # While open, a second breakout bar is counted but refused as position_already_open
    next_bars = [
        *sell_bars[1:4],
        [1700000280, 2649.8, 2649.9, 2649.0, 2649.2, 150.0],
        [1700000340, 2649.2, 2649.3, 2649.1, 2649.2, 20.0],
    ]
    skipped = session.on_bars(bars=next_bars, bid=2649.60, ask=2649.80, now=1070.0)
    assert skipped["opened"] is False and skipped["reason"] == "position_already_open"

    # Sell marks on Ask: 2649.80 - 2649.50 = +$0.30 * 0.08 * 100 = +$2.40 >= $2.00 target
    closed = session.mark_quote(bid=2649.30, ask=2649.50, now=1080.0)
    assert closed["closed"] is True
    assert closed["trade"]["exit_reason"] == "target_hit"
    assert closed["trade"]["net_pnl_usd"] == pytest.approx(2.4, abs=1e-4)

    # ReadOnlyNativeFeed rejects non-loopback URLs and write tools
    cli = _load_cli()
    with pytest.raises(cli.Blocked):
        cli.ReadOnlyNativeFeed("https://example.com/mcp", "token_1234567890", "XAUUSD_i")
    feed = cli.ReadOnlyNativeFeed("http://127.0.0.1:22346/mcp", "token_1234567890", "XAUUSD_i")
    with pytest.raises(cli.Blocked):
        feed._call("trade_send_market_order", {})
    src_text = Path(ROOT / "src/trade_mcp/daytrade_paper.py").read_text(encoding="utf-8")
    assert "from .execution" not in src_text
    assert "propose_open" not in src_text and "trade_open" not in src_text and "trade_send_market_order" not in src_text

