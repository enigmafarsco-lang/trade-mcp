"""Bundled, dependency-free indicator catalog; never executes uploaded code."""
from . import indicators as ta

CATALOG = [
    {"id": "ema", "name": "EMA 20/50/200", "help": "میانگین نمایی؛ جهت روند، نه پیش‌بینی قیمت"},
    {"id": "rsi", "name": "RSI 14", "help": "قدرت حرکت؛ اشباع خرید به‌تنهایی سیگنال فروش نیست"},
    {"id": "atr", "name": "ATR 14", "help": "نوسان برای برآورد فاصلهٔ ضرر؛ جهت بازار را نمی‌گوید"},
    {"id": "adx", "name": "ADX 14", "help": "قدرت روند همراه با DI مثبت و منفی"},
    {"id": "bollinger", "name": "Bollinger 20/2", "help": "باند نوسان؛ لمس باند به‌تنهایی ورود نیست"},
    {"id": "macd", "name": "MACD 12/26/9", "help": "اختلاف میانگین‌ها و خط سیگنال"},
    {"id": "stochastic", "name": "Stochastic 14/3", "help": "جایگاه قیمت در دامنهٔ اخیر"},
]


def calculate(bars, selected):
    unknown = set(selected) - {p['id'] for p in CATALOG}
    if unknown:
        raise ValueError('unknown indicator')
    closes = [float(b['close']) for b in bars]
    highs = [float(b['high']) for b in bars]
    lows = [float(b['low']) for b in bars]
    out = {}
    for key in selected:
        if key == 'ema':
            out[key] = {str(n): ta.ema(closes, n) for n in (20, 50, 200)}
        elif key == 'rsi':
            out[key] = {'14': ta.rsi(closes, 14)}
        elif key == 'atr':
            out[key] = {'14': ta.atr(highs, lows, closes, 14)}
        elif key == 'adx':
            a, p, m = ta.adx(highs, lows, closes, 14)
            out[key] = {'adx': a, '+di': p, '-di': m}
        elif key == 'bollinger':
            u, m, lower = ta.bollinger(closes, 20, 2)
            out[key] = {'upper': u, 'middle': m, 'lower': lower}
        elif key == 'macd':
            fast, slow = ta.ema(closes, 12), ta.ema(closes, 26)
            line = [a-b if a is not None and b is not None else None for a, b in zip(fast, slow, strict=True)]
            valid = [v for v in line if v is not None]
            sig = [None] * (len(line)-len(valid)) + ta.ema(valid, 9)
            out[key] = {'macd': line, 'signal': sig,
                        'histogram': [a-b if a is not None and b is not None else None for a, b in zip(line, sig, strict=True)]}
        elif key == 'stochastic':
            k = [None] * len(bars)
            for i in range(13, len(bars)):
                lo, hi = min(lows[i-13:i+1]), max(highs[i-13:i+1])
                k[i] = 100*(closes[i]-lo)/(hi-lo) if hi != lo else 50.0
            out[key] = {'k': k, 'd': [None]*min(13, len(bars)) + ta.sma(k[13:], 3)}
    return out
