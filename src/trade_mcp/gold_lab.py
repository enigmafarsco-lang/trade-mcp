"""GoldAgent Lab — Strictly causal multi-engine M5 XAUUSD benchmark & n8n live signal API.

Head-to-head pure-Python evaluation on identical Alpari CFD Leverage (100 oz/lot)
execution economics, zero lookahead, conservative SL-before-trail OHLC fills:
  1. M5 Momentum (locked M5_5 + London/NY + H1 ADX>=20, plus exploratory ADX 20..35 check)
  2. Deterministic Smart Money Concepts (SMC):
       - BOS + Fair Value Gap (FVG) retest
       - Institutional Liquidity Sweep (stop-hunt wick rejection)
  3. Adapted vendored `day-trader-skill` scanner patterns on M5:
       - breakout_20_bars + reclaim_ma20 (symmetric long/short)
       - oversold/overbought RSI14 bounce
  4. 1,000-trial Monte Carlo trade-sequence drawdown stress test (Tradetropy-style)
  5. Single-snapshot multi-engine live JSON payload for 5-minute n8n workflows
     (strictly read-only: execution_authorized=False, orders_submitted=0).
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Any

from .daytrade_paper import (
    HARD_MAX_LOT,
    resolve_pricing_basis,
    round_to_step,
)
from .indicators import rsi as rsi_series
from .m1_momentum import (
    HARD_MAX_M5_STOP_USD,
    LONDON_NY_BLOCKED_UTC_HOURS,
    HTFContext,
    _adx_bucket_label,
    _capital_sizing_guide,
    _utc_session_label,
    atr_series,
    build_htf_index,
    clean_ohlcv_bars,
    compute_htf_coverage,
    detect_m1_impulse,
    htf_allows_signal,
    lookup_htf,
    replay_m1_momentum,
    resample_bars_to_tf,
    split_contiguous_bars,
    summarize_months_utc,
)


def _iso_utc(ep: float) -> str:
    return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def monte_carlo_drawdown(
    pnls: list[float],
    *,
    trials: int = 1000,
    seed: int = 42,
) -> dict[str, Any]:
    """Deterministic sequence-reshuffle Monte Carlo drawdown stress test."""
    vals = [float(x) for x in pnls]
    n = len(vals)
    if n == 0:
        return {
            "trials": trials,
            "trades": 0,
            "median_max_dd_usd": 0.0,
            "p90_max_dd_usd": 0.0,
            "p95_max_dd_usd": 0.0,
            "p99_max_dd_usd": 0.0,
            "worst_max_dd_usd": 0.0,
            "prob_dd_gt_65_pct": 0.0,
            "prob_dd_gt_100_pct": 0.0,
            "prob_dd_gt_150_pct": 0.0,
        }
    rng = random.Random(seed)
    dds: list[float] = []
    buf = list(vals)
    for _ in range(trials):
        rng.shuffle(buf)
        eq = 0.0
        peak = 0.0
        mdd = 0.0
        for p in buf:
            eq += p
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > mdd:
                mdd = dd
        dds.append(mdd)
    dds.sort()

    def _pct(q: float) -> float:
        idx = min(len(dds) - 1, max(0, int(math.floor(q * (len(dds) - 1)))))
        return round(dds[idx], 2)

    return {
        "trials": trials,
        "trades": n,
        "median_max_dd_usd": _pct(0.50),
        "p90_max_dd_usd": _pct(0.90),
        "p95_max_dd_usd": _pct(0.95),
        "p99_max_dd_usd": _pct(0.99),
        "worst_max_dd_usd": round(dds[-1], 2),
        "prob_dd_gt_65_pct": round(100.0 * sum(1 for d in dds if d > 65.0) / trials, 1),
        "prob_dd_gt_100_pct": round(100.0 * sum(1 for d in dds if d > 100.0) / trials, 1),
        "prob_dd_gt_150_pct": round(100.0 * sum(1 for d in dds if d > 150.0) / trials, 1),
    }


def _summarize_trades(
    trades: list[dict[str, Any]],
    *,
    peak_margin_usd: float,
    refusals: dict[str, int],
) -> dict[str, Any]:
    wins = [t for t in trades if t["net_pnl_usd"] > 0]
    losses = [t for t in trades if t["net_pnl_usd"] <= 0]
    gp = sum(t["net_pnl_usd"] for t in wins)
    gl = abs(sum(t["net_pnl_usd"] for t in losses))
    net = gp - gl
    pf = round(gp / gl, 3) if gl > 0 else (99.0 if gp > 0 else None)
    best_win = max((t["net_pnl_usd"] for t in wins), default=0.0)
    worst_loss = min((t["net_pnl_usd"] for t in losses), default=0.0)
    net_excl_best = net - best_win
    gp_excl = gp - best_win
    pf_excl = round(gp_excl / gl, 3) if gl > 0 else (99.0 if gp_excl > 0 else None)

    eq = 0.0
    peak = 0.0
    mdd = 0.0
    streak = 0
    max_streak = 0
    exit_reasons: dict[str, int] = {}
    for t in trades:
        p = float(t["net_pnl_usd"])
        eq += p
        if eq > peak:
            peak = eq
        if peak - eq > mdd:
            mdd = peak - eq
        if p <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
        er = str(t.get("exit_reason") or "unknown")
        exit_reasons[er] = exit_reasons.get(er, 0) + 1

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(trades), 2) if trades else 0.0,
        "gross_won_usd": round(gp, 2),
        "gross_lost_usd": round(gl, 2),
        "net_pnl_usd": round(net, 2),
        "avg_win_usd": round(gp / len(wins), 2) if wins else None,
        "avg_loss_usd": round(gl / len(losses), 2) if losses else None,
        "payoff_ratio": round((gp / len(wins)) / (gl / len(losses)), 3) if wins and losses and gl > 0 else None,
        "best_trade_usd": round(best_win, 2),
        "worst_trade_usd": round(worst_loss, 2),
        "net_without_best_trade_usd": round(net_excl_best, 2),
        "pf": pf,
        "pf_without_best_trade": pf_excl,
        "max_dd_usd": round(mdd, 2),
        "max_loss_streak": max_streak,
        "peak_margin_usd": round(peak_margin_usd, 2),
        "exit_reasons": exit_reasons,
        "refusal_counts": refusals,
    }


def _sim_single_position_engine(
    m5_bars: list[list[float]],
    *,
    spec: dict[str, Any],
    htf_indices: tuple[
        tuple[list[int], list[HTFContext]],
        tuple[list[int], list[HTFContext]],
        tuple[list[int], list[HTFContext]],
    ],
    signal_generator: str,
    spread_price: float = 0.34,
    commission_per_lot: float = 0.0,
    stop_usd: float = 12.0,
    max_lot: float = 0.01,
    leverage: float = 500.0,
    trail_arm_r: float = 0.8,
    blocked_utc_hours: tuple[int, ...] = LONDON_NY_BLOCKED_UTC_HOURS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Unified zero-lookahead M5 simulator for SMC and day-trader-skill engines.

    Conservative intrabar rules match `replay_m1_momentum`:
      - Stop loss checked STRICTLY BEFORE trailing stop or time exit.
      - Broker pricing resolved via `resolve_pricing_basis(spec, 'auto')` (100 oz/lot).
      - Signal decisions at bar `i` read ONLY strictly closed bars `<= i-1` and closed HTF bars.
    """
    if signal_generator not in ("smc_bos_fvg", "smc_liquidity_sweep", "skill_breakout20_ma20", "skill_rsi_bounce"):
        raise ValueError(f"unsupported signal_generator: {signal_generator}")

    _, multiplier = resolve_pricing_basis(spec, "auto")
    contract_size = float(spec.get("trade_contract_size") or 100.0)
    tick_size = float(spec.get("trade_tick_size") or spec.get("point") or 0.01)
    vmin = float(spec.get("volume_min") or 0.01)
    vmax = float(spec.get("volume_max") or 50.0)
    vstep = float(spec.get("volume_step") or 0.01)
    eff_stop_usd = min(float(stop_usd), HARD_MAX_M5_STOP_USD)
    eff_max_lot = min(float(max_lot), HARD_MAX_LOT)
    blocked_set = set(blocked_utc_hours)

    m15_t, m15_ctx = htf_indices[0]
    h1_t, h1_ctx = htf_indices[1]
    h4_t, h4_ctx = htf_indices[2]

    segments = [s for s in split_contiguous_bars(clean_ohlcv_bars(m5_bars), bar_seconds=300) if len(s) >= 35]
    trades: list[dict[str, Any]] = []
    refusals: dict[str, int] = {}
    peak_margin = 0.0

    def _ref(reason: str) -> None:
        refusals[reason] = refusals.get(reason, 0) + 1

    def _pnl(pos: dict[str, Any], exit_px: float) -> float:
        diff = (exit_px - pos["entry"]) if pos["side"] == "buy" else (pos["entry"] - exit_px)
        return diff * pos["vol"] * multiplier - pos["comm"]

    for seg in segments:
        atr_vals = atr_series(seg, period=14)
        closes = [b[4] for b in seg]
        rsi_vals = rsi_series(closes, 14) if signal_generator == "skill_rsi_bounce" else []

        pos: dict[str, Any] | None = None
        last_swing_high: float | None = None
        last_swing_low: float | None = None
        pending_fvg: dict[str, Any] | None = None

        for i in range(25, len(seg)):
            bar = seg[i]
            t, o, h, low, c = bar[0], bar[1], bar[2], bar[3], bar[4]
            bar_end = t + 300.0
            atr_now = atr_vals[i - 1] or max(h - low, tick_size)

            # Causal 5-bar fractal swing confirmed strictly at bar i-3 (only uses bars <= i-1!)
            j = i - 3
            if j >= 2:
                if (
                    seg[j][2] > seg[j - 1][2]
                    and seg[j][2] > seg[j - 2][2]
                    and seg[j][2] > seg[j + 1][2]
                    and seg[j][2] > seg[j + 2][2]
                ):
                    last_swing_high = seg[j][2]
                if (
                    seg[j][3] < seg[j - 1][3]
                    and seg[j][3] < seg[j - 2][3]
                    and seg[j][3] < seg[j + 1][3]
                    and seg[j][3] < seg[j + 2][3]
                ):
                    last_swing_low = seg[j][3]

            # 1) Check entry ONLY if flat and not in blocked UTC hour.
            opened_this_bar = False
            if pos is None:
                hr = (int(t) // 3600) % 24
                if hr in blocked_set:
                    if pending_fvg is not None and i > pending_fvg["expires_at"]:
                        pending_fvg = None
                else:
                    m15_c = lookup_htf(m15_t, m15_ctx, int(t))
                    h1_c = lookup_htf(h1_t, h1_ctx, int(t))
                    h4_c = lookup_htf(h4_t, h4_ctx, int(t))

                    entry_sig: dict[str, Any] | None = None
                    b1 = seg[i - 1]
                    b2 = seg[i - 2]
                    b3 = seg[i - 3]

                    if signal_generator == "smc_bos_fvg":
                        # Check if latest closed bar b1 confirmed BOS + FVG (b3 -> b2 -> b1)
                        if last_swing_high is not None and b1[4] > last_swing_high and b1[3] > b3[2]:
                            gap = b1[3] - b3[2]
                            if gap >= 0.25 * atr_now and h1_c and h4_c and h1_c.close >= (h1_c.ema_slow or 0) and (h4_c.ema_fast or 0) >= (h4_c.ema_slow or 0):
                                pending_fvg = {
                                    "side": "buy",
                                    "trig_bid": b1[3] - 0.35 * gap,
                                    "struct_stop": min(b3[3], b2[3]) - 0.15 * atr_now,
                                    "expires_at": i + 6,
                                }
                        elif last_swing_low is not None and b1[4] < last_swing_low and b1[2] < b3[3]:
                            gap = b3[3] - b1[2]
                            if gap >= 0.25 * atr_now and h1_c and h4_c and h1_c.close <= (h1_c.ema_slow or 1e9) and (h4_c.ema_fast or 0) <= (h4_c.ema_slow or 0):
                                pending_fvg = {
                                    "side": "sell",
                                    "trig_bid": b1[2] + 0.35 * gap,
                                    "struct_stop": max(b3[2], b2[2]) + 0.15 * atr_now,
                                    "expires_at": i + 6,
                                }

                        if pending_fvg is not None:
                            if i > pending_fvg["expires_at"]:
                                pending_fvg = None
                            else:
                                side = pending_fvg["side"]
                                trig = pending_fvg["trig_bid"]
                                filled = (low <= trig) if side == "buy" else (h >= trig)
                                if filled:
                                    epx = (min(o, trig) + spread_price) if side == "buy" else max(o, trig)
                                    sdist = abs(epx - pending_fvg["struct_stop"])
                                    entry_sig = {"side": side, "entry": epx, "stop_dist": max(sdist, atr_now * 1.1)}
                                    pending_fvg = None

                    elif signal_generator == "smc_liquidity_sweep":
                        rng1 = b1[2] - b1[3]
                        if rng1 > 0 and h4_c and h4_c.ema_fast is not None and h4_c.ema_slow is not None:
                            # Bullish liquidity sweep: wick below swing low, close firmly back above swing low
                            lower_wick = min(b1[1], b1[4]) - b1[3]
                            upper_wick = b1[2] - max(b1[1], b1[4])
                            if (
                                last_swing_low is not None
                                and b1[3] <= last_swing_low - 0.15 * atr_now
                                and b1[4] > last_swing_low
                                and lower_wick / rng1 >= 0.35
                                and h4_c.ema_fast >= h4_c.ema_slow
                            ):
                                epx = o + spread_price
                                sdist = max(epx - b1[3] + 0.1 * atr_now, atr_now * 1.1)
                                entry_sig = {"side": "buy", "entry": epx, "stop_dist": sdist}
                            elif (
                                last_swing_high is not None
                                and b1[2] >= last_swing_high + 0.15 * atr_now
                                and b1[4] < last_swing_high
                                and upper_wick / rng1 >= 0.35
                                and h4_c.ema_fast <= h4_c.ema_slow
                            ):
                                epx = o
                                sdist = max(b1[2] + spread_price - epx + 0.1 * atr_now, atr_now * 1.1)
                                entry_sig = {"side": "sell", "entry": epx, "stop_dist": sdist}

                    elif signal_generator == "skill_breakout20_ma20":
                        # Direct adaptation of vendored day-trader-skill `breakout_20_bars` & `reclaim_ma20`
                        prev20 = closes[i - 21 : i - 1]
                        ma20_now = sum(closes[i - 20 : i]) / 20.0
                        ma20_prev = sum(prev20) / 20.0
                        c1 = closes[i - 1]
                        c2 = closes[i - 2]
                        cand_side: str | None = None
                        if c1 > max(prev20) or (c2 < ma20_prev and c1 > ma20_now):
                            cand_side = "buy"
                        elif c1 < min(prev20) or (c2 > ma20_prev and c1 < ma20_now):
                            cand_side = "sell"
                        if cand_side is not None:
                            ok_htf, deny = htf_allows_signal(
                                side=cand_side, htf_filter="m15_h1_h4", m5=m15_c, m15=h1_c, h1=h4_c,
                                min_m5_adx=18.0, min_mid_adx=20.0,
                            )
                            if ok_htf:
                                epx = (o + spread_price) if cand_side == "buy" else o
                                sdist = max(abs(b1[2] - b1[3]) * 1.0, atr_now * 1.2)
                                entry_sig = {"side": cand_side, "entry": epx, "stop_dist": sdist}
                            else:
                                _ref(str(deny))

                    elif signal_generator == "skill_rsi_bounce":
                        # Direct adaptation of vendored day-trader-skill `oversold_bounce` (symmetric)
                        r_prev = rsi_vals[i - 2]
                        r_now = rsi_vals[i - 1]
                        c1 = closes[i - 1]
                        c2 = closes[i - 2]
                        if r_prev is not None and r_now is not None and h4_c and h4_c.ema_fast is not None and h4_c.ema_slow is not None:
                            if r_prev < 30.0 and c1 > c2 and h4_c.ema_fast >= h4_c.ema_slow:
                                epx = o + spread_price
                                entry_sig = {"side": "buy", "entry": epx, "stop_dist": max(epx - b1[3], atr_now * 1.2)}
                            elif r_prev > 70.0 and c1 < c2 and h4_c.ema_fast <= h4_c.ema_slow:
                                epx = o
                                entry_sig = {"side": "sell", "entry": epx, "stop_dist": max(b1[2] + spread_price - epx, atr_now * 1.2)}

                    if entry_sig is not None:
                        side = entry_sig["side"]
                        epx = float(entry_sig["entry"])
                        min_budget_dist = max(0.0, (eff_stop_usd / min(eff_max_lot, vmax) - commission_per_lot) / multiplier)
                        sdist = max(float(entry_sig["stop_dist"]), min_budget_dist, spread_price * 2.5, tick_size)
                        per_lot_risk = sdist * multiplier + commission_per_lot
                        vol = round_to_step(min(eff_stop_usd / per_lot_risk, eff_max_lot, vmax), vstep, "down") if per_lot_risk > 0 else 0.0
                        if vol < vmin:
                            _ref("volume_below_minimum")
                        else:
                            est_margin = vol * contract_size * o / leverage
                            if est_margin > 25.0 + 1e-6:
                                _ref("margin_cap_exceeded")
                            else:
                                max_px_dist = (eff_stop_usd - commission_per_lot * vol) / (vol * multiplier)
                                if max_px_dist > 0:
                                    eff_dist = min(sdist, max_px_dist)
                                    stop_px = (epx - eff_dist) if side == "buy" else (epx + eff_dist)
                                    init_risk = eff_dist * vol * multiplier + commission_per_lot * vol
                                    pos = {
                                        "side": side,
                                        "vol": vol,
                                        "entry": epx,
                                        "stop": stop_px,
                                        "comm": commission_per_lot * vol,
                                        "init_risk": init_risk,
                                        "opened_at": t,
                                        "opened_i": i,
                                        "trailed": False,
                                    }
                                    peak_margin = max(peak_margin, est_margin)
                                    opened_this_bar = True

            # 2) Manage open position (SL strictly checked BEFORE trail updates!)
            if pos is not None:
                if pos["side"] == "buy":
                    open_ex = (pos["entry"] - spread_price) if opened_this_bar else o
                    hit_sl = low <= pos["stop"]
                    fill_sl = min(open_ex, pos["stop"])
                    close_mark = c
                else:
                    open_ex = (pos["entry"] + spread_price) if opened_this_bar else (o + spread_price)
                    hit_sl = (h + spread_price) >= pos["stop"]
                    fill_sl = max(open_ex, pos["stop"])
                    close_mark = c + spread_price

                if hit_sl:
                    trades.append({
                        "opened_at": pos["opened_at"],
                        "closed_at": bar_end,
                        "side": pos["side"],
                        "net_pnl_usd": round(_pnl(pos, fill_sl), 4),
                        "exit_reason": "trailing_stop" if pos["trailed"] else "stop_loss",
                    })
                    pos = None
                    continue

                # Uncapped 0.8R trailing stop management on closed M5 bar
                if not opened_this_bar:
                    unreal = _pnl(pos, close_mark)
                    arm = trail_arm_r * pos["init_risk"]
                    if unreal >= arm:
                        be_px = (
                            pos["entry"] + (pos["comm"] + 0.15 * pos["init_risk"]) / (pos["vol"] * multiplier)
                            if pos["side"] == "buy"
                            else pos["entry"] - (pos["comm"] + 0.15 * pos["init_risk"]) / (pos["vol"] * multiplier)
                        )
                        trail_px = (c - max(atr_now * 1.15, spread_price * 2.0)) if pos["side"] == "buy" else (c + spread_price + max(atr_now * 1.15, spread_price * 2.0))
                        cand_stop = max(pos["stop"], be_px, trail_px) if pos["side"] == "buy" else min(pos["stop"], be_px, trail_px)
                        if (pos["side"] == "buy" and cand_stop > pos["stop"]) or (pos["side"] == "sell" and cand_stop < pos["stop"]):
                            pos["stop"] = cand_stop
                            pos["trailed"] = True
                    # Fast invalidation cut after 2 full adverse bars without progress
                    if i - pos["opened_i"] >= 2 and not pos["trailed"] and unreal <= -0.55 * pos["init_risk"]:
                        trades.append({
                            "opened_at": pos["opened_at"],
                            "closed_at": bar_end,
                            "side": pos["side"],
                            "net_pnl_usd": round(unreal, 4),
                            "exit_reason": "fast_invalidation",
                        })
                        pos = None

        # Close any open position cleanly at segment end
        if pos is not None:
            lb = seg[-1]
            ex = lb[4] if pos["side"] == "buy" else lb[4] + spread_price
            trades.append({
                "opened_at": pos["opened_at"],
                "closed_at": lb[0] + 300.0,
                "side": pos["side"],
                "net_pnl_usd": round(_pnl(pos, ex), 4),
                "exit_reason": "segment_end",
            })

    summary = _summarize_trades(trades, peak_margin_usd=peak_margin, refusals=refusals)
    return summary, trades


def evaluate_gold_lab_bakeoff(
    m5_bars: list[list[float]],
    *,
    spec: dict[str, Any],
    symbol: str = "XAUUSD_i",
    spread_price: float = 0.34,
    m15_bars: list[list[float]] | None = None,
    h1_bars: list[list[float]] | None = None,
    h4_bars: list[list[float]] | None = None,
    max_lot: float = 0.01,
    leverage: float = 500.0,
    split_recent_days: int = 180,
    mc_trials: int = 1000,
) -> dict[str, Any]:
    """Run fair 365-day head-to-head bake-off across Momentum, SMC, and DayTrader-Skill engines."""
    clean_m5 = clean_ohlcv_bars(m5_bars)
    if len(clean_m5) < 50:
        raise ValueError("insufficient M5 bars for GoldAgent Lab bake-off")

    p_src = m15_bars if m15_bars else resample_bars_to_tf(clean_m5, src_seconds=300, dst_seconds=900)
    m_src = h1_bars if h1_bars else resample_bars_to_tf(clean_m5, src_seconds=300, dst_seconds=3600)
    g_src = h4_bars if h4_bars else resample_bars_to_tf(m_src, src_seconds=3600, dst_seconds=14400)
    htf_indices = (
        build_htf_index(p_src, period_seconds=900, ema_fast=9, ema_slow=21),
        build_htf_index(m_src, period_seconds=3600, ema_fast=9, ema_slow=21),
        build_htf_index(g_src, period_seconds=14400, ema_fast=20, ema_slow=50),
    )
    cov_raw = compute_htf_coverage(clean_m5, htf_indices=htf_indices)
    coverage = {"m15_pct": cov_raw["m5_pct"], "h1_pct": cov_raw["m15_pct"], "h4_pct": cov_raw["h1_pct"]}

    last_ts = int(clean_m5[-1][0])
    cutoff_ts = last_ts - split_recent_days * 86400
    half1_bars = [b for b in clean_m5 if int(b[0]) < cutoff_ts]
    half2_bars = [b for b in clean_m5 if int(b[0]) >= cutoff_ts]
    half1_span = round((half1_bars[-1][0] - half1_bars[0][0]) / 86400.0, 2) if len(half1_bars) >= 2 else 0.0
    half2_span = round((half2_bars[-1][0] - half2_bars[0][0]) / 86400.0, 2) if len(half2_bars) >= 2 else 0.0
    has_dual_halves = len(half1_bars) >= 200 and half1_span >= 45.0 and len(half2_bars) >= 200

    engines: list[dict[str, Any]] = [
        {
            "id": "M5_MOMENTUM_G5",
            "family": "xauusd_day_trader",
            "label": "Locked M5_5 + London/NY (07-20 UTC) + H1 ADX >= 20",
            "kind": "momentum",
            "exploratory_in_sample_flag": False,
            "max_mid_adx": None,
        },
        {
            "id": "M5_MOMENTUM_G5_ADX20_35",
            "family": "xauusd_day_trader",
            "label": "EXPLORATORY: M5_5 + London/NY + healthy H1 ADX 20..35 (skip exhausted >=35)",
            "kind": "momentum",
            "exploratory_in_sample_flag": True,
            "max_mid_adx": 35.0,
        },
        {
            "id": "SMC_BOS_FVG",
            "family": "ai_smc_deterministic",
            "label": "Deterministic SMC: Causal Swing BOS + Fair Value Gap Retest (H1+H4, London/NY)",
            "kind": "smc_bos_fvg",
            "exploratory_in_sample_flag": False,
        },
        {
            "id": "SMC_LIQUIDITY_SWEEP",
            "family": "ai_smc_deterministic",
            "label": "Deterministic SMC: Liquidity Sweep / Stop-Hunt Rejection (H4, London/NY)",
            "kind": "smc_liquidity_sweep",
            "exploratory_in_sample_flag": False,
        },
        {
            "id": "SKILL_BREAKOUT20_MA20",
            "family": "day_trader_skill_11",
            "label": "Vendored day-trader-skill: breakout_20_bars + reclaim_ma20 (M15+H1+H4, London/NY)",
            "kind": "skill_breakout20_ma20",
            "exploratory_in_sample_flag": False,
        },
        {
            "id": "SKILL_RSI_BOUNCE",
            "family": "day_trader_skill_11",
            "label": "Vendored day-trader-skill: RSI14 <30/>70 bounce (H4, London/NY)",
            "kind": "skill_rsi_bounce",
            "exploratory_in_sample_flag": False,
        },
    ]

    cost_ladder: list[tuple[str, float, float]] = [
        ("base_live_spread", spread_price, 0.0),
        ("stress_spread_050", max(spread_price, 0.50), 0.0),
        ("stress_spread_050_comm5", max(spread_price, 0.50), 5.0),
        ("stress_spread_070_comm7", max(spread_price, 0.70), 7.0),
    ]

    def _run_one(bars: list[list[float]], eng: dict[str, Any], sp: float, comm: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if eng["kind"] == "momentum":
            res = replay_m1_momentum(
                bars,
                spec=spec,
                symbol=symbol,
                bar_seconds=300,
                htf_indices=htf_indices,
                entry_mode="pullback_or_2bar",
                htf_filter="m15_h1_h4",
                exit_mode="trail_only",
                stop_usd=12.0,
                max_lot=max_lot,
                max_legs=1,
                spread_price=sp,
                commission_per_lot=comm,
                leverage=leverage,
                max_margin_usd=25.0,
                min_m5_adx=18.0,
                min_mid_adx=20.0,
                max_mid_adx=eng.get("max_mid_adx"),
                trail_arm_r=0.8,
                blocked_utc_hours=LONDON_NY_BLOCKED_UTC_HOURS,
                include_trades=True,
            )
            tr = res.pop("trades", [])
            summary = _summarize_trades(tr, peak_margin_usd=float(res["peak_margin_used_usd"]), refusals=res["refusal_counts"])
            return summary, tr
        return _sim_single_position_engine(
            bars,
            spec=spec,
            htf_indices=htf_indices,
            signal_generator=eng["kind"],
            spread_price=sp,
            commission_per_lot=comm,
            stop_usd=12.0,
            max_lot=max_lot,
            leverage=leverage,
            trail_arm_r=0.8,
            blocked_utc_hours=LONDON_NY_BLOCKED_UTC_HOURS,
        )

    rows: list[dict[str, Any]] = []
    for eng in engines:
        full_sum, full_trades = _run_one(clean_m5, eng, spread_price, 0.0)
        h1_sum, _ = _run_one(half1_bars, eng, spread_price, 0.0) if has_dual_halves else (None, [])
        h2_sum, _ = _run_one(half2_bars, eng, spread_price, 0.0) if len(half2_bars) >= 20 else (None, [])

        months = summarize_months_utc(full_trades)
        active_months = [m for m in months if m["trades"] >= 3]
        pos_months = sum(1 for m in active_months if m["net_pnl_usd"] > 0)
        mc = monte_carlo_drawdown([float(t["net_pnl_usd"]) for t in full_trades], trials=mc_trials, seed=42)

        stress: dict[str, dict[str, Any]] = {}
        for s_name, s_sp, s_comm in cost_ladder:
            s_sum = full_sum if s_name == "base_live_spread" else _run_one(clean_m5, eng, s_sp, s_comm)[0]
            stress[s_name] = {
                "spread_price": round(s_sp, 4),
                "commission_per_lot": round(s_comm, 2),
                "trades": s_sum["trades"],
                "net_pnl_usd": s_sum["net_pnl_usd"],
                "pf": s_sum["pf"],
                "max_dd_usd": s_sum["max_dd_usd"],
            }

        dd = float(full_sum["max_dd_usd"])
        pnl = float(full_sum["net_pnl_usd"])
        p_dd = round(pnl / dd, 2) if dd > 0 else None
        s050_c5 = stress["stress_spread_050_comm5"]

        gates: dict[str, bool] = {
            "full_positive_and_pf_ge_1_30": pnl > 0 and (full_sum["pf"] or 0.0) >= 1.30 and full_sum["trades"] >= 60,
            "full_profitable_without_best_trade": full_sum["net_without_best_trade_usd"] > 0,
            "full_drawdown_le_65": dd <= 65.0,
            "profit_to_dd_ratio_ge_2_2": bool(p_dd is not None and p_dd >= 2.2),
            "survives_spread_050_comm5": s050_c5["net_pnl_usd"] > 0 and (s050_c5["pf"] or 0.0) >= 1.10,
            "months_majority_positive": bool(len(active_months) >= 3 and (pos_months / len(active_months)) >= 0.65),
            "monte_carlo_p95_dd_le_120": mc["p95_max_dd_usd"] <= 120.0,
        }
        if has_dual_halves and h1_sum is not None and h2_sum is not None:
            gates["half1_positive_and_pf_ge_1_15"] = (
                h1_sum["net_pnl_usd"] > 0
                and (h1_sum["pf"] or 0.0) >= 1.15
                and h1_sum["trades"] >= 25
                and h1_sum["net_without_best_trade_usd"] >= 0.0
            )
            gates["half2_positive_and_pf_ge_1_15"] = (
                h2_sum["net_pnl_usd"] > 0
                and (h2_sum["pf"] or 0.0) >= 1.15
                and h2_sum["trades"] >= 25
            )
        failed = [k for k, ok in gates.items() if not ok]

        rows.append({
            "id": eng["id"],
            "family": eng["family"],
            "label": eng["label"],
            "exploratory_in_sample_flag": bool(eng["exploratory_in_sample_flag"]),
            "full_period": {**full_sum, "profit_to_dd_ratio": p_dd},
            "half1_older_180d": h1_sum,
            "half2_recent_180d": h2_sum,
            "monte_carlo": mc,
            "full_cost_stress": stress,
            "monthly_breakdown": months,
            "active_months_positive": f"{pos_months}/{len(active_months)}",
            "capital_sizing_guide": _capital_sizing_guide(dd, float(full_sum["peak_margin_usd"])),
            "gates": gates,
            "gates_passed_count": sum(1 for ok in gates.values() if ok),
            "failed_gates": failed,
            "all_gates_passed": len(failed) == 0,
        })

    non_exp_passed = [r for r in rows if r["all_gates_passed"] and not r["exploratory_in_sample_flag"]]
    ranked = sorted(
        rows,
        key=lambda r: (
            1 if (r["all_gates_passed"] and not r["exploratory_in_sample_flag"]) else 0,
            r["gates_passed_count"],
            r["full_period"]["profit_to_dd_ratio"] or 0.0,
            r["full_period"]["net_pnl_usd"],
        ),
        reverse=True,
    )

    return {
        "ok": True,
        "paper": True,
        "executable": False,
        "execution_authorized": False,
        "orders_submitted": 0,
        "htf_coverage_pct": coverage,
        "split": {
            "split_recent_days": split_recent_days,
            "cutoff_utc": _iso_utc(cutoff_ts),
            "has_dual_halves": has_dual_halves,
            "half1_older_bars": len(half1_bars),
            "half1_older_span_days": half1_span,
            "half2_recent_bars": len(half2_bars),
            "half2_recent_span_days": half2_span,
        },
        "engines": rows,
        "verdict": {
            "any_non_exploratory_passed": len(non_exp_passed) > 0,
            "passed_ids": [r["id"] for r in rows if r["all_gates_passed"]],
            "recommended_for_live_paper": non_exp_passed[0]["id"] if non_exp_passed else None,
            "best_overall_diagnostic_id": ranked[0]["id"] if ranked else None,
            "status": (
                "PASS_GOLD_LAB_ENGINE_READY_FOR_LIVE_PAPER"
                if non_exp_passed
                else "DIAGNOSTIC_ONLY_REVIEW_BEFORE_LIVE_PAPER"
            ),
        },
    }


def evaluate_live_m5_snapshot(
    m5_bars: list[list[float]],
    *,
    symbol: str = "XAUUSD_i",
    bid: float = 0.0,
    ask: float = 0.0,
    m15_bars: list[list[float]] | None = None,
    h1_bars: list[list[float]] | None = None,
    h4_bars: list[list[float]] | None = None,
) -> dict[str, Any]:
    """Evaluate ONE strictly closed M5 snapshot for 5-minute n8n workflows (read-only).

    Drops the last bar if it may still be forming (caller passes closed bars or full MT5 list;
    we evaluate the latest strictly closed bar cleanly with zero lookahead).
    """
    clean_m5 = clean_ohlcv_bars(m5_bars)
    if len(clean_m5) < 35:
        raise ValueError("Need at least 35 closed M5 bars for multi-engine snapshot")

    p_src = clean_ohlcv_bars(m15_bars) if m15_bars else resample_bars_to_tf(clean_m5, src_seconds=300, dst_seconds=900)
    m_src = clean_ohlcv_bars(h1_bars) if h1_bars else resample_bars_to_tf(clean_m5, src_seconds=300, dst_seconds=3600)
    g_src = clean_ohlcv_bars(h4_bars) if h4_bars else resample_bars_to_tf(m_src, src_seconds=3600, dst_seconds=14400)

    m15_idx = build_htf_index(p_src, period_seconds=900, ema_fast=9, ema_slow=21)
    h1_idx = build_htf_index(m_src, period_seconds=3600, ema_fast=9, ema_slow=21)
    h4_idx = build_htf_index(g_src, period_seconds=14400, ema_fast=20, ema_slow=50)

    last_closed = clean_m5[-1]
    eval_ts = int(last_closed[0]) + 300
    spread_price = round(max(0.0, float(ask) - float(bid)), 4) if bid > 0 and ask > 0 else 0.34
    utc_hour = (eval_ts // 3600) % 24
    session_label = _utc_session_label(utc_hour)
    in_london_ny = utc_hour not in LONDON_NY_BLOCKED_UTC_HOURS

    m15_c = lookup_htf(m15_idx[0], m15_idx[1], eval_ts)
    h1_c = lookup_htf(h1_idx[0], h1_idx[1], eval_ts)
    h4_c = lookup_htf(h4_idx[0], h4_idx[1], eval_ts)
    h1_adx = round(h1_c.adx, 2) if (h1_c and h1_c.adx is not None) else None

    # Append synthetic dummy current bar at eval_ts so detect_m1_impulse reads up to clean_m5[-1] (strictly closed!)
    seg_for_eval = clean_m5[-40:] + [[float(eval_ts), last_closed[4], last_closed[4], last_closed[4], last_closed[4], 0.0]]
    atr_vals = atr_series(seg_for_eval, period=14)
    idx_now = len(seg_for_eval) - 1
    atr_m5 = round(float(atr_vals[idx_now - 1] or (last_closed[2] - last_closed[3])), 4)

    # 1) M5 Momentum G5 check
    mom_sig = detect_m1_impulse(seg_for_eval, atr_vals, idx_now, entry_mode="impulse_pullback", spread_price=spread_price)
    if mom_sig is None:
        mom_sig = detect_m1_impulse(seg_for_eval, atr_vals, idx_now, entry_mode="impulse_2bar", spread_price=spread_price)
    mom_out: dict[str, Any] = {"signal": "none", "reason": "no_m5_impulse"}
    if mom_sig is not None:
        if not in_london_ny:
            mom_out = {"signal": "none", "reason": "outside_london_ny_session", "raw_side": mom_sig["side"]}
        else:
            ok_htf, deny = htf_allows_signal(
                side=mom_sig["side"], htf_filter="m15_h1_h4", m5=m15_c, m15=h1_c, h1=h4_c,
                min_m5_adx=18.0, min_mid_adx=20.0,
            )
            if ok_htf:
                mom_out = {
                    "signal": mom_sig["side"],
                    "reason": f"m5_{mom_sig['mode']}_aligned_m15_h1_h4",
                    "limit_trigger_bid": round(float(mom_sig["limit_trigger_bid"]), 2) if mom_sig.get("limit_trigger_bid") else None,
                    "h1_exhausted_warning": bool(h1_adx is not None and h1_adx >= 35.0),
                    "suggested_stop_usd": 12.0,
                    "suggested_lot": 0.01,
                }
            else:
                mom_out = {"signal": "none", "reason": str(deny), "raw_side": mom_sig["side"]}

    # 2) Causal SMC swings, BOS+FVG, Liquidity Sweep on strictly closed M5 bars
    sw_h: float | None = None
    sw_l: float | None = None
    for j in range(2, len(clean_m5) - 2):
        if clean_m5[j][2] > max(clean_m5[j - 1][2], clean_m5[j - 2][2], clean_m5[j + 1][2], clean_m5[j + 2][2]):
            sw_h = clean_m5[j][2]
        if clean_m5[j][3] < min(clean_m5[j - 1][3], clean_m5[j - 2][3], clean_m5[j + 1][3], clean_m5[j + 2][3]):
            sw_l = clean_m5[j][3]

    b1, b3 = clean_m5[-1], clean_m5[-3]
    smc_fvg_out: dict[str, Any] = {"signal": "none", "reason": "no_bos_fvg", "last_swing_high": sw_h, "last_swing_low": sw_l}
    if sw_h is not None and b1[4] > sw_h and b1[3] > b3[2] and (b1[3] - b3[2]) >= 0.25 * atr_m5:
        smc_fvg_out = {
            "signal": "buy" if (in_london_ny and h4_c and (h4_c.ema_fast or 0) >= (h4_c.ema_slow or 0)) else "none",
            "reason": "bullish_bos_and_fvg",
            "fvg_zone": [round(b3[2], 2), round(b1[3], 2)],
            "last_swing_high": sw_h,
            "last_swing_low": sw_l,
        }
    elif sw_l is not None and b1[4] < sw_l and b1[2] < b3[3] and (b3[3] - b1[2]) >= 0.25 * atr_m5:
        smc_fvg_out = {
            "signal": "sell" if (in_london_ny and h4_c and (h4_c.ema_fast or 0) <= (h4_c.ema_slow or 0)) else "none",
            "reason": "bearish_bos_and_fvg",
            "fvg_zone": [round(b1[2], 2), round(b3[3], 2)],
            "last_swing_high": sw_h,
            "last_swing_low": sw_l,
        }

    rng1 = b1[2] - b1[3]
    smc_sweep_out: dict[str, Any] = {"signal": "none", "reason": "no_liquidity_sweep"}
    if rng1 > 0:
        lw = min(b1[1], b1[4]) - b1[3]
        uw = b1[2] - max(b1[1], b1[4])
        if sw_l is not None and b1[3] <= sw_l - 0.15 * atr_m5 and b1[4] > sw_l and lw / rng1 >= 0.35:
            smc_sweep_out = {"signal": "buy" if in_london_ny else "none", "reason": "bullish_liquidity_sweep_below_swing_low"}
        elif sw_h is not None and b1[2] >= sw_h + 0.15 * atr_m5 and b1[4] < sw_h and uw / rng1 >= 0.35:
            smc_sweep_out = {"signal": "sell" if in_london_ny else "none", "reason": "bearish_liquidity_sweep_above_swing_high"}

    # 3) Vendored day-trader-skill patterns on closed M5 bars
    closes = [b[4] for b in clean_m5]
    rsi_v = rsi_series(closes, 14)
    prev20 = closes[-21:-1]
    ma20_now = sum(closes[-20:]) / 20.0
    ma20_prev = sum(prev20) / 20.0
    tags: list[str] = []
    skill_side = "none"
    if closes[-1] > max(prev20):
        tags.append("breakout_20_bars_up")
        skill_side = "buy"
    elif closes[-1] < min(prev20):
        tags.append("breakout_20_bars_down")
        skill_side = "sell"
    if closes[-2] < ma20_prev and closes[-1] > ma20_now:
        tags.append("reclaim_ma20_up")
        skill_side = "buy"
    elif closes[-2] > ma20_prev and closes[-1] < ma20_now:
        tags.append("reclaim_ma20_down")
        skill_side = "sell"
    if rsi_v[-2] is not None and rsi_v[-2] < 30.0 and closes[-1] > closes[-2]:
        tags.append("oversold_bounce_rsi14")
    elif rsi_v[-2] is not None and rsi_v[-2] > 70.0 and closes[-1] < closes[-2]:
        tags.append("overbought_fade_rsi14")

    skill_out = {
        "signal": skill_side if in_london_ny else "none",
        "tags": tags,
        "rsi14": round(float(rsi_v[-1]), 2) if rsi_v[-1] is not None else None,
        "ma20": round(ma20_now, 2),
    }

    engines_map = {
        "m5_momentum_g5": mom_out,
        "smc_bos_fvg": smc_fvg_out,
        "smc_liquidity_sweep": smc_sweep_out,
        "daytrader_skill_patterns": skill_out,
    }
    buy_engs = [k for k, v in engines_map.items() if v.get("signal") == "buy"]
    sell_engs = [k for k, v in engines_map.items() if v.get("signal") == "sell"]
    if buy_engs and not sell_engs:
        rec = "observe_buy"
    elif sell_engs and not buy_engs:
        rec = "observe_sell"
    elif buy_engs and sell_engs:
        rec = "conflict_wait"
    else:
        rec = "wait"

    return {
        "ok": True,
        "paper": True,
        "executable": False,
        "execution_authorized": False,
        "orders_submitted": 0,
        "symbol": symbol,
        "last_closed_m5_utc": _iso_utc(last_closed[0]),
        "eval_utc": _iso_utc(eval_ts),
        "quote": {
            "bid": bid or last_closed[4],
            "ask": ask or round(last_closed[4] + spread_price, 2),
            "spread_price": spread_price,
        },
        "regime": {
            "utc_hour": utc_hour,
            "utc_session": session_label,
            "is_london_ny_session": in_london_ny,
            "m5_atr14": atr_m5,
            "m15_bias": m15_c.bias if m15_c else "unavailable",
            "m15_adx": round(m15_c.adx, 2) if (m15_c and m15_c.adx is not None) else None,
            "h1_bias": h1_c.bias if h1_c else "unavailable",
            "h1_adx": h1_adx,
            "h1_regime": _adx_bucket_label(h1_adx),
            "h4_bias": h4_c.bias if h4_c else "unavailable",
        },
        "engines": engines_map,
        "consensus": {
            "active_signals_count": len(buy_engs) + len(sell_engs),
            "aligned_buy_engines": buy_engs,
            "aligned_sell_engines": sell_engs,
            "recommended_action": rec,
            "note": "Read-only observation payload for n8n/GoldAgent logging; zero orders submitted.",
        },
    }
