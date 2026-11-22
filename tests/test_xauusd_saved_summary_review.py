"""Only sanitized summary reports; no historical ticks, network or orders."""
import copy
import importlib.util
import json
import socket
from pathlib import Path

import pytest


@pytest.fixture
def job():
    path = Path(__file__).resolve().parents[1] / 'scripts/xauusd_saved_summary_review.py'
    spec = importlib.util.spec_from_file_location('saved_summary_review', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def reports(job):
    return tuple(job.load_report(path) for path in (job.BOUNDARY_PATH, job.NEXT_PATH, job.V2_PATH))


def lookup(result, sample, variant):
    matches = [item for item in result['cases'] if item['sample'] == sample and item['variant'] == variant]
    assert len(matches) == 1
    return matches[0]


def test_sanitized_aggregate_review_makes_only_conditional_inferences(job, reports, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Aggregate review must not create a network socket')

    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    before = copy.deepcopy(reports)
    output = job.review(*reports)
    assert reports == before
    assert output['status'] == 'completed_aggregate_review_no_new_tick_replay'
    assert output['case_counts'] == {
        'total': 46, 'reported_boundary_replay': 12, 'inferred_flat_at_interior': 22,
        'unknown_legacy_position_open': 12, 'known_negative_gross': 21, 'known_zero_gross': 13,
        'known_positive_gross': 0, 'known_zero_gross_with_no_entries': 13,
    }
    assert output['new_tick_replays'] == 0 and output['execution_authorized'] is False
    assert output['actual_broker_fills_verified'] is False and output['profitability_proven'] is False
    assert output['net_pnl'] is output['commission'] is output['swap'] is None
    assert all(item['net_pnl'] is None for item in output['cases'])

    reported = lookup(output, 'w1', '2_changes_spread_cap_1_00')
    assert reported['basis'] == 'reported_boundary_replay'
    assert reported['corrected_gross_usd_model'] == pytest.approx(-.20)
    assert reported['minimum_net_positive_adjustment_for_zero_usd_model'] == pytest.approx(.20)
    inferred = lookup(output, 'w5', '3_changes_spread_cap_0_75')
    assert inferred['basis'] == 'inferred_flat_at_interior'
    assert inferred['corrected_gross_usd_model'] == pytest.approx(-4.26)
    assert inferred['position_open_at_last_observed'] is False
    no_entry = lookup(output, 'w2', '2_changes_spread_cap_0_50')
    assert no_entry['basis'] == 'inferred_flat_at_interior' and no_entry['entries'] == 0
    assert no_entry['corrected_gross_usd_model'] == 0
    assert no_entry['minimum_net_positive_adjustment_for_zero_usd_model'] is None
    unknown = lookup(output, 'w1', '2_changes_no_spread_cap')
    assert unknown['basis'] == 'unknown_legacy_position_open'
    assert unknown['legacy_gross_usd_model'] == pytest.approx(1.22)
    assert unknown['corrected_gross_usd_model'] is None
    assert unknown['position_open_at_last_observed'] is None
    assert unknown['profit_exits'] is unknown['loss_exits'] is None
    assert unknown['minimum_net_positive_adjustment_for_zero_usd_model'] is None

    paired = {item['sample']: item for item in output['paired_reported_comparisons']}
    assert paired['additional_sample']['candidate_minus_original_gross_usd_model'] == pytest.approx(-6.16)
    assert paired['w1']['candidate_minus_original_gross_usd_model'] == pytest.approx(3.84)
    assert paired['w4']['original_open_at_last_observed'] is True
    json.dumps(output, allow_nan=False)


def test_published_json_is_exactly_reproducible_from_sanitized_inputs(job, reports):
    path = job.REPORTS / 'xauusd-saved-summary-inference.json'
    assert json.loads(path.read_text(encoding='utf-8')) == job.review(*reports)


def test_negative_gross_requires_a_net_positive_adjustment_not_a_cost_guess(job, reports):
    output = job.review(*reports)
    negative = [item for item in output['cases']
                if item['minimum_net_positive_adjustment_for_zero_usd_model'] is not None]
    assert len(negative) == 21
    for item in negative:
        gross = item['corrected_gross_usd_model']
        adjustment = item['minimum_net_positive_adjustment_for_zero_usd_model']
        assert adjustment > 0 and gross + adjustment == pytest.approx(0)
        assert item['net_pnl'] is None  # This threshold does not provide actual fee or swap values.


@pytest.mark.parametrize('mutation', [
    'missing_net', 'wrong_id', 'partial_run', 'boolean_failure_count', 'duplicate_variant',
    'bad_entry_count', 'nonfinite_gross', 'nonzero_without_entry', 'mismatched_reference',
    'mismatched_latch', 'boundary_entry_change', 'flat_boundary_changes_gross', 'invalid_open_flag',
])
def test_inconsistent_or_incomplete_reports_fail_closed(job, reports, mutation):
    boundary, nxt, v2 = copy.deepcopy(reports)
    if mutation == 'missing_net':
        del nxt['net_pnl']
    elif mutation == 'wrong_id':
        nxt['report_id'] = 'another-report'
    elif mutation == 'partial_run':
        nxt['result']['new_tests_completed'] = 24
    elif mutation == 'boolean_failure_count':
        boundary['result']['failures'] = False
    elif mutation == 'duplicate_variant':
        nxt['result']['windows'][0]['tests'][1]['variant'] = nxt['result']['windows'][0]['tests'][0]['variant']
    elif mutation == 'bad_entry_count':
        nxt['result']['windows'][0]['tests'][0]['entries'] += 3
    elif mutation == 'nonfinite_gross':
        nxt['result']['windows'][0]['tests'][0]['gross_total'] = float('nan')
    elif mutation == 'nonzero_without_entry':
        nxt['result']['windows'][0]['tests'][2]['gross_total'] = 1
    elif mutation == 'mismatched_reference':
        v2['result']['windows'][0]['variants'][0]['gross_total_mark'] += 1
    elif mutation == 'mismatched_latch':
        v2['result']['windows'][0]['variants'][0]['latch'] = True
    elif mutation == 'boundary_entry_change':
        boundary['result']['comparisons'][0]['managed']['entries'] += 1
    elif mutation == 'flat_boundary_changes_gross':
        comparison = boundary['result']['comparisons'][0]
        comparison['managed']['gross_total'] += 1
        comparison['delta_vs_legacy'] += 1
    else:
        v2['result']['windows'][0]['variants'][1]['open_at_end'] = 'false'
    with pytest.raises(ValueError, match='unexpected_or_inconsistent_sanitized_report'):
        job.review(boundary, nxt, v2)


def test_missing_exit_counts_only_allowed_for_reported_zero_entry(job):
    zero = {'entries': 0, 'open_at_end': False, 'gross_total_mark': 0}
    parsed = job.side(zero, profit='profit_exits', loss='loss_exits', gross='gross_total_mark',
                      open_field='open_at_end', zero_missing_exits=True)
    assert parsed['profit_exits'] == parsed['loss_exits'] == 0 and parsed['open'] is False
    invalid = dict(zero, entries=1, open_at_end=True)
    with pytest.raises(ValueError):
        job.side(invalid, profit='profit_exits', loss='loss_exits', gross='gross_total_mark',
                 open_field='open_at_end', zero_missing_exits=True)


def test_main_reads_fixed_sanitized_inputs_and_creates_no_files(job, reports, tmp_path, monkeypatch, capsys):
    for constant, report in zip(('BOUNDARY_PATH', 'NEXT_PATH', 'V2_PATH'), reports, strict=True):
        original = getattr(job, constant)
        path = tmp_path / original.name
        path.write_text(json.dumps(report, allow_nan=False))
        monkeypatch.setattr(job, constant, path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: pytest.fail('network access forbidden'))
    assert job.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output['case_counts']['total'] == 46
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_main_emits_only_fixed_error_for_unexpected_input(job, reports, tmp_path, monkeypatch, capsys):
    boundary, nxt, v2 = copy.deepcopy(reports)
    nxt['report_id'] = 'PRIVATE INPUT TEXT'
    for constant, report in zip(('BOUNDARY_PATH', 'NEXT_PATH', 'V2_PATH'), (boundary, nxt, v2), strict=True):
        path = tmp_path / getattr(job, constant).name
        path.write_text(json.dumps(report, allow_nan=False))
        monkeypatch.setattr(job, constant, path)
    assert job.main() == 1
    captured = capsys.readouterr()
    assert 'blocked by missing or inconsistent input' in captured.err
    assert 'PRIVATE INPUT TEXT' not in captured.out + captured.err
