"""Position sizing and pre-trade validation. Pure functions, easy to test."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from .config import Config


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def round_to_step(volume: float, step: float, mode: str = "down") -> float:
    if step <= 0:
        return round(volume, 2)
    units = volume / step
    units = math.floor(units + 1e-9) if mode == "down" else round(units)
    return round(units * step, 8)


def normalise_volume(volume: float, info: dict[str, Any]) -> tuple[float, list[str]]:
    """Clamp a volume to the symbol's min / max / step grid."""
    warnings: list[str] = []
    step = _f(info.get("volume_step"), 0.01) or 0.01
    vmin = _f(info.get("volume_min"), 0.01) or 0.01
    vmax = _f(info.get("volume_max"), 100.0) or 100.0
    vol = round_to_step(volume, step, "down")
    if vol < vmin:
        vol = vmin
        warnings.append(f"volume raised to the symbol minimum ({vmin})")
    if vol > vmax:
        vol = vmax
        warnings.append(f"volume capped at the symbol maximum ({vmax})")
    return round(vol, 8), warnings


def loss_per_lot(info: dict[str, Any], stop_distance: float) -> float:
    """Account-currency loss for one lot when price travels ``stop_distance``."""
    tick_size = _f(info.get("trade_tick_size")) or _f(info.get("point"), 0.0)
    tick_value = _f(info.get("trade_tick_value"))
    if tick_size <= 0 or tick_value <= 0:
        contract = _f(info.get("trade_contract_size"), 100000.0)
        return abs(stop_distance) * contract
    return abs(stop_distance) / tick_size * tick_value


def lots_for_risk(
    balance: float,
    risk_percent: float,
    entry: float,
    stop: float,
    info: dict[str, Any],
    max_lot: float | None = None,
) -> dict[str, Any]:
    """Volume such that hitting ``stop`` costs ``risk_percent`` of balance."""
    distance = abs(float(entry) - float(stop))
    risk_amount = max(0.0, float(balance) * float(risk_percent) / 100.0)
    per_lot = loss_per_lot(info, distance)
    warnings: list[str] = []
    if distance <= 0:
        return {
            "ok": False,
            "error": "entry and stop are identical - cannot size the position",
            "volume": 0.0,
        }
    if per_lot <= 0:
        return {"ok": False, "error": "symbol has no tick value information", "volume": 0.0}
    raw = risk_amount / per_lot
    volume, notes = normalise_volume(raw, info)
    warnings.extend(notes)
    if raw < _f(info.get("volume_min"), 0.01):
        warnings.append(
            "risk budget is smaller than one minimum lot - the trade would exceed your risk limit"
        )
    if max_lot and volume > max_lot:
        volume, notes = normalise_volume(max_lot, info)
        warnings.append(f"volume capped by TRADE_MCP_MAX_LOT ({max_lot})")
        warnings.extend(notes)
    return {
        "ok": True,
        "volume": volume,
        "raw_volume": round(raw, 4),
        "risk_amount": round(risk_amount, 2),
        "risk_per_lot": round(per_lot, 2),
        "stop_distance": round(distance, 5),
        "actual_risk": round(volume * per_lot, 2),
        "warnings": warnings,
    }


def reward_risk(entry: float, stop: float, take_profit: float) -> float | None:
    risk = abs(float(entry) - float(stop))
    if risk <= 0:
        return None
    return round(abs(float(take_profit) - float(entry)) / risk, 2)


def min_stop_distance(info: dict[str, Any]) -> float:
    """Broker's minimum distance between price and SL/TP, in price units."""
    point = _f(info.get("point"), 0.0)
    stops_level = _f(info.get("trade_stops_level"))
    spread = _f(info.get("spread_points"), _f(info.get("spread"), 0.0))
    return (max(stops_level, spread) + 2) * point


def validate_order(
    *,
    cfg: Config,
    info: dict[str, Any],
    symbol: str,
    side: str,
    volume: float,
    entry: float,
    stop: float,
    take_profit: float,
    account: dict[str, Any],
    open_positions: Iterable[dict[str, Any]] = (),
    margin_required: float | None = None,
) -> list[dict[str, str]]:
    """Return a list of problems. Empty list == the trade passes all guards."""
    issues: list[dict[str, str]] = []

    def add(severity: str, code: str, message: str) -> None:
        issues.append({"severity": severity, "code": code, "message": message})

    if not cfg.allow_trading:
        add("blocker", "trading_disabled", "TRADE_MCP_ALLOW_TRADING is false - orders cannot be sent")
    if not cfg.is_symbol_allowed(symbol):
        add("blocker", "symbol_not_allowed", f"{symbol} is not in TRADE_MCP_ALLOWED_SYMBOLS")
    if side not in {"buy", "sell"}:
        add("blocker", "bad_side", "side must be 'buy' or 'sell'")

    vmin = _f(info.get("volume_min"), 0.01)
    vmax = _f(info.get("volume_max"), 100.0)
    step = _f(info.get("volume_step"), 0.01) or 0.01
    if volume < vmin:
        add("blocker", "volume_below_min", f"volume {volume} < broker minimum {vmin}")
    if volume > vmax:
        add("blocker", "volume_above_max", f"volume {volume} > broker maximum {vmax}")
    if step > 0 and abs(round(volume / step) * step - volume) > 1e-6:
        add("blocker", "volume_step", f"volume {volume} is not a multiple of the step {step}")
    if volume > cfg.max_lot:
        add("blocker", "max_lot", f"volume {volume} exceeds TRADE_MCP_MAX_LOT {cfg.max_lot}")

    positions = list(open_positions)
    if len(positions) >= cfg.max_open_positions:
        add(
            "blocker",
            "max_positions",
            f"{len(positions)} positions already open (limit {cfg.max_open_positions})",
        )

    spread_points = _f(info.get("spread_points"), _f(info.get("spread"), 0.0))
    if spread_points > cfg.max_spread_points:
        add(
            "blocker",
            "spread_too_wide",
            f"spread {spread_points:.0f} points > limit {cfg.max_spread_points:.0f}",
        )

    digits = int(_f(info.get("digits"), 2))
    if stop:
        distance = abs(entry - stop)
        required = min_stop_distance(info)
        if distance < required:
            add(
                "blocker",
                "stop_too_close",
                f"stop is {distance:.{digits}f} away, broker needs at least {required:.{digits}f}",
            )
        if (side == "buy" and stop >= entry) or (side == "sell" and stop <= entry):
            add("blocker", "stop_wrong_side", "stop loss is on the wrong side of the entry")
    else:
        add("warning", "no_stop", "no stop loss - risk is unbounded")

    if take_profit:
        if (side == "buy" and take_profit <= entry) or (side == "sell" and take_profit >= entry):
            add("blocker", "tp_wrong_side", "take profit is on the wrong side of the entry")
        distance = abs(take_profit - entry)
        if distance < min_stop_distance(info):
            add("blocker", "tp_too_close", "take profit is closer than the broker minimum")
    else:
        add("warning", "no_tp", "no take profit set")

    if stop and entry:
        per_lot = loss_per_lot(info, abs(entry - stop))
        risk = per_lot * volume
        equity = _f(account.get("equity"), _f(account.get("balance"), 0.0))
        if equity > 0:
            risk_pct = risk / equity * 100.0
            if risk_pct > cfg.max_risk_percent + 1e-9:
                add(
                    "blocker",
                    "risk_exceeded",
                    f"the stop would risk {risk_pct:.2f}% of equity "
                    f"(limit {cfg.max_risk_percent:.2f}%)",
                )

    if margin_required is not None:
        free = _f(account.get("margin_free"), 0.0)
        if margin_required > free:
            add(
                "blocker",
                "insufficient_margin",
                f"margin required {margin_required:.2f} > free margin {free:.2f}",
            )

    return issues


def has_blockers(issues: Iterable[dict[str, str]]) -> bool:
    return any(i.get("severity") == "blocker" for i in issues)
