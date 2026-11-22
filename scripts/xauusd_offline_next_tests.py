"""Run bounded follow-up hypotheses on SIX existing saved samples, completely offline.

The prior five v2 windows are reused only for new parameter hypotheses; their
original baseline is replayed solely to verify continuity with the saved report.
A candidate selected after those outcomes is also evaluated on the separately
archived earlier sample. That sample's original result was already known, so
none of this is a blind holdout, a profitability claim or authority to trade.
"""
import json
import math
import os
import sys
from datetime import datetime
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
from xauusd_sparse_screen_v2 import validate_rows  # noqa: E402

from trade_mcp.mcpclient import McpHttpClient  # noqa: E402
from trade_mcp.tick_cash_replay import economics, replay_cash_scenario  # noqa: E402

OLD_RAW = ROOT / 'logs/research-inputs/xauusd-ticks-corrected-20261122T073154Z/raw-response.json'
OLD_CASH_REPORT = ROOT / 'reports/research/20261122T075608Z-xauusd-cash-offline.json'
OLD_SIGNAL_REPORT = ROOT / 'reports/research/20261122T073857Z-xauusd-signal-replay.json'
PREVIOUS_VARIANTS = ROOT / 'reports/research/20261122T120000Z-xauusd-v2-offline-variants.json'
OUTPUT = ROOT / 'logs/research-inputs/xauusd-offline-next-tests-v1'
OLD_WINDOW = ('additional_sample', '2026-11-20T19:23:00', '2026-11-20T19:28:00')
BASELINE = ('original_3_changes', 3, None)
CANDIDATE = ('2_changes_spread_cap_1_00', 2, 1.00)
# Fixed before execution; 0.75 is the midpoint between the two earlier caps.
# This choice is post-hoc and NOT a validation or tuning recommendation.
NEW_VARIANTS = (
    ('2_changes_no_spread_cap', 2, None),
    ('2_changes_spread_cap_0_50', 2, .50),
    ('2_changes_spread_cap_0_75', 2, .75),
    ('3_changes_spread_cap_0_75', 3, .75),
)


def validate_additional_response(response):
    """Accept a separate earlier archive without inventing its timezone or request bounds."""
    if not isinstance(response, dict) or response.get('isError'):
        raise ValueError('invalid_saved_response')
    data = McpHttpClient.structured_content(response)
    if not isinstance(data, dict) or not isinstance(data.get('history'), list) or len(data['history']) < 2:
        raise ValueError('invalid_saved_history')
    rows = data['history']
    first = datetime.fromisoformat(rows[0]['time_ms'])
    last = datetime.fromisoformat(rows[-1]['time_ms'])
    if first.tzinfo is not None or last.tzinfo is not None:
        raise ValueError('unexpected_timestamp_basis')
    for _, start, end in WINDOWS:
        if first <= datetime.fromisoformat(end) and last >= datetime.fromisoformat(start):
            raise ValueError('additional_sample_label_overlaps_previous_window')
    # Use observed labels only to validate the stored sample internally. This
    # does not establish that returned labels match the old requested bounds.
    return validate_rows(response, first.isoformat(), last.isoformat())


def previous_original_matches(scenario, cash_report, signal_report):
    """Match published earlier result; this is not a raw-archive hash check."""
    if (not isinstance(cash_report, dict) or not isinstance(signal_report, dict)
            or not isinstance(cash_report.get('result'), dict)
            or not isinstance(signal_report.get('result'), dict)):
        return False
    cash, signal = cash_report['result'], signal_report['result']
    if (cash.get('status') != 'gross_scenario_executed_net_costs_unknown'
            or signal.get('signal_replay_executed') is not True):
        return False
    current_signal = scenario['signal_replay']
    for key in ('input_rows', 'replayed_rows', 'interior_minutes_seen', 'signal_counts'):
        if key not in signal or current_signal[key] != signal[key]:
            return False
    for key in ('counts', 'closed_scenarios', 'closed_wins', 'closed_losses',
                'position_open_at_sample_end', 'pending_entry_at_sample_end',
                'simulated_three_consecutive_loss_latch', 'net_pnl'):
        if key not in cash or scenario[key] != cash[key]:
            return False
    for key in ('gross_realized_usd_model', 'gross_open_mark_usd_model',
                'gross_total_mark_usd_model', 'gross_mark_drawdown_usd_model'):
        if key not in cash or not same_number(scenario[key], cash[key]):
            return False
    return True


def read_previous_candidates(path):
    """Copy only fixed aggregate fields, not arbitrary contents of a report."""
    raw = read_json(path, limit=1_000_000)
    if not isinstance(raw, dict) or not isinstance(raw.get('result'), dict):
        raise ValueError('invalid_prior_variants')
    report = raw['result']
    if (report.get('status') != 'completed_all_simulations' or report.get('failures') != 0
            or not isinstance(report.get('windows'), list)):
        raise ValueError('invalid_prior_variants')
    result = {}
    expected = {name for name, _, _ in WINDOWS}
    for win in report['windows']:
        if not isinstance(win, dict) or win.get('id') not in expected or win['id'] in result:
            raise ValueError('invalid_prior_variants')
        candidates = [v for v in win.get('variants', []) if isinstance(v, dict)
                      and v.get('variant_id') == CANDIDATE[0]]
        if len(candidates) != 1 or win.get('baseline_reproduced') is not True:
            raise ValueError('invalid_prior_variants')
        item = candidates[0]
        for key in ('entries', 'profit_exits', 'loss_exits'):
            if type(item.get(key)) is not int or not 0 <= item[key] <= 2000:
                raise ValueError('invalid_prior_variants')
        for key in ('open_at_end', 'latch'):
            if type(item.get(key)) is not bool:
                raise ValueError('invalid_prior_variants')
        amount = item.get('gross_total_mark')
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
            raise ValueError('invalid_prior_variants')
        result[win['id']] = {k: item[k] for k in
                             ('entries', 'profit_exits', 'loss_exits', 'gross_total_mark',
                              'open_at_end', 'latch')}
    if set(result) != expected:
        raise ValueError('invalid_prior_variants')
    return result


def add_prior_comparison(item, prior):
    if prior is None:
        item['prior_candidate_comparison'] = 'unavailable'
        return
    delta = item['gross_total_mark_usd_model'] - prior['gross_total_mark']
    if not math.isfinite(delta):
        raise ValueError('invalid_prior_comparison')
    item['gross_total_mark_delta_vs_previous_2_cap_1_usd_model'] = delta
    item['same_reported_metrics_as_previous_2_cap_1'] = (
        item['simulated_entries'] == prior['entries']
        and item['cash_profit_exits'] == prior['profit_exits']
        and item['cash_loss_exits'] == prior['loss_exits']
        and item['position_open_at_sample_end'] is prior['open_at_end']
        and item['simulated_three_consecutive_loss_latch'] is prior['latch']
        and same_number(item['gross_total_mark_usd_model'], prior['gross_total_mark']))


def mark_skipped(window, variants, reason):
    window['status'] = reason
    window['variants'] = [{'variant_id': v[0], 'status': 'not_simulated_' + reason} for v in variants]


def run_campaign(archive=None, old_raw=None, economics_file=None, v2_reference=None,
                 old_cash_report=None, old_signal_report=None, previous_variants=None):
    archive = ARCHIVE if archive is None else archive
    old_raw = OLD_RAW if old_raw is None else old_raw
    economics_file = ECONOMICS if economics_file is None else economics_file
    v2_reference = REFERENCE if v2_reference is None else v2_reference
    old_cash_report = OLD_CASH_REPORT if old_cash_report is None else old_cash_report
    old_signal_report = OLD_SIGNAL_REPORT if old_signal_report is None else old_signal_report
    previous_variants = PREVIOUS_VARIANTS if previous_variants is None else previous_variants
    report = {
        'schema': 'xauusd-offline-next-tests/v1', 'status': 'started',
        'history_calls_made': 0, 'network_calls_made': 0, 'models_called': 0, 'orders_sent': 0,
        'execution_authorized': False, 'profitability_proven': False,
        'commission': None, 'swap': None, 'net_pnl': None,
        'complete_tick_coverage_verified': False, 'utc_verified': False,
        'economics_symbol_binding_independently_verified': False,
        'baseline_check_is_metrics_not_raw_archive_hash': True,
        'additional_sample_requested_bounds_independently_verified': False,
        'independent_hypothetical_windows_not_continuous': True,
        'prior_sample_baseline_was_known': True, 'not_blind_or_unbiased_holdout': True,
        'new_tests_planned': 1 + (1 + len(WINDOWS)) * len(NEW_VARIANTS),
        'new_tests_completed': 0, 'baseline_checks_attempted': 0, 'windows': [],
        'limitations': [
            'The previously selected candidate is assessed on one additional archived sample; its baseline was known',
            'The four additional parameter hypotheses were selected after seeing earlier aggregate results',
            'Six sparse samples and multiple comparisons are not evidence of general profitability',
            'The same saved current economics snapshot is used; historical contract rules are unknown',
            'Each sample and hypothesis starts flat with a separate simulated three-loss latch',
            'Real latch, account risk, live settings and orders are never accessed or modified',
            'Spread cap is checked on signal and next quote; fills, slippage and complete ticks are unverified',
            'No-entry is not profitability; marks on open positions are not realized profit',
            'Equal reported metrics do not prove identical hypothetical trades or execution',
        ],
    }
    try:
        info = read_json(economics_file, limit=100_000)
        if not isinstance(info, dict) or set(info) != ECONOMICS_FIELDS:
            raise ValueError('saved_economics_invalid')
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

    # Evaluate the preselected candidate on the separate old sample before the
    # new hypotheses, then continue regardless of profit/loss/failure.
    samples = [(OLD_WINDOW, old_raw, True)] + [
        ((name, start, end), archive / (name + '-raw-response.json'), False)
        for name, start, end in WINDOWS]
    for (name, start, end), path, is_old in samples:
        variants = (CANDIDATE, *NEW_VARIANTS) if is_old else NEW_VARIANTS
        window = {'id': name, 'status': 'started', 'baseline_reproduced': False,
                  'baseline_check': None, 'previous_candidate_reported_metrics': candidates.get(name),
                  'variants': []}
        report['windows'].append(window)
        if (is_old and old_refs is None) or (not is_old and name not in v2_refs):
            mark_skipped(window, variants, 'prior_baseline_reference_unavailable')
            continue
        if path.is_symlink() or not path.is_file():
            mark_skipped(window, variants, 'saved_tick_archive_missing_or_symlink')
            continue
        try:
            response = read_json(path)
            data = validate_additional_response(response) if is_old else validate_rows(response, start, end)
            if data['kind'] != 'rows':
                raise ValueError('no_saved_rows')
            rows = data['rows']
        except Exception:
            mark_skipped(window, variants, 'saved_tick_archive_invalid_or_out_of_bounds')
            continue
        report['baseline_checks_attempted'] += 1
        try:
            baseline = replay_cash_scenario(rows, info)
            matches = (previous_original_matches(baseline, *old_refs) if is_old
                       else baseline_matches(baseline, v2_refs[name], len(rows)))
            if not matches:
                mark_skipped(window, variants, 'prior_baseline_mismatch')
                continue
            window['baseline_check'] = summarize(BASELINE, baseline, baseline)
            window['baseline_reproduced'] = True
        except Exception:
            mark_skipped(window, variants, 'baseline_replay_or_comparison_failed')
            continue
        window['status'] = 'simulated'
        for variant in variants:
            try:
                _, changes, spread = variant
                scenario = replay_cash_scenario(rows, info, consecutive_changes=changes,
                                                max_spread_cost_usd=spread)
                result = summarize(variant, scenario, baseline)
                if not is_old:
                    add_prior_comparison(result, candidates.get(name))
                window['variants'].append(result)
                report['new_tests_completed'] += 1
            except Exception:
                window['status'] = 'some_variants_failed'
                window['variants'].append({'variant_id': variant[0], 'status': 'offline_simulation_failed'})
    if report['new_tests_completed'] == report['new_tests_planned']:
        report['status'] = 'completed_all_new_tests'
    elif report['new_tests_completed']:
        report['status'] = 'completed_partial_new_tests'
    else:
        report['status'] = 'no_verified_saved_samples'
    return report


def main():
    try:
        OUTPUT.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        print('Offline follow-up output exists; not overwriting or repeating.')
        return 1
    except OSError:
        print('Cannot create private offline output; no network calls made.')
        return 1
    report = run_campaign()
    fd = os.open(OUTPUT / 'summary.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report['status'] == 'completed_all_new_tests' else 1


if __name__ == '__main__':
    raise SystemExit(main())
