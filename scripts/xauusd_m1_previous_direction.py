"""Compare two fixed M1-context hypotheses on existing archives, entirely offline.

This exploratory fixed-output job was reported completed once and is closed.
For each sample and model, an unrestricted managed replay checked published
aggregate metrics only. Two new paths shared the same adjacent previous-interior
minute availability rule; one also required the prior observed bid direction to
match the signal. Nothing here connects to MT5, places orders, or changes the
live strategy or its protections. Never rerun this job on real archives.
"""
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from compare_xauusd_v2_offline_variants import (  # noqa: E402
    ARCHIVE,
    ECONOMICS,
    ECONOMICS_FIELDS,
    ROOT,
    WINDOWS,
    read_json,
    same_number,
    summarize,
)
from xauusd_offline_next_tests import OLD_RAW, OLD_WINDOW, validate_additional_response  # noqa: E402
from xauusd_saved_summary_review import (  # noqa: E402
    BOUNDARY_PATH,
    CANDIDATE,
    ORIGINAL,
    SAMPLES,
    close,
    exact_count,
    invalid,
    money,
    result,
    same_side,
    side,
)
from xauusd_sparse_screen_v2 import validate_rows  # noqa: E402

from trade_mcp.tick_cash_replay import economics, replay_cash_scenario  # noqa: E402

OUTPUT = ROOT / 'logs/research-inputs/xauusd-m1-previous-direction-v1'
# The one-shot job completed on the workstation. Refuse it even when a fresh
# checkout cannot see the original local output marker. Do not disable this
# guard to repeat the job; synthetic unit tests alone override it in memory.
M1_RESEARCH_RUN_CLOSED = True
BOUNDARY_REPORT_ID = BOUNDARY_PATH.stem
# These are the two corrected historical models, not live strategy changes.
MODELS = ((ORIGINAL, 3, None), (CANDIDATE, 2, 1.0))
CONTEXT_MODES = ('prior_interior_available', 'prior_interior_aligned')


def published_managed_references(path):
    """Validate the completed sanitized report; return only fixed aggregate fields."""
    raw = read_json(path, limit=1_000_000)
    report = result(raw, BOUNDARY_REPORT_ID, 'xauusd-final-boundary-replay/v1',
                    'completed_all_comparisons')
    if (not exact_count(report, 'planned_comparisons', 12)
            or not exact_count(report, 'completed_comparisons', 12)
            or not exact_count(report, 'failures', 0)
            or not exact_count(report, 'history_calls_made', 0)
            or report.get('all_legacies_reproduced') is not True):
        invalid()
    expected = {(sample, model[0]) for sample in SAMPLES for model in MODELS}
    comparisons = report.get('comparisons')
    if not isinstance(comparisons, list) or len(comparisons) != len(expected):
        invalid()
    refs = {}
    for item in comparisons:
        if not isinstance(item, dict):
            invalid()
        key = (item.get('sample'), item.get('variant'))
        if key not in expected or key in refs:
            invalid()
        old_raw, new_raw = item.get('legacy'), item.get('managed')
        old = side(old_raw, profit='profit', loss='loss', gross='gross_total', open_field='open_at_interior')
        new = side(new_raw, profit='profit', loss='loss', gross='gross_total',
                   open_field='open_at_last_observed')
        if (type(old_raw.get('latch')) is not bool or type(new_raw.get('latch')) is not bool
                or old['entries'] != new['entries']
                or not close(new['gross_total_usd_model'] - old['gross_total_usd_model'],
                             money(item.get('delta_vs_legacy')))
                or (not old['open'] and (not same_side(old, new)
                                         or old_raw['latch'] is not new_raw['latch']))):
            invalid()
        refs[key] = dict(new, latch=new_raw['latch'])
    if set(refs) != expected:
        invalid()
    return refs


def managed_reference_matches(scenario, ref):
    counts = scenario['counts']
    return (scenario['schema'] == 'xauusd-next-quote-cash-scenario/v2'
            and counts.get('simulated_entries', 0) == ref['entries']
            and counts.get('cash_profit_exits', 0) == ref['profit_exits']
            and counts.get('cash_loss_exits', 0) == ref['loss_exits']
            and scenario['position_open_at_sample_end'] is ref['open']
            and scenario['simulated_three_consecutive_loss_latch'] is ref['latch']
            and same_number(scenario['gross_total_mark_usd_model'], ref['gross_total_usd_model'])
            and scenario['commission'] is None and scenario['swap'] is None and scenario['net_pnl'] is None)


def checked_context_summary(model, scenario, mode):
    """Copy only aggregate outputs and check the internal accounting invariants."""
    if (scenario['execution_authorized'] is not False or scenario['profitability_proven'] is not False
            or scenario['net_pnl'] is not None or scenario['commission'] is not None
            or scenario['swap'] is not None or scenario['spread_included'] is not True):
        raise ValueError('unsafe_or_unexpected_scenario')
    details = scenario['research_variant']
    if (details['m1_entry_context'] != mode
            or details['previous_minute_basis'] != 'adjacent_interior_first_and_last_observed_bid_at_least_two_quotes'
            or details['signal_replay_counts_before_entry_context_filter'] is not True):
        raise ValueError('unexpected_context')
    value = summarize(model, scenario, scenario)
    value.pop('gross_total_mark_delta_vs_original_usd_model')
    entries = value['simulated_entries']
    wins, losses = value['cash_profit_exits'], value['cash_loss_exits']
    opened = value['position_open_at_sample_end']
    if (any(type(x) is not int or not 0 <= x <= 2000 for x in (entries, wins, losses))
            or type(opened) is not bool or entries != wins + losses + int(opened)
            or type(value['simulated_three_consecutive_loss_latch']) is not bool):
        raise ValueError('scenario_accounting_mismatch')
    gross_open = value['gross_open_mark_usd_model']
    if ((opened and gross_open is None) or (not opened and gross_open is not None)
            or not same_number(value['gross_total_mark_usd_model'],
                               value['gross_realized_usd_model'] + (gross_open or 0))):
        raise ValueError('scenario_mark_mismatch')
    eligible = details['eligible_current_minute_buckets']
    nonflat = details['nonflat_previous_interior_minute_buckets']
    directions = details['previous_observed_bid_directions']
    signals = details['raw_signals_before_position_latch_spread_or_context_filters']
    direction_keys = ('up', 'down', 'flat')
    signal_keys = ('previous_unavailable', 'previous_available', 'previous_available_and_direction_aligned')
    all_counts = [eligible, nonflat, *(directions[k] for k in direction_keys), *(signals[k] for k in signal_keys)]
    if (any(type(x) is not int or not 0 <= x <= 2000 for x in all_counts)
            or eligible != sum(directions[k] for k in direction_keys)
            or nonflat != directions['up'] + directions['down']
            or signals['previous_available_and_direction_aligned'] > signals['previous_available']
            or signals['previous_unavailable'] + signals['previous_available']
            != sum(value['signal_counts'].values())):
        raise ValueError('context_accounting_mismatch')
    value['m1_entry_context'] = mode
    value['eligible_current_minute_buckets'] = eligible
    value['previous_observed_bid_directions'] = {key: directions[key] for key in direction_keys}
    value['raw_signals_before_all_entry_filters'] = {key: signals[key] for key in signal_keys}
    value['signals_blocked_by_missing_previous_interior_minute'] = scenario['counts'].get(
        'signals_blocked_by_missing_previous_interior_minute', 0)
    value['signals_blocked_by_previous_interior_direction'] = scenario['counts'].get(
        'signals_blocked_by_previous_interior_direction', 0)
    boundary = scenario['final_boundary_management']
    value['final_boundary_management'] = {key: boundary[key] for key in
                                          ('boundary_quotes_seen', 'position_marked_quotes',
                                           'cash_profit_exits', 'cash_loss_exits', 'pending_entries_expired')}
    return value


def compare_pair(model, rows, info, ref):
    """Check the previously published baseline, then run the two new matched paths."""
    _, changes, spread = model
    kwargs = {'consecutive_changes': changes, 'max_spread_cost_usd': spread, 'manage_final_boundary': True}
    baseline = replay_cash_scenario(rows, info, **kwargs)
    if not managed_reference_matches(baseline, ref):
        return {'model_id': model[0], 'status': 'prior_managed_aggregate_mismatch',
                'baseline_aggregate_matched': False}
    result_pair = {'model_id': model[0], 'baseline_aggregate_matched': True,
                   'published_full_interior_reference': {
                       'simulated_entries': ref['entries'], 'cash_profit_exits': ref['profit_exits'],
                       'cash_loss_exits': ref['loss_exits'], 'gross_total_mark_usd_model': ref['gross_total_usd_model'],
                       'position_open_at_last_observed': ref['open'],
                       'simulated_three_consecutive_loss_latch': ref['latch'],
                   }}
    try:
        result_pair.update(compare_contexts(model, rows, info, kwargs))
    except Exception:
        # Preserve the successful verification count, but never leak raw errors.
        result_pair['status'] = 'offline_comparison_failed'
    return result_pair


def compare_contexts(model, rows, info, kwargs):
    summaries = []
    for mode in CONTEXT_MODES:
        scenario = replay_cash_scenario(rows, info, m1_context=mode, **kwargs)
        summaries.append(checked_context_summary(model, scenario, mode))
    matched, aligned = summaries
    for key in ('signal_counts', 'eligible_current_minute_buckets', 'previous_observed_bid_directions',
                'raw_signals_before_all_entry_filters'):
        if matched[key] != aligned[key]:
            raise ValueError('matched_opportunity_accounting_mismatch')
    signals = matched['raw_signals_before_all_entry_filters']
    if matched['eligible_current_minute_buckets'] == 0:
        interpretation = 'no_eligible_adjacent_previous_minute'
    elif signals['previous_available'] == 0:
        interpretation = 'no_raw_signals_in_eligible_minutes'
    elif not matched['simulated_entries'] and not aligned['simulated_entries']:
        interpretation = 'no_simulated_entries_in_either_mode'
    elif not aligned['simulated_entries']:
        interpretation = 'direction_aligned_mode_had_no_simulated_entries'
    else:
        interpretation = 'exploratory_matched_comparison_not_validation'
    delta = aligned['gross_total_mark_usd_model'] - matched['gross_total_mark_usd_model']
    if not math.isfinite(delta):
        raise ValueError('nonfinite_pair_delta')
    if not matched['simulated_entries'] and not aligned['simulated_entries']:
        delta = None  # A no-trade outcome must not look like evidence of improvement.
    return {'status': 'compared', 'previous_available_only': matched,
            'previous_direction_aligned': aligned,
            'aligned_minus_matched_gross_total_mark_usd_model': delta,
            'interpretation': interpretation}


def skipped_pairs(reason):
    return [{'model_id': model[0], 'status': 'not_compared_' + reason} for model in MODELS]


def file_presence(path, *, parent_symlink=False):
    """Report only fixed input availability, without reading a file or exposing its contents."""
    try:
        if parent_symlink or path.is_symlink():
            return 'symlink_refused'
        return 'present_unverified' if path.is_file() else 'missing_or_not_regular'
    except OSError:
        return 'presence_unavailable'


def run_experiment(archive=None, old_raw=None, economics_file=None, boundary_reference=None):
    """Return sanitized aggregates; never create a marker or connect externally."""
    archive = ARCHIVE if archive is None else archive
    old_raw = OLD_RAW if old_raw is None else old_raw
    economics_file = ECONOMICS if economics_file is None else economics_file
    boundary_reference = BOUNDARY_PATH if boundary_reference is None else boundary_reference
    samples = [(OLD_WINDOW, old_raw, True)] + [
        ((name, start, end), archive / (name + '-raw-response.json'), False)
        for name, start, end in WINDOWS]
    archive_is_symlink = archive.is_symlink()
    inventory = {'economics': file_presence(economics_file),
                 'published_boundary_reference': file_presence(boundary_reference),
                 'saved_samples': {name: file_presence(path, parent_symlink=not is_old and archive_is_symlink)
                                   for (name, _start, _end), path, is_old in samples}}
    report = {
        'schema': 'xauusd-m1-previous-direction/v1', 'status': 'started',
        'history_calls_made': 0, 'network_calls_made': 0, 'strategy_models_called': 0, 'orders_sent': 0,
        'execution_authorized': False, 'profitability_proven': False,
        'commission': None, 'fees': None, 'swap': None, 'net_pnl': None,
        'spread_included': True, 'additional_slippage_modeled': False,
        'prior_hypothesis_selection_after_results': True, 'blind_holdout_or_out_of_sample': False,
        'independent_hypothetical_windows_not_continuous': True,
        'complete_tick_coverage_verified': False, 'utc_verified': False,
        'historical_contract_rules_verified': False, 'economics_symbol_binding_independently_verified': False,
        'additional_sample_requested_bounds_independently_verified': False,
        'baseline_check_is_metrics_not_raw_archive_hash': True,
        'planned_paired_comparisons': len(SAMPLES) * len(MODELS), 'completed_paired_comparisons': 0,
        'baseline_checks_attempted': 0, 'baseline_checks_matched': 0,
        'input_presence_only_not_validation': inventory, 'windows': [],
        'limitations': [
            'Post-hoc exploratory hypothesis on six previously examined short samples; not a blind holdout',
            'The first and last returned minutes never generate signals; the first interior minute supplies no context',
            'Prior direction uses first and last observed bid in an adjacent interior minute with at least two quotes',
            'Two quotes do not establish full tick coverage or the true candle open and close',
            'Both new paths use identical context availability; only one requires aligned nonflat previous direction',
            'An unrestricted replay checks the already-published managed baseline metrics, not the raw archive hash',
            'Published unrestricted baselines have a wider opportunity set and are not matched-control deltas',
            'Each path/window starts flat with an independent simulated latch; different entries change later state',
            'Next-quote fills, gross cash exits with overshoot, and current metadata are assumptions, not broker results',
            'Spread is already included; actual commission, fees, swap, execution, slippage and net remain unknown',
            'A zero from no simulated entries is not evidence of profitability; open marks are unrealized',
            'No live M1 strategy, account-risk control, persistent latch, order, or broker protection is changed',
        ],
    }
    try:
        info = read_json(economics_file, limit=100_000)
        if not isinstance(info, dict) or set(info) != ECONOMICS_FIELDS:
            raise ValueError('invalid_economics')
        economics(info)
    except Exception:
        report['status'] = 'blocked_saved_economics_unavailable_or_invalid'
        return report
    try:
        refs = published_managed_references(boundary_reference)
    except Exception:
        report['status'] = 'blocked_published_reference_unavailable_or_invalid'
        return report

    for (name, start, end), path, is_old in samples:
        window = {'id': name, 'status': 'started', 'pairs': []}
        report['windows'].append(window)
        if (not is_old and archive.is_symlink()) or path.is_symlink() or not path.is_file():
            window.update(status='saved_tick_archive_missing_or_symlink',
                          pairs=skipped_pairs('saved_tick_archive_missing_or_symlink'))
            continue
        try:
            response = read_json(path)
            data = validate_additional_response(response) if is_old else validate_rows(response, start, end)
            if data['kind'] != 'rows':
                raise ValueError('no_saved_rows')
            rows = data['rows']
        except Exception:
            window.update(status='saved_tick_archive_invalid_or_out_of_bounds',
                          pairs=skipped_pairs('saved_tick_archive_invalid_or_out_of_bounds'))
            continue
        window['returned_rows'] = len(rows)
        for model in MODELS:
            report['baseline_checks_attempted'] += 1
            try:
                pair = compare_pair(model, rows, info, refs[(name, model[0])])
                if pair['baseline_aggregate_matched']:
                    report['baseline_checks_matched'] += 1
                if pair['status'] == 'compared':
                    report['completed_paired_comparisons'] += 1
                window['pairs'].append(pair)
            except Exception:
                # Do not print exception text, archive contents, account information or economics.
                window['pairs'].append({'model_id': model[0], 'status': 'offline_comparison_failed'})
        window['status'] = ('compared' if all(p['status'] == 'compared' for p in window['pairs'])
                            else 'some_pairs_not_compared')
    completed, planned = report['completed_paired_comparisons'], report['planned_paired_comparisons']
    report['status'] = ('completed_all_paired_comparisons' if completed == planned
                        else 'completed_partial_paired_comparisons' if completed
                        else 'no_verified_saved_pairs')
    return report


def main():
    if M1_RESEARCH_RUN_CLOSED:
        print('This M1 comparison already completed; no replay or network calls will be made.')
        return 1
    # Preserve the no-overwrite logic for synthetic unit tests of the historical job.
    if OUTPUT.exists() or OUTPUT.is_symlink():
        print('New M1 comparison output already exists; not overwriting or repeating.')
        return 1
    report = run_experiment()
    print(json.dumps(report, indent=2, allow_nan=False))
    if not report['completed_paired_comparisons']:
        print('No verified comparison; no new output directory was created.', file=sys.stderr)
        return 1
    try:
        OUTPUT.mkdir(mode=0o700, parents=True, exist_ok=False)
        fd = os.open(OUTPUT / 'summary.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write('\n')
    except OSError:
        print('Cannot create exclusive private output; do not remove any marker or repeat.', file=sys.stderr)
        return 1
    return 0 if report['status'] == 'completed_all_paired_comparisons' else 1


if __name__ == '__main__':
    raise SystemExit(main())
