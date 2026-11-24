"""Position sizing and pre-trade validation. Pure functions, easy to test."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from .config import Config, sizing_balance
from .risk_manager import HARD_MAX_RISK_PERCENT


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


def cfd_contract_size_mismatch(info: dict[str, Any]) -> dict[str, Any] | None:
    """Detect USD-profit CFD symbols where trade_contract_size exceeds tick_value/tick_size.

    Example: Alpari MT5 ``XAUUSD_i`` reports ``trade_contract_size=100``,
    ``currency_profit=USD``, ``trade_calc_mode="CFD Leverage"`` while reporting
    ``trade_tick_size=0.01`` and ``trade_tick_value=0.1`` (10x smaller than real
    CFD Leverage P&L). Returns mismatch details when conservative contract_size
    override is required, otherwise None.
    """
    tick_size = _f(info.get("trade_tick_size")) or _f(info.get("point"), 0.0)
    tick_value = _f(info.get("trade_tick_value"))
    contract = _f(info.get("trade_contract_size"))
    currency_profit = str(info.get("currency_profit") or "").strip().upper()
    calc_mode = str(info.get("trade_calc_mode") or "").strip().lower()
    if (
        not all(math.isfinite(v) and v > 0 for v in (tick_size, tick_value, contract))
        or currency_profit != "USD"
    ):
        return None
    raw_per_unit = tick_value / tick_size
    if not math.isfinite(raw_per_unit) or raw_per_unit <= 0:
        return None
    ratio = contract / raw_per_unit
    threshold = 1.5 if "cfd" in calc_mode else 2.0
    if ("cfd" in calc_mode or not calc_mode) and ratio >= threshold:
        return {
            "raw_tick_multiplier": round(raw_per_unit, 6),
            "contract_size": contract,
            "ratio": round(ratio, 4),
            "trade_calc_mode": calc_mode or None,
        }
    return None


def loss_per_lot(info: dict[str, Any], stop_distance: float) -> float:
    """Account-currency loss for one lot when price travels ``stop_distance``."""
    tick_size = _f(info.get("trade_tick_size")) or _f(info.get("point"), 0.0)
    tick_value = _f(info.get("trade_tick_value"))
    if tick_size <= 0 or tick_value <= 0:
        contract = _f(info.get("trade_contract_size"), 100000.0)
        return abs(stop_distance) * contract
    mismatch = cfd_contract_size_mismatch(info)
    if mismatch is not None:
        return abs(stop_distance) * mismatch["contract_size"]
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
    if any(isinstance(v, bool) or v is None or not math.isfinite(float(v)) or float(v) <= 0
           for v in (balance, risk_percent, entry, stop)):
        return {'ok': False, 'error': 'invalid_sizing_inputs', 'volume': 0.0}
    risk_percent = min(float(risk_percent), HARD_MAX_RISK_PERCENT)
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
    if not math.isfinite(per_lot) or per_lot <= 0:
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
    if volume * per_lot > risk_amount + 1e-9:
        return {"ok": False, "volume": 0.0, "risk_amount": risk_amount,
                "actual_risk": 0.0, "warnings": warnings,
                "minimum_volume": _f(info.get("volume_min"), 0.01),
                "minimum_volume_loss": round(_f(info.get("volume_min"), 0.01) * per_lot, 4),
                "risk_per_lot": round(per_lot, 4),
                "stop_distance": round(distance, 5),
                "raw_volume": raw,
                "error": "حداقل حجم بروکر از بودجهٔ ریسک بیشتر است؛ معامله نکنید"}
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
    open_positions = list(open_positions)

    def add(severity: str, code: str, message: str) -> None:
        issues.append({"severity": severity, "code": code, "message": message})

    # Validate before arithmetic/rounding: NaN comparisons must not pass silently.
    numeric = (volume, entry, stop, take_profit, cfg.max_lot, cfg.max_risk_percent)
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) for v in numeric):
        add('blocker', 'nonfinite_order', 'Order and risk limits must be finite numbers')
        return issues
    if volume <= 0 or entry <= 0 or stop <= 0 or take_profit < 0 or cfg.max_lot <= 0 or cfg.max_risk_percent <= 0:
        add('blocker', 'invalid_order_numbers', 'Positive volume, entry, protective stop and limits required')
        return issues
    if cfg.demo_cash_policy:
        if account.get('trade_mode') != 'demo' or account.get('currency') != 'USD':
            add('blocker','usd_demo_required','Cash policy requires a USD demo account')
        cash_loss = loss_per_lot(info,abs(entry-stop))*volume
        if not stop or not math.isfinite(cash_loss) or cash_loss > 10.0+1e-9:
            add('blocker','cash_loss_limit','Planned stop loss must not exceed 10 USD')
        if list(open_positions):
            add('blocker','cash_single_position','Cash policy permits only one open position')
    if not cfg.allow_trading:
        add("blocker", "trading_disabled", "TRADE_MCP_ALLOW_TRADING is false - orders cannot be sent")
    if not cfg.is_symbol_allowed(symbol):
        add("blocker", "symbol_not_allowed", f"{symbol} is not in TRADE_MCP_ALLOWED_SYMBOLS")
    if side not in {"buy", "sell"}:
        add("blocker", "bad_side", "side must be 'buy' or 'sell'")

    vmin = _f(info.get("volume_min"), 0.01)
    vmax = _f(info.get("volume_max"), 100.0)
    step = _f(info.get("volume_step"), 0.01) or 0.01
    if (not all(math.isfinite(v) and v > 0 for v in (vmin, vmax, step)) or vmin > vmax):
        add('blocker', 'invalid_volume_spec', 'Finite positive broker volume limits required')
        return issues
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
    if not math.isfinite(spread_points) or spread_points < 0:
        add('blocker', 'invalid_spread', 'Spread must be finite and nonnegative')
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
        add("blocker", "no_stop", "protective stop loss is mandatory")

    if take_profit:
        if (side == "buy" and take_profit <= entry) or (side == "sell" and take_profit >= entry):
            add("blocker", "tp_wrong_side", "take profit is on the wrong side of the entry")
        distance = abs(take_profit - entry)
        if distance < min_stop_distance(info):
            add("blocker", "tp_too_close", "take profit is closer than the broker minimum")
    else:
        add("warning", "no_tp", "no take profit set")

    if stop and entry:
        mismatch = cfd_contract_size_mismatch(info)
        if mismatch is not None:
            add(
                "warning",
                "cfd_contract_size_override",
                f"Using conservative trade_contract_size={mismatch['contract_size']:g} "
                f"instead of tick_value/tick_size={mismatch['raw_tick_multiplier']:g} "
                f"({mismatch['ratio']:g}x mismatch)",
            )
        per_lot = loss_per_lot(info, abs(entry - stop))
        risk = per_lot * volume
        if not math.isfinite(risk) or per_lot <= 0:
            add('blocker', 'invalid_price_risk', 'Finite positive broker price risk required')
        equity = sizing_balance(account, cfg)
        if equity <= 0:
            add("blocker", "invalid_budget", "sizing budget must be positive")
        if equity > 0:
            risk_pct = risk / equity * 100.0
            if risk_pct > min(cfg.max_risk_percent, HARD_MAX_RISK_PERCENT) + 1e-9:
                add(
                    "blocker",
                    "risk_exceeded",
                    f"the stop would risk {risk_pct:.2f}% of sizing budget "
                    f"(limit {min(cfg.max_risk_percent, HARD_MAX_RISK_PERCENT):.2f}%)",
                )

    if margin_required is not None and (not math.isfinite(float(margin_required)) or margin_required < 0):
        add('blocker', 'invalid_margin', 'Required margin must be finite and nonnegative')
    if margin_required is not None:
        free = _f(account.get("margin_free"), 0.0)
        if not math.isfinite(free) or free < 0:
            add('blocker', 'invalid_free_margin', 'Free margin must be finite and nonnegative')
        if margin_required > free:
            add(
                "blocker",
                "insufficient_margin",
                f"margin required {margin_required:.2f} > free margin {free:.2f}",
            )

    return issues


def has_blockers(issues: Iterable[dict[str, str]]) -> bool:
    return any(i.get("severity") == "blocker" for i in issues)
