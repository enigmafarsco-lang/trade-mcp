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


@pytest.mark.parametrize(('bid', 'ask', 'expected_pnl', 'exit_key'), [
    (103, 103.1, 5.8, 'cash_profit_exits'),
    (98, 98.1, -4.2, 'cash_loss_exits'),
])
def test_final_boundary_manages_existing_position_without_new_entries(
        monkeypatch, bid, ask, expected_pnl, exit_key):
    first_buy(monkeypatch)
    rows = ticks()
    for row in rows[245:]:
        row['bid'], row['ask'] = bid, ask
    old = cash.replay_cash_scenario(rows, metadata())
    corrected = cash.replay_cash_scenario(rows, metadata(), manage_final_boundary=True)
    assert old['position_open_at_sample_end'] is True
    assert old['gross_open_mark_usd_model'] == pytest.approx(-.2)
    assert corrected['position_open_at_sample_end'] is False
    assert corrected['gross_open_mark_usd_model'] is None
    assert corrected['gross_realized_usd_model'] == pytest.approx(expected_pnl)
    assert corrected['counts'][exit_key] == 1
    assert corrected['counts']['simulated_entries'] == 1
    assert corrected['final_boundary_management'][exit_key] == 1
    assert corrected['final_boundary_management']['position_marked_quotes'] == 6
    assert corrected['final_boundary_management']['boundary_quotes_seen'] == 60
    assert corrected['commission'] is None and corrected['net_pnl'] is None
    assert corrected['execution_authorized'] is False


def test_final_boundary_closes_short_using_ask_not_bid(monkeypatch):
    class OneSell:
        def __init__(self, **kwargs):
            self.called = False

        def observe(self, *args, **kwargs):
            if not self.called:
                self.called = True
                return {'action': 'sell'}
            return {'action': 'wait'}
    monkeypatch.setattr(cash, 'TickMomentum', OneSell)
    rows = ticks()
    for row in rows[245:]:
        row['bid'], row['ask'] = 97, 97.1
    report = cash.replay_cash_scenario(rows, metadata(), manage_final_boundary=True)
    assert report['gross_realized_usd_model'] == pytest.approx(5.8)
    assert report['final_boundary_management']['cash_profit_exits'] == 1
    assert report['position_open_at_sample_end'] is False


def test_final_boundary_marks_open_position_at_last_observed_quote(monkeypatch):
    first_buy(monkeypatch)
    rows = ticks()
    for row in rows[240:]:
        row['bid'], row['ask'] = 100.4, 100.5
    rows[-1]['bid'], rows[-1]['ask'] = 100.8, 100.9
    result = cash.replay_cash_scenario(rows, metadata(), manage_final_boundary=True)
    assert result['position_open_at_sample_end'] is True
    assert result['closed_scenarios'] == 0
    assert result['gross_open_mark_usd_model'] == pytest.approx(1.4)
    assert result['final_boundary_management']['position_marked_quotes'] == 60


def test_final_boundary_closing_third_loss_sets_only_simulated_latch(monkeypatch):
    class AlwaysBuy:
        def __init__(self, **kwargs):
            pass

        def observe(self, *args, **kwargs):
            return {'action': 'buy'}
    monkeypatch.setattr(cash, 'TickMomentum', AlwaysBuy)
    rows = ticks()
    rows[61]['ask'] = rows[63]['ask'] = 102
    for row in rows[240:]:
        row['bid'], row['ask'] = 98, 98.1
    old = cash.replay_cash_scenario(rows, metadata())
    corrected = cash.replay_cash_scenario(rows, metadata(), manage_final_boundary=True)
    assert old['counts']['cash_loss_exits'] == 2
    assert old['simulated_three_consecutive_loss_latch'] is False
    assert old['position_open_at_sample_end'] is True
    assert corrected['counts']['cash_loss_exits'] == 3
    assert corrected['simulated_three_consecutive_loss_latch'] is True
    assert corrected['position_open_at_sample_end'] is False
    assert corrected['counts']['simulated_entries'] == 3
    assert corrected['final_boundary_management']['cash_loss_exits'] == 1


def test_final_boundary_expires_pending_and_does_not_generate_new_signals(monkeypatch):
    observations = []

    class LastInteriorSignal:
        def __init__(self, **kwargs):
            pass

        def observe(self, tick, *args, **kwargs):
            observations.append(tick['time_msc'])
            return {'action': 'buy' if tick['time_msc'] == 239000 else 'wait'}
    monkeypatch.setattr(cash, 'TickMomentum', LastInteriorSignal)
    rows = ticks()
    old = cash.replay_cash_scenario(rows, metadata())
    observations.clear()
    corrected = cash.replay_cash_scenario(rows, metadata(), manage_final_boundary=True)
    assert old['pending_entry_at_sample_end'] is True
    assert corrected['pending_entry_at_sample_end'] is False
    assert corrected['final_boundary_management']['pending_entries_expired'] == 1
    assert corrected['counts'].get('simulated_entries', 0) == 0
    assert observations and max(observations) == 239000


def test_final_boundary_opt_in_preserves_old_default():
    rows = ticks()
    assert cash.replay_cash_scenario(rows, metadata()) == cash.replay_cash_scenario(
        rows, metadata(), manage_final_boundary=False)
    with pytest.raises(ValueError, match='boolean'):
        cash.replay_cash_scenario(rows, metadata(), manage_final_boundary='yes')


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
