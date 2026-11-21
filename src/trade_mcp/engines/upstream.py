"""Optional genuine upstream adapters. No imports/downloads at module load; no orders."""
import hashlib
import importlib.util
import math
from pathlib import Path

from .technical import validate_ohlcv

KRONOS_COMMIT = '67b630e67f6a18c9e9be918d9b4337c960db1e9a'
FREQTRADE_COMMIT = 'f3340ce11f5bdf62f598522e64d1f5638eaa13f5'
STRATEGY001_SHA256 = '2357996e6594458e8db45b5f757918462afa3c998de9d06e2af0c009ddc4b80d'


def minute_rows(rows):
    clean = validate_ohlcv(rows)
    if len(clean) < 120 or any(b[0]-a[0] != 60 for a, b in zip(clean, clean[1:], strict=False)):
        raise ValueError('At least 120 contiguous closed M1 OHLCV rows required')
    return clean


def kronos_predict(rows, predictor):
    """Accept an explicitly provisioned official KronosPredictor on a cloud worker.

    The caller supplies CLOSED M1 bars. No weights are loaded/downloaded here.
    The actual upstream predict method is called, not an independent imitation.
    """
    import pandas as pd
    clean = minute_rows(rows)
    df = pd.DataFrame(clean, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    historical = pd.Series(pd.to_datetime(df.pop('time'), unit='s', utc=True).dt.tz_localize(None))
    future = pd.Series(pd.date_range(historical.iloc[-1]+pd.Timedelta(minutes=1), periods=60, freq='min'))
    result = predictor.predict(df=df, x_timestamp=historical, y_timestamp=future,
                               pred_len=60, sample_count=1, verbose=False)
    if len(result) != 60:
        raise ValueError('Kronos must return 60 future candles')
    output = [[clean[-1][0]+60*(i+1), *[float(row[k]) for k in ('open', 'high', 'low', 'close')],
               float(row.get('volume', 0))] for i, (_, row) in enumerate(result.iterrows())]
    volume_adjustments = []
    for i, row in enumerate(output):
        # Prices remain unmodified; retain every volume-domain correction.
        if math.isfinite(row[5]) and row[5] < 0:
            volume_adjustments.append({'minute': i+1, 'raw_volume': row[5], 'used_volume': 0.0})
            row[5] = 0.0
    output = validate_ohlcv(output)
    return {'candles': output, 'source': 'official_KronosPredictor.predict',
            'upstream_commit_expected': KRONOS_COMMIT, 'execution_authorized': False,
            'postprocessing': {'volume_policy': 'negative_to_zero_with_raw_audit',
                               'prices_modified': False, 'volume_adjustments': volume_adjustments},
            'validated_for_trading': False}


def evaluate_kronos(rows, predictor):
    """Fixed final-60-bar holdout, never provided to the predictor at this origin."""
    clean = minute_rows(rows)
    if len(clean) < 180:
        raise ValueError('180 M1 bars required for training context and 60-bar holdout')
    training, holdout = clean[:-60], clean[-60:]
    forecast = kronos_predict(training, predictor)
    metrics = []
    for horizon in (15, 30, 60):
        actual = holdout[horizon-1][4]
        predicted = forecast['candles'][horizon-1][4]
        metrics.append({'minutes_ahead': horizon, 'absolute_error': abs(predicted-actual),
                        'no_change_absolute_error': abs(training[-1][4]-actual)})
    return {'source': forecast['source'], 'holdout_candles': 60, 'metrics': metrics,
            'independent_origins': 1, 'validated_for_trading': False,
            'warning': 'One holdout origin is not profitability or generalization proof'}


def freqtrade_strategy001(rows, source_path):
    """Run verified upstream GPL Strategy001 externally; no GPL source is vendored.

    Requires an already provisioned isolated worker with Freqtrade/pandas/TA-Lib.
    Hash verification happens BEFORE executing the strategy module.
    """
    source = Path(source_path)
    if hashlib.sha256(source.read_bytes()).hexdigest() != STRATEGY001_SHA256:
        raise ValueError('Unverified Strategy001 source; execution refused')
    import pandas as pd
    clean = validate_ohlcv(rows)
    if len(clean) < 200 or any(b[0]-a[0] != 300 for a, b in zip(clean, clean[1:], strict=False)):
        raise ValueError('Strategy001 requires at least 200 contiguous closed 5m bars')
    spec = importlib.util.spec_from_file_location('verified_upstream_strategy001', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    strategy = module.Strategy001({})
    df = pd.DataFrame(clean, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df['date'] = pd.to_datetime(df.pop('time'), unit='s', utc=True)
    metadata = {'pair': 'RESEARCH/ONLY'}
    df = strategy.populate_indicators(df, metadata)
    df = strategy.populate_entry_trend(df, metadata)
    df = strategy.populate_exit_trend(df, metadata)
    last = df.iloc[-1]
    return {'source': 'freqtrade/freqtrade-strategies/Strategy001',
            'upstream_commit': FREQTRADE_COMMIT, 'license': 'GPL-3.0',
            'enter_long': bool(last.get('enter_long', 0) == 1),
            'exit_long': bool(last.get('exit_long', 0) == 1),
            'execution_authorized': False}
