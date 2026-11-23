"""Fail-closed post-analysis entry checks. Never adjusts model price levels."""
import math
from datetime import datetime

MAX_ANALYSIS_AGE_SECONDS = 30.0
MAX_TICK_AGE_SECONDS = 10.0
MAX_FUTURE_TICK_SECONDS = 2.0
MAX_ENTRY_DRIFT_R = 0.25


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def post_response_check(*, quote, tick, info, decision, elapsed, now, max_spread_points):
    """Validate same-side drift vs original stop distance, not a moved stop."""
    reasons = []
    result = {'ok':False, 'reasons':reasons, 'analysis_age_seconds':elapsed,
              'max_analysis_age_seconds':MAX_ANALYSIS_AGE_SECONDS,
              'max_tick_age_seconds':MAX_TICK_AGE_SECONDS, 'max_entry_drift_r':MAX_ENTRY_DRIFT_R}
    if not math.isfinite(elapsed) or elapsed < 0 or elapsed > MAX_ANALYSIS_AGE_SECONDS:
        reasons.append('analysis_expired')
    try:
        timestamp = datetime.fromisoformat(quote['tick_time'].replace('Z','+00:00'))
        original_age = now-timestamp.timestamp() if timestamp.tzinfo is not None else None
    except (KeyError, ValueError, TypeError, AttributeError):
        original_age = None
    if original_age is None or not -MAX_FUTURE_TICK_SECONDS <= original_age <= MAX_ANALYSIS_AGE_SECONDS:
        reasons.append('original_quote_time_invalid_or_expired')
    tick_time = number(tick.get('time'))
    age = now-tick_time if tick_time is not None else None
    result['tick_age_seconds'] = age
    if age is None or not -MAX_FUTURE_TICK_SECONDS <= age <= MAX_TICK_AGE_SECONDS:
        reasons.append('fresh_quote_time_invalid_or_stale')
    bid, ask, point = (number(tick.get('bid')), number(tick.get('ask')), number(info.get('point')))
    if bid is None or ask is None or point is None or bid <= 0 or ask < bid or point <= 0:
        reasons.append('fresh_quote_or_point_invalid')
        return result
    spread = (ask-bid)/point
    result['spread_points'] = spread
    if spread > max_spread_points:
        reasons.append('spread_too_wide_after_response')
    side = decision.get('action')
    entry = ask if side == 'buy' else bid
    reference = number(quote.get('ask' if side == 'buy' else 'bid'))
    sl, tp = number(decision.get('stop_loss')), number(decision.get('take_profit'))
    result['checked_entry'] = entry
    if side not in {'buy','sell'} or reference is None or reference <= 0 or sl is None or tp is None or sl <= 0 or tp <= 0:
        reasons.append('decision_levels_invalid')
        return result
    valid_original = sl < reference < tp if side == 'buy' else tp < reference < sl
    valid_fresh = sl < entry < tp if side == 'buy' else tp < entry < sl
    if not valid_original or not valid_fresh:
        reasons.append('decision_levels_crossed_or_wrong_side')
    distance = abs(reference-sl)
    drift = abs(entry-reference)/distance if distance else None
    result['entry_drift_r'] = drift
    if drift is None or drift > MAX_ENTRY_DRIFT_R:
        reasons.append('entry_drift_exceeded')
    result['ok'] = not reasons
    return result
