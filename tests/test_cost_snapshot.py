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
