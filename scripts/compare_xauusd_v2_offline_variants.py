"""Compare fixed strategy hypotheses on the five ALREADY SAVED v2 responses.

No config, MT5, network, model, live strategy or order access. Each window and
variant is independent; a losing model or a bad local sample does not prevent
trying other independent comparisons. These post-hoc comparisons are exploratory,
not blind out-of-sample evidence and not authority to trade.
"""
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from xauusd_sparse_screen_v2 import validate_rows  # noqa: E402

from trade_mcp.tick_cash_replay import economics, replay_cash_scenario  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / 'logs/research-inputs/xauusd-sparse-screen-20261109-10-v2-5win'
ECONOMICS = ROOT / 'logs/research-inputs/xauusd-cash-research-20261122T075312Z/symbol-economics.json'
REFERENCE = ROOT / 'reports/research/20261122T110500Z-xauusd-sparse-screen-v2-5win.json'
OUTPUT = ROOT / 'logs/research-inputs/xauusd-v2-offline-variants-v1'
MAX_INPUT_BYTES = 10_000_000
ECONOMICS_FIELDS = {'trade_contract_size', 'currency_profit', 'trade_calc_mode',
                    'volume_min', 'volume_max', 'volume_step'}
WINDOWS = (
    ('w1', '2026-11-09T13:00:00', '2026-11-09T13:05:00'),
    ('w2', '2026-11-09T19:00:00', '2026-11-09T19:05:00'),
    ('w3', '2026-11-10T09:00:00', '2026-11-10T09:05:00'),
    ('w4', '2026-11-10T13:00:00', '2026-11-10T13:05:00'),
    ('w5', '2026-11-10T19:00:00', '2026-11-10T19:05:00'),
)
# Fixed once before seeing any raw tick archives. All are still post-hoc because
# aggregate baseline outcomes were known when choosing the hypotheses.
VARIANTS = (
    ('original_3_changes', 3, None),
    ('3_changes_spread_cap_0_50', 3, .50),
    ('3_changes_spread_cap_1_00', 3, 1.00),
    ('2_changes_spread_cap_1_00', 2, 1.00),
)


def read_json(path, limit=MAX_INPUT_BYTES):
    if path.is_symlink() or not path.is_file():
        raise ValueError('local_file_missing_or_symlink')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if not 0 < len(raw) <= limit:
        raise ValueError('local_file_size_invalid')
    return json.loads(raw)


def valid_reference(value):
    if (not isinstance(value, dict) or value.get('status') != 'completed_5_windows'
            or value.get('attempted_history_calls') != 5 or not isinstance(value.get('windows'), list)):
        raise ValueError('prior_report_invalid')
    refs = {}
    expected = {name: (start, end) for name, start, end in WINDOWS}
    for item in value['windows']:
        if not isinstance(item, dict):
            raise ValueError('prior_report_invalid')
        name = item.get('id')
        if (name not in expected or name in refs or item.get('status') != 'simulated'
                or (item.get('start'), item.get('end')) != expected[name]):
            raise ValueError('prior_report_invalid')
        refs[name] = item
    if set(refs) != set(expected):
        raise ValueError('prior_report_invalid')
    return refs


def same_number(actual, expected):
    if actual is None or expected is None:
        return actual is expected
    if (isinstance(actual, bool) or isinstance(expected, bool)
            or not isinstance(actual, (int, float)) or not isinstance(expected, (int, float))):
        return False
    return math.isfinite(actual) and math.isfinite(expected) and math.isclose(
        actual, expected, rel_tol=0, abs_tol=1e-8)


def baseline_matches(scenario, ref, rows_count):
    required = {'returned_rows', 'replayed_rows', 'interior_minutes_seen', 'signal_counts',
                'simulated_entries', 'cash_loss_exits', 'cash_profit_exits',
                'signals_blocked_by_simulated_loss_latch', 'gross_realized_usd_model',
                'gross_open_mark_usd_model', 'gross_total_mark_usd_model',
                'position_open_at_sample_end', 'simulated_three_consecutive_loss_latch', 'net_pnl'}
    if not required <= ref.keys():
        return False
    signal = scenario['signal_replay']
    counts = scenario['counts']
    if (rows_count != ref.get('returned_rows')
            or signal['replayed_rows'] != ref.get('replayed_rows')
            or signal['interior_minutes_seen'] != ref.get('interior_minutes_seen')
            or signal['signal_counts'] != ref.get('signal_counts')):
        return False
    for key in ('simulated_entries', 'cash_loss_exits', 'cash_profit_exits',
                'signals_blocked_by_simulated_loss_latch'):
        if (counts.get(key) or 0) != (ref.get(key) or 0):
            return False
    for key in ('gross_realized_usd_model', 'gross_open_mark_usd_model', 'gross_total_mark_usd_model'):
        if not same_number(scenario[key], ref.get(key)):
            return False
    for key in ('position_open_at_sample_end', 'simulated_three_consecutive_loss_latch'):
        if scenario[key] is not ref.get(key):
            return False
    return scenario['net_pnl'] is None and ref.get('net_pnl') is None


def summarize(variant, scenario, baseline):
    name, consecutive, spread_cap = variant
    counts = scenario['counts']
    signals = scenario['signal_replay']['signal_counts']
    monetary = ('gross_realized_usd_model', 'gross_open_mark_usd_model', 'gross_total_mark_usd_model')
    if any(not same_number(scenario[key], scenario[key]) for key in monetary):
        raise ValueError('invalid modeled amount')
    delta = scenario['gross_total_mark_usd_model'] - baseline['gross_total_mark_usd_model']
    if not math.isfinite(delta):
        raise ValueError('invalid modeled comparison')
    return {
        'variant_id': name,
        'status': 'simulated',
        'consecutive_quote_changes': consecutive,
        'max_entry_spread_cost_usd_model': spread_cap,
        'signal_counts': {'buy': signals.get('buy', 0), 'sell': signals.get('sell', 0)},
        'simulated_entries': counts.get('simulated_entries', 0),
        'cash_profit_exits': counts.get('cash_profit_exits', 0),
        'cash_loss_exits': counts.get('cash_loss_exits', 0),
        'signals_blocked_by_simulated_loss_latch': counts.get('signals_blocked_by_simulated_loss_latch', 0),
        'signals_blocked_by_spread': counts.get('signals_blocked_by_spread', 0),
        'pending_entries_blocked_by_spread': counts.get('pending_entries_blocked_by_spread', 0),
        'gross_realized_usd_model': scenario['gross_realized_usd_model'],
        'gross_open_mark_usd_model': scenario['gross_open_mark_usd_model'],
        'gross_total_mark_usd_model': scenario['gross_total_mark_usd_model'],
        'gross_total_mark_delta_vs_original_usd_model': delta,
        'position_open_at_sample_end': scenario['position_open_at_sample_end'],
        'simulated_three_consecutive_loss_latch': scenario['simulated_three_consecutive_loss_latch'],
        'commission': None, 'swap': None, 'net_pnl': None,
    }


def skipped_variants(reason):
    return [{'variant_id': name, 'status': 'not_simulated_' + reason} for name, _, _ in VARIANTS]


def compare(archive=None, economics_file=None, reference_file=None):
    archive = ARCHIVE if archive is None else archive
    economics_file = ECONOMICS if economics_file is None else economics_file
    reference_file = REFERENCE if reference_file is None else reference_file
    report = {
        'schema': 'xauusd-existing-v2-offline-variants/v1',
        'status': 'started', 'history_calls_made': 0, 'network_calls_made': 0,
        'models_called': 0, 'orders_sent': 0,
        'execution_authorized': False, 'profitability_proven': False,
        'time_basis': 'broker_wall_clock_labels_utc_unverified',
        'complete_tick_coverage_verified': False,
        'economics_symbol_binding_independently_verified': False,
        'commission': None, 'swap': None, 'net_pnl': None,
        'independent_hypothetical_windows_not_continuous': True,
        'selection_after_baseline_results': True,
        'blind_holdout_or_out_of_sample': False,
        'baseline_check_is_metrics_not_raw_archive_hash': True,
        'max_simulations': len(WINDOWS) * len(VARIANTS),
        'completed_simulations': 0, 'windows': [],
        'limitations': [
            'Exploratory hypotheses selected after the original five outcomes were known',
            'Only five sparse five-minute samples with boundary minutes excluded',
            'Each variant and window begins flat with its own hypothetical simulated latch',
            'Real persistent latch and live trading are not accessed or changed',
            'Spread cap uses current saved contract metadata and is checked at signal and next quote',
            'No historical contract, complete feed, actual broker fills, slippage, UTC or account risk verification',
            'Commission, swap and net PnL remain unknown; never treat a no-trade outcome as a profitable strategy',
            'No continuous equity curve or proof of profitability',
        ],
    }
    try:
        info = read_json(economics_file, limit=100_000)
        if not isinstance(info, dict) or set(info) != ECONOMICS_FIELDS:
            raise ValueError('saved_economics_invalid')
        economics(info)
    except (OSError, TypeError, ValueError, OverflowError, RecursionError):
        report['status'] = 'blocked_saved_economics_unavailable_or_invalid'
        return report
    try:
        refs = valid_reference(read_json(reference_file, limit=1_000_000))
    except (OSError, TypeError, ValueError, OverflowError, RecursionError):
        report['status'] = 'blocked_prior_sanitized_reference_unavailable_or_invalid'
        return report

    for name, start, end in WINDOWS:
        window = {'id': name, 'start': start, 'end': end, 'status': 'started',
                  'baseline_reproduced': False, 'variants': []}
        report['windows'].append(window)
        try:
            raw_response = read_json(archive / (name + '-raw-response.json'))
            outcome = validate_rows(raw_response, start, end)
            if outcome['kind'] != 'rows':
                raise ValueError('no_usable_saved_rows')
            rows = outcome['rows']
            baseline = replay_cash_scenario(rows, info)
            if not baseline_matches(baseline, refs[name], len(rows)):
                window['status'] = 'prior_baseline_mismatch'
                window['variants'] = skipped_variants('prior_baseline_mismatch')
                continue
        except Exception:
            # A broken window is not allowed to suppress unrelated comparisons.
            # Never include input data or exception details in the report.
            window['status'] = 'saved_window_unavailable_invalid_or_unreplayable'
            window['variants'] = skipped_variants('saved_window_invalid')
            continue
        window['baseline_reproduced'] = True
        window['status'] = 'simulated'
        for index, variant in enumerate(VARIANTS):
            variant_id, consecutive, spread_cap = variant
            try:
                scenario = baseline if index == 0 else replay_cash_scenario(
                    rows, info, consecutive_changes=consecutive, max_spread_cost_usd=spread_cap)
                window['variants'].append(summarize(variant, scenario, baseline))
                report['completed_simulations'] += 1
            except Exception:
                # A broken local variant does not prevent other independent tests;
                # never print the exception or any raw values.
                window['status'] = 'some_variants_failed'
                window['variants'].append({'variant_id': variant_id, 'status': 'offline_simulation_failed'})
    if report['completed_simulations'] == report['max_simulations']:
        report['status'] = 'completed_all_simulations'
    elif report['completed_simulations']:
        report['status'] = 'completed_partial_simulations'
    else:
        report['status'] = 'no_verified_saved_windows'
    return report


def main():
    try:
        OUTPUT.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        print('Offline comparison output exists; not overwriting or repeating.')
        return 1
    except OSError:
        print('Cannot create private offline comparison output; no network calls made.')
        return 1
    report = compare()
    output = OUTPUT / 'summary.json'
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report['status'] == 'completed_all_simulations' else 1


if __name__ == '__main__':
    raise SystemExit(main())
