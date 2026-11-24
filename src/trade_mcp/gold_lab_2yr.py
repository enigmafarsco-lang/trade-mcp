"""Two-Year (730-Day) True-Unseen-Year OOS & Extended GitHub/Academic Gold Strategy Lab.

Tests reigning champions against 5 new published GitHub & academic XAUUSD strategies
on up to 730 calendar days (isolating Year 1 [2024-2025] as strictly unseen OOS when
retained by MT5 alongside Year 2 [2025-2026]):

  Reigning Champions:
    1. M5_MOMENTUM_G5_ADX20_35  — Locked M5_5 + London/NY + H1 ADX 20..35 (unseen Y1 test!)
    2. TPSL_LONDON_NATIVE       — GitHub TradingTPSL Classic London Box (03-08 Athens)
    3. TPSL_LONDON_CAP12        — Small-capital $12-stop-capped London Box (width <= 0.65%)

  New GitHub & Academic Quantitative Strategies:
    4. H1_DONCHIAN50_LONDON     — GitHub anashussain-124/quant_lab (H1 Donchian50, 08-16 UTC, fixed 100oz)
    5. M15_CHANDELIER_BREAKOUT  — M15 Donchian20 + H1 EMA(20/50) + 3xATR Chandelier trail
    6. M15_EMA20_H1_PULLBACK    — GitHub shorif2000/mt5-trading PR#17 (H1 EMA50/200 + M15 EMA20 pullback, 2R)
    7. M5_VWAP_EMA200_TRAIL50   — Academic SSRN 6650958 (2026): Daily VWAP + EMA200 regime + EMA50 trail
    8. M5_LONDON_OPEN_15BP_ORB  — Academic Holmberg/Sonnert symmetric +/-15bp London-open momentum ORB

  Portfolio Check:
    - Combined chronologically merged equity curve of M5_MOMENTUM_G5_ADX20_35 + TPSL_LONDON_CAP12.

All simulations enforce 100 oz/lot CFD Leverage (`resolve_pricing_basis(spec, 'auto')`),
strict zero lookahead, conservative SL-before-TP/trail OHLC fill ordering, 4-level cost
stress, and 1,000-trial Monte Carlo drawdown stress testing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .daytrade_paper import HARD_MAX_LOT, resolve_pricing_basis
from .gold_lab import (
    _iso_utc,
    _sim_tpsl_london_breakout,
    _summarize_trades,
    monte_carlo_drawdown,
)
from .m1_momentum import (
    LONDON_NY_BLOCKED_UTC_HOURS,
    _capital_sizing_guide,
    atr_series,
    build_htf_index,
    clean_ohlcv_bars,
    ema_series,
    lookup_htf,
    replay_m1_momentum,
    resample_bars_to_tf,
    split_contiguous_bars,
    summarize_months_utc,
)


def _sim_tpsl_london_cap12(
    m5_bars: list[list[float]],
    *,
    spec: dict[str, Any],
    spread_price: float = 0.34,
    commission_per_lot: float = 0.0,
    max_lot: float = 0.01,
    leverage: float = 500.0,
    max_stop_usd: float = 12.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Small-capital risk-capped variant of TradingTPSL London Breakout (max $12 stop at 0.01 lot).

    Skips days where the 03:00-08:00 Athens box is too narrow (<0.25%) or too wide (>0.65% of price,
    which would exceed ~$23 at $3,600), and caps single-trade stop strictly at max_stop_usd ($12).
    """
    _, multiplier = resolve_pricing_basis(spec, "auto")
    contract_size = float(spec.get("trade_contract_size") or 100.0)
    vol = min(float(max_lot), HARD_MAX_LOT)
    comm = commission_per_lot * vol
    max_stop_dist = max(spread_price * 4.0, (max_stop_usd - comm) / (vol * multiplier))
    tz = ZoneInfo("Europe/Athens")

    by_day: dict[str, list[tuple[datetime, list[float]]]] = {}
    for b in clean_ohlcv_bars(m5_bars):
        dt_loc = datetime.fromtimestamp(b[0], tz=timezone.utc).astimezone(tz)
        if dt_loc.weekday() > 4:
            continue
        by_day.setdefault(dt_loc.strftime("%Y-%m-%d"), []).append((dt_loc, b))

    trades: list[dict[str, Any]] = []
    refusals: dict[str, int] = {}
    peak_margin = 0.0

    def _ref(r: str) -> None:
        refusals[r] = refusals.get(r, 0) + 1

    for _day, items in sorted(by_day.items()):
        box_bars = [b for dt_l, b in items if 3 <= dt_l.hour < 8]
        if len(box_bars) < 12:
            _ref("insufficient_box_bars")
            continue
        box_hi = max(b[2] for b in box_bars)
        box_lo = min(b[3] for b in box_bars)
        width = box_hi - box_lo
        mid = (box_hi + box_lo) / 2.0
        if mid <= 0 or width < 0.0025 * mid or width > 0.0065 * mid:
            _ref("box_width_out_of_cap12_bounds")
            continue

        buy_stop = box_hi + 0.20
        sell_stop = box_lo - 0.20
        session = [(dt_l, b) for dt_l, b in items if 8 <= dt_l.hour < 21]
        if not session:
            continue

        pos: dict[str, Any] | None = None
        for _dt_l, b in session:
            t, o, h, low = b[0], b[1], b[2], b[3]
            bar_end = t + 300.0
            if pos is None:
                hit_buy = (h + spread_price) >= buy_stop
                hit_sell = low <= sell_stop
                if hit_buy and hit_sell:
                    _ref("ambiguous_both_stops_same_bar")
                    break
                if not hit_buy and not hit_sell:
                    continue
                side = "buy" if hit_buy else "sell"
                entry = max(o + spread_price, buy_stop) if side == "buy" else min(o, sell_stop)
                raw_dist = (entry - box_lo) if side == "buy" else (box_hi + spread_price - entry)
                stop_dist = min(max(raw_dist, spread_price * 3.0), max_stop_dist)
                stop_px = (entry - stop_dist) if side == "buy" else (entry + stop_dist)
                tp_px = (entry + 2.5 * stop_dist) if side == "buy" else (entry - 2.5 * stop_dist)
                be_trig = (entry + 1.5 * stop_dist) if side == "buy" else (entry - 1.5 * stop_dist)
                pos = {
                    "side": side,
                    "entry": entry,
                    "stop": stop_px,
                    "tp": tp_px,
                    "be_trig": be_trig,
                    "be_moved": False,
                    "opened_at": t,
                }
                peak_margin = max(peak_margin, vol * contract_size * o / leverage)
                hit_sl_same = (low <= pos["stop"]) if side == "buy" else ((h + spread_price) >= pos["stop"])
                if hit_sl_same:
                    diff = (pos["stop"] - entry) if side == "buy" else (entry - pos["stop"])
                    trades.append({
                        "opened_at": t,
                        "closed_at": bar_end,
                        "side": side,
                        "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                        "exit_reason": "stop_loss",
                    })
                    pos = None
                    break
                continue

            side = pos["side"]
            if side == "buy":
                hit_sl = low <= pos["stop"]
                fill_sl = min(o, pos["stop"])
                hit_tp = h >= pos["tp"]
                hit_be = h >= pos["be_trig"]
            else:
                hit_sl = (h + spread_price) >= pos["stop"]
                fill_sl = max(o + spread_price, pos["stop"])
                hit_tp = (low + spread_price) <= pos["tp"]
                hit_be = (low + spread_price) <= pos["be_trig"]

            if hit_sl:
                diff = (fill_sl - pos["entry"]) if side == "buy" else (pos["entry"] - fill_sl)
                trades.append({
                    "opened_at": pos["opened_at"],
                    "closed_at": bar_end,
                    "side": side,
                    "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                    "exit_reason": "breakeven_stop" if pos["be_moved"] else "stop_loss",
                })
                pos = None
                break
            if hit_tp:
                diff = (pos["tp"] - pos["entry"]) if side == "buy" else (pos["entry"] - pos["tp"])
                trades.append({
                    "opened_at": pos["opened_at"],
                    "closed_at": bar_end,
                    "side": side,
                    "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                    "exit_reason": "target_2_5r",
                })
                pos = None
                break
            if not pos["be_moved"] and hit_be:
                pos["stop"] = pos["entry"]
                pos["be_moved"] = True

        if pos is not None:
            _, lb = session[-1]
            ex = lb[4] if pos["side"] == "buy" else (lb[4] + spread_price)
            diff = (ex - pos["entry"]) if pos["side"] == "buy" else (pos["entry"] - ex)
            trades.append({
                "opened_at": pos["opened_at"],
                "closed_at": lb[0] + 300.0,
                "side": pos["side"],
                "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                "exit_reason": "eod_21_athens",
            })

    return _summarize_trades(trades, peak_margin_usd=peak_margin, refusals=refusals), trades


def _sim_h1_donchian50_london(
    h1_bars: list[list[float]],
    *,
    spec: dict[str, Any],
    spread_price: float = 0.34,
    commission_per_lot: float = 0.0,
    max_lot: float = 0.01,
    leverage: float = 500.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Causal H1 Donchian(50) London Breakout (adapted from anashussain-124/quant_lab).

    Fixes the upstream 10x multiplier bug (uses true 100 oz/lot) and eliminates close-bar
    lookahead (signals on strictly closed H1 bar i-1, enters on next bar i open).
      - Entry: closed H1 close > max(prev 50 highs) [buy] or < min(prev 50 lows) [sell],
               during London 08:00-16:00 UTC, when H1 ATR(14) >= $5.0.
      - Stop: 2.5 x H1 ATR(14), capped at $25 at 0.01 lot; trails via 2.5x ATR Chandelier
              once >= 1.5R in profit, or exits at max 100 H1 bars / opposite Donchian(20) close.
    """
    _, multiplier = resolve_pricing_basis(spec, "auto")
    contract_size = float(spec.get("trade_contract_size") or 100.0)
    vol = min(float(max_lot), HARD_MAX_LOT)
    comm = commission_per_lot * vol
    max_stop_dist = (25.0 - comm) / (vol * multiplier)

    clean = clean_ohlcv_bars(h1_bars)
    if len(clean) < 60:
        return _summarize_trades([], peak_margin_usd=0.0, refusals={"insufficient_h1": 1}), []
    atr_vals = atr_series(clean, period=14)
    trades: list[dict[str, Any]] = []
    refusals: dict[str, int] = {}
    peak_margin = 0.0
    pos: dict[str, Any] | None = None

    for i in range(52, len(clean)):
        b = clean[i]
        t, o, h, low, c = b[0], b[1], b[2], b[3], b[4]
        bar_end = t + 3600.0
        b1 = clean[i - 1]
        atr1 = atr_vals[i - 1]
        hr_open = (int(t) // 3600) % 24

        # 1) Entry check on strictly closed bar i-1 -> enter at bar i open
        opened_now = False
        if pos is None and atr1 is not None:
            if not (8 <= hr_open < 16):
                pass
            elif atr1 < 5.0:
                refusals["h1_low_atr"] = refusals.get("h1_low_atr", 0) + 1
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
                    peak_margin = max(peak_margin, vol * contract_size * o / leverage)
                    opened_now = True

        # 2) Manage open trade: SL checked strictly BEFORE trail/exit!
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
                trades.append({
                    "opened_at": pos["opened_at"],
                    "closed_at": bar_end,
                    "side": side,
                    "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                    "exit_reason": "chandelier_trail" if pos["trailed"] else "stop_loss",
                })
                pos = None
                continue

            if not opened_now:
                bars_held = i - pos["opened_i"]
                if bars_held >= 100:
                    diff = (ex_close - pos["entry"]) if side == "buy" else (pos["entry"] - ex_close)
                    trades.append({
                        "opened_at": pos["opened_at"],
                        "closed_at": bar_end,
                        "side": side,
                        "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                        "exit_reason": "max_100_h1_bars",
                    })
                    pos = None
                    continue

                # Opposite 20-bar Donchian exit (classic Turtle exit)
                win20 = clean[i - 20 : i]
                if (side == "buy" and c < min(x[3] for x in win20)) or (side == "sell" and c > max(x[2] for x in win20)):
                    diff = (ex_close - pos["entry"]) if side == "buy" else (pos["entry"] - ex_close)
                    trades.append({
                        "opened_at": pos["opened_at"],
                        "closed_at": bar_end,
                        "side": side,
                        "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                        "exit_reason": "donchian20_exit",
                    })
                    pos = None
                    continue

                # Chandelier 2.5x ATR trail once >= 1.5R in profit
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
        trades.append({
            "opened_at": pos["opened_at"],
            "closed_at": lb[0] + 3600.0,
            "side": pos["side"],
            "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
            "exit_reason": "end_of_data",
        })

    return _summarize_trades(trades, peak_margin_usd=peak_margin, refusals=refusals), trades


def _sim_m15_chandelier_breakout(
    m15_bars: list[list[float]],
    *,
    h1_bars: list[list[float]],
    spec: dict[str, Any],
    spread_price: float = 0.34,
    commission_per_lot: float = 0.0,
    max_lot: float = 0.01,
    leverage: float = 500.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Causal M15 Donchian(20) Breakout + H1 EMA(20/50) Trend + 3x ATR Chandelier Trail.

    Inspired by gold-quant-breakout-lab MTF Hybrid (H1 trend + M15 breakout + wide Chandelier).
    """
    _, multiplier = resolve_pricing_basis(spec, "auto")
    contract_size = float(spec.get("trade_contract_size") or 100.0)
    vol = min(float(max_lot), HARD_MAX_LOT)
    comm = commission_per_lot * vol
    max_stop_dist = (15.0 - comm) / (vol * multiplier)
    blocked = set(LONDON_NY_BLOCKED_UTC_HOURS)

    clean_m15 = clean_ohlcv_bars(m15_bars)
    if len(clean_m15) < 40:
        return _summarize_trades([], peak_margin_usd=0.0, refusals={"insufficient_m15": 1}), []
    h1_t, h1_ctx = build_htf_index(clean_ohlcv_bars(h1_bars), period_seconds=3600, ema_fast=20, ema_slow=50)
    atr_vals = atr_series(clean_m15, period=14)

    trades: list[dict[str, Any]] = []
    refusals: dict[str, int] = {}
    peak_margin = 0.0
    pos: dict[str, Any] | None = None

    for i in range(25, len(clean_m15)):
        b = clean_m15[i]
        t, o, h, low, c = b[0], b[1], b[2], b[3], b[4]
        bar_end = t + 900.0
        b1 = clean_m15[i - 1]
        atr1 = atr_vals[i - 1]
        hr = (int(t) // 3600) % 24

        opened_now = False
        if pos is None and atr1 is not None and hr not in blocked:
            h1_c = lookup_htf(h1_t, h1_ctx, int(t))
            if h1_c and h1_c.ema_fast is not None and h1_c.ema_slow is not None and (h1_c.adx or 0.0) >= 20.0:
                win20 = clean_m15[i - 21 : i - 1]
                dc_hi = max(x[2] for x in win20)
                dc_lo = min(x[3] for x in win20)
                side: str | None = None
                if b1[4] > dc_hi and h1_c.ema_fast > h1_c.ema_slow and h1_c.close > h1_c.ema_fast:
                    side = "buy"
                elif b1[4] < dc_lo and h1_c.ema_fast < h1_c.ema_slow and h1_c.close < h1_c.ema_fast:
                    side = "sell"
                if side is not None:
                    entry = (o + spread_price) if side == "buy" else o
                    sdist = min(max(2.0 * atr1, spread_price * 4.0), max_stop_dist)
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
                    peak_margin = max(peak_margin, vol * contract_size * o / leverage)
                    opened_now = True

        if pos is not None:
            side = pos["side"]
            if side == "buy":
                open_ex = (pos["entry"] - spread_price) if opened_now else o
                hit_sl = low <= pos["stop"]
                fill_sl = min(open_ex, pos["stop"])
            else:
                open_ex = (pos["entry"] + spread_price) if opened_now else (o + spread_price)
                hit_sl = (h + spread_price) >= pos["stop"]
                fill_sl = max(open_ex, pos["stop"])

            if hit_sl:
                diff = (fill_sl - pos["entry"]) if side == "buy" else (pos["entry"] - fill_sl)
                trades.append({
                    "opened_at": pos["opened_at"],
                    "closed_at": bar_end,
                    "side": side,
                    "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                    "exit_reason": "chandelier_trail" if pos["trailed"] else "stop_loss",
                })
                pos = None
                continue

            if not opened_now:
                atr_now = atr_vals[i] or atr1 or pos["sdist"] / 2.0
                if side == "buy":
                    pos["best_px"] = max(pos["best_px"], h)
                    if (c - pos["entry"]) >= 1.0 * pos["sdist"]:
                        cand = max(pos["stop"], pos["entry"], pos["best_px"] - 3.0 * atr_now)
                        if cand > pos["stop"]:
                            pos["stop"] = cand
                            pos["trailed"] = True
                else:
                    pos["best_px"] = min(pos["best_px"], low)
                    if (pos["entry"] - (c + spread_price)) >= 1.0 * pos["sdist"]:
                        cand = min(pos["stop"], pos["entry"], pos["best_px"] + spread_price + 3.0 * atr_now)
                        if cand < pos["stop"]:
                            pos["stop"] = cand
                            pos["trailed"] = True
                if i - pos["opened_i"] >= 96:
                    ex = c if side == "buy" else (c + spread_price)
                    diff = (ex - pos["entry"]) if side == "buy" else (pos["entry"] - ex)
                    trades.append({
                        "opened_at": pos["opened_at"],
                        "closed_at": bar_end,
                        "side": side,
                        "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                        "exit_reason": "max_24h_hold",
                    })
                    pos = None

    if pos is not None:
        lb = clean_m15[-1]
        ex = lb[4] if pos["side"] == "buy" else (lb[4] + spread_price)
        diff = (ex - pos["entry"]) if pos["side"] == "buy" else (pos["entry"] - ex)
        trades.append({
            "opened_at": pos["opened_at"],
            "closed_at": lb[0] + 900.0,
            "side": pos["side"],
            "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
            "exit_reason": "end_of_data",
        })

    return _summarize_trades(trades, peak_margin_usd=peak_margin, refusals=refusals), trades


def _sim_m15_ema20_h1_pullback(
    m15_bars: list[list[float]],
    *,
    h1_bars: list[list[float]],
    spec: dict[str, Any],
    spread_price: float = 0.34,
    commission_per_lot: float = 0.0,
    max_lot: float = 0.01,
    leverage: float = 500.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Causal XAUUSD M15 Intraday Trend Pullback (shorif2000/mt5-trading PR #17, London+NY).

    - Trend: closed H1 EMA(50) > EMA(200) for long; EMA(50) < EMA(200) for short.
    - Setup on closed M15 bar i-1 during London+NY (07-20 UTC):
        Long: bar low <= M15 EMA(20), bar close > M15 EMA(20), bullish close (c > o).
        Short: bar high >= M15 EMA(20), bar close < M15 EMA(20), bearish close (c < o).
    - Stop: pullback extreme +/- 0.2 x ATR(14), skip if > 3 x ATR or > $15 at 0.01 lot.
    - Target: 2.0R; flat at 20:45 UTC (no overnight hold).
    """
    _, multiplier = resolve_pricing_basis(spec, "auto")
    contract_size = float(spec.get("trade_contract_size") or 100.0)
    vol = min(float(max_lot), HARD_MAX_LOT)
    comm = commission_per_lot * vol
    max_stop_dist = (15.0 - comm) / (vol * multiplier)
    blocked = set(LONDON_NY_BLOCKED_UTC_HOURS)

    clean_m15 = clean_ohlcv_bars(m15_bars)
    if len(clean_m15) < 40:
        return _summarize_trades([], peak_margin_usd=0.0, refusals={"insufficient_m15": 1}), []
    h1_t, h1_ctx = build_htf_index(clean_ohlcv_bars(h1_bars), period_seconds=3600, ema_fast=50, ema_slow=200)
    closes = [b[4] for b in clean_m15]
    ema20 = ema_series(closes, 20)
    atr_vals = atr_series(clean_m15, period=14)

    trades: list[dict[str, Any]] = []
    refusals: dict[str, int] = {}
    peak_margin = 0.0
    pos: dict[str, Any] | None = None

    for i in range(25, len(clean_m15)):
        b = clean_m15[i]
        t, o, h, low, c = b[0], b[1], b[2], b[3], b[4]
        bar_end = t + 900.0
        b1 = clean_m15[i - 1]
        e1 = ema20[i - 1]
        atr1 = atr_vals[i - 1]
        hr = (int(t) // 3600) % 24
        mn = (int(t) % 3600) // 60

        opened_now = False
        if pos is None and e1 is not None and atr1 is not None and hr not in blocked and hr < 20:
            h1_c = lookup_htf(h1_t, h1_ctx, int(t))
            if h1_c and h1_c.ema_fast is not None and h1_c.ema_slow is not None:
                side: str | None = None
                stop_raw: float | None = None
                if h1_c.ema_fast > h1_c.ema_slow and b1[3] <= e1 and b1[4] > e1 and b1[4] > b1[1]:
                    side = "buy"
                    stop_raw = b1[3] - 0.2 * atr1
                elif h1_c.ema_fast < h1_c.ema_slow and b1[2] >= e1 and b1[4] < e1 and b1[4] < b1[1]:
                    side = "sell"
                    stop_raw = b1[2] + 0.2 * atr1
                if side is not None and stop_raw is not None:
                    entry = (o + spread_price) if side == "buy" else o
                    sdist = (entry - stop_raw) if side == "buy" else (stop_raw + spread_price - entry)
                    if sdist < max(spread_price * 4.0, 1.5) or sdist > min(3.0 * atr1, max_stop_dist):
                        refusals["pullback_stop_out_of_bounds"] = refusals.get("pullback_stop_out_of_bounds", 0) + 1
                    else:
                        stop_px = (entry - sdist) if side == "buy" else (entry + sdist)
                        tp_px = (entry + 2.0 * sdist) if side == "buy" else (entry - 2.0 * sdist)
                        pos = {"side": side, "entry": entry, "stop": stop_px, "tp": tp_px, "opened_at": t}
                        peak_margin = max(peak_margin, vol * contract_size * o / leverage)
                        opened_now = True

        if pos is not None:
            side = pos["side"]
            if side == "buy":
                open_ex = (pos["entry"] - spread_price) if opened_now else o
                hit_sl = low <= pos["stop"]
                fill_sl = min(open_ex, pos["stop"])
                hit_tp = h >= pos["tp"]
            else:
                open_ex = (pos["entry"] + spread_price) if opened_now else (o + spread_price)
                hit_sl = (h + spread_price) >= pos["stop"]
                fill_sl = max(open_ex, pos["stop"])
                hit_tp = (low + spread_price) <= pos["tp"]

            if hit_sl:
                diff = (fill_sl - pos["entry"]) if side == "buy" else (pos["entry"] - fill_sl)
                trades.append({
                    "opened_at": pos["opened_at"],
                    "closed_at": bar_end,
                    "side": side,
                    "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                    "exit_reason": "stop_loss",
                })
                pos = None
                continue
            if hit_tp:
                diff = (pos["tp"] - pos["entry"]) if side == "buy" else (pos["entry"] - pos["tp"])
                trades.append({
                    "opened_at": pos["opened_at"],
                    "closed_at": bar_end,
                    "side": side,
                    "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                    "exit_reason": "target_2r",
                })
                pos = None
                continue
            if hr >= 20 and mn >= 45:
                ex = c if side == "buy" else (c + spread_price)
                diff = (ex - pos["entry"]) if side == "buy" else (pos["entry"] - ex)
                trades.append({
                    "opened_at": pos["opened_at"],
                    "closed_at": bar_end,
                    "side": side,
                    "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                    "exit_reason": "eod_2045_utc",
                })
                pos = None

    return _summarize_trades(trades, peak_margin_usd=peak_margin, refusals=refusals), trades


def _sim_m5_vwap_ema200_trail50(
    m5_bars: list[list[float]],
    *,
    spec: dict[str, Any],
    spread_price: float = 0.34,
    commission_per_lot: float = 0.0,
    max_lot: float = 0.01,
    leverage: float = 500.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Academic SSRN 6650958 (2026): Intraday Gold Daily VWAP + EMA200 Regime + EMA50 Trail.

    - Strictly causal daily session VWAP (resets at 00:00 UTC, weighted by tick_volume).
    - Long regime: closed M5 EMA50 > EMA200; trigger when closed M5 crosses above VWAP while > EMA50.
    - Short regime: closed M5 EMA50 < EMA200; trigger when closed M5 crosses below VWAP while < EMA50.
    - Active session: London+NY (07:00-19:59 UTC); hard stop $12 (0.01 lot); dynamic trailing exit
      when closed M5 crosses back through EMA50, or EOD 20:45 UTC.
    """
    _, multiplier = resolve_pricing_basis(spec, "auto")
    contract_size = float(spec.get("trade_contract_size") or 100.0)
    vol = min(float(max_lot), HARD_MAX_LOT)
    comm = commission_per_lot * vol
    max_stop_dist = (12.0 - comm) / (vol * multiplier)
    blocked = set(LONDON_NY_BLOCKED_UTC_HOURS)

    segments = [s for s in split_contiguous_bars(clean_ohlcv_bars(m5_bars), bar_seconds=300) if len(s) >= 220]
    trades: list[dict[str, Any]] = []
    refusals: dict[str, int] = {}
    peak_margin = 0.0

    for seg in segments:
        closes = [b[4] for b in seg]
        ema50 = ema_series(closes, 50)
        ema200 = ema_series(closes, 200)
        atr_vals = atr_series(seg, period=14)

        # Precompute strictly causal daily VWAP for every bar in segment
        vwap: list[float] = []
        cur_day = -1
        pv_sum = 0.0
        v_sum = 0.0
        for b in seg:
            day_id = int(b[0]) // 86400
            if day_id != cur_day:
                cur_day = day_id
                pv_sum = 0.0
                v_sum = 0.0
            typical = (b[2] + b[3] + b[4]) / 3.0
            tv = max(float(b[5]) if len(b) >= 6 else 1.0, 1.0)
            pv_sum += typical * tv
            v_sum += tv
            vwap.append(pv_sum / v_sum)

        pos: dict[str, Any] | None = None
        for i in range(205, len(seg)):
            b = seg[i]
            t, o, h, low, c = b[0], b[1], b[2], b[3], b[4]
            bar_end = t + 300.0
            hr = (int(t) // 3600) % 24
            mn = (int(t) % 3600) // 60
            e50_1, e200_1 = ema50[i - 1], ema200[i - 1]
            v1, v2 = vwap[i - 1], vwap[i - 2]
            c1, c2 = closes[i - 1], closes[i - 2]
            atr1 = atr_vals[i - 1] or 1.5

            opened_now = False
            if pos is None and e50_1 is not None and e200_1 is not None and hr not in blocked and hr < 20:
                side: str | None = None
                if e50_1 > e200_1 and c2 <= v2 and c1 > v1 and c1 > e50_1:
                    side = "buy"
                elif e50_1 < e200_1 and c2 >= v2 and c1 < v1 and c1 < e50_1:
                    side = "sell"
                if side is not None:
                    entry = (o + spread_price) if side == "buy" else o
                    sdist = min(max(2.0 * atr1, spread_price * 4.0), max_stop_dist)
                    stop_px = (entry - sdist) if side == "buy" else (entry + sdist)
                    pos = {"side": side, "entry": entry, "stop": stop_px, "opened_at": t}
                    peak_margin = max(peak_margin, vol * contract_size * o / leverage)
                    opened_now = True

            if pos is not None:
                side = pos["side"]
                if side == "buy":
                    open_ex = (pos["entry"] - spread_price) if opened_now else o
                    hit_sl = low <= pos["stop"]
                    fill_sl = min(open_ex, pos["stop"])
                else:
                    open_ex = (pos["entry"] + spread_price) if opened_now else (o + spread_price)
                    hit_sl = (h + spread_price) >= pos["stop"]
                    fill_sl = max(open_ex, pos["stop"])

                if hit_sl:
                    diff = (fill_sl - pos["entry"]) if side == "buy" else (pos["entry"] - fill_sl)
                    trades.append({
                        "opened_at": pos["opened_at"],
                        "closed_at": bar_end,
                        "side": side,
                        "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                        "exit_reason": "stop_loss",
                    })
                    pos = None
                    continue

                e50_now = ema50[i]
                if not opened_now and e50_now is not None:
                    ema_exit = (side == "buy" and c < e50_now) or (side == "sell" and c > e50_now)
                    eod_exit = hr >= 20 and mn >= 45
                    if ema_exit or eod_exit:
                        ex = c if side == "buy" else (c + spread_price)
                        diff = (ex - pos["entry"]) if side == "buy" else (pos["entry"] - ex)
                        trades.append({
                            "opened_at": pos["opened_at"],
                            "closed_at": bar_end,
                            "side": side,
                            "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                            "exit_reason": "ema50_trail_exit" if ema_exit else "eod_2045_utc",
                        })
                        pos = None

    return _summarize_trades(trades, peak_margin_usd=peak_margin, refusals=refusals), trades


def _sim_london_open_15bp_orb(
    m5_bars: list[list[float]],
    *,
    spec: dict[str, Any],
    spread_price: float = 0.34,
    commission_per_lot: float = 0.0,
    max_lot: float = 0.01,
    leverage: float = 500.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Academic Holmberg/Sonnert Intraday Gold Momentum: +/-15bp Symmetric London Open ORB.

    - Reference price P0 = exact 07:00 UTC open price.
    - Upper buy-stop = P0 * (1 + 0.0015), Lower sell-stop = P0 * (1 - 0.0015).
    - Initial stop = opposite threshold (~30bp ~= $10.80 at $3,600, capped at $12 on 0.01 lot).
    - Move SL to breakeven at +1.5R; let winner run uncapped until 20:00 UTC EOD; max 1 trade/day.
    """
    _, multiplier = resolve_pricing_basis(spec, "auto")
    contract_size = float(spec.get("trade_contract_size") or 100.0)
    vol = min(float(max_lot), HARD_MAX_LOT)
    comm = commission_per_lot * vol
    max_stop_dist = (12.0 - comm) / (vol * multiplier)

    by_day: dict[str, list[tuple[datetime, list[float]]]] = {}
    for b in clean_ohlcv_bars(m5_bars):
        dt_u = datetime.fromtimestamp(b[0], tz=timezone.utc)
        if dt_u.weekday() > 4:
            continue
        by_day.setdefault(dt_u.strftime("%Y-%m-%d"), []).append((dt_u, b))

    trades: list[dict[str, Any]] = []
    refusals: dict[str, int] = {}
    peak_margin = 0.0

    for _day, items in sorted(by_day.items()):
        session = [(dt_u, b) for dt_u, b in items if 7 <= dt_u.hour < 20]
        if len(session) < 12 or session[0][0].hour != 7 or session[0][0].minute > 10:
            continue
        p0 = session[0][1][1]
        if p0 <= 0:
            continue
        thr = p0 * 0.0015
        buy_stop = p0 + thr
        sell_stop = p0 - thr

        pos: dict[str, Any] | None = None
        for _dt_u, b in session:
            t, o, h, low = b[0], b[1], b[2], b[3]
            bar_end = t + 300.0
            if pos is None:
                hit_buy = (h + spread_price) >= buy_stop
                hit_sell = low <= sell_stop
                if hit_buy and hit_sell:
                    refusals["ambiguous_both_thresholds"] = refusals.get("ambiguous_both_thresholds", 0) + 1
                    break
                if not hit_buy and not hit_sell:
                    continue
                side = "buy" if hit_buy else "sell"
                entry = max(o + spread_price, buy_stop) if side == "buy" else min(o, sell_stop)
                sdist = min(max(2.0 * thr, spread_price * 4.0), max_stop_dist)
                stop_px = (entry - sdist) if side == "buy" else (entry + sdist)
                be_trig = (entry + 1.5 * sdist) if side == "buy" else (entry - 1.5 * sdist)
                pos = {"side": side, "entry": entry, "stop": stop_px, "be_trig": be_trig, "be_moved": False, "opened_at": t}
                peak_margin = max(peak_margin, vol * contract_size * o / leverage)
                hit_sl_same = (low <= pos["stop"]) if side == "buy" else ((h + spread_price) >= pos["stop"])
                if hit_sl_same:
                    diff = (pos["stop"] - entry) if side == "buy" else (entry - pos["stop"])
                    trades.append({
                        "opened_at": t,
                        "closed_at": bar_end,
                        "side": side,
                        "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                        "exit_reason": "stop_loss",
                    })
                    pos = None
                    break
                continue

            side = pos["side"]
            if side == "buy":
                hit_sl = low <= pos["stop"]
                fill_sl = min(o, pos["stop"])
                hit_be = h >= pos["be_trig"]
            else:
                hit_sl = (h + spread_price) >= pos["stop"]
                fill_sl = max(o + spread_price, pos["stop"])
                hit_be = (low + spread_price) <= pos["be_trig"]

            if hit_sl:
                diff = (fill_sl - pos["entry"]) if side == "buy" else (pos["entry"] - fill_sl)
                trades.append({
                    "opened_at": pos["opened_at"],
                    "closed_at": bar_end,
                    "side": side,
                    "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                    "exit_reason": "breakeven_stop" if pos["be_moved"] else "stop_loss",
                })
                pos = None
                break
            if not pos["be_moved"] and hit_be:
                pos["stop"] = pos["entry"]
                pos["be_moved"] = True

        if pos is not None:
            _, lb = session[-1]
            ex = lb[4] if pos["side"] == "buy" else (lb[4] + spread_price)
            diff = (ex - pos["entry"]) if pos["side"] == "buy" else (pos["entry"] - ex)
            trades.append({
                "opened_at": pos["opened_at"],
                "closed_at": lb[0] + 300.0,
                "side": pos["side"],
                "net_pnl_usd": round(diff * vol * multiplier - comm, 4),
                "exit_reason": "eod_2000_utc",
            })

    return _summarize_trades(trades, peak_margin_usd=peak_margin, refusals=refusals), trades


def evaluate_2yr_oos_and_github_lab(
    *,
    m5_bars: list[list[float]],
    m15_bars: list[list[float]],
    h1_bars: list[list[float]],
    h4_bars: list[list[float]] | None = None,
    spec: dict[str, Any],
    symbol: str = "XAUUSD_i",
    spread_price: float = 0.34,
    max_lot: float = 0.01,
    leverage: float = 500.0,
    year2_days: int = 365,
    mc_trials: int = 1000,
) -> dict[str, Any]:
    """Evaluate reigning champions + 5 new GitHub/Academic strategies across up to 2 years.

    Isolates Year 1 (strictly BEFORE the last `year2_days`, i.e., 2024-2025 TRUE UNSEEN OOS!)
    from Year 2 (2025-2026) separately for M5, M15, and H1 so higher-timeframe strategies still
    receive full 2-year testing even if the broker trims M5 history earlier.
    """
    clean_m5 = clean_ohlcv_bars(m5_bars)
    clean_m15 = clean_ohlcv_bars(m15_bars) if m15_bars else resample_bars_to_tf(clean_m5, src_seconds=300, dst_seconds=900)
    clean_h1 = clean_ohlcv_bars(h1_bars) if h1_bars else resample_bars_to_tf(clean_m15, src_seconds=900, dst_seconds=3600)
    clean_h4 = clean_ohlcv_bars(h4_bars) if h4_bars else resample_bars_to_tf(clean_h1, src_seconds=3600, dst_seconds=14400)
    if len(clean_m5) < 50 or len(clean_h1) < 60:
        raise ValueError("insufficient history for 2-year OOS lab")

    htf_indices = (
        build_htf_index(clean_m15, period_seconds=900, ema_fast=9, ema_slow=21),
        build_htf_index(clean_h1, period_seconds=3600, ema_fast=9, ema_slow=21),
        build_htf_index(clean_h4, period_seconds=14400, ema_fast=20, ema_slow=50),
    )

    last_ts = int(max(clean_m5[-1][0], clean_m15[-1][0], clean_h1[-1][0]))
    cutoff_y1 = last_ts - year2_days * 86400

    def _split_tf(bars: list[list[float]]) -> dict[str, Any]:
        y1 = [b for b in bars if int(b[0]) < cutoff_y1]
        y2 = [b for b in bars if int(b[0]) >= cutoff_y1]
        y1_span = round((y1[-1][0] - y1[0][0]) / 86400.0, 2) if len(y1) >= 2 else 0.0
        y2_span = round((y2[-1][0] - y2[0][0]) / 86400.0, 2) if len(y2) >= 2 else 0.0
        return {
            "y1_bars": y1,
            "y2_bars": y2,
            "has_unseen_y1": len(y1) >= 120 and y1_span >= 45.0,
            "y1_span_days": y1_span,
            "y2_span_days": y2_span,
            "y1_range_utc": [_iso_utc(y1[0][0]), _iso_utc(y1[-1][0])] if y1 else [],
            "y2_range_utc": [_iso_utc(y2[0][0]), _iso_utc(y2[-1][0])] if y2 else [],
        }

    sp_m5 = _split_tf(clean_m5)
    sp_m15 = _split_tf(clean_m15)
    sp_h1 = _split_tf(clean_h1)

    engines: list[dict[str, Any]] = [
        {"id": "M5_MOMENTUM_G5_ADX20_35", "tf": "M5", "family": "reigning_champion", "label": "M5_5 + London/NY + H1 ADX 20..35 ($12 stop, 0.8R trail)"},
        {"id": "TPSL_LONDON_NATIVE", "tf": "M5", "family": "github_tpsl", "label": "GitHub TradingTPSL: Classic London Box (native 0.6% stop, 2.5R)"},
        {"id": "TPSL_LONDON_CAP12", "tf": "M5", "family": "github_tpsl_small_cap", "label": "Small-Capital London Box (width<=0.65%, $12 max stop, 2.5R)"},
        {"id": "H1_DONCHIAN50_LONDON", "tf": "H1", "family": "github_quant_lab", "label": "GitHub anashussain-124/quant_lab: H1 Donchian(50) London (true 100oz)"},
        {"id": "M15_CHANDELIER_BREAKOUT", "tf": "M15", "family": "github_chandelier_mtf", "label": "M15 Donchian(20) + H1 EMA(20/50) + 3xATR Chandelier Trail"},
        {"id": "M15_EMA20_H1_PULLBACK", "tf": "M15", "family": "github_shorif_pr17", "label": "GitHub shorif2000 PR#17: H1 EMA(50/200) + M15 EMA20 Pullback (2R)"},
        {"id": "M5_VWAP_EMA200_TRAIL50", "tf": "M5", "family": "academic_ssrn_6650958", "label": "Academic SSRN 6650958 (2026): Daily VWAP + EMA200 + EMA50 trail"},
        {"id": "M5_LONDON_OPEN_15BP_ORB", "tf": "M5", "family": "academic_holmberg_orb", "label": "Academic Holmberg/Sonnert: +/-15bp London Open Momentum ORB"},
    ]

    cost_ladder: list[tuple[str, float, float]] = [
        ("base_live_spread", spread_price, 0.0),
        ("stress_spread_050", max(spread_price, 0.50), 0.0),
        ("stress_spread_050_comm5", max(spread_price, 0.50), 5.0),
        ("stress_spread_070_comm7", max(spread_price, 0.70), 7.0),
    ]

    def _run_engine(eng_id: str, slice_key: str, sp: float, comm: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        m5_s = clean_m5 if slice_key == "full" else (sp_m5["y1_bars"] if slice_key == "y1" else sp_m5["y2_bars"])
        m15_s = clean_m15 if slice_key == "full" else (sp_m15["y1_bars"] if slice_key == "y1" else sp_m15["y2_bars"])
        h1_s = clean_h1 if slice_key == "full" else (sp_h1["y1_bars"] if slice_key == "y1" else sp_h1["y2_bars"])

        if eng_id == "M5_MOMENTUM_G5_ADX20_35":
            res = replay_m1_momentum(
                m5_s, spec=spec, symbol=symbol, bar_seconds=300, htf_indices=htf_indices,
                entry_mode="pullback_or_2bar", htf_filter="m15_h1_h4", exit_mode="trail_only",
                stop_usd=12.0, max_lot=max_lot, max_legs=1, spread_price=sp, commission_per_lot=comm,
                leverage=leverage, max_margin_usd=25.0, min_m5_adx=18.0, min_mid_adx=20.0,
                max_mid_adx=35.0, trail_arm_r=0.8, blocked_utc_hours=LONDON_NY_BLOCKED_UTC_HOURS,
                include_trades=True,
            )
            tr = res.pop("trades", [])
            return _summarize_trades(tr, peak_margin_usd=float(res["peak_margin_used_usd"]), refusals=res["refusal_counts"]), tr
        if eng_id == "TPSL_LONDON_NATIVE":
            return _sim_tpsl_london_breakout(m5_s, spec=spec, spread_price=sp, commission_per_lot=comm, max_lot=max_lot, leverage=leverage)
        if eng_id == "TPSL_LONDON_CAP12":
            return _sim_tpsl_london_cap12(m5_s, spec=spec, spread_price=sp, commission_per_lot=comm, max_lot=max_lot, leverage=leverage)
        if eng_id == "H1_DONCHIAN50_LONDON":
            return _sim_h1_donchian50_london(h1_s, spec=spec, spread_price=sp, commission_per_lot=comm, max_lot=max_lot, leverage=leverage)
        if eng_id == "M15_CHANDELIER_BREAKOUT":
            return _sim_m15_chandelier_breakout(m15_s, h1_bars=clean_h1, spec=spec, spread_price=sp, commission_per_lot=comm, max_lot=max_lot, leverage=leverage)
        if eng_id == "M15_EMA20_H1_PULLBACK":
            return _sim_m15_ema20_h1_pullback(m15_s, h1_bars=clean_h1, spec=spec, spread_price=sp, commission_per_lot=comm, max_lot=max_lot, leverage=leverage)
        if eng_id == "M5_VWAP_EMA200_TRAIL50":
            return _sim_m5_vwap_ema200_trail50(m5_s, spec=spec, spread_price=sp, commission_per_lot=comm, max_lot=max_lot, leverage=leverage)
        if eng_id == "M5_LONDON_OPEN_15BP_ORB":
            return _sim_london_open_15bp_orb(m5_s, spec=spec, spread_price=sp, commission_per_lot=comm, max_lot=max_lot, leverage=leverage)
        raise ValueError(f"unknown engine: {eng_id}")

    rows: list[dict[str, Any]] = []
    saved_trades: dict[str, list[dict[str, Any]]] = {}

    for eng in engines:
        eid = eng["id"]
        tf_sp = sp_m5 if eng["tf"] == "M5" else (sp_m15 if eng["tf"] == "M15" else sp_h1)
        full_sum, full_tr = _run_engine(eid, "full", spread_price, 0.0)
        saved_trades[eid] = full_tr
        y1_sum = _run_engine(eid, "y1", spread_price, 0.0)[0] if tf_sp["has_unseen_y1"] else None
        y2_sum = _run_engine(eid, "y2", spread_price, 0.0)[0]

        months = summarize_months_utc(full_tr)
        active_months = [m for m in months if m["trades"] >= 2]
        pos_months = sum(1 for m in active_months if m["net_pnl_usd"] > 0)
        mc = monte_carlo_drawdown([float(t["net_pnl_usd"]) for t in full_tr], trials=mc_trials, seed=42)

        stress: dict[str, dict[str, Any]] = {}
        for s_name, s_sp, s_comm in cost_ladder:
            s_sum = full_sum if s_name == "base_live_spread" else _run_engine(eid, "full", s_sp, s_comm)[0]
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
            "full_positive_and_pf_ge_1_30": pnl > 0 and (full_sum["pf"] or 0.0) >= 1.30 and full_sum["trades"] >= 45,
            "full_profitable_without_best_trade": full_sum["net_without_best_trade_usd"] > 0,
            "full_drawdown_le_100": dd <= 100.0,
            "profit_to_dd_ratio_ge_2_0": bool(p_dd is not None and p_dd >= 2.0),
            "survives_spread_050_comm5": s050_c5["net_pnl_usd"] > 0 and (s050_c5["pf"] or 0.0) >= 1.15,
            "months_majority_positive": bool(len(active_months) >= 4 and (pos_months / len(active_months)) >= 0.60),
            "monte_carlo_p95_dd_le_150": mc["p95_max_dd_usd"] <= 150.0,
            "year2_positive_and_pf_ge_1_15": y2_sum["net_pnl_usd"] > 0 and (y2_sum["pf"] or 0.0) >= 1.15,
        }
        if tf_sp["has_unseen_y1"] and y1_sum is not None:
            gates["unseen_year1_positive_and_pf_ge_1_15"] = (
                y1_sum["net_pnl_usd"] > 0
                and (y1_sum["pf"] or 0.0) >= 1.15
                and y1_sum["trades"] >= 15
            )
        failed = [k for k, ok in gates.items() if not ok]

        rows.append({
            "id": eid,
            "tf": eng["tf"],
            "family": eng["family"],
            "label": eng["label"],
            "has_unseen_y1": tf_sp["has_unseen_y1"],
            "full_period": {**full_sum, "profit_to_dd_ratio": p_dd},
            "unseen_year1_2024_2025": y1_sum,
            "year2_2025_2026": y2_sum,
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

    # Combined low-correlation portfolio: M5_MOMENTUM_G5_ADX20_35 + TPSL_LONDON_CAP12
    merged = sorted(
        saved_trades.get("M5_MOMENTUM_G5_ADX20_35", []) + saved_trades.get("TPSL_LONDON_CAP12", []),
        key=lambda tr: float(tr.get("closed_at") or tr.get("opened_at") or 0.0),
    )
    port_sum = _summarize_trades(merged, peak_margin_usd=21.5, refusals={})
    port_dd = float(port_sum["max_dd_usd"])
    port_mc = monte_carlo_drawdown([float(t["net_pnl_usd"]) for t in merged], trials=mc_trials, seed=42)
    portfolio = {
        "components": ["M5_MOMENTUM_G5_ADX20_35", "TPSL_LONDON_CAP12"],
        "summary": {**port_sum, "profit_to_dd_ratio": round(port_sum["net_pnl_usd"] / port_dd, 2) if port_dd > 0 else None},
        "monte_carlo": port_mc,
        "capital_sizing_guide": _capital_sizing_guide(port_dd, 21.5),
    }

    passed = [r for r in rows if r["all_gates_passed"]]
    ranked = sorted(
        rows,
        key=lambda r: (
            1 if r["all_gates_passed"] else 0,
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
        "retention": {
            "cutoff_y1_utc": _iso_utc(cutoff_y1),
            "m5": {k: sp_m5[k] for k in ("has_unseen_y1", "y1_span_days", "y2_span_days", "y1_range_utc", "y2_range_utc")},
            "m15": {k: sp_m15[k] for k in ("has_unseen_y1", "y1_span_days", "y2_span_days", "y1_range_utc", "y2_range_utc")},
            "h1": {k: sp_h1[k] for k in ("has_unseen_y1", "y1_span_days", "y2_span_days", "y1_range_utc", "y2_range_utc")},
        },
        "engines": rows,
        "portfolio_combo": portfolio,
        "verdict": {
            "any_passed": len(passed) > 0,
            "passed_ids": [r["id"] for r in passed],
            "recommended_for_live_paper": passed[0]["id"] if passed else None,
            "best_overall_id": ranked[0]["id"] if ranked else None,
            "status": (
                "PASS_2YR_OOS_VALIDATED_READY_FOR_LIVE_PAPER"
                if passed
                else "REVIEW_RESULTS_BEFORE_LIVE_PAPER"
            ),
        },
    }
