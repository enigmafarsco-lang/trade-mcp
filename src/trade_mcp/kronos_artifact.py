"""Read a local cloud-worker result without loading weights or executing code."""
import json
import math
from pathlib import Path

from .engines.technical import validate_ohlcv
from .engines.upstream import KRONOS_COMMIT


def load_forecast(path, symbol, timeframe, *, now):
    unavailable = {'available': False, 'execution_eligible': False}
    if not path or timeframe != 'M1':
        return {**unavailable, 'reason': 'cloud_Kronos_artifact_required_on_M1'}
    try:
        file = Path(path).expanduser()
        if file.is_symlink() or file.stat().st_size > 1_000_000:
            raise ValueError('Invalid artifact')
        data = json.loads(file.read_text())
        origin = float(data['forecast_origin'])
        if not math.isfinite(origin) or not 0 <= now-origin <= 120:
            raise ValueError('Stale forecast')
        if data['symbol'] != symbol or data['timeframe'] != 'M1':
            raise ValueError('Instrument mismatch')
        if data['source'] != 'official_KronosPredictor.predict' or data['upstream_commit_expected'] != KRONOS_COMMIT:
            raise ValueError('Unverified provenance')
        candles = validate_ohlcv(data['candles'])
        if len(candles) != 60 or any(r[0] != origin+60*i for i, r in enumerate(candles)):
            raise ValueError('Invalid forecast timestamps')
        metrics = data['holdout_evaluation']['metrics']
        if [m['minutes_ahead'] for m in metrics] != [15, 30, 60]:
            raise ValueError('Missing holdout evaluation')
        for metric in metrics:
            error, baseline = float(metric['absolute_error']), float(metric['no_change_absolute_error'])
            if not all(math.isfinite(v) and v >= 0 for v in (error, baseline)) or error >= baseline:
                raise ValueError('Failed baseline screen')
        return {**unavailable, 'available': True, 'candles': candles,
                'forecast_origin': origin, 'source': data['source'],
                'validated_for_trading': False,
                'warning': 'کندل پیش‌بینی Kronos؛ نه قیمت واقعی یا تضمین؛ آزمون محدود گذشته'}
    except (OSError, ValueError, KeyError, TypeError):
        return {**unavailable, 'reason': 'invalid_stale_or_failed_Kronos_artifact'}
