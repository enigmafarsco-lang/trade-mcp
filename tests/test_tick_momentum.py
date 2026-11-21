import pytest

from trade_mcp.tick_momentum import TickMomentum


def tick(ms, price):
    return {'time': ms/1000, 'time_msc': ms, 'bid': price, 'ask': price+.1}


@pytest.mark.parametrize('count', [2, 3])
def test_fresh_consecutive_changes(count):
    strategy = TickMomentum(count)
    candle = {'time': 60, 'open': 100}
    for i in range(count):
        assert strategy.observe(tick(61000+i*100, 101+i), candle, now=61+i*.1)['action'] == 'wait'
    assert strategy.observe(tick(61000+count*100, 101+count), candle, now=61+count*.1)['action'] == 'buy'
    assert strategy.observe(tick(61000+count*100, 101+count), candle, now=61+count*.1)['action'] == 'wait'


def test_sell_and_candle_reset():
    strategy = TickMomentum(2)
    candle = {'time': 60, 'open': 100}
    strategy.observe(tick(61000, 99), candle, now=61)
    strategy.observe(tick(61100, 98), candle, now=61.1)
    assert strategy.observe(tick(61200, 97), candle, now=61.2)['action'] == 'sell'
    assert strategy.observe(tick(120100, 96), {'time': 120, 'open': 97}, now=120.1)['action'] == 'wait'


def test_spread_only_change_does_not_count():
    strategy = TickMomentum(2)
    candle = {'time': 60, 'open': 100}
    strategy.observe(tick(61000, 101), candle, now=61)
    quote = tick(61100, 101)
    quote['ask'] += 1
    assert strategy.observe(quote, candle, now=61.1)['action'] == 'wait'
    assert strategy.count == 0
    assert strategy.observe(tick(61200, 102), candle, now=70)['reason'] == 'invalid_or_stale_quote'


def test_invalid_parameter():
    with pytest.raises(ValueError):
        TickMomentum(1)


def test_tick_step_uses_guarded_execution_route(mock_app, monkeypatch):
    from types import SimpleNamespace

    import trade_mcp.tick_momentum as module
    app = mock_app
    app.cfg = app.cfg.with_overrides(allow_trading=True, demo_cash_policy=True)
    now = 121.0
    mt5 = SimpleNamespace(resolve_symbol=lambda s: s,
        candles=lambda *a: [{'time': 120, 'open': 100}],
        tick=lambda s: {'time': now, 'bid': 101., 'ask': 101.1},
        positions=lambda: [], orders=lambda: [],
        symbol_info=lambda *a, **kw: {'volume_min': .01, 'volume_max': 1,
            'volume_step': .01, 'digits': 2, 'trade_tick_size': .01,
            'trade_tick_value': 1},
        account_info=lambda **kw: {'trade_mode': 'demo', 'currency': 'USD',
            'login': 1, 'server': 'demo', 'equity': 1000, 'balance': 1000})
    calls = []
    monkeypatch.setattr(module, 'loss_per_lot', lambda *a: 100)
    monkeypatch.setattr(module, 'propose_open', lambda **kw: calls.append(kw) or {'ok': True, 'proposal_id': 'p'})
    monkeypatch.setattr(module, 'confirm', lambda **kw: calls.append(kw) or {'ok': True})
    fake = SimpleNamespace(cfg=app.cfg, mt5=mt5, store=app.store)
    strategy = SimpleNamespace(observe=lambda *a: {'action': 'buy'})
    result = module.tick_step(fake, {'id': 'tick', 'symbols': ['XAUUSD'],
                                   'max_lot': .02, 'max_risk': .5}, strategy)
    assert result['symbols'][0]['status'] == 'executed'
    assert calls[0]['comment'] == 'agent:tick'
    assert calls[0]['cfg'].demo_cash_loss_usd == 2
    assert calls[1]['cfg'].demo_cash_loss_usd == 2
    assert calls[0]['volume'] == .02


def test_broker_clock_offset_does_not_reject_current_candle():
    strategy = TickMomentum(2)
    candle = {'time': 7260, 'time_utc': 60, 'open': 100}
    for i in range(2):
        strategy.observe(tick(61000+i*100, 101+i), candle, now=61+i*.1)
    result = strategy.observe(tick(61200, 103), candle, now=61.2)
    assert result['action'] == 'buy'


def test_received_quotes_are_not_claimed_as_broker_ticks():
    strategy = TickMomentum(2)
    candle = {'time': 60, 'open': 100}
    for i in range(3):
        quote = {**tick(61000+i*100, 101+i), 'time_source': 'received'}
        result = strategy.observe(quote, candle, now=61+i*.1)
    assert result['action'] == 'buy'
    assert result['evidence_kind'] == 'received_quote_samples'
