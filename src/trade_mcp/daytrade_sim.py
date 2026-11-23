"""Offline historical M1 breakout replay and seeded, slot-based bootstrap.

Adapted for caller-defined M1 segments from the ideas in Jake Schincariol's
MIT-licensed day-trader-skill Time Machine. This is not the upstream NYSE/Yahoo
program and does not make a claim about the probability of real executions.
"""
from __future__ import annotations

import math
import random
import statistics
from typing import Any

from . import indicators
from .daytrade import (
    M1_SECONDS,
    _integer,
    _number,
    _price,
    _side,
    _timestamp,
    _valid_levels,
    exit_plan,
    m1_rows,
)

MIN_BOOTSTRAP_SESSIONS = 20
MAX_SESSIONS = 100
MAX_SESSION_ROWS = 1000
MAX_TOTAL_ROWS = 50000
MAX_HORIZON = 300
MAX_REPLAY_STEPS = 2_000_000
MAX_BAND_PATHS = 200


def _sessions(value: Any, decision_time: Any, *, minimum: int) -> tuple[list[list[list[float]]], int]:
    """Require disjoint, equal-length, contiguous, strictly historical segments."""
    decision = _timestamp(decision_time, "decision_time")
    if not isinstance(value, list) or not minimum <= len(value) <= MAX_SESSIONS:
        raise ValueError(f"Provide {minimum} to {MAX_SESSIONS} historical segments")
    lengths = [len(session) if isinstance(session, list) else 0 for session in value]
    if not lengths or min(lengths) < 2 or max(lengths) > MAX_SESSION_ROWS:
        raise ValueError("Each historical segment must contain 2 to 1000 bars")
    if len(set(lengths)) != 1 or sum(lengths) > MAX_TOTAL_ROWS:
        raise ValueError("Historical segments must have equal lengths within the total row limit")
    parsed = []
    previous_end = None
    for session in value:
        rows = m1_rows(session)
        stamps = [int(row[0]) for row in rows]
        if any(b - a != M1_SECONDS for a, b in zip(stamps, stamps[1:], strict=False)):
            raise ValueError("Historical segments must be contiguous; gaps are not filled")
        if previous_end is not None and stamps[0] < previous_end:
            raise ValueError("Historical segments must be ordered and non-overlapping")
        end = stamps[-1] + M1_SECONDS
        if end > decision:
            raise ValueError("Every historical bar must close no later than decision_time")
        previous_end = end
        parsed.append(rows)
    return parsed, decision


def _bar_exit(side: str, bar: list[float], stop: float, target: float) -> tuple[str, float, bool, bool] | None:
    """Open occurs first; otherwise an intrabar stop/target tie resolves to stop.

    A gap through the stop fills at the worse opening price. A target gap is
    filled at the target, not optimistically at the opening price.
    """
    _, op, high, low, _, _ = bar
    if side == "buy":
        if op <= stop:
            return "stop", op, True, False
        if op >= target:
            return "target", target, False, False
        if low <= stop:
            return "stop", stop, False, high >= target
        if high >= target:
            return "target", target, False, False
    else:
        if op >= stop:
            return "stop", op, True, False
        if op <= target:
            return "target", target, False, False
        if high >= stop:
            return "stop", stop, False, low <= target
        if low <= target:
            return "target", target, False, False
    return None


def _r(side: str, entry: float, stop: float, price: float) -> float:
    value = (price - entry if side == "buy" else entry - price) / abs(entry - stop)
    if not math.isfinite(value):
        raise ValueError("Simulated price or R became non-finite")
    return value


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * q)]


def time_machine(
    sessions: Any, *, decision_time: int, side: str, entry: float, stop: float, target: float,
    entry_slot: int, horizon_bars: int, replays: int = 1000, seed: int = 19, window: int = 2,
) -> dict[str, Any]:
    """Recombine prior same-slot OHLC ratios; summarize simulated paths, not odds."""
    side = _side(side)
    entry, stop, target = _valid_levels(side, entry, stop, target)
    data, decision = _sessions(sessions, decision_time, minimum=MIN_BOOTSTRAP_SESSIONS)
    slots = len(data[0])
    entry_slot = _integer(entry_slot, "entry_slot", 0, slots - 2)
    horizon = _integer(horizon_bars, "horizon_bars", 1, min(MAX_HORIZON, slots - 1 - entry_slot))
    count = _integer(replays, "replays", 1, 10000)
    seed = _integer(seed, "seed", 0, 2**32 - 1)
    window = _integer(window, "window", 0, min(20, slots - 1))
    if count * horizon > MAX_REPLAY_STEPS:
        raise ValueError("replays multiplied by horizon exceeds the calculation limit")

    # Each historical bar is normalized by the preceding close in the same
    # segment. There is intentionally no invented prior close for slot zero.
    pool: list[list[tuple[float, float, float, float]]] = [[] for _ in range(slots)]
    for rows in data:
        for i in range(1, slots):
            previous = rows[i - 1][4]
            ratios = tuple(rows[i][j] / previous for j in (1, 2, 3, 4))
            if any(not math.isfinite(x) or x <= 0 for x in ratios):
                raise ValueError("Historical OHLC ratios must be finite and positive")
            pool[i].append(ratios)

    rng = random.Random(seed)
    counts = {"target": 0, "stop": 0, "open": 0}
    gap_stops = ambiguous_stops = 0
    path_closes: list[float] = []
    realized_r: list[float] = []
    open_r: list[float] = []
    band_columns: list[list[float]] = [[] for _ in range(horizon + 1)]
    for run in range(count):
        price = entry
        outcome = None
        trade_r = None
        if run < MAX_BAND_PATHS:
            band_columns[0].append(price)
        for step in range(1, horizon + 1):
            slot = entry_slot + step
            selected = slot if window == 0 else min(slots - 1, max(1, slot + rng.randint(-window, window)))
            ratios = rng.choice(pool[selected])
            op, high, low, close = (price * ratio for ratio in ratios)
            if not all(math.isfinite(x) and x > 0 for x in (op, high, low, close)):
                raise ValueError("A simulated price became non-finite or non-positive")
            if outcome is None:
                event = _bar_exit(side, [0, op, high, low, close, 0], stop, target)
                if event is not None:
                    outcome, fill, gap, ambiguous = event
                    trade_r = _r(side, entry, stop, fill)
                    gap_stops += int(gap)
                    ambiguous_stops += int(ambiguous)
            price = close
            if run < MAX_BAND_PATHS:
                band_columns[step].append(price)
        path_closes.append(price)
        if outcome is None:
            counts["open"] += 1
            open_r.append(_r(side, entry, stop, price))
        else:
            counts[outcome] += 1
            # trade_r was set together with outcome.
            assert trade_r is not None
            realized_r.append(trade_r)

    marked = realized_r + open_r
    bands = [{"step": step, "p05": _percentile(col, .05),
              "p50": _percentile(col, .5), "p95": _percentile(col, .95)}
             for step, col in enumerate(band_columns)]
    return {
        "ok": True, "executable": False, "execution_authorized": False,
        "method": "same-slot historical OHLC ratio bootstrap, with slot jitter",
        "decision_time": decision, "time_basis": "caller-supplied broker labels; UTC is not inferred",
        "historical_segments": len(data), "bars_per_segment": slots,
        "entry_slot": entry_slot, "horizon_bars": horizon, "seed": seed,
        "window": window, "replays": count, "counts": counts,
        "fractions": {key: value / count for key, value in counts.items()},
        "gap_stop_count": gap_stops, "ambiguous_bar_stop_first_count": ambiguous_stops,
        "mean_realized_gross_r_closed_only": statistics.fmean(realized_r) if realized_r else None,
        "mean_unrealized_gross_r_open_only": statistics.fmean(open_r) if open_r else None,
        "mean_gross_r_including_open_marks": statistics.fmean(marked),
        "horizon_market_close": {"p05": _percentile(path_closes, .05),
                                 "p50": _percentile(path_closes, .5),
                                 "p95": _percentile(path_closes, .95)},
        "band_path_count": min(count, MAX_BAND_PATHS), "price_bands": bands,
        "net_profit": None, "costs_verified": False, "forced_exit_at_horizon": False,
        "warnings": [
            "These are sample proportions from a resampling model, not real-world probabilities.",
            "Segments and decision time are caller assertions, not verified exchange sessions or UTC.",
            "Prices continue for the chart after a simulated exit; open trades are only marked at the horizon.",
            "OHLC cannot establish intrabar order; ambiguous bars use stop first and gaps can exceed 1R.",
            "Bid/ask differences, variable spread, commissions, swap, slippage and actual fills are not modeled.",
        ],
    }


def historical_replay(
    sessions: Any, *, decision_time: int, side: str, opening_range_bars: int = 5,
    stop_rule: str = "range_edge", stop_value: float | None = None,
    target_r: float = 1.5, tick_size: float | None = None,
    horizon_bars: int = 30, cutoff_slot: int | None = None,
) -> dict[str, Any]:
    """Replay a closed-bar breakout on previous segments with next-open entry.

    Every segment is an independent observation; an unhit position at the end
    of the observation window remains open rather than being forcibly closed.
    """
    side = _side(side)
    data, decision = _sessions(sessions, decision_time, minimum=1)
    slots = len(data[0])
    n_range = _integer(opening_range_bars, "opening_range_bars", 1, min(240, slots - 1))
    horizon = _integer(horizon_bars, "horizon_bars", 1, MAX_HORIZON)
    cutoff = slots - 1 if cutoff_slot is None else _integer(cutoff_slot, "cutoff_slot", n_range, slots - 1)
    target_r = _number(target_r, "target_r", minimum=1e-10, maximum=1000)
    if tick_size is not None:
        tick_size = _price(tick_size, "tick_size")
    if stop_rule not in ("range_edge", "range_mid", "price", "distance", "percent", "atr"):
        raise ValueError("Unknown stop_rule")
    if stop_rule in ("price", "distance", "percent", "atr"):
        stop_value = _number(stop_value, "stop_value", minimum=1e-10, maximum=1e12)
        if stop_rule == "percent" and stop_value >= 100:
            raise ValueError("stop percentage must be below 100")

    counts = {"no_signal": 0, "no_next_bar": 0, "invalid_plan": 0,
              "target": 0, "stop": 0, "open": 0}
    rows_out = []
    realized_r: list[float] = []
    open_r: list[float] = []
    gap_stops = ambiguous_stops = 0
    for rows in data:
        high = max(row[2] for row in rows[:n_range])
        low = min(row[3] for row in rows[:n_range])
        trigger = next((i for i in range(n_range, cutoff + 1)
                        if (rows[i][4] > high if side == "buy" else rows[i][4] < low)), None)
        summary: dict[str, Any] = {"segment_start": int(rows[0][0]), "range_high": high, "range_low": low}
        if trigger is None:
            counts["no_signal"] += 1
            rows_out.append({**summary, "status": "no_signal"})
            continue
        summary.update(trigger_slot=trigger, trigger_close_time=int(rows[trigger][0]) + M1_SECONDS,
                       trigger_close=rows[trigger][4])
        if trigger + 1 >= slots:
            counts["no_next_bar"] += 1
            rows_out.append({**summary, "status": "no_next_bar"})
            continue
        # The signal is only known at bar close. The next open is an assumed
        # fill, not proof a live order could have obtained that price.
        assumed_entry = rows[trigger + 1][1]
        atr_value = None
        if stop_rule == "atr":
            seen = rows[:trigger + 1]
            atr_value = indicators.atr([r[2] for r in seen], [r[3] for r in seen],
                                       [r[4] for r in seen], 14)[-1]
        try:
            plan = exit_plan(side=side, entry=assumed_entry, stop_rule=stop_rule,
                             stop_value=stop_value, target_r=target_r, tick_size=tick_size,
                             range_high=high, range_low=low, atr_value=atr_value)
        except ValueError:
            counts["invalid_plan"] += 1
            rows_out.append({**summary, "status": "invalid_plan", "assumed_entry": assumed_entry})
            continue
        observed = rows[trigger + 1:trigger + 1 + horizon]
        outcome = None
        for n, bar in enumerate(observed, start=1):
            event = _bar_exit(side, bar, plan["stop"], plan["target"])
            if event is not None:
                outcome, fill, gap, ambiguous = event
                gross_r = _r(side, assumed_entry, plan["stop"], fill)
                gap_stops += int(gap)
                ambiguous_stops += int(ambiguous)
                counts[outcome] += 1
                realized_r.append(gross_r)
                rows_out.append({**summary, "status": outcome, "assumed_entry": assumed_entry,
                                 "stop": plan["stop"], "target": plan["target"],
                                 "bars_observed": n, "assumed_exit": fill, "gross_r": gross_r,
                                 "gap_stop": gap, "ambiguous_stop_first": ambiguous})
                break
        if outcome is None:
            mark = observed[-1][4]
            gross_r = _r(side, assumed_entry, plan["stop"], mark)
            counts["open"] += 1
            open_r.append(gross_r)
            rows_out.append({**summary, "status": "open", "assumed_entry": assumed_entry,
                             "stop": plan["stop"], "target": plan["target"],
                             "bars_observed": len(observed), "mark_price": mark, "gross_r": gross_r,
                             "observation_shortened_by_segment_end": len(observed) < horizon})
    return {
        "ok": True, "executable": False, "execution_authorized": False,
        "method": "first close beyond caller-defined opening range; assumed entry at next M1 open",
        "time_basis": "caller-supplied broker labels; UTC is not inferred",
        "decision_time": decision, "historical_segments": len(data), "bars_per_segment": slots,
        "opening_range_bars": n_range, "horizon_bars": horizon, "cutoff_slot": cutoff,
        "counts": counts, "segments": rows_out, "gap_stop_count": gap_stops,
        "ambiguous_bar_stop_first_count": ambiguous_stops,
        "realized_gross_r_sum": sum(realized_r), "open_unrealized_gross_r_sum": sum(open_r),
        "net_profit": None, "costs_verified": False, "forced_exit_at_horizon": False,
        "warnings": [
            "Caller-defined segments are independent observations, not a continuous account backtest.",
            "The next open is a hypothetical price; bid/ask spread, slippage, commissions and swap are absent.",
            "Intrabar ambiguity uses stop first; a gap through the stop can be worse than 1R.",
            "Open positions are marked, not closed, when observation ends; results are not a profitability claim.",
        ],
    }
