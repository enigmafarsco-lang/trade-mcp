import pytest

from trade_mcp.bounded import run_window


class Timer:
    def __init__(self):
        self.now = 0
        self.stopped = False

    def clock(self):
        return self.now

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.now += seconds


def test_five_minute_bound():
    t = Timer()
    calls = []
    r = run_window(lambda: calls.append(t.now), t, clock=t.clock)
    assert len(calls) == 300
    assert t.now == 300
    assert not r['orders_guaranteed']


def test_slow_evaluation_does_not_catch_up():
    t = Timer()
    calls = []
    def step():
        calls.append(t.now)
        t.now += 3
    run_window(step, t, duration=10, clock=t.clock)
    assert calls == [0, 4, 8]


def test_error_no_retry():
    t = Timer()
    def step():
        raise RuntimeError('blocked')
    with pytest.raises(RuntimeError):
        run_window(step, t, clock=t.clock)
    assert t.now == 0


@pytest.mark.parametrize('duration', [0, 301, True, 2.5])
def test_invalid_duration(duration):
    t = Timer()
    with pytest.raises(ValueError):
        run_window(lambda: None, t, duration=duration, clock=t.clock)


def test_stop():
    t = Timer()
    def step():
        t.stopped = True
    assert run_window(step, t, clock=t.clock)['evaluations'] == 1
