from types import SimpleNamespace

import pytest

from trade_mcp.manual import automatic_stop


def test_automatic_stop_obeys_budget():
    spec = dict(trade_tick_size=.01, trade_tick_value=1, point=.01, spread_points=10)
    for side in ['buy', 'sell']:
        stop, volume = automatic_stop(spec=spec, quote={'bid':2000,'ask':2000.1},
            account={'equity':10000}, cfg=SimpleNamespace(training_budget=100,max_risk_percent=1),
            side=side, volume=.01)
        entry = 2000.1 if side == 'buy' else 2000
        assert abs(stop-entry)*100*volume <= 1.000001
    with pytest.raises(ValueError):
        automatic_stop(spec=spec, quote={'bid':2000,'ask':2000.1}, account={'equity':100},
            cfg=SimpleNamespace(training_budget=100,max_risk_percent=1), side='buy',volume=1)
