"""Compare old vs boundary-managed scenarios on six existing saved samples.

The original simulation ignored the entire final partial minute, including for
positions already open. This offline comparison checks exits/marks during that
minute, without generating any new signals or entries there. It is not broker
execution, a blind test, a continuous account, or authority to trade.
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
    REFERENCE,
    ROOT,
    WINDOWS,
    baseline_matches,
    read_json,
    same_number,
    summarize,
    valid_reference,
)
from xauusd_offline_next_tests import (  # noqa: E402
    BASELINE,
    CANDIDATE,
    OLD_CASH_REPORT,
    OLD_RAW,
    OLD_SIGNAL_REPORT,
    OLD_WINDOW,
    PREVIOUS_VARIANTS,
    previous_original_matches,
    read_previous_candidates,
    validate_additional_response,
)
from xauusd_sparse_screen_v2 import validate_rows  # noqa: E402

from trade_mcp.tick_cash_replay import economics, replay_cash_scenario  # noqa: E402

NEXT_TEST_REPORT = ROOT / 'reports/research/20261122T122500Z-xauusd-offline-next-tests.json'
OUTPUT = ROOT / 'logs/research-inputs/xauusd-final-boundary-replay-v1'


def extra_candidate_reference(path):
    """Only copy the fixed aggregate fields from the sanitized older report."""
    raw = read_json(path, limit=1_000_000)
    if not isinstance(raw, dict) or not isinstance(raw.get('result'), dict):
        raise ValueError('prior_report_invalid')
    result = raw['result']
    if (result.get('status') != 'completed_all_new_tests'
            or result.get('new_tests_completed') != 25 or result.get('all_baselines_reproduced') is not True
            or not isinstance(result.get('windows'), list)):
        raise ValueError('prior_report_invalid')
    windows = [w for w in result['windows'] if isinstance(w, dict) and w.get('id') == OLD_WINDOW[0]]
    if len(windows) != 1 or not isinstance(windows[0].get('tests'), list):
        raise ValueError('prior_report_invalid')
    matches = [x for x in windows[0]['tests'] if isinstance(x, dict) and x.get('variant') == CANDIDATE[0]]
    if len(matches) != 1:
        raise ValueError('prior_report_invalid')
    item = matches[0]
    for key in ('entries', 'profit', 'loss'):
        if type(item.get(key)) is not int or not 0 <= item[key] <= 2000:
            raise ValueError('prior_report_invalid')
    gross = item.get('gross_total')
    if isinstance(gross, bool) or not isinstance(gross, (int, float)) or not math.isfinite(gross):
        raise ValueError('prior_report_invalid')
    open_count = item['entries'] - item['profit'] - item['loss']
    if open_count not in (0, 1):
        raise ValueError('prior_report_invalid')
    return {'entries': item['entries'], 'profit_exits': item['profit'], 'loss_exits': item['loss'],
            'gross_total_mark': gross, 'open_at_end': bool(open_count)}


def candidate_matches(scenario, reference):
    counts = scenario['counts']
    expected_counts = {'simulated_entries': 'entries', 'cash_profit_exits': 'profit_exits',
                       'cash_loss_exits': 'loss_exits'}
    if any(counts.get(actual, 0) != reference.get(expected) for actual, expected in expected_counts.items()):
        return False
    if scenario['position_open_at_sample_end'] is not reference.get('open_at_end'):
        return False
    if 'latch' in reference and scenario['simulated_three_consecutive_loss_latch'] is not reference['latch']:
        return False
    return same_number(scenario['gross_total_mark_usd_model'], reference.get('gross_total_mark'))


def comparison(variant, legacy, managed):
    """Emit only sanitized counts and hypothetical USD marks; never quote data."""
    old = summarize(variant, legacy, legacy)
    new = summarize(variant, managed, legacy)
    old.pop('gross_total_mark_delta_vs_original_usd_model')
    delta = new.pop('gross_total_mark_delta_vs_original_usd_model')
    old['position_open_at_last_interior_quote'] = old.pop('position_open_at_sample_end')
    new['position_open_at_last_observed_quote'] = new.pop('position_open_at_sample_end')
    new['final_boundary_management'] = managed['final_boundary_management']
    return {'variant_id': variant[0], 'status': 'compared', 'legacy': old, 'boundary_managed': new,
            'gross_total_delta_vs_legacy_usd_model': delta}


def mark_skipped(window, reason):
    window['status'] = reason
    window['variants'] = [{'variant_id': variant[0], 'status': 'not_compared_' + reason}
                          for variant in (BASELINE, CANDIDATE)]


def run_comparisons(archive=None, old_raw=None, economics_file=None, v2_reference=None,
                    old_cash_report=None, old_signal_report=None, previous_variants=None,
                    next_report=None):
    archive = ARCHIVE if archive is None else archive
    old_raw = OLD_RAW if old_raw is None else old_raw
    economics_file = ECONOMICS if economics_file is None else economics_file
    v2_reference = REFERENCE if v2_reference is None else v2_reference
    old_cash_report = OLD_CASH_REPORT if old_cash_report is None else old_cash_report
    old_signal_report = OLD_SIGNAL_REPORT if old_signal_report is None else old_signal_report
    previous_variants = PREVIOUS_VARIANTS if previous_variants is None else previous_variants
    next_report = NEXT_TEST_REPORT if next_report is None else next_report
    report = {
        'schema': 'xauusd-final-boundary-replay/v1', 'status': 'started',
        'history_calls_made': 0, 'network_calls_made': 0, 'models_called': 0, 'orders_sent': 0,
        'execution_authorized': False, 'profitability_proven': False,
        'commission': None, 'swap': None, 'net_pnl': None,
        'complete_tick_coverage_verified': False, 'utc_verified': False,
        'baseline_check_is_metrics_not_raw_archive_hash': True,
        'additional_sample_requested_bounds_independently_verified': False,
        'economics_symbol_binding_independently_verified': False,
        'independent_hypothetical_windows_not_continuous': True,
        'blind_holdout_or_out_of_sample': False,
        'planned_comparisons': 2 * (1 + len(WINDOWS)),
        'completed_comparisons': 0, 'windows': [],
        'limitations': [
            'Earlier published marks stopped at the last interior quote, not the final observed sample quote',
            'The new mode only marks/exits an existing position in the final partial minute; no entry signals there',
            'Existing tick archives may be incomplete; the last observed quote is not necessarily a true window end',
            'The old baseline and candidate outcomes were already known; this is not a blind holdout',
            'Separate windows start flat and reset only a hypothetical latch, not the real persistent latch',
            'Assumed next-quote fills, current contract snapshot, fees, slippage and account risk are not verified',
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
        old_refs = (read_json(old_cash_report, limit=1_000_000),
                    read_json(old_signal_report, limit=1_000_000))
    except Exception:
        old_refs = None
    try:
        v2_refs = valid_reference(read_json(v2_reference, limit=1_000_000))
    except Exception:
        v2_refs = {}
    try:
        candidates = read_previous_candidates(previous_variants)
    except Exception:
        candidates = {}
    try:
        extra_candidate = extra_candidate_reference(next_report)
    except Exception:
        extra_candidate = None

    samples = [(OLD_WINDOW, old_raw, True)] + [
        ((name, start, end), archive / (name + '-raw-response.json'), False)
        for name, start, end in WINDOWS]
    for (name, start, end), path, is_old in samples:
        window = {'id': name, 'status': 'started', 'legacy_original_reproduced': False, 'variants': []}
        report['windows'].append(window)
        if (is_old and old_refs is None) or (not is_old and name not in v2_refs):
            mark_skipped(window, 'prior_baseline_reference_unavailable')
            continue
        if path.is_symlink() or not path.is_file():
            mark_skipped(window, 'saved_tick_archive_missing_or_symlink')
            continue
        try:
            raw = read_json(path)
            data = validate_additional_response(raw) if is_old else validate_rows(raw, start, end)
            if data['kind'] != 'rows':
                raise ValueError('no_saved_rows')
            rows = data['rows']
        except Exception:
            mark_skipped(window, 'saved_tick_archive_invalid_or_out_of_bounds')
            continue
        try:
            original = replay_cash_scenario(rows, info)
            reproduced = (previous_original_matches(original, *old_refs) if is_old
                          else baseline_matches(original, v2_refs[name], len(rows)))
            if not reproduced:
                mark_skipped(window, 'prior_baseline_mismatch')
                continue
            window['legacy_original_reproduced'] = True
        except Exception:
            mark_skipped(window, 'legacy_original_replay_failed')
            continue
        window['status'] = 'compared'
        for variant in (BASELINE, CANDIDATE):
            try:
                _, changes, cap = variant
                if variant == BASELINE:
                    legacy = original
                else:
                    prior = extra_candidate if is_old else candidates.get(name)
                    if prior is None:
                        window['status'] = 'some_variants_not_compared'
                        window['variants'].append({'variant_id': variant[0],
                                                   'status': 'not_compared_prior_candidate_reference_unavailable'})
                        continue
                    legacy = replay_cash_scenario(rows, info, consecutive_changes=changes,
                                                  max_spread_cost_usd=cap)
                    if not candidate_matches(legacy, prior):
                        window['status'] = 'some_variants_not_compared'
                        window['variants'].append({'variant_id': variant[0],
                                                   'status': 'not_compared_prior_candidate_mismatch'})
                        continue
                managed = replay_cash_scenario(rows, info, consecutive_changes=changes,
                                               max_spread_cost_usd=cap, manage_final_boundary=True)
                window['variants'].append(comparison(variant, legacy, managed))
                report['completed_comparisons'] += 1
            except Exception:
                window['status'] = 'some_variants_not_compared'
                window['variants'].append({'variant_id': variant[0], 'status': 'offline_replay_failed'})
    if report['completed_comparisons'] == report['planned_comparisons']:
        report['status'] = 'completed_all_comparisons'
    elif report['completed_comparisons']:
        report['status'] = 'completed_partial_comparisons'
    else:
        report['status'] = 'no_verified_saved_samples'
    return report


def main():
    try:
        OUTPUT.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        print('Boundary comparison output exists; not overwriting or repeating.')
        return 1
    except OSError:
        print('Cannot create private output; no network calls made.')
        return 1
    report = run_comparisons()
    fd = os.open(OUTPUT / 'summary.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report['status'] == 'completed_all_comparisons' else 1


if __name__ == '__main__':
    raise SystemExit(main())
