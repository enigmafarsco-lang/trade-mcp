"""Synthetic scenario tests, never evidence of actual trading profitability."""
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trade_mcp import tick_cash_replay as cash


def metadata():
    return {'trade_contract_size': 100, 'volume_min': .01, 'volume_max': 10,
            'volume_step': .01, 'currency_profit': 'USD', 'trade_calc_mode': 3}


def ticks():
    start = datetime(2026, 11, 20, 19, 23)
    return [{'time_ms': (start+timedelta(seconds=i)).isoformat(timespec='milliseconds'),
             'bid': 100, 'ask': 100.1} for i in range(300)]


def first_buy(monkeypatch):
    class OneSignal:
        def __init__(self, **kwargs):
            self.called = False

        def observe(self, *args, **kwargs):
            if not self.called:
                self.called = True
                return {'action': 'buy'}
            return {'action': 'wait'}
    monkeypatch.setattr(cash, 'TickMomentum', OneSignal)


def test_fill_uses_next_quote_not_signal_quote_and_keeps_open_mark(monkeypatch):
    first_buy(monkeypatch)
    rows = ticks()
    for r in rows[61:]:
        r['bid'], r['ask'] = 103, 103.1
    report = cash.replay_cash_scenario(rows, metadata())
    assert report['closed_scenarios'] == 0
    assert report['position_open_at_sample_end'] is True
    assert report['gross_open_mark_usd_model'] == pytest.approx(-.2)
    assert report['net_pnl'] is None
    assert report['commission'] is None
    assert report['execution_authorized'] is False


def test_threshold_overshoot_is_not_clipped(monkeypatch):
    first_buy(monkeypatch)
    rows = ticks()
    for r in rows[62:]:
        r['bid'], r['ask'] = 102, 102.1
    report = cash.replay_cash_scenario(rows, metadata())
    assert report['closed_scenarios'] == 1
    assert report['gross_realized_usd_model'] == pytest.approx(3.8)
    assert report['counts']['cash_profit_exits'] == 1


def test_one_position_spread_loss_and_simulated_three_loss_latch(monkeypatch):
    class AlwaysBuy:
        def __init__(self, **kwargs):
            pass

        def observe(self, *args, **kwargs):
            return {'action': 'buy'}
    monkeypatch.setattr(cash, 'TickMomentum', AlwaysBuy)
    rows = ticks()
    for r in rows:
        r['ask'] = 102
    report = cash.replay_cash_scenario(rows, metadata())
    assert report['counts']['simulated_entries'] == 3
    assert report['gross_realized_usd_model'] == -12
    assert report['simulated_three_consecutive_loss_latch'] is True
    assert report['position_open_at_sample_end'] is False


@pytest.mark.parametrize('key,value', [('trade_contract_size', None), ('currency_profit', 'EUR'),
                                      ('trade_calc_mode', 1), ('volume_min', .1), ('volume_step', .03)])
def test_no_economic_defaults_or_currency_conversion(key, value):
    info = metadata()
    info[key] = value
    with pytest.raises(ValueError):
        cash.economics(info)


def test_metadata_only_one_read_tool_with_no_account_or_orders():
    path = Path(__file__).resolve().parents[1]/'scripts/run_xauusd_cash_research.py'
    spec = importlib.util.spec_from_file_location('cash_job', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    calls = []

    class Client:
        def list_tools(self):
            return [{'name': m.TOOL, 'inputSchema': {
                'properties': {k: {} for k in ('symbol', 'limit', 'include_hidden')}}}]

        def call_tool(self, name, args):
            calls.append((name, args))
            return {'structuredContent': {'symbols': [dict(metadata(), name='XAUUSD_i')]}}
    assert m.read_metadata_once(Client(), 'XAUUSD_i') == metadata()
    assert calls == [('get_marketwatch_symbols', {'symbol': 'XAUUSD_i', 'limit': 10, 'include_hidden': True})]


@pytest.mark.parametrize('mode', ['cfd leverage', 'CFD LEVERAGE', ' cfd   leverage ',
                                 'cfd_leverage', 'cfdleverage', 6, 6.0,
                                 'SYMBOL_CALC_MODE_CFDLEVERAGE'])
def test_broker_cfd_leverage_display_aliases_match_same_linear_model(mode):
    info = metadata()
    info['trade_calc_mode'] = mode
    assert cash.economics(info) == 2


def test_explicit_baseline_options_preserve_original_scenario():
    rows = ticks()
    for index, row in enumerate(rows):
        row['bid'] = 100 + index*.01
        row['ask'] = row['bid'] + .1
    original = cash.replay_cash_scenario(rows, metadata())
    explicit = cash.replay_cash_scenario(rows, metadata(), consecutive_changes=3,
                                         max_spread_cost_usd=None)
    assert explicit == original
    assert 'research_variant' not in original


def test_offline_spread_filter_blocks_signals_without_resetting_actual_latch(monkeypatch):
    class AlwaysBuy:
        def __init__(self, **kwargs):
            pass

        def observe(self, *args, **kwargs):
            return {'action': 'buy'}
    monkeypatch.setattr(cash, 'TickMomentum', AlwaysBuy)
    rows = ticks()
    for row in rows:
        row['ask'] = 102
    report = cash.replay_cash_scenario(rows, metadata(), max_spread_cost_usd=1)
    assert report['counts']['signals_blocked_by_spread'] > 0
    assert report['counts'].get('simulated_entries', 0) == 0
    assert report['simulated_three_consecutive_loss_latch'] is False
    assert report['execution_authorized'] is False
    assert report['net_pnl'] is None


def test_offline_spread_filter_rechecks_next_quote_before_hypothetical_fill(monkeypatch):
    first_buy(monkeypatch)
    rows = ticks()
    rows[61]['ask'] = 101  # Wide on next quote only, not on the signal quote.
    report = cash.replay_cash_scenario(rows, metadata(), max_spread_cost_usd=1)
    assert report['counts']['pending_entries_blocked_by_spread'] == 1
    assert report['counts'].get('simulated_entries', 0) == 0
    assert report['position_open_at_sample_end'] is False


def test_offline_two_changes_is_distinct_from_original_three():
    rows = ticks()
    for index, row in enumerate(rows):
        row['bid'] = 100 + index*.01
        row['ask'] = row['bid'] + .1
    baseline = cash.replay_cash_scenario(rows, metadata())
    alternate = cash.replay_cash_scenario(rows, metadata(), consecutive_changes=2,
                                          max_spread_cost_usd=1)
    assert alternate['research_variant']['consecutive_changes'] == 2
    assert alternate['research_variant']['spread_checked_at_signal_and_next_quote'] is True
    assert (alternate['signal_replay']['signal_counts']['buy']
            > baseline['signal_replay']['signal_counts']['buy'])
    assert alternate['thresholds_gross_usd'] == baseline['thresholds_gross_usd'] == {'loss': -2, 'profit': 2}


@pytest.mark.parametrize('cap', [True, -1, float('nan'), float('inf'), '1'])
def test_offline_filter_fails_closed_on_invalid_cap(cap):
    with pytest.raises(ValueError):
        cash.replay_cash_scenario(ticks(), metadata(), max_spread_cost_usd=cap)


@pytest.mark.parametrize('n', [True, 0, 1, 4, 2.0])
def test_offline_variants_reject_unsupported_consecutive_changes(n):
    with pytest.raises(ValueError):
        cash.replay_cash_scenario(ticks(), metadata(), consecutive_changes=n)


def test_local_economics_job_cannot_connect_to_mt5(tmp_path, monkeypatch):
    import json

    path = Path(__file__).resolve().parents[1]/'scripts/run_xauusd_cash_research.py'
    spec = importlib.util.spec_from_file_location('cash_local_job', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    def forbidden(*args, **kwargs):
        pytest.fail('Offline mode must not connect, load .env or request metadata')

    monkeypatch.setattr(m, 'load_dotenv', forbidden)
    monkeypatch.setattr(m, 'McpHttpClient', type('OfflineClient', (), {
        'structured_content': staticmethod(lambda result: result['structuredContent']),
        '__init__': forbidden}))
    raw = tmp_path/'raw.json'
    raw.write_text(json.dumps({'structuredContent': {'ticks': ticks()}}))
    meta = tmp_path/'symbol-economics.json'
    info = metadata()
    info['trade_calc_mode'] = 'cfd leverage'
    meta.write_text(json.dumps(info))
    out = tmp_path/'output'
    monkeypatch.setattr(m.sys, 'argv', ['job', str(raw), str(out), '--economics', str(meta)])
    assert m.main() == 0
    report = json.loads((out/'summary.json').read_text())
    assert report['metadata_tool_calls'] == 0
    assert report['status'] == 'gross_scenario_executed_net_costs_unknown'
    assert report['scenario']['net_pnl'] is None
    assert report['scenario']['execution_authorized'] is False
