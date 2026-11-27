#!/usr/bin/env python3
"""Read-Only Multi-Symbol Alpari Spread Anatomy & Causal AW_V4 Wave-Surfing Audit (ORDERS=0).

Audits EVERY symbol recorded in `logs/s1_ticks_<SYMBOL>_*.jsonl` using strictly deduplicated
real Bid/Ask ticks (exact real broker spreads on every tick!), exact Alpari contract specs from
`reports/paper_live/alpari_wave_symbols_scan.json`, and our proven bug-fixed `COMBO_AW_V4_24H`
engine via linear spread-based normalization (`NormalizedAllWeatherV4`).

Outputs:
  SECTION 1 — Exact Alpari Spread Distribution (P10/P50/P90/Max, London/NY vs Night),
              Contract Economics ($/1.0/lot, 0.01-lot spread cost, real min-lot SL risk),
              and True Wave Impulse Ratios (Range/Spread, P98 30s/60s/5m moves vs Spread).
  SECTION 2 — Spread-Scaled AW_V4 Performance Matrix across ALL symbols (Exact Day/Session
              breakdown, Real Broker Lot & SL $, Net, DD, R/DD, W/L, PF).
  SECTION 3 — Per-Engine x Per-Day Reconciliation + Trade Log for Profitable Non-Gold Winners.
  SECTION 4 — Clear GO / WATCH / REJECT Verdict per Symbol.

Strictly read-only (`TRADE_MCP_ALLOW_TRADING=false`, `ORDERS=0`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from trade_mcp.m1_momentum import atr_series, ema_series  # noqa: E402
from trade_mcp.multi_symbol_aw_v4 import (  # noqa: E402
    GOLD_REF_SL_RISK_USD,
    GOLD_REF_SPREAD,
    AlpariSymbolSpec,
    NormalizedAllWeatherV4,
    compute_risk_parity_lot_scale,
    load_alpari_specs,
)
from trade_mcp.s1_dual_radar import make_synthetic_dual_session  # noqa: E402

TICK_FILE_RE = re.compile(r"^s1_ticks_(.+?)_\d{8}")
LONDON_NY_SRV_HOURS = set(range(9, 21))


def _iso_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _iso_hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M:%S")


def _fmt_usd(val: float, decimals: int = 2) -> str:
    if abs(val) < 0.005:
        return f"$0.{'0' * decimals}" if decimals else "$0"
    return f"+${val:.{decimals}f}" if val > 0 else f"-${abs(val):.{decimals}f}"


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return float(sorted_vals[idx])


def _scale_free_m1_context(bars: list[list[float]], *, sr_lookback: int = 15) -> dict[str, Any]:
    """Strictly causal closed-bar M1 S/R + ATR context without any Gold $0.10 ATR floor."""
    if len(bars) < max(25, sr_lookback + 2):
        return {
            "bias": 0,
            "label": "INSUFFICIENT_M1",
            "support": None,
            "resistance": None,
            "atr_m1": 0.0,
            "pullback_resume": False,
            "last_close": 0.0,
        }
    window = bars[-60:]
    closes = [b[4] for b in window]
    ema9 = [float(v) if v is not None else closes[i] for i, v in enumerate(ema_series(closes, 9))]
    ema21 = [float(v) if v is not None else closes[i] for i, v in enumerate(ema_series(closes, 21))]
    atr_vals = [float(v) if v is not None else 0.0 for v in atr_series(window, period=14)]

    last = window[-1]
    prev_sr = window[-(sr_lookback + 1) : -1]
    sup = min(b[3] for b in prev_sr)
    res = max(b[2] for b in prev_sr)
    atr_now = max(atr_vals[-1], 1e-6)
    c, o, h, low = float(last[4]), float(last[1]), float(last[2]), float(last[3])

    recent5 = window[-6:-1]
    had_green_pb = any(b[4] > b[1] for b in recent5)
    had_red_pb = any(b[4] < b[1] for b in recent5)
    slope10 = c - window[-11][4]

    bias = 0
    label = "CHOP_NEUTRAL"
    pb_resume = False
    if (ema9[-1] < ema21[-1] and c <= ema21[-1] and slope10 <= 0) or (c < sup and c < o):
        lower_wick = min(o, c) - low
        rng = max(h - low, 1e-9)
        dist_to_sup = c - sup
        if 0.0 < dist_to_sup < (0.25 * atr_now) and (lower_wick / rng) >= 0.45:
            bias, label = 0, "BEAR_AT_SUPPORT_WAIT"
        else:
            bias = -1
            if c < sup:
                label = "BEAR_SUP_BREAK"
            elif had_green_pb and c < o:
                label, pb_resume = "BEAR_PB_RESUME", True
            else:
                label = "BEAR_TREND"
    elif (ema9[-1] > ema21[-1] and c >= ema21[-1] and slope10 >= 0) or (c > res and c > o):
        upper_wick = h - max(o, c)
        rng = max(h - low, 1e-9)
        dist_to_res = res - c
        if 0.0 < dist_to_res < (0.25 * atr_now) and (upper_wick / rng) >= 0.45:
            bias, label = 0, "BULL_AT_RESISTANCE_WAIT"
        else:
            bias = 1
            if c > res:
                label = "BULL_RES_BREAK"
            elif had_red_pb and c > o:
                label, pb_resume = "BULL_PB_RESUME", True
            else:
                label = "BULL_TREND"

    return {
        "bias": bias,
        "label": label,
        "support": round(sup, 6),
        "resistance": round(res, 6),
        "atr_m1": round(atr_now, 6),
        "pullback_resume": pb_resume,
        "last_close": round(c, 6),
    }


def discover_recorded_symbol_files(logs_dir: Path) -> dict[str, list[Path]]:
    """Group all `logs/s1_ticks_<SYMBOL>_*.jsonl` files by exact broker symbol name."""
    by_sym: dict[str, list[Path]] = {}
    if not logs_dir.exists():
        return by_sym
    for p in sorted(logs_dir.glob("s1_ticks_*.jsonl")):
        if p.stat().st_size == 0:
            continue
        m = TICK_FILE_RE.match(p.name)
        if not m:
            continue
        sym = m.group(1)
        by_sym.setdefault(sym, []).append(p)
    return by_sym


def load_deduplicated_symbol_segments(
    files: list[Path],
    *,
    dedupe_sec: float = 0.22,
    gap_split_sec: float = 120.0,
) -> list[list[tuple[float, float, float, dict[str, Any]]]]:
    """Merge, chronologically sort, deduplicate, gap-split, and rebuild causal scale-free M1 context."""
    raw: list[tuple[float, float, float]] = []
    for f in files:
        with f.open("r", encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                try:
                    row = json.loads(s)
                    ts = float(row["ts"])
                    bid = float(row["bid"])
                    ask = float(row["ask"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if ts > 0.0 and bid > 0.0 and ask >= bid:
                    raw.append((ts, bid, ask))

    if not raw:
        return []
    raw.sort(key=lambda x: x[0])

    dedup: list[tuple[float, float, float]] = []
    last_ts = -1e18
    for item in raw:
        if (item[0] - last_ts) >= dedupe_sec:
            dedup.append(item)
            last_ts = item[0]

    seg_raw: list[list[tuple[float, float, float]]] = []
    cur: list[tuple[float, float, float]] = []
    for item in dedup:
        if cur and (item[0] - cur[-1][0]) > gap_split_sec:
            if len(cur) >= 30:
                seg_raw.append(cur)
            cur = []
        cur.append(item)
    if len(cur) >= 30:
        seg_raw.append(cur)

    segments: list[list[tuple[float, float, float, dict[str, Any]]]] = []
    for s_ticks in seg_raw:
        out_seg: list[tuple[float, float, float, dict[str, Any]]] = []
        synth_bars: list[list[float]] = []
        cur_bar: list[float] | None = None
        cur_bm = -1
        cached_m1: dict[str, Any] = _scale_free_m1_context([])
        for ts, bid, ask in s_ticks:
            mid = 0.5 * (bid + ask)
            bm = int(ts // 60)
            if cur_bar is None:
                cur_bm = bm
                cur_bar = [float(bm * 60), mid, mid, mid, mid, 1.0]
            elif bm > cur_bm:
                synth_bars.append(cur_bar)
                if len(synth_bars) > 90:
                    synth_bars = synth_bars[-90:]
                cached_m1 = _scale_free_m1_context(synth_bars)
                cur_bm = bm
                cur_bar = [float(bm * 60), mid, mid, mid, mid, 1.0]
            else:
                if mid > cur_bar[2]:
                    cur_bar[2] = mid
                if mid < cur_bar[3]:
                    cur_bar[3] = mid
                cur_bar[4] = mid
                cur_bar[5] += 1.0
            out_seg.append((ts, bid, ask, cached_m1))
        segments.append(out_seg)

    return segments


def analyze_symbol_microstructure(
    symbol: str,
    spec: AlpariSymbolSpec,
    segments: list[list[tuple[float, float, float, dict[str, Any]]]],
) -> dict[str, Any]:
    """Measure exact real tick-by-tick spread distribution and causal wave impulse ratios."""
    all_sp: list[float] = []
    day_sp: list[float] = []
    ngt_sp: list[float] = []
    mids_all: list[float] = []
    mv30: list[float] = []
    mv60: list[float] = []
    mv300: list[float] = []
    start_ts = 1e18
    end_ts = -1e18
    first_mid = 0.0
    last_mid = 0.0
    hi_px = -1e18
    lo_px = 1e18
    total_ticks = 0

    for seg in segments:
        sec_hist: deque[tuple[int, float]] = deque(maxlen=400)
        last_sec = -1
        for ts, bid, ask, _ in seg:
            total_ticks += 1
            mid = 0.5 * (bid + ask)
            sp = ask - bid
            if ts < start_ts:
                start_ts = ts
                first_mid = mid
            if ts > end_ts:
                end_ts = ts
                last_mid = mid
            if mid > hi_px:
                hi_px = mid
            if mid < lo_px:
                lo_px = mid
            all_sp.append(sp)
            mids_all.append(mid)
            hr = datetime.fromtimestamp(ts, tz=timezone.utc).hour
            if hr in LONDON_NY_SRV_HOURS:
                day_sp.append(sp)
            else:
                ngt_sp.append(sp)

            sec = int(ts)
            if sec > last_sec:
                sec_hist.append((sec, mid))
                last_sec = sec
                # Sample causal 30s / 60s / 300s absolute moves every 5s
                if sec % 5 == 0 and len(sec_hist) >= 10:
                    for win, acc in ((30, mv30), (60, mv60), (300, mv300)):
                        cut = sec - win
                        if sec_hist[0][0] <= cut:
                            for s0, p0 in sec_hist:
                                if s0 >= cut:
                                    acc.append(abs(mid - p0))
                                    break

    all_sp.sort()
    day_sp.sort()
    ngt_sp.sort()
    mv30.sort()
    mv60.sort()
    mv300.sort()

    med_sp = _quantile(all_sp, 0.50)
    p10_sp = _quantile(all_sp, 0.10)
    p90_sp = _quantile(all_sp, 0.90)
    max_sp = all_sp[-1] if all_sp else 0.0
    day_med_sp = _quantile(day_sp, 0.50) if day_sp else med_sp
    ngt_med_sp = _quantile(ngt_sp, 0.50) if ngt_sp else med_sp

    # Active reference spread: use London/NY median when available & tighter (avoids midnight rollover distortion)
    active_sp = day_med_sp if (len(day_sp) >= 500 and day_med_sp > 0.0) else med_sp
    active_sp = max(active_sp, 1e-6)

    rng = max(0.0, hi_px - lo_px) if total_ticks else 0.0
    p98_30 = _quantile(mv30, 0.98)
    p98_60 = _quantile(mv60, 0.98)
    p98_300 = _quantile(mv300, 0.98)
    mid_ref = mids_all[len(mids_all) // 2] if mids_all else 1.0
    usd_unit = spec.usd_per_price_unit(mid_ref)

    s_spread = active_sp / GOLD_REF_SPREAD
    s_vol = (p98_60 / 6.0) if p98_60 > 0.0 else s_spread
    lot_scale, real_lot, real_sl_usd, min_clamped = compute_risk_parity_lot_scale(
        price_scale=s_spread,
        usd_per_price_unit=usd_unit,
        target_vsnap_sl_usd=GOLD_REF_SL_RISK_USD,
        volume_min=spec.volume_min,
        volume_step=spec.volume_step,
    )

    rng_sp = rng / active_sp
    r30_sp = p98_30 / active_sp
    r60_sp = p98_60 / active_sp
    r300_sp = p98_300 / active_sp

    if rng_sp < 18.0 or r60_sp < 3.5:
        grade = "DEAD_RANGE"
    elif ngt_med_sp > 1.75 * max(day_med_sp, 1e-9) and day_sp:
        grade = "SESS_ONLY"
    elif r60_sp >= 6.5 and rng_sp >= 45.0:
        grade = "PRIME_WAVE"
    else:
        grade = "MODERATE"

    return {
        "symbol": symbol,
        "asset_class": spec.asset_class,
        "digits": spec.digits,
        "ticks": total_ticks,
        "segments": len(segments),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "first_mid": first_mid,
        "last_mid": last_mid,
        "lo": lo_px,
        "hi": hi_px,
        "range": rng,
        "med_sp": med_sp,
        "p10_sp": p10_sp,
        "p90_sp": p90_sp,
        "max_sp": max_sp,
        "day_med_sp": day_med_sp,
        "ngt_med_sp": ngt_med_sp,
        "active_sp": active_sp,
        "usd_per_unit": usd_unit,
        "sp_cost_001": round(active_sp * 0.01 * usd_unit, 3),
        "rng_to_sp": round(rng_sp, 1),
        "p98_30_to_sp": round(r30_sp, 1),
        "p98_60_to_sp": round(r60_sp, 1),
        "p98_5m_to_sp": round(r300_sp, 1),
        "s_spread": s_spread,
        "s_vol": s_vol,
        "lot_scale": lot_scale,
        "real_vsnap_lot": real_lot,
        "real_sl_usd": real_sl_usd,
        "min_lot_clamped": min_clamped,
        "grade": grade,
    }


def _bucket_for_ts(ts: float, *, is_mock: bool = False) -> str:
    if is_mock:
        return "MOCK_RUN"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    # Keep exact same day/session convention as Gold audit: 11-26 19:35 -> 11-27 = LAST_NIGHT
    if dt >= datetime(2026, 11, 26, 19, 35, 0, tzinfo=timezone.utc) and dt < datetime(2026, 11, 27, 18, 0, 0, tzinfo=timezone.utc):
        return "LAST_NIGHT"
    day_s = dt.strftime("%Y-%m-%d")
    if day_s == "2026-11-26":
        return "11-26_DAY"
    return day_s


def _stats(waves: list[dict[str, Any]]) -> dict[str, Any]:
    if not waves:
        return {"n": 0, "w": 0, "l": 0, "net": 0.0, "dd": 0.0, "rdd": 0.0, "pf": 0.0, "wr": 0.0}
    ordered = sorted(waves, key=lambda w: float(w.get("closed_at", 0.0)))
    eq = 0.0
    pk = 0.0
    dd = 0.0
    gw = 0.0
    gl = 0.0
    wins = 0
    for w in ordered:
        p = float(w.get("net_pnl_usd", 0.0))
        eq += p
        if eq > pk:
            pk = eq
        if (pk - eq) > dd:
            dd = pk - eq
        if p > 0:
            wins += 1
            gw += p
        else:
            gl += abs(p)
    n = len(ordered)
    losses = n - wins
    pf = (gw / gl) if gl > 1e-9 else (99.0 if gw > 0 else 0.0)
    rdd = (eq / dd) if dd > 1e-9 else (99.0 if eq > 0 else 0.0)
    return {
        "n": n,
        "w": wins,
        "l": losses,
        "net": round(eq, 2),
        "dd": round(dd, 2),
        "rdd": round(min(rdd, 99.0), 2),
        "pf": round(min(pf, 99.0), 2),
        "wr": round(100.0 * wins / n, 1),
    }


def _run_variant(
    symbol: str,
    spec: AlpariSymbolSpec,
    micro: dict[str, Any],
    segments: list[list[tuple[float, float, float, dict[str, Any]]]],
    *,
    price_scale: float,
    active_hours: tuple[int, ...] | None,
) -> tuple[list[dict[str, Any]], float, float, bool]:
    lot_scale, real_lot, real_sl_usd, clamped = compute_risk_parity_lot_scale(
        price_scale=price_scale,
        usd_per_price_unit=micro["usd_per_unit"],
        target_vsnap_sl_usd=GOLD_REF_SL_RISK_USD,
        volume_min=spec.volume_min,
        volume_step=spec.volume_step,
    )
    eng = NormalizedAllWeatherV4(
        symbol=symbol,
        price_scale=price_scale,
        usd_per_price_unit=micro["usd_per_unit"],
        lot_scale=lot_scale,
        digits=spec.digits,
        active_hours_srv=active_hours,
        allow_partial_split=True,
    )
    for idx, seg in enumerate(segments):
        if idx > 0:
            eng.reset_segment()
        for ts, bid, ask, m1_ctx in seg:
            eng.on_tick(ts, bid, ask, m1_ctx)
        if seg:
            eng.close_open(seg[-1][0], seg[-1][1], seg[-1][2], reason="eof_seg")
    return eng.waves_closed, real_lot, real_sl_usd, clamped


def _make_mock_symbol_streams() -> dict[str, list[list[tuple[float, float, float, dict[str, Any]]]]]:
    """Generate realistic multi-symbol scaled synthetic tick streams for fast hermetic --mock testing."""
    gold_ticks = make_synthetic_dual_session()
    streams: dict[str, list[list[tuple[float, float, float, dict[str, Any]]]]] = {"XAUUSD_i": [gold_ticks]}
    # Synthetic NAS100_i (~10x Gold spread/move) & XAGUSD_i (~0.09x Gold spread/move) & USDJPY_i (tight dead range)
    for sym, px_base, scale, sp_mult in (
        ("NAS100_i", 26000.0, 10.0, 1.0),
        ("XAGUSD_i", 42.0, 0.09, 1.0),
        ("USDJPY_i", 159.0, 0.006, 8.0),  # deliberately heavy spread -> DEAD_RANGE
    ):
        g0 = 0.5 * (gold_ticks[0][1] + gold_ticks[0][2])
        seg: list[tuple[float, float, float, dict[str, Any]]] = []
        for ts, bid, ask, m1_ctx in gold_ticks:
            mid_g = 0.5 * (bid + ask)
            sp_g = ask - bid
            mid_s = px_base + (mid_g - g0) * scale
            half_sp = 0.5 * sp_g * scale * sp_mult
            m1_s = dict(m1_ctx)
            for k in ("support", "resistance", "last_close"):
                if isinstance(m1_s.get(k), (int, float)):
                    m1_s[k] = round(px_base + (float(m1_s[k]) - g0) * scale, 6)
            if isinstance(m1_s.get("atr_m1"), (int, float)):
                m1_s["atr_m1"] = round(float(m1_s["atr_m1"]) * scale, 6)
            seg.append((ts, round(mid_s - half_sp, 5), round(mid_s + half_sp, 5), m1_s))
        streams[sym] = [seg]
    return streams


def _fmt_scale(val: float) -> str:
    if val < 1.0:
        return f"{val:6.3f}"
    if val < 100.0:
        return f"{val:6.2f}"
    return f"{val:6.1f}"


def _fetch_live_alpari_specs(
    symbols: list[str],
    base_specs: dict[str, AlpariSymbolSpec],
    *,
    save_path: Path | None = None,
) -> dict[str, AlpariSymbolSpec]:
    """Read-only MT5 query (`get_symbol_info`) to capture exact broker volume_min/step/contract_size/tick_value."""
    from daytrade_m1_paper_window import ReadOnlyNativeFeed
    from daytrade_mt5_readonly_check import loopback_native

    from trade_mcp.bridge.native import find
    from trade_mcp.config import Config, load_dotenv

    out = dict(base_specs)
    load_dotenv(ROOT / ".env")
    app_cfg = Config.from_env()
    if app_cfg.backend != "native":
        return out

    raw_dump: dict[str, Any] = {
        "schema": "alpari_symbol_specs_live_v1",
        "orders_submitted": 0,
        "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbols": {},
    }
    with loopback_native():
        feed = ReadOnlyNativeFeed(app_cfg.mcp_url, app_cfg.mcp_token, symbols[0], timeout=12.0)
        for sym in symbols:
            try:
                info = feed._call("get_symbol_info", {"symbol": sym})
            except Exception:
                continue
            base = out.get(sym, AlpariSymbolSpec(sym, "OTHER", 1.0, "USD", 2))
            csize = float(find(info, "trade_contract_size", "contract_size") or base.contract_size)
            cprof = str(find(info, "currency_profit") or base.currency_profit).upper()
            digits = int(float(find(info, "digits") if find(info, "digits") is not None else base.digits))
            vmin = float(find(info, "volume_min") or base.volume_min)
            vstep = float(find(info, "volume_step") or base.volume_step)
            tval = float(find(info, "trade_tick_value", "tick_value") or 0.0)
            tsize = float(find(info, "trade_tick_size", "tick_size", "point") or 0.0)
            sp_pts = float(find(info, "spread") or 0.0)
            out[sym] = AlpariSymbolSpec(
                symbol=sym,
                asset_class=base.asset_class,
                contract_size=csize,
                currency_profit=cprof,
                digits=digits,
                volume_min=vmin,
                volume_step=vstep,
                active_hours_srv=base.active_hours_srv,
            )
            raw_dump["symbols"][sym] = {
                "contract_size": csize,
                "currency_profit": cprof,
                "digits": digits,
                "volume_min": vmin,
                "volume_step": vstep,
                "tick_value": tval,
                "tick_size": tsize,
                "spread_points": sp_pts,
            }
    if save_path is not None and raw_dump["symbols"]:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = save_path.with_name(f".{save_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(raw_dump, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, save_path)
        print(f"SAVED_LIVE_SPECS={save_path} ({len(raw_dump['symbols'])} symbols)")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-Symbol Alpari Spread & AW_V4 Wave-Surfing Audit (ORDERS=0)")
    parser.add_argument("--logs-dir", default="logs")
    parser.add_argument("--scan-json", default="reports/paper_live/alpari_wave_symbols_scan.json")
    parser.add_argument("--symbols", nargs="*", default=[], help="Optional subset of symbols to audit")
    parser.add_argument("--no-gold", action="store_true", help="Exclude XAUUSD_i benchmark")
    parser.add_argument("--refresh-specs", action="store_true", help="Query live read-only MT5 get_symbol_info for exact volume_min/step/contract_size")
    parser.add_argument("--save", action="store_true", help="Save compact summary to reports/paper_live/multi_symbol_wave_audit.json")
    parser.add_argument("--mock", action="store_true", help="Run instant synthetic multi-symbol test")
    args = parser.parse_args(argv)

    os.environ["TRADE_MCP_ALLOW_TRADING"] = "false"
    os.environ["TRADE_MCP_PAPER_TRADING"] = "true"

    scan_path = Path(args.scan_json) if Path(args.scan_json).is_absolute() else (ROOT / args.scan_json)
    specs = load_alpari_specs(scan_path)
    live_specs_path = ROOT / "reports" / "paper_live" / "alpari_symbol_specs_live.json"
    if live_specs_path.exists():
        try:
            live_raw = json.loads(live_specs_path.read_text(encoding="utf-8"))
            for sym_k, row_k in live_raw.get("symbols", {}).items():
                base = specs.get(sym_k, AlpariSymbolSpec(sym_k, "OTHER", 1.0, "USD", 2))
                specs[sym_k] = AlpariSymbolSpec(
                    symbol=sym_k,
                    asset_class=base.asset_class,
                    contract_size=float(row_k.get("contract_size") or base.contract_size),
                    currency_profit=str(row_k.get("currency_profit") or base.currency_profit).upper(),
                    digits=int(row_k.get("digits") if row_k.get("digits") is not None else base.digits),
                    volume_min=float(row_k.get("volume_min") or base.volume_min),
                    volume_step=float(row_k.get("volume_step") or base.volume_step),
                    active_hours_srv=base.active_hours_srv,
                )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    sym_segments: dict[str, list[list[tuple[float, float, float, dict[str, Any]]]]] = {}
    if args.mock:
        sym_segments = _make_mock_symbol_streams()
    else:
        logs_dir = Path(args.logs_dir) if Path(args.logs_dir).is_absolute() else (ROOT / args.logs_dir)
        files_by_sym = discover_recorded_symbol_files(logs_dir)
        if not files_by_sym:
            print(f"No s1_ticks_*.jsonl files found in {logs_dir}", file=sys.stderr)
            return 2
        wanted = set(args.symbols) if args.symbols else None
        for sym, flist in sorted(files_by_sym.items()):
            if args.no_gold and sym.startswith("XAUUSD"):
                continue
            if wanted is not None and sym not in wanted:
                continue
            segs = load_deduplicated_symbol_segments(flist)
            if segs:
                sym_segments[sym] = segs

    if not sym_segments:
        print("No valid symbol tick segments found.", file=sys.stderr)
        return 2

    if args.refresh_specs and not args.mock:
        specs = _fetch_live_alpari_specs(
            sorted(sym_segments.keys()),
            specs,
            save_path=ROOT / "reports" / "paper_live" / "alpari_symbol_specs_live.json",
        )

    # Put XAUUSD_i first as benchmark, then remaining symbols sorted by Range/Spread descending
    micro_map: dict[str, dict[str, Any]] = {}
    for sym, segs in sym_segments.items():
        spec = specs.get(sym, AlpariSymbolSpec(sym, "OTHER", 1.0, "USD", 2))
        micro_map[sym] = analyze_symbol_microstructure(sym, spec, segs)

    ordered_syms = sorted(
        sym_segments.keys(),
        key=lambda s: (0 if s.startswith("XAUUSD") else 1, -micro_map[s]["rng_to_sp"], s),
    )

    print("=" * 136)
    print(f"=== MULTI-SYMBOL ALPARI REAL-SPREAD & AW_V4 WAVE-SURFING AUDIT ({len(ordered_syms)} SYMBOLS | ORDERS=0) ===")
    print("=" * 136)
    print("SECTION 1 — REAL TICK-BY-TICK SPREAD ANATOMY, CONTRACT ECONOMICS & WAVE PHYSICS:")
    print(
        f"{'SYMBOL':11s} {'TICKS':>7s} {'SPAN (SRV)':23s} | {'MED_SP':>7s} {'DAY_SP':>7s} {'NGT_SP':>7s} {'P90_SP':>7s} | "
        f"{'$/1/LOT':>7s} {'0.01SP$':>7s} {'RNG/SP':>6s} {'60s/SP':>6s} | {'S_SP':>6s} {'1xLOT':>5s} {'1xSL$':>6s} {'GRADE':10s}"
    )
    print("-" * 136)
    for sym in ordered_syms:
        m = micro_map[sym]
        d = max(1, min(4, int(m["digits"])))
        span_s = f"{_iso_dt(m['start_ts'])[5:]}->{_iso_dt(m['end_ts'])[5:]}"
        clamp_s = "*" if m["min_lot_clamped"] else " "
        print(
            f"{sym:11s} {m['ticks']:7,d} {span_s:23s} | "
            f"{m['med_sp']:7.{d}f} {m['day_med_sp']:7.{d}f} {m['ngt_med_sp']:7.{d}f} {m['p90_sp']:7.{d}f} | "
            f"{m['usd_per_unit']:7.1f} ${m['sp_cost_001']:6.2f} {m['rng_to_sp']:5.1f}x {m['p98_60_to_sp']:5.1f}x | "
            f"{_fmt_scale(m['s_spread'])} {m['real_vsnap_lot']:5.2f} ${m['real_sl_usd']:5.1f}{clamp_s} {m['grade']:10s}"
        )
    print("  (* = broker 0.02-lot splittable minimum exceeds Gold $5.60/SL risk target; actual 1-SL USD shown)")

    # Discover chronological buckets across all symbols
    all_buckets: list[str] = []
    for sym in ordered_syms:
        for seg in sym_segments[sym]:
            for ts, _, _, _ in seg[:: max(1, len(seg) // 200)]:
                b = _bucket_for_ts(ts, is_mock=args.mock)
                if b not in all_buckets:
                    all_buckets.append(b)
    if not all_buckets:
        all_buckets = ["ALL"]

    print("\nSECTION 2 — SPREAD-SCALED COMBO_AW_V4_24H BACKTEST (REAL BID/ASK TICKS, EXACT DAY/SESSION BREAKDOWN):")
    bhdr = " | ".join(f"{b:>15s}" for b in all_buckets)
    print("-" * (104 + 18 * len(all_buckets)))
    print(
        f"{'SYMBOL':11s} {'VARIANT':16s} {'S':>6s} {'LOT':>5s} {'SL$':>5s} | "
        f"{'ALL_NET':>9s} {'ALL_DD':>8s} {'R/DD':>5s} {'W/L':>7s} {'PF':>5s} | {bhdr}"
    )
    print("-" * (104 + 18 * len(all_buckets)))

    best_by_sym: dict[str, dict[str, Any]] = {}
    audit_rows: list[dict[str, Any]] = []

    for sym in ordered_syms:
        spec = specs.get(sym, AlpariSymbolSpec(sym, "OTHER", 1.0, "USD", 2))
        m = micro_map[sym]
        segs = sym_segments[sym]
        s_sp = m["s_spread"]
        # Bound s_vol within [0.75 * s_sp, 1.80 * s_sp] so spread can never exceed stop/target geometry
        s_vol = max(0.75 * s_sp, min(1.80 * s_sp, m["s_vol"]))

        variants: list[tuple[str, float, tuple[int, ...] | None]] = []
        if sym.startswith("XAUUSD"):
            variants.append(("V4_SHIPPED_1.0", 1.0, None))
        variants.append(("V4_SPREAD_24H", s_sp, None))
        if spec.active_hours_srv is not None:
            variants.append(("V4_SPREAD_SESS", s_sp, spec.active_hours_srv))
        if abs(s_vol - s_sp) / max(s_sp, 1e-9) >= 0.08:
            variants.append(("V4_VOL_24H", s_vol, None))
        variants.append(("V4_WIDE13_24H", s_sp * 1.30, None))

        sym_best: dict[str, Any] | None = None
        for vname, s_val, hrs in variants:
            waves, rlot, rsl, clamped = _run_variant(sym, spec, m, segs, price_scale=s_val, active_hours=hrs)
            sa = _stats(waves)
            by_b: dict[str, dict[str, Any]] = {}
            for b in all_buckets:
                bw = [w for w in waves if _bucket_for_ts(float(w.get("opened_at", w.get("closed_at", 0.0))), is_mock=args.mock) == b]
                by_b[b] = _stats(bw)
            bcells = " | ".join(
                f"{_fmt_usd(by_b[b]['net'], 1)}({by_b[b]['w']}/{by_b[b]['l']})" if by_b[b]["n"] else f"{'$0.0(0/0)':>15s}"
                for b in all_buckets
            )
            bcells_fmt = " | ".join(f"{c:>15s}" for c in bcells.split(" | "))
            print(
                f"{sym:11s} {vname:16s} {_fmt_scale(s_val)} {rlot:5.2f} ${rsl:4.1f} | "
                f"{_fmt_usd(sa['net']):>9s} -${sa['dd']:6.2f} {sa['rdd']:5.2f} {sa['w']:2d}W/{sa['l']:<2d}L {sa['pf']:5.2f} | {bcells_fmt}"
            )
            rec = {
                "symbol": sym,
                "variant": vname,
                "price_scale": round(s_val, 4),
                "active_hours": list(hrs) if hrs is not None else None,
                "real_vsnap_lot": rlot,
                "real_sl_usd": rsl,
                "min_lot_clamped": clamped,
                "stats": sa,
                "by_bucket": by_b,
                "waves": waves,
            }
            audit_rows.append(rec)
            # Rank variants by R/DD + profitability (requiring >= 3 trades)
            score = (1 if (sa["net"] > 0 and sa["n"] >= 3) else 0, sa["rdd"] if sa["net"] > 0 else -1.0, sa["net"])
            if sym_best is None or score > sym_best["_score"]:
                sym_best = {**rec, "_score": score}

        if sym_best is not None:
            best_by_sym[sym] = sym_best
        print("-" * (104 + 18 * len(all_buckets)))

    # SECTION 3: Per-Engine x Per-Day Reconciliation + Trade Log for Profitable Non-Gold Symbols
    winners = [
        best_by_sym[s]
        for s in ordered_syms
        if (not s.startswith("XAUUSD")) and best_by_sym[s]["stats"]["net"] > 0.0 and best_by_sym[s]["stats"]["n"] >= 1
    ]
    print("\nSECTION 3 — PROFITABLE NON-GOLD WINNERS (PER-ENGINE x PER-DAY + TRADE LIST):")
    if not winners:
        print("  (No non-Gold symbol was net-positive in this window — see Section 4 for spread/range diagnosis)")
    for wrec in winners:
        sym = wrec["symbol"]
        waves = wrec["waves"]
        sa = wrec["stats"]
        print(
            f"\n  [{sym} | {wrec['variant']} | S={_fmt_scale(wrec['price_scale']).strip()} | Lot={wrec['real_vsnap_lot']:.2f} | 1-SL=${wrec['real_sl_usd']:.1f}] "
            f"NET {_fmt_usd(sa['net'])} | DD -${sa['dd']:.2f} | R/DD {sa['rdd']:.2f} | PF {sa['pf']:.2f} ({sa['w']}W/{sa['l']}L)"
        )
        engs = [e for e in ("VSNAP6", "SHOCK10", "MACRO_WAVE", "ZSCORE_MR") if any(w.get("_engine") == e for w in waves)]
        print(f"  {'ENGINE':12s} | " + " | ".join(f"{b:>15s}" for b in all_buckets) + f" | {'TOTAL':>18s}")
        print("  " + "-" * (35 + 18 * len(all_buckets)))
        for e in engs:
            ew = [w for w in waves if w.get("_engine") == e]
            cells = []
            for b in all_buckets:
                eb = [w for w in ew if _bucket_for_ts(float(w.get("opened_at", w.get("closed_at", 0.0))), is_mock=args.mock) == b]
                st = _stats(eb)
                cells.append(f"{_fmt_usd(st['net'])}({st['w']}/{st['l']})" if st["n"] else "$0.00(0/0)")
            se = _stats(ew)
            print(f"  {e:12s} | " + " | ".join(f"{c:>15s}" for c in cells) + f" | {_fmt_usd(se['net'])} ({se['w']}W/{se['l']}L)")
        # Print up to 20 trades in native prices & real broker lots
        d = max(1, min(4, int(micro_map[sym]["digits"])))
        print(f"  Trades (native prices, real lots, showing up to 20 of {len(waves)}):")
        cum = 0.0
        for i, w in enumerate(sorted(waves, key=lambda x: float(x.get("opened_at", 0.0)))[:20], 1):
            p = float(w.get("net_pnl_usd", 0.0))
            cum += p
            print(
                f"    {i:2d} {_iso_hms(float(w.get('opened_at', 0.0))):14s} {str(w.get('_engine', ''))[:10]:10s} "
                f"{str(w.get('side', '')).upper():4s} {float(w.get('total_volume', 0.0)):5.2f}L "
                f"{float(w.get('avg_entry', 0.0)):.{d}f}->{float(w.get('exit_price', 0.0)):.{d}f} "
                f"{str(w.get('exit_reason', ''))[:18]:18s} {_fmt_usd(p):>9s} (cum {_fmt_usd(cum):>9s})"
            )

    # SECTION 4: Honest GO / WATCH / REJECT Verdict
    print("\nSECTION 4 — FINAL MULTI-SYMBOL FEASIBILITY VERDICT:")
    print(f"  {'SYMBOL':11s} | {'BEST_VARIANT':16s} | {'MED_SP':>8s} {'RNG/SP':>6s} {'1xSL$':>6s} | {'NET':>9s} {'DD':>8s} {'R/DD':>5s} {'N':>3s} | {'VERDICT'}")
    print("  " + "-" * 105)
    summary_out: dict[str, Any] = {
        "schema": "multi_symbol_wave_audit_v1",
        "orders_submitted": 0,
        "symbols": {},
    }
    for sym in ordered_syms:
        m = micro_map[sym]
        b = best_by_sym[sym]
        sa = b["stats"]
        if sym.startswith("XAUUSD"):
            verdict = "BENCHMARK_LIVE (AW_V4)"
        elif m["grade"] == "DEAD_RANGE":
            verdict = "REJECT (SPREAD_HEAVY / DEAD RANGE)"
        elif sa["n"] < 4:
            verdict = "WATCH (TOO FEW TRADES — RECORD 24-48H MORE)"
        elif sa["net"] > 0.0 and sa["rdd"] >= 2.0 and sa["pf"] >= 1.5:
            verdict = "PROMOTE_CANDIDATE (SOLID EDGE)" if not b["min_lot_clamped"] else "CANDIDATE_HIGH_MIN_LOT (CHECK SL$)"
        elif sa["net"] > 0.0:
            verdict = "MARGINAL_WATCH (POSITIVE BUT THIN)"
        else:
            verdict = "REJECT_CURRENT_V4 (NEGATIVE ON REAL SPREADS)"
        d = max(1, min(4, int(m["digits"])))
        print(
            f"  {sym:11s} | {b['variant']:16s} | {m['active_sp']:8.{d}f} {m['rng_to_sp']:5.1f}x ${b['real_sl_usd']:5.1f} | "
            f"{_fmt_usd(sa['net']):>9s} -${sa['dd']:6.2f} {sa['rdd']:5.2f} {sa['n']:3d} | {verdict}"
        )
        summary_out["symbols"][sym] = {
            "micro": m,
            "best_variant": b["variant"],
            "price_scale": b["price_scale"],
            "real_vsnap_lot": b["real_vsnap_lot"],
            "real_sl_usd": b["real_sl_usd"],
            "stats": sa,
            "verdict": verdict,
        }

    if args.save:
        out_path = (ROOT / "reports" / "paper_live" / "multi_symbol_wave_audit.json").resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(summary_out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, out_path)
        print(f"\nSAVED_AUDIT={out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
