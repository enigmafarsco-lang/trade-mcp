"""Synthetic-only M1 day-trader tests: no broker history, model or orders."""
import asyncio
import copy
import json
import math
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from trade_mcp.daytrade import check_alert, entry_signal, exit_plan, m1_rows, run_request
from trade_mcp.daytrade_sim import historical_replay, time_machine

BASE = 1_700_000_000 // 60 * 60
SPEC = dict(trade_tick_size=.01, trade_tick_value=1, volume_step=.01,
            volume_min=.01, volume_max=100, point=.01, trade_stops_level=0)


@pytest.fixture(autouse=True)
def isolated_cash_policy(monkeypatch):
    # Do not let a workstation's live policy change synthetic fixture expectations.
    monkeypatch.setenv('TRADE_MCP_DEMO_CASH_POLICY', 'false')


def row(stamp, op=100, hi=101, lo=99, close=100, volume=100, spread=None):
    result = [stamp, op, hi, lo, close, volume]
    return result if spread is None else [*result, spread]


def breakout_rows():
    return [row(BASE + i * 60) for i in range(5)] + [
        row(BASE + 5 * 60, hi=102, close=101),  # equal to range high: no breakout
        row(BASE + 6 * 60, hi=103, close=102),
        row(BASE + 7 * 60, hi=104, close=103),
        row(BASE + 8 * 60, hi=105, close=104),  # forming, ignored
    ]


def sessions(n=20, size=8, *, special=None):
    result = []
    spacing = max(3600, size * 60 + 60)
    for day in range(n):
        start = BASE + day * spacing
        bars = [row(start + i * 60, hi=100.4, lo=99.6) for i in range(size)]
        if special:
            for index, values in special.items():
                bars[index] = row(start + index * 60, *values)
        result.append(bars)
    return result


def decision_for(data):
    return int(data[-1][-1][0] + 60)


def machine(data, **kwargs):
    params = dict(decision_time=decision_for(data), side='buy', entry=100, stop=99,
                  target=101, entry_slot=0, horizon_bars=1, replays=10, seed=19, window=0)
    params.update(kwargs)
    return time_machine(data, **params)


def test_first_closed_range_breakout_excludes_forming_and_reports_age():
    bars = breakout_rows()
    result = entry_signal(bars, side='buy', start_time=BASE, opening_range_bars=5)
    assert result['status'] == 'triggered'
    assert result['trigger']['slot'] == 6
    assert result['trigger']['close'] == 102
    assert result['trigger']['bar_close_time'] == BASE + 7 * 60
    assert result['closed_bars_since_trigger'] == 1
    assert result['trigger_is_latest_closed_bar'] is False
    assert result['opening_range']['high'] == 101 and result['opening_range']['low'] == 99
    assert result['last_supplied_bar_excluded'] and not result['freshness_validated_for_execution']
    assert result['execution_authorized'] is False
    changed = copy.deepcopy(bars)
    changed[-1] = row(BASE + 8 * 60, hi=200, lo=98, close=199, volume=900)
    assert entry_signal(changed, side='buy', start_time=BASE) == result


def test_forming_price_alone_cannot_trigger_and_short_is_symmetric():
    bars = [row(BASE + i * 60) for i in range(7)]
    bars[-1] = row(BASE + 6 * 60, hi=110, close=109)
    assert entry_signal(bars, side='buy', start_time=BASE)['status'] == 'waiting'
    bars[-2] = row(BASE + 5 * 60, hi=101, lo=97, close=98)
    short = entry_signal(bars, side='sell', start_time=BASE)
    assert short['trigger']['slot'] == 5 and short['level'] == 99
    assert short['trigger_is_latest_closed_bar']


def test_level_cutoff_volume_filter_and_incomplete_range():
    bars = breakout_rows()
    bars[5][4], bars[5][5] = 102, 110
    bars[6][5] = 250
    out = entry_signal(bars, side='buy', start_time=BASE, min_tick_volume_ratio=2)
    assert out['trigger']['slot'] == 6 and out['volume_rejections'] == 1
    assert out['trigger']['tick_volume_ratio'] == 2.5
    fixed = entry_signal(bars, side='buy', start_time=BASE + 5 * 60,
                         setup='level', level=101, cutoff_time=BASE + 6 * 60)
    assert fixed['trigger']['slot'] == 0
    expired = entry_signal(bars, side='buy', start_time=BASE + 5 * 60,
                           setup='level', level=120, cutoff_time=BASE + 6 * 60)
    assert expired['status'] == 'expired'
    forming = entry_signal(bars[:5], side='buy', start_time=BASE, opening_range_bars=5)
    assert forming['status'] == 'range_forming' and forming['opening_range']['bars'] == 4
    waiting = entry_signal(bars, side='buy', start_time=BASE + 12 * 60)
    assert waiting['status'] == 'waiting_for_start'


def test_gaps_are_not_silently_bridged_even_with_a_trigger():
    bars = breakout_rows()
    with pytest.raises(ValueError, match='missing'):
        entry_signal(bars[:2] + bars[3:], side='buy', start_time=BASE)
    # Huge timestamp gaps must fail immediately, not loop over trillions of minutes.
    with pytest.raises(ValueError, match='missing'):
        entry_signal([row(BASE), row(8_000_000_000_000_000 // 60 * 60)], side='buy', start_time=BASE)
    with pytest.raises(ValueError, match='precedes'):
        entry_signal(bars, side='buy', start_time=BASE - 60)


@pytest.mark.parametrize('mutation', [
    lambda b: b[1].__setitem__(0, b[0][0]),
    lambda b: b[1].__setitem__(0, b[1][0] + 1),
    lambda b: b[1].__setitem__(2, 90),
    lambda b: b[1].__setitem__(4, math.nan),
    lambda b: b[1].__setitem__(5, -1),
    lambda b: b[1].__setitem__(1, True),
    lambda b: b[1].__setitem__(1, '100'),
    lambda b: b[1].append(-1),
])
def test_bad_m1_rows_rejected(mutation):
    bars = breakout_rows()
    mutation(bars)
    with pytest.raises(ValueError):
        m1_rows(bars)


def test_get_candles_seven_columns_and_unknown_spread_supported():
    bars = [r + [None if i == 0 else 20] for i, r in enumerate(breakout_rows())]
    assert entry_signal(bars, side='buy', start_time=BASE)['status'] == 'triggered'


def test_volume_filter_requires_a_real_baseline_and_level_contract():
    bars = breakout_rows()
    for item in bars[:5]:
        item[5] = 0
    with pytest.raises(ValueError, match='volume is zero'):
        entry_signal(bars, side='buy', start_time=BASE, min_tick_volume_ratio=1)
    with pytest.raises(ValueError, match='requires an opening range'):
        entry_signal(bars, side='buy', start_time=BASE, setup='level', level=100,
                     min_tick_volume_ratio=1)


@pytest.mark.parametrize(('side', 'rule', 'value', 'extra', 'expected'), [
    ('buy', 'price', 98, {}, 98),
    ('buy', 'distance', 2, {}, 98),
    ('sell', 'distance', 2, {}, 102),
    ('buy', 'percent', 2, {}, 98),
    ('sell', 'percent', 2, {}, 102),
    ('buy', 'atr', 2, {'atr_value': .5}, 99),
    ('sell', 'atr', 2, {'atr_value': .5}, 101),
    ('buy', 'range_edge', None, {'range_high': 99, 'range_low': 98}, 98),
    ('sell', 'range_edge', None, {'range_high': 102, 'range_low': 101}, 102),
    ('buy', 'range_mid', None, {'range_high': 99, 'range_low': 97}, 98),
])
def test_exit_stop_rules_for_buy_and_sell(side, rule, value, extra, expected):
    plan = exit_plan(side=side, entry=100, stop_rule=rule, stop_value=value,
                     target_r=2, **extra)
    assert plan['stop'] == expected and plan['gross_reward_risk'] == pytest.approx(2)
    assert plan['executable'] is False
    assert plan['target'] == (100 + 2 * abs(100 - expected) if side == 'buy'
                               else 100 - 2 * abs(100 - expected))


def test_tick_rounding_across_seeded_hypothetical_plans():
    import random
    from decimal import Decimal

    rng = random.Random(19)
    for _ in range(1000):
        side = rng.choice(('buy', 'sell'))
        entry = 2000 + rng.randrange(1000) / 100
        distance = rng.randrange(10, 300) / 100
        ratio = rng.randrange(5, 31) / 10
        plan = exit_plan(side=side, entry=entry, stop_rule='distance', stop_value=distance,
                         target_r=ratio, tick_size=.05)
        assert Decimal(str(plan['stop'])) % Decimal('.05') == 0
        assert Decimal(str(plan['target'])) % Decimal('.05') == 0
        assert plan['gross_reward_risk'] <= ratio + 1e-10
        if side == 'buy':
            assert plan['stop'] <= entry - distance + 1e-10 and plan['target'] > entry
        else:
            assert plan['stop'] >= entry + distance - 1e-10 and plan['target'] < entry
        assert not plan['execution_authorized']


def test_tick_rounding_never_inflates_reward_risk_and_target_price():
    long = exit_plan(side='buy', entry=100.03, stop_rule='distance', stop_value=.31,
                     target_r=1.5, tick_size=.05)
    assert long['stop'] == 99.7 and long['target'] == 100.5
    assert long['gross_reward_risk'] < 1.5
    short = exit_plan(side='sell', entry=100.03, stop_rule='distance', stop_value=.31,
                      target_r=1.5, tick_size=.05)
    assert short['stop'] == 100.35 and short['target'] == 99.55
    assert short['gross_reward_risk'] == pytest.approx(1.5)
    fixed = exit_plan(side='buy', entry=100, stop_rule='price', stop_value=99,
                      target_r=None, target_price=102)
    assert fixed['target'] == 102 and fixed['target_rule'] == 'price'


@pytest.mark.parametrize('params', [
    {'side': 'buy', 'entry': 100, 'stop_value': 101},
    {'side': 'sell', 'entry': 100, 'stop_value': 99},
    {'side': 'buy', 'entry': 100, 'stop_value': 99, 'target_price': 102},
    {'side': 'buy', 'entry': 100, 'stop_value': 99, 'target_r': 0},
    {'side': 'buy', 'entry': 100, 'stop_value': math.nan},
    {'side': 'buy', 'entry': True, 'stop_value': 99},
    {'side': 'buy', 'entry': 100, 'stop_rule': 'percent', 'stop_value': 100},
    {'side': 'buy', 'entry': 100, 'stop_rule': 'atr', 'stop_value': 2},
])
def test_invalid_exit_plans_rejected(params):
    with pytest.raises(ValueError):
        exit_plan(**params)


def test_sizing_never_assumes_unknown_costs_and_obeys_caps(mock_app):
    size = dict(spec=SPEC, budget=10000, risk_percent=99)
    plan = exit_plan(side='buy', entry=100, stop_value=99, target_r=2,
                     tick_size=.01, sizing=size, cfg=mock_app.cfg)
    preview = plan['sizing']
    assert preview['status'] == 'costs_unknown'
    assert preview['volume'] is None and preview['estimated_stop_loss'] is None
    assert set(preview['missing_costs']) == {'commission_per_lot', 'slippage_price'}
    assert preview['applied_risk_percent'] == 1
    plan = exit_plan(side='buy', entry=100, stop_value=99, target_r=2, tick_size=.01,
                     sizing={**size, 'commission_per_lot': 2, 'slippage_price': .01}, cfg=mock_app.cfg)
    preview = plan['sizing']
    assert preview['status'] == 'estimate_only' and preview['volume'] == .1
    assert preview['estimated_stop_loss'] <= preview['hypothetical_risk_limit']
    assert preview['execution_authorized'] is False and not preview['account_and_broker_validated']


def test_min_lot_and_config_caps_block_instead_of_rounding_up(mock_app):
    size = dict(spec=SPEC, budget=100, risk_percent=1, commission_per_lot=2, slippage_price=.01)
    result = exit_plan(side='buy', entry=100, stop_value=99, target_r=2, tick_size=.01,
                       sizing=size, cfg=mock_app.cfg)['sizing']
    assert result['volume'] == 0 and result['status'] == 'blocked'
    assert result['gross_loss_at_stop_for_minimum_lot'] == 1
    mock_app.cfg.max_lot = .0099999999999
    result = exit_plan(side='buy', entry=100, stop_value=99, target_r=2, tick_size=.01,
                       sizing={**size, 'budget': 10000}, cfg=mock_app.cfg)['sizing']
    assert result['volume'] == 0 and result['status'] == 'blocked'


def test_demo_cash_hypothetical_cap_without_claiming_account_is_verified(mock_app):
    mock_app.cfg.demo_cash_policy = True
    result = exit_plan(side='buy', entry=100, stop_value=99, target_r=2, tick_size=.01,
                       sizing=dict(spec=SPEC, budget=10000, risk_percent=1,
                                   commission_per_lot=0, slippage_price=0), cfg=mock_app.cfg)['sizing']
    assert result['hypothetical_risk_limit'] == 10
    assert result['estimated_stop_loss'] <= 10
    assert result['account_currency'] == 'not verified'


def test_sizing_rejects_bad_specs_and_marks_broker_constraints(mock_app):
    base = dict(spec=SPEC, budget=10000, risk_percent=1, commission_per_lot=0, slippage_price=0)
    with pytest.raises(ValueError, match='volume'):
        exit_plan(side='buy', entry=100, stop_value=99, sizing={**base, 'spec': {**SPEC, 'volume_min': .015}},
                  cfg=mock_app.cfg)
    result = exit_plan(side='buy', entry=100, stop_value=99.005, target_r=1,
                       sizing=base, cfg=mock_app.cfg)['sizing']
    assert result['status'] == 'blocked' and result['volume'] == 0
    assert any('grid' in note for note in result['blockers'])


@pytest.mark.parametrize(('side', 'bid', 'ask', 'status', 'exit_side'), [
    ('buy', 99, 99.2, 'stop_touched', 'bid'),
    ('buy', 100.9, 101, 'neither', 'bid'),
    ('buy', 101, 101.2, 'target_touched', 'bid'),
    ('sell', 100.8, 101, 'stop_touched', 'ask'),
    ('sell', 98.8, 99, 'target_touched', 'ask'),
])
def test_one_shot_alert_uses_correct_bid_ask(side, bid, ask, status, exit_side):
    stop, target = (99, 101) if side == 'buy' else (101, 99)
    out = check_alert(side=side, stop=stop, target=target, bid=bid, ask=ask)
    assert out['status'] == status and out['observed_exit_side'] == exit_side
    assert not out['automatic_monitoring'] and not out['quote_freshness_verified']
    assert not out['execution_authorized']


def test_ambiguous_bars_stop_first_and_gap_stops_can_exceed_one_r():
    both = sessions(special={1: (100, 102, 98, 100, 100)})
    out = machine(both)
    assert out['counts'] == {'target': 0, 'stop': 10, 'open': 0}
    assert out['ambiguous_bar_stop_first_count'] == 10
    assert out['mean_realized_gross_r_closed_only'] == -1
    gap = sessions(special={1: (98.5, 101.5, 98, 100, 100)})
    out = machine(gap)
    assert out['gap_stop_count'] == 10
    assert out['mean_realized_gross_r_closed_only'] == -1.5
    # At the open the target is already reached, so a later low cannot be first.
    target_gap = sessions(special={1: (101.5, 102, 98, 100, 100)})
    assert machine(target_gap)['counts']['target'] == 10
    short_gap = sessions(special={1: (101.5, 102, 98.5, 100, 100)})
    short = machine(short_gap, side='sell', stop=101, target=99)
    assert short['gap_stop_count'] == 10
    assert short['mean_realized_gross_r_closed_only'] == pytest.approx(-1.5)


def test_open_positions_remain_open_past_one_minute_and_seed_is_reproducible():
    data = sessions()
    out = machine(data, horizon_bars=3, replays=30, window=2)
    assert out == machine(data, horizon_bars=3, replays=30, window=2)
    assert out['counts'] == {'target': 0, 'stop': 0, 'open': 30}
    assert out['mean_realized_gross_r_closed_only'] is None
    assert out['mean_unrealized_gross_r_open_only'] == 0
    assert not out['forced_exit_at_horizon'] and out['horizon_bars'] == 3
    assert out['net_profit'] is None and not out['costs_verified']
    assert len(out['price_bands']) == 4 and out['band_path_count'] == 30


def test_simulation_rejects_missing_sessions_lookahead_gaps_overlap_and_excessive_work():
    data = sessions()
    with pytest.raises(ValueError, match='20 to'):
        machine(data[:19])
    with pytest.raises(ValueError, match='no later'):
        machine(data, decision_time=decision_for(data) - 60)
    gap = copy.deepcopy(data)
    gap[0][2][0] += 60
    with pytest.raises(ValueError):
        machine(gap)
    overlap = copy.deepcopy(data)
    overlap[1] = copy.deepcopy(overlap[0])
    with pytest.raises(ValueError, match='non-overlapping'):
        machine(overlap)
    unequal = copy.deepcopy(data)
    unequal[0].pop()
    with pytest.raises(ValueError, match='equal lengths'):
        machine(unequal)
    big = sessions(size=250)
    with pytest.raises(ValueError, match='calculation limit'):
        machine(big, horizon_bars=201, replays=10000)


def replay_rows():
    # Range high 101, low 99; signal at slot 2; assumed next-open fill 102.
    return [[
        row(BASE, hi=101, lo=99),
        row(BASE + 60, hi=101, lo=99),
        row(BASE + 120, hi=102, lo=99.5, close=101.5),
        row(BASE + 180, op=102, hi=103, lo=101, close=102),
        row(BASE + 240, op=102, hi=103, lo=100, close=102),
        row(BASE + 300, op=102, hi=103, lo=100, close=102),
    ]]


def replay(data, **kwargs):
    params = dict(decision_time=decision_for(data), side='buy', opening_range_bars=2,
                  stop_rule='range_edge', target_r=1, horizon_bars=3)
    params.update(kwargs)
    return historical_replay(data, **params)


def test_historical_entry_is_next_open_and_horizon_does_not_close_position():
    out = replay(replay_rows())
    detail = out['segments'][0]
    assert detail['trigger_close'] == 101.5 and detail['assumed_entry'] == 102
    assert detail['stop'] == 99 and detail['target'] == 105
    assert detail['status'] == 'open' and detail['bars_observed'] == 3
    assert out['counts']['open'] == 1 and out['realized_gross_r_sum'] == 0
    assert not out['forced_exit_at_horizon'] and out['net_profit'] is None


def test_historical_gap_stop_and_intrabar_ambiguity():
    data = replay_rows()
    data[0][3] = row(BASE + 180, op=102, hi=106, lo=98, close=102)
    out = replay(data)
    assert out['segments'][0]['status'] == 'stop'
    assert out['ambiguous_bar_stop_first_count'] == 1
    assert out['realized_gross_r_sum'] == -1
    data = replay_rows()
    data[0][4] = row(BASE + 240, op=98, hi=100, lo=97, close=99)
    out = replay(data)
    assert out['segments'][0]['assumed_exit'] == 98
    assert out['segments'][0]['gross_r'] == pytest.approx(-4/3)
    assert out['gap_stop_count'] == 1


def test_historical_missing_next_bar_invalid_plan_cutoff_and_short():
    data = replay_rows()
    assert replay(data, cutoff_slot=2)['counts']['open'] == 1
    data[0][2] = row(BASE + 120, hi=101, lo=99.5, close=101)
    data[0][3] = row(BASE + 180, op=100, hi=101, lo=99, close=100)
    data[0][4] = row(BASE + 240, op=100, hi=101, lo=99, close=100)
    data[0][5] = row(BASE + 300, op=100, hi=102, lo=99, close=101.5)
    assert replay(data)['counts']['no_next_bar'] == 1
    assert replay(data, cutoff_slot=2)['counts']['no_signal'] == 1
    data = replay_rows()
    data[0][3] = row(BASE + 180, op=98, hi=100, lo=97, close=99)
    assert replay(data)['counts']['invalid_plan'] == 1
    assert replay(replay_rows(), stop_rule='atr', stop_value=1)['counts']['invalid_plan'] == 1
    short = [[row(BASE, hi=101, lo=99), row(BASE + 60, hi=101, lo=99),
              row(BASE + 120, hi=100, lo=97, close=98),
              row(BASE + 180, op=98, hi=99, lo=97, close=98)]]
    assert replay(short, side='sell')['segments'][0]['stop'] == 101


def test_dispatch_rejects_unknown_fields():
    with pytest.raises(ValueError, match='unknown'):
        run_request('alerts', {'side': 'buy', 'stop': 99, 'target': 101, 'bid': 100, 'ask': 100.1,
                               'execute': True})


def test_mcp_tools_use_no_broker_model_or_proposal(mock_app, monkeypatch):
    import trade_mcp.app as app_mod
    from trade_mcp import server

    monkeypatch.setattr(app_mod, '_APP', mock_app)
    monkeypatch.setattr(mock_app.mt5, 'request', lambda *a, **k: pytest.fail('broker call forbidden'))
    monkeypatch.setattr('trade_mcp.agents.LLMClient.chat', lambda *a, **k: pytest.fail('model call forbidden'))
    before = mock_app.store.list_proposals()
    result = server.daytrade_entry_signal(breakout_rows(), 'buy', BASE)
    assert result['status'] == 'triggered'
    plan = server.daytrade_exit_plan('buy', 100, stop_value=99, target_r=2, tick_size=.01,
        sizing=dict(spec=SPEC, budget=10000, risk_percent=1, commission_per_lot=0, slippage_price=0))
    assert plan['sizing']['status'] == 'estimate_only'
    data = sessions()
    assert server.daytrade_historical_replay(data, decision_for(data), 'buy')['ok']
    assert server.daytrade_time_machine(data, decision_for(data), 'buy', 100, 99, 101, 0, 3)['ok']
    assert server.daytrade_check_alert('buy', 99, 101, 100, 100.2)['status'] == 'neither'
    invalid = server.daytrade_exit_plan('buy', 100, stop_value=101)
    assert invalid['error'] == 'invalid_input' and not invalid['execution_authorized']
    assert mock_app.store.list_proposals() == before
    assert mock_app.store.list_model_usage() == []
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert {'daytrade_entry_signal', 'daytrade_exit_plan', 'daytrade_historical_replay',
            'daytrade_time_machine', 'daytrade_check_alert'} <= names
    wire_result = asyncio.run(server.mcp.call_tool('daytrade_entry_signal',
                             {'ohlcv': breakout_rows(), 'side': 'buy', 'start_time': BASE}))
    if isinstance(wire_result, tuple):
        wire_payload = wire_result[1]
    else:
        wire_payload = json.loads(wire_result[0].text)
    assert wire_payload['status'] == 'triggered' and not wire_payload['execution_authorized']
    # The real MCP validation path must not coerce booleans into prices or timestamps.
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        asyncio.run(server.mcp.call_tool('daytrade_exit_plan', {'side': 'buy', 'entry': True, 'stop_value': 99}))


def test_mcp_sizing_without_initialized_app_does_not_start_bridge_or_journal(monkeypatch):
    import trade_mcp.app as app_mod
    from trade_mcp import server

    monkeypatch.setattr(app_mod, '_APP', None)
    monkeypatch.setattr(app_mod, 'build_app', lambda *a, **k: pytest.fail('app must not start'))
    monkeypatch.setenv('TRADE_MCP_BACKEND', 'file')
    monkeypatch.setenv('TRADE_MCP_FILES_DIR', '')
    monkeypatch.setenv('TRADE_MCP_TRAINING_BUDGET', '100')
    monkeypatch.setenv('TRADE_MCP_MAX_RISK_PERCENT', '1')
    monkeypatch.setenv('TRADE_MCP_MAX_LOT', '0.1')
    monkeypatch.setenv('TRADE_MCP_DEMO_CASH_POLICY', 'false')
    result = server.daytrade_exit_plan('buy', 100, stop_value=99, tick_size=.01,
        sizing=dict(spec=SPEC, budget=100, risk_percent=1, commission_per_lot=0, slippage_price=0))
    assert result['ok'] and result['sizing']['status'] == 'estimate_only'
    assert app_mod._APP is None


def test_console_api_is_offline_and_keeps_origin_guard(mock_app, monkeypatch):
    from test_terminal import console

    monkeypatch.setattr(mock_app.mt5, 'request', lambda *a, **k: pytest.fail('broker call forbidden'))
    monkeypatch.setattr('trade_mcp.agents.LLMClient.chat', lambda *a, **k: pytest.fail('model call forbidden'))
    before = mock_app.store.list_proposals()
    with console(mock_app) as base:
        def post(path, body, **headers):
            req = Request(base + path, data=json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json', **headers})
            with urlopen(req) as response:
                return json.load(response)
        entry = post('/api/daytrade/entry', {'ohlcv': breakout_rows(), 'side': 'buy', 'start_time': BASE})
        assert entry['status'] == 'triggered'
        plan = post('/api/daytrade/plan', {'side': 'buy', 'entry': 100, 'stop_value': 99})
        assert plan['stop'] == 99 and plan['execution_authorized'] is False
        data = sessions()
        history = post('/api/daytrade/history', {'sessions': data, 'decision_time': decision_for(data), 'side': 'buy'})
        assert history['historical_segments'] == 20
        mc = post('/api/daytrade/time-machine', {'sessions': data, 'decision_time': decision_for(data),
                  'side': 'buy', 'entry': 100, 'stop': 99, 'target': 101, 'entry_slot': 0,
                  'horizon_bars': 3, 'replays': 10, 'window': 0})
        assert mc['counts']['open'] == 10
        alert = post('/api/daytrade/alerts', {'side': 'sell', 'stop': 101, 'target': 99, 'bid': 100, 'ask': 101})
        assert alert['status'] == 'stop_touched'
        with pytest.raises(HTTPError) as bad:
            post('/api/daytrade/entry', {'ohlcv': [], 'side': 'buy', 'start_time': BASE})
        assert bad.value.code == 400
        with pytest.raises(HTTPError) as foreign:
            post('/api/daytrade/alerts', {}, Origin='https://foreign.invalid')
        assert foreign.value.code == 403
        oversized = Request(base + '/api/daytrade/alerts', data=b'{}',
                            headers={'Content-Length': '4000001', 'Content-Type': 'application/json'})
        with pytest.raises(HTTPError) as large:
            urlopen(oversized, timeout=3)
        assert large.value.code == 413
        with urlopen(base + '/') as response:
            html = response.read().decode()
        assert 'daytrade-workbench' in html and 'daytrade-machine-run' in html
        with urlopen(base + '/workspace.js') as response:
            assert b'/api/daytrade/time-machine' in response.read()
    assert mock_app.store.list_proposals() == before and mock_app.store.list_model_usage() == []


def test_synthetic_ui_workflow_against_mock_console(mock_app, monkeypatch):
    import os
    import shutil
    import subprocess
    from pathlib import Path

    from test_terminal import console

    node = shutil.which('node')
    if not node or subprocess.run([node, '-e', 'process.exit(typeof fetch === "function" ? 0 : 1)'],
                                  capture_output=True, check=False).returncode:
        pytest.skip('Node with fetch is optional; Python and HTTP tests remain available')
    monkeypatch.setattr(mock_app.mt5, 'request', lambda *a, **k: pytest.fail('broker call forbidden'))
    root = Path(__file__).resolve().parents[1]
    with console(mock_app) as base:
        result = subprocess.run([node, str(root / 'tests/daytrade_web_smoke.js'),
                                 str(root / 'src/trade_mcp/web/workspace.js')],
                                env={**os.environ, 'TEST_CONSOLE_URL': base},
                                capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'synthetic UI workflow passed' in result.stdout
    assert mock_app.store.list_proposals() == [] and mock_app.store.list_model_usage() == []
