import pytest

from trade_mcp.config import Config, sizing_balance
from trade_mcp.plugins import CATALOG, calculate
from trade_mcp.risk import lots_for_risk


def test_all_bundled_plugins_are_aligned():
    bars = [dict(close=100+i*.1, high=101+i*.1, low=99+i*.1) for i in range(250)]
    out = calculate(bars, [p['id'] for p in CATALOG])
    for series in out.values():
        for values in series.values():
            assert len(values) == len(bars)
            assert values[-1] is not None
    assert out['rsi']['14'][-1] == 100
    assert out['macd']['histogram'][-1] == pytest.approx(0, abs=.001)
    with pytest.raises(ValueError):
        calculate(bars, ['arbitrary-code'])


def test_shared_budget_never_uses_full_demo_balance():
    cfg = Config()
    assert sizing_balance({'equity': 97604}, cfg) == 100
    assert sizing_balance({'equity': 50}, cfg) == 50
    cfg.training_budget = float('nan')
    assert sizing_balance({'equity': 100}, cfg) == 0


def test_cannot_raise_minimum_lot_above_risk_budget():
    result = lots_for_risk(balance=100, risk_percent=1, entry=3900, stop=3910,
                           info={'trade_tick_size': .01, 'trade_tick_value': 1,
                                 'volume_min': .01, 'volume_step': .01})
    assert not result['ok']
    assert result['volume'] == 0


def test_agent_proposal_uses_practice_cap(mock_app):
    from trade_mcp.execution import propose_open

    mock_app.cfg.training_budget = 100
    price = mock_app.mt5.tick('XAUUSD')['ask']
    result = propose_open(mt5=mock_app.mt5, cfg=mock_app.cfg, store=mock_app.store,
                          side='buy', symbol='XAUUSD', stop_loss=price-10)
    assert not result['ok']
    assert result['error'] == 'sizing_failed'
