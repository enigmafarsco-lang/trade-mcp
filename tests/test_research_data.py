import pytest

from trade_mcp.research_data import closed_m1_segment


def bars(n, start=0):
    return [{'time_utc': start+i*60, 'open': 100, 'high': 102, 'low': 99,
             'close': 101, 'tick_volume': 2} for i in range(n)]


def test_closed_only_and_limit():
    result = closed_m1_segment(bars(1001), now=60000, maximum=500)
    assert len(result) == 500
    assert result[-1][0]+60 <= 60000
    assert len(result[0]) == 6


def test_latest_eligible_segment_without_gap_filling():
    data = bars(450)+bars(430, start=60000)+bars(5, start=100000)
    # Invalid minute boundary is rejected even in a short tail.
    with pytest.raises(ValueError):
        closed_m1_segment(data, now=200000)
    data = bars(450)+bars(430, start=60000)+bars(5, start=120000)
    result = closed_m1_segment(data, now=200000)
    assert len(result) == 430
    assert result[0][0] == 60000


def test_unknown_clock_and_insufficient_history_rejected():
    data = bars(500)
    data[0]['time_utc'] = None
    with pytest.raises(ValueError, match='Verified UTC'):
        closed_m1_segment(data, now=60000)
    with pytest.raises(ValueError, match='sufficiently long'):
        closed_m1_segment(bars(180), now=60000)
