"""Offline tests for the expanded Day Trader adapters and bundled reference."""
import ast
import asyncio
import copy
import hashlib
import importlib.util
import json
import math
import random
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from trade_mcp.config import Config
from trade_mcp.daytrade import run_request
from trade_mcp.daytrade_extra import evaluate_alerts, review_limits, scan_setups
from trade_mcp.daytrade_sources import parse_news, stock_disclosures, stock_facts

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / 'third_party/day-trader-skill/upstream'
FIX = UPSTREAM / 'tests/fixtures'
BASE = 1_700_000_000 // 60 * 60
SPEC = dict(trade_tick_size=.01, trade_tick_value=1, volume_step=.01,
            volume_min=.01, volume_max=100, point=.01, trade_stops_level=0)
SIZING = dict(spec=SPEC, budget=10000, risk_percent=1, commission_per_lot=0, slippage_price=0)
LIMITS = dict(max_loss_per_trade=20, daily_risk_cap=50, risk_used_today=5,
              trades_today=1, max_trades_per_day=3, min_net_reward_risk=1)


@pytest.fixture(autouse=True)
def isolated_cash_policy(monkeypatch):
    # A workstation's live guardian must not run during these synthetic tests.
    monkeypatch.setenv('TRADE_MCP_DEMO_CASH_POLICY', 'false')


def bars(closes):
    return [[BASE + i * 60, c, c + .5, c - .5, c, 100] for i, c in enumerate(closes)]


def load_upstream(path):
    spec = importlib.util.spec_from_file_location('reference_daytrader', UPSTREAM / path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def payloads():
    market = {'symbol': 'XAUUSD_i', 'ohlcv': bars([100 + i / 100 for i in range(32)])}
    rule = {'id': 's', 'type': 'stop', 'symbol': 'XAUUSD_i', 'side': 'buy', 'price': 99}
    news_xml = ('<rss><channel><item><title>Fed inflation report</title><guid>x</guid>'
                '<link>https://example.test/x</link><pubDate>Tue, 02 Jan 2024 11:00:00 GMT</pubDate>'
                '</item></channel></rss>')
    facts = {'cik': 123, 'entityName': 'Synthetic company', 'facts': {'us-gaap': {
        'Revenues': {'units': {'USD': [
            {'start': '2024-01-01', 'end': '2024-03-31', 'filed': '2024-05-01',
             'form': '10-Q', 'val': 100, 'fy': 2024, 'fp': 'Q1', 'accn': 'x'}]}}}}}
    index = ('<FinancialDisclosure><Member><First>Alex</First><Last>Example</Last>'
             '<Year>2024</Year><DocID>123</DocID><FilingType>P</FilingType>'
             '<FilingDate>05/01/2024</FilingDate></Member></FinancialDisclosure>')
    return {
        'scan': {'markets': [market]},
        'alert-rules': {'rules': [rule], 'quotes': {'XAUUSD_i': {'bid': 99, 'ask': 99.1}}, 'now': BASE},
        'review': {'side': 'buy', 'entry': 100, 'stop': 99, 'target': 102,
                   'sizing': SIZING, 'limits': LIMITS},
        'news': {'feeds': [{'kind': 'rss', 'symbol': 'XAUUSD_i', 'source': 'Synthetic', 'xml': news_xml}],
                 'watch': {'XAUUSD_i': 'gold'}, 'now': '2024-01-02T12:00:00Z'},
        'stock-facts': {'companyfacts': facts, 'as_of': '2024-06-01'},
        'stock-disclosures': {'kind': 'index', 'text': index, 'year': 2024,
                              'as_of': '2024-06-01', 'members': ['Example']},
    }


def test_bundled_reference_files_match_manifest_and_all_eleven_skills():
    lines = (UPSTREAM.parent / 'SHA256SUMS').read_text().splitlines()
    assert len(lines) == 85
    for line in lines:
        digest, name = line.split('  ', 1)
        assert hashlib.sha256((UPSTREAM.parent / name).read_bytes()).hexdigest() == digest
    assert len(list(UPSTREAM.glob('trader-*/SKILL.md'))) == 11
    license_text = (ROOT / 'LICENSES/day-trader-skill-MIT.txt').read_text()
    assert (UPSTREAM / 'LICENSE').read_text() == license_text
    from importlib.resources import files
    assert files('trade_mcp._upstream_daytrade').joinpath('LICENSE.txt').read_text(encoding='utf-8') == license_text


@pytest.mark.parametrize(('name', 'upstream'), [
    ('facts', 'trader-analyst/facts.py'), ('news', 'trader-news/news.py'),
    ('disclosures', 'trader-pelosi/ptr.py'),
])
def test_extracted_pure_upstream_functions_retain_their_source(name, upstream):
    original = ast.parse((UPSTREAM / upstream).read_text())
    copied = ast.parse((ROOT / 'src/trade_mcp/_upstream_daytrade' / f'{name}.py').read_text())
    defs = {n.name: ast.dump(n, include_attributes=False) for n in original.body
            if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for node in copied.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            assert ast.dump(node, include_attributes=False) == defs[node.name]


def test_m1_pattern_definitions_match_original_applied_to_bars():
    upstream = load_upstream('trader-scanner/scan.py')
    rng = random.Random(901)
    for _ in range(50):
        closes = [100.0]
        for _i in range(35):
            closes.append(max(1, closes[-1] + rng.uniform(-1.5, 1.5)))
        original, numbers = upstream.find_setups(closes[:-1])
        result = scan_setups([{'symbol': 'XAUUSD_i', 'ohlcv': bars(closes)}])['results'][0]
        expected = ['breakout_20_bars' if name == 'breakout_20d' else name for name in original]
        assert result['setups'] == expected
        assert result['rsi14'] == pytest.approx(numbers['rsi'])
        assert result['previous_rsi14'] == pytest.approx(numbers['rsi_prev'])
        assert result['high20_previous_closes'] == pytest.approx(numbers['high20'])
        assert result['ma20'] == pytest.approx(numbers['ma20'])


def test_scanner_excludes_latest_reports_partial_and_never_fills_gaps():
    data = bars([100 + i / 100 for i in range(32)])
    changed = copy.deepcopy(data)
    changed[-1] = [changed[-1][0], 300, 301, 299, 300, 9]
    first = scan_setups([{'symbol': 'gold', 'ohlcv': data}])
    assert first == scan_setups([{'symbol': 'gold', 'ohlcv': changed}])
    assert first['results'][0]['setups'] == ['breakout_20_bars']
    assert first['counts'] == {'requested': 1, 'unusable': 0, 'hits': 1}
    broken = copy.deepcopy(data)
    del broken[7]
    partial = scan_setups([{'symbol': 'gold', 'ohlcv': broken},
                           {'symbol': 'short', 'ohlcv': data[:10]}])
    assert [r['status'] for r in partial['results']] == ['invalid_data', 'insufficient_history']
    assert partial['counts']['unusable'] == 2 and not partial['execution_authorized']
    with pytest.raises(ValueError, match='Duplicate'):
        scan_setups([{'symbol': 'a', 'ohlcv': data}, {'symbol': 'a', 'ohlcv': data}])
    with pytest.raises(ValueError, match='opening_range'):
        scan_setups([{'symbol': 'a', 'ohlcv': data}], start_time=BASE)


def test_scanner_multi_range_detects_first_closed_bar_not_last():
    seq = bars([100] * 5 + [101, 102, 103])
    # Range high is 100.5; the first close above it is at slot 5.
    out = scan_setups([{'symbol': 'long', 'ohlcv': seq}, {'symbol': 'bad', 'ohlcv': seq[:1]}],
                      mode='opening_range', start_time=BASE, opening_range_bars=5)
    assert out['results'][0]['trigger']['slot'] == 5
    assert out['results'][1]['status'] == 'invalid_data'
    assert out['counts']['hits'] == 1
    with pytest.raises(ValueError, match='start_time'):
        scan_setups([{'symbol': 'long', 'ohlcv': seq}], mode='opening_range')


def test_alert_rules_once_rearm_changed_and_missing_quotes():
    rules = [
        {'id': 'stop', 'type': 'stop', 'symbol': 'XAUUSD_i', 'side': 'buy', 'price': 99, 'rearm': True},
        {'id': 'short-target', 'type': 'target', 'symbol': 'XAUUSD_i', 'side': 'sell', 'price': 99},
        {'id': 'up', 'type': 'pct_move', 'symbol': 'XAUUSD_i', 'quote_side': 'ask',
         'reference': 100, 'pct': 2, 'direction': 'up'},
        {'id': 'clock', 'type': 'time', 'at_time': BASE + 10},
    ]
    quote = {'XAUUSD_i': {'bid': 98.5, 'ask': 99}}
    first = evaluate_alerts(rules=rules, quotes=quote, now=BASE)
    assert {e['id'] for e in first['events']} == {'stop', 'short-target'}
    assert first['events'][0]['quote_side'] == 'bid'
    missing = evaluate_alerts(rules=rules, quotes={}, now=BASE + 1, state=first['state'])
    assert missing['results'][0]['status'] == 'unavailable'
    assert missing['state']['rules']['stop']['latched']
    repeat = evaluate_alerts(rules=rules, quotes=quote, now=BASE + 2, state=missing['state'])
    assert repeat['events'] == []
    clear = evaluate_alerts(rules=rules, quotes={'XAUUSD_i': {'bid': 101, 'ask': 102}},
                            now=BASE + 10, state=repeat['state'])
    assert {e['id'] for e in clear['events']} == {'clock', 'up'}
    assert not clear['state']['rules']['stop']['latched']
    triggered_again = evaluate_alerts(rules=rules, quotes=quote, now=BASE + 11, state=clear['state'])
    assert [e['id'] for e in triggered_again['events']] == ['stop']
    changed_rules = copy.deepcopy(rules)
    changed_rules[0]['price'] = 99.1
    changed = evaluate_alerts(rules=changed_rules, quotes=quote, now=BASE + 12,
                              state=triggered_again['state'])
    assert changed['results'][0]['rule_changed'] and changed['events'][0]['id'] == 'stop'
    assert not changed['quote_freshness_verified'] and not changed['execution_authorized']


@pytest.mark.parametrize('mutation', [
    lambda r, q: r[0].update(price=True),
    lambda r, q: r[0].update(rearm='false'),
    lambda r, q: r[0].update(type='opening_bell'),
    lambda r, q: r[0].update(type=[]),
    lambda r, q: r[0].update(side='short'),
    lambda r, q: q['XAUUSD_i'].update(bid=101),
    lambda r, q: q['XAUUSD_i'].update(ask=math.nan),
])
def test_alert_rules_reject_bad_inputs(mutation):
    p = payloads()['alert-rules']
    mutation(p['rules'], p['quotes'])
    with pytest.raises((ValueError, TypeError)):
        evaluate_alerts(**p)


def test_alert_state_rejects_backward_clock_and_forged_shape():
    p = payloads()['alert-rules']
    state = evaluate_alerts(**p)['state']
    with pytest.raises(ValueError, match='backwards'):
        evaluate_alerts(**{**p, 'now': BASE - 1, 'state': state})
    state['rules']['s']['latched'] = 'false'
    with pytest.raises(ValueError, match='state'):
        evaluate_alerts(**{**p, 'state': state})
    state['rules']['s']['latched'] = True
    state['version'] = 1.0
    with pytest.raises(ValueError, match='state'):
        evaluate_alerts(**{**p, 'state': state})
    with pytest.raises(ValueError, match='unique'):
        evaluate_alerts(**{**p, 'rules': p['rules'] * 2})


def test_review_extra_limits_never_authorize_and_missing_is_not_zero():
    cfg = Config(training_budget=10000)
    p = payloads()['review']
    out = review_limits(**p, cfg=cfg)
    assert out['status'] == 'advisory_only' and out['estimated_net_reward_risk'] == pytest.approx(2)
    assert not out['execution_authorized'] and not out['plan']['sizing']['account_and_broker_validated']
    partial = review_limits(**{**p, 'limits': {}}, cfg=cfg)
    assert partial['status'] == 'incomplete' and len(partial['missing']) == 6
    unknown = review_limits(**{**p, 'sizing': {**SIZING, 'slippage_price': None}}, cfg=cfg)
    assert unknown['status'] == 'incomplete' and unknown['plan']['sizing']['volume'] is None
    blocked = review_limits(**{**p, 'limits': {**LIMITS, 'daily_risk_cap': 10}}, cfg=cfg)
    assert blocked['status'] == 'blocked' and any('daily cap' in b for b in blocked['blockers'])
    by_count = review_limits(**{**p, 'limits': {**LIMITS, 'trades_today': 3}}, cfg=cfg)
    assert by_count['status'] == 'blocked'
    with pytest.raises(ValueError):
        review_limits(**{**p, 'sizing': None}, cfg=cfg)


def facts_timeline():
    def fact(start, end, filed, value, fp, form='10-Q'):
        return dict(start=start, end=end, filed=filed, val=value, fy=2024, fp=fp, form=form, accn=filed)
    values = [fact('2024-01-01', '2024-03-31', '2024-05-01', 100, 'Q1'),
              fact('2024-04-01', '2024-06-30', '2024-08-01', 110, 'Q2'),
              fact('2024-07-01', '2024-09-30', '2024-11-01', 130, 'Q3'),
              fact('2024-01-01', '2024-12-31', '2025-02-01', 500, 'FY', '10-K'),
              fact('2024-01-01', '2024-03-31', '2025-06-01', 120, 'Q1')]
    return {'cik': 123, 'entityName': 'Synthetic company',
            'facts': {'us-gaap': {'Revenues': {'units': {'USD': values}}}}}


def test_stock_facts_cutoff_restated_values_derived_q4_and_units():
    data = facts_timeline()
    original = copy.deepcopy(data)
    at = stock_facts(companyfacts=data, as_of='2025-03-01')
    assert at['result']['rows'][0]['revenue'] == 160
    assert at['result']['rows'][0]['derived'] and at['result']['ttm']['revenue'] == 500
    assert at['excluded_after_as_of'] == 1 and data == original
    latest = stock_facts(companyfacts=data)
    assert latest['result']['rows'][0]['revenue'] == 140
    assert latest['result']['ttm']['revenue'] == 500
    euros = copy.deepcopy(data)
    euros['facts']['us-gaap']['Revenues']['units']['EUR'] = euros['facts']['us-gaap']['Revenues']['units'].pop('USD')
    with pytest.raises(ValueError, match='no quarterly'):
        stock_facts(companyfacts=euros)
    with pytest.raises(ValueError, match='date'):
        stock_facts(companyfacts=data, as_of='2025-02-30')
    with pytest.raises(ValueError, match='CIK'):
        stock_facts(companyfacts={**data, 'cik': True})
    invalid_period = copy.deepcopy(data)
    invalid_period['facts']['us-gaap']['Revenues']['units']['USD'][0]['filed'] = '2024-03-01'
    with pytest.raises(ValueError, match='period or filing'):
        stock_facts(companyfacts=invalid_period)


def test_stock_facts_extreme_yoy_never_emits_nonfinite_json():
    values = [
        {'start': '2023-01-01', 'end': '2023-03-31', 'filed': '2023-05-01',
         'form': '10-Q', 'val': 5e-324, 'fy': 2023, 'fp': 'Q1', 'accn': 'a'},
        {'start': '2024-01-01', 'end': '2024-03-31', 'filed': '2024-05-01',
         'form': '10-Q', 'val': 1e18, 'fy': 2024, 'fp': 'Q1', 'accn': 'b'},
    ]
    data = {'cik': 123, 'facts': {'us-gaap': {'Revenues': {'units': {'USD': values}}}}}
    with pytest.raises(ValueError, match='too small'):
        stock_facts(companyfacts=data)


def test_original_stock_fixture_numbers_match_the_reference():
    data = json.loads((FIX / 'trader-analyst/companyfacts-CIK0000320193.json').read_text())
    result = stock_facts(companyfacts=data)['result']
    upstream = load_upstream('trader-analyst/facts.py')
    assert result == upstream.analyse(data, quarters=6)
    assert result['rows'][0]['revenue'] == 109417000000
    assert result['ttm']['eps_diluted'] == 8.71


def test_news_filters_future_deduplicates_tracks_seen_and_tags():
    p = payloads()['news']
    xml = p['feeds'][0]['xml'].replace('</channel>',
        '<item><title>Fed inflation report</title><guid>y</guid><link>https://example.test/x</link>'
        '<pubDate>Tue, 02 Jan 2024 11:00:00 GMT</pubDate></item>'
        '<item><title>Future</title><guid>z</guid><pubDate>Tue, 02 Jan 2024 13:00:00 GMT</pubDate></item>'
        '<item><title>Undated</title><guid>u</guid></item></channel>')
    p['feeds'][0]['xml'] = xml
    first = parse_news(**p)
    assert len(first['items']) == 1 and first['items'][0]['tags'] == ['macro']
    assert first['future_excluded'] == 1 and first['undated_excluded'] == 1
    repeat = parse_news(**{**p, 'seen_ids': first['seen_ids']})
    assert repeat['items'] == []
    visible = parse_news(**{**p, 'seen_ids': first['seen_ids'], 'show_all': True,
                            'moves': [{'symbol': 'XAUUSD_i', 'price': 101, 'reference': 100}]})
    assert not visible['items'][0]['new'] and visible['price_moves'][0]['change_pct'] == pytest.approx(1)
    assert not visible['automatic_fetch'] and not visible['execution_authorized']
    with pytest.raises(ValueError, match='DTD'):
        parse_news(**{**p, 'feeds': [{**p['feeds'][0], 'xml': '<!DOCTYPE a [<!ENTITY x "z">]><rss/>'}]})
    with pytest.raises(ValueError, match='SEC Atom'):
        parse_news(**{**p, 'feeds': [{**p['feeds'][0], 'kind': 'sec_atom'}]})
    with pytest.raises(ValueError, match='offset'):
        parse_news(**{**p, 'now': '2024-01-02T12:00:00'})
    with pytest.raises(ValueError, match='news window'):
        parse_news(**{**p, 'now': '0001-01-01T00:00:00Z'})


def test_stock_disclosures_upstream_house_fixtures_and_asof():
    with zipfile.ZipFile(FIX / 'trader-pelosi/2026FD.zip') as archive:
        xml = archive.read('2026FD.xml').decode('utf-8-sig')
    params = dict(kind='index', text=xml, year=2026, as_of='2026-10-13', members=['pelosi'])
    first = stock_disclosures(**params)
    assert [row['doc_id'] for row in first['filings']] == ['20035553', '20035143', '20034836', '20033725']
    recent = stock_disclosures(**{**params, 'as_of': '2026-08-31'})
    assert [row['doc_id'] for row in recent['filings']] == ['20035143', '20034836', '20033725']
    repeat = stock_disclosures(**{**params, 'seen_ids': first['seen_ids'], 'new_only': True})
    assert repeat['filings'] == []
    text = (FIX / 'trader-pelosi/ptr-20035143.txt').read_text()
    report = stock_disclosures(kind='report', text=text)['report']
    assert report['name'] == 'Hon. Nancy Pelosi' and len(report['rows']) == 7
    assert report['rows'][0]['days_to_disclose'] == 28
    with pytest.raises(ValueError, match='DTD'):
        stock_disclosures(**{**params, 'text': '<!DOCTYPE a [<!ENTITY x "z">]><a/>'})
    with pytest.raises(ValueError, match='option descriptions'):
        stock_disclosures(kind='report', text='1 call options ' * 51)
    with pytest.raises(ValueError, match='line'):
        stock_disclosures(kind='report', text='x' * 1001)


def test_all_dispatchers_reject_unknown_fields_and_do_not_touch_broker(monkeypatch):
    import trade_mcp.app as app_mod

    monkeypatch.setattr(app_mod, '_APP', None)
    monkeypatch.setattr(app_mod, 'build_app', lambda *a, **k: pytest.fail('must not initialize MT5'))
    monkeypatch.setattr('socket.socket.connect', lambda *a, **k: pytest.fail('network forbidden'))
    monkeypatch.setattr('socket.create_connection', lambda *a, **k: pytest.fail('network forbidden'))
    monkeypatch.setattr('trade_mcp.execution.propose_open', lambda *a, **k: pytest.fail('proposal forbidden'))
    monkeypatch.setattr('trade_mcp.execution.confirm', lambda *a, **k: pytest.fail('execution forbidden'))
    for op, body in payloads().items():
        result = run_request(op, body, cfg=Config(training_budget=10000))
        assert result['ok'] and result['execution_authorized'] is False
        with pytest.raises(ValueError, match='fields'):
            run_request(op, {**body, 'unexpected': True}, cfg=Config())
    assert app_mod._APP is None


def test_mcp_new_tools_are_registered_wire_and_review_is_offline(monkeypatch):
    import trade_mcp.app as app_mod
    from trade_mcp import server

    monkeypatch.setattr(app_mod, '_APP', None)
    monkeypatch.setattr(app_mod, 'build_app', lambda *a, **k: pytest.fail('must not initialize MT5'))
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {'daytrade_scan', 'daytrade_alert_rules', 'daytrade_review_limits', 'daytrade_parse_news',
            'daytrade_stock_facts', 'daytrade_stock_disclosures'} <= names
    for name, op in [('daytrade_scan', 'scan'), ('daytrade_alert_rules', 'alert-rules'),
                     ('daytrade_review_limits', 'review'), ('daytrade_parse_news', 'news'),
                     ('daytrade_stock_facts', 'stock-facts'), ('daytrade_stock_disclosures', 'stock-disclosures')]:
        wire = asyncio.run(server.mcp.call_tool(name, payloads()[op]))
        response = wire[1] if isinstance(wire, tuple) else json.loads(wire[0].text)
        assert response['ok'] and response['execution_authorized'] is False
    assert app_mod._APP is None
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        asyncio.run(server.mcp.call_tool('daytrade_alert_rules', {**payloads()['alert-rules'], 'now': True}))


def test_http_new_operations_enforce_origin_size_and_no_broker(mock_app, monkeypatch):
    from test_terminal import console

    monkeypatch.setattr(mock_app.mt5, 'request', lambda *a, **k: pytest.fail('broker call forbidden'))
    monkeypatch.setattr('trade_mcp.agents.LLMClient.chat', lambda *a, **k: pytest.fail('model call forbidden'))
    before = mock_app.store.list_proposals()
    with console(mock_app) as base:
        for op, body in payloads().items():
            req = Request(base + '/api/daytrade/' + op, data=json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json'})
            with urlopen(req) as response:
                result = json.load(response)
            assert result['ok'] and result['execution_authorized'] is False
        forbidden = Request(base + '/api/daytrade/news', data=b'{}',
                            headers={'Origin': 'https://foreign.invalid'})
        with pytest.raises(HTTPError) as denied:
            urlopen(forbidden)
        assert denied.value.code == 403
        oversized = Request(base + '/api/daytrade/stock-facts', data=b'{}',
                            headers={'Content-Length': '4000001'})
        with pytest.raises(HTTPError) as denied:
            urlopen(oversized, timeout=3)
        assert denied.value.code == 413
    assert mock_app.store.list_proposals() == before and mock_app.store.list_model_usage() == []
