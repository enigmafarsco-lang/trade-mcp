"""Review published XAUUSD aggregates; no ticks, MT5, network or orders.

The final-boundary comparison actually replayed only two variants per sample.
For a different legacy variant that ended flat, the corrected model's entry,
exit and gross totals are logically unchanged: it cannot create new entries in
its final boundary minute. A legacy open position leaves the corrected outcome
unknown. These conditional inferences are not new simulations or actual PnL.
"""
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / 'reports/research'
BOUNDARY_PATH = REPORTS / '20261122T124500Z-xauusd-final-boundary-replay.json'
NEXT_PATH = REPORTS / '20261122T122500Z-xauusd-offline-next-tests.json'
V2_PATH = REPORTS / '20261122T120000Z-xauusd-v2-offline-variants.json'
MAX_REPORT_BYTES = 1_000_000
SAMPLES = ('additional_sample', 'w1', 'w2', 'w3', 'w4', 'w5')
ORIGINAL = 'original_3_changes'
CANDIDATE = '2_changes_spread_cap_1_00'
NEXT_VARIANTS = ('2_changes_no_spread_cap', '2_changes_spread_cap_0_50',
                 '2_changes_spread_cap_0_75', '3_changes_spread_cap_0_75')
V2_VARIANTS = ('3_changes_spread_cap_0_50', '3_changes_spread_cap_1_00')


def invalid():
    raise ValueError('unexpected_or_inconsistent_sanitized_report')


def load_report(path):
    if path.is_symlink() or not path.is_file():
        invalid()
    with path.open('rb') as stream:
        data = stream.read(MAX_REPORT_BYTES + 1)
    if not 0 < len(data) <= MAX_REPORT_BYTES:
        invalid()
    return json.loads(data, parse_constant=lambda _: invalid())


def result(report, report_id, schema, status):
    if (not isinstance(report, dict) or report.get('schema') != 'xauusd-research-check-report/v1'
            or report.get('report_id') != report_id or report.get('raw_data_committed') is not False
            or 'net_pnl' not in report or report['net_pnl'] is not None
            or not isinstance(report.get('result'), dict)):
        invalid()
    item = report['result']
    if item.get('schema') != schema or item.get('status') != status:
        invalid()
    return item


def index_exact(items, field, expected):
    if not isinstance(items, list) or len(items) != len(expected):
        invalid()
    indexed = {}
    for item in items:
        if not isinstance(item, dict) or item.get(field) not in expected or item[field] in indexed:
            invalid()
        indexed[item[field]] = item
    if set(indexed) != set(expected):
        invalid()
    return indexed


def count(value):
    if type(value) is not int or not 0 <= value <= 2000:
        invalid()
    return value


def money(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        invalid()
    return float(value)


def close(a, b):
    return math.isclose(a, b, rel_tol=0, abs_tol=1e-8)


def side(raw, *, profit, loss, gross, open_field=None, zero_missing_exits=False):
    if not isinstance(raw, dict):
        invalid()
    entries = count(raw.get('entries'))
    amount = money(raw.get(gross))
    if zero_missing_exits and entries == 0 and profit not in raw and loss not in raw:
        wins = losses = 0  # The one-position model cannot exit without an entry.
    else:
        wins, losses = count(raw.get(profit)), count(raw.get(loss))
    residual = entries - wins - losses
    if residual not in (0, 1) or (entries == 0 and not close(amount, 0)):
        invalid()
    if open_field is not None:
        opened = raw.get(open_field)
        if type(opened) is not bool or opened is not bool(residual):
            invalid()
    return {'entries': entries, 'profit_exits': wins, 'loss_exits': losses,
            'gross_total_usd_model': amount, 'open': bool(residual)}


def same_side(a, b):
    return (all(a[key] == b[key] for key in ('entries', 'profit_exits', 'loss_exits', 'open'))
            and close(a['gross_total_usd_model'], b['gross_total_usd_model']))


def exact_count(item, key, expected):
    return type(item.get(key)) is int and item[key] == expected


def review(boundary_report, next_report, v2_report):
    """Infer only invariants from sanitized aggregates, never rereplay a sample."""
    boundary = result(boundary_report, BOUNDARY_PATH.stem, 'xauusd-final-boundary-replay/v1',
                      'completed_all_comparisons')
    nxt = result(next_report, NEXT_PATH.stem, 'xauusd-offline-next-tests/v1', 'completed_all_new_tests')
    v2 = result(v2_report, V2_PATH.stem, 'xauusd-existing-v2-offline-variants/v1', 'completed_all_simulations')
    if (not exact_count(boundary, 'completed_comparisons', 12)
            or not exact_count(boundary, 'planned_comparisons', 12)
            or not exact_count(boundary, 'failures', 0) or boundary.get('all_legacies_reproduced') is not True
            or not exact_count(nxt, 'new_tests_completed', 25) or not exact_count(nxt, 'new_tests_planned', 25)
            or nxt.get('all_baselines_reproduced') is not True
            or not exact_count(v2, 'total_simulations', 20) or not exact_count(v2, 'failures', 0)
            or v2.get('all_baselines_reproduced') is not True):
        invalid()

    cases, legacy_sides, legacy_latches = [], {}, {}
    expected = {(sample, variant) for sample in SAMPLES for variant in (ORIGINAL, CANDIDATE)}
    comparisons = boundary.get('comparisons')
    if not isinstance(comparisons, list) or len(comparisons) != len(expected):
        invalid()
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            invalid()
        key = (comparison.get('sample'), comparison.get('variant'))
        if key not in expected or key in legacy_sides:
            invalid()
        old_raw, new_raw = comparison.get('legacy'), comparison.get('managed')
        old = side(old_raw, profit='profit', loss='loss', gross='gross_total', open_field='open_at_interior')
        new = side(new_raw, profit='profit', loss='loss', gross='gross_total',
                   open_field='open_at_last_observed')
        if (type(old_raw.get('latch')) is not bool or type(new_raw.get('latch')) is not bool
                or old['entries'] != new['entries']
                or not close(new['gross_total_usd_model'] - old['gross_total_usd_model'],
                             money(comparison.get('delta_vs_legacy')))):
            invalid()
        if not old['open'] and (not same_side(old, new) or old_raw['latch'] is not new_raw['latch']):
            invalid()
        legacy_sides[key] = old
        legacy_latches[key] = old_raw['latch']
        cases.append(case(key, 'reported_boundary_replay', old, new))
    if set(legacy_sides) != expected:
        invalid()

    next_windows = index_exact(nxt.get('windows'), 'id', SAMPLES)
    for sample in SAMPLES:
        expected_variants = NEXT_VARIANTS + ((CANDIDATE,) if sample == 'additional_sample' else ())
        variants = index_exact(next_windows[sample].get('tests'), 'variant', expected_variants)
        for variant in expected_variants:
            old = side(variants[variant], profit='profit', loss='loss', gross='gross_total')
            if variant == CANDIDATE:
                if not same_side(old, legacy_sides[(sample, variant)]):
                    invalid()
            else:
                cases.append(inferred_case((sample, variant), old, 'offline_next_tests'))

    v2_windows = index_exact(v2.get('windows'), 'id', SAMPLES[1:])
    for sample in SAMPLES[1:]:
        window = v2_windows[sample]
        if window.get('baseline_reproduced') is not True:
            invalid()
        variants = index_exact(window.get('variants'), 'variant_id', (ORIGINAL, CANDIDATE) + V2_VARIANTS)
        for variant in (ORIGINAL, CANDIDATE) + V2_VARIANTS:
            old = side(variants[variant], profit='profit_exits', loss='loss_exits',
                       gross='gross_total_mark', open_field='open_at_end', zero_missing_exits=True)
            latch = variants[variant].get('latch')
            if type(latch) is not bool:
                invalid()
            if variant in (ORIGINAL, CANDIDATE):
                if (not same_side(old, legacy_sides[(sample, variant)])
                        or latch is not legacy_latches[(sample, variant)]):
                    invalid()
            else:
                cases.append(inferred_case((sample, variant), old, 'v2_variants'))

    known = [item for item in cases if item['corrected_gross_usd_model'] is not None]
    negative = [item for item in known if item['corrected_gross_usd_model'] < -1e-8]
    zero = [item for item in known if close(item['corrected_gross_usd_model'], 0)]
    positive = [item for item in known if item['corrected_gross_usd_model'] > 1e-8]
    direct = {(item['sample'], item['variant']): item for item in cases
              if item['basis'] == 'reported_boundary_replay'}
    paired = []
    for sample in SAMPLES:
        original, candidate = direct[(sample, ORIGINAL)], direct[(sample, CANDIDATE)]
        difference = candidate['corrected_gross_usd_model'] - original['corrected_gross_usd_model']
        if not math.isfinite(difference):
            invalid()
        paired.append({'sample': sample, 'candidate_minus_original_gross_usd_model': difference,
                       'candidate_minus_original_entries': candidate['entries'] - original['entries'],
                       'original_open_at_last_observed': original['position_open_at_last_observed']})
    return {
        'schema': 'xauusd-saved-summary-inference/v1',
        'status': 'completed_aggregate_review_no_new_tick_replay',
        'source_reports': [BOUNDARY_PATH.name, NEXT_PATH.name, V2_PATH.name],
        'case_counts': {'total': len(cases),
                        'reported_boundary_replay': sum(x['basis'] == 'reported_boundary_replay' for x in cases),
                        'inferred_flat_at_interior': sum(x['basis'] == 'inferred_flat_at_interior' for x in cases),
                        'unknown_legacy_position_open': sum(x['basis'] == 'unknown_legacy_position_open' for x in cases),
                        'known_negative_gross': len(negative), 'known_zero_gross': len(zero),
                        'known_positive_gross': len(positive),
                        'known_zero_gross_with_no_entries': sum(x['entries'] == 0 for x in zero)},
        'cases': cases, 'paired_reported_comparisons': paired,
        'commission': None, 'swap': None, 'net_pnl': None,
        'actual_broker_fills_verified': False, 'profitability_proven': False,
        'execution_authorized': False, 'new_tick_replays': 0,
        'limitations': [
            'Inferences assume the published aggregate counts and the same historical inputs/model are correct',
            'When flat at the last interior quote, final-boundary management cannot enter a new position',
            'Open legacy positions have unknown corrected gross unless the separate boundary report replayed them',
            'Matching aggregate reports is not a cryptographic verification of archived tick identity',
            'Gross includes modeled spread; unknown commission, swap and actual execution cannot be inferred',
            'Six retrospective windows are independent hypothetical scenarios, not a continuous account or holdout',
        ],
    }


def case(key, basis, old, corrected):
    gross = corrected['gross_total_usd_model'] if corrected is not None else None
    return {'sample': key[0], 'variant': key[1], 'basis': basis, 'entries': old['entries'],
            'legacy_gross_usd_model': old['gross_total_usd_model'],
            'corrected_gross_usd_model': gross,
            'profit_exits': corrected['profit_exits'] if corrected is not None else None,
            'loss_exits': corrected['loss_exits'] if corrected is not None else None,
            'position_open_at_last_observed': corrected['open'] if corrected is not None else None,
            # For a negative gross G, additive adjustments must total at least -G
            # to reach zero. This is not an estimate of swap, fees or actual net.
            'minimum_net_positive_adjustment_for_zero_usd_model': -gross if gross is not None and gross < -1e-8 else None,
            'net_pnl': None}


def inferred_case(key, old, source):
    if old['open']:
        item = case(key, 'unknown_legacy_position_open', old, None)
    else:
        item = case(key, 'inferred_flat_at_interior', old, old)
    item['legacy_report'] = source
    return item


def main():
    try:
        report = review(load_report(BOUNDARY_PATH), load_report(NEXT_PATH), load_report(V2_PATH))
    except (OSError, TypeError, ValueError, KeyError, OverflowError, RecursionError):
        print('Sanitized aggregate review blocked by missing or inconsistent input.', file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
