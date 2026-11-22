from datetime import datetime, timedelta

import pytest

from trade_mcp.tick_replay import replay_signals


def ticks():
    start = datetime(2026, 11, 20, 19, 23)
    return [{'time_ms': (start+timedelta(seconds=i)).isoformat(timespec='milliseconds'),
             'bid': 2000+i*.01, 'ask': 2000.1+i*.01} for i in range(300)]


def test_replays_actual_signal_logic_without_pnl_or_utc_claim():
    r = replay_signals(ticks())
    assert r['replayed_rows'] == 180
    assert r['boundary_rows_discarded'] == 120
    assert r['interior_minutes_seen'] == 3
    assert r['signal_counts']['buy'] > 0
    assert r['signal_counts'].get('sell', 0) == 0
    assert r['pnl'] is None and r['execution_authorized'] is False
    assert r['utc_verified'] is False
    assert sum(r['wait_reason_counts'].values())+sum(r['signal_counts'].values()) == 180


@pytest.mark.parametrize('defect', ['duplicate', 'aware', 'crossed', 'no_interior'])
def test_reject_unsupported_input(defect):
    rows = ticks()
    if defect == 'duplicate':
        rows[1]['time_ms'] = rows[0]['time_ms']
    elif defect == 'aware':
        rows[0]['time_ms'] += 'Z'
    elif defect == 'crossed':
        rows[0]['ask'] = 1
    else:
        rows = rows[:60]
    with pytest.raises(ValueError):
        replay_signals(rows)
