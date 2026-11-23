"""Additional bounded, advisory Day Trader adaptations for MT5/M1.

Pattern definitions are adapted from the MIT-licensed day-trader-skill scanner
(revision 3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd; copyright 2026 Jake
Schincariol; see LICENSES/day-trader-skill-MIT.txt). Unlike the source, the bars
here are M1, not US daily bars, and no exchange calendar or network is assumed.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import indicators
from .daytrade import (
    MAX_PRICE,
    MAX_TIMESTAMP,
    MIN_PRICE,
    _integer,
    _number,
    _price,
    _side,
    _valid_levels,
    entry_signal,
    exit_plan,
    m1_rows,
)

_SYMBOL = re.compile(r"[\w.#+/!&() -]{1,64}\Z")
_RULE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
MAX_MARKETS = 50
MAX_SCAN_ROWS = 50_000
MAX_RULES = 100


def _symbol(value: Any) -> str:
    if not isinstance(value, str) or not _SYMBOL.fullmatch(value) or not value.strip():
        raise ValueError("Invalid symbol")
    return value


def _flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def scan_setups(
    markets: Any, *, mode: str = "patterns", side: str = "buy", start_time: int | None = None,
    opening_range_bars: int = 5, cutoff_time: int | None = None,
    min_tick_volume_ratio: float | None = None,
) -> dict[str, Any]:
    """Scan multiple supplied M1 series without fetching data or trading.

    'patterns' adapts the upstream daily close formulas to bars (not days).
    'opening_range' delegates to the same first-closed-bar entry implementation.
    The last row is excluded for both. Invalid symbols are reported individually.
    """
    if mode not in ("patterns", "opening_range"):
        raise ValueError("mode must be patterns or opening_range")
    if not isinstance(markets, list) or not 1 <= len(markets) <= MAX_MARKETS:
        raise ValueError("Provide 1 to 50 markets")
    if mode == "patterns" and (start_time is not None or cutoff_time is not None
                                or min_tick_volume_ratio is not None):
        raise ValueError("start, cutoff and volume filter require opening_range mode")
    if mode == "opening_range" and start_time is None:
        raise ValueError("start_time is required for opening_range mode")
    _side(side)
    seen: set[str] = set()
    total = 0
    for market in markets:
        if not isinstance(market, dict) or set(market) != {"symbol", "ohlcv"}:
            raise ValueError("Each market requires only symbol and ohlcv")
        symbol = _symbol(market["symbol"])
        if symbol in seen:
            raise ValueError("Duplicate symbols are not allowed")
        seen.add(symbol)
        rows = market["ohlcv"]
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("Each market must have at most 1000 M1 rows")
        total += len(rows)
    if total > MAX_SCAN_ROWS:
        raise ValueError("Too many total M1 rows")

    results = []
    for market in markets:
        symbol, raw = market["symbol"], market["ohlcv"]
        try:
            if mode == "opening_range":
                result = entry_signal(raw, side=side, start_time=start_time,
                                      opening_range_bars=opening_range_bars,
                                      cutoff_time=cutoff_time,
                                      min_tick_volume_ratio=min_tick_volume_ratio)
                results.append({"symbol": symbol, **result})
                continue
            rows = m1_rows(raw)
            if len(rows) < 31:
                results.append({"symbol": symbol, "status": "insufficient_history",
                                "closed_bars": max(0, len(rows) - 1), "setups": []})
                continue
            if any(int(b[0] - a[0]) != 60 for a, b in zip(rows, rows[1:], strict=False)):
                raise ValueError("Pattern scan requires contiguous M1 bars; gaps are not filled")
            closed = rows[:-1]
            c = [r[4] for r in closed]
            rsi = indicators.rsi(c, 14)[-1]
            prev_rsi = indicators.rsi(c[:-1], 14)[-1]
            high20 = max(c[-21:-1])
            ma20 = sum(c[-20:]) / 20.0
            ma20_prev = sum(c[-21:-1]) / 20.0
            run = c[-6] / c[-16] - 1
            range5 = (max(c[-5:]) - min(c[-5:])) / c[-1]
            tags = []
            if c[-1] > high20:
                tags.append("breakout_20_bars")
            if run > 0.10 and range5 < 0.05:
                tags.append("bull_flag")
            if c[-2] < ma20_prev and c[-1] > ma20:
                tags.append("reclaim_ma20")
            if rsi is not None and rsi < 30:
                tags.append("oversold")
            if prev_rsi is not None and prev_rsi < 30 and c[-1] > c[-2]:
                tags.append("oversold_bounce")
            results.append({"symbol": symbol, "status": "scanned", "setups": tags,
                            "closed_bars": len(closed), "last_closed_bar_time": int(closed[-1][0]),
                            "last_excluded_bar_time": int(rows[-1][0]), "close": c[-1],
                            "high20_previous_closes": high20, "ma20": ma20,
                            "rsi14": rsi, "previous_rsi14": prev_rsi,
                            "run_10_bars_pct": run * 100, "range_5_bars_pct": range5 * 100})
        except (ValueError, TypeError, OverflowError) as exc:
            results.append({"symbol": symbol, "status": "invalid_data", "error": str(exc)})
    invalid = sum(r["status"] in ("invalid_data", "insufficient_history") for r in results)
    return {
        "ok": True, "executable": False, "execution_authorized": False,
        "mode": mode, "timeframe": "M1", "results": results,
        "counts": {"requested": len(markets), "unusable": invalid,
                   "hits": sum(bool(r.get("setups")) if mode == "patterns"
                               else r.get("status") == "triggered" for r in results)},
        "last_supplied_bar_excluded": True, "quote_freshness_verified": False,
        "warnings": ["The upstream daily formulas are applied to M1 bars, not 20 days.",
                     "A caller-defined range is not an exchange opening time.",
                     "Tick volume is broker-specific activity; data and freshness are not verified."],
    }


def _rule(rule: Any) -> dict[str, Any]:
    if not isinstance(rule, dict):
        raise ValueError("Each alert rule must be an object")
    common = {"id", "type", "rearm"}
    typ = rule.get("type")
    fields = {
        "above": {"symbol", "price", "quote_side"},
        "below": {"symbol", "price", "quote_side"},
        "stop": {"symbol", "price", "side"},
        "target": {"symbol", "price", "side"},
        "pct_move": {"symbol", "reference", "pct", "direction", "quote_side"},
        "time": {"at_time"},
    }
    if not isinstance(typ, str) or typ not in fields or set(rule) - common - fields[typ]:
        raise ValueError("Unknown alert type or field")
    rid = rule.get("id")
    if not isinstance(rid, str) or not _RULE_ID.fullmatch(rid):
        raise ValueError("Alert id must contain 1 to 64 safe characters")
    clean: dict[str, Any] = {"id": rid, "type": typ, "rearm": _flag(rule.get("rearm", False), "rearm")}
    if typ == "time":
        if clean["rearm"]:
            raise ValueError("A time alert cannot rearm")
        clean["at_time"] = _integer(rule.get("at_time"), "at_time", 0, MAX_TIMESTAMP)
        return clean
    clean["symbol"] = _symbol(rule.get("symbol"))
    if typ in ("stop", "target"):
        clean["side"] = _side(rule.get("side"))
        clean["quote_side"] = "bid" if clean["side"] == "buy" else "ask"
    else:
        if rule.get("quote_side") not in ("bid", "ask"):
            raise ValueError("quote_side must explicitly be bid or ask")
        clean["quote_side"] = rule["quote_side"]
    if typ == "pct_move":
        clean["reference"] = _price(rule.get("reference"), "reference")
        clean["pct"] = _number(rule.get("pct"), "pct", minimum=MIN_PRICE, maximum=1e6)
        if rule.get("direction") not in ("up", "down", "either"):
            raise ValueError("direction must be up, down or either")
        clean["direction"] = rule["direction"]
    else:
        clean["price"] = _price(rule.get("price"), "price")
    return clean


def _signature(rule: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(rule, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def evaluate_alerts(*, rules: Any, quotes: Any, now: int, state: Any = None) -> dict[str, Any]:
    """Evaluate a batch with caller-owned state; no polling, notification or orders.

    A rule latches on its first hit. A rearming price rule clears only when a
    *valid* new observation no longer matches. Changing a rule resets its latch.
    A missing quote never clears it. `now` and time rules share a caller-defined
    clock; no NYSE calendar or timezone is inferred.
    """
    stamp = _integer(now, "now", 0, MAX_TIMESTAMP)
    if not isinstance(rules, list) or not 1 <= len(rules) <= MAX_RULES:
        raise ValueError("Provide 1 to 100 alert rules")
    clean_rules = [_rule(r) for r in rules]
    ids = [r["id"] for r in clean_rules]
    if len(set(ids)) != len(ids):
        raise ValueError("Alert ids must be unique")
    if not isinstance(quotes, dict) or len(quotes) > MAX_RULES:
        raise ValueError("quotes must be a symbol-to-quote object")
    for symbol, quote in quotes.items():
        _symbol(symbol)
        if not isinstance(quote, dict) or set(quote) != {"bid", "ask"}:
            raise ValueError("Each quote must contain only bid and ask")
        bid, ask = _price(quote["bid"], "bid"), _price(quote["ask"], "ask")
        if bid > ask:
            raise ValueError("bid cannot exceed ask")
    if state is None:
        saved: dict[str, Any] = {}
    else:
        if (not isinstance(state, dict) or set(state) != {"version", "updated_at", "rules"}
                or type(state.get("version")) is not int or state["version"] != 1
                or not isinstance(state.get("rules"), dict) or len(state["rules"]) > MAX_RULES):
            raise ValueError("Invalid alert state")
        previous = _integer(state["updated_at"], "state.updated_at", 0, MAX_TIMESTAMP)
        if stamp < previous:
            raise ValueError("Alert evaluations must not move backwards in time")
        saved = state["rules"]
        for key, item in saved.items():
            if (not isinstance(key, str) or not _RULE_ID.fullmatch(key) or not isinstance(item, dict)
                    or set(item) != {"signature", "latched"}
                    or not isinstance(item["signature"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["signature"])
                    or not isinstance(item["latched"], bool)):
                raise ValueError("Invalid alert state entry")
    next_rules, events, results = {}, [], []
    for rule in clean_rules:
        rid, typ = rule["id"], rule["type"]
        signature = _signature(rule)
        old = saved.get(rid, {})
        changed = bool(old and old["signature"] != signature)
        latched = bool(old.get("latched")) if not changed else False
        hit = None
        observed = None
        if typ == "time":
            hit = stamp >= rule["at_time"]
        elif rule["symbol"] in quotes:
            observed = quotes[rule["symbol"]][rule["quote_side"]]
            if typ == "pct_move":
                move = (observed / rule["reference"] - 1) * 100
                direction = rule["direction"]
                hit = (move >= rule["pct"] if direction == "up" else
                        move <= -rule["pct"] if direction == "down" else abs(move) >= rule["pct"])
            elif typ in ("above", "below"):
                hit = observed >= rule["price"] if typ == "above" else observed <= rule["price"]
            else:
                lower = (typ == "stop") == (rule["side"] == "buy")
                hit = observed <= rule["price"] if lower else observed >= rule["price"]
        fired = bool(hit and not latched)
        if fired:
            latched = True
            events.append({"id": rid, "type": typ, "symbol": rule.get("symbol"),
                           "observed_price": observed, "quote_side": rule.get("quote_side"),
                           "observed_at": stamp})
        elif hit is False and rule["rearm"]:
            latched = False
        next_rules[rid] = {"signature": signature, "latched": latched}
        results.append({"id": rid, "status": "unavailable" if hit is None else
                        "fired" if fired else "latched" if hit and latched else "clear",
                        "matched": hit, "latched": latched, "rule_changed": changed})
    return {"ok": True, "executable": False, "execution_authorized": False,
            "events": events, "results": results,
            "state": {"version": 1, "updated_at": stamp, "rules": next_rules},
            "quote_freshness_verified": False, "automatic_monitoring": False,
            "warning": "Caller-owned state and supplied observations only; no notification, fill or order."}


def review_limits(
    *, side: str, entry: float, stop: float, target: float, sizing: Any, limits: Any, cfg: Any,
) -> dict[str, Any]:
    """Review hypothetical sizing and optional daily limits; never a risk gate.

    The daily risk consumed is the sum of planned stop risks of earlier entries,
    not realised P&L. Missing observations remain unknown, never assumed zero.
    """
    side = _side(side)
    entry, stop, target = _valid_levels(side, entry, stop, target)
    if not isinstance(sizing, dict) or cfg is None:
        raise ValueError("sizing and configuration are required")
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    fields = ("max_loss_per_trade", "daily_risk_cap", "risk_used_today", "trades_today",
              "max_trades_per_day", "min_net_reward_risk")
    if set(limits) - set(fields):
        raise ValueError("Unknown review limit")
    clean = {}
    for key in fields:
        value = limits.get(key)
        if value is None:
            clean[key] = None
        elif key in ("trades_today", "max_trades_per_day"):
            clean[key] = _integer(value, key, 0 if key == "trades_today" else 1, 100000)
        else:
            clean[key] = _number(value, key, minimum=0 if key == "risk_used_today" else MIN_PRICE,
                                 maximum=MAX_PRICE)
    plan = exit_plan(side=side, entry=entry, stop_rule="price", stop_value=stop,
                     target_r=None, target_price=target, sizing=sizing, cfg=cfg)
    estimate = plan["sizing"]
    loss = estimate.get("estimated_stop_loss")
    profit = estimate.get("estimated_target_profit")
    net_rr = profit / loss if loss is not None and loss > 0 and profit is not None else None
    blockers = list(estimate.get("blockers", []))
    if loss is not None and clean["max_loss_per_trade"] is not None and loss > clean["max_loss_per_trade"]:
        blockers.append("estimated stop loss exceeds the supplied per-trade limit")
    if (loss is not None and clean["daily_risk_cap"] is not None and clean["risk_used_today"] is not None
            and loss + clean["risk_used_today"] > clean["daily_risk_cap"]):
        blockers.append("estimated stop loss plus prior committed risk exceeds the supplied daily cap")
    if (clean["trades_today"] is not None and clean["max_trades_per_day"] is not None
            and clean["trades_today"] >= clean["max_trades_per_day"]):
        blockers.append("the supplied daily entry count is at its limit")
    if net_rr is not None and clean["min_net_reward_risk"] is not None and net_rr < clean["min_net_reward_risk"]:
        blockers.append("estimated net reward/risk is below the supplied minimum")
    missing = [k for k, value in clean.items() if value is None]
    if estimate["status"] == "costs_unknown":
        missing.extend(estimate.get("missing_costs", []))
    status = "blocked" if blockers else "incomplete" if missing else "advisory_only"
    return {"ok": True, "executable": False, "execution_authorized": False,
            "status": status, "plan": plan, "estimated_net_reward_risk": net_rr,
            "limits": clean, "blockers": blockers, "missing": missing,
            "warning": "User-supplied estimates only, not a broker risk check or human approval. "
                       "Use propose_order then explicit confirmation; losses can exceed a stop."}
