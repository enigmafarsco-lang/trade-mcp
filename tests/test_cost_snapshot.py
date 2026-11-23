import pytest

from trade_mcp.cost_snapshot import CostSnapshotError, read_cost_snapshot

GOOD = 'meta\t1\t123\tDemo\tUSD\t1000\t100\t999\t1\ndeal\t8\t9\t500\t0\t0\t2\t0\t-0.1\t0\n'


def read(tmp_path, text=GOOD, **kwargs):
    path = tmp_path / 'costs.tsv'
    path.write_text(text)
    return read_cost_snapshot(path, login='123', server='Demo', currency='USD',
                              now=kwargs.get('now', 1001))


def test_explicit_costs_not_execution_permission(tmp_path):
    result = read(tmp_path)
    assert result['deals'][0]['commission'] == -0.1
    assert result['deals'][0]['fee'] == 0
    assert result['deals'][0]['costs_complete'] is True
    assert result['execution_eligible'] is False


@pytest.mark.parametrize('text', [
    GOOD.replace('\tUSD\t', '\tEUR\t'),
    GOOD.replace('\t123\t', '\t456\t'),
    GOOD.replace('\tDemo\t', '\tOther\t'),
    GOOD.replace('\t-0.1\t', '\tnan\t'),
    GOOD.replace('\t-0.1\t', '\tinf\t'),
    GOOD.replace('\t-0.1\t', '\t\t'),
    GOOD.replace('\t999\t1', '\t999\t2'),
    GOOD + GOOD.splitlines(keepends=True)[1],
    GOOD.splitlines(keepends=True)[0],
    GOOD.replace('\t500\t', '\t99\t'),
])
def test_fail_closed(tmp_path, text):
    with pytest.raises(CostSnapshotError):
        read(tmp_path, text)


@pytest.mark.parametrize('now', [999, 1031, float('nan')])
def test_stale_or_future(tmp_path, now):
    with pytest.raises(CostSnapshotError):
        read(tmp_path, now=now)


def test_monitor_has_no_execution_or_network_calls():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / 'mt5-connector'
    for name in ('TradeMCPCostExport.mqh', 'TradeMCPCostMonitor.mq5',
                 'TradeMCPCostSnapshot.mq5'):
        source = (root / name).read_text()
        for forbidden in ('OrderSend(', 'OrderSendAsync(', 'WebRequest(',
                          'SocketConnect(', '#include <Trade/'):
            assert forbidden not in source
    monitor = (root / 'TradeMCPCostMonitor.mq5').read_text()
    assert 'EventSetTimer(IntervalSeconds)' in monitor
    assert 'EventKillTimer()' in monitor


def test_shared_exporter_checks_account_and_publication():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / 'mt5-connector' /
              'TradeMCPCostExport.mqh').read_text()
    assert 'AccountInfoInteger(ACCOUNT_LOGIN) != login' in source
    assert 'AccountInfoString(ACCOUNT_SERVER) != server' in source
    assert 'AccountInfoString(ACCOUNT_CURRENCY) != currency' in source
    assert 'HistoryDealsTotal() != count' in source
    assert 'return FileMove(' in source


V2 = ('meta\t2\t123\tDemo\tUSD\t1000\t100\t999\t1\t1\n'
      'deal\t8\t9\t500\t0\t0\t0\t0\t-0.1\t0\t0.1\t746d63703a616263\n'
      'position\t10\t9\t0\t500\t0.1\t1.5\t-0.2\t90\t110\t746d63703a616263\n')


def test_v2_volume_and_floating_fields(tmp_path):
    result = read(tmp_path, V2)
    assert result['source'] == 'mql5_cost_snapshot_v2'
    assert result['deals'][0]['volume'] == 0.1
    assert result['deals'][0]['comment'] == 'tmcp:abc'
    p = result['positions'][0]
    assert p['identifier'] == '9' and p['ticket'] == '10'
    assert p['profit'] == 1.5 and p['swap'] == -0.2
    assert p['pnl_complete'] is True
    assert result['execution_eligible'] is False


@pytest.mark.parametrize('text', [
    V2.replace('\t1\t1\n', '\t1\t2\n'),
    V2.replace('746d63703a616263', 'not-hex'),
    V2.replace('746d63703a616263', 'ff'),
    V2.replace('\t0.1\t1.5\t', '\t0\t1.5\t'),
    V2.replace('\t0.1\t1.5\t', '\t0.1\tnan\t'),
    V2.replace('\t0.1\t746d', '\t-1\t746d'),
    V2.replace('\t90\t110\t', '\t-90\t110\t'),
    V2.split('position')[0],
    V2 + V2.splitlines(keepends=True)[-1],
])
def test_v2_rejects_malformed_or_incomplete_data(tmp_path, text):
    with pytest.raises(CostSnapshotError):
        read(tmp_path, text)


def test_v1_does_not_invent_floating_positions(tmp_path):
    result = read(tmp_path)
    assert result['positions'] == []
    assert 'volume' not in result['deals'][0]
