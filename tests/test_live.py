import threading
from types import SimpleNamespace

import pytest

from trade_mcp.bridge.base import BridgeError
from trade_mcp.live import QuoteFeed


class Prices:
    def __init__(self):
        self.calls = 0
        self.failed = False

    def tick(self, name):
        self.calls += 1
        if self.failed:
            raise BridgeError('offline', code='offline')
        return {'bid': 100, 'ask': 101, 'time': 123}


def test_coalescing_and_shared_cache():
    prices = Prices()
    feed = QuoteFeed(prices, threading.Lock(), ttl=10)
    responses = []
    threads = [threading.Thread(target=lambda: responses.append(feed.snapshot(['GOLD', 'GOLD']))) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert prices.calls == 1
    assert len(responses) == 5
    assert responses[0]['quotes']['GOLD']['received_at_ms'] > 0
    assert responses[0]['transport'] == 'polling'


def test_failure_does_not_report_old_price_as_fresh():
    prices = Prices()
    feed = QuoteFeed(prices, threading.Lock(), ttl=0)
    old = feed.snapshot(['GOLD'])
    prices.failed = True
    new = feed.snapshot(['GOLD'])
    assert old['quotes']['GOLD']['sequence'] == 1
    assert not new['quotes']
    assert 'offline' in new['errors']['GOLD']


@pytest.mark.parametrize('symbols', [[], ['A','B','C','D','E'], ['x'*65], ['<script>']])
def test_bounds(symbols):
    with pytest.raises(ValueError):
        QuoteFeed(Prices(), threading.Lock()).snapshot(symbols)


def test_bad_prices_and_bounded_cache():
    p = SimpleNamespace(tick=lambda name: {'bid': float('nan'), 'ask': 1})
    result = QuoteFeed(p, threading.Lock()).snapshot(['GOLD'])
    assert result['errors'] and not result['quotes']
    feed = QuoteFeed(Prices(), threading.Lock())
    for i in range(40):
        feed.snapshot([str(i)])
    assert len(feed.cache) <= 32
