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


def plan_paper_trade(
    *,
    side: str,
    bid: float,
    ask: float,
    range_edge: float,
    spec: dict[str, float],
    stop_usd: float = 10.0,
    target_usd: float = 2.0,
    max_lot: float = HARD_MAX_LOT,
    commission_per_lot: float = 0.0,
    max_spread_ratio: float = 0.40,
) -> dict[str, Any]:
    """Size and price one bounded paper trade from live Bid/Ask and broker spec."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
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


class DaytradePaperSession:
    """In-memory bounded Paper trading session for live M1 quotes and bars.

    Enforces single open position, $10 max planned stop, $2-$10 profit target,
    realistic Bid/Ask spread accounting, and a 3-consecutive-loss latch.
    """

    def __init__(
        self,
        *,
        symbol: str,
        spec: dict[str, float],
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
    ):
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        if type(duration_seconds) is not int or not MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS:
            raise ValueError(f"duration_seconds must be {MIN_DURATION_SECONDS}..{MAX_DURATION_SECONDS}")
        if type(opening_range_bars) is not int or not 2 <= opening_range_bars <= 15:
            raise ValueError("opening_range_bars must be 2..15")
        if type(max_consecutive_losses) is not int or not 1 <= max_consecutive_losses <= 5:
            raise ValueError("max_consecutive_losses must be 1..5")
        self.symbol = symbol.strip()
        self.spec = dict(spec)
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

        self.balance = self.initial_balance
        self.peak_equity = self.initial_balance
        self.max_drawdown_usd = 0.0
        self.position: PaperPosition | None = None
        self.last_evaluated_bar_time: int | None = None
        self.loss_streak = 0
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

    def seed_baseline(self, bars: list[list[float]]) -> int | None:
        """Mark already-closed M1 bars before window start so stale bars do not fire."""
        tail = contiguous_m1_tail(bars)
        if len(tail) >= 2:
            self.last_evaluated_bar_time = int(tail[-2][0])
        return self.last_evaluated_bar_time

    def mark_quote(self, *, bid: float, ask: float, now: float) -> dict[str, Any]:
        """Mark open paper position against live Bid/Ask and auto-close on SL/TP."""
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

        # Conservative tie-break if a quote somehow satisfies both (should never happen).
        exit_reason = "stop_hit" if stop_hit else ("target_hit" if target_hit else None)
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
            }

        self.balance += net_pnl
        if net_pnl < 0:
            self.loss_streak += 1
            if self.loss_streak >= self.max_consecutive_losses:
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
        return {
            "ok": True,
            "paper": True,
            "open": False,
            "closed": True,
            "trade": record,
            "latched": self.latched,
        }

    def on_bars(self, *, bars: list[list[float]], bid: float, ask: float, now: float) -> dict[str, Any]:
        """Evaluate newly closed M1 bars and open at most one paper position."""
        bid = _finite(bid, "bid", minimum=1e-10)
        ask = _finite(ask, "ask", minimum=bid)
        sig = evaluate_m1_direction(
            bars, symbol=self.symbol, opening_range_bars=self.opening_range_bars,
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

        if self.latched:
            self._refuse("loss_latch_active")
            return {"ok": True, "paper": True, "opened": False, "action": action, "reason": "loss_latch_active"}
        if self.position is not None:
            self._refuse("position_already_open")
            return {"ok": True, "paper": True, "opened": False, "action": action, "reason": "position_already_open"}

        plan = plan_paper_trade(
            side=action, bid=bid, ask=ask, range_edge=float(sig["range_edge"]),
            spec=self.spec, stop_usd=self.stop_usd, target_usd=self.target_usd,
            max_lot=self.max_lot, commission_per_lot=self.commission_per_lot,
            max_spread_ratio=self.max_spread_ratio,
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
                "closed_at_window_end": False,
            }

        warnings = [
            "Paper simulation only: zero broker orders were created or submitted.",
            f"A +${self.target_usd:g} target vs -${self.stop_usd:g} stop requires >{breakeven_wr}% win rate to break even before costs.",
            "Ten minutes is a tiny sample and cannot establish long-term profitability.",
            "Real broker fills can experience slippage, requotes and gaps beyond -$10.",
        ]
        if self.commission_per_lot == 0.0:
            warnings.append("Commission is modeled as 0.0 USD/lot (simulation assumption, not verified broker commission).")

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
            },
            "polls": self.polls,
            "m1_bars_evaluated": self.m1_bars_evaluated,
            "signals": {
                "buy": self.signals_buy,
                "sell": self.signals_sell,
                "wait": self.signals_wait,
            },
            "refusal_counts": dict(self.refusal_counts),
            "trades_opened": self._next_trade_id,
            "trades_closed": len(self.closed_trades),
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate_pct": win_rate,
            "required_breakeven_win_rate_pct": breakeven_wr,
            "realized_pnl_usd": realized,
            "open_position_at_end": self.position is not None,
            "open_position": open_summary,
            "unrealized_pnl_usd": unrealized,
            "total_marked_pnl_usd": total_marked,
            "ending_paper_balance_usd": round(self.balance, 4),
            "max_drawdown_usd": round(self.max_drawdown_usd, 4),
            "loss_streak": self.loss_streak,
            "latched": self.latched,
            "trades": sanitized_trades,
            "warnings": warnings,
        }
