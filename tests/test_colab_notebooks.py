import json
from pathlib import Path

from trade_mcp.engines.upstream import FREQTRADE_COMMIT, KRONOS_COMMIT, STRATEGY001_SHA256
from trade_mcp.kronos_artifact import load_forecast

ROOT = Path(__file__).resolve().parents[1]
KRONOS_NB = ROOT / 'notebooks' / 'kronos_colab.ipynb'
FREQ_NB = ROOT / 'notebooks' / 'freqtrade_strategy001_colab.ipynb'


def _nb(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _text(nb):
    parts = []
    for cell in nb.get('cells', []):
        src = cell.get('source', [])
        parts.append(''.join(src) if isinstance(src, list) else str(src))
    return '\n'.join(parts)


def test_kronos_notebook_has_required_safety_and_schema():
    nb = _nb(KRONOS_NB)
    text = _text(nb)
    assert nb.get('nbformat') == 4
    assert KRONOS_COMMIT in text
    for required in [
        'google.colab', 'IN_COLAB', 'This notebook must run only inside Google Colab runtime',
        'redact', 'No file uploaded',
        'official_KronosPredictor.predict', 'upstream_commit_expected', 'holdout_evaluation',
        'files.download', 'execution_authorized', 'validated_for_trading', '120'
    ]:
        assert required in text
    assert ('جعلی' in text) or ('fabricate' in text.lower())
    assert ('Failed baseline screen' in text) or ('failed_historical_baseline_check' in text)
    # artifact format accepted by trade-mcp load_forecast
    artifact = {
        'symbol': 'XAUUSD', 'timeframe': 'M1', 'forecast_origin': 6000,
        'source': 'official_KronosPredictor.predict', 'upstream_commit_expected': KRONOS_COMMIT,
        'candles': [[6000+i*60, 100, 102, 99, 101, 0] for i in range(60)],
        'holdout_evaluation': {'metrics': [{'minutes_ahead': h, 'absolute_error': 1, 'no_change_absolute_error': 2} for h in (15,30,60)]},
        'execution_authorized': False, 'validated_for_trading': False,
    }
    tmp = ROOT / 'logs' / 'test-kronos-artifact-schema.json'
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(artifact), encoding='utf-8')
    try:
        res = load_forecast(tmp, 'XAUUSD', 'M1', now=6030)
        assert res['available'] is True
        assert len(res['candles']) == 60
    finally:
        tmp.unlink(missing_ok=True)


def test_freqtrade_notebook_has_required_safety_and_pins():
    nb = _nb(FREQ_NB)
    text = _text(nb)
    assert nb.get('nbformat') == 4
    assert FREQTRADE_COMMIT in text
    assert STRATEGY001_SHA256 in text
    for required in [
        'google.colab', 'IN_COLAB', 'This notebook must run only inside Google Colab runtime',
        'redact', 'Strategy001.py', 'populate_indicators', 'populate_entry_trend', 'populate_exit_trend',
        'execution_authorized', 'GPL-3.0', '5m', 'M1 tick momentum', 'is_m1_tick_momentum',
        'files.download', 'No file uploaded', 'validated_for_trading'
    ]:
        assert required in text
    assert 'order' not in text.lower() or 'no ... order' in text.lower() or 'سفارش' in text  # Persian warning present; allow "order" word in context


def test_notebooks_do_not_claim_execution_completed():
    for path in (KRONOS_NB, FREQ_NB):
        text = _text(_nb(path)).lower()
        assert 'تا زمانی که واقعاً اجرا نشده' in _text(_nb(path)) or 'عدم اجرای نوت‌بوک' in _text(_nb(path))
        assert 'expose' not in text or 'public server' in text


def test_kronos_checks_python_before_install_and_preserves_pip_diagnostics():
    text = _text(_nb(KRONOS_NB))
    assert "sys.version_info[:2] <= (3, 12)" in text
    assert text.index('Runtime Python:') < text.index("'pip','install'")
    assert "'pip','install','-q'" not in text
    assert 'Read the pip ERROR lines above' in text
    # Parse code cells without installing or executing cloud dependencies.
    for cell in _nb(KRONOS_NB)['cells']:
        if cell['cell_type'] == 'code':
            compile(''.join(cell['source']), '<colab-cell>', 'exec')


def test_kronos_runtime_validation_precedes_upload_and_protects_partial_imports():
    nb = _nb(KRONOS_NB)
    text = _text(nb)
    assert text.index('validate runtime BEFORE') < text.index('uploaded = files.upload()')
    assert '_changed_loaded' in text
    assert 'TORCH_LIBRARY/prims' in text
    assert 'Runtime > Restart session' in text
    assert 'torch.tensor([1.0, 2.0])' in text
    for cell in nb['cells']:
        if cell['cell_type'] != 'code':
            continue
        code = ''.join(cell['source'])
        compile(code, '<colab-cell>', 'exec')
        assert cell['execution_count'] is None
        assert cell['outputs'] == []
        if code.startswith('# COLAB ONLY - actual upstream inference'):
            assert code.index("globals().get('_KRONOS_RUNTIME_READY')") < code.index('from model import')


def test_generated_kronos_defects_are_diagnosed_not_silently_repaired():
    text = _text(_nb(KRONOS_NB))
    for code in ('non_finite_model_output', 'non_positive_price',
                 'envelope_preserve_open_close_with_raw_audit', 'ohlc_adjustments',
                 'negative_to_zero_with_raw_audit'):
        assert code in text
    assert 'all(math.isfinite(x)' in text
    assert 'No artifact was produced' in text
