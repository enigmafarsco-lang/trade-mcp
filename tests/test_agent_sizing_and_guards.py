"""Agent sizing must honour the agent lot cap BEFORE a proposal is created,
and guarded mode must refuse volatility-spiking / disagreement warnings before
proposing. Offline only: mock backend, no provider, no broker order.
"""
from __future__ import annotations

import pytest

from trade_mcp import execution
from trade_mcp.agents import AgentEngine, LLMClient


def _engine(app, **overrides):
    cfg = app.cfg.with_overrides(**overrides) if overrides else app.cfg
    return AgentEngine(cfg=cfg, mt5=app.mt5, store=app.store, llm=LLMClient())


def _forced(engine, monkeypatch, side='sell', confidence=75, risk=0.5, stop_distance=19.2):
    def decide(agent, analysis):
        entry = float(analysis['quote']['bid'])
        sign = 1 if side == 'sell' else -1
        return {'action': side, 'source': 'rules', 'confidence': confidence,
                'risk_percent': risk,
                'stop_loss': entry + sign * stop_distance,
                'take_profit': entry - sign * stop_distance}
    monkeypatch.setattr(engine, '_decide', decide)


# --------------------------------------------------------------- execution
def test_sizing_applies_stricter_call_cap_instead_of_config_cap(mock_app):
    cfg = mock_app.cfg.with_overrides(max_lot=0.10, allow_trading=True)
    price = mock_app.mt5.tick('XAUUSD')['ask']
    out = execution.propose_open(
        mt5=mock_app.mt5, cfg=cfg, store=mock_app.store, symbol='XAUUSD', side='buy',
        stop_loss=price - 3.0, risk_percent=0.5, max_lot=0.02,
    )
    assert out['ok'], out
    preview = out['preview']
    assert preview['volume'] <= 0.02 + 1e-9
    assert preview['volume'] >= 0.01


def test_call_cap_never_widens_the_config_cap(mock_app):
    cfg = mock_app.cfg.with_overrides(max_lot=0.01, allow_trading=True)
    price = mock_app.mt5.tick('XAUUSD')['ask']
    out = execution.propose_open(
        mt5=mock_app.mt5, cfg=cfg, store=mock_app.store, symbol='XAUUSD', side='buy',
        stop_loss=price - 3.0, risk_percent=0.5, max_lot=5.0,
    )
    assert out['ok'], out
    assert out['preview']['volume'] <= 0.01 + 1e-9


def test_invalid_call_cap_is_ignored_not_trusted(mock_app):
    cfg = mock_app.cfg.with_overrides(max_lot=0.02, allow_trading=True)
    price = mock_app.mt5.tick('XAUUSD')['ask']
    out = execution.propose_open(
        mt5=mock_app.mt5, cfg=cfg, store=mock_app.store, symbol='XAUUSD', side='buy',
        stop_loss=price - 3.0, risk_percent=0.5, max_lot=0,
    )
    assert out['ok'], out
    assert out['preview']['volume'] <= 0.02 + 1e-9


def test_no_volume_fits_returns_no_trade_without_proposal(mock_app):
    """When the minimum lot would exceed the risk budget, nothing is created."""
    cfg = mock_app.cfg.with_overrides(max_lot=0.10, allow_trading=True)
    before = mock_app.store.list_proposals()
    price = mock_app.mt5.tick('XAUUSD')['ask']
    out = execution.propose_open(
        mt5=mock_app.mt5, cfg=cfg, store=mock_app.store, symbol='XAUUSD', side='buy',
        stop_loss=price - 15.0, risk_percent=0.01, max_lot=0.02,
    )
    assert out['ok'] is False
    assert out['error'] == 'sizing_failed'
    assert out['sizing']['minimum_volume_loss'] > out['sizing']['risk_amount']
    assert mock_app.store.list_proposals() == before


def test_explicit_volume_above_call_cap_is_refused(mock_app):
    cfg = mock_app.cfg.with_overrides(max_lot=0.10, allow_trading=True)
    price = mock_app.mt5.tick('XAUUSD')['ask']
    out = execution.propose_open(
        mt5=mock_app.mt5, cfg=cfg, store=mock_app.store, symbol='XAUUSD', side='buy',
        volume=0.20, stop_loss=price - 3.0, max_lot=0.02,
    )
    assert out['ok'] is False
    assert out['error'] == 'volume_above_call_lot_cap'


# --------------------------------------------------------------- agent flow
def test_engine_sizes_inside_agent_cap_and_never_creates_oversized_proposal(mock_app, monkeypatch):
    mock_app.cfg = mock_app.cfg.with_overrides(max_lot=0.10, allow_trading=True)
    mock_app.mt5.bridge.allow_trading = True
    agent = mock_app.store.create_agent(
        {'name': 'cap', 'symbols': ['XAUUSD'], 'max_lot': 0.02, 'max_risk': 0.5,
         'mode': 'advisory'}
    )
    engine = _engine(mock_app)
    _forced(engine, monkeypatch)
    report = engine.run(agent)
    assert not report['errors'], report
    entry = report['decisions'][0]
    assert 'cancelled' not in str(entry.get('note') or ''), entry
    proposals = [p for p in mock_app.store.list_proposals(limit=20)
                 if p['status'] != 'cancelled']
    assert proposals, 'sizing inside the cap must still create a proposal'
    for prop in proposals:
        payload = prop.get('payload') or {}
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        assert float(payload.get('volume') or 0.0) <= 0.02 + 1e-9


def test_engine_returns_no_trade_when_min_lot_exceeds_risk_budget(mock_app, monkeypatch):
    mock_app.cfg = mock_app.cfg.with_overrides(max_lot=0.10, allow_trading=True)
    agent = mock_app.store.create_agent(
        {'name': 'tiny', 'symbols': ['XAUUSD'], 'max_lot': 0.01, 'max_risk': 0.01,
         'mode': 'advisory'}
    )
    engine = _engine(mock_app)
    _forced(engine, monkeypatch, risk=0.01, stop_distance=15.0)
    before = mock_app.store.list_proposals()
    report = engine.run(agent)
    assert not report['errors'], report
    entry = report['decisions'][0]
    assert entry['action'] == 'wait'
    assert entry['no_trade'] is True
    assert entry['decision_action'] == 'sell'
    assert 'NO TRADE' in entry['note']
    assert mock_app.store.list_proposals() == before


# ------------------------------------------------- guarded warning gating
@pytest.mark.parametrize('warning', [
    'volatility is spiking - widen stops or stay out',
    'timeframes disagree - reduce size or wait for alignment',
    'VOLATILITY IS SPIKING - WIDEN STOPS OR STAY OUT',
])
def test_guarded_refuses_real_warning_prose_before_proposal(mock_app, monkeypatch, warning):
    mock_app.cfg = mock_app.cfg.with_overrides(allow_trading=True)
    agent = mock_app.store.create_agent(
        {'name': 'g', 'symbols': ['XAUUSD'], 'max_lot': 0.02, 'max_risk': 0.5,
         'mode': 'guarded'}
    )
    engine = _engine(mock_app)
    _forced(engine, monkeypatch, confidence=80)
    real_analyze = __import__('trade_mcp.agents', fromlist=['analyze_symbol']).analyze_symbol

    def analyze(*args, **kwargs):
        report = real_analyze(*args, **kwargs)
        report['warnings'] = list(report.get('warnings') or []) + [warning]
        report['guarded_risk_warnings'] = [warning]
        return report

    monkeypatch.setattr('trade_mcp.agents.analyze_symbol', analyze)
    before = mock_app.store.list_proposals()
    report = engine.run(agent)
    entry = report['decisions'][0]
    assert entry['status'] == 'refused'
    assert entry['proposal_created'] is False
    assert entry['action'] == 'wait'
    assert 'spiking' in entry['note'].lower() or 'disagree' in entry['note'].lower()
    assert mock_app.store.list_proposals() == before


def test_guarded_prose_only_analysis_is_still_blocked(mock_app, monkeypatch):
    """A caller that supplies only prose (no structured list) must not slip through."""
    mock_app.cfg = mock_app.cfg.with_overrides(allow_trading=True)
    agent = mock_app.store.create_agent(
        {'name': 'g2', 'symbols': ['XAUUSD'], 'max_lot': 0.02, 'max_risk': 0.5,
         'mode': 'guarded'}
    )
    engine = _engine(mock_app)
    _forced(engine, monkeypatch, confidence=80)
    real_analyze = __import__('trade_mcp.agents', fromlist=['analyze_symbol']).analyze_symbol

    def analyze(*args, **kwargs):
        report = real_analyze(*args, **kwargs)
        report.pop('guarded_risk_warnings', None)
        report['warnings'] = ['volatility is spiking - widen stops or stay out']
        return report

    monkeypatch.setattr('trade_mcp.agents.analyze_symbol', analyze)
    report = engine.run(agent)
    entry = report['decisions'][0]
    assert entry['status'] == 'refused' and entry['proposal_created'] is False


def test_guarded_clean_warnings_do_not_block_confident_setup(mock_app, monkeypatch):
    mock_app.cfg = mock_app.cfg.with_overrides(allow_trading=True)
    agent = mock_app.store.create_agent(
        {'name': 'clean', 'symbols': ['XAUUSD'], 'max_lot': 0.02, 'max_risk': 0.5,
         'mode': 'guarded'}
    )
    engine = _engine(mock_app)
    _forced(engine, monkeypatch, confidence=80)
    real_analyze = __import__('trade_mcp.agents', fromlist=['analyze_symbol']).analyze_symbol

    def analyze(*args, **kwargs):
        report = real_analyze(*args, **kwargs)
        report['warnings'] = [w for w in (report.get('warnings') or [])
                              if 'spiking' not in w and 'disagree' not in w]
        report['guarded_risk_warnings'] = []
        report['scenario'] = {**(report.get('scenario') or {}), 'confidence': 80}
        return report

    monkeypatch.setattr('trade_mcp.agents.analyze_symbol', analyze)
    report = engine.run(agent)
    entry = report['decisions'][0]
    assert entry.get('status') != 'refused', entry
    assert entry.get('proposal_created') is not False


def test_guarded_confidence_threshold_unchanged():
    from trade_mcp.agents import GUARDED_MIN_CONFIDENCE
    assert GUARDED_MIN_CONFIDENCE == 62
