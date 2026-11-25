"""Multi-timeframe (M1..H4) zero-leakage Kronos forecast & spread-adjusted trade evaluator.

Pure-Python evaluation core (no PyTorch dependency) shared by offline unit tests
and the Google Colab GPU benchmark notebook (`notebooks/kronos_multitf_m1_h4_colab.ipynb`).
"""

from __future__ import annotations

import math
from typing import Any

from trade_mcp.candlestick_lab import _summarize_trades

TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
}

FORBIDDEN_REDACTION_KEYS: tuple[str, ...] = (
    "login",
    "password",
    "token",
    "secret",
    "account",
    "journal",
    "credential",
    "balance",
    "equity",
    "server",
)


def select_chronological_origins(
    bars: list[list[float]],
    *,
    context_len: int = 256,
    pred_len: int = 12,
    n_origins: int = 40,
) -> list[int]:
    """Select strictly chronological, non-overlapping origin indices across `bars`.

    For each returned index `idx`:
      - Context is `bars[idx - context_len : idx]` (closed bars strictly before `idx`)
      - Ground-truth future is `bars[idx : idx + pred_len]`
      - Spacing `>= pred_len` guarantees zero overlap between consecutive forecast windows.
    """
    n = len(bars)
    first = context_len
    last = n - pred_len
    if last < first or n_origins <= 0:
        return []
    usable = last - first
    if usable == 0:
        return [first]
    max_non_overlap = (usable // pred_len) + 1
    count = min(n_origins, max_non_overlap)
    if count <= 1:
        return [first]
    step = max(pred_len, usable // (count - 1))
    origins: list[int] = []
    pos = first
    while pos <= last and len(origins) < count:
        origins.append(pos)
        pos += step
    return origins


def evaluate_kronos_predictions_for_tf(
    *,
    tf_label: str,
    bars: list[list[float]],
    origin_results: list[dict[str, Any]],
    spread_px: float,
    mult: float,
    lot: float = 0.01,
    horizons: tuple[int, ...] = (1, 2, 5, 12),
) -> dict[str, Any]:
    """Score chronological Kronos forecasts against No-Change AND real spread-adjusted trades.

    Each item in `origin_results` must contain:
      - `origin_idx`: int (index of first future bar; last context bar is `origin_idx - 1`)
      - `pred_closes`: list[float] (length >= max(horizons))
      - optional `ohlc_invalid_count`: int
      - optional `vol_neg_count`: int
    """
    ordered = sorted(origin_results, key=lambda r: int(r["origin_idx"]))
    n_orig = len(ordered)
    if n_orig == 0:
        return {"timeframe": tf_label, "origins": 0, "error": "no_origins"}

    split_pos = n_orig // 2
    half_sp = spread_px * 0.5
    stress_half_sp = spread_px * 0.75
    stress_comm = 5.0 * lot

    horizon_reports: dict[str, Any] = {}
    for h in horizons:
        k_abs: list[float] = []
        nc_abs: list[float] = []
        dir_hits_all = 0
        dir_n_all = 0
        dir_hits_filt = 0
        dir_n_filt = 0

        tr_all: list[dict[str, Any]] = []
        tr_all_h1: list[dict[str, Any]] = []
        tr_all_h2: list[dict[str, Any]] = []
        tr_all_stress: list[dict[str, Any]] = []

        tr_filt: list[dict[str, Any]] = []
        tr_filt_h1: list[dict[str, Any]] = []
        tr_filt_h2: list[dict[str, Any]] = []
        tr_filt_stress: list[dict[str, Any]] = []

        for rank, item in enumerate(ordered):
            idx = int(item["origin_idx"])
            preds = item["pred_closes"]
            if idx < 1 or idx + h - 1 >= len(bars) or len(preds) < h:
                continue
            last_close = float(bars[idx - 1][4])
            entry_open = float(bars[idx][1])
            actual_close = float(bars[idx + h - 1][4])
            pred_close = float(preds[h - 1])
            if not all(math.isfinite(x) for x in (last_close, entry_open, actual_close, pred_close)):
                continue

            k_abs.append(abs(pred_close - actual_close))
            nc_abs.append(abs(last_close - actual_close))

            pred_delta = pred_close - last_close
            if pred_delta == 0.0:
                continue
            direction = 1 if pred_delta > 0 else -1
            gross_move = (actual_close - entry_open) * direction
            dir_n_all += 1
            if gross_move > 0:
                dir_hits_all += 1

            pnl = (gross_move - 2.0 * half_sp) * mult * lot
            pnl_stress = (gross_move - 2.0 * stress_half_sp) * mult * lot - stress_comm
            trec = {
                "entry_ts": float(bars[idx][0]),
                "exit_ts": float(bars[idx + h - 1][0]),
                "side": "BUY" if direction > 0 else "SELL",
                "pnl": round(pnl, 4),
            }
            trec_st = dict(trec, pnl=round(pnl_stress, 4))
            tr_all.append(trec)
            tr_all_stress.append(trec_st)
            if rank < split_pos:
                tr_all_h1.append(trec)
            else:
                tr_all_h2.append(trec)

            # High-conviction filter: predicted move must exceed 2x broker spread
            if abs(pred_delta) >= 2.0 * spread_px:
                dir_n_filt += 1
                if gross_move > 0:
                    dir_hits_filt += 1
                tr_filt.append(trec)
                tr_filt_stress.append(trec_st)
                if rank < split_pos:
                    tr_filt_h1.append(trec)
                else:
                    tr_filt_h2.append(trec)

        k_mae = (sum(k_abs) / len(k_abs)) if k_abs else None
        nc_mae = (sum(nc_abs) / len(nc_abs)) if nc_abs else None
        ratio = (k_mae / nc_mae) if (k_mae is not None and nc_mae and nc_mae > 0) else None

        s_all = _summarize_trades(tr_all)
        s_all_h1 = _summarize_trades(tr_all_h1)
        s_all_h2 = _summarize_trades(tr_all_h2)
        s_all_st = _summarize_trades(tr_all_stress)

        s_filt = _summarize_trades(tr_filt)
        s_filt_h1 = _summarize_trades(tr_filt_h1)
        s_filt_h2 = _summarize_trades(tr_filt_h2)
        s_filt_st = _summarize_trades(tr_filt_stress)

        horizon_reports[f"h{h}"] = {
            "horizon_bars": h,
            "n": len(k_abs),
            "kronos_mae": round(k_mae, 4) if k_mae is not None else None,
            "no_change_mae": round(nc_mae, 4) if nc_mae is not None else None,
            "mae_ratio_vs_no_change": round(ratio, 3) if ratio is not None else None,
            "beats_no_change": bool(ratio is not None and ratio < 1.0),
            "all_signals": {
                "dir_hit_pct": round(100.0 * dir_hits_all / dir_n_all, 2) if dir_n_all else None,
                "trades": s_all,
                "h1_net": s_all_h1["net"],
                "h2_net": s_all_h2["net"],
                "stress_net": s_all_st["net"],
            },
            "conviction_2x_spread": {
                "dir_hit_pct": round(100.0 * dir_hits_filt / dir_n_filt, 2) if dir_n_filt else None,
                "trades": s_filt,
                "h1_net": s_filt_h1["net"],
                "h2_net": s_filt_h2["net"],
                "stress_net": s_filt_st["net"],
            },
        }

    ohlc_invalid = sum(int(r.get("ohlc_invalid_count", 0)) for r in ordered)
    vol_neg = sum(int(r.get("vol_neg_count", 0)) for r in ordered)
    return {
        "timeframe": tf_label,
        "bars": len(bars),
        "origins": n_orig,
        "spread_px": round(spread_px, 5),
        "mult": round(mult, 4),
        "ohlc_invalid_candles": ohlc_invalid,
        "vol_neg_candles": vol_neg,
        "horizons": horizon_reports,
    }


def format_kronos_multitf_summary(report: dict[str, Any]) -> str:
    """Format a compact multi-timeframe leaderboard for M1..H4 Kronos results."""
    lines = [
        "=== KRONOS MULTI-TIMEFRAME BENCHMARK (M1 -> H4 | ZERO LEAKAGE | REAL ALPARI SPREAD) ===",
        f"SYMBOL={report.get('symbol')} MODEL={report.get('model_id')} SAMPLES={report.get('sample_count')} "
        f"SPREAD={report.get('spread_px')} COMMIT={report.get('kronos_commit')}",
    ]
    for tf_rep in report.get("timeframes", []):
        tf = tf_rep["timeframe"]
        if "error" in tf_rep:
            lines.append(f"\n[{tf}] ERROR: {tf_rep['error']}")
            continue
        lines.append(
            f"\n[{tf}] bars={tf_rep['bars']} origins={tf_rep['origins']} "
            f"ohlc_fixed={tf_rep['ohlc_invalid_candles']} vol_fixed={tf_rep['vol_neg_candles']}"
        )
        for hkey in ("h1", "h2", "h5", "h12"):
            hr = tf_rep["horizons"].get(hkey)
            if not hr:
                continue
            al = hr["all_signals"]
            fc = hr["conviction_2x_spread"]
            at = al["trades"]
            ft = fc["trades"]
            ratio_s = "  None" if hr["mae_ratio_vs_no_change"] is None else f"{hr['mae_ratio_vs_no_change']:6.3f}"
            pf_a = " None" if at["pf"] is None else f"{at['pf']:5.2f}"
            pf_f = " None" if ft["pf"] is None else f"{ft['pf']:5.2f}"
            tag = "BEAT" if hr["beats_no_change"] else "FAIL"
            lines.append(
                f"  +{hr['horizon_bars']:2d}B [{tag}] MAE={hr['kronos_mae']} vs NC={hr['no_change_mae']} "
                f"(ratio={ratio_s}) | ALL: dir={al['dir_hit_pct']}% net=${at['net']:+7.2f} PF={pf_a} "
                f"H1/H2=({al['h1_net']:+.1f}/{al['h2_net']:+.1f}) st=${al['stress_net']:+.1f} | "
                f"CONV(>=2sp,n={ft['trades']:2d}): dir={fc['dir_hit_pct']}% net=${ft['net']:+7.2f} PF={pf_f}"
            )
    return "\n".join(lines)
