"""Sanitized live-check reports do not turn partial or synthetic evidence into success."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'runtime_live', Path(__file__).resolve().parents[1] / 'scripts/publish_runtime_report.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def live():
    return {'commit': 'a' * 40, 'checks': {
        'daytrade_mt5_terminal': 'pass', 'daytrade_mt5_symbol': 'pass',
        'daytrade_mt5_quote': 'pass', 'daytrade_mt5_quote_update': 'pass',
        'daytrade_mt5_quote_freshness': 'blocked', 'daytrade_mt5_session': 'blocked',
        'daytrade_mt5_m1': 'pass', 'daytrade_mt5_m1_freshness': 'blocked',
        'daytrade_mcp_discovery': 'pass', 'daytrade_live_entry_buy': 'pass',
        'daytrade_live_historical_replay': 'pass', 'daytrade_live_time_machine': 'pass',
        'daytrade_fixture_news': 'pass'},
        'counts': {'daytrade_mt5_quote_samples': 2, 'daytrade_mt5_m1_bars': 201,
                   'daytrade_mt5_segments': 20, 'daytrade_mt5_bootstrap_paths': 20},
        'reasons': {'daytrade_mt5_quote_freshness': 'timestamp_unverified',
                    'daytrade_mt5_session': 'session_unverified',
                    'daytrade_mt5_m1_freshness': 'timestamp_unverified'}}


def test_safe_live_evidence_keeps_session_and_freshness_separate():
    output = publisher.sanitize(live())
    assert output['checks']['daytrade_live_entry_buy'] == 'pass'
    assert output['checks']['daytrade_fixture_news'] == 'pass'
    assert output['checks']['daytrade_mt5_quote_update'] == 'pass'
    assert output['checks']['daytrade_mt5_quote_freshness'] == 'blocked'
    assert output['checks']['daytrade_mt5_session'] == 'blocked'
    assert output['checks']['daytrade_opencode_connection'] == 'not_run'
    assert output['checks']['daytrade_live_browser_monitor'] == 'not_run'
    assert output['checks']['order'] == 'not_run'
    assert output['counts']['orders_submitted'] is None
    assert output['execution_authorized_by_report'] is False
    assert output['reasons']['daytrade_mt5_session'] == 'session_unverified'


@pytest.mark.parametrize('modification', [
    lambda x: x['checks'].pop('daytrade_mt5_terminal'),
    lambda x: x['checks'].update(daytrade_mt5_symbol='blocked'),
    lambda x: x['counts'].update(daytrade_mt5_quote_samples=1),
    lambda x: x['counts'].update(daytrade_mt5_m1_bars=1),
    lambda x: x['checks'].update(daytrade_mcp_discovery='not_run'),
    lambda x: x['checks'].update(daytrade_mt5_m1='blocked'),
    lambda x: x['counts'].update(daytrade_mt5_segments=19),
    lambda x: x['counts'].update(daytrade_mt5_bootstrap_paths=0),
])
def test_live_pass_requires_measured_upstream_evidence(modification):
    value = live()
    modification(value)
    with pytest.raises(ValueError, match='Incomplete evidence'):
        publisher.sanitize(value)


@pytest.mark.parametrize('reason', [
    {'daytrade_mt5_session': 'account=private'},
    {'daytrade_mt5_terminal': 'session_unverified'},  # a pass cannot carry a blocker
    {'order': 'session_unverified'},
    {'daytrade_mt5_session': ['session_unverified']},
])
def test_free_text_private_reasons_and_inconsistent_states_rejected(reason):
    value = live()
    value['reasons'] = reason
    with pytest.raises(ValueError, match='reason'):
        publisher.sanitize(value)


def test_unknown_live_counts_cannot_be_detached_from_measured_state():
    value = {'commit': 'a' * 40, 'counts': {'daytrade_mt5_m1_bars': 200}}
    with pytest.raises(ValueError, match='measured check'):
        publisher.sanitize(value)
    value['checks'] = {'daytrade_mt5_m1': 'blocked'}
    assert publisher.sanitize(value)['counts']['daytrade_mt5_m1_bars'] == 200


@pytest.mark.parametrize('check', ['daytrade_opencode_connection', 'daytrade_live_console_http',
                                  'daytrade_live_browser_monitor', 'daytrade_prior_m1_forward'])
def test_deployed_and_forward_claims_require_their_distinct_preconditions(check):
    value = {'commit': 'a' * 40, 'checks': {check: 'pass'}}
    with pytest.raises(ValueError, match='Incomplete'):
        publisher.sanitize(value)


def test_http_pass_requires_an_actual_broker_input_not_only_synthetic_fixtures():
    value = {'commit': 'a' * 40, 'checks': {'daytrade_mcp_discovery': 'pass',
             'daytrade_fixture_news': 'pass', 'daytrade_broker_input_http': 'pass'},
             'counts': {'daytrade_http_cases': 1}}
    with pytest.raises(ValueError, match='broker input'):
        publisher.sanitize(value)
    value['checks'].update(daytrade_mt5_terminal='pass', daytrade_mt5_symbol='pass', daytrade_mt5_quote='pass')
    value['counts']['daytrade_mt5_quote_samples'] = 1
    assert publisher.sanitize(value)['checks']['daytrade_broker_input_http'] == 'pass'


def test_probe_pytest_requires_measured_counts_and_does_not_prove_mt5():
    value = {'commit': 'a' * 40, 'checks': {'daytrade_probe_offline_tests': 'pass'}, 'counts': {
        'daytrade_probe_pytest_passed': 12, 'daytrade_probe_pytest_skipped': 0,
        'daytrade_probe_pytest_failed': 0, 'daytrade_probe_pytest_errors': 0,
        'daytrade_probe_pytest_total': 12, 'daytrade_probe_pytest_exit_code': 0}}
    output = publisher.sanitize(value)
    assert output['checks']['daytrade_mt5_terminal'] == 'not_run'
    del value['counts']['daytrade_probe_pytest_skipped']
    with pytest.raises(ValueError, match='Incomplete'):
        publisher.sanitize(value)


def test_expanded_pytest_is_separate_from_original_suite_and_requires_complete_counts():
    value = {'commit': 'a' * 40, 'checks': {'daytrade_expanded_offline_tests': 'pass'}, 'counts': {
        'daytrade_expanded_pytest_passed': 70, 'daytrade_expanded_pytest_skipped': 1,
        'daytrade_expanded_pytest_failed': 0, 'daytrade_expanded_pytest_errors': 0,
        'daytrade_expanded_pytest_total': 71, 'daytrade_expanded_pytest_exit_code': 0}}
    output = publisher.sanitize(value)
    assert output['checks']['daytrade_offline_tests'] == 'not_run'
    assert output['counts']['daytrade_expanded_pytest_skipped'] == 1
    value['counts']['daytrade_expanded_pytest_failed'] = 1
    with pytest.raises(ValueError, match='Incomplete'):
        publisher.sanitize(value)


def test_upstream_pass_needs_all_counts_and_skips_stay_visible():
    value = {'commit': 'a' * 40, 'checks': {'daytrade_upstream_tests': 'pass'}, 'counts': {
        'daytrade_upstream_passed': 315, 'daytrade_upstream_skipped': 2,
        'daytrade_upstream_failed': 0, 'daytrade_upstream_errors': 0,
        'daytrade_upstream_total': 317, 'daytrade_upstream_exit_code': 0}}
    out = publisher.sanitize(value)
    assert out['counts']['daytrade_upstream_skipped'] == 2
    assert out['checks']['daytrade_mt5_terminal'] == 'not_run'
    value['counts'].pop('daytrade_upstream_skipped')
    with pytest.raises(ValueError, match='Incomplete'):
        publisher.sanitize(value)
