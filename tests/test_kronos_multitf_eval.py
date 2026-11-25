"""Hermetic offline tests for Kronos multi-timeframe (M1..H4) exporter, evaluator, and Colab notebook."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from trade_mcp.engines.upstream import KRONOS_COMMIT
from trade_mcp.kronos_multitf_eval import (
    evaluate_kronos_predictions_for_tf,
    format_kronos_multitf_summary,
    select_chronological_origins,
)

ROOT = Path(__file__).resolve().parents[1]


def _make_wave_bars(n: int = 600, step_sec: int = 3600) -> list[list[float]]:
    t0 = 1700000000
    px = 3000.0
    bars: list[list[float]] = []
    for i in range(n):
        delta = 4.0 if ((i // 6) % 2 == 0) else -4.0
        o = px
        c = o + delta
        bars.append([float(t0 + i * step_sec), o, max(o, c) + 1.0, min(o, c) - 1.0, c, 100.0])
        px = c
    return bars


def test_select_chronological_origins_non_overlapping() -> None:
    bars = _make_wave_bars(900)
    origins = select_chronological_origins(bars, context_len=256, pred_len=12, n_origins=40)
    assert len(origins) == 40
    assert origins[0] >= 256
    assert origins[-1] + 12 <= len(bars)
    assert all(b - a >= 12 for a, b in zip(origins, origins[1:], strict=False))


def test_evaluate_kronos_predictions_oracle_and_conviction_filter() -> None:
    bars = _make_wave_bars(700)
    origins = select_chronological_origins(bars, context_len=256, pred_len=12, n_origins=20)
    # Near-oracle predictions: true future close +/- tiny noise
    oracle_preds = [
        {
            "origin_idx": idx,
            "pred_closes": [bars[idx + h][4] for h in range(12)],
            "ohlc_invalid_count": 0,
            "vol_neg_count": 0,
        }
        for idx in origins
    ]
    rep = evaluate_kronos_predictions_for_tf(
        tf_label="H1",
        bars=bars,
        origin_results=oracle_preds,
        spread_px=0.32,
        mult=100.0,
    )
    h1 = rep["horizons"]["h1"]
    assert h1["beats_no_change"] is True
    assert h1["mae_ratio_vs_no_change"] == 0.0
    assert h1["all_signals"]["dir_hit_pct"] == 100.0
    assert h1["all_signals"]["trades"]["net"] > 0
    assert h1["all_signals"]["stress_net"] > 0
    assert h1["conviction_2x_spread"]["trades"]["trades"] == h1["all_signals"]["trades"]["trades"]

    # Flat/tiny prediction (< 2*spread) should be filtered out by conviction_2x_spread
    tiny_preds = [
        {
            "origin_idx": idx,
            "pred_closes": [bars[idx - 1][4] + 0.10 for _ in range(12)],
        }
        for idx in origins
    ]
    rep_tiny = evaluate_kronos_predictions_for_tf(
        tf_label="H1", bars=bars, origin_results=tiny_preds, spread_px=0.32, mult=100.0,
    )
    assert rep_tiny["horizons"]["h1"]["conviction_2x_spread"]["trades"]["trades"] == 0

    summary = format_kronos_multitf_summary({
        "symbol": "XAUUSD_i", "model_id": "NeoQuasar/Kronos-small",
        "sample_count": 3, "spread_px": 0.32, "kronos_commit": KRONOS_COMMIT,
        "timeframes": [rep],
    })
    assert "XAUUSD_i" in summary and "[BEAT]" in summary


def test_export_kronos_multitf_bundle_mock_cli() -> None:
    out_file = ROOT / "logs" / "_pytest_kronos_multitf_bundle.json"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "scripts/export_kronos_multitf_bundle.py",
                "--mock",
                "--out",
                str(out_file),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ORDERS=0" in proc.stdout
        assert "REDACTED=True" in proc.stdout
        assert out_file.exists() and (out_file.stat().st_mode & 0o777) == 0o600
        data = json.loads(out_file.read_text(encoding="utf-8"))
        assert data["schema"] == "kronos-multitf-redacted-ohlcv/v1"
        assert set(data["timeframes"].keys()) == {"M1", "M5", "M15", "H1", "H4"}
    finally:
        if out_file.exists():
            out_file.unlink()


def test_kronos_multitf_colab_notebook_valid_and_safe() -> None:
    nb_path = ROOT / "notebooks" / "kronos_multitf_m1_h4_colab.ipynb"
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    assert nb["nbformat"] == 4
    full_text = "\n".join("".join(c["source"]) for c in nb["cells"])
    assert KRONOS_COMMIT in full_text
    assert "IN_COLAB" in full_text
    assert "official_KronosPredictor.predict" in full_text
    assert "execution_authorized" in full_text
    assert "validated_for_trading" in full_text
    assert "files.download" in full_text
    for idx, cell in enumerate(nb["cells"]):
        if cell["cell_type"] == "code":
            compile("".join(cell["source"]), f"<nb-cell-{idx}>", "exec")
