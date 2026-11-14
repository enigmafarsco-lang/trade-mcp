"""Protective stop for an explicit manual volume; never silently omit protection."""
import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .config import sizing_balance
from .risk import min_stop_distance


def automatic_stop(*, spec, quote, account, cfg, side, volume=None, risk=None):
    if side not in {'buy', 'sell'}:
        raise ValueError('جهت سفارش باید Buy یا Sell باشد.')
    volume = float(volume if volume is not None else spec.get('volume_min', .01))
    risk = float(risk if risk is not None else cfg.max_risk_percent)
    if not all(math.isfinite(v) and v > 0 for v in (volume, risk)):
        raise ValueError('حجم و درصد ریسک باید مثبت باشند.')
    if risk > cfg.max_risk_percent:
        raise ValueError('درصد ریسک از سقف تنظیمات بیشتر است.')
    tick = float(spec.get('trade_tick_size') or 0)
    value = float(spec.get('trade_tick_value') or 0)
    if not all(math.isfinite(v) and v > 0 for v in (tick, value)):
        raise ValueError('ارزش و اندازهٔ تیک معتبر برای محاسبهٔ SL دریافت نشد.')
    entry = float(quote.get('ask' if side == 'buy' else 'bid') or 0)
    allowance = sizing_balance(account, cfg) * risk / 100
    if not math.isfinite(entry) or entry <= 0 or allowance <= 0:
        raise ValueError('قیمت یا بودجه معتبر نیست.')
    distance = allowance / (volume * value) * tick
    price = entry-distance if side == 'buy' else entry+distance
    rounded = (Decimal(str(price))/Decimal(str(tick))).to_integral_value(
        rounding=ROUND_CEILING if side == 'buy' else ROUND_FLOOR)*Decimal(str(tick))
    stop = float(rounded)
    if stop <= 0 or abs(entry-stop)+1e-9 < min_stop_distance(spec):
        raise ValueError('بودجهٔ ریسک با حداقل فاصلهٔ SL و این حجم سازگار نیست؛ حجم را کم کنید یا معامله نکنید.')
    return stop, volume
