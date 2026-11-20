"""Bounded synchronous rules evaluations; never bypasses the supplied guarded step."""
import time


def run_window(step, stop, *, duration=300, clock=time.monotonic):
    if type(duration) is not int or not 1 <= duration <= 300:
        raise ValueError('duration must be 1..300 seconds')
    start = clock()
    deadline = start + duration
    attempts = 0
    pending = 0
    executed = 0
    halt_reason = None
    while attempts < duration and clock() < deadline and not stop.is_set():
        # Exceptions stop the entire window; no automatic retry.
        report = step()
        attempts += 1
        if isinstance(report, dict):
            entries = report.get('symbols') or []
            executed += sum(e.get('status') == 'executed' for e in entries)
            pending += sum(e.get('status') == 'pending' for e in entries)
            if pending or report.get('errors'):
                halt_reason = 'pending_confirmation' if pending else 'evaluation_error'
                break  # Never flood the journal with unconfirmed duplicates.
        # Skip missed slots: never catch up with an order burst.
        next_slot = max(start + attempts, clock() + 1)
        stop.wait(max(0, min(next_slot, deadline) - clock()))
    return {'ok': True, 'evaluations': attempts, 'stopped': stop.is_set(),
            'duration_limit_seconds': duration, 'orders_guaranteed': False,
            'executed_entries': executed, 'pending_entries': pending, 'halt_reason': halt_reason}
