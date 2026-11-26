"""Hermetic tests for Fast-Snap / V-Reversal / Acceleration causal engines and lab CLI (ORDERS=0)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from trade_mcp.fast_snap_engines import FastSnapEngine, fast_snap_profiles  # noqa: E402


def _make_ny_open_v_spike_ticks() -> list[tuple[float, float, float]]:
    """Faithful 1s synthetic tape matching a real NY-open box + $12.37 V-top crash (spread=$0.40)."""
    # Start at 2026-11-26 14:35:00 SRV (active London/NY hour 14..15)
    t0 = 1795703700.0  # 2026-11-26T14:35:00Z
    ticks: list[tuple[float, float, float]] = []

    def add_seg(start_px: float, end_px: float, dur_sec: int) -> None:
        nonlocal t0
        for i in range(1, dur_sec + 1):
            frac = i / max(1, dur_sec)
            mid = start_px + (end_px - start_px) * frac
            ticks.append((t0 + i, round(mid - 0.20, 2), round(mid + 0.20, 2)))
        t0 += dur_sec

    # 14:35 -> 14:55: calm $8 box (3565 -> 3574.90 -> 3564.50 -> 3566.00) to build 20m prior hi/lo
    add_seg(3566.00, 3574.90, 420)
    add_seg(3574.90, 3564.50, 480)
    add_seg(3564.50, 3566.00, 300)

    # 14:55 -> 15:02:00: fast rally up to 3575.46 (sweeping 3574.90 high by $0.56!)
    add_seg(3566.00, 3571.50, 240)
    add_seg(3571.50, 3575.46, 180)

    # 15:02:00 -> 15:03:05: SHARP V-TOP CRASH (-$9.60 in 65 seconds!)
    add_seg(3575.46, 3572.30, 25)  # sharp -$3.16 snap off the swept 3575.46 high
    add_seg(3572.30, 3565.80, 40)  # acceleration crash (-$9.66 total in 65s)

    # 15:03:05 -> 15:04:40: tiny pause then final leg down to 3563.09
    add_seg(3565.80, 3567.20, 15)
    add_seg(3567.20, 3563.09, 60)

    # 15:04:40 -> 15:06:00: bounce to trigger trailing / ratchet lock
    add_seg(3563.09, 3566.50, 80)
    return ticks


def test_fast_snap_engines_catch_sharp_v_top_crash_causally() -> None:
    ticks = _make_ny_open_v_spike_ticks()
    by_name = dict(fast_snap_profiles())
    results = {}
    for name, cfg in by_name.items():
        eng = FastSnapEngine(cfg=cfg)
        for ts, bid, ask in ticks:
            eng.on_tick(ts=ts, bid=bid, ask=ask)
        eng.force_close_open(ts=ticks[-1][0], bid=ticks[-1][1], ask=ticks[-1][2])
        results[name] = eng.summary()

    # The 3 primary sharp-spike / V-reversal hunters MUST fire and profit on the -$12.37 crash
    for key in ("VSNAP_30S_SESS", "VSNAP_SHOCK_6", "VTOP_SWEEP_SNAP", "ACCEL_SCALP_15S"):
        st = results[key]
        assert st["waves_count"] >= 1, f"{key} failed to catch the sharp V-top crash"
        assert st["realized_pnl_usd"] > 0.0, f"{key} lost money on clean V-top crash: {st}"
        assert all(w["orders_submitted"] == 0 and w["paper_only"] is True for w in st["waves"])


def test_fast_snap_lab_cli_runs_and_saves_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import test_fast_snap_lab as lab_cli

    monkeypatch.setattr(lab_cli, "ROOT", tmp_path)
    (tmp_path / "logs").mkdir(parents=True)
    tick_file = tmp_path / "logs" / "s1_ticks_XAUUSD_i_20261126.jsonl"
    ticks = _make_ny_open_v_spike_ticks()
    with tick_file.open("w", encoding="utf-8") as fh:
        for ts, bid, ask in ticks:
            fh.write(json.dumps({"ts": ts, "bid": bid, "ask": ask}) + "\n")

    rc = lab_cli.main(["--save"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PART 1: TODAY CONTINUOUS FILE" in out
    assert "VTOP_SWEEP_SNAP" in out
    assert "VSNAP_30S_SESS" in out
    saved = json.loads((tmp_path / "reports" / "paper_live" / "fast_snap_lab.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "fast_snap_lab_v1"
    assert saved["orders_submitted"] == 0
    assert saved["today"]["engines"]["VSNAP_30S_SESS"]["trades"] >= 1
