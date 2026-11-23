"""Independent transparent rule ensemble, not copied GPL strategy code."""
from .technical import analyze_ohlcv


def strategy_signal(rows, provider='builtin'):
    analysis = analyze_ohlcv(rows, provider)
    if not analysis['ready']:
        return dict(ok=True, signal='HOLD', confidence_percent=None,
                    reason='insufficient_warmup', execution_authorized=False)
    i = analysis['indicators']
    votes = {
        'ema_trend': 1 if i['ema20'] > i['ema50'] > i['ema200'] else
                     -1 if i['ema20'] < i['ema50'] < i['ema200'] else 0,
        'macd': 1 if i['macd_histogram'] > 0 else -1 if i['macd_histogram'] < 0 else 0,
        'supertrend': int(i['supertrend_direction']),
        'rsi': 1 if 50 < i['rsi14'] < 70 else -1 if 30 < i['rsi14'] < 50 else 0,
    }
    total = sum(votes.values())
    signal = 'BUY' if total >= 3 else 'SELL' if total <= -3 else 'HOLD'
    return dict(ok=True, signal=signal, confidence_percent=abs(total)/len(votes)*100,
                confidence_kind='uncalibrated_rule_agreement_not_probability', votes=votes,
                provenance='Independent rule ensemble; not Freqtrade or AI Hedge Fund output',
                execution_authorized=False, technical=analysis)
