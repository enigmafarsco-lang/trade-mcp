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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trade_mcp.all_weather_paper import AllWeatherPaperSuite

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
    """Runs production `AllWeatherPaperSuite(v4)` on ANY symbol via linear price normalization (ORDERS=0).

    Includes a built-in Stale-Quote / Market-Reopen Gap Guard: if `(bid, ask)` remains frozen
    identically for `stale_freeze_sec` (e.g. daily rollover or holiday halt), frozen ticks are
    ignored, and when live quotes resume, rolling velocity buffers reset with a `reopen_warmup_sec`
    entry suppression window so multi-hour reopen gaps never trigger false velocity shocks.
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

    def _init_suite(self) -> None:
        self.suite = AllWeatherPaperSuite(
            initial_capital_usd=self.initial_capital_usd,
            engine_version="v4",
            risk_multiplier=1.0,
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

    def _has_open_position(self) -> bool:
        if self.suite.fast_shock_engine is not None and self.suite.fast_shock_engine.basket is not None:
            return True
        if self.suite.shock_engine is not None and self.suite.shock_engine.basket is not None:
            return True
        if self.suite.macro_engine.basket is not None:
            return True
        return self.suite.calm_engine.basket is not None

    def on_tick(self, ts: float, bid: float, ask: float, m1_context: dict[str, Any]) -> list[dict[str, Any]]:
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return []

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
                            self.close_open(ts, bid, ask, reason="market_freeze_halt")
                        self._is_frozen = True
                    self.frozen_ticks_skipped += 1
                    return []
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

        # During post-reopen warmup, let rolling buffers absorb the new price level without firing into reopen gap
        if ts < self._reopen_block_until and not self._has_open_position():
            # Feed a neutral/blocked m1 context and keep suite cooldowns armed until warmup finishes
            for eng in (self.suite.fast_shock_engine, self.suite.shock_engine, self.suite.macro_engine, self.suite.calm_engine):
                if eng is not None and hasattr(eng, "cooldown_until"):
                    eng.cooldown_until = max(float(eng.cooldown_until), self._reopen_block_until)

        s = self.price_scale
        nbid = bid / s
        nask = ask / s
        mid_norm = 0.5 * (nbid + nask)

        # Single-tick teleport / bad-tick flash-jump guard across all engines
        if (
            self.max_single_tick_jump_norm > 0.0
            and self._prev_mid_norm > 0.0
            and abs(mid_norm - self._prev_mid_norm) >= self.max_single_tick_jump_norm
        ):
            self.teleport_blocks += 1
            self._teleport_block_until = max(self._teleport_block_until, ts + self.teleport_cooldown_sec)
            for eng in (self.suite.fast_shock_engine, self.suite.shock_engine, self.suite.macro_engine, self.suite.calm_engine):
                if eng is None:
                    continue
                if hasattr(eng, "cooldown_until"):
                    eng.cooldown_until = max(float(eng.cooldown_until), self._teleport_block_until)
                if getattr(eng, "pending", None) is not None and eng.pending.get("kind") == "entry":
                    eng.pending = None
                if getattr(eng, "pending_order", None) is not None and eng.pending_order.get("kind") == "entry":
                    eng.pending_order = None
        self._prev_mid_norm = mid_norm

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
        return evs

    def close_open(self, ts: float, bid: float, ask: float, reason: str = "eof_seg") -> list[dict[str, Any]]:
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return []
        s = self.price_scale
        prev = len(self.suite.trades_closed)
        evs = self.suite.close_all_open(ts=ts, bid=bid / s, ask=ask / s, reason=reason)
        if len(self.suite.trades_closed) > prev:
            for t in self.suite.trades_closed[prev:]:
                self.waves_closed.append(self._denormalize_wave(t))
        return evs
