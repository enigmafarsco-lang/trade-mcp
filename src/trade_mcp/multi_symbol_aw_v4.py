"""Symbol-Adaptive Multi-Asset All-Weather V4 Wrapper & Alpari Economics (ORDERS=0).

Applies our proven, bug-fixed `AllWeatherPaperSuite(engine_version="v4")` (`COMBO_AW_V4_24H`)
to ANY broker symbol (Indices, Silver, Oil, Crypto, FX, Gold) via exact linear price-space
normalization (`norm_px = real_px / price_scale`) and risk-parity lot scaling:

  - Every Gold-validated price threshold ($4.80/30s impulse, $2.80 SL, $2.60 50%-partial bank,
    $0.60 runner SL cushion, $0.65 spread ceiling, $5/$8 15m/30m HTF guard, $9/$12 Z-Score
    breakout guard, etc.) scales cleanly by ONE physical parameter `price_scale = S`.
  - Real broker spreads (`ask - bid`) are preserved tick-by-tick (`(ask - bid) / S`).
  - Real USD PnL is exact to the penny via `effective_multiplier = raw_multiplier * S * lot_scale`,
    and all closed waves are translated back into native symbol prices & real broker lot sizes.

Strictly read-only / paper-only (`paper_only=True`, `orders_submitted=0`).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trade_mcp.all_weather_paper import AllWeatherPaperSuite
from trade_mcp.m1_momentum import atr_series, clean_ohlcv_bars, ema_series

GOLD_REF_SPREAD = 0.40
GOLD_REF_VSNAP_SL = 2.80
GOLD_REF_VSNAP_LOT = 0.02
GOLD_REF_SL_RISK_USD = GOLD_REF_VSNAP_SL * GOLD_REF_VSNAP_LOT * 100.0  # $5.60 per 1x VSNAP6 trade


@dataclass(frozen=True)
class AlpariSymbolSpec:
    """Broker contract economics for one Alpari MT5 symbol."""

    symbol: str
    asset_class: str
    contract_size: float
    currency_profit: str = "USD"
    digits: int = 2
    volume_min: float = 0.01
    volume_step: float = 0.01
    active_hours_srv: tuple[int, ...] | None = None  # None = 24h

    def usd_per_price_unit(self, mid_price: float, *, eur_usd: float = 1.08) -> float:
        """Exact USD value of a +1.00 price move at 1.00 lot."""
        cp = self.currency_profit.upper()
        if cp == "USD":
            return float(self.contract_size)
        if cp == "EUR":
            return float(self.contract_size) * float(eur_usd)
        if cp == "JPY":
            return float(self.contract_size) / max(float(mid_price), 1.0)
        if cp == "GBP":
            return float(self.contract_size) * 1.27
        if cp == "CHF":
            return float(self.contract_size) * 1.12
        return float(self.contract_size)


# Verified Alpari MT5 defaults (overridden automatically by reports/paper_live/alpari_wave_symbols_scan.json when present)
DEFAULT_ALPARI_SPECS: dict[str, AlpariSymbolSpec] = {
    "XAUUSD_i": AlpariSymbolSpec("XAUUSD_i", "METAL", 100.0, "USD", 2, 0.01, 0.01, None),
    "XAGUSD_i": AlpariSymbolSpec("XAGUSD_i", "METAL", 5000.0, "USD", 3, 0.01, 0.01, None),
    "NAS100_i": AlpariSymbolSpec("NAS100_i", "INDEX", 1.0, "USD", 1, 0.01, 0.01, tuple(range(10, 23))),
    "US30_i": AlpariSymbolSpec("US30_i", "INDEX", 1.0, "USD", 1, 0.01, 0.01, tuple(range(10, 23))),
    "US500_i": AlpariSymbolSpec("US500_i", "INDEX", 1.0, "USD", 1, 0.01, 0.01, tuple(range(10, 23))),
    "GER40_i": AlpariSymbolSpec("GER40_i", "INDEX", 1.0, "EUR", 1, 0.01, 0.01, tuple(range(9, 20))),
    "WTI_i": AlpariSymbolSpec("WTI_i", "ENERGY", 1000.0, "USD", 2, 0.01, 0.01, tuple(range(9, 22))),
    "BRN_i": AlpariSymbolSpec("BRN_i", "ENERGY", 1000.0, "USD", 2, 0.01, 0.01, tuple(range(9, 22))),
    "BITCOIN_i": AlpariSymbolSpec("BITCOIN_i", "CRYPTO", 1.0, "USD", 2, 0.01, 0.01, None),
    "ETHEREUM_i": AlpariSymbolSpec("ETHEREUM_i", "CRYPTO", 5.0, "USD", 2, 0.01, 0.01, None),
    "USDJPY_i": AlpariSymbolSpec("USDJPY_i", "FX", 100000.0, "JPY", 3, 0.01, 0.01, tuple(range(2, 20))),
    "GBPUSD_i": AlpariSymbolSpec("GBPUSD_i", "FX", 100000.0, "USD", 5, 0.01, 0.01, tuple(range(9, 20))),
}


def load_alpari_specs(scan_json: Path | None = None) -> dict[str, AlpariSymbolSpec]:
    """Load Alpari symbol specs, merging live scan JSON on top of verified defaults."""
    specs = dict(DEFAULT_ALPARI_SPECS)
    if scan_json is None or not scan_json.exists():
        return specs
    try:
        payload = json.loads(scan_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return specs
    for row in payload.get("candidates", []):
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").strip()
        if not sym:
            continue
        base = specs.get(sym)
        csize = float(row.get("contract_size") or (base.contract_size if base else 1.0))
        cprof = str(row.get("currency_profit") or (base.currency_profit if base else "USD")).upper()
        digits = int(row.get("digits") if row.get("digits") is not None else (base.digits if base else 2))
        role = str(row.get("role") or "").upper()
        if base is not None:
            aclass = base.asset_class
            hrs = base.active_hours_srv
            vmin = base.volume_min
            vstep = base.volume_step
        else:
            if "INDEX" in role:
                aclass, hrs = "INDEX", tuple(range(10, 23))
            elif "ENERGY" in role:
                aclass, hrs = "ENERGY", tuple(range(9, 22))
            elif "CRYPTO" in role:
                aclass, hrs = "CRYPTO", None
            elif "METAL" in role:
                aclass, hrs = "METAL", None
            else:
                aclass, hrs = "FX", tuple(range(9, 20))
            vmin, vstep = 0.01, 0.01
        specs[sym] = AlpariSymbolSpec(
            symbol=sym,
            asset_class=aclass,
            contract_size=csize,
            currency_profit=cprof,
            digits=digits,
            volume_min=vmin,
            volume_step=vstep,
            active_hours_srv=hrs,
        )
    return specs


def compute_risk_parity_lot_scale(
    *,
    price_scale: float,
    usd_per_price_unit: float,
    target_vsnap_sl_usd: float = GOLD_REF_SL_RISK_USD,
    volume_min: float = 0.01,
    volume_step: float = 0.01,
) -> tuple[float, float, float, bool]:
    """Return (lot_scale, real_vsnap_lot, actual_sl_usd, min_lot_clamped).

    `lot_scale` maps internal Gold-space lots (`0.01` macro/zscore, `0.02` vsnap6) cleanly to valid
    Alpari broker lots (`>= volume_min`, multiple of `volume_step`, and 50/50 splittable for VSNAP6).
    """
    sl_price = GOLD_REF_VSNAP_SL * float(price_scale)
    sl_usd_per_lot = max(sl_price * float(usd_per_price_unit), 1e-9)
    raw_vsnap_lot = float(target_vsnap_sl_usd) / sl_usd_per_lot
    step = max(0.01, float(volume_step))
    min_sub_lot = max(step, float(volume_min))  # minimum valid lot for 0.01-base engines (MACRO/ZSCORE/SHOCK10)
    min_vsnap_lot = round(2.0 * min_sub_lot, 2)  # minimum 50/50 splittable lot for VSNAP6

    if raw_vsnap_lot + 1e-9 >= min_vsnap_lot:
        # Quantize `lot_scale` to integer multiples of (step / 0.01) so both 0.01*ls and 0.02*ls are exact broker steps
        raw_sub_lot = raw_vsnap_lot * 0.5
        q_sub_lot = max(min_sub_lot, round(raw_sub_lot / step) * step)
        q_vsnap_lot = round(2.0 * q_sub_lot, 2)
        clamped = False
    else:
        q_vsnap_lot = min_vsnap_lot
        clamped = True

    lot_scale = round(q_vsnap_lot / GOLD_REF_VSNAP_LOT, 4)
    actual_sl_usd = round(q_vsnap_lot * sl_usd_per_lot, 2)
    return lot_scale, q_vsnap_lot, actual_sl_usd, clamped


def analyze_scale_free_m1_context(
    m1_bars: list[list[float]],
    *,
    server_now_ts: float | None = None,
    sr_lookback: int = 15,
) -> dict[str, Any]:
    """Scale-free causal M1 trend & S/R analyzer working identically on Gold, Indices, Silver, and FX.

    Unlike Gold's `analyze_m1_sr_context` (which has a hardcoded `0.10` ATR floor & 2-decimal rounding
    that distort low-priced instruments like Silver `XAGUSD_i` ~44.000), this uses relative price
    epsilon (`max(close * 1e-6, 1e-7)`) and 6-decimal precision.
    """
    clean = clean_ohlcv_bars(m1_bars)
    if server_now_ts is not None:
        closed = [b for b in clean if (float(b[0]) + 60.0) <= (server_now_ts + 1.0)]
    else:
        closed = list(clean)

    n = len(closed)
    if n < 4:
        return {
            "bias": 0,
            "label": "INSUFFICIENT_M1",
            "support": None,
            "resistance": None,
            "atr_m1": 0.0,
            "pullback_resume": False,
            "last_close": 0.0,
        }

    # Causal short-history warmup for minutes 4..24 (mirrors Gold's _mini_m1_context without Gold constants)
    if n < max(25, sr_lookback + 2):
        closes = [float(b[4]) for b in closed]
        hi = max(float(b[2]) for b in closed[:-1])
        lo = min(float(b[3]) for b in closed[:-1])
        atr_est = sum(max(float(b[2]) - float(b[3]), 1e-7) for b in closed[-min(14, n) :]) / min(14, n)
        c = closes[-1]
        c0 = closes[0]
        slope = c - c0
        e_fast = sum(closes[-min(4, n) :]) / min(4, n)
        e_slow = sum(closes[-min(12, n) :]) / min(12, n)
        bias = 0
        label = "CHOP_NEUTRAL"
        if (e_fast > e_slow and slope > 0.35 * atr_est) or c > hi:
            bias = 1
            label = "BULL_RES_BREAK" if c > hi else "BULL_TREND"
        elif (e_fast < e_slow and slope < -0.35 * atr_est) or c < lo:
            bias = -1
            label = "BEAR_SUP_BREAK" if c < lo else "BEAR_TREND"
        return {
            "bias": bias,
            "label": label,
            "support": round(lo, 6),
            "resistance": round(hi, 6),
            "atr_m1": round(atr_est, 6),
            "pullback_resume": False,
            "last_close": round(c, 6),
        }

    window = closed[-60:]
    closes = [float(b[4]) for b in window]
    ema9 = [float(v) if v is not None else closes[i] for i, v in enumerate(ema_series(closes, 9))]
    ema21 = [float(v) if v is not None else closes[i] for i, v in enumerate(ema_series(closes, 21))]
    atr_vals = [float(v) if v is not None else 0.0 for v in atr_series(window, period=14)]

    last = window[-1]
    prev_sr = window[-(sr_lookback + 1) : -1]
    sup = min(float(b[3]) for b in prev_sr)
    res = max(float(b[2]) for b in prev_sr)
    c = float(last[4])
    o = float(last[1])
    h = float(last[2])
    low = float(last[3])
    atr_now = max(atr_vals[-1], max(abs(c) * 1e-6, 1e-7))

    recent5 = window[-6:-1]
    had_green_pb = any(float(b[4]) > float(b[1]) for b in recent5)
    had_red_pb = any(float(b[4]) < float(b[1]) for b in recent5)
    slope10 = c - float(window[-11][4])

    bias = 0
    label = "CHOP_NEUTRAL"
    pb_resume = False

    if (ema9[-1] < ema21[-1] and c <= ema21[-1] and slope10 <= 0) or (c < sup and c < o):
        lower_wick = min(o, c) - low
        rng = max(h - low, 1e-9)
        near_sup_bounce = (c <= sup + 0.15 * atr_now) and (lower_wick / rng >= 0.55) and (c > o)
        if near_sup_bounce:
            bias = 0
            label = "SUPPORT_REJECTION"
        else:
            bias = -1
            if c < sup:
                label = "BEAR_SUP_BREAK"
            elif had_green_pb and c < o:
                label = "BEAR_PB_RESUME"
                pb_resume = True
            else:
                label = "BEAR_TREND"
    elif (ema9[-1] > ema21[-1] and c >= ema21[-1] and slope10 >= 0) or (c > res and c > o):
        upper_wick = h - max(o, c)
        rng = max(h - low, 1e-9)
        near_res_reject = (c >= res - 0.15 * atr_now) and (upper_wick / rng >= 0.55) and (c < o)
        if near_res_reject:
            bias = 0
            label = "RESISTANCE_REJECTION"
        else:
            bias = 1
            if c > res:
                label = "BULL_RES_BREAK"
            elif had_red_pb and c > o:
                label = "BULL_PB_RESUME"
                pb_resume = True
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


def _scale_m1_context(m1_ctx: dict[str, Any], price_scale: float) -> dict[str, Any]:
    if not m1_ctx or abs(price_scale - 1.0) < 1e-12:
        return m1_ctx
    out = dict(m1_ctx)
    for k in ("support", "resistance", "atr_m1", "last_close"):
        val = out.get(k)
        if isinstance(val, (int, float)) and math.isfinite(val):
            out[k] = round(float(val) / price_scale, 6)
    return out


@dataclass
class NormalizedAllWeatherV4:
    """Runs production `AllWeatherPaperSuite(v4|v5)` on ANY symbol via linear price normalization (ORDERS=0).

    Includes built-in:
      - Stale-Quote / Market-Reopen Gap Guard (`stale_freeze_sec`, `reopen_warmup_sec`),
      - Cross-Engine Single-Tick Anti-Teleport Guard (`max_single_tick_jump_norm`, `teleport_cooldown_sec`),
      - Normalized M1 bar seeding (`seed_m1_bars`) so 60m/120m HTF & ZSCORE_MR are warmed up from tick #1.
    """

    symbol: str
    price_scale: float
    usd_per_price_unit: float
    lot_scale: float = 1.0
    digits: int = 2
    initial_capital_usd: float = 1000.0
    active_hours_srv: tuple[int, ...] | None = None
    allow_partial_split: bool = True
    stale_freeze_sec: float = 90.0
    reopen_warmup_sec: float = 120.0
    max_single_tick_jump_norm: float = 0.0
    teleport_cooldown_sec: float = 90.0
    engine_version: str = "v4"
    risk_multiplier: float = 1.0
    suite_mutator: Callable[[AllWeatherPaperSuite], None] | None = None

    suite: AllWeatherPaperSuite = field(init=False)
    waves_closed: list[dict[str, Any]] = field(default_factory=list)
    _last_quote: tuple[float, float] | None = field(default=None, init=False, repr=False)
    _last_change_ts: float = field(default=0.0, init=False, repr=False)
    _prev_mid_norm: float = field(default=0.0, init=False, repr=False)
    _is_frozen: bool = field(default=False, init=False, repr=False)
    _reopen_block_until: float = field(default=0.0, init=False, repr=False)
    _teleport_block_until: float = field(default=0.0, init=False, repr=False)
    frozen_ticks_skipped: int = field(default=0, init=False)
    reopen_resets: int = field(default=0, init=False)
    teleport_blocks: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.price_scale <= 0.0 or not math.isfinite(self.price_scale):
            raise ValueError(f"price_scale must be > 0, got {self.price_scale!r}")
        if self.usd_per_price_unit <= 0.0 or not math.isfinite(self.usd_per_price_unit):
            raise ValueError(f"usd_per_price_unit must be > 0, got {self.usd_per_price_unit!r}")
        self._init_suite()

    def _apply_engine_overrides(self) -> None:
        eff_mult = float(self.usd_per_price_unit) * float(self.price_scale) * float(self.lot_scale)
        self.suite.set_multiplier(eff_mult)
        if self.suite.fast_shock_engine is not None:
            if not self.allow_partial_split:
                self.suite.fast_shock_engine.cfg.partial_arm_price = 0.0
            if self.active_hours_srv is not None:
                self.suite.fast_shock_engine.cfg.active_server_hours = self.active_hours_srv
            self.suite.fast_shock_engine.cfg.max_single_tick_jump_price = float(self.max_single_tick_jump_norm)
            self.suite.fast_shock_engine.cfg.teleport_cooldown_sec = float(self.teleport_cooldown_sec)
        if self.active_hours_srv is not None:
            self.suite.calm_engine.cfg.active_server_hours = self.active_hours_srv
            self.suite.macro_engine.cfg.active_server_hours = self.active_hours_srv
        if self.suite_mutator is not None:
            self.suite_mutator(self.suite)

    def _init_suite(self) -> None:
        # Wrapper enforces stale-quote freeze & cross-engine teleport guard in normalized space,
        # so disable duplicate inner suite-level freeze/teleport wrappers to prevent double-counting.
        self.suite = AllWeatherPaperSuite(
            initial_capital_usd=self.initial_capital_usd,
            engine_version=self.engine_version,
            risk_multiplier=float(self.risk_multiplier),
            stale_freeze_sec=0.0,
            max_single_tick_jump_price=0.0,
        )
        # Exact USD PnL isomorphism: diff_norm * vol_norm * (usd_per_unit * S * lot_scale) == diff_real * vol_real * usd_per_unit
        self._apply_engine_overrides()

    def reset_segment(self) -> None:
        """Reset per-engine rolling buffers across >120s gaps while preserving closed trade history."""
        self.suite.reset_file_state()
        self._apply_engine_overrides()
        self._last_quote = None
        self._last_change_ts = 0.0
        self._prev_mid_norm = 0.0
        self._is_frozen = False

    def seed_m1_bars(
        self,
        m1_bars: list[list[float]],
        *,
        server_now_ts: float,
        spread: float = 0.0,
    ) -> int:
        """Warm up ZSCORE_MR, VSNAP6, and MACRO_WAVE HTF context using normalized closed MT5 M1 bars."""
        s = self.price_scale
        norm_bars: list[list[float]] = []
        for row in m1_bars:
            if len(row) < 5:
                continue
            norm_bars.append([
                float(row[0]),
                float(row[1]) / s,
                float(row[2]) / s,
                float(row[3]) / s,
                float(row[4]) / s,
                float(row[5]) if len(row) > 5 else 1.0,
            ])
        return self.suite.seed_m1_bars(
            norm_bars,
            server_now_ts=server_now_ts,
            spread=max(0.0, float(spread)) / s,
        )

    def _denormalize_wave(self, raw_wave: dict[str, Any]) -> dict[str, Any]:
        s = self.price_scale
        ls = self.lot_scale
        d = max(1, min(6, int(self.digits)))
        w = dict(raw_wave)
        for pk in ("avg_entry", "leg1_entry", "exit_price", "stop_final"):
            if pk in w and isinstance(w[pk], (int, float)):
                w[pk] = round(float(w[pk]) * s, d)
        if "total_volume" in w and isinstance(w["total_volume"], (int, float)):
            w["total_volume"] = round(float(w["total_volume"]) * ls, 2)
        w["symbol"] = self.symbol
        lbl = str(w.get("engine_label", w.get("engine", "OTHER")))
        w["_engine"] = {"FAST_SHOCK6": "VSNAP6", "NEWS_SHOCK": "SHOCK10"}.get(lbl, lbl)
        return w

    def _denormalize_event(self, ev: dict[str, Any]) -> dict[str, Any]:
        s = self.price_scale
        ls = self.lot_scale
        d = max(1, min(6, int(self.digits)))
        out = dict(ev)
        out["symbol"] = self.symbol
        for pk in ("entry", "add_price", "stop", "price"):
            if pk in out and isinstance(out[pk], (int, float)):
                out[pk] = round(float(out[pk]) * s, d)
        for lk in ("leg_lot", "total_volume", "closed_volume", "remaining_volume"):
            if lk in out and isinstance(out[lk], (int, float)):
                out[lk] = round(float(out[lk]) * ls, 2)
        if out.get("type") == "CLOSE_ALL" and isinstance(out.get("wave"), dict):
            out["wave"] = self._denormalize_wave(out["wave"])
        return out

    def open_positions(self, bid: float, ask: float) -> list[dict[str, Any]]:
        s = self.price_scale
        ls = self.lot_scale
        d = max(1, min(6, int(self.digits)))
        rows = self.suite.open_positions(bid / s, ask / s)
        out: list[dict[str, Any]] = []
        for r in rows:
            rc = dict(r)
            rc["symbol"] = self.symbol
            rc["avg_entry"] = round(float(rc["avg_entry"]) * s, d)
            rc["stop"] = round(float(rc["stop"]) * s, d)
            rc["volume"] = round(float(rc["volume"]) * ls, 2)
            out.append(rc)
        return out

    def floating_pnl_usd(self, bid: float, ask: float) -> float:
        if bid <= 0.0 or ask <= 0.0:
            return 0.0
        s = self.price_scale
        return self.suite.floating_pnl_usd(bid / s, ask / s)

    def account_summary(self, bid: float = 0.0, ask: float = 0.0) -> dict[str, Any]:
        s = self.price_scale
        acct = self.suite.account_summary(bid / s if bid > 0 else 0.0, ask / s if ask > 0 else 0.0)
        acct["symbol"] = self.symbol
        acct["price_scale"] = round(self.price_scale, 4)
        acct["lot_scale"] = round(self.lot_scale, 4)
        acct["frozen_ticks_skipped"] = self.frozen_ticks_skipped
        acct["reopen_resets"] = self.reopen_resets
        acct["teleport_blocks"] = self.teleport_blocks
        return acct

    def _has_open_position(self) -> bool:
        if self.suite.fast_shock_engine is not None and self.suite.fast_shock_engine.basket is not None:
            return True
        if self.suite.shock_engine is not None and self.suite.shock_engine.basket is not None:
            return True
        if self.suite.macro_engine.basket is not None:
            return True
        return self.suite.calm_engine.basket is not None

    def _suppress_entries_until(self, block_until: float) -> None:
        for eng in (self.suite.fast_shock_engine, self.suite.shock_engine, self.suite.macro_engine, self.suite.calm_engine):
            if eng is None:
                continue
            if hasattr(eng, "cooldown_until"):
                eng.cooldown_until = max(float(eng.cooldown_until), block_until)
            if eng.basket is None:
                pend = getattr(eng, "pending", None)
                if isinstance(pend, dict) and pend.get("kind") in ("entry", "open"):
                    eng.pending = None
                pend_o = getattr(eng, "pending_order", None)
                if isinstance(pend_o, dict) and pend_o.get("kind") in ("entry", "open"):
                    eng.pending_order = None

    def on_tick(self, ts: float, bid: float, ask: float, m1_context: dict[str, Any]) -> list[dict[str, Any]]:
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return []
        close_evs: list[dict[str, Any]] = []

        # Stale-quote / market-closed freeze detector
        if self.stale_freeze_sec > 0.0:
            q_now = (bid, ask)
            if self._last_quote is None:
                self._last_quote = q_now
                self._last_change_ts = ts
            elif q_now == self._last_quote:
                if (ts - self._last_change_ts) >= self.stale_freeze_sec:
                    if not self._is_frozen:
                        # Close any open position cleanly at last pre-halt quote before freezing
                        if self._has_open_position():
                            close_evs.extend(self.close_open(ts, bid, ask, reason="market_freeze_halt"))
                        self._is_frozen = True
                    self.frozen_ticks_skipped += 1
                    return close_evs
            else:
                was_frozen = self._is_frozen
                self._last_quote = q_now
                self._last_change_ts = ts
                if was_frozen:
                    self._is_frozen = False
                    self.reset_segment()
                    self._last_quote = q_now
                    self._last_change_ts = ts
                    self.reopen_resets += 1
                    if self.reopen_warmup_sec > 0.0:
                        self._reopen_block_until = ts + self.reopen_warmup_sec

        s = self.price_scale
        nbid = bid / s
        nask = ask / s
        mid_norm = 0.5 * (nbid + nask)

        # Single-tick teleport / bad-tick flash-jump guard across all 4 engines
        if (
            self.max_single_tick_jump_norm > 0.0
            and self._prev_mid_norm > 0.0
            and abs(mid_norm - self._prev_mid_norm) >= self.max_single_tick_jump_norm
        ):
            self.teleport_blocks += 1
            self._teleport_block_until = max(self._teleport_block_until, ts + self.teleport_cooldown_sec)
            self._prev_mid_norm = mid_norm
            self._suppress_entries_until(self._teleport_block_until)
            if not self._has_open_position():
                return close_evs
        else:
            self._prev_mid_norm = mid_norm

        block_until = max(self._reopen_block_until, self._teleport_block_until)
        if block_until > 0.0 and ts < block_until:
            self._suppress_entries_until(block_until)

        prev = len(self.suite.trades_closed)
        evs = self.suite.on_tick(
            ts=ts,
            bid=nbid,
            ask=nask,
            m1_context=_scale_m1_context(m1_context, s),
        )
        if len(self.suite.trades_closed) > prev:
            for t in self.suite.trades_closed[prev:]:
                self.waves_closed.append(self._denormalize_wave(t))
        return close_evs + [self._denormalize_event(e) for e in evs]

    def close_open(self, ts: float, bid: float, ask: float, reason: str = "eof_seg") -> list[dict[str, Any]]:
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return []
        s = self.price_scale
        prev = len(self.suite.trades_closed)
        evs = self.suite.close_all_open(ts=ts, bid=bid / s, ask=ask / s, reason=reason)
        if len(self.suite.trades_closed) > prev:
            for t in self.suite.trades_closed[prev:]:
                self.waves_closed.append(self._denormalize_wave(t))
        return [self._denormalize_event(e) for e in evs]


@dataclass
class NormalizedAllWeatherV5(NormalizedAllWeatherV4):
    """Production bug-fixed Multi-Symbol COMBO_AW_V5_24H wrapper (60m/120m HTF + 4.5x teleport + 300s freeze guard)."""

    stale_freeze_sec: float = 300.0
    reopen_warmup_sec: float = 120.0
    max_single_tick_jump_norm: float = 4.50
    teleport_cooldown_sec: float = 90.0
    engine_version: str = "v5"
