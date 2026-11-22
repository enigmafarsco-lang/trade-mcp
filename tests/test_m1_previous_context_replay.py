"""Synthetic research-only M1 context tests; no broker, history or orders."""
from datetime import datetime, timedelta

import pytest

from trade_mcp import tick_cash_replay as cash


def metadata():
    return {'trade_contract_size': 100, 'volume_min': .01, 'volume_max': 10,
            'volume_step': .01, 'currency_profit': 'USD', 'trade_calc_mode': 3}


def ticks():
    start = datetime(2026, 1, 1)
    return [{'time_ms': (start + timedelta(seconds=i)).isoformat(), 'bid': 100., 'ask': 100.1}
            for i in range(300)]


def only_signal(monkeypatch, action='buy', second=120):
    class Signal:
        def __init__(self, **kwargs):
            pass

        def observe(self, tick, *args, **kwargs):
            return {'action': action if tick['time_msc'] == second * 1000 else 'wait'}

    monkeypatch.setattr(cash, 'TickMomentum', Signal)


def previous_direction(rows, direction):
    for index in range(60, 120):
        bid = 100 + direction * (index - 60) * .01
        rows[index]['bid'], rows[index]['ask'] = bid, bid + .1


@pytest.mark.parametrize(('action', 'direction', 'accepted'), [
    ('buy', 1, True), ('buy', -1, False), ('buy', 0, False),
    ('sell', -1, True), ('sell', 1, False), ('sell', 0, False),
])
def test_matched_baseline_and_direction_gate_use_previous_observed_interior_minute(
        monkeypatch, action, direction, accepted):
    only_signal(monkeypatch, action)
    rows = ticks()
    previous_direction(rows, direction)
    kwargs = {'consecutive_changes': 2, 'max_spread_cost_usd': 1}
    matched = cash.replay_cash_scenario(rows, metadata(), m1_context='prior_interior_available', **kwargs)
    aligned = cash.replay_cash_scenario(rows, metadata(), m1_context='prior_interior_aligned', **kwargs)
    assert matched['counts']['simulated_entries'] == 1
    assert aligned['counts'].get('simulated_entries', 0) == int(accepted)
    assert aligned['counts'].get('signals_blocked_by_previous_interior_direction', 0) == int(not accepted)
    assert aligned['research_variant']['m1_entry_context'] == 'prior_interior_aligned'
    assert aligned['research_variant']['signal_replay_counts_before_entry_context_filter'] is True
    assert aligned['execution_authorized'] is False and aligned['net_pnl'] is None


@pytest.mark.parametrize('case', ['first_interior', 'gap', 'single_previous_quote'])
def test_context_never_uses_first_partial_minute_or_nonadjacent_or_single_quote(monkeypatch, case):
    rows = ticks()
    if case == 'first_interior':
        only_signal(monkeypatch, second=60)
    elif case == 'gap':
        rows = [row for i, row in enumerate(rows) if not 120 <= i < 180]
        only_signal(monkeypatch, second=180)
    else:
        rows = [row for i, row in enumerate(rows) if not 61 <= i < 120]
        only_signal(monkeypatch, second=120)
    for mode in ('prior_interior_available', 'prior_interior_aligned'):
        result = cash.replay_cash_scenario(rows, metadata(), m1_context=mode)
        assert result['counts'].get('simulated_entries', 0) == 0
        assert result['counts']['signals_blocked_by_missing_previous_interior_minute'] == 1


def test_later_quotes_in_current_or_final_minute_do_not_choose_prior_direction(monkeypatch):
    only_signal(monkeypatch)
    rows = ticks()
    previous_direction(rows, 1)
    changed = [dict(row) for row in rows]
    for row in changed[122:]:
        row['bid'], row['ask'] = 50, 50.1
    kwargs = {'m1_context': 'prior_interior_aligned', 'max_spread_cost_usd': 1}
    original = cash.replay_cash_scenario(rows, metadata(), **kwargs)
    later_changed = cash.replay_cash_scenario(changed, metadata(), **kwargs)
    assert original['counts']['simulated_entries'] == later_changed['counts']['simulated_entries'] == 1
    assert original['counts'].get('signals_blocked_by_previous_interior_direction', 0) == 0
    assert later_changed['counts'].get('signals_blocked_by_previous_interior_direction', 0) == 0


def test_context_variant_rechecks_spread_at_next_quote(monkeypatch):
    only_signal(monkeypatch)
    rows = ticks()
    previous_direction(rows, 1)
    rows[121]['ask'] = 101
    result = cash.replay_cash_scenario(rows, metadata(), m1_context='prior_interior_aligned',
                                       consecutive_changes=2, max_spread_cost_usd=1)
    assert result['counts']['pending_entries_blocked_by_spread'] == 1
    assert result['counts'].get('simulated_entries', 0) == 0


def test_context_variant_manages_existing_position_at_final_boundary(monkeypatch):
    only_signal(monkeypatch)
    rows = ticks()
    previous_direction(rows, 1)
    for row in rows[245:]:
        row['bid'], row['ask'] = 98, 98.1
    result = cash.replay_cash_scenario(rows, metadata(), m1_context='prior_interior_aligned')
    assert result['counts']['simulated_entries'] == 1
    assert result['final_boundary_management']['cash_loss_exits'] == 1
    assert result['gross_realized_usd_model'] == pytest.approx(-4.2)
    assert result['position_open_at_sample_end'] is False


@pytest.mark.parametrize('context', [None, True, '', 'future', 'prior_direction'])
def test_context_rejects_unknown_modes(context):
    with pytest.raises(ValueError, match='Invalid offline M1 context'):
        cash.replay_cash_scenario(ticks(), metadata(), m1_context=context)


def test_new_context_modes_cannot_use_old_truncated_boundary():
    with pytest.raises(ValueError, match='requires final-boundary'):
        cash.replay_cash_scenario(ticks(), metadata(), m1_context='prior_interior_aligned',
                                  manage_final_boundary=False)


@pytest.mark.parametrize(('changes', 'cap'), [(3, None), (2, 1)])
def test_real_momentum_uses_prior_direction_and_reports_unfiltered_opportunities(changes, cap):
    rows = ticks()
    for i, row in enumerate(rows):
        if 60 <= i < 120:
            bid = 100 + (i - 60)*.01
        elif 120 <= i < 180:
            bid = 100.6 - (i - 120)*.01
        elif 180 <= i < 240:
            bid = 100 + (i - 180)*.01
        else:
            bid = 100
        row['bid'], row['ask'] = bid, bid + .1
    kwargs = {'consecutive_changes': changes, 'max_spread_cost_usd': cap}
    matched = cash.replay_cash_scenario(rows, metadata(), m1_context='prior_interior_available', **kwargs)
    aligned = cash.replay_cash_scenario(rows, metadata(), m1_context='prior_interior_aligned', **kwargs)
    m, a = matched['research_variant'], aligned['research_variant']
    assert m['eligible_current_minute_buckets'] == a['eligible_current_minute_buckets'] == 2
    assert m['previous_observed_bid_directions'] == a['previous_observed_bid_directions'] == {
        'up': 1, 'down': 1, 'flat': 0}
    raw = m['raw_signals_before_position_latch_spread_or_context_filters']
    assert raw == a['raw_signals_before_position_latch_spread_or_context_filters']
    assert raw['previous_available'] > 0 and raw['previous_available_and_direction_aligned'] == 0
    assert raw['previous_unavailable'] + raw['previous_available'] == sum(
        matched['signal_replay']['signal_counts'].values())
    assert matched['counts']['simulated_entries'] > 0
    assert aligned['counts'].get('simulated_entries', 0) == 0
    assert aligned['counts']['signals_blocked_by_previous_interior_direction'] > 0
    assert matched['net_pnl'] is aligned['net_pnl'] is None


@pytest.mark.parametrize('manage', [True, False])
@pytest.mark.parametrize(('changes', 'cap'), [(3, None), (2, 1)])
def test_explicit_no_context_preserves_existing_scenario_shape(manage, changes, cap):
    rows = ticks()
    previous_direction(rows, 1)
    kwargs = {'consecutive_changes': changes, 'max_spread_cost_usd': cap, 'manage_final_boundary': manage}
    default = cash.replay_cash_scenario(rows, metadata(), **kwargs)
    explicit = cash.replay_cash_scenario(rows, metadata(), m1_context='none', **kwargs)
    assert default == explicit
    assert 'm1_entry_context' not in default.get('research_variant', {})
