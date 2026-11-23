import math

import pytest

from trade_mcp.training import risk_plan, signal, training_snapshot

SPEC = dict(trade_tick_size=.01, trade_tick_value=1, volume_step=.01,
            volume_min=.01, volume_max=100, point=.01, trade_stops_level=0)


def test_risk_sizing_and_costs():
    p = risk_plan(spec=SPEC, budget=100, risk_percent=1, distance=1, target=2,
                  commission=4, slippage=.01)
    assert p['loss_estimate'] <= 1
    assert p['volume'] == 0  # .01 lots would exceed the one-dollar limit
    assert p['blockers']
    p = risk_plan(spec=SPEC, budget=100, risk_percent=1, distance=1, target=2)
    assert p['volume'] == .01
    assert p['loss_estimate'] == 1
    assert p['profit_estimate'] == 2
    assert p['executable'] is False


@pytest.mark.parametrize('bad', [0, -1, math.nan, math.inf])
def test_invalid_budget(bad):
    with pytest.raises(ValueError):
        risk_plan(spec=SPEC, budget=bad, risk_percent=1, distance=1, target=1)


def test_stops_and_nonpositive_profit_block():
    p = risk_plan(spec={**SPEC, 'trade_stops_level': 200}, budget=100,
                  risk_percent=1, distance=1, target=.01, slippage=.1)
    assert len(p['blockers']) >= 2


def test_forming_candles_do_not_change_signal():
    bars = [dict(time=i*900, open=100+i*.1, high=101+i*.1,
                 low=99+i*.1, close=100+i*.1) for i in range(260)]
    before = signal(bars, bars)
    changed = [*bars[:-1], {**bars[-1], 'close': 99999, 'high': 99999}]
    assert signal(changed, changed) == before
    assert before['closed_candle_time'] == bars[-2]['time']
    assert signal([], [])['side'] == 'wait'


class Demo:
    def resolve_symbol(self, symbol):
        return symbol

    def account_info(self, **kw):
        return dict(trade_mode='demo', margin_free=1000, currency='USD', server='Test')

    def symbol_info(self, *a, **kw):
        return SPEC

    def tick(self, symbol):
        return dict(bid=125, ask=125.1)

    def candles(self, *args):
        return [dict(time=i*900, open=100+i*.1, high=101+i*.1,
                     low=99+i*.1, close=100+i*.1) for i in range(260)]

    def calc_order(self, *args):
        return {'margin': 2}


def test_readonly_snapshot_and_money_target(monkeypatch):
    monkeypatch.setattr('trade_mcp.training.signal', lambda *a: {
        'side': 'buy', 'suggested_distance': 1})
    d = training_snapshot(Demo(), {'budget': '100', 'risk': '1', 'target_money': '1'})
    assert d['plan']['profit_estimate'] == pytest.approx(1)
    assert d['sl'] == pytest.approx(124.1)
    assert d['tp'] == pytest.approx(126.1)
    assert d['margin'] == 2
    assert not d['plan']['executable']


def test_real_account_is_blocked(monkeypatch):
    class Real(Demo):
        def account_info(self, **kw):
            return dict(trade_mode='real', margin_free=1000)

    monkeypatch.setattr('trade_mcp.training.signal', lambda *a: {
        'side': 'buy', 'suggested_distance': 1})
    result = training_snapshot(Real(), {})
    assert result['plan']['blockers']
    assert result['margin'] is None


def test_backtest_closed_data_and_conservative_same_bar(monkeypatch):
    from trade_mcp.training import backtest

    def idea(bars, trend, strategy):
        # Last item is a forming placeholder; actual data must be in the past.
        assert trend[-2]['time'] + 3600 <= bars[-1]['time']
        return {'side': 'buy', 'suggested_distance': 1}

    monkeypatch.setattr('trade_mcp.training.signal', idea)
    # H1 history before sample, then candles hitting both stop and target.
    trend = [dict(time=i*3600, open=100, high=102, low=98, close=100)
             for i in range(220)]
    bars = [dict(time=220*3600+i*900, open=100, high=102, low=98, close=100)
            for i in range(65)]
    result = backtest(bars, trend, spec=SPEC)
    assert len(result['trades']) > 0
    assert all(t['reason'] == 'stop' for t in result['trades'])
    assert result['ending'] < 100
    assert result['max_drawdown_percent'] > 0


def test_http_training_endpoint():
    import json
    import threading
    from types import SimpleNamespace
    from urllib.request import urlopen

    from trade_mcp.console import ConsoleServer

    app = SimpleNamespace(mt5=Demo())
    server = ConsoleServer(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.httpd.server_port
        with urlopen(f'http://127.0.0.1:{port}/api/training?budget=100&risk=1') as response:
            result = json.load(response)
        assert result['ok']
        assert result['plan']['executable'] is False
        with urlopen(f'http://127.0.0.1:{port}/') as response:
            assert 'tr-backtest' in response.read().decode()
    finally:
        server.stop()
        thread.join(timeout=3)
