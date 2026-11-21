import pytest

from trade_mcp.risk import has_blockers, validate_order
from trade_mcp.risk_manager import calculate_position_size


def test_sizing_formula_and_unit_multiplier():
    assert calculate_position_size(10000, 1, 100, 95) == 20
    assert calculate_position_size(10000, 1, 100, 95, loss_per_price_unit=100,
                                    min_size=.01, max_size=.1, step=.01) == .1
    assert calculate_position_size(10000, 1, 100, 95, max_cash_risk=10) == 2
    assert calculate_position_size(100, 1, 100, 95, min_size=1, step=1) == 0


@pytest.mark.parametrize('pct', [2.01, 100, float('nan'), -1, 0, True])
def test_hard_risk_limit(pct):
    with pytest.raises(ValueError):
        calculate_position_size(10000, pct, 100, 95)


def test_floor_cannot_exceed_risk():
    size = calculate_position_size(1234, 1, 100, 97, step=.07)
    assert size*3 <= 12.34


@pytest.mark.parametrize('volume,stop', [(float('nan'), 90), (1, 0), (float('inf'), 90), ('bad', 90)])
def test_validation_blocks_invalid_order_numbers(mock_app, volume, stop):
    info = mock_app.mt5.symbol_info('XAUUSD')
    assert has_blockers(validate_order(cfg=mock_app.cfg, info=info, symbol='XAUUSD',
        side='buy', volume=volume, entry=100, stop=stop, take_profit=110,
        account={'equity': 10000}))


def test_config_cannot_raise_hard_ceiling(mock_app):
    cfg = mock_app.cfg.with_overrides(allow_trading=True, max_risk_percent=99,
                                      max_lot=10, training_budget=10000)
    info = mock_app.mt5.symbol_info('XAUUSD')
    issues = validate_order(cfg=cfg, info=info, symbol='XAUUSD', side='buy', volume=1,
                            entry=100, stop=90, take_profit=120, account={'equity': 10000})
    assert 'risk_exceeded' in [i['code'] for i in issues]
