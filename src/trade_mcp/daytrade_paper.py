"""Bounded live-feed Paper simulator for Day Trader M1 signals.

This module NEVER imports execution/order APIs and NEVER submits or modifies
broker orders. All balances, positions, fills and P&L are simulated paper
results marked against observed Bid/Ask quotes. Spread is paid via entry Ask /
exit Bid for buy and entry Bid / exit Ask for sell. Commission defaults to an
explicit zero simulation assumption unless supplied by the caller.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from .daytrade import M1_SECONDS, entry_signal, m1_rows
from .daytrade_extra import scan_setups
from .risk import loss_per_lot, round_to_step

HARD_MAX_LOT = 0.10
HARD_MAX_STOP_USD = 10.0
MIN_TARGET_USD = 2.0
MAX_TARGET_USD = 10.0
MIN_DURATION_SECONDS = 10
MAX_DURATION_SECONDS = 600


def _finite(value: Any, name: str, *, minimum: float | None = None,
            maximum: float | None = None) -> float:
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


def _round_tick(price: float, tick_size: float, rounding: str) -> float:
    step = Decimal(str(tick_size))
    value = Decimal(str(price))
    return float((value / step).to_integral_value(rounding=rounding) * step)


def contiguous_m1_tail(bars: list[list[float]]) -> list[list[float]]:
    """Return the longest contiguous trailing M1 segment (60-second spacing)."""
    clean = m1_rows(bars)
    start = len(clean) - 1
    while start > 0 and int(clean[start][0] - clean[start - 1][0]) == M1_SECONDS:
        start -= 1
    return clean[start:]


def evaluate_m1_direction(
    bars: list[list[float]],
    *,
    symbol: str = "XAUUSD_i",
    opening_range_bars: int = 3,
    include_patterns: bool = True,
) -> dict[str, Any]:
    """Detect closed-M1 buy/sell direction using daytrade.entry_signal.

    The final bar is treated as currently forming and excluded by entry_signal.
    A signal fires only when the newest closed M1 bar breaks out of the
    immediately preceding ``opening_range_bars`` closed M1 bars.
    """
    if type(opening_range_bars) is not int or not 2 <= opening_range_bars <= 15:
        raise ValueError("opening_range_bars must be an integer between 2 and 15")
    tail = contiguous_m1_tail(bars)
    needed = opening_range_bars + 2  # range bars + 1 closed trigger bar + 1 forming bar
    if len(tail) < needed:
        return {
            "ok": True,
            "action": "wait",
            "reason": "insufficient_contiguous_m1",
            "closed_bar_time": int(tail[-2][0]) if len(tail) >= 2 else None,
            "forming_bar_time": int(tail[-1][0]),
            "pattern_tags": [],
        }

    window = tail[-needed:]
    start_time = int(window[0][0])
    closed_bar_time = int(window[-2][0])
    forming_bar_time = int(window[-1][0])

    buy = entry_signal(
        window, side="buy", start_time=start_time,
        setup="opening_range", opening_range_bars=opening_range_bars,
    )
    sell = entry_signal(
        window, side="sell", start_time=start_time,
        setup="opening_range", opening_range_bars=opening_range_bars,
    )

    pattern_tags: list[str] = []
    if include_patterns and len(tail) >= 31:
        scan = scan_setups([{"symbol": symbol, "ohlcv": tail[-min(120, len(tail)):]}], mode="patterns")
        if scan.get("results") and scan["results"][0].get("status") == "scanned":
            pattern_tags = list(scan["results"][0].get("setups") or [])

    buy_hit = buy.get("status") == "triggered" and bool(buy.get("trigger_is_latest_closed_bar"))
    sell_hit = sell.get("status") == "triggered" and bool(sell.get("trigger_is_latest_closed_bar"))
    opening_range = buy.get("opening_range") or sell.get("opening_range") or {}

    if buy_hit and not sell_hit:
        return {
            "ok": True,
            "action": "buy",
            "reason": "m1_opening_range_breakout_up",
            "closed_bar_time": closed_bar_time,
            "forming_bar_time": forming_bar_time,
            "trigger_close": float(buy["trigger"]["close"]),
            "range_high": float(opening_range["high"]),
            "range_low": float(opening_range["low"]),
            "range_edge": float(opening_range["low"]),
            "pattern_tags": pattern_tags,
        }
    if sell_hit and not buy_hit:
        return {
            "ok": True,
            "action": "sell",
            "reason": "m1_opening_range_breakout_down",
            "closed_bar_time": closed_bar_time,
            "forming_bar_time": forming_bar_time,
            "trigger_close": float(sell["trigger"]["close"]),
            "range_high": float(opening_range["high"]),
            "range_low": float(opening_range["low"]),
            "range_edge": float(opening_range["high"]),
            "pattern_tags": pattern_tags,
        }
    return {
        "ok": True,
        "action": "wait",
        "reason": "conflicting_signal" if (buy_hit and sell_hit) else "no_m1_breakout",
        "closed_bar_time": closed_bar_time,
        "forming_bar_time": forming_bar_time,
        "range_high": float(opening_range["high"]) if "high" in opening_range else None,
        "range_low": float(opening_range["low"]) if "low" in opening_range else None,
        "pattern_tags": pattern_tags,
    }


def audit_symbol_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Audit broker symbol sizing fields and flag contract-vs-tick discrepancies."""
    tick_size = _finite(spec.get("trade_tick_size"), "trade_tick_size", minimum=1e-10)
    tick_value = _finite(spec.get("trade_tick_value"), "trade_tick_value", minimum=1e-10)
    point = _finite(spec.get("point"), "point", minimum=1e-10)
    tick_multiplier = loss_per_lot(
        {"trade_tick_size": tick_size, "trade_tick_value": tick_value, "point": point}, 1.0
    )

    contract_raw = spec.get("trade_contract_size")
    contract_size: float | None = None
    if isinstance(contract_raw, (int, float)) and not isinstance(contract_raw, bool):
        cval = float(contract_raw)
        if math.isfinite(cval) and cval > 0:
            contract_size = cval

    currency_raw = spec.get("currency_profit")
    currency_profit = str(currency_raw).strip() if currency_raw is not None and str(currency_raw).strip() else None
    calc_raw = spec.get("trade_calc_mode")
    trade_calc_mode = str(calc_raw).strip() if calc_raw is not None and str(calc_raw).strip() else None

    economics_mismatch: bool | None = None
    contract_to_tick_ratio: float | None = None
    warning: str | None = None
    if contract_size is not None and currency_profit and currency_profit.upper() == "USD":
        contract_to_tick_ratio = round(contract_size / tick_multiplier, 4)
        economics_mismatch = abs(contract_to_tick_ratio - 1.0) > 0.01
        if economics_mismatch:
            warning = (
                f"Broker spec discrepancy: trade_contract_size={contract_size:g} (currency_profit=USD) "
                f"differs by {contract_to_tick_ratio:g}x from trade_tick_value/trade_tick_size="
                f"{tick_multiplier:g} (tick_value={tick_value:g}, tick_size={tick_size:g}). "
                "Verify real MT5 PnL per 0.01 lot before trusting lot sizing on any broker account."
            )

    return {
        "trade_tick_size": tick_size,
        "trade_tick_value": tick_value,
        "point": point,
        "tick_multiplier": round(tick_multiplier, 6),
        "trade_contract_size": contract_size,
        "currency_profit": currency_profit,
        "trade_calc_mode": trade_calc_mode,
        "contract_to_tick_ratio": contract_to_tick_ratio,
        "economics_mismatch": economics_mismatch,
        "warning": warning,
    }


def small_profit_guard_defaults(target_usd: float) -> dict[str, Any]:
    """Return sensible floating small-profit guard defaults scaled to target_usd."""
    target = _finite(target_usd, "target_usd", minimum=MIN_TARGET_USD, maximum=MAX_TARGET_USD)
    trigger = round(target * 0.5, 2)
    floor = round(max(0.5, target * 0.2), 2)
    if floor >= trigger:
        floor = round(trigger * 0.5, 2)
    return {
        "profit_protect_trigger_usd": trigger,
        "profit_protect_floor_usd": floor,
        "stale_profit_seconds": 180.0,
        "stale_min_profit_usd": 0.5,
        "exit_on_opposite_if_profit": True,
        "close_profitable_at_end": True,
    }


def plan_paper_trade(
    *,
    side: str,
    bid: float,
    ask: float,
    range_edge: float,
    spec: dict[str, Any],
    stop_usd: float = 10.0,
    target_usd: float = 2.0,
    max_lot: float = HARD_MAX_LOT,
    commission_per_lot: float = 0.0,
    max_spread_ratio: float = 0.40,
    pricing_basis: str = "tick_value",
) -> dict[str, Any]:
    """Size and price one bounded paper trade from live Bid/Ask and broker spec."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    if pricing_basis not in ("tick_value", "contract_size"):
        raise ValueError("pricing_basis must be 'tick_value' or 'contract_size'")
    bid = _finite(bid, "bid", minimum=1e-10)
    ask = _finite(ask, "ask", minimum=bid)
    range_edge = _finite(range_edge, "range_edge", minimum=1e-10)
    stop_usd = _finite(stop_usd, "stop_usd", minimum=1.0, maximum=HARD_MAX_STOP_USD)
    target_usd = _finite(target_usd, "target_usd", minimum=MIN_TARGET_USD, maximum=MAX_TARGET_USD)
    max_lot = _finite(max_lot, "max_lot", minimum=0.001, maximum=HARD_MAX_LOT)
    commission_per_lot = _finite(commission_per_lot, "commission_per_lot", minimum=0.0, maximum=1000.0)
    max_spread_ratio = _finite(max_spread_ratio, "max_spread_ratio", minimum=0.05, maximum=0.80)

    tick_size = _finite(spec.get("trade_tick_size"), "trade_tick_size", minimum=1e-10)
    tick_value = _finite(spec.get("trade_tick_value"), "trade_tick_value", minimum=1e-10)
    volume_min = _finite(spec.get("volume_min"), "volume_min", minimum=1e-8)
    volume_max = _finite(spec.get("volume_max"), "volume_max", minimum=volume_min)
    volume_step = _finite(spec.get("volume_step"), "volume_step", minimum=1e-8)
    point = _finite(spec.get("point"), "point", minimum=1e-10)
    stops_level = _finite(spec.get("trade_stops_level", 0.0), "trade_stops_level", minimum=0.0)

    if pricing_basis == "contract_size":
        currency = str(spec.get("currency_profit") or "").strip().upper()
        if currency != "USD":
            return {"ok": False, "reason": "contract_size_requires_usd_profit_currency"}
        multiplier = _finite(spec.get("trade_contract_size"), "trade_contract_size", minimum=1e-10)
    else:
        multiplier = loss_per_lot(
            {"trade_tick_size": tick_size, "trade_tick_value": tick_value, "point": point}, 1.0
        )
    if not math.isfinite(multiplier) or multiplier <= 0:
        return {"ok": False, "reason": "invalid_tick_economics"}

    entry = ask if side == "buy" else bid
    spread = ask - bid
    min_stop_level_dist = stops_level * point

    # Give the trade at least the natural M1 range distance OR the stop_usd budget
    # distance at max_lot so narrow 1-minute jitter does not force a micro-stop.
    max_lot_cap = min(max_lot, volume_max)
    min_budget_dist = max(0.0, (stop_usd / max_lot_cap - commission_per_lot) / multiplier)
    raw_edge_dist = (entry - range_edge) if side == "buy" else (range_edge - entry)
    stop_dist = max(raw_edge_dist, min_budget_dist, min_stop_level_dist, tick_size)

    if spread > 0 and spread >= stop_dist * max_spread_ratio:
        return {"ok": False, "reason": "spread_too_wide", "spread": spread, "stop_distance": stop_dist}

    per_lot_risk = stop_dist * multiplier + commission_per_lot
    if per_lot_risk <= 0:
        return {"ok": False, "reason": "invalid_risk_distance"}

    raw_volume = stop_usd / per_lot_risk
    volume = round_to_step(min(raw_volume, max_lot_cap), volume_step, "down")
    if volume < volume_min:
        return {"ok": False, "reason": "volume_below_minimum", "raw_volume": round(raw_volume, 6)}

    # Keep rounded stop inside the $stop_usd planned ceiling.
    max_price_dist = (stop_usd - commission_per_lot * volume) / (volume * multiplier)
    if max_price_dist <= 0:
        return {"ok": False, "reason": "commission_exceeds_stop_budget"}
    effective_stop_dist = min(stop_dist, max_price_dist)

    if side == "buy":
        stop_price = _round_tick(entry - effective_stop_dist, tick_size, ROUND_CEILING)
        target_dist = (target_usd + commission_per_lot * volume) / (volume * multiplier)
        target_price = _round_tick(entry + target_dist, tick_size, ROUND_CEILING)
        valid_sides = stop_price < entry < target_price
    else:
        stop_price = _round_tick(entry + effective_stop_dist, tick_size, ROUND_FLOOR)
        target_dist = (target_usd + commission_per_lot * volume) / (volume * multiplier)
        target_price = _round_tick(entry - target_dist, tick_size, ROUND_FLOOR)
        valid_sides = target_price < entry < stop_price

    if not valid_sides:
        return {"ok": False, "reason": "invalid_rounded_levels"}
    if min_stop_level_dist > 0 and (
        abs(entry - stop_price) + 1e-12 < min_stop_level_dist
        or abs(target_price - entry) + 1e-12 < min_stop_level_dist
    ):
        return {"ok": False, "reason": "stops_level_violation"}

    planned_stop_usd = abs(entry - stop_price) * volume * multiplier + commission_per_lot * volume
    if planned_stop_usd > stop_usd + 1e-6:
        return {"ok": False, "reason": "rounded_sl_exceeds_budget"}
    planned_target_usd = abs(target_price - entry) * volume * multiplier - commission_per_lot * volume
    entry_spread_usd = spread * volume * multiplier

    return {
        "ok": True,
        "paper": True,
        "executable": False,
        "execution_authorized": False,
        "side": side,
        "entry": entry,
        "stop": stop_price,
        "target": target_price,
        "volume": volume,
        "multiplier": multiplier,
        "planned_stop_usd": round(planned_stop_usd, 4),
        "planned_target_usd": round(planned_target_usd, 4),
        "entry_spread_usd": round(entry_spread_usd, 4),
        "commission_usd": round(commission_per_lot * volume, 4),
    }


@dataclass
class PaperPosition:
    trade_id: int
    symbol: str
    side: str
    volume: float
    entry: float
    stop: float
    target: float
    multiplier: float
    commission_usd: float
    entry_spread_usd: float
    opened_at: float
    signal_bar_time: int
    signal_reason: str
    pattern_tags: list[str] = field(default_factory=list)
    last_mark_price: float = 0.0
    unrealized_pnl_usd: float = 0.0
    max_favorable_usd: float = 0.0
    max_adverse_usd: float = 0.0
    profit_floor_armed: bool = False


class DaytradePaperSession:
    """In-memory bounded Paper trading session for live M1 quotes and bars.

    Enforces single open position, $10 max planned stop, $2-$10 profit target,
    realistic Bid/Ask spread accounting, optional floating small-profit guards,
    and a 3-consecutive-loss latch.
    """

    def __init__(
        self,
        *,
        symbol: str,
        spec: dict[str, Any],
        duration_seconds: int = 600,
        target_usd: float = 2.0,
        stop_usd: float = 10.0,
        opening_range_bars: int = 3,
        max_lot: float = HARD_MAX_LOT,
        commission_per_lot: float = 0.0,
        max_consecutive_losses: int = 3,
        max_spread_ratio: float = 0.40,
        initial_balance: float = 100.0,
        started_at: float = 0.0,
        pricing_basis: str = "tick_value",
        profit_protect_trigger_usd: float | None = None,
        profit_protect_floor_usd: float | None = None,
        stale_profit_seconds: float | None = None,
        stale_min_profit_usd: float = 0.50,
        exit_on_opposite_if_profit: bool = False,
        close_profitable_at_end: bool = False,
        enable_loss_latch: bool = True,
    ):
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        if type(duration_seconds) is not int or not MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS:
            raise ValueError(f"duration_seconds must be {MIN_DURATION_SECONDS}..{MAX_DURATION_SECONDS}")
        if type(opening_range_bars) is not int or not 2 <= opening_range_bars <= 15:
            raise ValueError("opening_range_bars must be 2..15")
        if type(max_consecutive_losses) is not int or not 1 <= max_consecutive_losses <= 5:
            raise ValueError("max_consecutive_losses must be 1..5")
        if pricing_basis not in ("tick_value", "contract_size"):
            raise ValueError("pricing_basis must be 'tick_value' or 'contract_size'")
        self.symbol = symbol.strip()
        self.spec = dict(spec)
        self.spec_audit = audit_symbol_spec(self.spec)
        self.pricing_basis = pricing_basis
        self.duration_seconds = duration_seconds
        self.target_usd = _finite(target_usd, "target_usd", minimum=MIN_TARGET_USD, maximum=MAX_TARGET_USD)
        self.stop_usd = _finite(stop_usd, "stop_usd", minimum=1.0, maximum=HARD_MAX_STOP_USD)
        self.opening_range_bars = opening_range_bars
        self.max_lot = _finite(max_lot, "max_lot", minimum=0.001, maximum=HARD_MAX_LOT)
        self.commission_per_lot = _finite(commission_per_lot, "commission_per_lot", minimum=0.0, maximum=1000.0)
        self.max_consecutive_losses = max_consecutive_losses
        self.max_spread_ratio = _finite(max_spread_ratio, "max_spread_ratio", minimum=0.05, maximum=0.80)
        self.initial_balance = _finite(initial_balance, "initial_balance", minimum=10.0)
        self.started_at = float(started_at)
        self.enable_loss_latch = bool(enable_loss_latch)

        if (profit_protect_trigger_usd is None) != (profit_protect_floor_usd is None):
            raise ValueError("profit_protect_trigger_usd and profit_protect_floor_usd must be set together")
        if profit_protect_trigger_usd is not None and profit_protect_floor_usd is not None:
            self.profit_protect_trigger_usd: float | None = _finite(
                profit_protect_trigger_usd, "profit_protect_trigger_usd", minimum=0.05, maximum=self.target_usd,
            )
            self.profit_protect_floor_usd: float | None = _finite(
                profit_protect_floor_usd, "profit_protect_floor_usd", minimum=0.01,
            )
            if self.profit_protect_floor_usd >= self.profit_protect_trigger_usd:
                raise ValueError("profit_protect_floor_usd must be < profit_protect_trigger_usd")
        else:
            self.profit_protect_trigger_usd = None
            self.profit_protect_floor_usd = None

        self.stale_profit_seconds: float | None = (
            _finite(stale_profit_seconds, "stale_profit_seconds", minimum=30.0)
            if stale_profit_seconds is not None else None
        )
        self.stale_min_profit_usd = _finite(
            stale_min_profit_usd, "stale_min_profit_usd", minimum=0.01, maximum=self.target_usd,
        )
        self.exit_on_opposite_if_profit = bool(exit_on_opposite_if_profit)
        self.close_profitable_at_end = bool(close_profitable_at_end)

        self.balance = self.initial_balance
        self.peak_equity = self.initial_balance
        self.max_drawdown_usd = 0.0
        self.position: PaperPosition | None = None
        self.last_evaluated_bar_time: int | None = None
        self.loss_streak = 0
        self.max_loss_streak = 0
        self.latch_triggers = 0
        self.latched = False

        self.polls = 0
        self.m1_bars_evaluated = 0
        self.signals_buy = 0
        self.signals_sell = 0
        self.signals_wait = 0
        self.refusal_counts: dict[str, int] = {}
        self.closed_trades: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self._next_trade_id = 0

    def _refuse(self, reason: str) -> None:
        self.refusal_counts[reason] = self.refusal_counts.get(reason, 0) + 1

    def _update_drawdown(self) -> None:
        equity = self.balance + (self.position.unrealized_pnl_usd if self.position else 0.0)
        if equity > self.peak_equity:
            self.peak_equity = equity
        dd = max(0.0, self.peak_equity - equity)
        if dd > self.max_drawdown_usd:
            self.max_drawdown_usd = dd

    def _close_position(self, *, exit_price: float, exit_reason: str, now: float) -> dict[str, Any]:
        pos = self.position
        assert pos is not None
        sign = 1.0 if pos.side == "buy" else -1.0
        gross_pnl = (exit_price - pos.entry) * sign * pos.volume * pos.multiplier
        net_pnl = gross_pnl - pos.commission_usd
        pos.last_mark_price = exit_price
        pos.unrealized_pnl_usd = net_pnl
        pos.max_favorable_usd = max(pos.max_favorable_usd, net_pnl)
        pos.max_adverse_usd = min(pos.max_adverse_usd, net_pnl)

        self.balance += net_pnl
        if net_pnl < 0:
            self.loss_streak += 1
            if self.loss_streak > self.max_loss_streak:
                self.max_loss_streak = self.loss_streak
            if self.loss_streak >= self.max_consecutive_losses:
                self.latch_triggers += 1
                if self.enable_loss_latch:
                    self.latched = True
        else:
            self.loss_streak = 0

        hold_seconds = round(max(0.0, float(now) - pos.opened_at), 2)
        record = {
            "trade_id": pos.trade_id,
            "paper": True,
            "symbol": pos.symbol,
            "side": pos.side,
            "volume": pos.volume,
            "entry": pos.entry,
            "exit": exit_price,
            "stop": pos.stop,
            "target": pos.target,
            "exit_reason": exit_reason,
            "gross_pnl_usd": round(gross_pnl, 4),
            "commission_usd": round(pos.commission_usd, 4),
            "net_pnl_usd": round(net_pnl, 4),
            "entry_spread_usd": round(pos.entry_spread_usd, 4),
            "max_favorable_usd": round(pos.max_favorable_usd, 4),
            "max_adverse_usd": round(pos.max_adverse_usd, 4),
            "profit_floor_armed": pos.profit_floor_armed,
            "hold_seconds": hold_seconds,
            "opened_at": pos.opened_at,
            "closed_at": float(now),
            "signal_bar_time": pos.signal_bar_time,
            "signal_reason": pos.signal_reason,
            "pattern_tags": list(pos.pattern_tags),
        }
        self.closed_trades.append(record)
        self.events.append({"event": "close", **record})
        self.position = None
        self._update_drawdown()
        return record

    def seed_baseline(self, bars: list[list[float]]) -> int | None:
        """Mark already-closed M1 bars before window start so stale bars do not fire."""
        tail = contiguous_m1_tail(bars)
        if len(tail) >= 2:
            self.last_evaluated_bar_time = int(tail[-2][0])
        return self.last_evaluated_bar_time

    def mark_quote(self, *, bid: float, ask: float, now: float) -> dict[str, Any]:
        """Mark open paper position against live Bid/Ask and auto-close on SL/TP/guards."""
        bid = _finite(bid, "bid", minimum=1e-10)
        ask = _finite(ask, "ask", minimum=bid)
        self.polls += 1
        pos = self.position
        if pos is None:
            return {"ok": True, "paper": True, "open": False, "closed": False}

        exit_price = bid if pos.side == "buy" else ask
        sign = 1.0 if pos.side == "buy" else -1.0
        gross_pnl = (exit_price - pos.entry) * sign * pos.volume * pos.multiplier
        net_pnl = gross_pnl - pos.commission_usd
        pos.last_mark_price = exit_price
        pos.unrealized_pnl_usd = net_pnl
        pos.max_favorable_usd = max(pos.max_favorable_usd, net_pnl)
        pos.max_adverse_usd = min(pos.max_adverse_usd, net_pnl)

        if self.profit_protect_trigger_usd is not None and net_pnl >= self.profit_protect_trigger_usd - 1e-9:
            pos.profit_floor_armed = True

        target_hit = (
            net_pnl >= self.target_usd - 1e-9
            or (pos.side == "buy" and exit_price >= pos.target)
            or (pos.side == "sell" and exit_price <= pos.target)
        )
        stop_hit = (
            net_pnl <= -self.stop_usd + 1e-9
            or (pos.side == "buy" and exit_price <= pos.stop)
            or (pos.side == "sell" and exit_price >= pos.stop)
        )

        # Conservative precedence: real stop always wins; then full target; then floating small-profit guards.
        exit_reason: str | None = None
        if stop_hit:
            exit_reason = "stop_hit"
        elif target_hit:
            exit_reason = "target_hit"
        elif (
            pos.profit_floor_armed
            and self.profit_protect_floor_usd is not None
            and net_pnl <= self.profit_protect_floor_usd + 1e-9
        ):
            exit_reason = "profit_floor_hit"
        elif (
            self.stale_profit_seconds is not None
            and max(0.0, float(now) - pos.opened_at) >= self.stale_profit_seconds
            and net_pnl >= self.stale_min_profit_usd - 1e-9
        ):
            exit_reason = "stale_small_profit"

        if exit_reason is None:
            self._update_drawdown()
            return {
                "ok": True,
                "paper": True,
                "open": True,
                "closed": False,
                "trade_id": pos.trade_id,
                "side": pos.side,
                "unrealized_pnl_usd": round(net_pnl, 4),
                "profit_floor_armed": pos.profit_floor_armed,
            }

        record = self._close_position(exit_price=exit_price, exit_reason=exit_reason, now=now)
        return {
            "ok": True,
            "paper": True,
            "open": False,
            "closed": True,
            "trade": record,
            "latched": self.latched,
        }

    def finalize_window(self, *, bid: float, ask: float, now: float) -> dict[str, Any]:
        """At window end, close an open position ONLY if profitable and guard is enabled.

        Losing positions are never closed at a fabricated profit; they stay honestly
        marked as open unrealized losses.
        """
        bid = _finite(bid, "bid", minimum=1e-10)
        ask = _finite(ask, "ask", minimum=bid)
        if self.position is None:
            return {"ok": True, "paper": True, "closed": False}
        mark = self.mark_quote(bid=bid, ask=ask, now=now)
        if mark.get("closed"):
            return mark
        if not self.close_profitable_at_end or self.position is None:
            return {"ok": True, "paper": True, "closed": False, "open": self.position is not None}
        pos = self.position
        exit_price = bid if pos.side == "buy" else ask
        sign = 1.0 if pos.side == "buy" else -1.0
        net_pnl = (exit_price - pos.entry) * sign * pos.volume * pos.multiplier - pos.commission_usd
        if net_pnl >= self.stale_min_profit_usd - 1e-9:
            record = self._close_position(exit_price=exit_price, exit_reason="window_end_profit", now=now)
            return {
                "ok": True,
                "paper": True,
                "open": False,
                "closed": True,
                "trade": record,
                "latched": self.latched,
            }
        return {"ok": True, "paper": True, "closed": False, "open": True}

    def on_bars(
        self,
        *,
        bars: list[list[float]],
        bid: float,
        ask: float,
        now: float,
        include_patterns: bool = True,
    ) -> dict[str, Any]:
        """Evaluate newly closed M1 bars and open at most one paper position."""
        bid = _finite(bid, "bid", minimum=1e-10)
        ask = _finite(ask, "ask", minimum=bid)
        sig = evaluate_m1_direction(
            bars,
            symbol=self.symbol,
            opening_range_bars=self.opening_range_bars,
            include_patterns=include_patterns,
        )
        closed_time = sig.get("closed_bar_time")
        if closed_time is None:
            self._refuse(str(sig.get("reason") or "insufficient_contiguous_m1"))
            return {"ok": True, "paper": True, "opened": False, "action": "wait", "reason": sig.get("reason")}
        if self.last_evaluated_bar_time is not None and closed_time <= self.last_evaluated_bar_time:
            return {"ok": True, "paper": True, "opened": False, "action": "already_evaluated"}

        self.last_evaluated_bar_time = int(closed_time)
        self.m1_bars_evaluated += 1
        action = sig["action"]
        if action == "buy":
            self.signals_buy += 1
        elif action == "sell":
            self.signals_sell += 1
        else:
            self.signals_wait += 1
            return {"ok": True, "paper": True, "opened": False, "action": "wait",
                    "reason": sig.get("reason"), "closed_bar_time": closed_time}

        if self.position is not None:
            if self.exit_on_opposite_if_profit and action != self.position.side:
                pos = self.position
                exit_price = bid if pos.side == "buy" else ask
                sign = 1.0 if pos.side == "buy" else -1.0
                net_pnl = (exit_price - pos.entry) * sign * pos.volume * pos.multiplier - pos.commission_usd
                if net_pnl >= self.stale_min_profit_usd - 1e-9:
                    record = self._close_position(
                        exit_price=exit_price, exit_reason="opposite_signal_profit", now=now,
                    )
                    return {
                        "ok": True,
                        "paper": True,
                        "opened": False,
                        "closed_by_opposite": True,
                        "action": action,
                        "reason": "opposite_signal_profit",
                        "trade": record,
                    }
            self._refuse("position_already_open")
            return {"ok": True, "paper": True, "opened": False, "action": action, "reason": "position_already_open"}

        if self.latched:
            self._refuse("loss_latch_active")
            return {"ok": True, "paper": True, "opened": False, "action": action, "reason": "loss_latch_active"}

        plan = plan_paper_trade(
            side=action, bid=bid, ask=ask, range_edge=float(sig["range_edge"]),
            spec=self.spec, stop_usd=self.stop_usd, target_usd=self.target_usd,
            max_lot=self.max_lot, commission_per_lot=self.commission_per_lot,
            max_spread_ratio=self.max_spread_ratio, pricing_basis=self.pricing_basis,
        )
        if not plan.get("ok"):
            reason = str(plan.get("reason") or "sizing_refused")
            self._refuse(reason)
            return {"ok": True, "paper": True, "opened": False, "action": action, "reason": reason}

        self._next_trade_id += 1
        exit_side_price = bid if action == "buy" else ask
        sign = 1.0 if action == "buy" else -1.0
        init_pnl = (exit_side_price - plan["entry"]) * sign * plan["volume"] * plan["multiplier"] - plan["commission_usd"]
        pos = PaperPosition(
            trade_id=self._next_trade_id,
            symbol=self.symbol,
            side=action,
            volume=plan["volume"],
            entry=plan["entry"],
            stop=plan["stop"],
            target=plan["target"],
            multiplier=plan["multiplier"],
            commission_usd=plan["commission_usd"],
            entry_spread_usd=plan["entry_spread_usd"],
            opened_at=float(now),
            signal_bar_time=int(closed_time),
            signal_reason=str(sig["reason"]),
            pattern_tags=list(sig.get("pattern_tags") or []),
            last_mark_price=exit_side_price,
            unrealized_pnl_usd=init_pnl,
            max_favorable_usd=init_pnl,
            max_adverse_usd=init_pnl,
        )
        self.position = pos
        event = {
            "event": "open",
            "paper": True,
            "trade_id": pos.trade_id,
            "symbol": pos.symbol,
            "side": pos.side,
            "volume": pos.volume,
            "entry": pos.entry,
            "stop": pos.stop,
            "target": pos.target,
            "planned_stop_usd": plan["planned_stop_usd"],
            "planned_target_usd": plan["planned_target_usd"],
            "entry_spread_usd": plan["entry_spread_usd"],
            "commission_usd": plan["commission_usd"],
            "opened_at": pos.opened_at,
            "signal_bar_time": pos.signal_bar_time,
            "signal_reason": pos.signal_reason,
            "pattern_tags": list(pos.pattern_tags),
        }
        self.events.append(event)
        self._update_drawdown()
        return {"ok": True, "paper": True, "opened": True, "action": action, "trade": event}

    def summary(self, *, ended_at: float) -> dict[str, Any]:
        """Return a sanitized, honest summary of the bounded paper session."""
        wins = sum(1 for t in self.closed_trades if t["net_pnl_usd"] > 0)
        losses = sum(1 for t in self.closed_trades if t["net_pnl_usd"] < 0)
        breakeven = len(self.closed_trades) - wins - losses
        realized = round(sum(t["net_pnl_usd"] for t in self.closed_trades), 4)
        unrealized = round(self.position.unrealized_pnl_usd, 4) if self.position else 0.0
        total_marked = round(realized + unrealized, 4)
        win_rate = round(wins / len(self.closed_trades) * 100.0, 2) if self.closed_trades else None
        breakeven_wr = round(self.stop_usd / (self.stop_usd + self.target_usd) * 100.0, 2)

        exit_reasons: dict[str, int] = {}
        for t in self.closed_trades:
            r = str(t["exit_reason"])
            exit_reasons[r] = exit_reasons.get(r, 0) + 1

        gross_wins = sum(t["net_pnl_usd"] for t in self.closed_trades if t["net_pnl_usd"] > 0)
        gross_losses = abs(sum(t["net_pnl_usd"] for t in self.closed_trades if t["net_pnl_usd"] < 0))
        profit_factor = round(gross_wins / gross_losses, 3) if gross_losses > 0 else None

        sanitized_trades = [
            {
                "trade_id": t["trade_id"],
                "side": t["side"],
                "volume": t["volume"],
                "exit_reason": t["exit_reason"],
                "net_pnl_usd": t["net_pnl_usd"],
                "entry_spread_usd": t["entry_spread_usd"],
                "hold_seconds": t["hold_seconds"],
            }
            for t in self.closed_trades
        ]
        open_summary = None
        if self.position is not None:
            open_summary = {
                "trade_id": self.position.trade_id,
                "side": self.position.side,
                "volume": self.position.volume,
                "unrealized_pnl_usd": round(self.position.unrealized_pnl_usd, 4),
                "entry_spread_usd": round(self.position.entry_spread_usd, 4),
                "hold_seconds": round(max(0.0, float(ended_at) - self.position.opened_at), 2),
                "profit_floor_armed": self.position.profit_floor_armed,
                "closed_at_window_end": False,
            }

        guard_active = any([
            self.profit_protect_trigger_usd is not None,
            self.stale_profit_seconds is not None,
            self.exit_on_opposite_if_profit,
            self.close_profitable_at_end,
        ])

        warnings = [
            "Paper simulation only: zero broker orders were created or submitted.",
            f"A +${self.target_usd:g} target vs -${self.stop_usd:g} stop requires >{breakeven_wr}% win rate to break even before costs.",
            "Ten minutes is a tiny sample and cannot establish long-term profitability.",
            "Real broker fills can experience slippage, requotes and gaps beyond -$10.",
        ]
        if self.commission_per_lot == 0.0:
            warnings.append("Commission is modeled as 0.0 USD/lot (simulation assumption, not verified broker commission).")
        if self.spec_audit.get("warning"):
            warnings.append(str(self.spec_audit["warning"]))
        if guard_active:
            warnings.append(
                "Floating small-profit guard only protects trades that reach positive P&L; "
                "trades moving straight into loss still hit the -$10 stop or stay negative."
            )

        return {
            "ok": True,
            "paper": True,
            "executable": False,
            "execution_authorized": False,
            "orders_submitted": 0,
            "symbol": self.symbol,
            "duration_limit_seconds": self.duration_seconds,
            "elapsed_seconds": round(max(0.0, float(ended_at) - self.started_at), 2),
            "parameters": {
                "target_usd": self.target_usd,
                "stop_usd": self.stop_usd,
                "opening_range_bars": self.opening_range_bars,
                "max_lot": self.max_lot,
                "max_consecutive_losses": self.max_consecutive_losses,
                "commission_assumption_usd_per_lot": self.commission_per_lot,
                "initial_paper_balance_usd": self.initial_balance,
                "pricing_basis": self.pricing_basis,
                "floating_profit_guard": {
                    "enabled": guard_active,
                    "profit_protect_trigger_usd": self.profit_protect_trigger_usd,
                    "profit_protect_floor_usd": self.profit_protect_floor_usd,
                    "stale_profit_seconds": self.stale_profit_seconds,
                    "stale_min_profit_usd": self.stale_min_profit_usd if guard_active else None,
                    "exit_on_opposite_if_profit": self.exit_on_opposite_if_profit,
                    "close_profitable_at_end": self.close_profitable_at_end,
                },
            },
            "spec_audit": dict(self.spec_audit),
            "polls": self.polls,
            "m1_bars_evaluated": self.m1_bars_evaluated,
            "signals": {
                "buy": self.signals_buy,
                "sell": self.signals_sell,
                "wait": self.signals_wait,
            },
            "refusal_counts": dict(self.refusal_counts),
            "exit_reasons": exit_reasons,
            "trades_opened": self._next_trade_id,
            "trades_closed": len(self.closed_trades),
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate_pct": win_rate,
            "profit_factor": profit_factor,
            "required_breakeven_win_rate_pct": breakeven_wr,
            "realized_pnl_usd": realized,
            "open_position_at_end": self.position is not None,
            "open_position": open_summary,
            "unrealized_pnl_usd": unrealized,
            "total_marked_pnl_usd": total_marked,
            "ending_paper_balance_usd": round(self.balance, 4),
            "max_drawdown_usd": round(self.max_drawdown_usd, 4),
            "loss_streak": self.loss_streak,
            "max_loss_streak": self.max_loss_streak,
            "latch_triggers": self.latch_triggers,
            "latched": self.latched,
            "trades": sanitized_trades,
            "warnings": warnings,
        }


def contiguous_m1_segments(bars: list[list[float]]) -> list[list[list[float]]]:
    """Split sorted M1 bars into contiguous 60-second segments."""
    clean = m1_rows(bars)
    if not clean:
        return []
    segments: list[list[list[float]]] = [[clean[0]]]
    for row in clean[1:]:
        if int(row[0] - segments[-1][-1][0]) == M1_SECONDS:
            segments[-1].append(row)
        else:
            segments.append([row])
    return segments


def _floor_price_for_pnl(pos: PaperPosition, floor_usd: float) -> float:
    sign = 1.0 if pos.side == "buy" else -1.0
    delta = (floor_usd + pos.commission_usd) / (pos.volume * pos.multiplier)
    return pos.entry + sign * delta


def _simulate_m1_bar_intrabar(
    session: DaytradePaperSession,
    bar: list[float],
    *,
    spread_price: float,
) -> None:
    """Conservatively step an open paper position across one M1 OHLC bar.

    Uses Bid OHLC with constant ``spread_price`` added for Ask. If a bar's
    range touches both SL and TP/profit-floor, SL ALWAYS wins (conservative).
    """
    pos = session.position
    if pos is None:
        return

    t, o, h, low, c = float(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])
    bar_end = t + M1_SECONDS
    sign = 1.0 if pos.side == "buy" else -1.0
    scale = pos.volume * pos.multiplier

    if pos.side == "buy":
        open_exit = o
        worst_exit = low
        best_exit = h
        close_exit = c
    else:
        open_exit = o + spread_price
        worst_exit = h + spread_price
        best_exit = low + spread_price
        close_exit = c + spread_price

    worst_pnl = (worst_exit - pos.entry) * sign * scale - pos.commission_usd
    best_pnl = (best_exit - pos.entry) * sign * scale - pos.commission_usd
    close_pnl = (close_exit - pos.entry) * sign * scale - pos.commission_usd

    pos.max_adverse_usd = min(pos.max_adverse_usd, worst_pnl)
    pos.max_favorable_usd = max(pos.max_favorable_usd, best_pnl)

    # 1. Conservative stop check first (catches intrabar adverse move and gap opens).
    stop_hit = (
        worst_pnl <= -session.stop_usd + 1e-9
        or (pos.side == "buy" and worst_exit <= pos.stop)
        or (pos.side == "sell" and worst_exit >= pos.stop)
    )
    if stop_hit:
        fill = min(open_exit, pos.stop) if pos.side == "buy" else max(open_exit, pos.stop)
        session._close_position(exit_price=fill, exit_reason="stop_hit", now=bar_end)
        return

    # 2. If profit floor was ALREADY armed before this bar, check adverse floor breach BEFORE TP.
    if pos.profit_floor_armed and session.profit_protect_floor_usd is not None:
        floor_px = _floor_price_for_pnl(pos, session.profit_protect_floor_usd)
        floor_hit = (
            worst_pnl <= session.profit_protect_floor_usd + 1e-9
            or (pos.side == "buy" and worst_exit <= floor_px)
            or (pos.side == "sell" and worst_exit >= floor_px)
        )
        if floor_hit:
            fill = min(open_exit, floor_px) if pos.side == "buy" else max(open_exit, floor_px)
            session._close_position(exit_price=fill, exit_reason="profit_floor_hit", now=bar_end)
            return

    # 3. Full target check (fills conservatively at planned target, or open if gapped past target).
    target_hit = (
        best_pnl >= session.target_usd - 1e-9
        or (pos.side == "buy" and best_exit >= pos.target)
        or (pos.side == "sell" and best_exit <= pos.target)
    )
    if target_hit:
        fill = max(open_exit, pos.target) if pos.side == "buy" else min(open_exit, pos.target)
        session._close_position(exit_price=fill, exit_reason="target_hit", now=bar_end)
        return

    # 4. Arm profit floor if bar's favorable excursion reached trigger; check close pullback.
    if (
        not pos.profit_floor_armed
        and session.profit_protect_trigger_usd is not None
        and session.profit_protect_floor_usd is not None
        and best_pnl >= session.profit_protect_trigger_usd - 1e-9
    ):
        pos.profit_floor_armed = True
        if close_pnl <= session.profit_protect_floor_usd + 1e-9:
            session._close_position(exit_price=close_exit, exit_reason="profit_floor_hit", now=bar_end)
            return

    # 5. Stale-time small-profit exit at bar close.
    pos.last_mark_price = close_exit
    pos.unrealized_pnl_usd = close_pnl
    if (
        session.stale_profit_seconds is not None
        and max(0.0, bar_end - pos.opened_at) >= session.stale_profit_seconds
        and close_pnl >= session.stale_min_profit_usd - 1e-9
    ):
        session._close_position(exit_price=close_exit, exit_reason="stale_small_profit", now=bar_end)
        return

    session._update_drawdown()


def replay_m1_bars(
    bars: list[list[float]],
    *,
    spec: dict[str, Any],
    symbol: str = "XAUUSD_i",
    spread_price: float = 0.0,
    target_usd: float = 2.0,
    stop_usd: float = 10.0,
    opening_range_bars: int = 3,
    max_lot: float = HARD_MAX_LOT,
    commission_per_lot: float = 0.0,
    max_consecutive_losses: int = 3,
    max_spread_ratio: float = 0.40,
    pricing_basis: str = "tick_value",
    profit_protect_trigger_usd: float | None = None,
    profit_protect_floor_usd: float | None = None,
    stale_profit_seconds: float | None = None,
    stale_min_profit_usd: float = 0.50,
    exit_on_opposite_if_profit: bool = False,
    close_profitable_at_end: bool = False,
    enable_loss_latch: bool = False,
) -> dict[str, Any]:
    """Replay historical M1 OHLC bars with zero lookahead and conservative intrabar order.

    This is an offline simulation helper only: never calls MT5 or submits orders.
    """
    spread_price = _finite(spread_price, "spread_price", minimum=0.0)
    segments = contiguous_m1_segments(bars)
    usable_segments = [s for s in segments if len(s) >= opening_range_bars + 2]
    started_at = float(usable_segments[0][0][0]) if usable_segments else 0.0

    session = DaytradePaperSession(
        symbol=symbol,
        spec=spec,
        duration_seconds=MAX_DURATION_SECONDS,
        target_usd=target_usd,
        stop_usd=stop_usd,
        opening_range_bars=opening_range_bars,
        max_lot=max_lot,
        commission_per_lot=commission_per_lot,
        max_consecutive_losses=max_consecutive_losses,
        max_spread_ratio=max_spread_ratio,
        started_at=started_at,
        pricing_basis=pricing_basis,
        profit_protect_trigger_usd=profit_protect_trigger_usd,
        profit_protect_floor_usd=profit_protect_floor_usd,
        stale_profit_seconds=stale_profit_seconds,
        stale_min_profit_usd=stale_min_profit_usd,
        exit_on_opposite_if_profit=exit_on_opposite_if_profit,
        close_profitable_at_end=close_profitable_at_end,
        enable_loss_latch=enable_loss_latch,
    )

    last_bar: list[float] | None = None
    needed = opening_range_bars + 2
    for seg in usable_segments:
        # If a position carried across a gap into a new segment, mark early bars first.
        for j in range(min(needed - 1, len(seg))):
            if session.position is not None:
                _simulate_m1_bar_intrabar(session, seg[j], spread_price=spread_price)
                last_bar = seg[j]
        for i in range(needed - 1, len(seg)):
            bar = seg[i]
            t, o = float(bar[0]), float(bar[1])
            bid_open = o
            ask_open = o + spread_price
            window = seg[i - (needed - 1) : i + 1]
            # 1. At bar open (right after seg[i-1] closed), evaluate signal with zero lookahead.
            session.on_bars(
                bars=window,
                bid=bid_open,
                ask=ask_open,
                now=t,
                include_patterns=False,
            )
            # 2. Step through bar[i]'s OHLC conservatively.
            _simulate_m1_bar_intrabar(session, bar, spread_price=spread_price)
            last_bar = bar

    ended_at = float(last_bar[0]) + M1_SECONDS if last_bar is not None else started_at
    if last_bar is not None and session.position is not None:
        c = float(last_bar[4])
        session.finalize_window(bid=c, ask=c + spread_price, now=ended_at)

    out = session.summary(ended_at=ended_at)
    out["mode"] = "historical_m1_ohlc_replay"
    out["replay_bars_total"] = sum(len(s) for s in segments)
    out["replay_segments_total"] = len(segments)
    out["replay_segments_used"] = len(usable_segments)
    out["spread_price_assumption"] = spread_price
    out["loss_latch_enforced"] = enable_loss_latch
    out["warnings"] = [
        "Historical M1 OHLC replay only: zero broker orders were created or submitted.",
        "Intrabar tick order is approximated conservatively from M1 OHLC (SL checked before TP on ambiguous bars).",
        "Constant spread assumption applied from observed quote; real spreads widen around news and rollovers.",
        f"A +${target_usd:g} target vs -${stop_usd:g} stop requires >{out['required_breakeven_win_rate_pct']}% win rate to break even before costs.",
    ]
    if commission_per_lot == 0.0:
        out["warnings"].append(
            "Commission is modeled as 0.0 USD/lot (simulation assumption, not verified broker commission)."
        )
    if session.spec_audit.get("warning"):
        out["warnings"].append(str(session.spec_audit["warning"]))
    return out


def replay_m1_matrix(
    bars: list[list[float]],
    *,
    spec: dict[str, Any],
    symbol: str = "XAUUSD_i",
    spread_price: float = 0.0,
    targets: list[float] | tuple[float, ...] = (2.0, 4.0, 6.0, 10.0),
    stop_usd: float = 10.0,
    opening_range_bars: int = 3,
    max_lot: float = HARD_MAX_LOT,
    commission_per_lot: float = 0.0,
    pricing_basis: str = "tick_value",
) -> list[dict[str, Any]]:
    """Compare baseline vs floating small-profit guard across target USD levels."""
    rows: list[dict[str, Any]] = []
    for target in targets:
        base = replay_m1_bars(
            bars, spec=spec, symbol=symbol, spread_price=spread_price,
            target_usd=float(target), stop_usd=stop_usd,
            opening_range_bars=opening_range_bars, max_lot=max_lot,
            commission_per_lot=commission_per_lot, pricing_basis=pricing_basis,
        )
        guard_cfg = small_profit_guard_defaults(float(target))
        guarded = replay_m1_bars(
            bars, spec=spec, symbol=symbol, spread_price=spread_price,
            target_usd=float(target), stop_usd=stop_usd,
            opening_range_bars=opening_range_bars, max_lot=max_lot,
            commission_per_lot=commission_per_lot, pricing_basis=pricing_basis,
            **guard_cfg,
        )
        for label, res in (("no_guard", base), ("small_profit_guard", guarded)):
            rows.append({
                "target_usd": float(target),
                "stop_usd": float(stop_usd),
                "mode": label,
                "pricing_basis": pricing_basis,
                "trades_opened": res["trades_opened"],
                "trades_closed": res["trades_closed"],
                "wins": res["wins"],
                "losses": res["losses"],
                "win_rate_pct": res["win_rate_pct"],
                "required_breakeven_win_rate_pct": res["required_breakeven_win_rate_pct"],
                "realized_pnl_usd": res["realized_pnl_usd"],
                "unrealized_pnl_usd": res["unrealized_pnl_usd"],
                "total_marked_pnl_usd": res["total_marked_pnl_usd"],
                "max_drawdown_usd": res["max_drawdown_usd"],
                "profit_factor": res["profit_factor"],
                "max_loss_streak": res["max_loss_streak"],
                "latch_triggers": res["latch_triggers"],
                "exit_reasons": res["exit_reasons"],
                "refusal_counts": res["refusal_counts"],
            })
    return rows

