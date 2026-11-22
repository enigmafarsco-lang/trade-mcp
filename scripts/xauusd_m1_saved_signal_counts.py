"""Extract safe per-window counts from an existing, completed M1 summary.

This is aggregate-only: it does not import or invoke the replay, inspect tick
archives or economics, call MT5, open network connections, or place orders.
It checks all 12 saved scenario pairs against the committed sanitized report
before publishing any counts. The check does not verify the raw archive hash.
"""
import json
import math
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / 'logs/research-inputs/xauusd-m1-previous-direction-v1/summary.json'
REFERENCE = ROOT / 'reports/research/20261122T130500Z-xauusd-m1-previous-direction.json'
OUTPUT = ROOT / 'reports/research/xauusd-m1-saved-signal-counts.json'
MAX_BYTES = 2_000_000
SAMPLES = ('additional_sample', 'w1', 'w2', 'w3', 'w4', 'w5')
# Published older aggregates: (returned rows, 3-change raw buys, raw sells).
# Sources: 20261122T073857Z-xauusd-signal-replay.json and
# 20261122T110500Z-xauusd-sparse-screen-v2-5win.json. Not a raw-input hash.
PUBLISHED_WINDOW_COUNTS = {
    'additional_sample': (1015, 16, 17), 'w1': (647, 20, 5), 'w2': (738, 6, 9),
    'w3': (820, 16, 10), 'w4': (745, 24, 13), 'w5': (862, 25, 12),
}
MODELS = {'original_3_changes': (3, None), '2_changes_spread_cap_1_00': (2, 1.0)}
MODES = (('previous_available_only', 'pav'), ('previous_direction_aligned', 'pda'))


class AggregateMismatch(ValueError):
    """A data-free diagnostic assembled only from fixed validation labels."""


def invalid():
    raise ValueError('saved_aggregate_invalid_or_inconsistent')


def checked(label, func, *args):
    """Add a fixed scope to a failure without carrying any input or exception text."""
    try:
        return func(*args)
    except AggregateMismatch:
        raise
    except Exception:
        raise AggregateMismatch(label) from None


def has_symlink(path):
    # Inputs and the output directory must not resolve through an alias.
    return any(part.is_symlink() for part in (path, *path.parents))


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            invalid()
        value[key] = item
    return value


def load_json(path):
    if has_symlink(path):
        invalid()
    # Non-blocking avoids hanging on a special file; check the opened file,
    # not only its pre-open metadata. O_NOFOLLOW also refuses a swapped leaf.
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            invalid()
        raw = stream.read(MAX_BYTES + 1)
    if not 0 < len(raw) <= MAX_BYTES:
        invalid()
    return json.loads(raw, parse_constant=lambda _: invalid(), object_pairs_hook=unique_object)


def count(value, maximum=2000):
    if type(value) is not int or not 0 <= value <= maximum:
        invalid()
    return value


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        invalid()
    return float(value)


def close(actual, expected):
    return math.isclose(finite(actual), finite(expected), rel_tol=0, abs_tol=1e-8)


def index_exact(items, key, expected):
    if not isinstance(items, list) or len(items) != len(expected):
        invalid()
    indexed = {}
    for item in items:
        if not isinstance(item, dict) or item.get(key) not in expected or item[key] in indexed:
            invalid()
        indexed[item[key]] = item
    if set(indexed) != set(expected):
        invalid()
    return indexed


def validate_header(summary, reference):
    if (not isinstance(summary, dict) or not isinstance(reference, dict)
            or reference.get('schema') != 'xauusd-research-check-report/v1'
            or reference.get('report_id') != '20261122T130500Z-xauusd-m1-previous-direction'
            or reference.get('head_at_execution') != '8f684993ffac34bb4c8a36ca0aebda35677ef531'
            or type(reference.get('execution_count')) is not int or reference['execution_count'] != 1
            or reference.get('raw_data_committed') is not False
            or not isinstance(reference.get('result'), dict)):
        invalid()
    recorded = reference['result']
    for report in (summary, recorded):
        if (report.get('schema') != 'xauusd-m1-previous-direction/v1'
                or report.get('status') != 'completed_all_paired_comparisons'
                or report.get('execution_authorized') is not False
                or report.get('profitability_proven') is not False
                or report.get('blind_holdout_or_out_of_sample') is not False
                or report.get('prior_hypothesis_selection_after_results') is not True
                or report.get('independent_hypothetical_windows_not_continuous') is not True
                or report.get('spread_included') is not True
                or report.get('additional_slippage_modeled') is not False
                or report.get('complete_tick_coverage_verified') is not False
                or report.get('utc_verified') is not False
                or report.get('historical_contract_rules_verified') is not False
                or report.get('baseline_check_is_metrics_not_raw_archive_hash') is not True
                or any(report.get(key, object()) is not None for key in ('commission', 'fees', 'swap', 'net_pnl'))):
            invalid()
        for key in ('planned_paired_comparisons', 'completed_paired_comparisons',
                    'baseline_checks_attempted', 'baseline_checks_matched'):
            if count(report.get(key)) != 12:
                invalid()
        for key in ('history_calls_made', 'network_calls_made', 'strategy_models_called', 'orders_sent'):
            if count(report.get(key)) != 0:
                invalid()
    return recorded


def matched_side(actual, recorded, model, mode):
    if not isinstance(actual, dict) or not isinstance(recorded, dict):
        invalid()
    changes, cap = MODELS[model]
    expected_context = 'prior_interior_available' if mode == 'pav' else 'prior_interior_aligned'
    if (actual.get('status') != 'simulated' or actual.get('variant_id') != model
            or count(actual.get('consecutive_quote_changes')) != changes
            or actual.get('m1_entry_context') != expected_context
            or (cap is None and actual.get('max_entry_spread_cost_usd_model', object()) is not None)
            or (cap is not None and not close(actual.get('max_entry_spread_cost_usd_model'), cap))
            or any(actual.get(k, object()) is not None for k in ('commission', 'swap', 'net_pnl'))):
        invalid()
    for source, published in (('simulated_entries', 'entries'), ('cash_profit_exits', 'profit'),
                              ('cash_loss_exits', 'loss')):
        if count(actual.get(source)) != count(recorded.get(published)):
            invalid()
    for source, published in (('position_open_at_sample_end', 'open_at_end'),
                              ('simulated_three_consecutive_loss_latch', 'latch')):
        if type(actual.get(source)) is not bool or actual[source] is not recorded.get(published):
            invalid()
    if (actual['simulated_entries'] != actual['cash_profit_exits'] + actual['cash_loss_exits']
            + int(actual['position_open_at_sample_end'])
            or not close(actual.get('gross_total_mark_usd_model'), recorded.get('gross_total_mark'))):
        invalid()


def opportunities(side, rows):
    directions = side.get('previous_observed_bid_directions')
    raw = side.get('raw_signals_before_all_entry_filters')
    signals = side.get('signal_counts')
    if not all(isinstance(x, dict) for x in (directions, raw, signals)):
        invalid()
    eligible = count(side.get('eligible_current_minute_buckets'), rows)
    clean_directions = {key: count(directions.get(key), rows) for key in ('up', 'down', 'flat')}
    buy, sell = (count(signals.get(key), rows) for key in ('buy', 'sell'))
    missing, available, aligned = (count(raw.get(key), rows) for key in
                                   ('previous_unavailable', 'previous_available',
                                    'previous_available_and_direction_aligned'))
    if (sum(clean_directions.values()) != eligible or missing + available != buy + sell
            or buy + sell > rows or aligned > available
            or (eligible == 0 and available != 0)
            or (clean_directions['up'] + clean_directions['down'] == 0 and aligned != 0)):
        invalid()
    return {'eligible_current_minute_buckets': eligible, 'previous_observed_bid_directions': clean_directions,
            'raw_signal_counts': {'buy': buy, 'sell': sell, 'previous_unavailable': missing,
                                  'previous_available': available,
                                  'previous_available_and_direction_aligned': aligned}}


def validate_window(sample, saved, published):
    if saved.get('status') != 'compared' or published.get('status') != 'compared':
        invalid()
    rows = count(saved.get('returned_rows'), 1999)
    if rows < 2 or rows != PUBLISHED_WINDOW_COUNTS[sample][0]:
        invalid()
    return (rows, index_exact(saved.get('pairs'), 'model_id', MODELS),
            index_exact(published.get('pairs'), 'model', MODELS))


def extract_pair(sample, model, pair, prior, rows):
    if (pair.get('status') != 'compared' or pair.get('baseline_aggregate_matched') is not True
            or prior.get('baseline_matched') is not True):
        invalid()
    sides = {}
    for full_mode, short_mode in MODES:
        side, ref = pair.get(full_mode), prior.get(short_mode)
        checked(f'pair/{sample}/{model}/{short_mode}', matched_side, side, ref, model, short_mode)
        sides[short_mode] = side
    pav, pda = sides['pav'], sides['pda']
    delta = finite(pda['gross_total_mark_usd_model']) - finite(pav['gross_total_mark_usd_model'])
    if (not close(delta, pair.get('aligned_minus_matched_gross_total_mark_usd_model'))
            or not close(delta, prior.get('aligned_minus_marked'))):
        invalid()
    control, filtered = opportunities(pav, rows), opportunities(pda, rows)
    if control != filtered:
        invalid()
    context = (control['eligible_current_minute_buckets'], control['previous_observed_bid_directions'])
    raw = control['raw_signal_counts']
    if model == 'original_3_changes' and (raw['buy'], raw['sell']) != PUBLISHED_WINDOW_COUNTS[sample][1:]:
        invalid()
    unavailable_pav = count(pav.get('signals_blocked_by_missing_previous_interior_minute'), rows)
    unavailable_pda = count(pda.get('signals_blocked_by_missing_previous_interior_minute'), rows)
    direction_pav = count(pav.get('signals_blocked_by_previous_interior_direction'), rows)
    direction_pda = count(pda.get('signals_blocked_by_previous_interior_direction'), rows)
    if (unavailable_pav > raw['previous_unavailable'] or unavailable_pda > raw['previous_unavailable']
            or direction_pav != 0
            or direction_pda > raw['previous_available'] - raw['previous_available_and_direction_aligned']
            or pav['simulated_entries'] > raw['previous_available']
            or pda['simulated_entries'] > raw['previous_available_and_direction_aligned']):
        invalid()
    return ({
        'model_id': model, 'raw_signals_before_all_entry_filters': raw,
        'simulated_entries': {'previous_available_only': pav['simulated_entries'],
                              'previous_direction_aligned': pda['simulated_entries']},
        'signals_blocked_at_entry_decision': {
            'previous_unavailable_control': unavailable_pav,
            'previous_unavailable_aligned': unavailable_pda,
            'previous_direction_aligned_mode': direction_pda,
        },
    }, context)


def extract(summary, reference):
    """Return only fixed aggregate counts from two already-existing JSON reports."""
    recorded = checked('header', validate_header, summary, reference)
    saved_windows = checked('saved_window_set', index_exact, summary.get('windows'), 'id', SAMPLES)
    published_windows = checked('published_window_set', index_exact, recorded.get('windows'), 'id', SAMPLES)
    clean_windows = []
    for sample in SAMPLES:
        saved, published = saved_windows[sample], published_windows[sample]
        rows, saved_pairs, published_pairs = checked(
            f'window/{sample}', validate_window, sample, saved, published)
        clean_models = []
        window_context = None
        for model in MODELS:
            clean_model, context = checked(f'pair/{sample}/{model}', extract_pair,
                                           sample, model, saved_pairs[model], published_pairs[model], rows)
            if window_context is not None and context != window_context:
                raise AggregateMismatch(f'window/{sample}/cross_model_context')
            window_context = context
            clean_models.append(clean_model)
        clean_windows.append({'id': sample, 'returned_rows': rows,
                              'eligible_current_minute_buckets': window_context[0],
                              'previous_observed_bid_directions': window_context[1], 'models': clean_models})
    return {
        'schema': 'xauusd-m1-saved-signal-counts/v1',
        'status': 'completed_aggregate_extraction_no_replay',
        'source_report': REFERENCE.name, 'matched_scenario_pairs': 12,
        'published_row_and_original_raw_signal_counts_matched': True,
        'history_calls_made': 0, 'network_calls_made': 0, 'strategy_models_called': 0, 'orders_sent': 0,
        'tick_replays': 0, 'execution_authorized': False, 'profitability_proven': False,
        'commission': None, 'fees': None, 'swap': None, 'net_pnl': None,
        'blind_holdout_or_out_of_sample': False,
        'aggregate_comparison_does_not_verify_raw_archive_hash': True,
        'raw_signals_are_not_entries_and_precede_position_latch_spread_and_direction_filters': True,
        'windows': clean_windows,
        'limitations': [
            'Only existing saved aggregates were read; no quotes, fills, account records or costs were checked',
            'The six sparse samples and two overlapping models are not a continuous account or unseen test',
            'Directions use observed previous-minute first and last bid; complete feed and true candles are unverified',
            'Broker-label time and the actual research execution dates are not independently verified',
            'A matching aggregate is not a cryptographic identity check on raw archives',
            'Commission, fees, swap, slippage and net remain unknown; no trading is authorized',
        ],
    }


def main():
    if len(sys.argv) != 1:
        print('This aggregate-only tool takes no arguments.', file=sys.stderr)
        return 2
    try:
        if has_symlink(OUTPUT.parent):
            print('Aggregate output directory is unsafe; no output written.', file=sys.stderr)
            return 1
        if OUTPUT.exists() or OUTPUT.is_symlink():
            print('Aggregate output already exists; it will not be overwritten.', file=sys.stderr)
            return 1
    except OSError:
        print('Aggregate output directory is unsafe or unreadable; no output written.', file=sys.stderr)
        return 1
    try:
        summary = load_json(SUMMARY)
    except FileNotFoundError:
        print('Saved M1 summary is missing; no output written.', file=sys.stderr)
        return 1
    except Exception:
        print('Saved M1 summary is unsafe, unreadable, or invalid; no output written.', file=sys.stderr)
        return 1
    try:
        reference = load_json(REFERENCE)
    except FileNotFoundError:
        print('Published M1 reference is missing; no output written.', file=sys.stderr)
        return 1
    except Exception:
        print('Published M1 reference is unsafe, unreadable, or invalid; no output written.', file=sys.stderr)
        return 1
    try:
        report = extract(summary, reference)
        encoded = json.dumps(report, indent=2, allow_nan=False) + '\n'
    except AggregateMismatch as exc:
        # Labels are assembled from fixed constants, never from saved data.
        print(f'Saved aggregate/reference mismatch ({exc}); no output written.', file=sys.stderr)
        return 1
    except Exception:
        print('Saved aggregate/reference validation failed; no output written.', file=sys.stderr)
        return 1
    created = False
    try:
        fd = os.open(OUTPUT, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(encoded)
    except OSError:
        if created:
            print('Aggregate output may be incomplete; leave it unchanged and report the failure.', file=sys.stderr)
        else:
            print('Cannot create exclusive aggregate output; no existing file overwritten.', file=sys.stderr)
        return 1
    print(encoded, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
