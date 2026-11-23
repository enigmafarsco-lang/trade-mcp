import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from trade_mcp.paper import PaperLedger


def open_position(ledger):
    return ledger.open(symbol='TEST', side='buy', volume=.01, entry=100, stop=99, target=102, multiplier=100)


def test_paper_profit_persists_and_is_labeled(tmp_path):
    path = tmp_path / 'trades_log.json'
    ledger = PaperLedger(path, 100)
    open_position(ledger)
    result = ledger.mark(symbol='TEST', bid=102, ask=102.1)
    assert result['paper'] and result['closed'] and result['pnl'] == 2
    assert PaperLedger(path).status()['balance'] == 102
    data = json.loads(path.read_text())
    assert data['events'][-1]['fee_assumption'] == 'zero_simulated_not_broker_costs'
    assert path.stat().st_mode & 0o777 == 0o600


def test_three_paper_losses_latch_across_reopen(tmp_path):
    path = tmp_path / 'trades_log.json'
    for _ in range(3):
        ledger = PaperLedger(path, 100)
        open_position(ledger)
        assert ledger.mark(symbol='TEST', bid=99, ask=99.1)['pnl'] == -1
    assert PaperLedger(path).status()['latched']
    with pytest.raises(ValueError):
        open_position(PaperLedger(path))


@pytest.mark.parametrize('volume', [3, float('nan'), -1])
def test_paper_risk_cannot_be_bypassed(tmp_path, volume):
    with pytest.raises(ValueError):
        PaperLedger(tmp_path/'paper.json', 100).open(symbol='TEST', side='buy', volume=volume,
                                                  entry=100, stop=99, target=102)


def test_concurrent_open_has_one_winner(tmp_path):
    path = tmp_path/'paper.json'
    def attempt(_):
        try:
            open_position(PaperLedger(path))
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, range(2))) == 1
    assert len(json.loads(path.read_text())['events']) == 1


def test_price_gap_is_not_filled_at_an_invented_stop(tmp_path):
    ledger = PaperLedger(tmp_path/'paper.json')
    open_position(ledger)
    assert ledger.mark(symbol='TEST', bid=95, ask=95.1)['pnl'] == -5


def test_explicit_paper_flag_blocks_broker_confirmation_without_reading_proposal(mock_app):
    from unittest.mock import Mock

    from trade_mcp.execution import confirm
    broker, store = Mock(), Mock()
    cfg = mock_app.cfg.with_overrides(allow_trading=True, paper_trading=True)
    assert not cfg.trading_enabled
    result = confirm(mt5=broker, cfg=cfg, store=store, proposal_id='not-read')
    assert result['error'] == 'paper_mode_no_broker_execution'
    assert not broker.mock_calls and not store.mock_calls
