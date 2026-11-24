"""Read-only M1 momentum/acceleration simulator with multi-timeframe (M5/M15/H1) context.

Pure Python, offline, zero network calls and zero execution imports.
Designed to test M1 acceleration entries, higher-timeframe regime/bias filters,
fast invalidation cuts, uncapped trailing/deceleration exits, and safe
profit-only pyramiding on historical OHLC bars with conservative intrabar order
and zero lookahead bias.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .daytrade import M1_SECONDS
from .daytrade_paper import (
    HARD_MAX_LOT,
    HARD_MAX_STOP_USD,
    audit_symbol_spec,
    resolve_pricing_basis,
)
from .risk import round_to_step

HARD_MAX_M5_STOP_USD = 15.0

TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
}


def _finite(value: Any, name: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    num = float(value)
    if not math.isfinite(num):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and num < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and num > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return num


def clean_ohlcv_bars(bars: list[list[float]]) -> list[list[float]]:
    """Validate, sort, and deduplicate generic OHLCV bars [epoch, o, h, l, c, v]."""
    seen: dict[int, list[float]] = {}
    for row in bars:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        t = int(float(row[0]))
        o, h, low, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
        v = float(row[5]) if len(row) >= 6 else 0.0
        if not all(math.isfinite(x) for x in (t, o, h, low, c, v)):
            continue
        if low <= 0 or h < max(o, c) or low > min(o, c):
            continue
        seen[t] = [float(t), o, h, low, c, max(0.0, v)]
    return [seen[k] for k in sorted(seen)]


def split_contiguous_bars(bars: list[list[float]], *, bar_seconds: int = M1_SECONDS) -> list[list[list[float]]]:
    """Split arbitrarily long OHLCV history into strictly contiguous bar_seconds segments."""
    if bar_seconds <= 0:
        raise ValueError("bar_seconds must be positive")
    clean = clean_ohlcv_bars(bars)
    if not clean:
        return []
    segments: list[list[list[float]]] = [[clean[0]]]
    for row in clean[1:]:
        if int(row[0]) - int(segments[-1][-1][0]) == bar_seconds:
            segments[-1].append(row)
        else:
            segments.append([row])
    return segments


def split_contiguous_m1(m1_bars: list[list[float]]) -> list[list[list[float]]]:
    """Split arbitrarily long M1 OHLCV history into strictly contiguous 60s segments."""
    return split_contiguous_bars(m1_bars, bar_seconds=M1_SECONDS)


def resample_bars_to_tf(
    bars: list[list[float]],
    *,
    src_seconds: int,
    dst_seconds: int,
) -> list[list[float]]:
    """Resample sorted OHLCV bars from src_seconds into strictly aligned dst_seconds buckets."""
    if src_seconds <= 0 or dst_seconds <= src_seconds or dst_seconds % src_seconds != 0:
        raise ValueError("dst_seconds must be a strict multiple of src_seconds")
    clean = clean_ohlcv_bars(bars)
    buckets: dict[int, list[list[float]]] = {}
    for row in clean:
        bucket = (int(row[0]) // dst_seconds) * dst_seconds
        buckets.setdefault(bucket, []).append(row)
    expected = dst_seconds // src_seconds
    min_count = max(2, int(math.ceil(expected * 0.6)))
    out: list[list[float]] = []
    for bucket in sorted(buckets):
        rows = buckets[bucket]
        if len(rows) < min_count:
            continue
        out.append([
            float(bucket),
            rows[0][1],
            max(r[2] for r in rows),
            min(r[3] for r in rows),
            rows[-1][4],
            sum(r[5] for r in rows),
        ])
    return out


def resample_m1_to_tf(m1_bars: list[list[float]], period_seconds: int) -> list[list[float]]:
    """Resample sorted M1 bars into strictly aligned higher-timeframe buckets."""
    if period_seconds not in (300, 900, 3600, 14400):
        raise ValueError("period_seconds must be 300, 900, 3600, or 14400")
    return resample_bars_to_tf(m1_bars, src_seconds=M1_SECONDS, dst_seconds=period_seconds)


def ema_series(values: list[float], period: int) -> list[float | None]:
    """Compute standard exponential moving average series."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = alpha * values[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def atr_series(bars: list[list[float]], period: int = 14) -> list[float | None]:
    """Compute Wilder ATR series on OHLC bars."""
    n = len(bars)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    trs: list[float] = []
    for i, row in enumerate(bars):
        h, low = row[2], row[3]
        if i == 0:
            trs.append(h - low)
        else:
            prev_c = bars[i - 1][4]
            trs.append(max(h - low, abs(h - prev_c), abs(low - prev_c)))
    seed = sum(trs[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def adx_series(bars: list[list[float]], period: int = 14) -> list[float | None]:
    """Compute Wilder ADX(period) series on OHLC bars."""
    n = len(bars)
    out: list[float | None] = [None] * n
    if period <= 1 or n < period * 2:
        return out
    trs: list[float] = [0.0] * n
    pdm: list[float] = [0.0] * n
    mdm: list[float] = [0.0] * n
    for i in range(1, n):
        h, low = bars[i][2], bars[i][3]
        ph, pl, pc = bars[i - 1][2], bars[i - 1][3], bars[i - 1][4]
        trs[i] = max(h - low, abs(h - pc), abs(low - pc))
        up = h - ph
        down = pl - low
        pdm[i] = up if (up > down and up > 0) else 0.0
        mdm[i] = down if (down > up and down > 0) else 0.0

    tr_s = sum(trs[1 : period + 1])
    p_s = sum(pdm[1 : period + 1])
    m_s = sum(mdm[1 : period + 1])
    dxs: list[float | None] = [None] * n

    def _dx(tr_v: float, p_v: float, m_v: float) -> float:
        if tr_v <= 0:
            return 0.0
        pdi = 100.0 * p_v / tr_v
        mdi = 100.0 * m_v / tr_v
        denom = pdi + mdi
        return 100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0

    dxs[period] = _dx(tr_s, p_s, m_s)
    for i in range(period + 1, n):
        tr_s = tr_s - (tr_s / period) + trs[i]
        p_s = p_s - (p_s / period) + pdm[i]
        m_s = m_s - (m_s / period) + mdm[i]
        dxs[i] = _dx(tr_s, p_s, m_s)

    first_adx_idx = period * 2 - 1
    valid_dx = [x for x in dxs[period : first_adx_idx + 1] if x is not None]
    if len(valid_dx) < period:
        return out
    adx = sum(valid_dx) / len(valid_dx)
    out[first_adx_idx] = adx
    for i in range(first_adx_idx + 1, n):
        dx_val = dxs[i] if dxs[i] is not None else 0.0
        adx = (adx * (period - 1) + dx_val) / period
        out[i] = adx
    return out


@dataclass(frozen=True)
class HTFContext:
    close_time: int
    close: float
    ema_fast: float | None
    ema_slow: float | None
    adx: float | None
    atr: float | None
    donchian_pos: float | None
    bias: str


def build_htf_index(
    bars: list[list[float]],
    *,
    period_seconds: int,
    ema_fast: int = 9,
    ema_slow: int = 21,
    adx_period: int = 14,
    donchian_period: int = 20,
) -> tuple[list[int], list[HTFContext]]:
    """Precompute causal higher-timeframe context indexed by bar CLOSE timestamp."""
    clean = clean_ohlcv_bars(bars)
    if not clean:
        return [], []
    closes = [r[4] for r in clean]
    ef = ema_series(closes, ema_fast)
    es = ema_series(closes, ema_slow)
    adx = adx_series(clean, adx_period)
    atr = atr_series(clean, adx_period)

    close_times: list[int] = []
    contexts: list[HTFContext] = []
    for i, row in enumerate(clean):
        ct = int(row[0]) + period_seconds
        c = row[4]
        dpos: float | None = None
        if i + 1 >= donchian_period:
            window = clean[i + 1 - donchian_period : i + 1]
            hi = max(r[2] for r in window)
            lo = min(r[3] for r in window)
            if hi > lo:
                dpos = (c - lo) / (hi - lo)
        bias = "neutral"
        if ef[i] is not None and es[i] is not None:
            if c > ef[i] and ef[i] > es[i]:
                bias = "buy"
            elif c < ef[i] and ef[i] < es[i]:
                bias = "sell"
            elif ef[i] > es[i]:
                bias = "lean_buy"
            elif ef[i] < es[i]:
                bias = "lean_sell"
        close_times.append(ct)
        contexts.append(HTFContext(
            close_time=ct,
            close=c,
            ema_fast=ef[i],
            ema_slow=es[i],
            adx=adx[i],
            atr=atr[i],
            donchian_pos=dpos,
            bias=bias,
        ))
    return close_times, contexts


def lookup_htf(close_times: list[int], contexts: list[HTFContext], at_ts: int) -> HTFContext | None:
    """Return the newest higher-timeframe bar strictly closed at or before ``at_ts``."""
    idx = bisect_right(close_times, at_ts) - 1
    return contexts[idx] if idx >= 0 else None


def detect_m1_impulse(
    seg: list[list[float]],
    atr_vals: list[float | None],
    idx: int,
    *,
    entry_mode: str = "impulse_close",
    spread_price: float = 0.0,
    min_impulse_atr: float = 1.25,
    max_impulse_atr: float = 3.0,
    min_body_ratio: float = 0.60,
    min_close_pos: float = 0.72,
    breakout_lookback: int = 3,
    max_spread_to_bar: float = 0.22,
    pullback_frac: float = 0.30,
    min_volume_ratio: float | None = None,
) -> dict[str, Any] | None:
    """Detect an M1/M5 momentum/acceleration signal from strictly closed bars <= idx-1.

    Bar ``seg[idx]`` is the newly opening bar where entry may execute (zero lookahead).
    """
    if entry_mode not in ("impulse_close", "impulse_2bar", "impulse_pullback"):
        raise ValueError("entry_mode must be impulse_close, impulse_2bar, or impulse_pullback")
    min_prev = max(breakout_lookback + 1, 15)
    if idx < min_prev:
        return None
    atr = atr_vals[idx - 1]
    if atr is None or atr <= 0:
        return None

    b1 = seg[idx - 1]
    o1, h1, l1, c1 = b1[1], b1[2], b1[3], b1[4]
    v1 = float(b1[5]) if len(b1) >= 6 else 0.0
    r1 = h1 - l1
    body1 = abs(c1 - o1)
    if r1 <= 0 or body1 <= 0:
        return None

    if min_volume_ratio is not None and min_volume_ratio > 0:
        look_start = max(0, idx - 21)
        prev_vols = [float(seg[k][5]) for k in range(look_start, idx - 1) if len(seg[k]) >= 6]
        avg_v = (sum(prev_vols) / len(prev_vols)) if prev_vols else 0.0
        if avg_v > 0 and v1 < min_volume_ratio * avg_v:
            return None

    side = "buy" if c1 > o1 else ("sell" if c1 < o1 else None)
    if side is None:
        return None

    close_pos = (c1 - l1) / r1 if side == "buy" else (h1 - c1) / r1
    body_ratio = body1 / r1

    if entry_mode == "impulse_2bar":
        b0 = seg[idx - 2]
        o0, h0, l0, c0 = b0[1], b0[2], b0[3], b0[4]
        same_dir = (c0 > o0 and side == "buy") or (c0 < o0 and side == "sell")
        extends = (c1 > c0) if side == "buy" else (c1 < c0)
        push = abs(c1 - o0)
        if not (same_dir and extends):
            return None
        if not (min_impulse_atr * 1.25 * atr <= push <= max_impulse_atr * 1.3 * atr):
            return None
        if body_ratio < max(0.50, min_body_ratio - 0.05) or close_pos < max(0.65, min_close_pos - 0.04):
            return None
        if spread_price > 0 and spread_price > max(r1, 0.5 * push) * max_spread_to_bar:
            return None
        impulse_low = min(l0, l1)
        impulse_high = max(h0, h1)
        impulse_mid = (o0 + c1) / 2.0
        impulse_range = impulse_high - impulse_low
        return {
            "side": side,
            "mode": entry_mode,
            "bar_time": int(b1[0]),
            "impulse_body": body1,
            "impulse_range": impulse_range,
            "impulse_low": impulse_low,
            "impulse_high": impulse_high,
            "impulse_mid": impulse_mid,
            "impulse_volume": v1,
            "atr_m1": atr,
            "limit_trigger_bid": None,
        }

    # impulse_close or impulse_pullback
    if not (min_impulse_atr * atr <= r1 <= max_impulse_atr * atr):
        return None
    if body_ratio < min_body_ratio or close_pos < min_close_pos:
        return None
    if spread_price > 0 and spread_price > r1 * max_spread_to_bar:
        return None

    prev_closes = [seg[k][4] for k in range(idx - 1 - breakout_lookback, idx - 1)]
    if side == "buy" and c1 <= max(prev_closes):
        return None
    if side == "sell" and c1 >= min(prev_closes):
        return None

    limit_bid: float | None = None
    if entry_mode == "impulse_pullback":
        limit_bid = (c1 - pullback_frac * body1) if side == "buy" else (c1 + pullback_frac * body1)

    return {
        "side": side,
        "mode": entry_mode,
        "bar_time": int(b1[0]),
        "impulse_body": body1,
        "impulse_range": r1,
        "impulse_low": l1,
        "impulse_high": h1,
        "impulse_mid": (o1 + c1) / 2.0,
        "impulse_volume": v1,
        "atr_m1": atr,
        "limit_trigger_bid": limit_bid,
    }


def htf_allows_signal(
    *,
    side: str,
    htf_filter: str,
    m5: HTFContext | None,
    m15: HTFContext | None,
    h1: HTFContext | None,
    min_m5_adx: float = 18.0,
    min_mid_adx: float | None = None,
) -> tuple[bool, str | None]:
    """Check whether strictly closed HTF context permits an impulse entry.

    Supports M1-style labels ('m5_m15', 'm5_m15_h1') and M5-style labels
    ('m15_h1', 'm15_h1_h4'), where slots (m5, m15, h1) map to
    (primary_tf, mid_tf, macro_tf).
    """
    if htf_filter == "none":
        return True, None
    if htf_filter not in ("m5_m15", "m5_m15_h1", "m15_h1", "m15_h1_h4"):
        raise ValueError("htf_filter must be none, m5_m15, m5_m15_h1, m15_h1, or m15_h1_h4")
    is_m5_exec = htf_filter in ("m15_h1", "m15_h1_h4")
    p_lbl, m_lbl, g_lbl = ("m15", "h1", "h4") if is_m5_exec else ("m5", "m15", "h1")
    use_macro = htf_filter in ("m5_m15_h1", "m15_h1_h4")

    if m5 is None or m5.adx is None or m5.ema_fast is None or m5.ema_slow is None:
        return False, f"htf_{p_lbl}_unavailable"
    if m5.adx < min_m5_adx:
        return False, f"htf_{p_lbl}_low_adx_chop"
    if side == "buy" and not (m5.close >= m5.ema_fast and m5.ema_fast >= m5.ema_slow):
        return False, f"htf_{p_lbl}_bias_mismatch"
    if side == "sell" and not (m5.close <= m5.ema_fast and m5.ema_fast <= m5.ema_slow):
        return False, f"htf_{p_lbl}_bias_mismatch"
    # Avoid buying the extreme top of a non-strong primary HTF channel (or selling extreme bottom).
    if m5.donchian_pos is not None and m5.adx < 28.0:
        if side == "buy" and m5.donchian_pos > 0.94:
            return False, f"htf_{p_lbl}_range_extreme"
        if side == "sell" and m5.donchian_pos < 0.06:
            return False, f"htf_{p_lbl}_range_extreme"

    if m15 is None or m15.ema_slow is None:
        return False, f"htf_{m_lbl}_unavailable"
    if min_mid_adx is not None and min_mid_adx > 0:
        if m15.adx is None:
            return False, f"htf_{m_lbl}_adx_unavailable"
        if m15.adx < min_mid_adx:
            return False, f"htf_{m_lbl}_low_adx_chop"
    if side == "buy" and m15.close < m15.ema_slow:
        return False, f"htf_{m_lbl}_bias_mismatch"
    if side == "sell" and m15.close > m15.ema_slow:
        return False, f"htf_{m_lbl}_bias_mismatch"

    if use_macro:
        if h1 is None or h1.ema_fast is None or h1.ema_slow is None:
            return False, f"htf_{g_lbl}_unavailable"
        if side == "buy" and h1.ema_fast < h1.ema_slow:
            return False, f"htf_{g_lbl}_bias_mismatch"
        if side == "sell" and h1.ema_fast > h1.ema_slow:
            return False, f"htf_{g_lbl}_bias_mismatch"

    return True, None


@dataclass
class MomentumLeg:
    trade_id: int
    side: str
    volume: float
    entry: float
    stop: float
    multiplier: float
    commission_usd: float
    entry_spread_usd: float
    opened_at: float
    opened_bar_index: int
    impulse_body: float
    impulse_mid: float
    impulse_volume: float = 0.0
    initial_risk_usd: float = 5.0
    target: float | None = None
    opened_via_pullback: bool = False
    stop_trailed: bool = False
    max_favorable_usd: float = 0.0
    max_adverse_usd: float = 0.0


def _is_deceleration_bar(bar: list[float], *, side: str, impulse_body: float) -> bool:
    """Objective M1 deceleration detector: opposite close, stalling doji, or rejection wick."""
    o, h, low, c = bar[1], bar[2], bar[3], bar[4]
    rng = h - low
    body = abs(c - o)
    if side == "buy":
        if c < o:
            return True
        upper_wick = h - max(o, c)
        if rng > 0 and upper_wick / rng >= 0.45:
            return True
    else:
        if c > o:
            return True
        lower_wick = min(o, c) - low
        if rng > 0 and lower_wick / rng >= 0.45:
            return True
    return rng > 0 and body / rng <= 0.28 and body <= 0.35 * max(impulse_body, 1e-9)


def replay_m1_momentum(
    m1_bars: list[list[float]],
    *,
    spec: dict[str, Any],
    symbol: str = "XAUUSD_i",
    spread_price: float = 0.0,
    m5_bars: list[list[float]] | None = None,
    m15_bars: list[list[float]] | None = None,
    h1_bars: list[list[float]] | None = None,
    entry_mode: str = "impulse_close",
    htf_filter: str = "m5_m15",
    exit_mode: str = "decel_and_trail",
    stop_usd: float = 5.0,
    max_lot: float = 0.02,
    max_legs: int = 1,
    commission_per_lot: float = 0.0,
    min_m5_adx: float = 18.0,
    min_mid_adx: float | None = None,
    trail_arm_usd: float = 1.5,
    min_decel_profit_usd: float = 1.0,
    fast_invalidation_cut: bool = True,
    leverage: float = 500.0,
    max_margin_usd: float = 25.0,
    pricing_basis: str = "auto",
    bar_seconds: int = M1_SECONDS,
    blocked_utc_hours: tuple[int, ...] = (),
    min_decel_r: float | None = None,
    trail_arm_r: float | None = None,
    fixed_tp_r: float | None = None,
    min_volume_ratio: float | None = None,
    volume_dryup_ratio: float | None = None,
    daily_loss_limit_usd: float | None = None,
    weekly_loss_limit_usd: float | None = None,
    loss_streak_pause_count: int = 0,
    loss_streak_cooldown_bars: int = 0,
    htf_indices: tuple[
        tuple[list[int], list[HTFContext]],
        tuple[list[int], list[HTFContext]],
        tuple[list[int], list[HTFContext]],
    ] | None = None,
    include_trades: bool = False,
) -> dict[str, Any]:
    """Replay M1 or M5 acceleration + HTF filter + uncapped/asymmetric exits.

    Conservative intrabar fill rules: stop loss is ALWAYS checked before target,
    trailing updates, or deceleration exits; pullbacks that touch stop on the entry
    bar count as stop losses (and cannot claim same-bar target fills). Zero
    lookahead across execution and higher timeframes.
    """
    if bar_seconds not in (60, 300):
        raise ValueError("bar_seconds must be 60 (M1) or 300 (M5)")
    if entry_mode not in ("impulse_close", "impulse_2bar", "impulse_pullback", "pullback_or_2bar"):
        raise ValueError("entry_mode must be impulse_close, impulse_2bar, impulse_pullback, or pullback_or_2bar")
    if exit_mode not in ("decel_and_trail", "trail_only", "fixed_tp_and_be"):
        raise ValueError("exit_mode must be 'decel_and_trail', 'trail_only', or 'fixed_tp_and_be'")
    if exit_mode == "fixed_tp_and_be" and (fixed_tp_r is None or fixed_tp_r <= 0):
        raise ValueError("fixed_tp_and_be requires positive fixed_tp_r")
    spread_price = _finite(spread_price, "spread_price", minimum=0.0)
    max_stop_cap = HARD_MAX_M5_STOP_USD if bar_seconds == 300 else HARD_MAX_STOP_USD
    stop_usd = _finite(stop_usd, "stop_usd", minimum=1.0, maximum=max_stop_cap)
    max_lot = _finite(max_lot, "max_lot", minimum=0.01, maximum=HARD_MAX_LOT)
    commission_per_lot = _finite(commission_per_lot, "commission_per_lot", minimum=0.0, maximum=1000.0)
    if type(max_legs) is not int or not 1 <= max_legs <= 3:
        raise ValueError("max_legs must be an integer 1..3")
    blocked_hours_set = {int(h) % 24 for h in blocked_utc_hours}

    spec_audit = audit_symbol_spec(spec)
    resolved_basis, multiplier = resolve_pricing_basis(spec, pricing_basis)
    vmin = _finite(spec.get("volume_min"), "volume_min", minimum=1e-8)
    vmax = _finite(spec.get("volume_max"), "volume_max", minimum=vmin)
    vstep = _finite(spec.get("volume_step"), "volume_step", minimum=1e-8)
    tick_size = _finite(spec.get("trade_tick_size"), "trade_tick_size", minimum=1e-10)
    contract_size = float(spec_audit.get("trade_contract_size") or multiplier)

    segments = split_contiguous_bars(m1_bars, bar_seconds=bar_seconds)
    usable = [s for s in segments if len(s) >= 20]

    # Build strictly closed HTF indices (or reuse precomputed indices for fast walk-forward sweeps).
    if htf_indices is not None:
        (m5_t, m5_ctx), (m15_t, m15_ctx), (h1_t, h1_ctx) = htf_indices
    elif bar_seconds == 300:
        # M5 execution -> HTF slots are (M15, H1, H4)
        p_src = m5_bars if m5_bars else resample_bars_to_tf(m1_bars, src_seconds=300, dst_seconds=900)
        m_src = m15_bars if m15_bars else resample_bars_to_tf(m1_bars, src_seconds=300, dst_seconds=3600)
        g_src = h1_bars if h1_bars else resample_bars_to_tf(m_src, src_seconds=3600, dst_seconds=14400)
        m5_t, m5_ctx = build_htf_index(p_src, period_seconds=900, ema_fast=9, ema_slow=21)
        m15_t, m15_ctx = build_htf_index(m_src, period_seconds=3600, ema_fast=9, ema_slow=21)
        h1_t, h1_ctx = build_htf_index(g_src, period_seconds=14400, ema_fast=20, ema_slow=50)
    else:
        m5_src = m5_bars if m5_bars else resample_m1_to_tf(m1_bars, 300)
        m15_src = m15_bars if m15_bars else resample_m1_to_tf(m1_bars, 900)
        h1_src = h1_bars if h1_bars else resample_m1_to_tf(m1_bars, 3600)
        m5_t, m5_ctx = build_htf_index(m5_src, period_seconds=300, ema_fast=9, ema_slow=21)
        m15_t, m15_ctx = build_htf_index(m15_src, period_seconds=900, ema_fast=9, ema_slow=21)
        h1_t, h1_ctx = build_htf_index(h1_src, period_seconds=3600, ema_fast=20, ema_slow=50)

    open_legs: list[MomentumLeg] = []
    closed_trades: list[dict[str, Any]] = []
    refusals: dict[str, int] = {}
    signals_detected = 0
    signals_passed_htf = 0
    next_id = 0

    balance = 100.0
    peak_equity = 100.0
    max_drawdown = 0.0
    peak_open_lots = 0.0
    peak_margin_used = 0.0
    loss_streak = 0
    streak_since_cooldown = 0
    max_loss_streak = 0
    current_day_id: int | None = None
    current_iso_week: tuple[int, int] | None = None
    daily_realized_pnl = 0.0
    weekly_realized_pnl = 0.0
    cooldown_until_epoch = 0.0

    def _refuse(reason: str) -> None:
        refusals[reason] = refusals.get(reason, 0) + 1

    def _sync_calendar(ts: float) -> None:
        nonlocal current_day_id, current_iso_week, daily_realized_pnl, weekly_realized_pnl
        day_id = int(ts) // 86400
        if day_id != current_day_id:
            current_day_id = day_id
            daily_realized_pnl = 0.0
            iso = datetime.fromtimestamp(day_id * 86400, tz=timezone.utc).isocalendar()
            wk = (int(iso[0]), int(iso[1]))
            if wk != current_iso_week:
                current_iso_week = wk
                weekly_realized_pnl = 0.0

    def _leg_pnl(leg: MomentumLeg, exit_px: float) -> float:
        sign = 1.0 if leg.side == "buy" else -1.0
        return (exit_px - leg.entry) * sign * leg.volume * leg.multiplier - leg.commission_usd

    def _update_dd(mark_bid: float) -> None:
        nonlocal peak_equity, max_drawdown
        unreal = sum(
            _leg_pnl(lg, mark_bid if lg.side == "buy" else mark_bid + spread_price)
            for lg in open_legs
        )
        eq = balance + unreal
        if eq > peak_equity:
            peak_equity = eq
        dd = max(0.0, peak_equity - eq)
        if dd > max_drawdown:
            max_drawdown = dd

    def _close_leg(leg: MomentumLeg, *, exit_px: float, reason: str, closed_at: float) -> None:
        nonlocal balance, loss_streak, streak_since_cooldown, max_loss_streak
        nonlocal daily_realized_pnl, weekly_realized_pnl, cooldown_until_epoch
        _sync_calendar(closed_at)
        net = _leg_pnl(leg, exit_px)
        balance += net
        daily_realized_pnl += net
        weekly_realized_pnl += net
        if net < 0:
            loss_streak += 1
            streak_since_cooldown += 1
            max_loss_streak = max(max_loss_streak, loss_streak)
            if (
                loss_streak_pause_count > 0
                and loss_streak_cooldown_bars > 0
                and streak_since_cooldown >= loss_streak_pause_count
            ):
                cooldown_until_epoch = max(
                    cooldown_until_epoch,
                    closed_at + loss_streak_cooldown_bars * bar_seconds,
                )
                streak_since_cooldown = 0
        else:
            loss_streak = 0
            streak_since_cooldown = 0
        closed_trades.append({
            "trade_id": leg.trade_id,
            "side": leg.side,
            "volume": leg.volume,
            "entry": leg.entry,
            "exit": exit_px,
            "exit_reason": reason,
            "net_pnl_usd": round(net, 4),
            "entry_spread_usd": round(leg.entry_spread_usd, 4),
            "opened_at": leg.opened_at,
            "hold_seconds": round(max(0.0, closed_at - leg.opened_at), 2),
        })

    def _try_open_leg(
        *,
        sig: dict[str, Any],
        entry_px: float,
        opened_at: float,
        bar_idx: int,
        mark_price: float,
        via_pullback: bool = False,
    ) -> MomentumLeg | None:
        nonlocal next_id, peak_open_lots, peak_margin_used
        side = sig["side"]
        atr = sig["atr_m1"]
        struct_dist = (
            max(entry_px - sig["impulse_low"], sig["impulse_range"] * 0.85, atr * 1.1)
            if side == "buy"
            else max(sig["impulse_high"] + spread_price - entry_px, sig["impulse_range"] * 0.85, atr * 1.1)
        )
        min_budget_dist = max(0.0, (stop_usd / min(max_lot, vmax) - commission_per_lot) / multiplier)
        stop_dist = max(struct_dist, min_budget_dist, spread_price * 2.5, tick_size)
        per_lot_risk = stop_dist * multiplier + commission_per_lot
        if per_lot_risk <= 0:
            _refuse("invalid_risk_distance")
            return None
        raw_vol = stop_usd / per_lot_risk
        vol = round_to_step(min(raw_vol, max_lot, vmax), vstep, "down")
        if vol < vmin:
            _refuse("volume_below_minimum")
            return None

        # Enforce small-capital margin ceiling across concurrent legs.
        new_total_lots = sum(lg.volume for lg in open_legs) + vol
        est_margin = new_total_lots * contract_size * mark_price / leverage
        if est_margin > max_margin_usd + 1e-6:
            _refuse("margin_cap_exceeded")
            return None

        max_price_dist = (stop_usd - commission_per_lot * vol) / (vol * multiplier)
        if max_price_dist <= 0:
            _refuse("commission_exceeds_stop_budget")
            return None
        eff_dist = min(stop_dist, max_price_dist)
        stop_px = (entry_px - eff_dist) if side == "buy" else (entry_px + eff_dist)
        actual_risk = eff_dist * vol * multiplier + commission_per_lot * vol
        target_px: float | None = None
        if fixed_tp_r is not None and fixed_tp_r > 0:
            tp_dist = (fixed_tp_r * actual_risk + commission_per_lot * vol) / (vol * multiplier)
            target_px = (entry_px + tp_dist) if side == "buy" else (entry_px - tp_dist)

        next_id += 1
        leg = MomentumLeg(
            trade_id=next_id,
            side=side,
            volume=vol,
            entry=entry_px,
            stop=stop_px,
            multiplier=multiplier,
            commission_usd=commission_per_lot * vol,
            entry_spread_usd=spread_price * vol * multiplier,
            opened_at=opened_at,
            opened_bar_index=bar_idx,
            impulse_body=sig["impulse_body"],
            impulse_mid=sig["impulse_mid"],
            impulse_volume=float(sig.get("impulse_volume", 0.0)),
            initial_risk_usd=actual_risk,
            target=target_px,
            opened_via_pullback=via_pullback,
        )
        open_legs.append(leg)
        peak_open_lots = max(peak_open_lots, new_total_lots)
        peak_margin_used = max(peak_margin_used, est_margin)
        return leg

    last_bar: list[float] | None = None
    for seg in usable:
        atr_vals = atr_series(seg, period=14)
        pending_unfilled_pb_time: int | None = None
        for i in range(15, len(seg)):
            bar = seg[i]
            t, o, h, low, c = bar[0], bar[1], bar[2], bar[3], bar[4]
            bar_end = t + bar_seconds
            _sync_calendar(t)
            atr_now = atr_vals[i] or atr_vals[i - 1] or max(h - low, tick_size)

            # 1) Detect impulse from strictly closed bars <= i-1 (zero lookahead).
            sig: dict[str, Any] | None = None
            if entry_mode == "pullback_or_2bar":
                if pending_unfilled_pb_time is not None and pending_unfilled_pb_time == int(seg[i - 2][0]):
                    sig = detect_m1_impulse(
                        seg, atr_vals, i, entry_mode="impulse_2bar", spread_price=spread_price,
                        min_volume_ratio=min_volume_ratio,
                    )
                pending_unfilled_pb_time = None
                if sig is None:
                    sig = detect_m1_impulse(
                        seg, atr_vals, i, entry_mode="impulse_pullback", spread_price=spread_price,
                        min_volume_ratio=min_volume_ratio,
                    )
            else:
                sig = detect_m1_impulse(
                    seg, atr_vals, i, entry_mode=entry_mode, spread_price=spread_price,
                    min_volume_ratio=min_volume_ratio,
                )

            opened_this_bar: MomentumLeg | None = None
            if sig is not None:
                signals_detected += 1
                if blocked_hours_set and (int(t) // 3600) % 24 in blocked_hours_set:
                    _refuse("blocked_utc_hour")
                elif daily_loss_limit_usd is not None and daily_realized_pnl <= -daily_loss_limit_usd + 1e-9:
                    _refuse("daily_loss_brake")
                elif weekly_loss_limit_usd is not None and weekly_realized_pnl <= -weekly_loss_limit_usd + 1e-9:
                    _refuse("weekly_loss_brake")
                elif cooldown_until_epoch > 0.0 and t < cooldown_until_epoch:
                    _refuse("loss_streak_cooldown")
                else:
                    m5 = lookup_htf(m5_t, m5_ctx, int(t))
                    m15 = lookup_htf(m15_t, m15_ctx, int(t))
                    h1 = lookup_htf(h1_t, h1_ctx, int(t))
                    allowed, deny_reason = htf_allows_signal(
                        side=sig["side"], htf_filter=htf_filter, m5=m5, m15=m15, h1=h1,
                        min_m5_adx=min_m5_adx, min_mid_adx=min_mid_adx,
                    )
                    if not allowed:
                        _refuse(str(deny_reason))
                    else:
                        signals_passed_htf += 1
                        can_enter = False
                        if not open_legs:
                            can_enter = True
                        elif len(open_legs) < max_legs and all(lg.side == sig["side"] for lg in open_legs):
                            open_mark = o if sig["side"] == "buy" else o + spread_price
                            if all(
                                lg.stop_trailed
                                and _leg_pnl(lg, open_mark) >= (
                                    trail_arm_r * lg.initial_risk_usd
                                    if trail_arm_r is not None
                                    else trail_arm_usd
                                )
                                for lg in open_legs
                            ):
                                can_enter = True
                            else:
                                _refuse("pyramid_requires_breakeven_profit")
                        else:
                            _refuse("position_already_open")

                        if can_enter:
                            if sig["limit_trigger_bid"] is not None:
                                trig = float(sig["limit_trigger_bid"])
                                filled = (low <= trig) if sig["side"] == "buy" else (h >= trig)
                                if filled:
                                    entry_px = (min(o, trig) + spread_price) if sig["side"] == "buy" else max(o, trig)
                                    opened_this_bar = _try_open_leg(
                                        sig=sig, entry_px=entry_px, opened_at=t, bar_idx=i,
                                        mark_price=o, via_pullback=True,
                                    )
                                else:
                                    _refuse("pullback_unfilled")
                                    if entry_mode == "pullback_or_2bar":
                                        pending_unfilled_pb_time = int(sig["bar_time"])
                            else:
                                entry_px = (o + spread_price) if sig["side"] == "buy" else o
                                opened_this_bar = _try_open_leg(
                                    sig=sig, entry_px=entry_px, opened_at=t, bar_idx=i,
                                    mark_price=o, via_pullback=False,
                                )

            # 2) Intrabar stop / trailing-stop / target check (conservative: SL ALWAYS checked BEFORE TP).
            survivors: list[MomentumLeg] = []
            for leg in open_legs:
                if leg.side == "buy":
                    open_exit, worst_exit, best_exit = (
                        (leg.entry - spread_price if leg is opened_this_bar else o),
                        low,
                        h,
                    )
                    hit_sl = worst_exit <= leg.stop
                    fill_sl = min(open_exit, leg.stop)
                    allow_tp = leg.target is not None and not (leg is opened_this_bar and leg.opened_via_pullback)
                    hit_tp = allow_tp and best_exit >= float(leg.target)
                else:
                    open_exit, worst_exit, best_exit = (
                        (leg.entry + spread_price if leg is opened_this_bar else o + spread_price),
                        h + spread_price,
                        low + spread_price,
                    )
                    hit_sl = worst_exit >= leg.stop
                    fill_sl = max(open_exit, leg.stop)
                    allow_tp = leg.target is not None and not (leg is opened_this_bar and leg.opened_via_pullback)
                    hit_tp = allow_tp and best_exit <= float(leg.target)

                if hit_sl:
                    net_if_closed = _leg_pnl(leg, fill_sl)
                    reason = "trail_stop_hit" if (leg.stop_trailed and net_if_closed >= 0) else "stop_hit"
                    _close_leg(leg, exit_px=fill_sl, reason=reason, closed_at=bar_end)
                elif hit_tp and leg.target is not None:
                    _close_leg(leg, exit_px=leg.target, reason="target_hit", closed_at=bar_end)
                else:
                    survivors.append(leg)
            open_legs = survivors

            # 3) At bar close: fast invalidation cut, deceleration exit, or trailing/BE stop update.
            if open_legs:
                side = open_legs[0].side
                close_exit = c if side == "buy" else c + spread_price
                total_net = sum(_leg_pnl(lg, close_exit) for lg in open_legs)
                total_risk = sum(lg.initial_risk_usd for lg in open_legs)
                decel_threshold = (
                    min_decel_r * total_risk if min_decel_r is not None else min_decel_profit_usd
                )
                lead = open_legs[0]
                bars_held = i - lead.opened_bar_index + 1

                # 3a) Fast invalidation cut: if fresh impulse immediately fails back across midpoint in loss
                if (
                    fast_invalidation_cut
                    and len(open_legs) == 1
                    and bars_held <= 2
                    and total_net < 0
                    and (
                        (side == "buy" and c < o and c < lead.impulse_mid)
                        or (side == "sell" and c > o and c > lead.impulse_mid)
                    )
                ):
                    for lg in open_legs:
                        _close_leg(lg, exit_px=close_exit, reason="fast_invalidation_cut", closed_at=bar_end)
                    open_legs = []

                # 3b) Uncapped momentum deceleration or tick-volume dry-up exit while in profit
                elif (
                    exit_mode == "decel_and_trail"
                    and total_net >= decel_threshold - 1e-9
                    and (
                        _is_deceleration_bar(bar, side=side, impulse_body=lead.impulse_body)
                        or (
                            volume_dryup_ratio is not None
                            and volume_dryup_ratio > 0
                            and lead.impulse_volume > 0
                            and len(bar) >= 6
                            and bar[5] > 0
                            and bar[5] <= volume_dryup_ratio * lead.impulse_volume
                        )
                    )
                ):
                    decel_hit = _is_deceleration_bar(bar, side=side, impulse_body=lead.impulse_body)
                    reason_lbl = "momentum_deceleration" if decel_hit else "volume_dryup"
                    for lg in open_legs:
                        _close_leg(lg, exit_px=close_exit, reason=reason_lbl, closed_at=bar_end)
                    open_legs = []

                # 3c) Monotonic candle-low/high trailing stop (or BE-only lock for fixed_tp_and_be)
                else:
                    trail_buf = 0.15 * atr_now
                    for lg in open_legs:
                        leg_net = _leg_pnl(lg, close_exit)
                        arm_threshold = (
                            trail_arm_r * lg.initial_risk_usd if trail_arm_r is not None else trail_arm_usd
                        )
                        if leg_net >= arm_threshold - 1e-9:
                            if lg.side == "buy":
                                be_stop = lg.entry + max(tick_size, 0.05)
                                if exit_mode == "fixed_tp_and_be":
                                    new_stop = min(c - tick_size, max(lg.stop, be_stop))
                                else:
                                    candle_stop = low - trail_buf
                                    new_stop = min(c - tick_size, max(lg.stop, be_stop, candle_stop))
                                if new_stop > lg.stop:
                                    lg.stop = new_stop
                                    lg.stop_trailed = True
                            else:
                                be_stop = lg.entry - max(tick_size, 0.05)
                                if exit_mode == "fixed_tp_and_be":
                                    new_stop = max(c + spread_price + tick_size, min(lg.stop, be_stop))
                                else:
                                    candle_stop = h + spread_price + trail_buf
                                    new_stop = max(c + spread_price + tick_size, min(lg.stop, be_stop, candle_stop))
                                if new_stop < lg.stop:
                                    lg.stop = new_stop
                                    lg.stop_trailed = True

            _update_dd(c)
            last_bar = bar

    # Honest unrealized mark for any position still open at end of dataset.
    unrealized = 0.0
    if open_legs and last_bar is not None:
        end_c = last_bar[4]
        unrealized = sum(
            _leg_pnl(lg, end_c if lg.side == "buy" else end_c + spread_price)
            for lg in open_legs
        )

    wins = [t for t in closed_trades if t["net_pnl_usd"] > 0]
    losses = [t for t in closed_trades if t["net_pnl_usd"] < 0]
    realized = round(sum(t["net_pnl_usd"] for t in closed_trades), 4)
    gross_w = sum(t["net_pnl_usd"] for t in wins)
    gross_l = abs(sum(t["net_pnl_usd"] for t in losses))
    max_single_win = max((t["net_pnl_usd"] for t in closed_trades), default=0.0)
    max_single_loss = min((t["net_pnl_usd"] for t in closed_trades), default=0.0)
    gross_w_excl_top = max(0.0, gross_w - max(0.0, max_single_win))
    pnl_excl_largest_win = round(realized - max(0.0, max_single_win), 4)
    pf_excl_largest_win = round(gross_w_excl_top / gross_l, 3) if gross_l > 0 else None
    avg_win = round(gross_w / len(wins), 4) if wins else None
    avg_loss = round(gross_l / len(losses), 4) if losses else None
    payoff = round(avg_win / avg_loss, 3) if (avg_win and avg_loss and avg_loss > 0) else None
    pf = round(gross_w / gross_l, 3) if gross_l > 0 else None
    win_rate = round(len(wins) / len(closed_trades) * 100.0, 2) if closed_trades else None
    avg_vol = round(sum(t["volume"] for t in closed_trades) / len(closed_trades), 4) if closed_trades else None
    avg_spread = (
        round(sum(t["entry_spread_usd"] for t in closed_trades) / len(closed_trades), 4)
        if closed_trades else None
    )

    exit_reasons: dict[str, int] = {}
    for t in closed_trades:
        r = str(t["exit_reason"])
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    out: dict[str, Any] = {
        "ok": True,
        "paper": True,
        "executable": False,
        "execution_authorized": False,
        "orders_submitted": 0,
        "symbol": symbol,
        "entry_mode": entry_mode,
        "htf_filter": htf_filter,
        "exit_mode": exit_mode,
        "stop_usd": stop_usd,
        "max_lot": max_lot,
        "max_legs": max_legs,
        "pricing_basis": resolved_basis,
        "effective_multiplier": round(multiplier, 6),
        "signals_detected": signals_detected,
        "signals_passed_htf": signals_passed_htf,
        "trades_closed": len(closed_trades),
        "open_legs_at_end": len(open_legs),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate,
        "gross_profit_usd": round(gross_w, 4),
        "gross_loss_usd": round(gross_l, 4),
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "payoff_ratio": payoff,
        "max_single_win_usd": round(max_single_win, 4),
        "max_single_loss_usd": round(max_single_loss, 4),
        "pnl_excl_largest_win_usd": pnl_excl_largest_win,
        "pf_excl_largest_win": pf_excl_largest_win,
        "avg_volume": avg_vol,
        "avg_entry_spread_usd": avg_spread,
        "realized_pnl_usd": realized,
        "unrealized_pnl_usd": round(unrealized, 4),
        "total_marked_pnl_usd": round(realized + unrealized, 4),
        "max_drawdown_usd": round(max_drawdown, 4),
        "profit_factor": pf,
        "max_loss_streak": max_loss_streak,
        "peak_open_lots": round(peak_open_lots, 4),
        "peak_margin_used_usd": round(peak_margin_used, 2),
        "exit_reasons": exit_reasons,
        "refusal_counts": refusals,
    }
    if include_trades:
        out["trades"] = closed_trades
    return out


def replay_m1_momentum_matrix(
    m1_bars: list[list[float]],
    *,
    spec: dict[str, Any],
    symbol: str = "XAUUSD_i",
    spread_price: float = 0.0,
    m5_bars: list[list[float]] | None = None,
    m15_bars: list[list[float]] | None = None,
    h1_bars: list[list[float]] | None = None,
    stop_usd: float = 5.0,
    max_lot: float = 0.02,
    commission_per_lot: float = 0.0,
    leverage: float = 500.0,
) -> list[dict[str, Any]]:
    """Run a structured comparison grid answering entry, HTF, exit, and pyramiding questions."""
    m5_src = m5_bars if m5_bars else resample_m1_to_tf(m1_bars, 300)
    m15_src = m15_bars if m15_bars else resample_m1_to_tf(m1_bars, 900)
    h1_src = h1_bars if h1_bars else resample_m1_to_tf(m1_bars, 3600)
    htf_indices = (
        build_htf_index(m5_src, period_seconds=300, ema_fast=9, ema_slow=21),
        build_htf_index(m15_src, period_seconds=900, ema_fast=9, ema_slow=21),
        build_htf_index(h1_src, period_seconds=3600, ema_fast=20, ema_slow=50),
    )

    configs: list[tuple[str, dict[str, Any]]] = [
        # 1) Compare entry points under M5+M15 filter + deceleration & trail
        ("impulse_close | htf=none | decel+trail | 1-leg",
         {"entry_mode": "impulse_close", "htf_filter": "none", "exit_mode": "decel_and_trail", "max_legs": 1}),
        ("impulse_close | htf=m5+m15 | decel+trail | 1-leg",
         {"entry_mode": "impulse_close", "htf_filter": "m5_m15", "exit_mode": "decel_and_trail", "max_legs": 1}),
        ("impulse_close | htf=m5+m15+h1 | decel+trail | 1-leg",
         {"entry_mode": "impulse_close", "htf_filter": "m5_m15_h1", "exit_mode": "decel_and_trail", "max_legs": 1}),
        ("impulse_2bar | htf=m5+m15 | decel+trail | 1-leg",
         {"entry_mode": "impulse_2bar", "htf_filter": "m5_m15", "exit_mode": "decel_and_trail", "max_legs": 1}),
        ("impulse_pullback | htf=m5+m15 | decel+trail | 1-leg",
         {"entry_mode": "impulse_pullback", "htf_filter": "m5_m15", "exit_mode": "decel_and_trail", "max_legs": 1}),
        # 2) Compare deceleration exit vs pure candle-low trailing stop (uncapped runners!)
        ("impulse_close | htf=m5+m15 | trail_only | 1-leg",
         {"entry_mode": "impulse_close", "htf_filter": "m5_m15", "exit_mode": "trail_only", "max_legs": 1}),
        ("impulse_pullback | htf=m5+m15 | trail_only | 1-leg",
         {"entry_mode": "impulse_pullback", "htf_filter": "m5_m15", "exit_mode": "trail_only", "max_legs": 1}),
        ("impulse_close | htf=m5+m15+h1 | trail_only | 1-leg",
         {"entry_mode": "impulse_close", "htf_filter": "m5_m15_h1", "exit_mode": "trail_only", "max_legs": 1}),
        # 3) Test safe profit-only pyramiding (up to 2 legs, only after leg 1 is at breakeven+)
        ("impulse_close | htf=m5+m15 | decel+trail | 2-leg pyramid",
         {"entry_mode": "impulse_close", "htf_filter": "m5_m15", "exit_mode": "decel_and_trail", "max_legs": 2}),
        ("impulse_close | htf=m5+m15 | trail_only | 2-leg pyramid",
         {"entry_mode": "impulse_close", "htf_filter": "m5_m15", "exit_mode": "trail_only", "max_legs": 2}),
    ]
    rows: list[dict[str, Any]] = []
    for label, kw in configs:
        res = replay_m1_momentum(
            m1_bars,
            spec=spec,
            symbol=symbol,
            spread_price=spread_price,
            htf_indices=htf_indices,
            stop_usd=stop_usd,
            max_lot=max_lot,
            commission_per_lot=commission_per_lot,
            leverage=leverage,
            **kw,
        )
        rows.append({"scenario": label, **res})
    return rows


def _iso_utc(epoch: int | float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_m1_weekly_windows(
    m1_bars: list[list[float]],
    *,
    window_days: int = 7,
    min_bars_per_window: int = 500,
    bar_seconds: int = M1_SECONDS,
) -> list[dict[str, Any]]:
    """Split sorted OHLCV bars into non-overlapping chronological weekly slices.

    Slices are anchored backward from the newest bar so the most recent slice always
    covers a complete ``window_days`` calendar span, then returned oldest -> newest.
    Partial edge fragments with fewer than ``min_bars_per_window`` bars are excluded.
    """
    clean = clean_ohlcv_bars(m1_bars)
    if not clean:
        return []
    win_sec = window_days * 86400
    last_ts = int(clean[-1][0])
    first_ts = int(clean[0][0])

    raw_slices: list[list[list[float]]] = []
    end_ts = last_ts + bar_seconds
    while end_ts > first_ts:
        start_ts = end_ts - win_sec
        chunk = [b for b in clean if start_ts <= int(b[0]) < end_ts]
        if len(chunk) >= min_bars_per_window:
            raw_slices.append(chunk)
        end_ts = start_ts

    raw_slices.reverse()
    windows: list[dict[str, Any]] = []
    for idx, chunk in enumerate(raw_slices, start=1):
        s_ep = int(chunk[0][0])
        e_ep = int(chunk[-1][0])
        windows.append({
            "week": f"W{idx}",
            "start_epoch": s_ep,
            "end_epoch": e_ep,
            "start_utc": _iso_utc(s_ep),
            "end_utc": _iso_utc(e_ep),
            "m1_bars": len(chunk),
            "bars_count": len(chunk),
            "bars": chunk,
        })
    return windows


def split_chronological_halves(bars: list[list[float]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split sorted OHLCV bars cleanly at the exact calendar midpoint into Half-1 and Half-2."""
    clean = clean_ohlcv_bars(bars)
    if len(clean) < 2:
        empty = {"half": "H1", "start_utc": None, "end_utc": None, "bars_count": 0, "bars": []}
        return empty, {**empty, "half": "H2"}
    first_ts = int(clean[0][0])
    last_ts = int(clean[-1][0])
    mid_ts = (first_ts + last_ts) // 2
    first_half = [b for b in clean if int(b[0]) <= mid_ts]
    second_half = [b for b in clean if int(b[0]) > mid_ts]
    if not first_half or not second_half:
        cut = len(clean) // 2
        first_half, second_half = clean[:cut], clean[cut:]
    h1_meta = {
        "half": "Half1_Older",
        "start_utc": _iso_utc(first_half[0][0]),
        "end_utc": _iso_utc(first_half[-1][0]),
        "bars_count": len(first_half),
        "bars": first_half,
    }
    h2_meta = {
        "half": "Half2_Recent",
        "start_utc": _iso_utc(second_half[0][0]),
        "end_utc": _iso_utc(second_half[-1][0]),
        "bars_count": len(second_half),
        "bars": second_half,
    }
    return h1_meta, h2_meta


def compute_htf_coverage(
    m1_bars: list[list[float]],
    *,
    htf_indices: tuple[
        tuple[list[int], list[HTFContext]],
        tuple[list[int], list[HTFContext]],
        tuple[list[int], list[HTFContext]],
    ],
) -> dict[str, float]:
    """Measure what % of M1 timestamps have fully warmed-up causal M5/M15/H1 indicators."""
    clean = clean_ohlcv_bars(m1_bars)
    if not clean:
        return {"m5_pct": 0.0, "m15_pct": 0.0, "h1_pct": 0.0}
    (m5_t, m5_ctx), (m15_t, m15_ctx), (h1_t, h1_ctx) = htf_indices
    n = len(clean)
    ok_m5 = ok_m15 = ok_h1 = 0
    for row in clean:
        ts = int(row[0])
        c5 = lookup_htf(m5_t, m5_ctx, ts)
        if c5 is not None and c5.adx is not None and c5.ema_fast is not None and c5.ema_slow is not None:
            ok_m5 += 1
        c15 = lookup_htf(m15_t, m15_ctx, ts)
        if c15 is not None and c15.ema_slow is not None:
            ok_m15 += 1
        ch1 = lookup_htf(h1_t, h1_ctx, ts)
        if ch1 is not None and ch1.ema_fast is not None and ch1.ema_slow is not None:
            ok_h1 += 1
    return {
        "m5_pct": round(100.0 * ok_m5 / n, 2),
        "m15_pct": round(100.0 * ok_m15 / n, 2),
        "h1_pct": round(100.0 * ok_h1 / n, 2),
    }


def _session_bucket_utc(epoch: float) -> str:
    hr = datetime.fromtimestamp(float(epoch), tz=timezone.utc).hour
    if 0 <= hr < 7:
        return "asia_00_07"
    if 7 <= hr < 12:
        return "london_07_12"
    if 12 <= hr < 16:
        return "ny_overlap_12_16"
    if 16 <= hr < 21:
        return "ny_late_16_21"
    return "rollover_21_24"


def summarize_sessions_utc(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate closed trades by UTC session window (diagnostic only)."""
    buckets: dict[str, list[dict[str, Any]]] = {
        "asia_00_07": [],
        "london_07_12": [],
        "ny_overlap_12_16": [],
        "ny_late_16_21": [],
        "rollover_21_24": [],
    }
    for tr in trades:
        b = _session_bucket_utc(float(tr.get("opened_at", 0.0)))
        buckets[b].append(tr)
    out: dict[str, dict[str, Any]] = {}
    for name, rows in buckets.items():
        if not rows:
            out[name] = {"trades": 0, "win_rate_pct": None, "pnl_usd": 0.0, "profit_factor": None}
            continue
        wins = [r for r in rows if r["net_pnl_usd"] > 0]
        losses = [r for r in rows if r["net_pnl_usd"] < 0]
        gw = sum(r["net_pnl_usd"] for r in wins)
        gl = abs(sum(r["net_pnl_usd"] for r in losses))
        out[name] = {
            "trades": len(rows),
            "win_rate_pct": round(100.0 * len(wins) / len(rows), 2),
            "pnl_usd": round(sum(r["net_pnl_usd"] for r in rows), 4),
            "profit_factor": round(gw / gl, 3) if gl > 0 else None,
        }
    return out


DEFAULT_VALIDATION_CANDIDATES: list[dict[str, Any]] = [
    {
        "id": "C1",
        "label": "pullback | M5+M15 | decel+trail | $5",
        "entry_mode": "impulse_pullback",
        "htf_filter": "m5_m15",
        "exit_mode": "decel_and_trail",
        "stop_usd": 5.0,
    },
    {
        "id": "C2",
        "label": "pullback | M5+M15+H1 | decel+trail | $5",
        "entry_mode": "impulse_pullback",
        "htf_filter": "m5_m15_h1",
        "exit_mode": "decel_and_trail",
        "stop_usd": 5.0,
    },
    {
        "id": "C3",
        "label": "close | M5+M15+H1 | decel+trail | $5",
        "entry_mode": "impulse_close",
        "htf_filter": "m5_m15_h1",
        "exit_mode": "decel_and_trail",
        "stop_usd": 5.0,
    },
    {
        "id": "C4",
        "label": "pullback_or_2bar | M5+M15+H1 | decel+trail | $5",
        "entry_mode": "pullback_or_2bar",
        "htf_filter": "m5_m15_h1",
        "exit_mode": "decel_and_trail",
        "stop_usd": 5.0,
    },
    {
        "id": "C5",
        "label": "2bar | M5+M15 | decel+trail | $8",
        "entry_mode": "impulse_2bar",
        "htf_filter": "m5_m15",
        "exit_mode": "decel_and_trail",
        "stop_usd": 8.0,
    },
    {
        "id": "C6",
        "label": "2bar | M5+M15+H1 | decel+trail | $8",
        "entry_mode": "impulse_2bar",
        "htf_filter": "m5_m15_h1",
        "exit_mode": "decel_and_trail",
        "stop_usd": 8.0,
    },
    {
        "id": "C7",
        "label": "close | M5+M15+H1 | trail_only | $8",
        "entry_mode": "impulse_close",
        "htf_filter": "m5_m15_h1",
        "exit_mode": "trail_only",
        "stop_usd": 8.0,
    },
    {
        "id": "C8",
        "label": "pullback_or_2bar | M5+M15+H1 | trail_only | $8",
        "entry_mode": "pullback_or_2bar",
        "htf_filter": "m5_m15_h1",
        "exit_mode": "trail_only",
        "stop_usd": 8.0,
    },
]


def validate_m1_momentum_candidates(
    m1_bars: list[list[float]],
    *,
    spec: dict[str, Any],
    symbol: str = "XAUUSD_i",
    spread_price: float = 0.34,
    m5_bars: list[list[float]] | None = None,
    m15_bars: list[list[float]] | None = None,
    h1_bars: list[list[float]] | None = None,
    max_lot: float = 0.02,
    leverage: float = 500.0,
    candidates: list[dict[str, Any]] | None = None,
    window_days: int = 7,
    min_bars_per_window: int = 500,
    adx_thresholds: tuple[float, ...] = (14.0, 18.0, 22.0, 25.0),
) -> dict[str, Any]:
    """Run multi-week walk-forward, cost-stress, and ADX-sensitivity validation.

    Strictly single-leg (max_legs=1), small-capital bounded (max_margin_usd=25),
    conservative pricing_basis='auto', and strictly causal across M1/M5/M15/H1.
    """
    clean_m1 = clean_ohlcv_bars(m1_bars)
    m5_src = m5_bars if m5_bars else resample_m1_to_tf(clean_m1, 300)
    m15_src = m15_bars if m15_bars else resample_m1_to_tf(clean_m1, 900)
    h1_src = h1_bars if h1_bars else resample_m1_to_tf(clean_m1, 3600)
    htf_indices = (
        build_htf_index(m5_src, period_seconds=300, ema_fast=9, ema_slow=21),
        build_htf_index(m15_src, period_seconds=900, ema_fast=9, ema_slow=21),
        build_htf_index(h1_src, period_seconds=3600, ema_fast=20, ema_slow=50),
    )
    coverage = compute_htf_coverage(clean_m1, htf_indices=htf_indices)
    weekly_windows = split_m1_weekly_windows(
        clean_m1, window_days=window_days, min_bars_per_window=min_bars_per_window,
    )

    cost_scenarios: list[tuple[str, float, float]] = [
        ("base_live_spread", spread_price, 0.0),
        ("stress_spread_050", max(spread_price, 0.50), 0.0),
        ("stress_spread_050_comm5", max(spread_price, 0.50), 5.0),
        ("stress_spread_070_comm7", max(spread_price, 0.70), 7.0),
    ]

    cand_list = candidates if candidates is not None else DEFAULT_VALIDATION_CANDIDATES
    results: list[dict[str, Any]] = []

    for c in cand_list:
        base_kw = {
            "spec": spec,
            "symbol": symbol,
            "htf_indices": htf_indices,
            "entry_mode": c["entry_mode"],
            "htf_filter": c["htf_filter"],
            "exit_mode": c["exit_mode"],
            "stop_usd": float(c["stop_usd"]),
            "max_lot": max_lot,
            "max_legs": 1,
            "leverage": leverage,
            "max_margin_usd": 25.0,
        }

        # 1) Full-period baseline + UTC session breakdown
        full = replay_m1_momentum(
            clean_m1, spread_price=spread_price, commission_per_lot=0.0,
            min_m5_adx=18.0, include_trades=True, **base_kw,
        )
        trades = full.pop("trades", [])
        sessions = summarize_sessions_utc(trades)

        # 2) Independent non-overlapping weekly walk-forward slices
        weeks_out: list[dict[str, Any]] = []
        for win in weekly_windows:
            w_res = replay_m1_momentum(
                win["bars"], spread_price=spread_price, commission_per_lot=0.0,
                min_m5_adx=18.0, **base_kw,
            )
            weeks_out.append({
                "week": win["week"],
                "start_utc": win["start_utc"],
                "end_utc": win["end_utc"],
                "m1_bars": win["m1_bars"],
                "trades": w_res["trades_closed"],
                "win_rate_pct": w_res["win_rate_pct"],
                "pnl_usd": w_res["total_marked_pnl_usd"],
                "max_dd_usd": w_res["max_drawdown_usd"],
                "pf": w_res["profit_factor"],
            })

        total_weeks = len(weeks_out)
        pos_weeks = sum(1 for w in weeks_out if w["pnl_usd"] > 0)
        pf11_weeks = sum(1 for w in weeks_out if (w["pf"] or 0.0) >= 1.10)
        worst_week_pnl = round(min((w["pnl_usd"] for w in weeks_out), default=0.0), 4)

        # 3) Spread + commission stress ladder
        stress_out: dict[str, dict[str, Any]] = {}
        for s_name, s_sp, s_comm in cost_scenarios:
            if s_name == "base_live_spread":
                s_res = full
            else:
                s_res = replay_m1_momentum(
                    clean_m1, spread_price=s_sp, commission_per_lot=s_comm,
                    min_m5_adx=18.0, **base_kw,
                )
            stress_out[s_name] = {
                "spread_price": round(s_sp, 4),
                "commission_per_lot": round(s_comm, 2),
                "trades": s_res["trades_closed"],
                "pnl_usd": s_res["total_marked_pnl_usd"],
                "max_dd_usd": s_res["max_drawdown_usd"],
                "pf": s_res["profit_factor"],
            }

        # 4) ADX threshold sensitivity sweep
        adx_out: list[dict[str, Any]] = []
        for adx_th in adx_thresholds:
            if abs(adx_th - 18.0) < 1e-9:
                a_res = full
            else:
                a_res = replay_m1_momentum(
                    clean_m1, spread_price=spread_price, commission_per_lot=0.0,
                    min_m5_adx=adx_th, **base_kw,
                )
            adx_out.append({
                "min_m5_adx": adx_th,
                "trades": a_res["trades_closed"],
                "pnl_usd": a_res["total_marked_pnl_usd"],
                "max_dd_usd": a_res["max_drawdown_usd"],
                "pf": a_res["profit_factor"],
            })
        adx_pos = sum(1 for a in adx_out if a["pnl_usd"] > 0 and (a["pf"] or 0.0) >= 1.05)

        # 5) Objective anti-overfitting gate checklist
        s050 = stress_out["stress_spread_050"]
        gates = {
            "sample_ge_40": full["trades_closed"] >= 40,
            "full_pnl_positive_and_pf_ge_1_15": (
                full["total_marked_pnl_usd"] > 0 and (full["profit_factor"] or 0.0) >= 1.15
            ),
            "drawdown_le_50": full["max_drawdown_usd"] <= 50.0,
            "margin_le_25": full["peak_margin_used_usd"] <= 25.0,
            "weekly_consistency": (
                total_weeks >= 3
                and (pos_weeks / total_weeks) >= 0.75
                and worst_week_pnl >= -25.0
            ),
            "survives_spread_050": (
                s050["pnl_usd"] > 0 and (s050["pf"] or 0.0) >= 1.05
            ),
            "adx_sensitivity_stable": adx_pos >= max(3, len(adx_thresholds) - 1),
        }
        failed_gates = [k for k, ok in gates.items() if not ok]
        overall_pass = len(failed_gates) == 0

        results.append({
            "id": c["id"],
            "label": c["label"],
            "config": {
                "entry_mode": c["entry_mode"],
                "htf_filter": c["htf_filter"],
                "exit_mode": c["exit_mode"],
                "stop_usd": float(c["stop_usd"]),
                "max_lot": max_lot,
                "max_legs": 1,
            },
            "full_period": full,
            "weekly_walkforward": {
                "total_weeks": total_weeks,
                "positive_weeks": pos_weeks,
                "weeks_pf_ge_1_10": pf11_weeks,
                "worst_week_pnl_usd": worst_week_pnl,
                "weeks": weeks_out,
            },
            "cost_stress": stress_out,
            "adx_sensitivity": {
                "stable_thresholds_count": adx_pos,
                "total_thresholds": len(adx_thresholds),
                "sweeps": adx_out,
            },
            "sessions_utc": sessions,
            "gates": gates,
            "failed_gates": failed_gates,
            "overall_pass": overall_pass,
        })

    passed = [r for r in results if r["overall_pass"]]
    passed.sort(
        key=lambda r: (
            r["cost_stress"]["stress_spread_050"]["pf"] or 0.0,
            r["full_period"]["total_marked_pnl_usd"],
        ),
        reverse=True,
    )
    # Also identify best-performing candidate for diagnostic review even if none passed all gates.
    ranked_all = sorted(
        results,
        key=lambda r: (
            1 if r["overall_pass"] else 0,
            sum(1 for ok in r["gates"].values() if ok),
            r["full_period"]["profit_factor"] or 0.0,
            r["full_period"]["total_marked_pnl_usd"],
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
        "weekly_windows_meta": [
            {k: v for k, v in w.items() if k != "bars"} for w in weekly_windows
        ],
        "candidates": results,
        "verdict": {
            "any_candidate_passed": len(passed) > 0,
            "passed_ids": [r["id"] for r in passed],
            "recommended_for_live_paper": passed[0]["id"] if passed else None,
            "best_diagnostic_id": ranked_all[0]["id"] if ranked_all else None,
            "status": (
                "PASS_READY_FOR_BOUNDED_LIVE_PAPER"
                if passed
                else "FAIL_DO_NOT_PROMOTE_TO_DEMO_OR_LIVE"
            ),
        },
    }


ROLLOVER_BLOCKED_UTC_HOURS: tuple[int, ...] = (21, 22, 23)

DEFAULT_M5_VALIDATION_CANDIDATES: list[dict[str, Any]] = [
    {
        "id": "M5_1",
        "label": "M5 pullback | M15+H1 | decel>=1.2R+trail | $10 | 0.01lot",
        "entry_mode": "impulse_pullback",
        "htf_filter": "m15_h1",
        "exit_mode": "decel_and_trail",
        "stop_usd": 10.0,
        "trail_arm_r": 0.8,
        "min_decel_r": 1.2,
        "fixed_tp_r": None,
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
    },
    {
        "id": "M5_2",
        "label": "M5 pullback | M15+H1+H4 | decel>=1.2R+trail | $10 | 0.01lot",
        "entry_mode": "impulse_pullback",
        "htf_filter": "m15_h1_h4",
        "exit_mode": "decel_and_trail",
        "stop_usd": 10.0,
        "trail_arm_r": 0.8,
        "min_decel_r": 1.2,
        "fixed_tp_r": None,
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
    },
    {
        "id": "M5_3",
        "label": "M5 close | M15+H1+H4 | trail_only (arm 0.8R) | $12 | 0.01lot",
        "entry_mode": "impulse_close",
        "htf_filter": "m15_h1_h4",
        "exit_mode": "trail_only",
        "stop_usd": 12.0,
        "trail_arm_r": 0.8,
        "min_decel_r": None,
        "fixed_tp_r": None,
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
    },
    {
        "id": "M5_4",
        "label": "M5 2bar | M15+H1+H4 | decel>=1.2R+trail | $12 | 0.01lot",
        "entry_mode": "impulse_2bar",
        "htf_filter": "m15_h1_h4",
        "exit_mode": "decel_and_trail",
        "stop_usd": 12.0,
        "trail_arm_r": 0.8,
        "min_decel_r": 1.2,
        "fixed_tp_r": None,
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
    },
    {
        "id": "M5_5",
        "label": "M5 pullback_or_2bar | M15+H1+H4 | trail_only (arm 0.8R) | $12 | 0.01lot",
        "entry_mode": "pullback_or_2bar",
        "htf_filter": "m15_h1_h4",
        "exit_mode": "trail_only",
        "stop_usd": 12.0,
        "trail_arm_r": 0.8,
        "min_decel_r": None,
        "fixed_tp_r": None,
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
    },
    {
        "id": "M5_6",
        "label": "M5 pullback | M15+H1+H4 | fixed_tp 1.8R + BE(1.0R) | $12 | 0.01lot",
        "entry_mode": "impulse_pullback",
        "htf_filter": "m15_h1_h4",
        "exit_mode": "fixed_tp_and_be",
        "stop_usd": 12.0,
        "trail_arm_r": 1.0,
        "min_decel_r": None,
        "fixed_tp_r": 1.8,
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
    },
]

DEFAULT_M1_FINAL_CANDIDATES: list[dict[str, Any]] = [
    {
        "id": "M1_1",
        "label": "M1 pullback | M5+M15+H1 | decel>=1.2R+trail | $5 | no-rollover",
        "entry_mode": "impulse_pullback",
        "htf_filter": "m5_m15_h1",
        "exit_mode": "decel_and_trail",
        "stop_usd": 5.0,
        "trail_arm_r": 0.8,
        "min_decel_r": 1.2,
        "fixed_tp_r": None,
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
    },
    {
        "id": "M1_2",
        "label": "M1 2bar | M5+M15+H1 | decel>=1.2R+trail | $5 | no-rollover",
        "entry_mode": "impulse_2bar",
        "htf_filter": "m5_m15_h1",
        "exit_mode": "decel_and_trail",
        "stop_usd": 5.0,
        "trail_arm_r": 0.8,
        "min_decel_r": 1.2,
        "fixed_tp_r": None,
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
    },
    {
        "id": "M1_3",
        "label": "M1 pullback | M5+M15+H1 | fixed_tp 1.5R + BE(0.8R) | $5 | no-rollover",
        "entry_mode": "impulse_pullback",
        "htf_filter": "m5_m15_h1",
        "exit_mode": "fixed_tp_and_be",
        "stop_usd": 5.0,
        "trail_arm_r": 0.8,
        "min_decel_r": None,
        "fixed_tp_r": 1.5,
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
    },
]


def validate_timeframe_candidates(
    exec_bars: list[list[float]],
    *,
    timeframe: str,
    spec: dict[str, Any],
    symbol: str = "XAUUSD_i",
    spread_price: float = 0.34,
    primary_htf_bars: list[list[float]] | None = None,
    mid_htf_bars: list[list[float]] | None = None,
    macro_htf_bars: list[list[float]] | None = None,
    max_lot: float = 0.01,
    leverage: float = 500.0,
    candidates: list[dict[str, Any]] | None = None,
    window_days: int = 7,
    min_bars_per_window: int | None = None,
    adx_thresholds: tuple[float, ...] = (14.0, 18.0, 22.0, 25.0),
) -> dict[str, Any]:
    """Run strict 8-gate validation (full + halves + weekly + cost stress + ADX) on M1 or M5.

    For ``timeframe == 'M5'``, HTF slots map to ``(M15, H1, H4)``.
    For ``timeframe == 'M1'``, HTF slots map to ``(M5, M15, H1)``.
    """
    tf = timeframe.upper()
    if tf not in ("M1", "M5"):
        raise ValueError("timeframe must be 'M1' or 'M5'")
    bar_seconds = 300 if tf == "M5" else 60
    clean_exec = clean_ohlcv_bars(exec_bars)

    if tf == "M5":
        p_src = primary_htf_bars if primary_htf_bars else resample_bars_to_tf(clean_exec, src_seconds=300, dst_seconds=900)
        m_src = mid_htf_bars if mid_htf_bars else resample_bars_to_tf(clean_exec, src_seconds=300, dst_seconds=3600)
        g_src = macro_htf_bars if macro_htf_bars else resample_bars_to_tf(m_src, src_seconds=3600, dst_seconds=14400)
        htf_indices = (
            build_htf_index(p_src, period_seconds=900, ema_fast=9, ema_slow=21),
            build_htf_index(m_src, period_seconds=3600, ema_fast=9, ema_slow=21),
            build_htf_index(g_src, period_seconds=14400, ema_fast=20, ema_slow=50),
        )
        cov_raw = compute_htf_coverage(clean_exec, htf_indices=htf_indices)
        coverage = {"m15_pct": cov_raw["m5_pct"], "h1_pct": cov_raw["m15_pct"], "h4_pct": cov_raw["h1_pct"]}
        default_min_bars = 700  # ~2.5 trading days of M5 so 1-day fragments don't distort weekly gates
        cand_list = candidates if candidates is not None else DEFAULT_M5_VALIDATION_CANDIDATES
    else:
        p_src = primary_htf_bars if primary_htf_bars else resample_m1_to_tf(clean_exec, 300)
        m_src = mid_htf_bars if mid_htf_bars else resample_m1_to_tf(clean_exec, 900)
        g_src = macro_htf_bars if macro_htf_bars else resample_m1_to_tf(clean_exec, 3600)
        htf_indices = (
            build_htf_index(p_src, period_seconds=300, ema_fast=9, ema_slow=21),
            build_htf_index(m_src, period_seconds=900, ema_fast=9, ema_slow=21),
            build_htf_index(g_src, period_seconds=3600, ema_fast=20, ema_slow=50),
        )
        coverage = compute_htf_coverage(clean_exec, htf_indices=htf_indices)
        default_min_bars = 3500  # ~2.5 trading days of M1 so 1-day edge slices are excluded
        cand_list = candidates if candidates is not None else DEFAULT_M1_FINAL_CANDIDATES

    eff_min_bars = default_min_bars if min_bars_per_window is None else min_bars_per_window
    weekly_windows = split_m1_weekly_windows(
        clean_exec, window_days=window_days, min_bars_per_window=eff_min_bars, bar_seconds=bar_seconds,
    )
    half1, half2 = split_chronological_halves(clean_exec)

    cost_scenarios: list[tuple[str, float, float]] = [
        ("base_live_spread", spread_price, 0.0),
        ("stress_spread_050", max(spread_price, 0.50), 0.0),
        ("stress_spread_050_comm5", max(spread_price, 0.50), 5.0),
        ("stress_spread_070_comm7", max(spread_price, 0.70), 7.0),
    ]

    results: list[dict[str, Any]] = []
    for c in cand_list:
        base_kw: dict[str, Any] = {
            "spec": spec,
            "symbol": symbol,
            "bar_seconds": bar_seconds,
            "htf_indices": htf_indices,
            "entry_mode": c["entry_mode"],
            "htf_filter": c["htf_filter"],
            "exit_mode": c["exit_mode"],
            "stop_usd": float(c["stop_usd"]),
            "max_lot": max_lot,
            "max_legs": 1,
            "leverage": leverage,
            "max_margin_usd": 25.0,
            "blocked_utc_hours": tuple(c.get("blocked_utc_hours", ())),
            "trail_arm_r": c.get("trail_arm_r"),
            "min_decel_r": c.get("min_decel_r"),
            "fixed_tp_r": c.get("fixed_tp_r"),
        }

        # 1) Full-period baseline + UTC session breakdown
        full = replay_m1_momentum(
            clean_exec, spread_price=spread_price, commission_per_lot=0.0,
            min_m5_adx=18.0, include_trades=True, **base_kw,
        )
        trades = full.pop("trades", [])
        sessions = summarize_sessions_utc(trades)

        # 2) Chronological Half-1 vs Half-2 out-of-sample split
        h1_res = replay_m1_momentum(
            half1["bars"], spread_price=spread_price, commission_per_lot=0.0, min_m5_adx=18.0, **base_kw,
        )
        h2_res = replay_m1_momentum(
            half2["bars"], spread_price=spread_price, commission_per_lot=0.0, min_m5_adx=18.0, **base_kw,
        )
        halves_out = {
            "half1_older": {
                "start_utc": half1["start_utc"],
                "end_utc": half1["end_utc"],
                "bars": half1["bars_count"],
                "trades": h1_res["trades_closed"],
                "win_rate_pct": h1_res["win_rate_pct"],
                "pnl_usd": h1_res["total_marked_pnl_usd"],
                "max_dd_usd": h1_res["max_drawdown_usd"],
                "pf": h1_res["profit_factor"],
            },
            "half2_recent": {
                "start_utc": half2["start_utc"],
                "end_utc": half2["end_utc"],
                "bars": half2["bars_count"],
                "trades": h2_res["trades_closed"],
                "win_rate_pct": h2_res["win_rate_pct"],
                "pnl_usd": h2_res["total_marked_pnl_usd"],
                "max_dd_usd": h2_res["max_drawdown_usd"],
                "pf": h2_res["profit_factor"],
            },
        }

        # 3) Non-overlapping weekly walk-forward slices
        weeks_out: list[dict[str, Any]] = []
        for win in weekly_windows:
            w_res = replay_m1_momentum(
                win["bars"], spread_price=spread_price, commission_per_lot=0.0,
                min_m5_adx=18.0, **base_kw,
            )
            weeks_out.append({
                "week": win["week"],
                "start_utc": win["start_utc"],
                "end_utc": win["end_utc"],
                "bars": win["bars_count"],
                "trades": w_res["trades_closed"],
                "win_rate_pct": w_res["win_rate_pct"],
                "pnl_usd": w_res["total_marked_pnl_usd"],
                "max_dd_usd": w_res["max_drawdown_usd"],
                "pf": w_res["profit_factor"],
            })

        total_weeks = len(weeks_out)
        pos_weeks = sum(1 for w in weeks_out if w["pnl_usd"] > 0)
        pf11_weeks = sum(1 for w in weeks_out if (w["pf"] or 0.0) >= 1.10)
        worst_week_pnl = round(min((w["pnl_usd"] for w in weeks_out), default=0.0), 4)

        # 4) Cost stress ladder
        stress_out: dict[str, dict[str, Any]] = {}
        for s_name, s_sp, s_comm in cost_scenarios:
            s_res = full if s_name == "base_live_spread" else replay_m1_momentum(
                clean_exec, spread_price=s_sp, commission_per_lot=s_comm, min_m5_adx=18.0, **base_kw,
            )
            stress_out[s_name] = {
                "spread_price": round(s_sp, 4),
                "commission_per_lot": round(s_comm, 2),
                "trades": s_res["trades_closed"],
                "pnl_usd": s_res["total_marked_pnl_usd"],
                "max_dd_usd": s_res["max_drawdown_usd"],
                "pf": s_res["profit_factor"],
            }

        # 5) ADX threshold sensitivity sweep
        adx_out: list[dict[str, Any]] = []
        for adx_th in adx_thresholds:
            a_res = full if abs(adx_th - 18.0) < 1e-9 else replay_m1_momentum(
                clean_exec, spread_price=spread_price, commission_per_lot=0.0, min_m5_adx=adx_th, **base_kw,
            )
            adx_out.append({
                "min_htf_adx": adx_th,
                "trades": a_res["trades_closed"],
                "pnl_usd": a_res["total_marked_pnl_usd"],
                "max_dd_usd": a_res["max_drawdown_usd"],
                "pf": a_res["profit_factor"],
            })
        adx_pos = sum(1 for a in adx_out if a["pnl_usd"] > 0 and (a["pf"] or 0.0) >= 1.05)

        # 6) Strict 8-gate anti-overfitting checklist
        s050 = stress_out["stress_spread_050"]
        h1_o = halves_out["half1_older"]
        h2_r = halves_out["half2_recent"]
        gates = {
            "sample_ge_40": full["trades_closed"] >= 40,
            "full_pnl_positive_and_pf_ge_1_15": (
                full["total_marked_pnl_usd"] > 0 and (full["profit_factor"] or 0.0) >= 1.15
            ),
            "drawdown_le_50": full["max_drawdown_usd"] <= 50.0,
            "margin_le_25": full["peak_margin_used_usd"] <= 25.0,
            "weekly_consistency": (
                total_weeks >= 3
                and (pos_weeks / total_weeks) >= 0.65
                and worst_week_pnl >= -25.0
            ),
            "both_halves_positive": (
                h1_o["pnl_usd"] > 0 and (h1_o["pf"] or 0.0) >= 1.05
                and h2_r["pnl_usd"] > 0 and (h2_r["pf"] or 0.0) >= 1.05
            ),
            "survives_spread_050": (
                s050["pnl_usd"] > 0 and (s050["pf"] or 0.0) >= 1.05
            ),
            "adx_sensitivity_stable": adx_pos >= max(3, len(adx_thresholds) - 1),
        }
        failed_gates = [k for k, ok in gates.items() if not ok]
        overall_pass = len(failed_gates) == 0

        results.append({
            "id": c["id"],
            "timeframe": tf,
            "label": c["label"],
            "config": {
                "timeframe": tf,
                "entry_mode": c["entry_mode"],
                "htf_filter": c["htf_filter"],
                "exit_mode": c["exit_mode"],
                "stop_usd": float(c["stop_usd"]),
                "max_lot": max_lot,
                "max_legs": 1,
                "trail_arm_r": c.get("trail_arm_r"),
                "min_decel_r": c.get("min_decel_r"),
                "fixed_tp_r": c.get("fixed_tp_r"),
                "blocked_utc_hours": list(c.get("blocked_utc_hours", ())),
            },
            "full_period": full,
            "chronological_halves": halves_out,
            "weekly_walkforward": {
                "total_weeks": total_weeks,
                "positive_weeks": pos_weeks,
                "weeks_pf_ge_1_10": pf11_weeks,
                "worst_week_pnl_usd": worst_week_pnl,
                "weeks": weeks_out,
            },
            "cost_stress": stress_out,
            "adx_sensitivity": {
                "stable_thresholds_count": adx_pos,
                "total_thresholds": len(adx_thresholds),
                "sweeps": adx_out,
            },
            "sessions_utc": sessions,
            "gates": gates,
            "failed_gates": failed_gates,
            "overall_pass": overall_pass,
        })

    passed = [r for r in results if r["overall_pass"]]
    passed.sort(
        key=lambda r: (
            r["cost_stress"]["stress_spread_050"]["pf"] or 0.0,
            r["full_period"]["total_marked_pnl_usd"],
        ),
        reverse=True,
    )
    ranked_all = sorted(
        results,
        key=lambda r: (
            1 if r["overall_pass"] else 0,
            sum(1 for ok in r["gates"].values() if ok),
            r["full_period"]["profit_factor"] or 0.0,
            r["full_period"]["total_marked_pnl_usd"],
        ),
        reverse=True,
    )

    return {
        "ok": True,
        "paper": True,
        "timeframe": tf,
        "executable": False,
        "execution_authorized": False,
        "orders_submitted": 0,
        "htf_coverage_pct": coverage,
        "weekly_windows_meta": [
            {k: v for k, v in w.items() if k != "bars"} for w in weekly_windows
        ],
        "candidates": results,
        "verdict": {
            "any_candidate_passed": len(passed) > 0,
            "passed_ids": [r["id"] for r in passed],
            "recommended_for_live_paper": passed[0]["id"] if passed else None,
            "best_diagnostic_id": ranked_all[0]["id"] if ranked_all else None,
            "status": (
                "PASS_READY_FOR_BOUNDED_LIVE_PAPER"
                if passed
                else "FAIL_DO_NOT_PROMOTE_TO_DEMO_OR_LIVE"
            ),
        },
    }


def summarize_months_utc(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate closed trades chronologically by UTC calendar month (YYYY-MM)."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for tr in trades:
        ym = datetime.fromtimestamp(float(tr.get("opened_at", 0.0)), tz=timezone.utc).strftime("%Y-%m")
        buckets.setdefault(ym, []).append(tr)
    out: list[dict[str, Any]] = []
    for ym in sorted(buckets):
        rows = buckets[ym]
        wins = [r for r in rows if r["net_pnl_usd"] > 0]
        losses = [r for r in rows if r["net_pnl_usd"] < 0]
        gw = sum(r["net_pnl_usd"] for r in wins)
        gl = abs(sum(r["net_pnl_usd"] for r in losses))
        net = sum(r["net_pnl_usd"] for r in rows)
        best = max((r["net_pnl_usd"] for r in rows), default=0.0)
        worst = min((r["net_pnl_usd"] for r in rows), default=0.0)
        out.append({
            "month": ym,
            "trades": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(100.0 * len(wins) / len(rows), 2) if rows else None,
            "gross_profit_usd": round(gw, 2),
            "gross_loss_usd": round(gl, 2),
            "net_pnl_usd": round(net, 2),
            "best_trade_usd": round(best, 2),
            "worst_trade_usd": round(worst, 2),
            "pf": round(gw / gl, 3) if gl > 0 else None,
        })
    return out


LOCKED_M5_OOS_STRATEGIES: list[dict[str, Any]] = [
    {
        "id": "M5_5_LOCKED",
        "role": "primary_candidate",
        "label": "M5 pullback_or_2bar | M15+H1+H4 | trail_only(0.8R) | $12 | 0.01lot | no-rollover",
        "entry_mode": "pullback_or_2bar",
        "htf_filter": "m15_h1_h4",
        "exit_mode": "trail_only",
        "stop_usd": 12.0,
        "trail_arm_r": 0.8,
        "min_decel_r": None,
        "fixed_tp_r": None,
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
    },
    {
        "id": "M5_3_LOCKED",
        "role": "sibling_control",
        "label": "M5 close | M15+H1+H4 | trail_only(0.8R) | $12 | 0.01lot | no-rollover",
        "entry_mode": "impulse_close",
        "htf_filter": "m15_h1_h4",
        "exit_mode": "trail_only",
        "stop_usd": 12.0,
        "trail_arm_r": 0.8,
        "min_decel_r": None,
        "fixed_tp_r": None,
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
    },
]


def _plain_trade_summary(res: dict[str, Any]) -> dict[str, Any]:
    """Concise, plain-dollar summary of a replay result."""
    return {
        "trades": res["trades_closed"],
        "wins": res["wins"],
        "losses": res["losses"],
        "win_rate_pct": res["win_rate_pct"],
        "gross_won_usd": round(float(res["gross_profit_usd"]), 2),
        "gross_lost_usd": round(float(res["gross_loss_usd"]), 2),
        "net_pnl_usd": round(float(res["total_marked_pnl_usd"]), 2),
        "avg_win_usd": round(float(res["avg_win_usd"]), 2) if res["avg_win_usd"] is not None else None,
        "avg_loss_usd": round(float(res["avg_loss_usd"]), 2) if res["avg_loss_usd"] is not None else None,
        "payoff_ratio": res["payoff_ratio"],
        "best_trade_usd": round(float(res["max_single_win_usd"]), 2),
        "worst_trade_usd": round(float(res["max_single_loss_usd"]), 2),
        "net_without_best_trade_usd": round(float(res["pnl_excl_largest_win_usd"]), 2),
        "pf": res["profit_factor"],
        "pf_without_best_trade": res["pf_excl_largest_win"],
        "max_dd_usd": round(float(res["max_drawdown_usd"]), 2),
        "peak_margin_usd": round(float(res["peak_margin_used_usd"]), 2),
        "exit_reasons": res["exit_reasons"],
    }


def evaluate_locked_m5_oos(
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
    recent_seen_days: int = 60,
    strategies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """True Out-of-Sample evaluation for locked M5 strategies over 180+ days.

    Splits M5 cleanly into:
      - ``unseen_older_oos``: strictly BEFORE the most recent ``recent_seen_days``
        (completely unseen during prior 60-day selection!)
      - ``seen_recent_window``: the most recent ``recent_seen_days``
      - ``full_period``: entire fetched history + month-by-month breakdown.
    """
    clean_m5 = clean_ohlcv_bars(m5_bars)
    if len(clean_m5) < 50:
        raise ValueError("insufficient M5 bars for out-of-sample evaluation")

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
    cutoff_ts = last_ts - recent_seen_days * 86400
    unseen_bars = [b for b in clean_m5 if int(b[0]) < cutoff_ts]
    recent_bars = [b for b in clean_m5 if int(b[0]) >= cutoff_ts]
    unseen_span_days = round((unseen_bars[-1][0] - unseen_bars[0][0]) / 86400.0, 2) if len(unseen_bars) >= 2 else 0.0
    recent_span_days = round((recent_bars[-1][0] - recent_bars[0][0]) / 86400.0, 2) if len(recent_bars) >= 2 else 0.0

    cost_ladder: list[tuple[str, float, float]] = [
        ("base_live_spread", spread_price, 0.0),
        ("stress_spread_050", max(spread_price, 0.50), 0.0),
        ("stress_spread_050_comm5", max(spread_price, 0.50), 5.0),
        ("stress_spread_070_comm7", max(spread_price, 0.70), 7.0),
    ]

    strat_list = strategies if strategies is not None else LOCKED_M5_OOS_STRATEGIES
    out_rows: list[dict[str, Any]] = []

    for st in strat_list:
        base_kw: dict[str, Any] = {
            "spec": spec,
            "symbol": symbol,
            "bar_seconds": 300,
            "htf_indices": htf_indices,
            "entry_mode": st["entry_mode"],
            "htf_filter": st["htf_filter"],
            "exit_mode": st["exit_mode"],
            "stop_usd": float(st["stop_usd"]),
            "max_lot": max_lot,
            "max_legs": 1,
            "leverage": leverage,
            "max_margin_usd": 25.0,
            "min_m5_adx": 18.0,
            "blocked_utc_hours": tuple(st.get("blocked_utc_hours", ())),
            "trail_arm_r": st.get("trail_arm_r"),
            "min_decel_r": st.get("min_decel_r"),
            "fixed_tp_r": st.get("fixed_tp_r"),
        }

        full_res = replay_m1_momentum(
            clean_m5, spread_price=spread_price, commission_per_lot=0.0, include_trades=True, **base_kw,
        )
        full_trades = full_res.pop("trades", [])
        months = summarize_months_utc(full_trades)
        active_months = [m for m in months if m["trades"] >= 3]
        pos_months = sum(1 for m in active_months if m["net_pnl_usd"] > 0)

        recent_res = replay_m1_momentum(
            recent_bars, spread_price=spread_price, commission_per_lot=0.0, **base_kw,
        ) if len(recent_bars) >= 20 else None

        oos_res = replay_m1_momentum(
            unseen_bars, spread_price=spread_price, commission_per_lot=0.0, **base_kw,
        ) if len(unseen_bars) >= 20 else None

        # Cost stress on BOTH True OOS and Full Period
        oos_stress: dict[str, dict[str, Any]] = {}
        full_stress: dict[str, dict[str, Any]] = {}
        for s_name, s_sp, s_comm in cost_ladder:
            f_s = full_res if s_name == "base_live_spread" else replay_m1_momentum(
                clean_m5, spread_price=s_sp, commission_per_lot=s_comm, **base_kw,
            )
            full_stress[s_name] = {
                "spread_price": round(s_sp, 4),
                "commission_per_lot": round(s_comm, 2),
                "trades": f_s["trades_closed"],
                "net_pnl_usd": round(float(f_s["total_marked_pnl_usd"]), 2),
                "pf": f_s["profit_factor"],
                "max_dd_usd": round(float(f_s["max_drawdown_usd"]), 2),
            }
            if oos_res is not None:
                o_s = oos_res if s_name == "base_live_spread" else replay_m1_momentum(
                    unseen_bars, spread_price=s_sp, commission_per_lot=s_comm, **base_kw,
                )
                oos_stress[s_name] = {
                    "spread_price": round(s_sp, 4),
                    "commission_per_lot": round(s_comm, 2),
                    "trades": o_s["trades_closed"],
                    "net_pnl_usd": round(float(o_s["total_marked_pnl_usd"]), 2),
                    "pf": o_s["profit_factor"],
                    "max_dd_usd": round(float(o_s["max_drawdown_usd"]), 2),
                }

        # Strict True Out-of-Sample gates
        has_oos = oos_res is not None and unseen_span_days >= 45.0
        oos_s050 = oos_stress.get("stress_spread_050")
        gates = {
            "oos_history_available_ge_45d": has_oos,
            "oos_sample_ge_30": bool(oos_res and oos_res["trades_closed"] >= 30),
            "oos_positive_and_pf_ge_1_15": bool(
                oos_res and oos_res["total_marked_pnl_usd"] > 0 and (oos_res["profit_factor"] or 0.0) >= 1.15
            ),
            "oos_profitable_without_best_trade": bool(
                oos_res and oos_res["pnl_excl_largest_win_usd"] > 0
            ),
            "oos_survives_spread_050": bool(
                oos_s050 and oos_s050["net_pnl_usd"] > 0 and (oos_s050["pf"] or 0.0) >= 1.05
            ),
            "oos_drawdown_le_65": bool(oos_res and oos_res["max_drawdown_usd"] <= 65.0),
            "full_months_majority_positive": bool(
                len(active_months) >= 3 and (pos_months / len(active_months)) >= 0.65
            ),
        }
        failed = [k for k, ok in gates.items() if not ok]
        out_rows.append({
            "id": st["id"],
            "role": st.get("role", "candidate"),
            "label": st["label"],
            "unseen_older_oos": _plain_trade_summary(oos_res) if oos_res else None,
            "seen_recent_window": _plain_trade_summary(recent_res) if recent_res else None,
            "full_period": _plain_trade_summary(full_res),
            "monthly_breakdown": months,
            "active_months_positive": f"{pos_months}/{len(active_months)}",
            "oos_cost_stress": oos_stress,
            "full_cost_stress": full_stress,
            "oos_gates": gates,
            "failed_oos_gates": failed,
            "oos_pass": len(failed) == 0,
        })

    primary = next((r for r in out_rows if r["id"] == "M5_5_LOCKED"), out_rows[0] if out_rows else None)
    passed_ids = [r["id"] for r in out_rows if r["oos_pass"]]
    if primary and primary["oos_pass"]:
        verdict_status = "TRUE_OOS_PASSED_READY_FOR_LIVE_PAPER"
    elif passed_ids:
        verdict_status = "CONTROL_PASSED_PRIMARY_FAILED_REVIEW"
    else:
        verdict_status = "TRUE_OOS_FAILED_DO_NOT_PROMOTE"

    return {
        "ok": True,
        "paper": True,
        "executable": False,
        "execution_authorized": False,
        "orders_submitted": 0,
        "htf_coverage_pct": coverage,
        "split": {
            "recent_seen_days": recent_seen_days,
            "cutoff_utc": _iso_utc(cutoff_ts),
            "unseen_older_oos_bars": len(unseen_bars),
            "unseen_older_oos_span_days": unseen_span_days,
            "unseen_older_oos_range_utc": (
                [_iso_utc(unseen_bars[0][0]), _iso_utc(unseen_bars[-1][0])] if unseen_bars else []
            ),
            "seen_recent_bars": len(recent_bars),
            "seen_recent_span_days": recent_span_days,
            "seen_recent_range_utc": (
                [_iso_utc(recent_bars[0][0]), _iso_utc(recent_bars[-1][0])] if recent_bars else []
            ),
        },
        "strategies": out_rows,
        "verdict": {
            "status": verdict_status,
            "primary_passed": bool(primary and primary["oos_pass"]),
            "passed_ids": passed_ids,
        },
    }


M5_RISK_BRAKE_VARIANTS: list[dict[str, Any]] = [
    {
        "id": "R0_BASE",
        "label": "Locked M5_5 baseline (stop $12, NO daily/weekly brake)",
        "stop_usd": 12.0,
        "daily_loss_limit_usd": None,
        "weekly_loss_limit_usd": None,
        "loss_streak_pause_count": 0,
        "loss_streak_cooldown_bars": 0,
    },
    {
        "id": "R1_DAILY15",
        "label": "Locked M5_5 (stop $12) + Daily brake -$15",
        "stop_usd": 12.0,
        "daily_loss_limit_usd": 15.0,
        "weekly_loss_limit_usd": None,
        "loss_streak_pause_count": 0,
        "loss_streak_cooldown_bars": 0,
    },
    {
        "id": "R2_DAILY15_WEEKLY25",
        "label": "Locked M5_5 (stop $12) + Daily -$15 + Weekly -$25",
        "stop_usd": 12.0,
        "daily_loss_limit_usd": 15.0,
        "weekly_loss_limit_usd": 25.0,
        "loss_streak_pause_count": 0,
        "loss_streak_cooldown_bars": 0,
    },
    {
        "id": "R3_BRAKE_PLUS_4H_CD",
        "label": "Locked M5_5 (stop $12) + Daily -$15 + Weekly -$25 + 4h pause after 2 losses",
        "stop_usd": 12.0,
        "daily_loss_limit_usd": 15.0,
        "weekly_loss_limit_usd": 25.0,
        "loss_streak_pause_count": 2,
        "loss_streak_cooldown_bars": 48,
    },
    {
        "id": "R4_STOP10_BRAKES",
        "label": "Locked M5_5 (tighter stop $10) + Daily -$15 + Weekly -$25",
        "stop_usd": 10.0,
        "daily_loss_limit_usd": 15.0,
        "weekly_loss_limit_usd": 25.0,
        "loss_streak_pause_count": 0,
        "loss_streak_cooldown_bars": 0,
    },
    {
        "id": "R5_STOP8_BRAKES",
        "label": "Locked M5_5 (tighter stop $8) + Daily -$12 + Weekly -$20",
        "stop_usd": 8.0,
        "daily_loss_limit_usd": 12.0,
        "weekly_loss_limit_usd": 20.0,
        "loss_streak_pause_count": 0,
        "loss_streak_cooldown_bars": 0,
    },
]


def _capital_sizing_guide(max_dd_usd: float, peak_margin_usd: float) -> dict[str, Any]:
    dd = max(0.0, float(max_dd_usd))
    # Recommend starting capital where historical max DD <= 20% of equity (rounded up to nearest $50)
    min_rec = max(150, int(math.ceil((dd * 5.0) / 50.0) * 50))
    return {
        "max_dd_usd": round(dd, 2),
        "peak_margin_usd": round(peak_margin_usd, 2),
        "dd_pct_of_100": round(dd, 1),
        "dd_pct_of_250": round(dd / 2.5, 1),
        "dd_pct_of_500": round(dd / 5.0, 1),
        "dd_pct_of_1000": round(dd / 10.0, 1),
        "min_recommended_account_usd_for_20pct_dd": min_rec,
    }


def evaluate_m5_risk_brakes(
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
    prior_seen_days: int = 180,
    variants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Test capital-protection risk brakes on locked M5_5 across up to 365 days.

    Signal logic (pullback_or_2bar, M15+H1+H4, trail_only 0.8R, no rollover) is
    held 100% FIXED across all variants. Only protective risk brakes vary.
    """
    clean_m5 = clean_ohlcv_bars(m5_bars)
    if len(clean_m5) < 50:
        raise ValueError("insufficient M5 bars for risk-brake evaluation")

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
    cutoff_180 = last_ts - prior_seen_days * 86400
    older_brand_new = [b for b in clean_m5 if int(b[0]) < cutoff_180]
    recent_180 = [b for b in clean_m5 if int(b[0]) >= cutoff_180]
    older_span_days = (
        round((older_brand_new[-1][0] - older_brand_new[0][0]) / 86400.0, 2)
        if len(older_brand_new) >= 2
        else 0.0
    )
    has_brand_new_oos = len(older_brand_new) >= 200 and older_span_days >= 45.0

    cost_ladder: list[tuple[str, float, float]] = [
        ("base_live_spread", spread_price, 0.0),
        ("stress_spread_050", max(spread_price, 0.50), 0.0),
        ("stress_spread_050_comm5", max(spread_price, 0.50), 5.0),
        ("stress_spread_070_comm7", max(spread_price, 0.70), 7.0),
    ]

    var_list = variants if variants is not None else M5_RISK_BRAKE_VARIANTS
    rows: list[dict[str, Any]] = []

    for v in var_list:
        base_kw: dict[str, Any] = {
            "spec": spec,
            "symbol": symbol,
            "bar_seconds": 300,
            "htf_indices": htf_indices,
            "entry_mode": "pullback_or_2bar",
            "htf_filter": "m15_h1_h4",
            "exit_mode": "trail_only",
            "stop_usd": float(v["stop_usd"]),
            "max_lot": max_lot,
            "max_legs": 1,
            "leverage": leverage,
            "max_margin_usd": 25.0,
            "min_m5_adx": 18.0,
            "trail_arm_r": 0.8,
            "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
            "daily_loss_limit_usd": v.get("daily_loss_limit_usd"),
            "weekly_loss_limit_usd": v.get("weekly_loss_limit_usd"),
            "loss_streak_pause_count": int(v.get("loss_streak_pause_count", 0)),
            "loss_streak_cooldown_bars": int(v.get("loss_streak_cooldown_bars", 0)),
        }

        full_res = replay_m1_momentum(
            clean_m5, spread_price=spread_price, commission_per_lot=0.0, include_trades=True, **base_kw,
        )
        full_trades = full_res.pop("trades", [])
        months = summarize_months_utc(full_trades)
        active_months = [m for m in months if m["trades"] >= 3]
        pos_months = sum(1 for m in active_months if m["net_pnl_usd"] > 0)

        prior180_res = replay_m1_momentum(
            recent_180, spread_price=spread_price, commission_per_lot=0.0, **base_kw,
        ) if len(recent_180) >= 20 else None

        brand_new_res = replay_m1_momentum(
            older_brand_new, spread_price=spread_price, commission_per_lot=0.0, **base_kw,
        ) if has_brand_new_oos else None

        stress_full: dict[str, dict[str, Any]] = {}
        for s_name, s_sp, s_comm in cost_ladder:
            s_res = full_res if s_name == "base_live_spread" else replay_m1_momentum(
                clean_m5, spread_price=s_sp, commission_per_lot=s_comm, **base_kw,
            )
            stress_full[s_name] = {
                "spread_price": round(s_sp, 4),
                "commission_per_lot": round(s_comm, 2),
                "trades": s_res["trades_closed"],
                "net_pnl_usd": round(float(s_res["total_marked_pnl_usd"]), 2),
                "pf": s_res["profit_factor"],
                "max_dd_usd": round(float(s_res["max_drawdown_usd"]), 2),
            }

        full_dd = float(full_res["max_drawdown_usd"])
        full_pnl = float(full_res["total_marked_pnl_usd"])
        mar_ratio = round(full_pnl / full_dd, 2) if full_dd > 0 else None
        s050 = stress_full["stress_spread_050"]

        gates: dict[str, bool] = {
            "full_positive_and_pf_ge_1_20": full_pnl > 0 and (full_res["profit_factor"] or 0.0) >= 1.20,
            "full_profitable_without_best_trade": float(full_res["pnl_excl_largest_win_usd"]) > 0,
            "full_drawdown_le_60": full_dd <= 60.0,
            "profit_to_dd_ratio_ge_2_0": bool(mar_ratio is not None and mar_ratio >= 2.0),
            "survives_spread_050": s050["net_pnl_usd"] > 0 and (s050["pf"] or 0.0) >= 1.10,
            "months_majority_positive": bool(
                len(active_months) >= 3 and (pos_months / len(active_months)) >= 0.65
            ),
        }
        if has_brand_new_oos and brand_new_res is not None:
            gates["brand_new_older_oos_positive"] = (
                float(brand_new_res["total_marked_pnl_usd"]) > 0
                and (brand_new_res["profit_factor"] or 0.0) >= 1.10
            )

        failed = [k for k, ok in gates.items() if not ok]
        refs = full_res["refusal_counts"]
        rows.append({
            "id": v["id"],
            "label": v["label"],
            "risk_controls": {
                "stop_usd": v["stop_usd"],
                "daily_loss_limit_usd": v.get("daily_loss_limit_usd"),
                "weekly_loss_limit_usd": v.get("weekly_loss_limit_usd"),
                "loss_streak_pause_count": v.get("loss_streak_pause_count", 0),
                "loss_streak_cooldown_hours": round(
                    int(v.get("loss_streak_cooldown_bars", 0)) * 5 / 60.0, 2
                ),
            },
            "brakes_triggered": {
                "daily_loss_brake": refs.get("daily_loss_brake", 0),
                "weekly_loss_brake": refs.get("weekly_loss_brake", 0),
                "loss_streak_cooldown": refs.get("loss_streak_cooldown", 0),
                "oversized_candle_skipped": refs.get("volume_below_minimum", 0),
            },
            "brand_new_unseen_older_oos": _plain_trade_summary(brand_new_res) if brand_new_res else None,
            "prior_180d_window": _plain_trade_summary(prior180_res) if prior180_res else None,
            "full_period": {
                **_plain_trade_summary(full_res),
                "profit_to_dd_ratio": mar_ratio,
                "max_loss_streak": full_res["max_loss_streak"],
            },
            "monthly_breakdown": months,
            "active_months_positive": f"{pos_months}/{len(active_months)}",
            "full_cost_stress": stress_full,
            "capital_sizing_guide": _capital_sizing_guide(full_dd, float(full_res["peak_margin_used_usd"])),
            "gates": gates,
            "failed_gates": failed,
            "all_gates_passed": len(failed) == 0,
        })

    passed = [r for r in rows if r["all_gates_passed"]]
    passed.sort(
        key=lambda r: (
            r["full_period"]["profit_to_dd_ratio"] or 0.0,
            r["full_cost_stress"]["stress_spread_050"]["pf"] or 0.0,
        ),
        reverse=True,
    )
    ranked_all = sorted(
        rows,
        key=lambda r: (
            1 if r["all_gates_passed"] else 0,
            sum(1 for ok in r["gates"].values() if ok),
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
            "prior_seen_days": prior_seen_days,
            "cutoff_utc": _iso_utc(cutoff_180),
            "has_brand_new_older_oos": has_brand_new_oos,
            "brand_new_older_oos_bars": len(older_brand_new),
            "brand_new_older_oos_span_days": older_span_days,
            "brand_new_older_oos_range_utc": (
                [_iso_utc(older_brand_new[0][0]), _iso_utc(older_brand_new[-1][0])]
                if older_brand_new else []
            ),
            "prior_180d_bars": len(recent_180),
            "prior_180d_range_utc": (
                [_iso_utc(recent_180[0][0]), _iso_utc(recent_180[-1][0])]
                if recent_180 else []
            ),
        },
        "variants": rows,
        "verdict": {
            "any_variant_passed": len(passed) > 0,
            "passed_ids": [r["id"] for r in passed],
            "recommended_for_live_paper": passed[0]["id"] if passed else None,
            "best_diagnostic_id": ranked_all[0]["id"] if ranked_all else None,
            "status": (
                "PASS_DRAWDOWN_CONTROLLED_READY_FOR_LIVE_PAPER"
                if passed
                else "FAIL_DRAWDOWN_OR_OOS_NOT_SOLVED"
            ),
        },
    }


LONDON_NY_BLOCKED_UTC_HOURS: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 21, 22, 23)
LONDON_NY_CORE_BLOCKED_UTC_HOURS: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 17, 18, 19, 20, 21, 22, 23)

M5_REGIME_VARIANTS: list[dict[str, Any]] = [
    {
        "id": "G0_BASE",
        "label": "Locked M5_5 baseline (22h except rollover 21-22 UTC, no H1 ADX gate)",
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
        "min_mid_adx": None,
        "daily_loss_limit_usd": None,
        "weekly_loss_limit_usd": None,
    },
    {
        "id": "G1_LONDON_NY",
        "label": "Active London+NY only (07:00-20:59 UTC; blocks Asia/rollover)",
        "blocked_utc_hours": LONDON_NY_BLOCKED_UTC_HOURS,
        "min_mid_adx": None,
        "daily_loss_limit_usd": None,
        "weekly_loss_limit_usd": None,
    },
    {
        "id": "G2_LONDON_NY_CORE",
        "label": "Core London+NY overlap (07:00-16:59 UTC; peak gold liquidity)",
        "blocked_utc_hours": LONDON_NY_CORE_BLOCKED_UTC_HOURS,
        "min_mid_adx": None,
        "daily_loss_limit_usd": None,
        "weekly_loss_limit_usd": None,
    },
    {
        "id": "G3_H1_ADX20",
        "label": "22h + H1 trend strength gate (closed H1 ADX >= 20)",
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
        "min_mid_adx": 20.0,
        "daily_loss_limit_usd": None,
        "weekly_loss_limit_usd": None,
    },
    {
        "id": "G4_H1_ADX25",
        "label": "22h + strong H1 trend gate (closed H1 ADX >= 25)",
        "blocked_utc_hours": ROLLOVER_BLOCKED_UTC_HOURS,
        "min_mid_adx": 25.0,
        "daily_loss_limit_usd": None,
        "weekly_loss_limit_usd": None,
    },
    {
        "id": "G5_LONDON_NY_H1_ADX20",
        "label": "Active London+NY (07-20 UTC) + H1 ADX >= 20",
        "blocked_utc_hours": LONDON_NY_BLOCKED_UTC_HOURS,
        "min_mid_adx": 20.0,
        "daily_loss_limit_usd": None,
        "weekly_loss_limit_usd": None,
    },
    {
        "id": "G6_LONDON_NY_H1_ADX20_R2",
        "label": "Active London+NY (07-20 UTC) + H1 ADX >= 20 + R2 (-$15d / -$25w)",
        "blocked_utc_hours": LONDON_NY_BLOCKED_UTC_HOURS,
        "min_mid_adx": 20.0,
        "daily_loss_limit_usd": 15.0,
        "weekly_loss_limit_usd": 25.0,
    },
]


def _utc_session_label(hour: int) -> str:
    if 0 <= hour <= 6:
        return "asia_00_06_utc"
    if 7 <= hour <= 11:
        return "london_07_11_utc"
    if 12 <= hour <= 16:
        return "overlap_12_16_utc"
    if 17 <= hour <= 20:
        return "late_ny_17_20_utc"
    return "late_evening_21_23_utc"


def _adx_bucket_label(adx: float | None) -> str:
    if adx is None:
        return "unavailable"
    if adx < 15.0:
        return "adx_lt_15_flat"
    if adx < 20.0:
        return "adx_15_20_weak"
    if adx < 25.0:
        return "adx_20_25_emerging"
    if adx < 35.0:
        return "adx_25_35_trending"
    return "adx_ge_35_strong"


def _bucket_trade_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [t for t in items if float(t["net_pnl_usd"]) > 0]
    losses = [t for t in items if float(t["net_pnl_usd"]) <= 0]
    gp = sum(float(t["net_pnl_usd"]) for t in wins)
    gl = abs(sum(float(t["net_pnl_usd"]) for t in losses))
    net = gp - gl
    pf = round(gp / gl, 3) if gl > 0 else (99.0 if gp > 0 else None)
    return {
        "trades": len(items),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(items), 1) if items else 0.0,
        "net_pnl_usd": round(net, 2),
        "profit_factor": pf,
    }


def _diagnose_baseline_regimes(
    trades: list[dict[str, Any]],
    *,
    h1_index: tuple[list[int], list[HTFContext]],
    cutoff_ts: int,
) -> dict[str, Any]:
    """Anatomy of baseline trades by UTC session and closed H1 ADX across both halves."""
    h1_t, h1_ctx = h1_index
    sessions_order = [
        "asia_00_06_utc",
        "london_07_11_utc",
        "overlap_12_16_utc",
        "late_ny_17_20_utc",
        "late_evening_21_23_utc",
    ]
    adx_order = [
        "adx_lt_15_flat",
        "adx_15_20_weak",
        "adx_20_25_emerging",
        "adx_25_35_trending",
        "adx_ge_35_strong",
        "unavailable",
    ]

    by_sess: dict[str, dict[str, list[dict[str, Any]]]] = {
        k: {"full": [], "half1": [], "half2": []} for k in sessions_order
    }
    by_adx: dict[str, dict[str, list[dict[str, Any]]]] = {
        k: {"full": [], "half1": [], "half2": []} for k in adx_order
    }

    for tr in trades:
        ts = int(tr["opened_at"])
        half = "half1" if ts < cutoff_ts else "half2"
        sess = _utc_session_label((ts // 3600) % 24)
        by_sess[sess]["full"].append(tr)
        by_sess[sess][half].append(tr)

        ctx = lookup_htf(h1_t, h1_ctx, ts)
        ab = _adx_bucket_label(ctx.adx if ctx is not None else None)
        by_adx[ab]["full"].append(tr)
        by_adx[ab][half].append(tr)

    return {
        "by_utc_session": {
            k: {
                "full": _bucket_trade_stats(v["full"]),
                "half1_older": _bucket_trade_stats(v["half1"]),
                "half2_recent": _bucket_trade_stats(v["half2"]),
            }
            for k, v in by_sess.items()
            if v["full"]
        },
        "by_closed_h1_adx": {
            k: {
                "full": _bucket_trade_stats(v["full"]),
                "half1_older": _bucket_trade_stats(v["half1"]),
                "half2_recent": _bucket_trade_stats(v["half2"]),
            }
            for k, v in by_adx.items()
            if v["full"]
        },
    }


def evaluate_m5_regime_filters(
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
    variants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate pre-committed session & H1 ADX market-regime filters on locked M5_5.

    Signal logic (pullback_or_2bar, M15+H1+H4, trail_only 0.8R, $12 stop, 0.01 lot)
    is held 100% FIXED. Enforces dual-half anti-overfitting gates: a filter must
    improve BOTH 6-month halves independently to earn live Paper eligibility.
    """
    clean_m5 = clean_ohlcv_bars(m5_bars)
    if len(clean_m5) < 50:
        raise ValueError("insufficient M5 bars for regime filter evaluation")

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
    half1_span = (
        round((half1_bars[-1][0] - half1_bars[0][0]) / 86400.0, 2) if len(half1_bars) >= 2 else 0.0
    )
    half2_span = (
        round((half2_bars[-1][0] - half2_bars[0][0]) / 86400.0, 2) if len(half2_bars) >= 2 else 0.0
    )
    has_dual_halves = len(half1_bars) >= 200 and half1_span >= 45.0 and len(half2_bars) >= 200

    cost_ladder: list[tuple[str, float, float]] = [
        ("base_live_spread", spread_price, 0.0),
        ("stress_spread_050", max(spread_price, 0.50), 0.0),
        ("stress_spread_050_comm5", max(spread_price, 0.50), 5.0),
        ("stress_spread_070_comm7", max(spread_price, 0.70), 7.0),
    ]

    var_list = variants if variants is not None else M5_REGIME_VARIANTS
    raw_rows: list[dict[str, Any]] = []
    baseline_anatomy: dict[str, Any] = {}
    base_h1_dd: float | None = None
    base_h2_dd: float | None = None

    for v in var_list:
        base_kw: dict[str, Any] = {
            "spec": spec,
            "symbol": symbol,
            "bar_seconds": 300,
            "htf_indices": htf_indices,
            "entry_mode": "pullback_or_2bar",
            "htf_filter": "m15_h1_h4",
            "exit_mode": "trail_only",
            "stop_usd": 12.0,
            "max_lot": max_lot,
            "max_legs": 1,
            "leverage": leverage,
            "max_margin_usd": 25.0,
            "min_m5_adx": 18.0,
            "min_mid_adx": v.get("min_mid_adx"),
            "trail_arm_r": 0.8,
            "blocked_utc_hours": tuple(v.get("blocked_utc_hours", ROLLOVER_BLOCKED_UTC_HOURS)),
            "daily_loss_limit_usd": v.get("daily_loss_limit_usd"),
            "weekly_loss_limit_usd": v.get("weekly_loss_limit_usd"),
        }

        full_res = replay_m1_momentum(
            clean_m5, spread_price=spread_price, commission_per_lot=0.0, include_trades=True, **base_kw,
        )
        full_trades = full_res.pop("trades", [])
        if v["id"] == "G0_BASE":
            baseline_anatomy = _diagnose_baseline_regimes(
                full_trades, h1_index=htf_indices[1], cutoff_ts=cutoff_ts,
            )

        months = summarize_months_utc(full_trades)
        active_months = [m for m in months if m["trades"] >= 3]
        pos_months = sum(1 for m in active_months if m["net_pnl_usd"] > 0)

        h1_res = (
            replay_m1_momentum(half1_bars, spread_price=spread_price, commission_per_lot=0.0, **base_kw)
            if has_dual_halves
            else None
        )
        h2_res = (
            replay_m1_momentum(half2_bars, spread_price=spread_price, commission_per_lot=0.0, **base_kw)
            if len(half2_bars) >= 20
            else None
        )
        if v["id"] == "G0_BASE" and h1_res is not None and h2_res is not None:
            base_h1_dd = float(h1_res["max_drawdown_usd"])
            base_h2_dd = float(h2_res["max_drawdown_usd"])

        stress_full: dict[str, dict[str, Any]] = {}
        for s_name, s_sp, s_comm in cost_ladder:
            s_res = full_res if s_name == "base_live_spread" else replay_m1_momentum(
                clean_m5, spread_price=s_sp, commission_per_lot=s_comm, **base_kw,
            )
            stress_full[s_name] = {
                "spread_price": round(s_sp, 4),
                "commission_per_lot": round(s_comm, 2),
                "trades": s_res["trades_closed"],
                "net_pnl_usd": round(float(s_res["total_marked_pnl_usd"]), 2),
                "pf": s_res["profit_factor"],
                "max_dd_usd": round(float(s_res["max_drawdown_usd"]), 2),
            }

        raw_rows.append({
            "v": v,
            "full_res": full_res,
            "h1_res": h1_res,
            "h2_res": h2_res,
            "months": months,
            "active_months": active_months,
            "pos_months": pos_months,
            "stress_full": stress_full,
        })

    rows: list[dict[str, Any]] = []
    for item in raw_rows:
        v = item["v"]
        full_res = item["full_res"]
        h1_res = item["h1_res"]
        h2_res = item["h2_res"]
        months = item["months"]
        active_months = item["active_months"]
        pos_months = item["pos_months"]
        stress_full = item["stress_full"]

        full_dd = float(full_res["max_drawdown_usd"])
        full_pnl = float(full_res["total_marked_pnl_usd"])
        mar_ratio = round(full_pnl / full_dd, 2) if full_dd > 0 else None
        s050_c5 = stress_full["stress_spread_050_comm5"]

        gates: dict[str, bool] = {
            "full_positive_and_pf_ge_1_30": (
                full_pnl > 0
                and (full_res["profit_factor"] or 0.0) >= 1.30
                and int(full_res["trades_closed"]) >= 80
            ),
            "full_profitable_without_best_trade": float(full_res["pnl_excl_largest_win_usd"]) > 0,
            "full_drawdown_le_65": full_dd <= 65.0,
            "profit_to_dd_ratio_ge_2_2": bool(mar_ratio is not None and mar_ratio >= 2.2),
            "survives_spread_050_comm5": (
                s050_c5["net_pnl_usd"] > 0 and (s050_c5["pf"] or 0.0) >= 1.10
            ),
            "months_majority_positive": bool(
                len(active_months) >= 3 and (pos_months / len(active_months)) >= 0.65
            ),
        }
        if has_dual_halves and h1_res is not None and h2_res is not None:
            h1_pnl = float(h1_res["total_marked_pnl_usd"])
            h2_pnl = float(h2_res["total_marked_pnl_usd"])
            h1_dd = float(h1_res["max_drawdown_usd"])
            h2_dd = float(h2_res["max_drawdown_usd"])
            gates["half1_positive_and_pf_ge_1_15"] = (
                h1_pnl > 0
                and (h1_res["profit_factor"] or 0.0) >= 1.15
                and int(h1_res["trades_closed"]) >= 30
                and float(h1_res["pnl_excl_largest_win_usd"]) >= 0.0
            )
            gates["half2_positive_and_pf_ge_1_15"] = (
                h2_pnl > 0
                and (h2_res["profit_factor"] or 0.0) >= 1.15
                and int(h2_res["trades_closed"]) >= 30
            )
            if base_h1_dd is not None and base_h2_dd is not None:
                gates["both_halves_dd_improved_or_le_60"] = (
                    (h1_dd < base_h1_dd or h1_dd <= 60.0)
                    and (h2_dd < base_h2_dd or h2_dd <= 60.0)
                )

        failed = [k for k, ok in gates.items() if not ok]
        refs = full_res["refusal_counts"]
        rows.append({
            "id": v["id"],
            "label": v["label"],
            "regime_controls": {
                "blocked_utc_hours": list(v.get("blocked_utc_hours", ROLLOVER_BLOCKED_UTC_HOURS)),
                "min_h1_adx": v.get("min_mid_adx"),
                "daily_loss_limit_usd": v.get("daily_loss_limit_usd"),
                "weekly_loss_limit_usd": v.get("weekly_loss_limit_usd"),
            },
            "filters_triggered": {
                "blocked_utc_hour": refs.get("blocked_utc_hour", 0),
                "htf_h1_low_adx_chop": refs.get("htf_h1_low_adx_chop", 0),
                "daily_loss_brake": refs.get("daily_loss_brake", 0),
                "weekly_loss_brake": refs.get("weekly_loss_brake", 0),
            },
            "half1_older_180d": _plain_trade_summary(h1_res) if h1_res is not None else None,
            "half2_recent_180d": _plain_trade_summary(h2_res) if h2_res is not None else None,
            "full_period": {
                **_plain_trade_summary(full_res),
                "profit_to_dd_ratio": mar_ratio,
                "max_loss_streak": full_res["max_loss_streak"],
            },
            "monthly_breakdown": months,
            "active_months_positive": f"{pos_months}/{len(active_months)}",
            "full_cost_stress": stress_full,
            "capital_sizing_guide": _capital_sizing_guide(full_dd, float(full_res["peak_margin_used_usd"])),
            "gates": gates,
            "gates_passed_count": sum(1 for ok in gates.values() if ok),
            "failed_gates": failed,
            "all_gates_passed": len(failed) == 0,
        })

    passed = [r for r in rows if r["all_gates_passed"]]
    passed.sort(
        key=lambda r: (
            r["full_period"]["profit_to_dd_ratio"] or 0.0,
            r["full_cost_stress"]["stress_spread_050_comm5"]["pf"] or 0.0,
        ),
        reverse=True,
    )
    ranked_all = sorted(
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
        "htf_coverage_pct": coverage,
        "split": {
            "split_recent_days": split_recent_days,
            "cutoff_utc": _iso_utc(cutoff_ts),
            "has_dual_halves": has_dual_halves,
            "half1_older_bars": len(half1_bars),
            "half1_older_span_days": half1_span,
            "half1_older_range_utc": (
                [_iso_utc(half1_bars[0][0]), _iso_utc(half1_bars[-1][0])] if half1_bars else []
            ),
            "half2_recent_bars": len(half2_bars),
            "half2_recent_span_days": half2_span,
            "half2_recent_range_utc": (
                [_iso_utc(half2_bars[0][0]), _iso_utc(half2_bars[-1][0])] if half2_bars else []
            ),
        },
        "baseline_regime_anatomy": baseline_anatomy,
        "variants": rows,
        "verdict": {
            "any_variant_passed": len(passed) > 0,
            "passed_ids": [r["id"] for r in passed],
            "recommended_for_live_paper": passed[0]["id"] if passed else None,
            "best_diagnostic_id": ranked_all[0]["id"] if ranked_all else None,
            "status": (
                "PASS_REGIME_SOLVED_READY_FOR_LIVE_PAPER"
                if passed
                else "FAIL_REGIME_NOT_SOLVED_DO_NOT_PROMOTE"
            ),
        },
    }




