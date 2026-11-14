"""Deterministic market analysis that the model reasons on top of.

Everything here is plain arithmetic on real MT5 bars - no LLM calls, no
opinions invented from nothing. The model's job is interpretation, not
invention, which is what keeps the assistant honest.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

from . import indicators as ta
from .config import sizing_balance
from .risk import lots_for_risk, reward_risk

TF_WEIGHTS = {
    "M1": 0.3, "M5": 0.5, "M15": 1.0, "M30": 1.2, "H1": 1.5,
    "H2": 1.5, "H3": 1.5, "H4": 2.0, "H6": 1.6, "H8": 1.2,
    "H12": 1.1, "D1": 1.0, "W1": 0.6, "MN1": 0.4,
}


def _r(value: float | None, digits: int = 5) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return round(float(value), digits)


def _iso(ts: float | int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _col(bars: Sequence[dict[str, Any]], key: str) -> list[float]:
    return [float(b.get(key) or 0.0) for b in bars]


def _series_last(series: Sequence[float | None]) -> float | None:
    return ta.last_defined(series)


# --------------------------------------------------------------------- profile
def profile(bars: Sequence[dict[str, Any]], timeframe: str, digits: int = 2) -> dict[str, Any]:
    """Indicator + structure summary for one timeframe."""
    if len(bars) < 30:
        return {"timeframe": timeframe, "ok": False, "error": "not enough bars", "bars": len(bars)}

    highs, lows, closes = _col(bars, "high"), _col(bars, "low"), _col(bars, "close")
    price = closes[-1]

    ema20, ema50, ema200 = ta.ema(closes, 20), ta.ema(closes, 50), ta.ema(closes, 200)
    e20, e50, e200 = _series_last(ema20), _series_last(ema50), _series_last(ema200)
    rsi_series = ta.rsi(closes, 14)
    atr_series = ta.atr(highs, lows, closes, 14)
    adx_series, pdi_series, mdi_series = ta.adx(highs, lows, closes, 14)
    slope_series = ta.slope(closes, 20)
    upper, mid, lower = ta.bollinger(closes, 20, 2.0)
    don_hi, don_lo = ta.donchian(highs, lows, 20)

    atr_val = _series_last(atr_series)
    atr_pct = (atr_val / price * 100.0) if atr_val and price else None
    bw = None
    if _series_last(upper) and _series_last(lower) and _series_last(mid):
        bw = (_series_last(upper) - _series_last(lower)) / _series_last(mid) * 100.0

    adx_val = _series_last(adx_series)
    pdi, mdi = _series_last(pdi_series), _series_last(mdi_series)
    slope_val = _series_last(slope_series)
    rsi_val = _series_last(rsi_series)

    don_hi_v, don_lo_v = _series_last(don_hi), _series_last(don_lo)
    don_pos = None
    if don_hi_v is not None and don_lo_v is not None and don_hi_v > don_lo_v:
        don_pos = (price - don_lo_v) / (don_hi_v - don_lo_v)

    swing_highs, swing_lows = ta.swings(highs, lows, 2, 2)
    recent_highs = [highs[i] for i in swing_highs[-4:]]
    recent_lows = [lows[i] for i in swing_lows[-4:]]
    pattern = "unknown"
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = highs[swing_highs[-1]] > highs[swing_highs[-2]]
        hl = lows[swing_lows[-1]] > lows[swing_lows[-2]]
        if hh and hl:
            pattern = "HH/HL (uptrend structure)"
        elif not hh and not hl:
            pattern = "LH/LL (downtrend structure)"
        else:
            pattern = "mixed / overlapping swings"

    # --- trend vote -------------------------------------------------------
    score = 0.0
    reasons: list[str] = []
    if e20 and e50:
        if e20 > e50:
            score += 0.25
            reasons.append("EMA20 above EMA50")
        else:
            score -= 0.25
            reasons.append("EMA20 below EMA50")
    if e50 and e200:
        if e50 > e200:
            score += 0.25
            reasons.append("EMA50 above EMA200 (higher-timeframe bullish)")
        else:
            score -= 0.25
            reasons.append("EMA50 below EMA200 (higher-timeframe bearish)")
    if e200:
        if price > e200:
            score += 0.15
            reasons.append("price above EMA200")
        else:
            score -= 0.15
            reasons.append("price below EMA200")
    if slope_val is not None:
        score += max(-0.15, min(0.15, slope_val * 0.5))
        reasons.append(f"regression slope {slope_val:+.3f}%/bar over 20 bars")
    if adx_val is not None and adx_val >= 20 and pdi is not None and mdi is not None:
        if pdi > mdi:
            score += 0.15
            reasons.append(f"ADX {adx_val:.1f} with +DI > -DI (trending up)")
        else:
            score -= 0.15
            reasons.append(f"ADX {adx_val:.1f} with -DI > +DI (trending down)")
    if don_pos is not None:
        score += (don_pos - 0.5) * 0.2
        reasons.append(f"price at {don_pos * 100:.0f}% of the 20-bar range")

    score = max(-1.0, min(1.0, score))
    if score >= 0.2:
        direction = "up"
    elif score <= -0.2:
        direction = "down"
    else:
        direction = "range"
    strength = min(1.0, abs(score) * (1.0 if (adx_val or 0) < 20 else 1.3))
    if adx_val is not None and adx_val < 18 and direction != "range":
        direction = "range"
        reasons.append(f"ADX {adx_val:.1f} below 18 - trend not confirmed")

    change_20 = None
    if len(closes) > 20:
        change_20 = ta.pct_change(price, closes[-21])

    return {
        "timeframe": timeframe,
        "ok": True,
        "bars": len(bars),
        "last_bar_time": _iso(bars[-1].get("time")),
        "price": _r(price, digits),
        "ema": {"20": _r(e20, digits), "50": _r(e50, digits), "200": _r(e200, digits)},
        "rsi14": _r(rsi_val, 2),
        "atr14": _r(atr_val, digits),
        "atr_percent": _r(atr_pct, 3),
        "adx14": _r(adx_val, 2),
        "di": {"plus": _r(pdi, 2), "minus": _r(mdi, 2)},
        "bollinger": {
            "upper": _r(_series_last(upper), digits),
            "middle": _r(_series_last(mid), digits),
            "lower": _r(_series_last(lower), digits),
            "width_percent": _r(bw, 3),
        },
        "donchian": {
            "upper": _r(don_hi_v, digits),
            "lower": _r(don_lo_v, digits),
            "position": _r(don_pos, 3),
        },
        "slope_percent_per_bar": _r(slope_val, 4),
        "change_percent_20_bars": _r(change_20, 3),
        "structure": {
            "pattern": pattern,
            "swing_highs": [_r(x, digits) for x in recent_highs],
            "swing_lows": [_r(x, digits) for x in recent_lows],
        },
        "trend": {
            "direction": direction,
            "score": _r(score, 3),
            "strength": _r(strength, 3),
            "reasons": reasons,
        },
        "volatility_regime": _volatility_regime(atr_series, atr_val),
    }


def _volatility_regime(
    atr_series: Sequence[float | None], current: float | None
) -> dict[str, Any]:
    history = [v for v in atr_series if v is not None]
    if not history or current is None:
        return {"regime": "unknown"}
    rank = ta.percentile_rank(history[-500:], current)
    if rank < 0.25:
        regime = "compressed"
    elif rank < 0.6:
        regime = "normal"
    elif rank < 0.85:
        regime = "elevated"
    else:
        regime = "spiking"
    return {"regime": regime, "percentile": round(rank * 100, 1)}


# ------------------------------------------------------------------ confluence
def confluence(profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    total_w = 0.0
    weighted = 0.0
    votes: dict[str, str] = {}
    for tf, prof in profiles.items():
        if not prof.get("ok"):
            continue
        w = TF_WEIGHTS.get(tf, 1.0)
        direction = prof["trend"]["direction"]
        votes[tf] = direction
        weighted += prof["trend"]["score"] * w
        total_w += w
    score = weighted / total_w if total_w else 0.0
    bullish = sum(1 for v in votes.values() if v == "up")
    bearish = sum(1 for v in votes.values() if v == "down")
    flat = sum(1 for v in votes.values() if v == "range")
    if score >= 0.2:
        bias = "long"
    elif score <= -0.2:
        bias = "short"
    else:
        bias = "neutral"
    aligned = max(bullish, bearish, flat)
    return {
        "bias": bias,
        "score": _r(score, 3),
        "votes": votes,
        "bullish_timeframes": bullish,
        "bearish_timeframes": bearish,
        "ranging_timeframes": flat,
        "alignment": _r(aligned / len(votes), 2) if votes else 0.0,
        "conflict": bullish > 0 and bearish > 0,
    }


# -------------------------------------------------------------------- market
def _market_open_estimate(now: datetime) -> dict[str, Any]:
    """Rough XAUUSD/FX session check. MT5 is the source of truth; this is a hint."""
    weekday = now.weekday()  # Mon=0
    hour = now.hour
    reason = None
    is_open = True
    if weekday == 5:
        is_open, reason = False, "Saturday - market closed"
    elif weekday == 6 and hour < 22:
        is_open, reason = False, "Sunday before the 22:00 UTC open"
    elif weekday == 4 and hour >= 21:
        is_open, reason = False, "Friday after the 21:00 UTC close"
    elif hour == 21:
        is_open, reason = False, "daily rollover break (~21:00-22:00 UTC)"
    return {"open_estimate": is_open, "reason": reason}


def key_levels(profiles: dict[str, dict[str, Any]], price: float, digits: int) -> dict[str, Any]:
    """Nearest structure above/below price, including psychologically round numbers."""
    levels: list[dict[str, Any]] = []
    for tf, prof in profiles.items():
        if not prof.get("ok"):
            continue
        structure = prof["structure"]
        for value in structure["swing_highs"]:
            if value and value > price:
                levels.append({"price": value, "kind": "swing_high", "timeframe": tf})
        for value in structure["swing_lows"]:
            if value and value < price:
                levels.append({"price": value, "kind": "swing_low", "timeframe": tf})
        don = prof["donchian"]
        if don["upper"]:
            levels.append({"price": don["upper"], "kind": "20-bar high", "timeframe": tf})
        if don["lower"]:
            levels.append({"price": don["lower"], "kind": "20-bar low", "timeframe": tf})

    if price > 0:
        step = 10 ** (math.floor(math.log10(price)) - 1)
        base = math.floor(price / step) * step
        for k in range(-2, 3):
            levels.append(
                {"price": _r(base + k * step, digits), "kind": "round number", "timeframe": "-"}
            )

    dedup: dict[float, dict[str, Any]] = {}
    for lv in levels:
        if lv["price"] is None:
            continue
        key = round(float(lv["price"]), digits)
        if abs(key - price) / price < 0.0005:
            continue
        dedup.setdefault(key, lv)
    ordered = sorted(dedup.values(), key=lambda x: abs(x["price"] - price))
    above = sorted(lv["price"] for lv in ordered if lv["price"] > price)
    below = sorted((lv["price"] for lv in ordered if lv["price"] < price), reverse=True)
    return {"nearest": ordered[:8], "resistance": above[:3], "support": below[:3]}


def history_stats(deals: Iterable[dict[str, Any]], days: int = 30) -> dict[str, Any]:
    deals = [d for d in deals if float(d.get("profit") or 0) != 0 or d.get("type")]
    profits = [float(d.get("profit") or 0) + float(d.get("swap") or 0) + float(d.get("commission") or 0) for d in deals]
    closed = [p for p in profits if p != 0]
    wins = [p for p in closed if p > 0]
    losses = [p for p in closed if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    by_symbol: dict[str, dict[str, Any]] = {}
    for d, p in zip(deals, profits, strict=False):
        sym = d.get("symbol") or "?"
        entry = by_symbol.setdefault(sym, {"deals": 0, "net": 0.0, "wins": 0, "losses": 0})
        entry["deals"] += 1
        entry["net"] = round(entry["net"] + p, 2)
        if p > 0:
            entry["wins"] += 1
        elif p < 0:
            entry["losses"] += 1
    return {
        "window_days": days,
        "deals": len(deals),
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": _r(len(wins) / len(closed) * 100, 1) if closed else None,
        "net_profit": _r(sum(closed), 2),
        "gross_profit": _r(gross_win, 2),
        "gross_loss": _r(-gross_loss, 2),
        "profit_factor": _r(gross_win / gross_loss, 2) if gross_loss else None,
        "average_win": _r(gross_win / len(wins), 2) if wins else None,
        "average_loss": _r(-gross_loss / len(losses), 2) if losses else None,
        "expectancy_per_trade": _r(sum(closed) / len(closed), 2) if closed else None,
        "by_symbol": by_symbol,
    }


# ------------------------------------------------------------------- scenario
def build_scenario(
    *,
    symbol: str,
    digits: int,
    price: float,
    profiles: dict[str, dict[str, Any]],
    bias: dict[str, Any],
    cfg,
    symbol_info: dict[str, Any],
    account: dict[str, Any] | None = None,
    risk_percent: float | None = None,
) -> dict[str, Any]:
    """Turn the analysis into an actionable, fully-priced plan."""
    trading_tfs = [tf for tf in profiles if profiles[tf].get("ok")]
    if not trading_tfs:
        return {"action": "none", "reason": "no usable timeframe data"}

    exec_tf = "M15" if "M15" in profiles and profiles["M15"].get("ok") else trading_tfs[0]
    prof = profiles[exec_tf]
    atr_val = prof.get("atr14") or (price * 0.002)
    ema20 = (prof.get("ema") or {}).get("20") or price
    direction = bias["bias"]

    if direction == "neutral":
        don = prof.get("donchian") or {}
        upper, lower = don.get("upper"), don.get("lower")
        plan = {
            "action": "wait",
            "reason": "timeframes disagree or the market is ranging - no directional edge",
            "range": {"upper": upper, "lower": lower},
            "if_range_play": {
                "long": {
                    "entry_zone": [_r((lower or price - atr_val) - 0.1 * atr_val, digits),
                                   _r((lower or price - atr_val) + 0.35 * atr_val, digits)],
                    "stop": _r((lower or price - atr_val) - 0.75 * atr_val, digits),
                },
                "short": {
                    "entry_zone": [_r((upper or price + atr_val) - 0.35 * atr_val, digits),
                                   _r((upper or price + atr_val) + 0.1 * atr_val, digits)],
                    "stop": _r((upper or price + atr_val) + 0.75 * atr_val, digits),
                },
            },
        }
        return plan

    sign = 1 if direction == "long" else -1
    extended = (price - ema20) * sign > 0.75 * atr_val
    if extended:
        anchor = ema20
        style = "pullback to EMA20"
    else:
        anchor = price
        style = "continuation from current price"

    entry_mid = anchor
    entry_low = entry_mid - 0.15 * atr_val if sign > 0 else entry_mid - 0.05 * atr_val
    entry_high = entry_mid + 0.15 * atr_val if sign > 0 else entry_mid + 0.05 * atr_val
    if sign < 0:
        entry_low, entry_high = entry_mid - 0.05 * atr_val, entry_mid + 0.15 * atr_val

    target_notes: list[str] = []
    structure = prof.get("structure") or {}
    swing_lows = [v for v in (structure.get("swing_lows") or []) if v]
    swing_highs = [v for v in (structure.get("swing_highs") or []) if v]

    if sign > 0:
        structural = min(swing_lows) if swing_lows and min(swing_lows) < entry_low else entry_low - atr_val
        stop = structural - 0.3 * atr_val
        if entry_mid - stop > 2.0 * atr_val:
            stop = entry_mid - 1.5 * atr_val
        if entry_mid - stop < 0.5 * atr_val:
            stop = entry_mid - 0.5 * atr_val
    else:
        structural = max(swing_highs) if swing_highs and max(swing_highs) > entry_high else entry_high + atr_val
        stop = structural + 0.3 * atr_val
        if stop - entry_mid > 2.0 * atr_val:
            stop = entry_mid + 1.5 * atr_val
        if stop - entry_mid < 0.5 * atr_val:
            stop = entry_mid + 0.5 * atr_val

    # never let the stop sit inside the spread / tick noise
    point = float(symbol_info.get("point") or 0.0)
    spread_points = float(symbol_info.get("spread_points") or symbol_info.get("spread") or 0.0)
    min_stop = max(0.5 * atr_val, (3.0 * spread_points + 2.0) * point)
    if abs(entry_mid - stop) < min_stop:
        stop = entry_mid - sign * min_stop
        target_notes.append("stop placed beyond the spread and tick noise")

    risk = abs(entry_mid - stop)
    tp1 = entry_mid + sign * 1.5 * risk
    tp2 = entry_mid + sign * 3.0 * risk
    don = prof.get("donchian") or {}
    if sign > 0 and don.get("upper") and tp1 < don["upper"] < tp2:
        target_notes.append(
            f"20-bar high {_r(don['upper'], digits)} sits between TP1 and TP2 - expect supply"
        )
    if sign < 0 and don.get("lower") and tp2 < don["lower"] < tp1:
        target_notes.append(
            f"20-bar low {_r(don['lower'], digits)} sits between TP1 and TP2 - expect demand"
        )

    plan: dict[str, Any] = {
        "action": "buy" if sign > 0 else "sell",
        "style": style,
        "entry_zone": [_r(entry_low, digits), _r(entry_high, digits)],
        "entry_reference": _r(entry_mid, digits),
        "stop_loss": _r(stop, digits),
        "take_profit_1": _r(tp1, digits),
        "take_profit_2": _r(tp2, digits),
        "risk_per_unit": _r(risk, digits),
        "reward_risk_1": reward_risk(entry_mid, stop, tp1),
        "reward_risk_2": reward_risk(entry_mid, stop, tp2),
        "invalidation": (
            f"a {exec_tf} close {'below' if sign > 0 else 'above'} "
            f"{_r(stop, digits)} voids the idea"
        ),
        "execution_timeframe": exec_tf,
        "stop_basis": "swing structure +/- 0.3 ATR",
        "target_notes": target_notes or None,
    }

    # --- confidence -------------------------------------------------------
    score = 50.0
    score += max(0, bias["alignment"] - 0.5) * 40
    adx_val = prof.get("adx14") or 0
    score += min(15.0, max(-10.0, (adx_val - 20) * 0.8))
    if bias.get("conflict"):
        score -= 15
    regime = (prof.get("volatility_regime") or {}).get("regime")
    if regime == "spiking":
        score -= 8
    if regime == "compressed":
        score -= 4
    plan["confidence"] = int(max(5, min(92, round(score))))

    # --- sizing -----------------------------------------------------------
    if account:
        pct = cfg.max_risk_percent if risk_percent is None else min(float(risk_percent), cfg.max_risk_percent)
        sizing = lots_for_risk(
            balance=sizing_balance(account, cfg),
            risk_percent=pct,
            entry=entry_mid,
            stop=stop,
            info=symbol_info or {},
            max_lot=cfg.max_lot,
        )
        plan["sizing"] = {**sizing, "risk_percent": pct,
                          "effective_sizing_balance": sizing_balance(account, cfg),
                          "training_budget": cfg.training_budget,
                          "account_equity": account.get("equity")}
    return plan


# --------------------------------------------------------------------- facade
def analyze_symbol(
    *,
    mt5,
    cfg,
    symbol: str | None = None,
    timeframes: Sequence[str] | None = None,
    bars_count: int = 400,
    risk_percent: float | None = None,
) -> dict[str, Any]:
    symbol = mt5.resolve_symbol(symbol or cfg.default_symbol)
    info = mt5.symbol_info(symbol)
    digits = int(float(info.get("digits") or 2))
    tfs = cfg.normalise_timeframes(timeframes)

    profiles: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for tf in tfs:
        try:
            bars = mt5.candles(symbol, tf, bars_count)
            profiles[tf] = profile(bars[:-1], tf, digits)  # exclude the forming candle
        except Exception as exc:  # noqa: BLE001 - surface per-timeframe failures
            errors[tf] = str(exc)

    usable = {tf: p for tf, p in profiles.items() if p.get("ok")}
    if not usable:
        return {
            "ok": False,
            "symbol": symbol,
            "error": "no timeframe produced enough bars",
            "details": errors,
        }

    tick = mt5.tick(symbol)
    price = float(tick.get("bid") or tick.get("ask") or 0.0)
    bias = confluence(usable)
    account = mt5.account_info()
    now = datetime.now(timezone.utc)

    scenario = build_scenario(
        symbol=symbol,
        digits=digits,
        price=price,
        profiles=usable,
        bias=bias,
        cfg=cfg,
        symbol_info=info,
        account=account,
        risk_percent=risk_percent,
    )
    spread_points = float(info.get("spread_points") or info.get("spread") or 0)

    warnings: list[str] = []
    if spread_points > cfg.max_spread_points:
        warnings.append(
            f"spread is {spread_points:.0f} points - above your {cfg.max_spread_points:.0f} limit"
        )
    market = _market_open_estimate(now)
    if not market["open_estimate"]:
        warnings.append(f"market looks closed: {market['reason']}")
    if bias.get("conflict"):
        warnings.append("timeframes disagree - reduce size or wait for alignment")
    if (usable.get("M15") or {}).get("volatility_regime", {}).get("regime") == "spiking":
        warnings.append("volatility is spiking - widen stops or stay out")
    if not cfg.allow_trading:
        warnings.append(
            "TRADE_MCP_ALLOW_TRADING=false: analysis only, confirm_order will refuse to execute"
        )

    status = {
        "ok": True,
        "symbol": symbol,
        "description": info.get("description"),
        "digits": digits,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quote": {
            "bid": tick.get("bid"),
            "ask": tick.get("ask"),
            "spread_points": spread_points,
            "tick_time": _iso(tick.get("time")),
        },
        "market": market,
        "account": {
            k: account.get(k)
            for k in ("login", "currency", "balance", "equity", "margin_free", "leverage", "margin_level")
        },
        "timeframes": usable,
        "timeframe_errors": errors or None,
        "bias": bias,
        "levels": key_levels(usable, price, digits),
        "scenario": scenario,
        "warnings": warnings,
        "assumptions": [
            "prices come from the broker feed via the MQL5 EA, not from this server",
            "sizing uses the stop distance and the symbol's tick value",
            f"risk per trade is capped at {cfg.max_risk_percent}% by configuration",
        ],
    }
    if info.get("mock"):
        status["mock"] = True
    status["summary"] = _summary(status)
    status["summary_fa"] = _summary_fa(status)
    return status


def _summary(result: dict[str, Any]) -> str:
    bias = result["bias"]
    sc = result["scenario"].get("action", "wait")
    conf = result["scenario"].get("confidence")
    tfs = ", ".join(f"{tf}:{p['trend']['direction']}" for tf, p in result["timeframes"].items())
    return (
        f"{result['symbol']} @ {result['quote']['bid']} | bias {bias['bias']} "
        f"(score {bias['score']}, alignment {bias['alignment']}) | plan {sc}"
        + (f" confidence {conf}%" if conf else "")
        + f" | timeframes {tfs}"
    )


def _summary_fa(result: dict[str, Any]) -> str:
    bias_map = {"long": "خرید", "short": "فروش", "neutral": "بی‌طرف"}
    action_map = {"buy": "خرید", "sell": "فروش", "wait": "صبر"}
    sc = result["scenario"]
    bias = result["bias"]["bias"]
    parts = [
        f"{result['symbol']} در قیمت {result['quote']['bid']}",
        f"سوگیری کلی: {bias_map.get(bias, bias)} (امتیاز {result['bias']['score']})",
    ]
    if sc.get("action") in {"buy", "sell"}:
        parts.append(
            "سناریو: {} در ناحیه {} تا {}، حد ضرر {}، هدف اول {} و هدف دوم {}، "
            "نسبت سود به ضرر {} و امتیاز تحلیل {} از ۱۰۰ (نه احتمال برد)".format(
                action_map.get(sc["action"], sc["action"]),
                sc.get("entry_zone", [None, None])[0],
                sc.get("entry_zone", [None, None])[1],
                sc.get("stop_loss"),
                sc.get("take_profit_1"),
                sc.get("take_profit_2"),
                sc.get("reward_risk_1"),
                sc.get("confidence"),
            )
        )
    else:
        parts.append("سناریو: فعلاً ورود توصیه نمی‌شود (بازار بدون جهت یا متناقض است)")
    if result.get("warnings"):
        parts.append("هشدارها: " + "؛ ".join(result["warnings"]))
    return " | ".join(parts)


def snapshot_symbol(*, mt5, cfg, symbol: str | None = None, timeframes=None,
                    bars_count: int = 300) -> dict[str, Any]:
    symbol = mt5.resolve_symbol(symbol or cfg.default_symbol)
    info = mt5.symbol_info(symbol)
    digits = int(float(info.get("digits") or 2))
    tfs = cfg.normalise_timeframes(timeframes)
    out: dict[str, Any] = {"ok": True, "symbol": symbol, "digits": digits, "timeframes": {}}
    for tf in tfs:
        try:
            bars = mt5.candles(symbol, tf, bars_count)
            prof = profile(bars, tf, digits)
            out["timeframes"][tf] = prof
        except Exception as exc:  # noqa: BLE001
            out["timeframes"][tf] = {"timeframe": tf, "ok": False, "error": str(exc)}
    tick = mt5.tick(symbol)
    out["quote"] = tick
    out["spread_points"] = info.get("spread_points")
    if info.get("mock"):
        out["mock"] = True
    return out
