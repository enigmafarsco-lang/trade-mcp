"""Read-only demo education. No order execution, no promised returns."""
from __future__ import annotations

import math
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from .indicators import atr, ema, last_defined, rsi


def positive(value: Any, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def risk_plan(*, spec: dict, budget: float, risk_percent: float, distance: float,
              target: float, commission: float = 0, slippage: float = 0) -> dict:
    """Account-currency estimate using broker-reported tick value, not leverage."""
    budget = positive(budget, "budget")
    risk_percent = positive(risk_percent, "risk_percent")
    if risk_percent > 100:
        raise ValueError("risk_percent must not exceed 100")
    distance, target = positive(distance, "distance"), positive(target, "target")
    tick = positive(spec.get("trade_tick_size"), "tick_size")
    value = positive(spec.get("trade_tick_value"), "tick_value")
    step = positive(spec.get("volume_step"), "volume_step")
    minimum = positive(spec.get("volume_min"), "volume_min")
    maximum = positive(spec.get("volume_max"), "volume_max")
    commission, slippage = float(commission), float(slippage)
    if not all(math.isfinite(x) and x >= 0 for x in (commission, slippage)):
        raise ValueError("costs must be finite and non-negative")
    allowance = budget * risk_percent / 100
    loss_per_lot = (distance + slippage) / tick * value + commission
    raw_volume = min(allowance / loss_per_lot, maximum)
    volume = float((Decimal(str(raw_volume)) / Decimal(str(step))).to_integral_value(
        rounding=ROUND_FLOOR) * Decimal(str(step)))
    blockers = []
    if volume < minimum:
        blockers.append("حداقل حجم بروکر از بودجهٔ ریسک بیشتر است؛ معامله نکنید.")
        volume = 0.0
    point = positive(spec.get("point"), "point")
    if distance < float(spec.get("trade_stops_level") or 0) * point:
        blockers.append("فاصلهٔ حد ضرر کمتر از حد مجاز بروکر است.")
    loss = volume * loss_per_lot
    # Entry is ask for buys / bid for sells: don't charge the spread twice.
    gain = volume * ((target - slippage) / tick * value - commission)
    if gain <= 0:
        blockers.append("هدف پس از هزینه‌های تخمینی سود مثبت ندارد.")
    return {"budget": budget, "risk_percent": risk_percent, "risk_money": allowance,
            "volume": volume, "loss_estimate": loss, "profit_estimate": gain,
            "reward_risk": gain / loss if loss else None,
            "point": point, "tick_size": tick, "tick_value_per_lot": value,
            "point_value": volume * point / tick * value,
            "pip": None, "blockers": blockers, "executable": False,
            "explanation_fa": [
                "پوینت واحد تغییر قیمت است؛ ارزش دلاری آن به حجم وابسته است.",
                "پیپ طلا تعریف یکسانی بین همهٔ بروکرها ندارد؛ محاسبه با تیک انجام شد.",
                "اهرم ریسک را تعیین نمی‌کند؛ حجم و فاصلهٔ حد ضرر تعیین‌کننده‌اند.",
                "ارزش تیک گزارش‌شدهٔ بروکر مبناست و هنوز با محاسبهٔ سود ترمینال اعتبارسنجی نشده است.",
                "زیان تضمین‌شده نیست؛ گپ و لغزش می‌توانند آن را بیشتر کنند."]}


def signal(bars: list[dict], trend_bars: list[dict], strategy: str = "pullback") -> dict:
    if strategy not in {"pullback", "breakout"}:
        raise ValueError("unknown strategy")
    # Exclude the forming candle on both timeframes.
    bars, trend_bars = bars[:-1], trend_bars[:-1]
    if len(bars) < 60 or len(trend_bars) < 210:
        return {"side": "wait", "reason_fa": "دادهٔ کافی برای تحلیل کندل‌های بسته وجود ندارد."}
    closes = [float(b["close"]) for b in bars]
    trend = [float(b["close"]) for b in trend_bars]
    fast, slow = last_defined(ema(trend, 50)), last_defined(ema(trend, 200))
    strength = last_defined(rsi(closes, 14))
    volatility = last_defined(atr([b["high"] for b in bars], [b["low"] for b in bars], closes, 14))
    center = last_defined(ema(closes, 20))
    side = "wait"
    if strategy == "pullback":
        if fast > slow and bars[-1]["low"] <= center < closes[-1] and 45 <= strength <= 65:
            side = "buy"
        elif fast < slow and bars[-1]["high"] >= center > closes[-1] and 35 <= strength <= 55:
            side = "sell"
    else:
        previous = bars[-21:-1]
        if fast > slow and closes[-1] > max(b["high"] for b in previous):
            side = "buy"
        elif fast < slow and closes[-1] < min(b["low"] for b in previous):
            side = "sell"
    return {"side": side, "strategy": strategy, "rsi": strength, "atr": volatility,
            "suggested_distance": 1.5 * volatility if volatility else None,
            "closed_candle_time": bars[-1]["time"],
            "reason_fa": ("شرایط قواعد اولیه برقرار است؛ این یک سناریوی آزمایشی است، نه تضمین سود."
                          if side != "wait" else "شرایط ورود کامل نیست؛ فعلاً معامله نکنید."),
            "rules_fa": "جهت H1 با EMA50/200؛ ورود M15 با پولبک EMA20 و RSI یا شکست ۲۰ کندل."}


def training_snapshot(mt5: Any, query: dict) -> dict:
    symbol = mt5.resolve_symbol(query.get("symbol") or "XAUUSD")
    account = mt5.account_info(fresh=True)
    spec = mt5.symbol_info(symbol, fresh=True)
    quote = mt5.tick(symbol)
    bars = mt5.candles(symbol, "M15", 300)
    trend = mt5.candles(symbol, "H1", 300)
    idea = signal(bars, trend, query.get("strategy", "pullback"))
    distance = positive(query.get("distance") or idea.get("suggested_distance") or 1, "distance")
    plan = risk_plan(spec=spec, budget=query.get("budget", 100),
                     risk_percent=query.get("risk", 1), distance=distance,
                     target=query.get("target", distance),
                     commission=query.get("commission", 0), slippage=query.get("slippage", 0))
    target_money = query.get("target_money")
    if target_money and plan["volume"]:
        desired = positive(target_money, "target_money")
        # Solve the net-profit equation using the already risk-sized volume.
        target = ((desired / plan["volume"] + float(query.get("commission", 0)))
                  * plan["tick_size"] / plan["tick_value_per_lot"]
                  + float(query.get("slippage", 0)))
        plan = risk_plan(spec=spec, budget=query.get("budget", 100),
                         risk_percent=query.get("risk", 1), distance=distance,
                         target=target, commission=query.get("commission", 0),
                         slippage=query.get("slippage", 0))
    else:
        target = float(query.get("target", distance))
    plan["target_distance"] = target
    plan["stop_distance"] = distance
    if account.get("trade_mode") != "demo":
        plan["blockers"].append("حساب دمو تأیید نشد؛ این بخش فقط برای تمرین است.")
    side = idea["side"]
    entry = quote.get("ask" if side == "buy" else "bid") if side != "wait" else None
    margin = None
    if entry and plan["volume"] and not plan["blockers"]:
        margin = mt5.calc_order(symbol, side, plan["volume"], entry).get("margin")
        if margin is None or margin <= 0:
            plan["blockers"].append("مارجین معتبر از ترمینال دریافت نشد.")
        elif margin > min(plan["budget"], float(account.get("margin_free") or 0)):
            plan["blockers"].append("مارجین از بودجهٔ تمرینی یا مارجین آزاد بیشتر است.")
    sign = 1 if side == "buy" else -1
    return {"ok": True, "symbol": symbol, "account": account, "quote": quote,
            "bars": bars, "signal": idea, "plan": plan, "margin": margin,
            "entry": entry, "sl": entry - sign * distance if entry else None,
            "tp": entry + sign * target if entry else None,
            "warnings_fa": ["این بخش هیچ سفارشی ارسال نمی‌کند.",
                            "بودجهٔ تمرینی مستقل از موجودی واقعی دمو است.",
                            "هزینهٔ کمیسیون و لغزش را وارد کنید؛ صفر به معنی رایگان‌بودن نیست.",
                            "این تحلیل قاعده‌محور است، نه پیش‌بینی قطعی یا تحلیل مدل زبانی."]}


def backtest(bars: list[dict], trend_bars: list[dict], *, spec: dict,
             budget: float = 100, risk_percent: float = 1, strategy: str = "pullback",
             commission: float = 0, slippage: float = 0, spread: float = 0) -> dict:
    """Small OHLC teaching simulator, NOT MT5 Strategy Tester.

    Decisions use only closed M15/H1 candles; fill at next open. One position
    at a time, fixed ATR stops, 1:1 gross reward/risk. If both levels occur in
    one candle, stop wins. Intrabar ticks and variable spreads are unavailable.
    """
    initial = positive(budget, "budget")
    if positive(risk_percent, "risk_percent") > 100:
        raise ValueError("risk_percent must not exceed 100")
    if strategy not in {"pullback", "breakout"}:
        raise ValueError("unknown strategy")
    for name, value in (("commission", commission), ("slippage", slippage)):
        if not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"{name} must be finite and non-negative")
    spread = float(spread)
    if not math.isfinite(spread) or spread < 0:
        raise ValueError("spread must be finite and non-negative")
    equity, peak, drawdown = initial, initial, 0.0
    trades: list[dict] = []
    position = None
    skipped = 0
    for i in range(61, len(bars)):
        bar = bars[i]
        if position is None:
            # H1 timestamp is its opening time. Only use fully closed H1 bars.
            history = [b for b in trend_bars if b['time'] + 3600 <= bar['time']]
            if len(history) < 210:
                continue
            idea = signal(bars[:i+1], [*history, history[-1]], strategy)
            if idea['side'] == 'wait' or not idea.get('suggested_distance'):
                continue
            distance = idea['suggested_distance']
            plan = risk_plan(spec=spec, budget=equity, risk_percent=risk_percent,
                             distance=distance, target=distance,
                             commission=commission, slippage=slippage)
            if plan['blockers']:
                skipped += 1
                continue
            direction = 1 if idea['side'] == 'buy' else -1
            # History is bid OHLC; buy fills at ask, sell exits at ask.
            entry = float(bar['open']) + (spread if direction == 1 else 0)
            position = dict(entry=entry, direction=direction, volume=plan['volume'],
                            sl=entry-direction*distance, tp=entry+direction*distance,
                            time=bar['time'])
        direction = position['direction']
        low = bar['low'] + (spread if direction == -1 else 0)
        high = bar['high'] + (spread if direction == -1 else 0)
        stopped = low <= position['sl'] if direction == 1 else high >= position['sl']
        won = high >= position['tp'] if direction == 1 else low <= position['tp']
        if not stopped and not won and i != len(bars)-1:
            continue
        opening = bar['open'] + (spread if direction == -1 else 0)
        if stopped:
            exit_price = min(position['sl'], opening) if direction == 1 else max(position['sl'], opening)
            reason = 'stop'
        elif won:
            exit_price, reason = position['tp'], 'target'
        else:
            exit_price = bar['close'] + (spread if direction == -1 else 0)
            reason = 'end_of_sample'
        pnl = ((direction*(exit_price-position['entry'])-float(slippage))
               / float(spec['trade_tick_size']) * float(spec['trade_tick_value'])
               - float(commission)) * position['volume']
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak-equity)/peak*100)
        trades.append({**position, 'exit': exit_price, 'exit_time': bar['time'],
                       'pnl': pnl, 'reason': reason})
        position = None
        if equity <= 0:
            break
    return {'ok': True, 'initial': initial, 'ending': equity, 'net': equity-initial,
            'max_drawdown_percent': drawdown, 'trades': trades, 'skipped': skipped,
            'win_rate': sum(t['pnl'] > 0 for t in trades)/len(trades)*100 if trades else None,
            'warnings_fa': ['بک‌تست آموزشی OHLC است؛ معادل تستر متاتریدر نیست.',
                            'ورود در بازشدن کندل بعد؛ برخورد همزمان هدف و ضرر به نفع ضرر حل می‌شود.',
                            'اسپرد ثابت فعلی و هزینه‌های ورودی استفاده شده‌اند؛ سواپ محاسبه نشده است.',
                            'هدف برابر فاصلهٔ ATR حد ضرر است؛ نتیجه تضمین عملکرد آینده نیست.',
                            'مارجین و تغییرات تاریخی مشخصات قرارداد در شبیه‌سازی مدل نشده‌اند.']}
