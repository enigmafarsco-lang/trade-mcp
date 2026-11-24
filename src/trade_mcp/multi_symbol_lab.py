"""Multi-Symbol Cross-Asset Generalization & Portfolio Lab (Metals, Forex, Indices, Oil, Crypto).

Tests our two verified Gold champions across ANY symbol available on the broker's MT5 Market Watch
using strictly symbol-adaptive economics (zero Gold-hardcoded price constants!):
  1. H1_DONCHIAN50_LONDON   : H1 Donchian(50) breakout (08-16 UTC), adaptive min-ATR & $25 max stop
  2. H1_DONCHIAN50_LDN_NY   : Same over London+NY (07-20 UTC) — crucial for US Indices, Oil & JPY
  3. M5_MOMENTUM_ADAPTIVE   : Universal M5_5 + London/NY + H1 ADX 20..35, adaptive ATR stop <= $12

Per-symbol economics:
  - Uses `resolve_pricing_basis(spec, 'auto')` (so USD-profit CFDs use true `contract_size` while
    JPY/CAD/CHF-quoted Forex pairs use broker-converted `tick_value / tick_size` accurately!).
  - Computes realistic margin USD per lot safely across Metals, Forex, Indices, Energy, and Crypto.
  - Evaluates Older Half vs Recent Half OOS, 1.5x-spread+$5 cost stress, 500-trial MC, and builds a
    diversified Multi-Symbol Portfolio on $1,000 and $4,000 with dynamic 0.01..0.10 lot sizing.
"""

from __future__ import annotations

import math
from typing import Any

from .daytrade_paper import HARD_MAX_LOT, resolve_pricing_basis
from .gold_lab import _iso_utc, _summarize_trades, monte_carlo_drawdown
from .m1_momentum import (
    LONDON_NY_BLOCKED_UTC_HOURS,
    atr_series,
    build_htf_index,
    clean_ohlcv_bars,
    replay_m1_momentum,
    resample_bars_to_tf,
)
from .portfolio_sizing_lab import (
    _normalize_unit_trade,
    monte_carlo_dynamic_account,
    simulate_dynamic_account,
)


def estimate_margin_per_lot_usd(spec: dict[str, Any], price: float, leverage: float = 500.0) -> float:
    """Estimate realistic 1.0-lot margin in USD across Metals, Forex, Indices, Oil & Crypto."""
    _, mult = resolve_pricing_basis(spec, "auto")
    csize = float(spec.get("trade_contract_size") or 100.0)
    c_prof = str(spec.get("currency_profit") or "").upper()
    c_base = str(spec.get("currency_base") or "").upper()
    # Forex major/cross where contract_size == 100,000 units of base currency (~$100k notional)
    if csize >= 50000.0 and c_base in {"EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF"}:
        notional = csize if c_base == "USD" else (csize * price if c_prof == "USD" else csize * 1.15)
        return max(10.0, notional / max(leverage, 1.0))
    # Metals, Indices, Oil, Crypto: notional = mult * price (USD-converted)
    notional = mult * max(price, 1e-6)
    return max(5.0, notional / max(leverage, 1.0))


def _sim_universal_h1_donchian50(
    h1_bars: list[list[float]],
    *,
    spec: dict[str, Any],
    spread_price: float,
    session_hours: tuple[int, int] = (8, 16),
    commission_per_lot: float = 0.0,
    max_lot: float = 0.01,
    leverage: float = 500.0,
    max_stop_usd: float = 25.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Symbol-agnostic causal H1 Donchian(50) breakout with adaptive ATR gate & $25 stop cap."""
    _, multiplier = resolve_pricing_basis(spec, "auto")
    vol = min(float(max_lot), HARD_MAX_LOT)
    comm = commission_per_lot * vol
    max_stop_dist = max(spread_price * 4.0, (max_stop_usd - comm) / max(vol * multiplier, 1e-9))

    clean = clean_ohlcv_bars(h1_bars)
    if len(clean) < 60:
        return _summarize_trades([], peak_margin_usd=0.0, refusals={"insufficient_h1": 1}), []

    atr_vals = atr_series(clean, period=14)
    start_h, end_h = session_hours
    trades: list[dict[str, Any]] = []
    refusals: dict[str, int] = {}
    peak_margin = 0.0
    pos: dict[str, Any] | None = None

    def _rec(p_obj: dict[str, Any], diff_px: float, closed_ts: float, reason: str) -> None:
        trades.append({
            "opened_at": p_obj["opened_at"],
            "closed_at": closed_ts,
            "side": p_obj["side"],
            "volume": vol,
            "entry": round(p_obj["entry"], 6),
            "initial_stop_dist_price": round(p_obj["sdist"], 6),
            "net_pnl_usd": round(diff_px * vol * multiplier - comm, 4),
            "exit_reason": reason,
        })

    for i in range(52, len(clean)):
        b = clean[i]
        t, o, h, low, c = b[0], b[1], b[2], b[3], b[4]
        bar_end = t + 3600.0
        b1 = clean[i - 1]
        atr1 = atr_vals[i - 1]
        hr_open = (int(t) // 3600) % 24

        opened_now = False
        if pos is None and atr1 is not None and o > 0:
            if not (start_h <= hr_open < end_h):
                pass
            else:
                # Universal volatility gate: ATR must beat spread cleanly (>= 5x spread AND >= 0.08% price)
                min_atr = max(spread_price * 5.0, o * 0.0008)
                if atr1 < min_atr:
                    refusals["low_atr_vs_spread"] = refusals.get("low_atr_vs_spread", 0) + 1
                else:
                    win50 = clean[i - 51 : i - 1]
                    dc_hi = max(x[2] for x in win50)
                    dc_lo = min(x[3] for x in win50)
                    side: str | None = None
                    if b1[4] > dc_hi:
                        side = "buy"
                    elif b1[4] < dc_lo:
                        side = "sell"
                    if side is not None:
                        entry = (o + spread_price) if side == "buy" else o
                        sdist = min(max(2.5 * atr1, spread_price * 4.0), max_stop_dist)
                        stop_px = (entry - sdist) if side == "buy" else (entry + sdist)
                        pos = {
                            "side": side,
                            "entry": entry,
                            "stop": stop_px,
                            "sdist": sdist,
                            "opened_at": t,
                            "opened_i": i,
                            "best_px": entry,
                            "trailed": False,
                        }
                        peak_margin = max(peak_margin, vol * estimate_margin_per_lot_usd(spec, o, leverage))
                        opened_now = True

        if pos is not None:
            side = pos["side"]
            if side == "buy":
                open_ex = (pos["entry"] - spread_price) if opened_now else o
                hit_sl = low <= pos["stop"]
                fill_sl = min(open_ex, pos["stop"])
                ex_close = c
            else:
                open_ex = (pos["entry"] + spread_price) if opened_now else (o + spread_price)
                hit_sl = (h + spread_price) >= pos["stop"]
                fill_sl = max(open_ex, pos["stop"])
                ex_close = c + spread_price

            if hit_sl:
                diff = (fill_sl - pos["entry"]) if side == "buy" else (pos["entry"] - fill_sl)
                _rec(pos, diff, bar_end, "chandelier_trail" if pos["trailed"] else "stop_loss")
                pos = None
                continue

            if not opened_now:
                bars_held = i - pos["opened_i"]
                if bars_held >= 100:
                    diff = (ex_close - pos["entry"]) if side == "buy" else (pos["entry"] - ex_close)
                    _rec(pos, diff, bar_end, "max_100_h1_bars")
                    pos = None
                    continue

                win20 = clean[i - 20 : i]
                if (side == "buy" and c < min(x[3] for x in win20)) or (side == "sell" and c > max(x[2] for x in win20)):
                    diff = (ex_close - pos["entry"]) if side == "buy" else (pos["entry"] - ex_close)
                    _rec(pos, diff, bar_end, "donchian20_exit")
                    pos = None
                    continue

                atr_now = atr_vals[i] or atr1 or pos["sdist"] / 2.5
                if side == "buy":
                    pos["best_px"] = max(pos["best_px"], h)
                    if (c - pos["entry"]) >= 1.5 * pos["sdist"]:
                        cand = max(pos["stop"], pos["entry"], pos["best_px"] - 2.5 * atr_now)
                        if cand > pos["stop"]:
                            pos["stop"] = cand
                            pos["trailed"] = True
                else:
                    pos["best_px"] = min(pos["best_px"], low)
                    if (pos["entry"] - (c + spread_price)) >= 1.5 * pos["sdist"]:
                        cand = min(pos["stop"], pos["entry"], pos["best_px"] + spread_price + 2.5 * atr_now)
                        if cand < pos["stop"]:
                            pos["stop"] = cand
                            pos["trailed"] = True

    if pos is not None:
        lb = clean[-1]
        ex = lb[4] if pos["side"] == "buy" else (lb[4] + spread_price)
        diff = (ex - pos["entry"]) if pos["side"] == "buy" else (pos["entry"] - ex)
        _rec(pos, diff, lb[0] + 3600.0, "end_of_data")

    return _summarize_trades(trades, peak_margin_usd=peak_margin, refusals=refusals), trades


def _adaptive_m5_stop_usd(m5_bars: list[list[float]], *, multiplier: float, spread_price: float, max_lot: float = 0.01) -> float:
    """Compute realistic per-symbol M5 stop budget in USD (matches $12 on Gold, ~$2-$4 on Forex)."""
    atrs = [a for a in atr_series(m5_bars, period=14) if a is not None and a > 0]
    if not atrs:
        return 12.0
    atrs.sort()
    med_atr = atrs[len(atrs) // 2]
    nat_dist = max(med_atr * 3.5, spread_price * 6.0)
    nat_usd = nat_dist * max_lot * multiplier
    return round(min(12.0, max(1.5, nat_usd)), 2)


def evaluate_symbol_bundle(
    *,
    symbol: str,
    spec: dict[str, Any],
    spread_price: float,
    h1_bars: list[list[float]],
    m5_bars: list[list[float]] | None = None,
    m15_bars: list[list[float]] | None = None,
    max_lot: float = 0.01,
    leverage: float = 500.0,
    mc_trials: int = 500,
) -> dict[str, Any]:
    """Benchmark all 3 universal champion engines on one symbol with real Alpari economics."""
    basis, mult = resolve_pricing_basis(spec, "auto")
    clean_h1 = clean_ohlcv_bars(h1_bars)
    if len(clean_h1) < 80:
        return {"symbol": symbol, "ok": False, "error": "insufficient_h1"}

    h1_atrs = [a for a in atr_series(clean_h1, period=14) if a is not None and a > 0]
    med_h1_atr = sorted(h1_atrs)[len(h1_atrs) // 2] if h1_atrs else 0.0
    spread_to_h1_atr_pct = round(100.0 * spread_price / med_h1_atr, 2) if med_h1_atr > 0 else 999.0
    last_px = clean_h1[-1][4]
    margin_001 = round(0.01 * estimate_margin_per_lot_usd(spec, last_px, leverage), 2)

    # 1) H1 Donchian(50) London (08-16 UTC)
    dc_ldn_sum, dc_ldn_tr = _sim_universal_h1_donchian50(
        clean_h1, spec=spec, spread_price=spread_price, session_hours=(8, 16), max_lot=max_lot, leverage=leverage,
    )
    dc_ldn_stress, _ = _sim_universal_h1_donchian50(
        clean_h1, spec=spec, spread_price=spread_price * 1.5, session_hours=(8, 16),
        commission_per_lot=5.0, max_lot=max_lot, leverage=leverage,
    )

    # 2) H1 Donchian(50) London+NY (07-20 UTC)
    dc_ln_sum, dc_ln_tr = _sim_universal_h1_donchian50(
        clean_h1, spec=spec, spread_price=spread_price, session_hours=(7, 20), max_lot=max_lot, leverage=leverage,
    )
    dc_ln_stress, _ = _sim_universal_h1_donchian50(
        clean_h1, spec=spec, spread_price=spread_price * 1.5, session_hours=(7, 20),
        commission_per_lot=5.0, max_lot=max_lot, leverage=leverage,
    )

    # 3) Adaptive M5_MOMENTUM_G5_ADX20_35 (if M5 bars provided)
    m5_sum: dict[str, Any] | None = None
    m5_tr: list[dict[str, Any]] = []
    m5_stress: dict[str, Any] | None = None
    m5_stop_usd: float | None = None
    if m5_bars and len(m5_bars) >= 300:
        clean_m5 = clean_ohlcv_bars(m5_bars)
        clean_m15 = clean_ohlcv_bars(m15_bars) if m15_bars else resample_bars_to_tf(clean_m5, src_seconds=300, dst_seconds=900)
        clean_h4 = resample_bars_to_tf(clean_h1, src_seconds=3600, dst_seconds=14400)
        htf_indices = (
            build_htf_index(clean_m15, period_seconds=900, ema_fast=9, ema_slow=21),
            build_htf_index(clean_h1, period_seconds=3600, ema_fast=9, ema_slow=21),
            build_htf_index(clean_h4, period_seconds=14400, ema_fast=20, ema_slow=50),
        )
        m5_stop_usd = _adaptive_m5_stop_usd(clean_m5, multiplier=mult, spread_price=spread_price, max_lot=max_lot)
        max_margin_cap = max(50.0, margin_001 * 4.0)
        r_m5 = replay_m1_momentum(
            clean_m5, spec=spec, symbol=symbol, bar_seconds=300, htf_indices=htf_indices,
            entry_mode="pullback_or_2bar", htf_filter="m15_h1_h4", exit_mode="trail_only",
            stop_usd=m5_stop_usd, max_lot=max_lot, max_legs=1, spread_price=spread_price,
            commission_per_lot=0.0, leverage=leverage, max_margin_usd=max_margin_cap,
            min_m5_adx=18.0, min_mid_adx=20.0, max_mid_adx=35.0, trail_arm_r=0.8,
            blocked_utc_hours=LONDON_NY_BLOCKED_UTC_HOURS, include_trades=True,
        )
        m5_tr = r_m5.pop("trades", [])
        m5_sum = _summarize_trades(m5_tr, peak_margin_usd=float(r_m5["peak_margin_used_usd"]), refusals=r_m5["refusal_counts"])
        r_m5_s = replay_m1_momentum(
            clean_m5, spec=spec, symbol=symbol, bar_seconds=300, htf_indices=htf_indices,
            entry_mode="pullback_or_2bar", htf_filter="m15_h1_h4", exit_mode="trail_only",
            stop_usd=m5_stop_usd, max_lot=max_lot, max_legs=1, spread_price=spread_price * 1.5,
            commission_per_lot=5.0, leverage=leverage, max_margin_usd=max_margin_cap,
            min_m5_adx=18.0, min_mid_adx=20.0, max_mid_adx=35.0, trail_arm_r=0.8,
            blocked_utc_hours=LONDON_NY_BLOCKED_UTC_HOURS, include_trades=True,
        )
        m5_stress = _summarize_trades(r_m5_s.pop("trades", []), peak_margin_usd=0.0, refusals={})

    def _pack(summary: dict[str, Any], trs: list[dict[str, Any]], stress: dict[str, Any]) -> dict[str, Any]:
        mid = len(trs) // 2
        h1_s = _summarize_trades(trs[:mid], peak_margin_usd=0.0, refusals={}) if mid >= 5 else None
        h2_s = _summarize_trades(trs[mid:], peak_margin_usd=0.0, refusals={}) if len(trs) - mid >= 5 else None
        mc = monte_carlo_drawdown([float(t["net_pnl_usd"]) for t in trs], trials=mc_trials, seed=42)
        dd = float(summary["max_dd_usd"])
        pnl = float(summary["net_pnl_usd"])
        p_dd = round(pnl / dd, 2) if dd > 0 else None
        qualifies = bool(
            pnl > 0
            and (summary["pf"] or 0.0) >= 1.25
            and summary["trades"] >= 30
            and summary["net_without_best_trade_usd"] > 0
            and stress["net_pnl_usd"] > 0
            and h1_s is not None and h1_s["net_pnl_usd"] > 0
            and h2_s is not None and h2_s["net_pnl_usd"] > 0
        )
        return {
            "summary": {**summary, "profit_to_dd_ratio": p_dd},
            "half1": h1_s,
            "half2": h2_s,
            "stress_1_5x_comm5": {"net_pnl_usd": stress["net_pnl_usd"], "pf": stress["pf"]},
            "monte_carlo": mc,
            "qualifies_oos": qualifies,
        }

    engines: dict[str, Any] = {
        "H1_DONCHIAN50_LONDON": _pack(dc_ldn_sum, dc_ldn_tr, dc_ldn_stress),
        "H1_DONCHIAN50_LDN_NY": _pack(dc_ln_sum, dc_ln_tr, dc_ln_stress),
    }
    if m5_sum is not None and m5_stress is not None:
        engines["M5_MOMENTUM_ADAPTIVE"] = {**_pack(m5_sum, m5_tr, m5_stress), "adaptive_stop_usd": m5_stop_usd}

    # Preserve unit trades for cross-symbol portfolio simulation
    unit_trades = {
        "H1_DONCHIAN50_LONDON": [
            _normalize_unit_trade(t, strategy=f"{symbol}:H1_LDN", default_stop_dist=max(med_h1_atr * 2.5, spread_price * 4.0))
            for t in dc_ldn_tr
        ],
        "H1_DONCHIAN50_LDN_NY": [
            _normalize_unit_trade(t, strategy=f"{symbol}:H1_LNY", default_stop_dist=max(med_h1_atr * 2.5, spread_price * 4.0))
            for t in dc_ln_tr
        ],
        "M5_MOMENTUM_ADAPTIVE": [
            _normalize_unit_trade(t, strategy=f"{symbol}:M5", default_stop_dist=max((m5_stop_usd or 12.0) / max(0.01 * mult, 1e-6), spread_price * 4.0))
            for t in m5_tr
        ],
    }

    return {
        "symbol": symbol,
        "ok": True,
        "spec_meta": {
            "currency_base": spec.get("currency_base"),
            "currency_profit": spec.get("currency_profit"),
            "pricing_basis": basis,
            "multiplier": mult,
            "live_spread_price": round(spread_price, 6),
            "median_h1_atr_price": round(med_h1_atr, 6),
            "spread_pct_of_h1_atr": spread_to_h1_atr_pct,
            "est_margin_001_usd": margin_001,
            "h1_bars": len(clean_h1),
            "h1_span_days": round((clean_h1[-1][0] - clean_h1[0][0]) / 86400.0, 1),
            "h1_range_utc": [_iso_utc(clean_h1[0][0]), _iso_utc(clean_h1[-1][0])],
            "m5_bars": len(m5_bars) if m5_bars else 0,
        },
        "engines": engines,
        "_unit_trades": unit_trades,
    }


def build_multisymbol_portfolio_report(
    symbol_results: list[dict[str, Any]],
    *,
    capitals: tuple[float, ...] = (1000.0, 4000.0),
    mc_trials: int = 500,
) -> dict[str, Any]:
    """Compare single-asset Gold vs Multi-Symbol Diversified H1_DONCHIAN50_LONDON Portfolio."""
    ok_rows = [r for r in symbol_results if r.get("ok")]
    winners = [r for r in ok_rows if r["engines"]["H1_DONCHIAN50_LONDON"]["qualifies_oos"]]
    # Scale every trade's stop_dist into normalized $100-multiplier units for unified dynamic sizing
    # Notice: for any symbol, risk_per_001_lot = stop_dist_price * (0.01 * symbol_mult)
    # So if we set virtual multiplier=100.0 and virtual_stop_dist = risk_per_001_lot / 1.0,
    # then raw_lot = (equity * risk_pct) / (virtual_stop_dist * 100.0) = (equity * risk_pct) / risk_per_1_lot! EXACT!
    def _to_virtual_usd_units(row: dict[str, Any], eng_key: str) -> list[dict[str, Any]]:
        mult = float(row["spec_meta"]["multiplier"])
        margin_001 = float(row["spec_meta"]["est_margin_001_usd"])
        out: list[dict[str, Any]] = []
        for t in row.get("_unit_trades", {}).get(eng_key, []):
            risk_per_001 = t["stop_dist_price"] * 0.01 * mult
            # Encode virtual entry so margin = lot * 100 * virtual_entry / 500 == lot * (margin_001 / 0.01)
            virt_entry = max(10.0, (margin_001 / 0.01) * 5.0)
            out.append({
                "strategy": t["strategy"],
                "side": t["side"],
                "opened_at": t["opened_at"],
                "closed_at": t["closed_at"],
                "entry": virt_entry,
                "stop_dist_price": max(risk_per_001, 0.5),
                "pnl_per_001": t["pnl_per_001"],
            })
        return out

    # Strip private _unit_trades before returning clean serializable rows
    winner_units: list[dict[str, Any]] = []
    winner_symbols: list[str] = []
    for r in winners:
        winner_symbols.append(r["symbol"])
        winner_units.extend(_to_virtual_usd_units(r, "H1_DONCHIAN50_LONDON"))
    winner_units.sort(key=lambda x: (x["opened_at"], x["closed_at"]))

    port_by_cap: dict[str, Any] = {}
    if winner_units:
        for cap in capitals:
            sim_001 = simulate_dynamic_account(winner_units, initial_capital=cap, risk_pct=None, min_lot=0.01, max_lot=0.10)
            sim_15 = simulate_dynamic_account(winner_units, initial_capital=cap, risk_pct=0.015, min_lot=0.01, max_lot=0.10, dd_governor_threshold_pct=12.0)
            mc_15 = monte_carlo_dynamic_account(winner_units, initial_capital=cap, risk_pct=0.015, min_lot=0.01, max_lot=0.10, dd_governor_threshold_pct=12.0, trials=mc_trials, seed=42)
            port_by_cap[f"usd_{int(cap)}"] = {
                "fixed_001": sim_001,
                "dyn_1_5pct_ddgov": sim_15,
                "mc_dyn_1_5pct_ddgov": mc_15,
            }

    # Build clean public rows (drop _unit_trades)
    public_rows = [{k: v for k, v in r.items() if k != "_unit_trades"} for r in symbol_results]

    # Rank all tested symbols by H1_DONCHIAN50_LONDON P/DD & PF
    ranked_h1 = sorted(
        [r for r in public_rows if r.get("ok")],
        key=lambda r: (
            1 if r["engines"]["H1_DONCHIAN50_LONDON"]["qualifies_oos"] else 0,
            r["engines"]["H1_DONCHIAN50_LONDON"]["summary"]["profit_to_dd_ratio"] or -99.0,
            r["engines"]["H1_DONCHIAN50_LONDON"]["summary"]["net_pnl_usd"],
        ),
        reverse=True,
    )

    return {
        "ok": True,
        "paper": True,
        "executable": False,
        "execution_authorized": False,
        "orders_submitted": 0,
        "symbols_tested_count": len(ok_rows),
        "qualified_h1_london_symbols": winner_symbols,
        "ranked_symbol_ids": [r["symbol"] for r in ranked_h1],
        "symbols": ranked_h1,
        "multisymbol_h1_portfolio": {
            "included_symbols": winner_symbols,
            "total_trades": len(winner_units),
            "by_capital": port_by_cap,
        },
    }


def select_candidate_symbols(all_marketwatch_names: list[str], *, max_symbols: int = 12) -> list[str]:
    """Auto-pick up to `max_symbols` diverse liquid symbols from broker Market Watch names."""
    # Priority groups: Metals, Majors, JPY Crosses, Indices, Energy, Crypto
    prefixes = [
        ("XAUUSD",),
        ("XAGUSD",),
        ("EURUSD",),
        ("GBPUSD",),
        ("USDJPY",),
        ("GBPJPY",),
        ("EURJPY",),
        ("AUDUSD",),
        ("USDCAD",),
        ("USTEC", "NAS100", "US100", "NDX"),
        ("US30", "DJ30", "WS30"),
        ("SPX500", "US500", "SP500"),
        ("DE40", "GER40", "DAX", "GER30", "DE30"),
        ("USOIL", "XTIUSD", "WTI", "UKOIL", "XBRUSD", "BRENT", "CL"),
        ("BTCUSD",),
        ("ETHUSD",),
    ]
    # Prefer symbols ending in `_i` (Alpari ECN/Pro) or shortest exact match
    cleaned = [n.strip() for n in all_marketwatch_names if n and n.strip()]
    picked: list[str] = []
    seen: set[str] = set()

    for group in prefixes:
        matches: list[str] = []
        for sym in cleaned:
            up = sym.upper()
            for pref in group:
                if up == pref or up.startswith(pref + "_") or up.startswith(pref + ".") or (up.startswith(pref) and len(up) <= len(pref) + 2):
                    matches.append(sym)
                    break
        if matches:
            # Prefer `_i` suffix on Alpari, then shortest name
            matches.sort(key=lambda s: (0 if s.endswith("_i") else 1, len(s), s))
            best = matches[0]
            if best not in seen:
                seen.add(best)
                picked.append(best)
        if len(picked) >= max_symbols:
            break
    return picked


def _safe_float(val: Any) -> float | None:
    try:
        x = float(val)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None
