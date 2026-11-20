"""Independent user-defined M1 tick momentum, not the generic confidence ensemble."""
import math
import time

from .execution import confirm, propose_open
from .risk import loss_per_lot, round_to_step


def number(value):
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError('Invalid tick numeric field')
    return float(value)


class TickMomentum:
    """Count 2 or 3 consecutive fresh, two-sided quote changes in the same M1 bar."""
    def __init__(self, consecutive=3):
        if type(consecutive) is not int or consecutive not in (2, 3):
            raise ValueError('Choose 2 or 3 consecutive quote changes')
        self.required = consecutive
        self.previous = None
        self.direction = 0
        self.count = 0

    def observe(self, tick, candle, *, now=None):
        now = time.time() if now is None else now
        # Millisecond timestamp preferred. Seconds-only sources count at most once/second.
        stamp = number(tick.get('time_msc') or number(tick['time'])*1000)
        bid, ask = number(tick['bid']), number(tick['ask'])
        opened = number(candle['open'])
        start = number(candle.get('time_utc', candle['time']))
        if min(bid, ask, opened) <= 0 or ask < bid or not -2 <= now-stamp/1000 <= 2:
            self.previous, self.count, self.direction = None, 0, 0
            return {'action': 'wait', 'reason': 'invalid_or_stale_quote'}
        if not start <= stamp/1000 < start+60 or not start <= now < start+60:
            self.previous, self.count, self.direction = None, 0, 0
            return {'action': 'wait', 'reason': 'forming_M1_required'}
        old = self.previous
        if old and stamp <= old[0]:
            return {'action': 'wait', 'reason': 'duplicate_or_out_of_order_tick'}
        self.previous = (stamp, bid, ask, start)
        if not old or old[3] != start or stamp-old[0] > 2000:
            self.count, self.direction = 0, 0
            return {'action': 'wait', 'reason': 'warming_up'}
        direction = 1 if bid > old[1] and ask > old[2] else -1 if bid < old[1] and ask < old[2] else 0
        self.count = self.count+1 if direction and direction == self.direction else 1 if direction else 0
        self.direction = direction
        aligned = bid > opened if direction == 1 else ask < opened if direction == -1 else False
        if self.count < self.required or not aligned:
            return {'action': 'wait', 'reason': 'insufficient_aligned_ticks'}
        self.count = 0  # consume signal, never replay after a failed order
        return {'action': 'buy' if direction == 1 else 'sell',
                'source': 'user_M1_tick_momentum', 'consecutive_changes': self.required,
                'evidence_kind': 'received_quote_samples' if tick.get('time_source') == 'received'
                                 else 'timestamped_quotes'}


def tick_step(app, agent, strategy):
    """Real proposal/confirmation route; caller must run the bounded guardian preflight."""
    mt5, cfg = app.mt5, app.cfg
    tick_cfg = cfg.with_overrides(demo_cash_loss_usd=2.0)
    if not cfg.trading_enabled or not cfg.demo_cash_policy:
        raise ValueError('Explicit demo execution policy required')
    symbol = mt5.resolve_symbol(agent['symbols'][0])
    candles = mt5.candles(symbol, 'M1', 2)
    if not candles:
        return {'symbols': [{'status': 'refused', 'blockers': ['forming_M1_missing']}]}
    tick = mt5.tick(symbol)
    signal = strategy.observe(tick, candles[-1])
    if signal['action'] == 'wait':
        return {'symbols': [{'status': 'refused', 'blockers': [signal['reason']]}]}
    if mt5.positions() or mt5.orders():
        return {'symbols': [{'status': 'refused', 'blockers': ['existing_exposure']}]}
    info = mt5.symbol_info(symbol, fresh=True)
    account = mt5.account_info(fresh=True)
    from .demo_guard import account_key
    account_key(account)
    volume = round_to_step(min(number(agent['max_lot']), number(cfg.max_lot),
                               number(info['volume_max'])), number(info['volume_step']))
    if volume <= 0 or volume < number(info['volume_min']):
        return {'symbols': [{'status': 'refused', 'blockers': ['volume_below_minimum']}]}
    multiplier = loss_per_lot(info, 1)
    if multiplier <= 0:
        raise ValueError('Tick economics required')
    from .config import sizing_balance
    cash = min(2.0, sizing_balance(account, cfg)*min(2, cfg.max_risk_percent,
                                                  number(agent['max_risk']))/100)
    if cash <= 0:
        raise ValueError('No risk budget')
    side = signal['action']
    entry = number(tick['ask'] if side == 'buy' else tick['bid'])
    sign = 1 if side == 'buy' else -1
    digits = int(info['digits'])
    stop = round(entry-sign*cash/(volume*multiplier), digits)
    target = round(entry+sign*2/(volume*multiplier), digits)
    if abs(entry-stop)*volume*multiplier > cash+1e-8:
        return {'symbols': [{'status': 'refused', 'blockers': ['rounded_SL_exceeds_budget']}]}
    # Entry guard rechecks signal age/price on propose; confirm independently revalidates.
    def guard(fresh, spec):
        good = (0 <= time.time()-number(fresh['time']) <= 2
                and number(fresh['bid']) == number(tick['bid'])
                and number(fresh['ask']) == number(tick['ask']))
        return {'ok': good, 'reasons': [] if good else ['tick_changed_before_proposal']}
    proposal = propose_open(mt5=mt5, cfg=tick_cfg, store=app.store, symbol=symbol,
        side=side, volume=volume, stop_loss=stop, take_profit=target,
        max_lot=agent['max_lot'], comment=f"agent:{agent['id']}", entry_guard=guard)
    if not proposal.get('ok') or proposal.get('blocked'):
        return {'symbols': [{'status': 'refused', 'blockers': ['tick_proposal_refused']}]}
    result = confirm(mt5=mt5, cfg=tick_cfg, store=app.store,
                     proposal_id=str(proposal.get('proposal_id') or proposal.get('id')))
    return {'symbols': [{'status': 'executed' if result.get('ok') else 'pending',
                          'source': 'user_M1_tick_momentum'}], 'used_llm': False}
