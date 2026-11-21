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
    ohlc_adjustments = []
    for i, row in enumerate(output):
        if not all(math.isfinite(x) for x in row[1:]) or min(row[1:5]) <= 0:
            raise ValueError('Non-finite output or non-positive model price')
        o, h, low, c = row[1:5]
        if h < max(o, c, low) or low > min(o, c, h):
            raw = {'open': o, 'high': h, 'low': low, 'close': c}
            row[2], row[3] = max(h, o, c), min(low, o, c)
            ohlc_adjustments.append({'minute': i+1, 'raw': raw,
                'used': {'open': o, 'high': row[2], 'low': row[3], 'close': c}})
        # Retain every declared physical-domain correction.
        if math.isfinite(row[5]) and row[5] < 0:
            volume_adjustments.append({'minute': i+1, 'raw_volume': row[5], 'used_volume': 0.0})
            row[5] = 0.0
    output = validate_ohlcv(output)
    return {'candles': output, 'source': 'official_KronosPredictor.predict',
            'upstream_commit_expected': KRONOS_COMMIT, 'execution_authorized': False,
            'postprocessing': {'volume_policy': 'negative_to_zero_with_raw_audit',
                               'ohlc_policy': 'envelope_preserve_open_close_with_raw_audit',
                               'prices_modified': bool(ohlc_adjustments),
                               'ohlc_adjustments': ohlc_adjustments,
                               'volume_adjustments': volume_adjustments},
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


def simple_trend_close_baseline(training_rows, horizon_minutes, *, lookback=15):
    """Deterministic simple trend baseline: linear slope over last `lookback` closes.

    No future leakage: uses only training_rows. Returns predicted close.
    """
    clean = validate_ohlcv(training_rows)
    if horizon_minutes < 1 or lookback < 2 or len(clean) < lookback:
        raise ValueError('Invalid trend baseline inputs')
    closes = [r[4] for r in clean[-lookback:]]
    n = len(closes)
    x_mean = (n - 1) / 2.0
    y_mean = sum(closes) / n
    denom = sum((i - x_mean) ** 2 for i in range(n))
    slope = 0.0 if denom == 0 else sum((i - x_mean) * (closes[i] - y_mean) for i in range(n)) / denom
    # project horizon_minutes bars ahead from last close index (n-1)
    return closes[-1] + slope * horizon_minutes


def _postprocessing_stats(postprocessing):
    post = postprocessing or {}
    ohlc = post.get('ohlc_adjustments') or []
    vol = post.get('volume_adjustments') or []
    ohlc_sizes = []
    for a in ohlc:
        raw, used = a.get('raw') or {}, a.get('used') or {}
        for k in ('open', 'high', 'low', 'close'):
            if k in raw and k in used:
                ohlc_sizes.append(abs(float(used[k]) - float(raw[k])))
    vol_sizes = [abs(float(a.get('raw_volume', 0.0)) - float(a.get('used_volume', 0.0))) for a in vol]
    return {
        'ohlc_correction_count': len(ohlc),
        'volume_correction_count': len(vol),
        'ohlc_correction_size_mean': (sum(ohlc_sizes) / len(ohlc_sizes)) if ohlc_sizes else 0.0,
        'ohlc_correction_size_max': max(ohlc_sizes) if ohlc_sizes else 0.0,
        'volume_correction_size_mean': (sum(vol_sizes) / len(vol_sizes)) if vol_sizes else 0.0,
        'volume_correction_size_max': max(vol_sizes) if vol_sizes else 0.0,
    }


def fixed_multi_origin_splits(rows, *, origins=5, horizon=60, min_context=120, step=60, final_test_origins=1):
    """Deterministic fixed splits with no future leakage.

    Returns list of dicts with origin_index, origin_time, split ('evaluation'|'final_test').
    The final_test_origins are the last origins in chronological order and must not be used for tuning/selection.
    """
    clean = minute_rows(rows)
    if origins < 1 or final_test_origins < 0 or final_test_origins >= origins:
        raise ValueError('Invalid split configuration')
    if horizon < 1 or min_context < 120 or step < 1:
        raise ValueError('Invalid split parameters')
    # Choose origins so each has min_context bars before/including origin and horizon bars after.
    latest = len(clean) - horizon - 1
    earliest = min_context - 1
    if latest < earliest:
        raise ValueError('Insufficient rows for requested splits')
    candidates = list(range(earliest, latest + 1))
    # deterministic evenly spaced selection over available candidates
    if len(candidates) < origins:
        raise ValueError('Insufficient rows for requested number of origins')
    if origins == 1:
        selected = [candidates[-1]]
    else:
        selected = []
        for i in range(origins):
            pos = int(round(i * (len(candidates) - 1) / (origins - 1)))
            selected.append(candidates[pos])
        # enforce minimum step when possible by greedy adjustment forward from left
        adjusted = [selected[0]]
        for idx in selected[1:]:
            adjusted.append(max(idx, adjusted[-1] + step))
        if adjusted[-1] > latest:
            # fallback: place from right to left
            adjusted = [latest - step * (origins - 1 - i) for i in range(origins)]
            if adjusted[0] < earliest:
                raise ValueError('Insufficient rows for requested origins/step')
        selected = adjusted
    splits = []
    for i, origin_idx in enumerate(selected):
        split = 'final_test' if i >= origins - final_test_origins else 'evaluation'
        splits.append({'origin_index': origin_idx, 'origin_time': clean[origin_idx][0], 'split': split})
    return splits


def evaluate_kronos_multi_origin(rows, predictor, *, origins=5, horizons=(15, 30, 60), lookback=15,
                                 final_test_origins=1, step=60):
    """Reproducible multi-origin Kronos evaluation with fixed splits and no leakage.

    - Uses fixed deterministic splits; final_test_origins are never for tuning/selection.
    - Each origin uses only rows <= origin as context; holdout is future rows.
    - Same predictor/settings/postprocessing path (kronos_predict) for every origin.
    - Compares to no-change and simple trend baselines.
    - Reports per-horizon errors and OHLC/volume correction frequency/size.
    No inference is performed unless predictor is called; tests may pass a mock.
    """
    clean = minute_rows(rows)
    horizons = tuple(int(h) for h in horizons)
    if horizons != (15, 30, 60):
        raise ValueError('This workflow currently requires horizons (15,30,60)')
    splits = fixed_multi_origin_splits(clean, origins=origins, horizon=60, min_context=max(120, lookback),
                                       step=step, final_test_origins=final_test_origins)
    origin_reports = []
    agg = {h: {'kronos_abs': [], 'no_change_abs': [], 'trend_abs': []} for h in horizons}
    split_agg = {}
    ohlc_counts, vol_counts, ohlc_sizes, vol_sizes = [], [], [], []
    for spec in splits:
        idx = spec['origin_index']
        training, holdout = clean[:idx + 1], clean[idx + 1:idx + 61]
        if len(holdout) < 60:
            raise ValueError('Insufficient holdout bars for origin')
        forecast = kronos_predict(training, predictor)
        post = _postprocessing_stats(forecast.get('postprocessing'))
        ohlc_counts.append(post['ohlc_correction_count'])
        vol_counts.append(post['volume_correction_count'])
        ohlc_sizes.append(post['ohlc_correction_size_mean'])
        vol_sizes.append(post['volume_correction_size_mean'])
        per_h = []
        for h in horizons:
            actual = float(holdout[h - 1][4])
            pred = float(forecast['candles'][h - 1][4])
            no_change = float(training[-1][4])
            trend = float(simple_trend_close_baseline(training, h, lookback=lookback))
            kronos_abs = abs(pred - actual)
            no_change_abs = abs(no_change - actual)
            trend_abs = abs(trend - actual)
            agg[h]['kronos_abs'].append(kronos_abs)
            agg[h]['no_change_abs'].append(no_change_abs)
            agg[h]['trend_abs'].append(trend_abs)
            split_agg.setdefault(spec['split'], {hh: {'kronos_abs': [], 'no_change_abs': [], 'trend_abs': []} for hh in horizons})
            split_agg[spec['split']][h]['kronos_abs'].append(kronos_abs)
            split_agg[spec['split']][h]['no_change_abs'].append(no_change_abs)
            split_agg[spec['split']][h]['trend_abs'].append(trend_abs)
            per_h.append({
                'minutes_ahead': h,
                'absolute_error': kronos_abs,
                'no_change_absolute_error': no_change_abs,
                'simple_trend_absolute_error': trend_abs,
                'ratio_vs_no_change': (kronos_abs / no_change_abs) if no_change_abs else None,
                'ratio_vs_simple_trend': (kronos_abs / trend_abs) if trend_abs else None,
            })
        origin_reports.append({
            'origin_index': idx,
            'origin_time': spec['origin_time'],
            'split': spec['split'],
            'forecast_origin': training[-1][0] + 60,
            'postprocessing': post,
            'metrics': per_h,
        })

    def _summarize(bucket):
        out = []
        for h in horizons:
            b = bucket[h]
            def mean(xs): return (sum(xs) / len(xs)) if xs else None
            ka, na, ta = b['kronos_abs'], b['no_change_abs'], b['trend_abs']
            out.append({
                'minutes_ahead': h,
                'n': len(ka),
                'kronos_mae': mean(ka),
                'no_change_mae': mean(na),
                'simple_trend_mae': mean(ta),
                'kronos_vs_no_change_ratio': (mean(ka) / mean(na)) if mean(na) else None,
                'kronos_vs_simple_trend_ratio': (mean(ka) / mean(ta)) if mean(ta) else None,
            })
        return out

    by_split = {name: _summarize(bucket) for name, bucket in split_agg.items()}
    return {
        'schema': 'kronos-multi-origin-evaluation/v1',
        'workflow': 'fixed_historical_splits_no_future_leakage',
        'horizons_minutes': list(horizons),
        'origins': len(origin_reports),
        'final_test_origins': final_test_origins,
        'final_test_used_for_tuning': False,
        'same_model_settings_postprocessing': True,
        'baselines': ['no_change_close', f'simple_trend_close_linear_lookback_{lookback}'],
        'split_summary': by_split,
        'overall_summary': _summarize(agg),
        'postprocessing_summary': {
            'origins_with_ohlc_corrections': sum(1 for c in ohlc_counts if c > 0),
            'origins_with_volume_corrections': sum(1 for c in vol_counts if c > 0),
            'ohlc_corrections_total': sum(ohlc_counts),
            'volume_corrections_total': sum(vol_counts),
            'ohlc_corrections_mean_per_origin': (sum(ohlc_counts) / len(ohlc_counts)) if ohlc_counts else 0.0,
            'volume_corrections_mean_per_origin': (sum(vol_counts) / len(vol_counts)) if vol_counts else 0.0,
            'ohlc_correction_size_mean_of_origin_means': (sum(ohlc_sizes) / len(ohlc_sizes)) if ohlc_sizes else 0.0,
            'volume_correction_size_mean_of_origin_means': (sum(vol_sizes) / len(vol_sizes)) if vol_sizes else 0.0,
        },
        'origins_detail': origin_reports,
    }
