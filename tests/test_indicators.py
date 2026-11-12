import math

from trade_mcp import indicators as ta


def test_sma_and_ema_basic():
    values = [1, 2, 3, 4, 5]
    assert ta.sma(values, 3) == [None, None, 2.0, 3.0, 4.0]
    ema = ta.ema(values, 3)
    assert ema[0] is None and ema[1] is None
    assert ema[2] == 2.0
    assert ema[3] > 2.0 and ema[4] > ema[3]


def test_rsi_bounds_and_direction():
    up = list(range(1, 60))
    down = list(range(60, 1, -1))
    assert ta.rsi(up)[-1] > 95
    assert ta.rsi(down)[-1] < 5
    mixed = [10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10]
    assert 0 <= ta.rsi(mixed)[-1] <= 100


def test_atr_and_true_range():
    highs = [10, 12, 11, 13, 15, 14, 16, 18, 17, 19, 20, 21, 22, 23, 24, 25]
    lows = [8, 9, 9, 10, 12, 11, 13, 15, 14, 16, 17, 18, 19, 20, 21, 22]
    closes = [9, 11, 10, 12, 14, 13, 15, 17, 16, 18, 19, 20, 21, 22, 23, 24]
    atr = ta.atr(highs, lows, closes, 5)
    assert atr[3] is None
    assert atr[4] is not None and atr[4] > 0
    assert atr[-1] is not None
    trs = ta.true_ranges(highs, lows, closes)
    assert trs[0] == 2
    assert trs[1] >= abs(highs[1] - closes[0])


def test_adx_trending_vs_flat():
    trending_h = [100 + i for i in range(60)]
    trending_l = [99 + i for i in range(60)]
    trending_c = [99.5 + i for i in range(60)]
    adx_t, pdi_t, mdi_t = ta.adx(trending_h, trending_l, trending_c, 14)
    assert adx_t[-1] is not None and adx_t[-1] > 40
    assert pdi_t[-1] > mdi_t[-1]

    flat_h = [100 + (0.1 if i % 2 else 0) for i in range(60)]
    flat_l = [99 - (0.1 if i % 2 else 0) for i in range(60)]
    flat_c = [99.5] * 60
    adx_f, _, _ = ta.adx(flat_h, flat_l, flat_c, 14)
    assert adx_f[-1] is not None and adx_f[-1] < 15


def test_swings_and_donchian():
    highs = [1, 2, 5, 2, 1, 2, 6, 2, 1]
    lows = [0, 1, 3, 1, 0, 1, 3, 1, 0]
    sh, sl = ta.swings(highs, lows, 1, 1)
    assert 2 in sh and 6 in sh
    assert 4 in sl
    upper, lower = ta.donchian(highs, lows, 3)
    assert upper[2] == 5
    assert lower[2] == 0


def test_slope_sign():
    rising = [float(i) for i in range(1, 40)]
    falling = list(reversed(rising))
    assert ta.slope(rising, 20)[-1] > 0
    assert ta.slope(falling, 20)[-1] < 0
    assert not math.isnan(ta.slope(rising, 20)[-1])


def test_percentile_rank():
    history = [1, 2, 3, 4, 5]
    assert ta.percentile_rank(history, 0.5) == 0.0
    assert ta.percentile_rank(history, 5) == 1.0
    assert 0.5 < ta.percentile_rank(history, 3.5) < 0.9
