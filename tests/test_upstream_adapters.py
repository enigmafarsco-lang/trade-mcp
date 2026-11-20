import pytest

from trade_mcp.engines.upstream import freqtrade_strategy001, minute_rows


def test_unverified_gpl_source_not_executed(tmp_path):
    path = tmp_path/'Strategy001.py'
    path.write_text('raise RuntimeError("must never execute")')
    with pytest.raises(ValueError, match='Unverified'):
        freqtrade_strategy001([], path)


def test_m1_gaps_rejected():
    with pytest.raises(ValueError):
        minute_rows([[i*120, 1, 2, .5, 1, 0] for i in range(180)])


def test_kronos_protocol_and_holdout_in_optional_research_environment():
    import os
    import subprocess
    from pathlib import Path

    python = os.environ.get('RESEARCH_TEST_PYTHON', '')
    if not python or not Path(python).is_file():
        pytest.skip('Optional pre-provisioned research Python unavailable')
    code = '''
from trade_mcp.engines.upstream import kronos_predict, evaluate_kronos
import pandas as pd
class FakePredictor:
    def predict(self, **kw):
        assert kw['pred_len'] == 60
        assert len(kw['df']) == 120
        assert kw['x_timestamp'].iloc[-1] < kw['y_timestamp'].iloc[0]
        return pd.DataFrame([dict(open=100,high=102,low=99,close=101,volume=1)]*60)
rows = [[i*60,100,102,99,101,1] for i in range(180)]
result = evaluate_kronos(rows, FakePredictor())
assert result['holdout_candles'] == 60
assert not result['validated_for_trading']
assert len(kronos_predict(rows[:120], FakePredictor())['candles']) == 60
'''
    result = subprocess.run([python, '-c', code], env={'PATH': os.environ.get('PATH', ''),
        'PYTHONPATH': str(Path(__file__).resolve().parents[1]/'src')},
        capture_output=True, timeout=30)
    assert result.returncode == 0, 'Optional adapter protocol test failed'
