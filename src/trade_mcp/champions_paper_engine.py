"""Live 2-Engine Champions Paper Forward-Tester (strictly virtual, ORDERS=0).

Runs our two empirically validated Alpari XAUUSD_i champions simultaneously on live
closed MT5 candles and live Bid/Ask ticks with ZERO orders ever sent to MT5:
  - Engine 1: M5_MOMENTUM_G5_ADX20_35 (pullback_or_2bar, M15/H1/H4 HTF alignment,
              M15 ADX >= 18, H1 ADX in [20, 35], London+NY hours, fast invalidation cut,
              and monotonic trail_only armed at +0.8R)
  - Engine 2: H1_DONCHIAN50_LONDON (50-bar H1 Donchian breakout during London 08-16 UTC,
              H1 ATR(14) >= $5.0, 2.5x ATR stop capped at $25/0.01 lot, opposite 20-bar
              Donchian exit, max 100 H1 bars, and 2.5x ATR Chandelier trail at >= 1.5R)

Supports dynamic risk-based lot sizing in [0.01 .. 0.10] with automatic 12% DD governor
(matching our validated portfolio_sizing_lab.py math) or fixed lot sizing, plus atomic
crash-safe JSON state persistence and append-only JSONL trade journaling.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .accel_pyramid_lab import (
    AccelPyramidConfig,
    _detect_accel_signal,
    _rolling_mean,
    default_m1_accel_configs,
    default_m5_accel_configs,
)
from .daytrade_paper import resolve_pricing_basis
from .m1_momentum import (
    LONDON_NY_BLOCKED_UTC_HOURS,
    atr_series,
    build_htf_index,
    clean_ohlcv_bars,
    detect_m1_impulse,
    ema_series,
    htf_allows_signal,
    lookup_htf,
    resample_bars_to_tf,
)


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _floor_step(val: float, step: float = 0.01) -> float:
    if val <= 0:
        return 0.0
    return round(math.floor((val + 1e-12) / step) * step, 2)


def _drop_forming_bar(bars: list[list[float]], *, period_seconds: int, server_now_ts: float) -> list[list[float]]:
    """Keep strictly closed bars (`bar_open + period_seconds <= server_now_ts`)."""
    clean = clean_ohlcv_bars(bars)
    if not clean:
        return []
    # Allow 1s clock tolerance so a just-closed bar isn't needlessly delayed
    return [b for b in clean if (float(b[0]) + period_seconds) <= (server_now_ts + 1.0)]


class ChampionsPaperEngine:
    """Stateful virtual portfolio: 2 Champions (M5 + H1) + optional 2 Acceleration Winner-Pyramid engines."""

    ENGINE_M5 = "M5_MOMENTUM_G5_ADX20_35"
    ENGINE_H1 = "H1_DONCHIAN50_LONDON"
    ENGINE_M1_ACCEL = "M1_PRE15_PB5_PYRAMID3"
    ENGINE_M5_ACCEL = "M5_VOL_PB_BURST_PYR3"

    def __init__(
        self,
        *,
        symbol: str = "XAUUSD_i",
        spec: dict[str, Any],
        initial_capital_usd: float = 1000.0,
        risk_pct: float = 1.5,
        fixed_lot: float | None = None,
        min_lot: float = 0.01,
        max_lot: float = 0.10,
        dd_governor_pct: float = 12.0,
        leverage: float = 500.0,
        min_h1_atr: float = 5.0,
        m5_base_stop_usd_001: float = 12.0,
        h1_base_stop_usd_001: float = 25.0,
        enable_accel_engines: bool = True,
        accel_leg_lot: float = 0.01,
    ) -> None:
        self.symbol = symbol
        self.spec = dict(spec)
        basis_name, mult_val = resolve_pricing_basis(self.spec, "auto")
        self.pricing_basis = basis_name
        self.multiplier = float(mult_val)
        self.contract_size = float(self.spec.get("trade_contract_size") or 100.0)
        self.tick_size = float(self.spec.get("trade_tick_size") or self.spec.get("point") or 0.01)

        self.initial_capital_usd = float(initial_capital_usd)
        self.risk_pct = float(risk_pct)
        self.fixed_lot = float(fixed_lot) if fixed_lot is not None else None
        self.min_lot = max(0.01, float(min_lot))
        self.max_lot = max(self.min_lot, min(0.10, float(max_lot)))
        self.dd_governor_pct = float(dd_governor_pct)
        self.leverage = float(leverage)
        self.min_h1_atr = float(min_h1_atr)
        self.m5_base_stop_usd_001 = float(m5_base_stop_usd_001)
        self.h1_base_stop_usd_001 = float(h1_base_stop_usd_001)
        self.enable_accel_engines = bool(enable_accel_engines)
        self.accel_leg_lot = max(0.01, min(0.05, _floor_step(float(accel_leg_lot), 0.01) or 0.01))

        # Exact validated configs from accel_pyramid_lab (single source of truth)
        self._m1_accel_cfg: AccelPyramidConfig = next(
            c for c in default_m1_accel_configs() if c.name == self.ENGINE_M1_ACCEL
        )
        self._m5_accel_cfg: AccelPyramidConfig = next(
            c for c in default_m5_accel_configs() if c.name == self.ENGINE_M5_ACCEL
        )

        # Persistent virtual ledger state
        self.realized_pnl_usd: float = 0.0
        self.peak_equity_usd: float = self.initial_capital_usd
        self.max_dd_usd: float = 0.0
        self.max_dd_pct: float = 0.0
        self.next_trade_id: int = 1
        self.last_m5_closed_ts: int = 0
        self.last_h1_closed_ts: int = 0
        self.last_m1_closed_ts: int = 0
        self.last_m5_accel_closed_ts: int = 0
        self.accel_cooldown_until_ts: dict[str, int] = {}
        self.pending_m5_pullback: dict[str, Any] | None = None
        self.pending_unfilled_pb_bar_ts: int | None = None
        self.positions: dict[str, dict[str, Any]] = {}  # keyed by engine name (max 1 open per engine)
        self.closed_trades: list[dict[str, Any]] = []
        self.last_snapshot: dict[str, Any] = {}

    # ------------------------------------------------------------------ sizing & equity
    def current_equity_usd(self, bid: float = 0.0, ask: float = 0.0) -> float:
        return round(self.initial_capital_usd + self.realized_pnl_usd + self.unrealized_pnl_usd(bid, ask), 4)

    def unrealized_pnl_usd(self, bid: float = 0.0, ask: float = 0.0) -> float:
        if not self.positions or bid <= 0 or ask <= 0:
            return 0.0
        total = 0.0
        for pos in self.positions.values():
            ex = bid if pos["side"] == "buy" else ask
            diff = (ex - pos["entry"]) if pos["side"] == "buy" else (pos["entry"] - ex)
            total += diff * pos["volume"] * self.multiplier
        return round(total, 4)

    def _update_dd_marks(self, bid: float, ask: float) -> None:
        eq = self.current_equity_usd(bid, ask)
        if eq > self.peak_equity_usd:
            self.peak_equity_usd = eq
        dd_usd = max(0.0, self.peak_equity_usd - eq)
        dd_pct = (100.0 * dd_usd / self.peak_equity_usd) if self.peak_equity_usd > 0 else 0.0
        if dd_usd > self.max_dd_usd:
            self.max_dd_usd = round(dd_usd, 4)
        if dd_pct > self.max_dd_pct:
            self.max_dd_pct = round(dd_pct, 2)

    def compute_position_lot(self, *, stop_dist_price: float, price: float, bid: float, ask: float) -> tuple[float, float]:
        """Compute virtual lot in [min_lot..max_lot] with DD governor & 50% margin ceiling."""
        eq = self.current_equity_usd(bid, ask)
        if eq <= 20.0 or stop_dist_price <= 0 or price <= 0:
            return 0.0, 0.0

        if self.fixed_lot is not None:
            vol = min(max(_floor_step(self.fixed_lot), self.min_lot), self.max_lot)
        else:
            cur_dd_pct = (100.0 * max(0.0, self.peak_equity_usd - eq) / self.peak_equity_usd) if self.peak_equity_usd > 0 else 0.0
            eff_risk_pct = self.risk_pct * 0.5 if (self.dd_governor_pct > 0 and cur_dd_pct >= self.dd_governor_pct) else self.risk_pct
            risk_budget = eq * (eff_risk_pct / 100.0)
            per_lot_risk = stop_dist_price * self.multiplier
            raw_vol = risk_budget / max(per_lot_risk, 1e-9)
            vol = _floor_step(raw_vol, 0.01)
            vol = min(max(vol, self.min_lot), self.max_lot)

        # Margin safety check: keep total used margin <= 50% of current virtual equity
        open_margin = sum(p["volume"] * self.contract_size * price / self.leverage for p in self.positions.values())
        add_margin = vol * self.contract_size * price / self.leverage
        while vol >= self.min_lot and (open_margin + add_margin) > (eq * 0.50):
            vol = round(vol - 0.01, 2)
            add_margin = vol * self.contract_size * price / self.leverage
        if vol < self.min_lot:
            return 0.0, 0.0
        risk_usd = round(stop_dist_price * vol * self.multiplier, 4)
        return vol, risk_usd

    def _open_position(
        self,
        *,
        engine: str,
        side: str,
        entry_px: float,
        stop_dist_price: float,
        ts: float,
        bid: float,
        ask: float,
        extra: dict[str, Any] | None = None,
        fixed_volume_override: float | None = None,
    ) -> dict[str, Any] | None:
        if fixed_volume_override is not None:
            eq = self.current_equity_usd(bid, ask)
            vol = _floor_step(fixed_volume_override, 0.01)
            open_margin = sum(p["volume"] * self.contract_size * entry_px / self.leverage for p in self.positions.values())
            add_margin = vol * self.contract_size * entry_px / self.leverage
            if eq <= 20.0 or vol < 0.01 or (open_margin + add_margin) > (eq * 0.50):
                return None
            risk_usd = round(stop_dist_price * vol * self.multiplier, 4)
        else:
            vol, risk_usd = self.compute_position_lot(
                stop_dist_price=stop_dist_price, price=entry_px, bid=bid, ask=ask,
            )
            if vol <= 0:
                return None
        stop_px = (entry_px - stop_dist_price) if side == "buy" else (entry_px + stop_dist_price)
        pos: dict[str, Any] = {
            "trade_id": self.next_trade_id,
            "engine": engine,
            "symbol": self.symbol,
            "side": side,
            "volume": vol,
            "entry": round(entry_px, 4),
            "stop": round(stop_px, 4),
            "initial_stop_dist_price": round(stop_dist_price, 4),
            "initial_risk_usd": risk_usd,
            "entry_spread_price": round(max(0.0, ask - bid), 5),
            "opened_at": float(ts),
            "opened_utc": _iso_utc(ts),
            "bars_held": 0,
            "trailed": False,
            "best_px": round(entry_px, 4),
            "legs_count": 1,
        }
        if extra:
            pos.update(extra)
        self.next_trade_id += 1
        self.positions[engine] = pos
        return pos

    def _close_position(self, *, engine: str, exit_px: float, ts: float, reason: str) -> dict[str, Any]:
        pos = self.positions.pop(engine)
        diff = (exit_px - pos["entry"]) if pos["side"] == "buy" else (pos["entry"] - exit_px)
        pnl = round(diff * pos["volume"] * self.multiplier, 4)
        self.realized_pnl_usd = round(self.realized_pnl_usd + pnl, 4)
        rec: dict[str, Any] = {
            "trade_id": pos["trade_id"],
            "engine": engine,
            "symbol": self.symbol,
            "side": pos["side"],
            "volume": pos["volume"],
            "legs_count": int(pos.get("legs_count", 1)),
            "entry": pos["entry"],
            "exit": round(exit_px, 4),
            "stop_final": pos["stop"],
            "initial_stop_dist_price": pos["initial_stop_dist_price"],
            "initial_risk_usd": pos["initial_risk_usd"],
            "entry_spread_price": pos["entry_spread_price"],
            "opened_at": pos["opened_at"],
            "opened_utc": pos["opened_utc"],
            "closed_at": float(ts),
            "closed_utc": _iso_utc(ts),
            "hold_seconds": round(max(0.0, float(ts) - float(pos["opened_at"])), 1),
            "bars_held": pos["bars_held"],
            "net_pnl_usd": pnl,
            "exit_reason": reason,
            "equity_after_usd": round(self.initial_capital_usd + self.realized_pnl_usd, 4),
            "paper_only": True,
            "orders_submitted": 0,
        }
        self.closed_trades.append(rec)
        # Update cooldown for acceleration pyramid engines (0 bars after win -> immediate re-entry!)
        if engine in (self.ENGINE_M1_ACCEL, self.ENGINE_M5_ACCEL):
            cfg = self._m1_accel_cfg if engine == self.ENGINE_M1_ACCEL else self._m5_accel_cfg
            period_sec = 60 if engine == self.ENGINE_M1_ACCEL else 300
            cd_bars = cfg.win_cooldown_bars if pnl > 0 else cfg.loss_cooldown_bars
            self.accel_cooldown_until_ts[engine] = int(ts) + cd_bars * period_sec
        return rec

    # ------------------------------------------------------------------ live tick handling
    def on_tick(self, *, bid: float, ask: float, ts: float) -> list[dict[str, Any]]:
        """Process one live Bid/Ask tick: check live SLs, live TPs, & pending M5 pullback limit fills."""
        events: list[dict[str, Any]] = []
        if bid <= 0 or ask <= 0 or ask < bid:
            return events

        # 1) Check live SL FIRST (worst-case), then live TP SECOND on all open virtual positions
        for eng in list(self.positions.keys()):
            pos = self.positions[eng]
            tp_px = pos.get("tp")
            if pos["side"] == "buy":
                pos["best_px"] = max(pos["best_px"], bid)
                if bid <= pos["stop"]:
                    reason = "trail_stop_hit" if pos["trailed"] else "stop_loss"
                    closed = self._close_position(engine=eng, exit_px=bid, ts=ts, reason=reason)
                    events.append({"type": "CLOSE", "trade": closed})
                elif tp_px is not None and bid >= float(tp_px):
                    closed = self._close_position(engine=eng, exit_px=bid, ts=ts, reason="take_profit")
                    events.append({"type": "CLOSE", "trade": closed})
            else:
                pos["best_px"] = min(pos["best_px"], ask)
                if ask >= pos["stop"]:
                    reason = "trail_stop_hit" if pos["trailed"] else "stop_loss"
                    closed = self._close_position(engine=eng, exit_px=ask, ts=ts, reason=reason)
                    events.append({"type": "CLOSE", "trade": closed})
                elif tp_px is not None and ask <= float(tp_px):
                    closed = self._close_position(engine=eng, exit_px=ask, ts=ts, reason="take_profit")
                    events.append({"type": "CLOSE", "trade": closed})

        # 2) Check pending M5 pullback trigger during the active M5 bar
        pb = self.pending_m5_pullback
        if pb is not None:
            if ts >= pb["expires_at"]:
                # Pullback was not touched during its 5-minute window -> remember for 2-bar fallback
                self.pending_unfilled_pb_bar_ts = int(pb["signal_bar_ts"])
                self.pending_m5_pullback = None
                events.append({"type": "PULLBACK_EXPIRED", "engine": self.ENGINE_M5, "side": pb["side"]})
            elif self.ENGINE_M5 not in self.positions:
                trig = float(pb["limit_trigger_bid"])
                touched = (bid <= trig) if pb["side"] == "buy" else (bid >= trig)
                if touched:
                    entry_px = ask if pb["side"] == "buy" else bid
                    spread = max(0.0, ask - bid)
                    sdist = self._m5_stop_distance(pb, entry_px=entry_px, spread_price=spread)
                    pos = self._open_position(
                        engine=self.ENGINE_M5,
                        side=pb["side"],
                        entry_px=entry_px,
                        stop_dist_price=sdist,
                        ts=ts,
                        bid=bid,
                        ask=ask,
                        extra={
                            "entry_mode": "impulse_pullback",
                            "impulse_mid": pb["impulse_mid"],
                            "impulse_body": pb["impulse_body"],
                            "opened_bar_ts": pb["active_bar_ts"],
                        },
                    )
                    self.pending_m5_pullback = None
                    if pos is not None:
                        events.append({"type": "OPEN", "position": dict(pos)})

        self._update_dd_marks(bid, ask)
        return events

    def _m5_stop_distance(self, sig: dict[str, Any], *, entry_px: float, spread_price: float) -> float:
        atr = float(sig["atr_m1"])
        if sig["side"] == "buy":
            struct_dist = max(entry_px - float(sig["impulse_low"]), float(sig["impulse_range"]) * 0.85, atr * 1.1)
        else:
            struct_dist = max(float(sig["impulse_high"]) + spread_price - entry_px, float(sig["impulse_range"]) * 0.85, atr * 1.1)
        max_dist = self.m5_base_stop_usd_001 / max(0.01 * self.multiplier, 1e-9)
        min_dist = max(spread_price * 2.5, self.tick_size)
        return round(min(max(struct_dist, min_dist), max_dist), 4)

    # ------------------------------------------------------------------ closed bar processing
    def on_bars(
        self,
        *,
        m5_bars: list[list[float]],
        m15_bars: list[list[float]],
        h1_bars: list[list[float]],
        bid: float,
        ask: float,
        server_now_ts: float,
        m1_bars: list[list[float]] | None = None,
    ) -> list[dict[str, Any]]:
        """Process strictly closed M1/M5/H1 bars (idempotent — safe to call repeatedly)."""
        events: list[dict[str, Any]] = []
        if bid <= 0 or ask <= 0:
            return events
        spread = max(0.0, ask - bid)

        closed_m1 = _drop_forming_bar(m1_bars or [], period_seconds=60, server_now_ts=server_now_ts)
        closed_m5 = _drop_forming_bar(m5_bars, period_seconds=300, server_now_ts=server_now_ts)
        closed_m15 = _drop_forming_bar(m15_bars, period_seconds=900, server_now_ts=server_now_ts)
        closed_h1 = _drop_forming_bar(h1_bars, period_seconds=3600, server_now_ts=server_now_ts)
        closed_h4 = resample_bars_to_tf(closed_h1, src_seconds=3600, dst_seconds=14400) if closed_h1 else []

        # --- ENGINE 1: M5_MOMENTUM_G5_ADX20_35 ---
        if len(closed_m5) >= 35 and len(closed_m15) >= 25 and len(closed_h1) >= 25:
            latest_m5 = closed_m5[-1]
            m5_bar_ts = int(latest_m5[0])
            if m5_bar_ts > self.last_m5_closed_ts:
                is_warmup_first = self.last_m5_closed_ts == 0
                self.last_m5_closed_ts = m5_bar_ts
                events.extend(
                    self._process_closed_m5(
                        closed_m5=closed_m5,
                        closed_m15=closed_m15,
                        closed_h1=closed_h1,
                        closed_h4=closed_h4,
                        bid=bid,
                        ask=ask,
                        spread=spread,
                        server_now_ts=server_now_ts,
                        allow_new_entry=True,
                        is_warmup_first=is_warmup_first,
                    )
                )

        # --- ENGINE 2: H1_DONCHIAN50_LONDON ---
        if len(closed_h1) >= 55:
            latest_h1 = closed_h1[-1]
            h1_bar_ts = int(latest_h1[0])
            if h1_bar_ts > self.last_h1_closed_ts:
                is_warmup_first = self.last_h1_closed_ts == 0
                self.last_h1_closed_ts = h1_bar_ts
                events.extend(
                    self._process_closed_h1(
                        closed_h1=closed_h1,
                        bid=bid,
                        ask=ask,
                        spread=spread,
                        server_now_ts=server_now_ts,
                        is_warmup_first=is_warmup_first,
                    )
                )

        if self.enable_accel_engines:
            # --- ENGINE 3: M1_PRE15_PB5_PYRAMID3 (Fast M1 Winner-Only 3-Leg Pyramid) ---
            if len(closed_m1) >= 120:
                m1_bar_ts = int(closed_m1[-1][0])
                if m1_bar_ts > self.last_m1_closed_ts:
                    is_warmup_first = self.last_m1_closed_ts == 0
                    self.last_m1_closed_ts = m1_bar_ts
                    events.extend(
                        self._process_closed_accel(
                            engine=self.ENGINE_M1_ACCEL,
                            snap_key="m1_accel",
                            closed_bars=closed_m1,
                            cfg=self._m1_accel_cfg,
                            period_seconds=60,
                            max_stop_usd_001=10.0,
                            bid=bid,
                            ask=ask,
                            spread=spread,
                            server_now_ts=server_now_ts,
                            is_warmup_first=is_warmup_first,
                        )
                    )

            # --- ENGINE 4: M5_VOL_PB_BURST_PYR3 (517d M5 Vol-Accel Pullback Burst Winner-Pyramid) ---
            if len(closed_m5) >= 120:
                m5a_bar_ts = int(closed_m5[-1][0])
                if m5a_bar_ts > self.last_m5_accel_closed_ts:
                    is_warmup_first = self.last_m5_accel_closed_ts == 0
                    self.last_m5_accel_closed_ts = m5a_bar_ts
                    events.extend(
                        self._process_closed_accel(
                            engine=self.ENGINE_M5_ACCEL,
                            snap_key="m5_accel",
                            closed_bars=closed_m5,
                            cfg=self._m5_accel_cfg,
                            period_seconds=300,
                            max_stop_usd_001=15.0,
                            bid=bid,
                            ask=ask,
                            spread=spread,
                            server_now_ts=server_now_ts,
                            is_warmup_first=is_warmup_first,
                        )
                    )

        self._update_dd_marks(bid, ask)
        return events

    def _process_closed_m5(
        self,
        *,
        closed_m5: list[list[float]],
        closed_m15: list[list[float]],
        closed_h1: list[list[float]],
        closed_h4: list[list[float]],
        bid: float,
        ask: float,
        spread: float,
        server_now_ts: float,
        allow_new_entry: bool,
        is_warmup_first: bool,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        last_bar = closed_m5[-1]
        bar_open_ts = int(last_bar[0])
        eval_ts = bar_open_ts + 300
        atr_list = atr_series(closed_m5[-40:], period=14)
        atr_now = float(atr_list[-1] or max(last_bar[2] - last_bar[3], self.tick_size))

        # 1) Manage open M5 position on bar close (fast invalidation cut + monotonic trail_only)
        if self.ENGINE_M5 in self.positions:
            pos = self.positions[self.ENGINE_M5]
            pos["bars_held"] += 1
            side = pos["side"]
            o, h, low, c = last_bar[1], last_bar[2], last_bar[3], last_bar[4]
            close_exit = bid if side == "buy" else ask
            net_now = ((close_exit - pos["entry"]) if side == "buy" else (pos["entry"] - close_exit)) * pos["volume"] * self.multiplier

            # 1a) Fast invalidation cut (first 2 bars, in loss, opposing bar closes past impulse_mid)
            imp_mid = pos.get("impulse_mid")
            if (
                pos["bars_held"] <= 2
                and net_now < 0
                and imp_mid is not None
                and ((side == "buy" and c < o and c < float(imp_mid)) or (side == "sell" and c > o and c > float(imp_mid)))
            ):
                closed = self._close_position(engine=self.ENGINE_M5, exit_px=close_exit, ts=server_now_ts, reason="fast_invalidation_cut")
                events.append({"type": "CLOSE", "trade": closed})
            else:
                # 1b) Monotonic trail_only armed at +0.8R
                arm_usd = 0.8 * float(pos["initial_risk_usd"])
                if net_now >= arm_usd - 1e-9:
                    trail_buf = 0.15 * atr_now
                    if side == "buy":
                        be_stop = pos["entry"] + max(self.tick_size, 0.05)
                        cand = min(c - self.tick_size, max(pos["stop"], be_stop, low - trail_buf))
                        if cand > pos["stop"]:
                            pos["stop"] = round(cand, 4)
                            pos["trailed"] = True
                            events.append({"type": "TRAIL", "engine": self.ENGINE_M5, "new_stop": pos["stop"]})
                    else:
                        be_stop = pos["entry"] - max(self.tick_size, 0.05)
                        cand = max(c + spread + self.tick_size, min(pos["stop"], be_stop, h + spread + trail_buf))
                        if cand < pos["stop"]:
                            pos["stop"] = round(cand, 4)
                            pos["trailed"] = True
                            events.append({"type": "TRAIL", "engine": self.ENGINE_M5, "new_stop": pos["stop"]})

        # 2) Expire any leftover pullback from previous bar
        if self.pending_m5_pullback is not None and eval_ts >= self.pending_m5_pullback["expires_at"]:
            self.pending_unfilled_pb_bar_ts = int(self.pending_m5_pullback["signal_bar_ts"])
            self.pending_m5_pullback = None

        # 3) Check new M5 signal (skip opening on a stale historical bar right at startup if bar is >90s old)
        m15_idx = build_htf_index(closed_m15, period_seconds=900, ema_fast=9, ema_slow=21)
        h1_idx = build_htf_index(closed_h1, period_seconds=3600, ema_fast=9, ema_slow=21)
        h4_idx = build_htf_index(closed_h4, period_seconds=14400, ema_fast=20, ema_slow=50)
        m15_c = lookup_htf(m15_idx[0], m15_idx[1], eval_ts)
        h1_c = lookup_htf(h1_idx[0], h1_idx[1], eval_ts)
        h4_c = lookup_htf(h4_idx[0], h4_idx[1], eval_ts)

        utc_hour = (eval_ts // 3600) % 24
        in_session = utc_hour not in LONDON_NY_BLOCKED_UTC_HOURS

        seg = closed_m5[-40:] + [[float(eval_ts), last_bar[4], last_bar[4], last_bar[4], last_bar[4], 0.0]]
        seg_atr = atr_series(seg, period=14)
        idx_now = len(seg) - 1

        sig: dict[str, Any] | None = None
        if self.pending_unfilled_pb_bar_ts is not None and len(seg) >= 3 and self.pending_unfilled_pb_bar_ts == int(seg[-3][0]):
            sig = detect_m1_impulse(seg, seg_atr, idx_now, entry_mode="impulse_2bar", spread_price=spread)
        self.pending_unfilled_pb_bar_ts = None
        if sig is None:
            sig = detect_m1_impulse(seg, seg_atr, idx_now, entry_mode="impulse_pullback", spread_price=spread)

        m5_status = "flat_no_signal"
        bar_age_sec = max(0.0, server_now_ts - float(eval_ts))
        fresh_for_entry = (not is_warmup_first) or (bar_age_sec <= 90.0)

        if sig is not None:
            if not in_session:
                m5_status = f"blocked_hour_{utc_hour}utc"
            else:
                ok_htf, deny = htf_allows_signal(
                    side=sig["side"], htf_filter="m15_h1_h4", m5=m15_c, m15=h1_c, h1=h4_c,
                    min_m5_adx=18.0, min_mid_adx=20.0, max_mid_adx=35.0,
                )
                if not ok_htf:
                    m5_status = f"htf_denied:{deny}"
                elif self.ENGINE_M5 in self.positions:
                    m5_status = "position_already_open"
                elif not allow_new_entry or not fresh_for_entry:
                    m5_status = "startup_bar_stale_skipped"
                elif sig["limit_trigger_bid"] is not None:
                    # Arm 1-bar pullback limit watch (checked immediately against live bid!)
                    trig = float(sig["limit_trigger_bid"])
                    touched_now = (bid <= trig) if sig["side"] == "buy" else (bid >= trig)
                    if touched_now:
                        entry_px = ask if sig["side"] == "buy" else bid
                        sdist = self._m5_stop_distance(sig, entry_px=entry_px, spread_price=spread)
                        pos = self._open_position(
                            engine=self.ENGINE_M5, side=sig["side"], entry_px=entry_px,
                            stop_dist_price=sdist, ts=server_now_ts, bid=bid, ask=ask,
                            extra={
                                "entry_mode": "impulse_pullback",
                                "impulse_mid": sig["impulse_mid"],
                                "impulse_body": sig["impulse_body"],
                                "opened_bar_ts": eval_ts,
                            },
                        )
                        if pos is not None:
                            events.append({"type": "OPEN", "position": dict(pos)})
                            m5_status = f"opened_{sig['side']}_pullback"
                    else:
                        self.pending_m5_pullback = {
                            "side": sig["side"],
                            "limit_trigger_bid": round(trig, 4),
                            "impulse_low": float(sig["impulse_low"]),
                            "impulse_high": float(sig["impulse_high"]),
                            "impulse_range": float(sig["impulse_range"]),
                            "impulse_mid": float(sig["impulse_mid"]),
                            "impulse_body": float(sig["impulse_body"]),
                            "atr_m1": float(sig["atr_m1"]),
                            "signal_bar_ts": int(sig["bar_time"]),
                            "active_bar_ts": eval_ts,
                            "expires_at": float(eval_ts + 300),
                        }
                        events.append({"type": "ARM_PULLBACK", "engine": self.ENGINE_M5, "side": sig["side"], "trigger_bid": round(trig, 4)})
                        m5_status = f"armed_pullback_{sig['side']}@{trig:.2f}"
                else:
                    entry_px = ask if sig["side"] == "buy" else bid
                    sdist = self._m5_stop_distance(sig, entry_px=entry_px, spread_price=spread)
                    pos = self._open_position(
                        engine=self.ENGINE_M5, side=sig["side"], entry_px=entry_px,
                        stop_dist_price=sdist, ts=server_now_ts, bid=bid, ask=ask,
                        extra={
                            "entry_mode": "impulse_2bar",
                            "impulse_mid": sig["impulse_mid"],
                            "impulse_body": sig["impulse_body"],
                            "opened_bar_ts": eval_ts,
                        },
                    )
                    if pos is not None:
                        events.append({"type": "OPEN", "position": dict(pos)})
                        m5_status = f"opened_{sig['side']}_2bar"

        self.last_snapshot["m5"] = {
            "bar_ts": bar_open_ts,
            "bar_utc": _iso_utc(bar_open_ts),
            "utc_hour": utc_hour,
            "in_session": in_session,
            "atr_m5": round(atr_now, 3),
            "m15_adx": round(m15_c.adx, 2) if (m15_c and m15_c.adx is not None) else None,
            "h1_adx": round(h1_c.adx, 2) if (h1_c and h1_c.adx is not None) else None,
            "status": m5_status,
        }
        return events

    def _process_closed_h1(
        self,
        *,
        closed_h1: list[list[float]],
        bid: float,
        ask: float,
        spread: float,
        server_now_ts: float,
        is_warmup_first: bool,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        b1 = closed_h1[-1]
        bar_open_ts = int(b1[0])
        eval_ts = bar_open_ts + 3600
        hr_open = (eval_ts // 3600) % 24
        atr_vals = atr_series(closed_h1[-70:], period=14)
        atr1 = atr_vals[-1]

        # 1) Manage existing H1 position on closed bar
        if self.ENGINE_H1 in self.positions:
            pos = self.positions[self.ENGINE_H1]
            pos["bars_held"] += 1
            side = pos["side"]
            ex_px = bid if side == "buy" else ask
            win20 = closed_h1[-21:-1]
            dc20_lo = min(x[3] for x in win20)
            dc20_hi = max(x[2] for x in win20)

            if pos["bars_held"] >= 100:
                closed = self._close_position(engine=self.ENGINE_H1, exit_px=ex_px, ts=server_now_ts, reason="max_100_h1_bars")
                events.append({"type": "CLOSE", "trade": closed})
            elif (side == "buy" and b1[4] < dc20_lo) or (side == "sell" and b1[4] > dc20_hi):
                closed = self._close_position(engine=self.ENGINE_H1, exit_px=ex_px, ts=server_now_ts, reason="donchian20_exit")
                events.append({"type": "CLOSE", "trade": closed})
            else:
                atr_now = float(atr1 or (pos["initial_stop_dist_price"] / 2.5))
                sdist = float(pos["initial_stop_dist_price"])
                if side == "buy":
                    pos["best_px"] = max(pos["best_px"], b1[2])
                    if (b1[4] - pos["entry"]) >= 1.5 * sdist:
                        cand = max(pos["stop"], pos["entry"], pos["best_px"] - 2.5 * atr_now)
                        if cand > pos["stop"]:
                            pos["stop"] = round(cand, 4)
                            pos["trailed"] = True
                            events.append({"type": "TRAIL", "engine": self.ENGINE_H1, "new_stop": pos["stop"]})
                else:
                    pos["best_px"] = min(pos["best_px"], b1[3])
                    if (pos["entry"] - (b1[4] + spread)) >= 1.5 * sdist:
                        cand = min(pos["stop"], pos["entry"], pos["best_px"] + spread + 2.5 * atr_now)
                        if cand < pos["stop"]:
                            pos["stop"] = round(cand, 4)
                            pos["trailed"] = True
                            events.append({"type": "TRAIL", "engine": self.ENGINE_H1, "new_stop": pos["stop"]})

        # 2) Entry check on strictly closed H1 bar (London 08-16 UTC, ATR >= min_h1_atr, Donchian 50)
        win50 = closed_h1[-51:-1]
        dc50_hi = max(x[2] for x in win50)
        dc50_lo = min(x[3] for x in win50)
        bar_age_sec = max(0.0, server_now_ts - float(eval_ts))
        fresh_for_entry = (not is_warmup_first) or (bar_age_sec <= 300.0)

        h1_status = "flat_inside_dc50"
        if self.ENGINE_H1 in self.positions:
            h1_status = f"open_{self.positions[self.ENGINE_H1]['side']}"
        elif not (8 <= hr_open < 16):
            h1_status = f"outside_london_{hr_open}utc"
        elif atr1 is None or atr1 < self.min_h1_atr:
            h1_status = f"low_atr({round(atr1 or 0.0, 2)}<{self.min_h1_atr})"
        elif not fresh_for_entry:
            h1_status = "startup_bar_stale_skipped"
        else:
            side = "buy" if b1[4] > dc50_hi else ("sell" if b1[4] < dc50_lo else None)
            if side is not None:
                entry_px = ask if side == "buy" else bid
                max_sdist = self.h1_base_stop_usd_001 / max(0.01 * self.multiplier, 1e-9)
                sdist = round(min(max(2.5 * float(atr1), spread * 4.0), max_sdist), 4)
                pos = self._open_position(
                    engine=self.ENGINE_H1, side=side, entry_px=entry_px,
                    stop_dist_price=sdist, ts=server_now_ts, bid=bid, ask=ask,
                    extra={"dc50_hi": round(dc50_hi, 3), "dc50_lo": round(dc50_lo, 3)},
                )
                if pos is not None:
                    events.append({"type": "OPEN", "position": dict(pos)})
                    h1_status = f"opened_{side}_dc50"

        self.last_snapshot["h1"] = {
            "bar_ts": bar_open_ts,
            "bar_utc": _iso_utc(bar_open_ts),
            "utc_hour": hr_open,
            "atr_h1": round(float(atr1 or 0.0), 3),
            "dc50_hi": round(dc50_hi, 2),
            "dc50_lo": round(dc50_lo, 2),
            "close": round(b1[4], 2),
            "status": h1_status,
        }
        return events

    def _process_closed_accel(
        self,
        *,
        engine: str,
        snap_key: str,
        closed_bars: list[list[float]],
        cfg: AccelPyramidConfig,
        period_seconds: int,
        max_stop_usd_001: float,
        bid: float,
        ask: float,
        spread: float,
        server_now_ts: float,
        is_warmup_first: bool,
    ) -> list[dict[str, Any]]:
        """Process strictly closed M1/M5 bar for a Winner-Only Pyramid Acceleration engine."""
        events: list[dict[str, Any]] = []
        window = closed_bars[-200:]
        closes = [b[4] for b in window]
        ema9 = [float(v) if v is not None else 0.0 for v in ema_series(closes, 9)]
        ema21 = [float(v) if v is not None else 0.0 for v in ema_series(closes, 21)]
        ema50 = [float(v) if v is not None else 0.0 for v in ema_series(closes, 50)]
        atr14 = [float(v) if v is not None else 0.0 for v in atr_series(window, period=14)]
        atr_base = _rolling_mean(atr14, 100)

        idx = len(window) - 1
        last_bar = window[idx]
        bar_open_ts = int(last_bar[0])
        eval_ts = bar_open_ts + period_seconds
        c = float(last_bar[4])
        atr_now = atr14[idx]
        vol_ratio = round(atr_now / atr_base[idx], 2) if atr_base[idx] > 0 else 0.0

        # 1) Manage existing acceleration position (Winner-Only Pyramid + BE/profit lock + trail + time-stop)
        if engine in self.positions:
            pos = self.positions[engine]
            pos["bars_held"] += 1
            side = pos["side"]
            ent1 = float(pos.get("leg1_entry", pos["entry"]))
            init_risk = float(pos["initial_stop_dist_price"])
            k = int(pos.get("legs_count", 1))

            # Bar-close milestone check (exact parity with accel_pyramid_lab.simulate_accel_pyramid)
            cur_gain = (c - ent1) if side == "buy" else (ent1 - (c + spread))
            if init_risk > 0 and cur_gain >= k * cfg.pyr_step_r * init_risk:
                lock_mult = 0.05 if k == 1 else (0.45 * (k - 1))
                old_stop = float(pos["stop"])
                if side == "buy":
                    new_stop = round(max(old_stop, ent1 + lock_mult * init_risk), 4)
                else:
                    new_stop = round(min(old_stop, ent1 - lock_mult * init_risk), 4)
                pos["stop"] = new_stop
                pos["trailed"] = True
                if new_stop != old_stop:
                    events.append({"type": "TRAIL", "engine": engine, "new_stop": new_stop})

                # Winner-only scale-in: add leg k+1 at live market ONLY if price hasn't breached locked SL
                if k < cfg.max_legs:
                    add_px = ask if side == "buy" else bid
                    safe_side = (side == "buy" and bid > new_stop) or (side == "sell" and ask < new_stop)
                    eq = self.current_equity_usd(bid, ask)
                    open_margin = sum(p["volume"] * self.contract_size * add_px / self.leverage for p in self.positions.values())
                    add_margin = self.accel_leg_lot * self.contract_size * add_px / self.leverage
                    if safe_side and eq > 20.0 and (open_margin + add_margin) <= (eq * 0.50):
                        old_vol = float(pos["volume"])
                        new_vol = round(old_vol + self.accel_leg_lot, 2)
                        new_avg = round((float(pos["entry"]) * old_vol + add_px * self.accel_leg_lot) / new_vol, 4)
                        pos["volume"] = new_vol
                        pos["entry"] = new_avg
                        pos["legs_count"] = k + 1
                        legs_list = list(pos.get("legs") or [{"entry": ent1, "volume": old_vol, "opened_at": pos["opened_at"]}])
                        legs_list.append({"entry": round(add_px, 4), "volume": self.accel_leg_lot, "opened_at": float(server_now_ts)})
                        pos["legs"] = legs_list
                        events.append({
                            "type": "PYRAMID_ADD",
                            "engine": engine,
                            "side": side,
                            "leg_num": k + 1,
                            "add_price": round(add_px, 4),
                            "total_volume": new_vol,
                            "avg_entry": new_avg,
                            "stop": new_stop,
                        })

            # Monotonic ATR trailing stop once BE/profit lock has armed
            if pos["trailed"] and cfg.trail_atr > 0 and atr_now > 0:
                old_stop = float(pos["stop"])
                if side == "buy":
                    cand = round(max(old_stop, c - cfg.trail_atr * atr_now), 4)
                else:
                    cand = round(min(old_stop, (c + spread) + cfg.trail_atr * atr_now), 4)
                if cand != old_stop:
                    pos["stop"] = cand
                    events.append({"type": "TRAIL", "engine": engine, "new_stop": cand})

            # Hard time-stop
            if pos["bars_held"] >= cfg.max_hold_bars:
                ex_px = bid if side == "buy" else ask
                closed = self._close_position(engine=engine, exit_px=ex_px, ts=server_now_ts, reason="time_stop")
                events.append({"type": "CLOSE", "trade": closed})

        # 2) Flat entry check on strictly closed bar
        bar_age_sec = max(0.0, server_now_ts - float(eval_ts))
        fresh_for_entry = (not is_warmup_first) or (bar_age_sec <= min(120.0, float(period_seconds)))
        cd_until = int(self.accel_cooldown_until_ts.get(engine, 0))

        status = "watching"
        if engine in self.positions:
            p = self.positions[engine]
            status = f"open_{p['side']}_{p.get('legs_count', 1)}L"
        elif int(server_now_ts) < cd_until:
            status = "cooldown"
        elif not fresh_for_entry:
            status = "startup_bar_stale_skipped"
        else:
            sig = _detect_accel_signal(idx, window, ema9, ema21, ema50, atr14, atr_base, cfg)
            if sig is not None:
                entry_px = ask if sig == "buy" else bid
                max_sdist = max_stop_usd_001 / max(0.01 * self.multiplier, 1e-9)
                sl_dist = round(min(max(spread * 3.0, cfg.sl_atr * atr_now), max_sdist), 4)
                tp_dist = round(max(spread * 4.0, cfg.tp_atr * atr_now), 4)
                tp_px = round((entry_px + tp_dist) if sig == "buy" else (entry_px - tp_dist), 4)
                pos = self._open_position(
                    engine=engine,
                    side=sig,
                    entry_px=entry_px,
                    stop_dist_price=sl_dist,
                    ts=server_now_ts,
                    bid=bid,
                    ask=ask,
                    fixed_volume_override=self.accel_leg_lot,
                    extra={
                        "tp": tp_px,
                        "leg1_entry": round(entry_px, 4),
                        "legs_count": 1,
                        "legs": [{"entry": round(entry_px, 4), "volume": self.accel_leg_lot, "opened_at": float(server_now_ts)}],
                    },
                )
                if pos is not None:
                    events.append({"type": "OPEN", "position": dict(pos)})
                    status = f"opened_{sig}_1L"

        self.last_snapshot[snap_key] = {
            "bar_ts": bar_open_ts,
            "bar_utc": _iso_utc(bar_open_ts),
            "atr": round(atr_now, 3),
            "vol_ratio": vol_ratio,
            "status": status,
        }
        return events

    # ------------------------------------------------------------------ summary & persistence
    def summary(self, bid: float = 0.0, ask: float = 0.0) -> dict[str, Any]:
        wins = [t for t in self.closed_trades if t["net_pnl_usd"] > 0]
        losses = [t for t in self.closed_trades if t["net_pnl_usd"] <= 0]
        gw = sum(t["net_pnl_usd"] for t in wins)
        gl = abs(sum(t["net_pnl_usd"] for t in losses))
        pf = round(gw / gl, 3) if gl > 0 else (99.0 if gw > 0 else None)

        by_engine: dict[str, dict[str, Any]] = {}
        for t in self.closed_trades:
            eng = str(t.get("engine", "unknown"))
            st = by_engine.setdefault(eng, {"trades": 0, "legs": 0, "wins": 0, "losses": 0, "net_usd": 0.0})
            st["trades"] += 1
            st["legs"] += int(t.get("legs_count", 1))
            if t["net_pnl_usd"] > 0:
                st["wins"] += 1
            else:
                st["losses"] += 1
            st["net_usd"] = round(st["net_usd"] + float(t["net_pnl_usd"]), 4)

        return {
            "symbol": self.symbol,
            "paper_only": True,
            "orders_submitted": 0,
            "enable_accel_engines": self.enable_accel_engines,
            "initial_capital_usd": self.initial_capital_usd,
            "realized_pnl_usd": self.realized_pnl_usd,
            "unrealized_pnl_usd": self.unrealized_pnl_usd(bid, ask),
            "equity_usd": self.current_equity_usd(bid, ask),
            "peak_equity_usd": round(self.peak_equity_usd, 4),
            "max_dd_usd": self.max_dd_usd,
            "max_dd_pct": self.max_dd_pct,
            "closed_trades_count": len(self.closed_trades),
            "wins": len(wins),
            "losses": len(losses),
            "pf": pf,
            "by_engine": by_engine,
            "open_positions": list(self.positions.values()),
            "pending_m5_pullback": self.pending_m5_pullback,
            "last_snapshot": self.last_snapshot,
        }

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "schema": "champions-paper-state/v1",
            "symbol": self.symbol,
            "initial_capital_usd": self.initial_capital_usd,
            "risk_pct": self.risk_pct,
            "fixed_lot": self.fixed_lot,
            "min_lot": self.min_lot,
            "max_lot": self.max_lot,
            "dd_governor_pct": self.dd_governor_pct,
            "enable_accel_engines": self.enable_accel_engines,
            "accel_leg_lot": self.accel_leg_lot,
            "realized_pnl_usd": self.realized_pnl_usd,
            "peak_equity_usd": self.peak_equity_usd,
            "max_dd_usd": self.max_dd_usd,
            "max_dd_pct": self.max_dd_pct,
            "next_trade_id": self.next_trade_id,
            "last_m5_closed_ts": self.last_m5_closed_ts,
            "last_h1_closed_ts": self.last_h1_closed_ts,
            "last_m1_closed_ts": self.last_m1_closed_ts,
            "last_m5_accel_closed_ts": self.last_m5_accel_closed_ts,
            "accel_cooldown_until_ts": self.accel_cooldown_until_ts,
            "pending_m5_pullback": self.pending_m5_pullback,
            "pending_unfilled_pb_bar_ts": self.pending_unfilled_pb_bar_ts,
            "positions": self.positions,
            "closed_trades": self.closed_trades,
            "last_snapshot": self.last_snapshot,
        }

    def load_state_dict(self, data: dict[str, Any]) -> None:
        if data.get("schema") != "champions-paper-state/v1":
            return
        if str(data.get("symbol", "")).upper() != self.symbol.upper():
            return
        self.realized_pnl_usd = float(data.get("realized_pnl_usd", 0.0))
        self.peak_equity_usd = float(data.get("peak_equity_usd", self.initial_capital_usd))
        self.max_dd_usd = float(data.get("max_dd_usd", 0.0))
        self.max_dd_pct = float(data.get("max_dd_pct", 0.0))
        self.next_trade_id = int(data.get("next_trade_id", 1))
        self.last_m5_closed_ts = int(data.get("last_m5_closed_ts", 0))
        self.last_h1_closed_ts = int(data.get("last_h1_closed_ts", 0))
        self.last_m1_closed_ts = int(data.get("last_m1_closed_ts", 0))
        self.last_m5_accel_closed_ts = int(data.get("last_m5_accel_closed_ts", 0))
        self.accel_cooldown_until_ts = {
            str(k): int(v) for k, v in (data.get("accel_cooldown_until_ts") or {}).items()
        }
        self.pending_m5_pullback = data.get("pending_m5_pullback")
        self.pending_unfilled_pb_bar_ts = data.get("pending_unfilled_pb_bar_ts")
        self.positions = dict(data.get("positions") or {})
        self.closed_trades = list(data.get("closed_trades") or [])
        self.last_snapshot = dict(data.get("last_snapshot") or {})


def save_state_atomic(path: Path, state: dict[str, Any]) -> None:
    """Atomically write owner-only (0600) state JSON inside logs/."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def append_trade_journal(path: Path, trade: dict[str, Any]) -> None:
    """Append one closed virtual trade line to owner-only (0600) JSONL journal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(trade, ensure_ascii=False) + "\n")
        fh.flush()
    os.chmod(path, 0o600)
