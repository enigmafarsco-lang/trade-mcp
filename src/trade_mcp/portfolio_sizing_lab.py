"""Multi-Capital Dynamic Lot Sizing (0.01-0.10) & Combined Champions Portfolio Lab.

Evaluates our two verified Alpari XAUUSD_i champions both solo and COMBINED simultaneously:
  - Champion 1: M5_MOMENTUM_G5_ADX20_35  (9/9 gates PASS over 517d M5)
  - Champion 2: H1_DONCHIAN50_LONDON     (+$1,216 over 790d H1; also tested up to 5 years!)

Core capabilities:
  1. 5-Year (2021-2026) yearly regime audit for H1_DONCHIAN50_LONDON comparing
     min_h1_atr=$5.0 vs min_h1_atr=$8.0 (checking low-volatility years honestly).
  2. Exact chronological event-timeline simulator for running M5_ONLY, H1_ONLY, and
     BOTH_COMBINED on the SAME account across starting capitals [$100, $1,000, $4,000]
     with dynamic risk-based lot sizing in [0.01 .. 0.10] (step 0.01):
       - Tighter stop distance -> automatically scales lot UP (up to 0.10 cap)
       - Wider stop distance   -> automatically scales lot DOWN (down to 0.01)
       - Exact simultaneous open margin, 100 oz/lot CFD Leverage math, 50% Stop-Out rule
       - Optional automatic Drawdown Governor (cuts risk 50% when DD >= 12%)
  3. 1,000-trial compounded dynamic-sizing Monte Carlo (median/P5 equity, P95 DD %,
     and exact Blow-Up Probability P(Blown) for every capital & risk profile).
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Any

from .daytrade_paper import resolve_pricing_basis
from .gold_lab import _iso_utc
from .gold_lab_2yr import _sim_h1_donchian50_london
from .m1_momentum import (
    LONDON_NY_BLOCKED_UTC_HOURS,
    build_htf_index,
    clean_ohlcv_bars,
    replay_m1_momentum,
    resample_bars_to_tf,
)


def _floor_lot(raw: float, *, step: float = 0.01) -> float:
    if raw <= 0:
        return 0.0
    return round(math.floor((raw + 1e-12) / step) * step, 2)


def _summarize_by_year_utc(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_yr: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        ts = float(t.get("closed_at") or t.get("opened_at") or 0.0)
        yr = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y")
        by_yr.setdefault(yr, []).append(t)

    out: list[dict[str, Any]] = []
    for yr in sorted(by_yr):
        trs = by_yr[yr]
        pnls = [float(t["net_pnl_usd"]) for t in trs]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gp = sum(wins)
        gl = abs(sum(losses))
        pf = round(gp / gl, 3) if gl > 0 else (99.0 if gp > 0 else None)
        eq = 0.0
        peak = 0.0
        dd = 0.0
        for p in pnls:
            eq += p
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        out.append({
            "year": yr,
            "trades": len(trs),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(100.0 * len(wins) / len(trs), 2) if trs else 0.0,
            "net_pnl_usd": round(sum(pnls), 2),
            "pf": pf,
            "max_dd_usd": round(dd, 2),
        })
    return out


def _monthly_correlation(trades_a: list[dict[str, Any]], trades_b: list[dict[str, Any]]) -> float | None:
    """Pearson correlation of monthly net USD P&L across overlapping calendar months."""
    ma: dict[str, float] = {}
    mb: dict[str, float] = {}
    for t in trades_a:
        k = datetime.fromtimestamp(float(t.get("closed_at") or t["opened_at"]), tz=timezone.utc).strftime("%Y-%m")
        ma[k] = ma.get(k, 0.0) + float(t["net_pnl_usd"])
    for t in trades_b:
        k = datetime.fromtimestamp(float(t.get("closed_at") or t["opened_at"]), tz=timezone.utc).strftime("%Y-%m")
        mb[k] = mb.get(k, 0.0) + float(t["net_pnl_usd"])
    months = sorted(set(ma) | set(mb))
    if len(months) < 4:
        return None
    xs = [ma.get(m, 0.0) for m in months]
    ys = [mb.get(m, 0.0) for m in months]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 1e-12 or vy <= 1e-12:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return round(cov / math.sqrt(vx * vy), 3)


def _normalize_unit_trade(raw: dict[str, Any], *, strategy: str, default_stop_dist: float) -> dict[str, Any]:
    """Normalize a 0.01-lot base trade into unit-lot economics for exact dynamic scaling."""
    base_vol = float(raw.get("volume") or 0.01)
    scale_to_001 = 0.01 / base_vol if base_vol > 0 else 1.0
    opened = float(raw["opened_at"])
    closed = float(raw.get("closed_at") or (opened + float(raw.get("hold_seconds") or 300.0)))
    if closed < opened:
        closed = opened
    stop_dist = float(raw.get("initial_stop_dist_price") or default_stop_dist)
    entry = float(raw.get("entry") or 3000.0)
    pnl_001 = float(raw["net_pnl_usd"]) * scale_to_001
    return {
        "strategy": strategy,
        "side": str(raw.get("side", "buy")),
        "opened_at": opened,
        "closed_at": closed,
        "entry": entry,
        "stop_dist_price": max(stop_dist, 1.0),
        "pnl_per_001": pnl_001,
    }


def simulate_dynamic_account(
    unit_trades: list[dict[str, Any]],
    *,
    initial_capital: float,
    risk_pct: float | None,
    min_lot: float = 0.01,
    max_lot: float = 0.10,
    lot_step: float = 0.01,
    multiplier: float = 100.0,
    contract_size: float = 100.0,
    leverage: float = 500.0,
    commission_per_lot: float = 0.0,
    dd_governor_threshold_pct: float | None = None,
    max_margin_ratio: float = 0.45,
    strict_risk_only: bool = False,
) -> dict[str, Any]:
    """Exact chronological dynamic-lot account simulator (0.01..0.10 lot, real margin & stop-out).

    If `risk_pct is None`, runs fixed `min_lot` (0.01 lot control).
    Otherwise sizes each trade dynamically at entry time `opened_at`:
      - risk_budget = current_equity * eff_risk_pct
      - raw_lot = risk_budget / (stop_dist_price * multiplier + commission_per_lot)
      - lot = floor(min(max_lot, raw_lot), step=0.01)
      - Smaller stop distance => larger lot (up to 0.10); larger stop distance => smaller lot!
      - If raw_lot < min_lot: skipped when strict_risk_only=True, else floored to 0.01 if margin allows.
    """
    # Build chronological event timeline: (timestamp, priority [0=close, 1=open], idx)
    events: list[tuple[float, int, int]] = []
    for idx, tr in enumerate(unit_trades):
        events.append((tr["opened_at"], 1, idx))
        events.append((tr["closed_at"], 0, idx))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    equity = float(initial_capital)
    peak_equity = equity
    max_dd_usd = 0.0
    max_dd_pct = 0.0
    open_positions: dict[int, dict[str, float]] = {}
    used_margin = 0.0
    peak_margin_usd = 0.0
    peak_margin_pct = 0.0
    max_concurrent = 0
    simultaneous_overlap_trades = 0

    executed = 0
    wins = 0
    losses = 0
    gross_profit = 0.0
    gross_loss = 0.0
    skipped_strict_risk = 0
    skipped_margin = 0
    skipped_after_blowup = 0
    floored_to_min_lot = 0
    capped_at_max_lot = 0
    dd_governor_active_trades = 0
    lots_used: list[float] = []
    realized_pnls: list[float] = []
    blown = False
    blown_at_trade: int | None = None
    min_equity = equity

    for _ts, etype, idx in events:
        tr = unit_trades[idx]
        if etype == 0:
            # Close position if it was opened
            pos = open_positions.pop(idx, None)
            if pos is None:
                continue
            used_margin = max(0.0, used_margin - pos["margin"])
            lot = pos["lot"]
            scale = lot / 0.01
            # Extra commission delta if commission_per_lot > 0 (base pnl_per_001 already has base spread)
            pnl = tr["pnl_per_001"] * scale - commission_per_lot * lot
            equity += pnl
            min_equity = min(min_equity, equity)
            realized_pnls.append(pnl)
            if pnl > 0:
                wins += 1
                gross_profit += pnl
            else:
                losses += 1
                gross_loss += abs(pnl)
            if equity > peak_equity:
                peak_equity = equity
            dd_now = peak_equity - equity
            dd_pct_now = (100.0 * dd_now / peak_equity) if peak_equity > 0 else 0.0
            if dd_now > max_dd_usd:
                max_dd_usd = dd_now
            if dd_pct_now > max_dd_pct:
                max_dd_pct = dd_pct_now
            # Alpari realistic stop-out check (equity <= max($15, 50% of used margin) or <= 0)
            if equity <= 15.0 or (used_margin > 0 and equity <= 0.50 * used_margin):
                blown = True
                if blown_at_trade is None:
                    blown_at_trade = executed
        else:
            if blown:
                skipped_after_blowup += 1
                continue
            per_lot_risk = tr["stop_dist_price"] * multiplier + commission_per_lot
            if risk_pct is None:
                lot = min_lot
            else:
                eff_risk_pct = risk_pct
                cur_dd_pct = (100.0 * (peak_equity - equity) / peak_equity) if peak_equity > 0 else 0.0
                if dd_governor_threshold_pct is not None and cur_dd_pct >= dd_governor_threshold_pct:
                    eff_risk_pct = risk_pct * 0.5
                    dd_governor_active_trades += 1
                raw_lot = (equity * eff_risk_pct) / max(per_lot_risk, 1e-6)
                if raw_lot < min_lot:
                    if strict_risk_only:
                        skipped_strict_risk += 1
                        continue
                    lot = min_lot
                    floored_to_min_lot += 1
                else:
                    if raw_lot >= max_lot:
                        capped_at_max_lot += 1
                    lot = _floor_lot(min(max_lot, raw_lot), step=lot_step)
                    if lot < min_lot:
                        lot = min_lot

            req_margin = lot * contract_size * tr["entry"] / leverage
            if (used_margin + req_margin) > equity * max_margin_ratio or equity <= req_margin + 10.0:
                # Try shrinking lot to fit margin ceiling before skipping
                max_fit_margin = max(0.0, equity * max_margin_ratio - used_margin)
                fit_lot = _floor_lot(max_fit_margin * leverage / max(contract_size * tr["entry"], 1.0), step=lot_step)
                if fit_lot < min_lot or equity <= (min_lot * contract_size * tr["entry"] / leverage) + 10.0:
                    skipped_margin += 1
                    continue
                lot = min(lot, fit_lot)
                req_margin = lot * contract_size * tr["entry"] / leverage

            if open_positions:
                simultaneous_overlap_trades += 1
            open_positions[idx] = {"lot": lot, "margin": req_margin}
            used_margin += req_margin
            peak_margin_usd = max(peak_margin_usd, used_margin)
            if equity > 0:
                peak_margin_pct = max(peak_margin_pct, 100.0 * used_margin / equity)
            max_concurrent = max(max_concurrent, len(open_positions))
            executed += 1
            lots_used.append(lot)

    net_pnl = equity - initial_capital
    ret_pct = round(100.0 * net_pnl / initial_capital, 2) if initial_capital > 0 else 0.0
    pf = round(gross_profit / gross_loss, 3) if gross_loss > 0 else (99.0 if gross_profit > 0 else None)
    return {
        "initial_capital_usd": round(initial_capital, 2),
        "final_equity_usd": round(equity, 2),
        "net_pnl_usd": round(net_pnl, 2),
        "return_pct": ret_pct,
        "trades_executed": executed,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / executed, 2) if executed else 0.0,
        "pf": pf,
        "max_dd_usd": round(max_dd_usd, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "return_to_max_dd_ratio": round(ret_pct / max_dd_pct, 2) if max_dd_pct > 0 else None,
        "min_equity_usd": round(min_equity, 2),
        "blown_account": blown,
        "blown_at_trade": blown_at_trade,
        "lot_stats": {
            "min_lot": round(min(lots_used), 2) if lots_used else 0.0,
            "avg_lot": round(sum(lots_used) / len(lots_used), 3) if lots_used else 0.0,
            "max_lot": round(max(lots_used), 2) if lots_used else 0.0,
            "floored_to_001_count": floored_to_min_lot,
            "capped_at_010_count": capped_at_max_lot,
            "dd_governor_trades": dd_governor_active_trades,
        },
        "concurrency": {
            "max_simultaneous_open": max_concurrent,
            "overlap_entries": simultaneous_overlap_trades,
            "peak_margin_usd": round(peak_margin_usd, 2),
            "peak_margin_pct": round(peak_margin_pct, 2),
        },
        "skipped": {
            "strict_risk": skipped_strict_risk,
            "margin": skipped_margin,
            "after_blowup": skipped_after_blowup,
        },
    }


def monte_carlo_dynamic_account(
    unit_trades: list[dict[str, Any]],
    *,
    initial_capital: float,
    risk_pct: float | None,
    min_lot: float = 0.01,
    max_lot: float = 0.10,
    multiplier: float = 100.0,
    contract_size: float = 100.0,
    leverage: float = 500.0,
    dd_governor_threshold_pct: float | None = None,
    trials: int = 1000,
    seed: int = 42,
) -> dict[str, Any]:
    """1,000-trial compounded Monte Carlo with exact dynamic lot sizing (0.01..0.10) & stop-out."""
    if not unit_trades or trials <= 0:
        return {
            "trials": 0,
            "median_final_equity_usd": initial_capital,
            "p05_final_equity_usd": initial_capital,
            "median_max_dd_pct": 0.0,
            "p95_max_dd_pct": 0.0,
            "prob_blown_pct": 0.0,
            "prob_dd_gt_25pct": 0.0,
            "prob_dd_gt_40pct": 0.0,
        }
    rng = random.Random(seed)
    items = [(t["stop_dist_price"], t["pnl_per_001"], t["entry"]) for t in unit_trades]
    finals: list[float] = []
    dds: list[float] = []
    blown_n = 0
    dd25_n = 0
    dd40_n = 0

    for _ in range(trials):
        seq = list(items)
        rng.shuffle(seq)
        eq = float(initial_capital)
        peak = eq
        max_dd_p = 0.0
        is_blown = False
        for sdist, pnl001, entry in seq:
            per_lot_risk = sdist * multiplier
            if risk_pct is None:
                lot = min_lot
            else:
                eff_r = risk_pct
                cur_dd_p = (100.0 * (peak - eq) / peak) if peak > 0 else 0.0
                if dd_governor_threshold_pct is not None and cur_dd_p >= dd_governor_threshold_pct:
                    eff_r = risk_pct * 0.5
                raw = (eq * eff_r) / max(per_lot_risk, 1e-6)
                lot = min_lot if raw < min_lot else _floor_lot(min(max_lot, raw), step=0.01)
                if lot < min_lot:
                    lot = min_lot
            req_m = lot * contract_size * entry / leverage
            if eq <= req_m + 10.0 or eq <= 15.0:
                is_blown = True
                break
            eq += pnl001 * (lot / 0.01)
            if eq > peak:
                peak = eq
            dd_p = (100.0 * (peak - eq) / peak) if peak > 0 else 100.0
            if dd_p > max_dd_p:
                max_dd_p = dd_p
            if eq <= 15.0 or eq <= 0.50 * req_m:
                is_blown = True
                break
        finals.append(round(eq, 2))
        dds.append(round(min(100.0, max_dd_p), 2))
        if is_blown:
            blown_n += 1
        if max_dd_p > 25.0:
            dd25_n += 1
        if max_dd_p > 40.0:
            dd40_n += 1

    finals.sort()
    dds.sort()

    def _pct(arr: list[float], p: float) -> float:
        idx = min(len(arr) - 1, max(0, int(round(p * (len(arr) - 1)))))
        return arr[idx]

    return {
        "trials": trials,
        "median_final_equity_usd": _pct(finals, 0.50),
        "p05_final_equity_usd": _pct(finals, 0.05),
        "p95_final_equity_usd": _pct(finals, 0.95),
        "median_max_dd_pct": _pct(dds, 0.50),
        "p95_max_dd_pct": _pct(dds, 0.95),
        "prob_blown_pct": round(100.0 * blown_n / trials, 2),
        "prob_dd_gt_25pct": round(100.0 * dd25_n / trials, 2),
        "prob_dd_gt_40pct": round(100.0 * dd40_n / trials, 2),
    }


def evaluate_champions_portfolio_lab(
    *,
    m5_bars: list[list[float]],
    m15_bars: list[list[float]],
    h1_bars: list[list[float]],
    h4_bars: list[list[float]] | None = None,
    spec: dict[str, Any],
    symbol: str = "XAUUSD_i",
    spread_price: float = 0.32,
    leverage: float = 500.0,
    capitals: tuple[float, ...] = (100.0, 1000.0, 4000.0),
    min_lot: float = 0.01,
    max_lot: float = 0.10,
    mc_trials: int = 1000,
) -> dict[str, Any]:
    """Run 5-year H1 audit + multi-capital dynamic sizing (0.01..0.10) solo & combined comparison."""
    _, multiplier = resolve_pricing_basis(spec, "auto")
    contract_size = float(spec.get("trade_contract_size") or 100.0)

    clean_m5 = clean_ohlcv_bars(m5_bars)
    clean_m15 = clean_ohlcv_bars(m15_bars) if m15_bars else resample_bars_to_tf(clean_m5, src_seconds=300, dst_seconds=900)
    clean_h1 = clean_ohlcv_bars(h1_bars) if h1_bars else resample_bars_to_tf(clean_m15, src_seconds=900, dst_seconds=3600)
    clean_h4 = clean_ohlcv_bars(h4_bars) if h4_bars else resample_bars_to_tf(clean_h1, src_seconds=3600, dst_seconds=14400)
    if len(clean_m5) < 50 or len(clean_h1) < 60:
        raise ValueError("insufficient bars for champions portfolio lab")

    # 1) Full multi-year (up to 5yr) H1_DONCHIAN50_LONDON audit: ATR >= $5 vs ATR >= $8
    h1_full_atr5_sum, h1_full_atr5_tr = _sim_h1_donchian50_london(
        clean_h1, spec=spec, spread_price=spread_price, max_lot=0.01, leverage=leverage, min_h1_atr=5.0,
    )
    h1_full_atr8_sum, h1_full_atr8_tr = _sim_h1_donchian50_london(
        clean_h1, spec=spec, spread_price=spread_price, max_lot=0.01, leverage=leverage, min_h1_atr=8.0,
    )
    h1_multiyear_audit = {
        "h1_bars": len(clean_h1),
        "h1_span_days": round((clean_h1[-1][0] - clean_h1[0][0]) / 86400.0, 2),
        "h1_range_utc": [_iso_utc(clean_h1[0][0]), _iso_utc(clean_h1[-1][0])],
        "atr_ge_5": {
            "summary_001_lot": h1_full_atr5_sum,
            "by_year": _summarize_by_year_utc(h1_full_atr5_tr),
        },
        "atr_ge_8": {
            "summary_001_lot": h1_full_atr8_sum,
            "by_year": _summarize_by_year_utc(h1_full_atr8_tr),
        },
    }

    # 2) Run M5 champion over full M5 span (~517d)
    htf_indices = (
        build_htf_index(clean_m15, period_seconds=900, ema_fast=9, ema_slow=21),
        build_htf_index(clean_h1, period_seconds=3600, ema_fast=9, ema_slow=21),
        build_htf_index(clean_h4, period_seconds=14400, ema_fast=20, ema_slow=50),
    )
    m5_res = replay_m1_momentum(
        clean_m5,
        spec=spec,
        symbol=symbol,
        bar_seconds=300,
        htf_indices=htf_indices,
        entry_mode="pullback_or_2bar",
        htf_filter="m15_h1_h4",
        exit_mode="trail_only",
        stop_usd=12.0,
        max_lot=0.01,
        max_legs=1,
        spread_price=spread_price,
        commission_per_lot=0.0,
        leverage=leverage,
        max_margin_usd=25.0,
        min_m5_adx=18.0,
        min_mid_adx=20.0,
        max_mid_adx=35.0,
        trail_arm_r=0.8,
        blocked_utc_hours=LONDON_NY_BLOCKED_UTC_HOURS,
        include_trades=True,
    )
    m5_raw_trades = m5_res.pop("trades", [])

    # Align H1 trades to exact overlapping M5 window for strict apples-to-apples comparison
    overlap_start = clean_m5[0][0]
    overlap_end = clean_m5[-1][0] + 300.0
    h1_overlap_raw = [t for t in h1_full_atr5_tr if float(t["opened_at"]) >= overlap_start and float(t["opened_at"]) <= overlap_end]

    m5_units = [_normalize_unit_trade(t, strategy="M5_MOMENTUM_G5_ADX20_35", default_stop_dist=12.0) for t in m5_raw_trades]
    h1_overlap_units = [_normalize_unit_trade(t, strategy="H1_DONCHIAN50_LONDON", default_stop_dist=20.0) for t in h1_overlap_raw]
    combined_units = sorted(m5_units + h1_overlap_units, key=lambda x: (x["opened_at"], x["closed_at"]))
    h1_full_units = [_normalize_unit_trade(t, strategy="H1_DONCHIAN50_LONDON", default_stop_dist=20.0) for t in h1_full_atr5_tr]

    monthly_corr = _monthly_correlation(m5_raw_trades, h1_overlap_raw)

    # 3) Multi-capital ($100, $1,000, $4,000) x Dynamic Risk Profiles (0.01 .. 0.10 lot)
    profiles: list[dict[str, Any]] = [
        {"id": "FIXED_0.01", "label": "Fixed 0.01 lot (baseline)", "risk_pct": None, "dd_gov": None},
        {"id": "DYN_1.0PCT", "label": "Dynamic 1.0% risk (0.01..0.10)", "risk_pct": 0.010, "dd_gov": None},
        {"id": "DYN_2.0PCT", "label": "Dynamic 2.0% risk (0.01..0.10)", "risk_pct": 0.020, "dd_gov": None},
        {"id": "DYN_2.0PCT_DDGOV", "label": "Dynamic 2.0% + half-risk brake at 12% DD", "risk_pct": 0.020, "dd_gov": 12.0},
    ]

    bundles: list[tuple[str, str, list[dict[str, Any]]]] = [
        ("M5_ONLY", "M5_MOMENTUM_G5_ADX20_35 (overlap window)", m5_units),
        ("H1_ONLY_OVERLAP", "H1_DONCHIAN50_LONDON (same overlap window)", h1_overlap_units),
        ("BOTH_COMBINED", "M5 + H1 RUNNING TOGETHER (same account)", combined_units),
        ("H1_FULL_MULTIYEAR", "H1_DONCHIAN50_LONDON (full multi-year H1)", h1_full_units),
    ]

    matrix: dict[str, Any] = {}
    for cap in capitals:
        cap_key = f"usd_{int(cap)}"
        cap_block: dict[str, Any] = {
            "initial_capital_usd": cap,
            "min_001_lot_risk_pct": {
                "m5_stop_12": round(100.0 * 12.0 / cap, 2),
                "h1_stop_25": round(100.0 * 25.0 / cap, 2),
            },
            "strict_2pct_feasibility": {},
            "bundles": {},
        }
        for b_id, b_label, u_trades in bundles:
            strict_check = simulate_dynamic_account(
                u_trades,
                initial_capital=cap,
                risk_pct=0.020,
                min_lot=min_lot,
                max_lot=max_lot,
                multiplier=multiplier,
                contract_size=contract_size,
                leverage=leverage,
                strict_risk_only=True,
            )
            cap_block["strict_2pct_feasibility"][b_id] = {
                "trades_taken_strict_2pct": strict_check["trades_executed"],
                "trades_skipped_min_lot_too_big": strict_check["skipped"]["strict_risk"],
            }
            prof_rows: list[dict[str, Any]] = []
            for prof in profiles:
                sim = simulate_dynamic_account(
                    u_trades,
                    initial_capital=cap,
                    risk_pct=prof["risk_pct"],
                    min_lot=min_lot,
                    max_lot=max_lot,
                    multiplier=multiplier,
                    contract_size=contract_size,
                    leverage=leverage,
                    dd_governor_threshold_pct=prof["dd_gov"],
                    strict_risk_only=False,
                )
                mc = monte_carlo_dynamic_account(
                    u_trades,
                    initial_capital=cap,
                    risk_pct=prof["risk_pct"],
                    min_lot=min_lot,
                    max_lot=max_lot,
                    multiplier=multiplier,
                    contract_size=contract_size,
                    leverage=leverage,
                    dd_governor_threshold_pct=prof["dd_gov"],
                    trials=mc_trials,
                    seed=42,
                )
                prof_rows.append({
                    "profile_id": prof["id"],
                    "label": prof["label"],
                    "simulation": sim,
                    "monte_carlo": mc,
                })
            cap_block["bundles"][b_id] = {"label": b_label, "profiles": prof_rows}
        matrix[cap_key] = cap_block

    return {
        "ok": True,
        "paper": True,
        "executable": False,
        "execution_authorized": False,
        "orders_submitted": 0,
        "overlap_window": {
            "span_days": round((overlap_end - overlap_start) / 86400.0, 2),
            "range_utc": [_iso_utc(overlap_start), _iso_utc(overlap_end)],
            "m5_trades": len(m5_units),
            "h1_overlap_trades": len(h1_overlap_units),
            "combined_trades": len(combined_units),
            "monthly_pnl_correlation": monthly_corr,
        },
        "h1_multiyear_audit": h1_multiyear_audit,
        "capital_matrix": matrix,
    }
