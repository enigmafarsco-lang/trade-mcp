"""Independent educational baselines, not copied upstream trading strategies."""
STRATEGIES = {
    'upstream_sma_cross': 'Backtesting.py 0.6.5 official SmaCross (long/short, default sizing)',
    'sma_cross': 'SMA 10/30 trend crossover',
    'rsi_reversion': 'RSI 14 mean reversion, enter below 30, exit above 55',
    'donchian_breakout': 'Donchian 20-bar breakout, 10-bar exit',
}


def strategy_signals(bars, strategy='sma_cross'):
    """Return close-time actions; execution belongs to the NEXT bar only."""
    if strategy not in STRATEGIES or strategy == 'upstream_sma_cross':
        raise ValueError('Unknown research strategy')
    closes = [float(b['close']) for b in bars]
    actions = [0] * len(bars)
    for i in range(30, len(bars)):
        if strategy == 'sma_cross':
            previous = sum(closes[i-10:i])/10 - sum(closes[i-30:i])/30
            current = sum(closes[i-9:i+1])/10 - sum(closes[i-29:i+1])/30
            actions[i] = 1 if previous <= 0 < current else -1 if previous >= 0 > current else 0
        elif strategy == 'rsi_reversion':
            # Simple rolling RSI, deliberately not Wilder-smoothed RSI.
            changes = [closes[j]-closes[j-1] for j in range(i-13, i+1)]
            gains = sum(max(d, 0) for d in changes)
            losses = sum(max(-d, 0) for d in changes)
            rsi = 100*gains/(gains+losses) if gains+losses else 50
            actions[i] = 1 if rsi < 30 else -1 if rsi > 55 else 0
        else:
            upper = max(float(b['high']) for b in bars[i-20:i])
            lower = min(float(b['low']) for b in bars[i-10:i])
            actions[i] = 1 if closes[i] > upper else -1 if closes[i] < lower else 0
    return actions
