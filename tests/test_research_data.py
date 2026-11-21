import pytest

from trade_mcp.research_data import closed_m1_segment


def bars(n, start=0):
    return [{'time_utc': start+i*60, 'open': 100, 'high': 102, 'low': 99,
             'close': 101, 'tick_volume': 2} for i in range(n)]


def test_closed_only_and_limit():
    result = closed_m1_segment(bars(1001), now=60000, maximum=500)
    assert len(result) == 500
    assert result[-1][0]+60 <= 60000
    assert len(result[0]) == 6


def test_latest_eligible_segment_without_gap_filling():
    data = bars(450)+bars(430, start=60000)+bars(5, start=100000)
    # Invalid minute boundary is rejected even in a short tail.
    with pytest.raises(ValueError):
        closed_m1_segment(data, now=200000)
    data = bars(450)+bars(430, start=60000)+bars(5, start=120000)
    result = closed_m1_segment(data, now=200000)
    assert len(result) == 430
    assert result[0][0] == 60000


def test_unknown_clock_and_insufficient_history_rejected():
    data = bars(500)
    data[0]['time_utc'] = None
    with pytest.raises(ValueError, match='Verified UTC'):
        closed_m1_segment(data, now=60000)
    with pytest.raises(ValueError, match='sufficiently long'):
        closed_m1_segment(bars(180), now=60000)


def test_safe_diagnostics_include_counts_not_market_values():
    from trade_mcp.research_data import HistoryExportError

    with pytest.raises(HistoryExportError) as exc:
        closed_m1_segment(bars(180), now=60000)
    assert exc.value.code == 'INSUFFICIENT_CONTIGUOUS_HISTORY'
    assert 'longest_segment=180' in str(exc.value)
    assert 'required=420' in str(exc.value)
    for data, code in [([], 'EMPTY_HISTORY'), ([{'time_utc': 'secret'}], 'UTC_UNVERIFIED'),
                       ([{'time_utc': 0}], 'INVALID_ROW')]:
        with pytest.raises(HistoryExportError) as exc:
            closed_m1_segment(data, now=60000)
        assert exc.value.code == code
        assert 'secret' not in str(exc.value)


def test_export_cli_hides_transport_secrets(tmp_path, monkeypatch, capsys):
    import importlib.util
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    path = Path(__file__).resolve().parents[1]/'scripts'/'export_kronos_history.py'
    spec = importlib.util.spec_from_file_location('history_export', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeMT5:
        def __init__(self, *args):
            pass

        def candles(self, *args):
            print('SECRET_TRANSPORT_PAYLOAD')
            raise RuntimeError('SECRET_TOKEN')

        def close(self):
            raise RuntimeError('SECRET_CLOSE')

    monkeypatch.setattr(module, 'MT5', FakeMT5)
    monkeypatch.setattr(module, 'NativeBridge', lambda *args, **kwargs: None)
    monkeypatch.setattr(module, 'load_dotenv', lambda: None)
    monkeypatch.setattr(module.Config, 'from_env', lambda: SimpleNamespace(
        backend='native', mcp_url='private', mcp_token='private', timeout=1))
    output = tmp_path/'output.json'
    monkeypatch.setattr(sys, 'argv', ['export', str(output)])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert 'HISTORY_REQUEST_FAILED' in str(exc.value)
    assert 'SECRET' not in str(exc.value)+str(capsys.readouterr())
    assert not output.exists()
