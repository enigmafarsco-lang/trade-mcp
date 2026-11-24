"""Offline M1 entry, exit planning, sizing estimates and snapshot alerts.

The caller supplies all prices and bars. None of these functions reads a broker,
creates a proposal, calls a model, or authorizes execution. See
``docs/DAY-TRADER-M1-FA.md`` for provenance, input contracts and limitations.
"""
from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from .engines.technical import validate_ohlcv
from .risk import cfd_contract_size_mismatch, loss_per_lot
from .risk_manager import HARD_MAX_RISK_PERCENT
from .training import risk_plan

M1_SECONDS = 60
MAX_ROWS = 10000
MAX_PRICE = 1e12
MIN_PRICE = 1e-10
MAX_TIMESTAMP = 2**53 - 60  # exact in a browser's JSON number representation


def _number(value: Any, name: str, *, minimum: float | None = None,
            maximum: float | None = None) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be finite") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} is below the allowed minimum")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} is above the allowed maximum")
    return number


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    number = _number(value, name, minimum=minimum, maximum=maximum)
    if not number.is_integer():
        raise ValueError(f"{name} must be an integer")
    return int(number)


def _timestamp(value: Any, name: str) -> int:
    stamp = _integer(value, name, 0, MAX_TIMESTAMP)
    if stamp % M1_SECONDS:
        raise ValueError(f"{name} must be on the M1 minute grid")
    return stamp


def _price(value: Any, name: str) -> float:
    return _number(value, name, minimum=MIN_PRICE, maximum=MAX_PRICE)


def _side(value: Any) -> str:
    if value not in ("buy", "sell"):
        raise ValueError("side must be buy or sell")
    return value


def m1_rows(rows: Any) -> list[list[float]]:
    """Validate compact MT5 6/7-column rows without assuming a timezone.

    A seventh spread-points column is checked but not used in OHLC calculations.
    Closedness is dealt with separately by each operation.
    """
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError("Provide 1 to 10000 M1 OHLCV rows")
    compact = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) not in (6, 7):
            raise ValueError("Each row must be [time, open, high, low, close, tick_volume, optional_spread]")
        _timestamp(row[0], "bar time")
        for value in row[1:5]:
            _price(value, "OHLC price")
        _number(row[5], "tick volume", minimum=0, maximum=1e15)
        if len(row) == 7 and row[6] is not None:
            _number(row[6], "spread points", minimum=0, maximum=1e12)
        compact.append(list(row[:6]))
    # Shared project validation enforces chronological order, uniqueness and OHLC bounds.
    return validate_ohlcv(compact)


def entry_signal(
    ohlcv: Any, *, side: str, start_time: int, setup: str = "opening_range",
    opening_range_bars: int = 5, level: float | None = None,
    cutoff_time: int | None = None, min_tick_volume_ratio: float | None = None,
) -> dict[str, Any]:
    """First closed M1 bar beyond a caller-defined range or level.

    The final supplied bar is always excluded: a later timestamp is the evidence
    that an earlier bar's interval has ended. No live freshness is inferred.
    """
    side = _side(side)
    start = _timestamp(start_time, "start_time")
    if setup not in ("opening_range", "level"):
        raise ValueError("setup must be opening_range or level")
    n_range = _integer(opening_range_bars, "opening_range_bars", 1, 240)
    if setup == "level":
        level = _price(level, "level")
        if min_tick_volume_ratio is not None:
            raise ValueError("tick-volume filter requires an opening range")
    elif level is not None:
        raise ValueError("level is only used for the level setup")
    if min_tick_volume_ratio is not None:
        min_tick_volume_ratio = _number(min_tick_volume_ratio, "min_tick_volume_ratio",
                                        minimum=0.000001, maximum=1000)
    cutoff = None if cutoff_time is None else _timestamp(cutoff_time, "cutoff_time")
    if cutoff is not None and cutoff < start + (n_range + 1 if setup == "opening_range" else 1) * 60:
        raise ValueError("cutoff_time must allow at least one eligible bar close")

    data = m1_rows(ohlcv)
    if len(data) < 2:
        raise ValueError("At least two bars are required; the latest is excluded")
    closed = data[:-1]
    latest_label = int(data[-1][0])
    result: dict[str, Any] = {
        "ok": True, "executable": False, "execution_authorized": False,
        "timeframe": "M1", "side": side, "setup": setup, "start_time": start,
        "cutoff_time": cutoff, "bars_supplied": len(data),
        "last_supplied_bar_excluded": True, "last_excluded_bar_time": latest_label,
        "last_closed_bar_time": int(closed[-1][0]),
        "freshness_validated_for_execution": False,
        "time_basis": "caller-supplied broker labels; UTC is not inferred",
        "price_basis": "caller-supplied OHLC; a close is not an executable quote",
        "warnings": ["Signals are advisory only; M1 is not an automatic exit timer.",
                     "A later bar establishes interval closure, not current-feed freshness.",
                     "Tick volume is broker-specific activity, not guaranteed traded volume."],
    }
    if start > latest_label:
        return {**result, "status": "waiting_for_start", "trigger": None}
    if start < int(data[0][0]):
        raise ValueError("start_time precedes the supplied history")

    # Do not invent a first trigger across missing bars. Bars outside the selected
    # interval need not be contiguous; the examined interval must be complete.
    end = min(latest_label, cutoff) if cutoff is not None else latest_label
    indexed = {int(row[0]): row for row in closed}
    if max(0, end - start) // M1_SECONDS > len(closed):
        raise ValueError("A required M1 bar is missing from the selected interval")
    expected = range(start, max(start, end), M1_SECONDS)
    window = []
    for stamp in expected:
        if stamp not in indexed:
            raise ValueError("A required M1 bar is missing from the selected interval")
        window.append(indexed[stamp])

    if setup == "opening_range":
        range_rows = window[:n_range]
        if len(range_rows) < n_range:
            return {**result, "status": "range_forming", "trigger": None,
                    "opening_range": {"bars": len(range_rows), "required_bars": n_range,
                                      "complete": False}}
        high = max(row[2] for row in range_rows)
        low = min(row[3] for row in range_rows)
        average_volume = sum(row[5] for row in range_rows) / n_range
        if min_tick_volume_ratio is not None and average_volume <= 0:
            raise ValueError("Opening-range tick volume is zero; volume filter cannot be evaluated")
        level = high if side == "buy" else low
        result["opening_range"] = {"high": high, "low": low, "bars": n_range,
                                   "complete": True, "average_tick_volume": average_volume}
        candidates = window[n_range:]
    else:
        average_volume = None
        candidates = window
    result["level"] = level
    volume_rejections = 0
    for row in candidates:
        passed_price = row[4] > level if side == "buy" else row[4] < level
        if not passed_price:
            continue
        ratio = row[5] / average_volume if average_volume else None
        if min_tick_volume_ratio is not None and (ratio is None or ratio < min_tick_volume_ratio):
            volume_rejections += 1
            continue
        stamp = int(row[0])
        return {**result, "status": "triggered", "volume_rejections": volume_rejections,
                "trigger": {"bar_start": stamp, "bar_close_time": stamp + M1_SECONDS,
                            "slot": (stamp - start) // M1_SECONDS, "close": row[4],
                            "tick_volume": row[5], "tick_volume_ratio": ratio},
                "closed_bars_since_trigger": sum(1 for later in closed if later[0] > stamp),
                "trigger_is_latest_closed_bar": stamp == int(closed[-1][0])}
    expired = cutoff is not None and latest_label >= cutoff
    return {**result, "status": "expired" if expired else "waiting", "trigger": None,
            "volume_rejections": volume_rejections}


def _d(value: float | int) -> Decimal:
    return Decimal(str(value))


def _on_grid(value: float, step: float) -> bool:
    return _d(value) % _d(step) == 0


def _grid(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


def _valid_levels(side: str, entry: float, stop: float, target: float) -> tuple[float, float, float]:
    side = _side(side)
    entry, stop, target = _price(entry, "entry"), _price(stop, "stop"), _price(target, "target")
    if (side == "buy" and not stop < entry < target) or (
            side == "sell" and not target < entry < stop):
        raise ValueError("Stop and target must be on opposite, correct sides of entry")
    return entry, stop, target


def exit_plan(
    *, side: str, entry: float, stop_rule: str = "price", stop_value: float | None = None,
    target_r: float | None = 1.5, target_price: float | None = None,
    tick_size: float | None = None, range_high: float | None = None,
    range_low: float | None = None, atr_value: float | None = None,
    sizing: dict[str, Any] | None = None, cfg: Any = None,
) -> dict[str, Any]:
    """Build a hypothetical bracket, optionally with a cost-aware sizing estimate."""
    side = _side(side)
    entry = _price(entry, "entry")
    e = _d(entry)
    direction = Decimal(1 if side == "buy" else -1)
    if stop_rule == "price":
        raw_stop = _d(_price(stop_value, "stop_value"))
    elif stop_rule in ("distance", "percent", "atr"):
        amount = _number(stop_value, "stop_value", minimum=MIN_PRICE, maximum=MAX_PRICE)
        if stop_rule == "percent":
            if amount >= 100:
                raise ValueError("stop percentage must be below 100")
            distance = e * _d(amount) / 100
        elif stop_rule == "atr":
            distance = _d(_price(atr_value, "atr_value")) * _d(amount)
        else:
            distance = _d(amount)
        raw_stop = e - direction * distance
    elif stop_rule in ("range_edge", "range_mid"):
        high, low = _price(range_high, "range_high"), _price(range_low, "range_low")
        if high < low:
            raise ValueError("range_high cannot be below range_low")
        raw_stop = (_d(high) + _d(low)) / 2 if stop_rule == "range_mid" else _d(
            low if side == "buy" else high)
    else:
        raise ValueError("Unknown stop_rule")

    step = None if tick_size is None else _price(tick_size, "tick_size")
    # Round stops away from the entry and targets toward it, never inflating R.
    stop = raw_stop if step is None else _grid(
        raw_stop, _d(step), ROUND_FLOOR if side == "buy" else ROUND_CEILING)
    if stop <= 0 or direction * (e - stop) <= 0:
        raise ValueError("Stop must be positive and on the correct side of entry")
    risk = direction * (e - stop)
    if target_price is not None:
        if target_r is not None:
            raise ValueError("Specify target_price or target_r, not both")
        raw_target = _d(_price(target_price, "target_price"))
        target_rule = "price"
    else:
        multiple = _number(target_r, "target_r", minimum=MIN_PRICE, maximum=1000)
        raw_target = e + direction * risk * _d(multiple)
        target_rule = "r_multiple"
    target = raw_target if step is None else _grid(
        raw_target, _d(step), ROUND_FLOOR if side == "buy" else ROUND_CEILING)
    entry_f, stop_f, target_f = _valid_levels(side, entry, float(stop), float(target))
    reward = direction * (target - e)
    plan: dict[str, Any] = {
        "ok": True, "executable": False, "execution_authorized": False,
        "side": side, "entry": entry_f, "stop": stop_f, "target": target_f,
        "stop_rule": stop_rule, "target_rule": target_rule, "tick_size": step,
        "risk_distance": float(risk), "reward_distance": float(reward),
        "gross_reward_risk": float(reward / risk),
        "target_r_requested": target_r if target_rule == "r_multiple" else None,
        "sizing": None,
        "warnings": ["The entry is a supplied hypothetical price, not a verified fill.",
                     "Broker, account, spread, margin and current quote are not checked.",
                     "Stops can fill worse than planned; M1 does not force a one-minute exit."],
    }
    if sizing is not None:
        plan["sizing"] = sizing_preview(plan, sizing, cfg)
    return plan


def sizing_preview(plan: dict[str, Any], sizing: Any, cfg: Any) -> dict[str, Any]:
    """Use the existing training risk arithmetic with conservative config caps.

    The budget and spec are assertions supplied by the caller, not live account
    checks. Unknown costs intentionally prevent a volume estimate.
    """
    if not isinstance(sizing, dict) or cfg is None:
        raise ValueError("sizing requires a configuration and a sizing object")
    allowed = {"spec", "budget", "risk_percent", "commission_per_lot", "slippage_price"}
    if set(sizing) - allowed:
        raise ValueError("Unknown sizing field")
    spec = sizing.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("sizing.spec must be a symbol specification")
    names = ("trade_tick_size", "trade_tick_value", "volume_step", "volume_min", "volume_max", "point")
    clean = {name: _number(spec.get(name), name, minimum=MIN_PRICE, maximum=MAX_PRICE)
             for name in names}
    clean["trade_stops_level"] = _number(spec.get("trade_stops_level"), "trade_stops_level",
                                          minimum=0, maximum=1e12)
    if spec.get("trade_contract_size") is not None:
        clean["trade_contract_size"] = _number(spec.get("trade_contract_size"), "trade_contract_size",
                                               minimum=MIN_PRICE, maximum=MAX_PRICE)
    if spec.get("currency_profit") is not None:
        clean["currency_profit"] = str(spec.get("currency_profit")).strip()
    if spec.get("trade_calc_mode") is not None:
        clean["trade_calc_mode"] = str(spec.get("trade_calc_mode")).strip()
    if clean["volume_min"] > clean["volume_max"] or not _on_grid(
            clean["volume_min"], clean["volume_step"]):
        raise ValueError("Inconsistent broker volume minimum, maximum or step")
    mismatch = cfd_contract_size_mismatch(clean)
    per_unit_multiplier = loss_per_lot(clean, 1.0)
    effective_spec = dict(clean)
    if mismatch is not None:
        effective_spec["trade_tick_value"] = per_unit_multiplier * clean["trade_tick_size"]
    budget = _number(sizing.get("budget"), "budget", minimum=MIN_PRICE, maximum=MAX_PRICE)
    requested = _number(sizing.get("risk_percent"), "risk_percent", minimum=MIN_PRICE, maximum=100)
    max_risk = _number(cfg.max_risk_percent, "configured max risk", minimum=MIN_PRICE, maximum=100)
    max_lot = _number(cfg.max_lot, "configured max lot", minimum=MIN_PRICE, maximum=MAX_PRICE)
    practice_cap = _number(cfg.training_budget, "configured training budget", minimum=MIN_PRICE,
                           maximum=MAX_PRICE)
    applied = min(requested, max_risk, HARD_MAX_RISK_PERCENT)
    effective_budget = min(budget, practice_cap)
    if cfg.demo_cash_policy:
        # Additional cap, not a claim that the account is actually USD/demo.
        effective_budget = min(effective_budget, 10.0 * 100.0 / applied)
    money_limit = effective_budget * applied / 100.0
    gross_min_loss = loss_per_lot(clean, plan["risk_distance"]) * clean["volume_min"]
    sizing_warnings = [
        "No equity, margin, account type, current spread or order guards were checked.",
        "The supplied commission and slippage are assumptions, not observed costs.",
        "Entry is assumed already priced on the executable side; financing/swap is omitted.",
    ]
    if mismatch is not None:
        sizing_warnings.append(
            f"Conservative CFD contract_size override active: using {mismatch['contract_size']:g} "
            f"instead of raw tick_value/tick_size={mismatch['raw_tick_multiplier']:g}."
        )
    result: dict[str, Any] = {
        "executable": False, "execution_authorized": False,
        "requested_budget": budget, "hypothetical_budget_after_caps": effective_budget,
        "requested_risk_percent": requested, "applied_risk_percent": applied,
        "hypothetical_risk_limit": money_limit, "configured_max_lot": max_lot,
        "gross_loss_at_stop_for_minimum_lot": gross_min_loss,
        "account_currency": "not verified", "account_and_broker_validated": False,
        "warnings": sizing_warnings,
    }
    missing = [name for name in ("commission_per_lot", "slippage_price") if sizing.get(name) is None]
    if missing:
        return {**result, "status": "costs_unknown", "missing_costs": missing,
                "volume": None, "estimated_stop_loss": None, "estimated_target_profit": None}
    commission = _number(sizing["commission_per_lot"], "commission_per_lot", minimum=0,
                         maximum=MAX_PRICE)
    slip = _number(sizing["slippage_price"], "slippage_price", minimum=0, maximum=MAX_PRICE)
    calculated = risk_plan(spec=effective_spec, budget=effective_budget, risk_percent=applied,
                           distance=plan["risk_distance"], target=plan["reward_distance"],
                           commission=commission, slippage=slip)
    cap = float(_grid(_d(min(max_lot, clean["volume_max"])),
                      _d(clean["volume_step"]), ROUND_FLOOR))
    candidate = min(calculated["volume"], cap)
    blockers = list(calculated["blockers"])
    if candidate < clean["volume_min"]:
        candidate = 0.0
        blockers.append("Configured lot cap or risk budget is below the broker minimum.")
    if not _on_grid(plan["stop"], clean["trade_tick_size"]) or not _on_grid(
            plan["target"], clean["trade_tick_size"]):
        blockers.append("Stop or target is not on the supplied broker tick-size grid.")
    if plan["reward_distance"] < clean["trade_stops_level"] * clean["point"]:
        blockers.append("Target is closer than the supplied broker stop distance.")
    per_lot_loss = (plan["risk_distance"] + slip) * per_unit_multiplier + commission
    per_lot_profit = (plan["reward_distance"] - slip) * per_unit_multiplier - commission
    if candidate * per_lot_loss > money_limit + 1e-8:
        blockers.append("Estimated loss exceeds the hypothetical risk limit.")
    volume = candidate if not blockers else 0.0
    return {**result, "status": "blocked" if blockers else "estimate_only", "volume": volume,
            "estimated_stop_loss": volume * per_lot_loss,
            "estimated_target_profit": volume * per_lot_profit,
            "commission_per_lot_assumed": commission, "slippage_price_assumed": slip,
            "blockers": blockers}


def check_alert(*, side: str, stop: float, target: float, bid: float, ask: float) -> dict[str, Any]:
    """Check a supplied quote once; this does not monitor or place a broker order."""
    side = _side(side)
    stop, target, bid, ask = (_price(stop, "stop"), _price(target, "target"),
                               _price(bid, "bid"), _price(ask, "ask"))
    if bid > ask:
        raise ValueError("bid cannot exceed ask")
    if (side == "buy" and stop >= target) or (side == "sell" and stop <= target):
        raise ValueError("Stop and target are not correctly ordered")
    price = bid if side == "buy" else ask
    hit_stop = price <= stop if side == "buy" else price >= stop
    hit_target = price >= target if side == "buy" else price <= target
    return {"ok": True, "executable": False, "execution_authorized": False,
            "status": "stop_touched" if hit_stop else "target_touched" if hit_target else "neither",
            "observed_exit_side": "bid" if side == "buy" else "ask", "observed_price": price,
            "quote_freshness_verified": False, "automatic_monitoring": False,
            "warning": "One supplied snapshot only: no notification, broker stop, fill or order is implied."}


def run_request(operation: str, payload: Any, cfg: Any = None) -> dict[str, Any]:
    """Shared whitelisted dispatcher for the console and MCP adapters."""
    if not isinstance(payload, dict):
        raise ValueError("A JSON object is required")
    from .daytrade_extra import evaluate_alerts, review_limits, scan_setups
    from .daytrade_sim import historical_replay, time_machine
    from .daytrade_sources import parse_news, stock_disclosures, stock_facts

    handlers = {"entry": entry_signal, "plan": exit_plan, "history": historical_replay,
                "time-machine": time_machine, "alerts": check_alert, "scan": scan_setups,
                "alert-rules": evaluate_alerts, "review": review_limits,
                "news": parse_news, "stock-facts": stock_facts,
                "stock-disclosures": stock_disclosures}
    fields = {
        "entry": {"ohlcv", "side", "start_time", "setup", "opening_range_bars", "level",
                  "cutoff_time", "min_tick_volume_ratio"},
        "plan": {"side", "entry", "stop_rule", "stop_value", "target_r", "target_price",
                 "tick_size", "range_high", "range_low", "atr_value", "sizing"},
        "history": {"sessions", "decision_time", "side", "opening_range_bars", "stop_rule",
                    "stop_value", "target_r", "tick_size", "horizon_bars", "cutoff_slot"},
        "time-machine": {"sessions", "decision_time", "side", "entry", "stop", "target",
                         "entry_slot", "horizon_bars", "replays", "seed", "window"},
        "alerts": {"side", "stop", "target", "bid", "ask"},
        "scan": {"markets", "mode", "side", "start_time", "opening_range_bars", "cutoff_time",
                 "min_tick_volume_ratio"},
        "alert-rules": {"rules", "quotes", "now", "state"},
        "review": {"side", "entry", "stop", "target", "sizing", "limits"},
        "news": {"feeds", "watch", "now", "hours", "seen_ids", "show_all", "market", "moves"},
        "stock-facts": {"companyfacts", "quarters", "as_of"},
        "stock-disclosures": {"kind", "text", "year", "as_of", "members", "since", "seen_ids",
                              "filed", "new_only"},
    }
    required = {
        "entry": {"ohlcv", "side", "start_time"},
        "plan": {"side", "entry"},
        "history": {"sessions", "decision_time", "side"},
        "time-machine": {"sessions", "decision_time", "side", "entry", "stop", "target",
                         "entry_slot", "horizon_bars"},
        "alerts": {"side", "stop", "target", "bid", "ask"},
        "scan": {"markets"},
        "alert-rules": {"rules", "quotes", "now"},
        "review": {"side", "entry", "stop", "target", "sizing", "limits"},
        "news": {"feeds", "watch", "now"},
        "stock-facts": {"companyfacts"},
        "stock-disclosures": {"kind", "text"},
    }
    if operation not in handlers:
        raise ValueError("Unknown day-trader operation")
    if set(payload) - fields[operation] or required[operation] - set(payload):
        raise ValueError("Missing or unknown day-trader fields")
    arguments = dict(payload)
    if operation in ("plan", "review"):
        arguments["cfg"] = cfg
    return handlers[operation](**arguments)
