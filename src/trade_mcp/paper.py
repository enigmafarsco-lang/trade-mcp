"""Local paper-only journal. No MT5/CCXT client or network API is imported."""
import fcntl
import json
import math
import os
import tempfile
import time
import uuid
from pathlib import Path


def positive(value):
    if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
        raise ValueError('Positive finite value required')
    return float(value)


class PaperLedger:
    """One open simulated position; locked atomic JSON with persistent loss latch."""
    def __init__(self, path, initial_balance=100.0):
        self.path = Path(path).expanduser()
        self.initial_balance = positive(initial_balance)

    def _update(self, change):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = os.open(str(self.path)+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.is_symlink():
                raise ValueError('Paper journal cannot be a symlink')
            if self.path.exists():
                if self.path.stat().st_size > 8*1024*1024:
                    raise ValueError('Paper journal too large')
                state = json.loads(self.path.read_text())
                if state.get('schema') != 1 or state.get('paper') is not True:
                    raise ValueError('Not a paper journal')
            else:
                state = dict(schema=1, paper=True, balance=self.initial_balance,
                             position=None, events=[], loss_streak=0, latched=False)
            if len(state['events']) >= 10000:
                raise ValueError('Paper event limit reached; no truncation')
            result = change(state)
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent,
                                                 prefix='.paper-', delete=False) as stream:
                    tmp = stream.name
                    os.chmod(tmp, 0o600)
                    json.dump(state, stream, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, self.path)
                tmp = None
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if tmp:
                    os.unlink(tmp)
            return result
        finally:
            os.close(lock)

    def open(self, *, symbol, side, volume, entry, stop, target, multiplier=1.0):
        volume, entry, stop, target, multiplier = map(positive, (volume, entry, stop, target, multiplier))
        if volume > .1:
            raise ValueError('Paper MT5 volume hard cap is 0.1 lots')
        if side not in ('buy', 'sell') or not (stop < entry < target if side == 'buy' else target < entry < stop):
            raise ValueError('Invalid paper direction or protective levels')
        if not isinstance(symbol, str) or not symbol or len(symbol) > 64:
            raise ValueError('Invalid paper symbol')
        def change(state):
            risk = abs(entry-stop)*multiplier*volume
            if state['latched'] or state['position'] or risk > min(positive(state['balance'])*.02, 10)+1e-9:
                raise ValueError('Paper loss latch, open position or risk limit')
            position = dict(id=uuid.uuid4().hex, symbol=symbol, side=side, volume=volume,
                            entry=entry, stop=stop, target=target, multiplier=multiplier,
                            opened_at=time.time())
            state['position'] = position
            state['events'].append(dict(event='open', paper=True, **position))
            return dict(ok=True, paper=True, position_id=position['id'], planned_risk=risk)
        return self._update(change)

    def mark(self, *, symbol, bid, ask):
        bid, ask = positive(bid), positive(ask)
        if ask < bid:
            raise ValueError('Invalid spread')
        def change(state):
            p = state['position']
            if p is None or p['symbol'] != symbol:
                return dict(ok=True, paper=True, closed=False)
            price = bid if p['side'] == 'buy' else ask
            pnl = (price-p['entry'])*p['volume']*p['multiplier']*(1 if p['side'] == 'buy' else -1)
            due = price <= p['stop'] or price >= p['target'] if p['side'] == 'buy' else price >= p['stop'] or price <= p['target']
            due = due or pnl <= -10 or pnl >= 2
            if not due:
                return dict(ok=True, paper=True, closed=False, unrealized_pnl=pnl)
            state['balance'] += pnl
            state['loss_streak'] = state['loss_streak']+1 if pnl < 0 else 0
            state['latched'] = state['latched'] or state['loss_streak'] >= 3
            state['position'] = None
            state['events'].append(dict(event='close', paper=True, position_id=p['id'],
                                        price=price, pnl=pnl, closed_at=time.time(),
                                        fee_assumption='zero_simulated_not_broker_costs'))
            return dict(ok=True, paper=True, closed=True, pnl=pnl, latched=state['latched'])
        return self._update(change)

    def status(self):
        return self._update(lambda state: dict(paper=True, balance=state['balance'],
                            open_position=bool(state['position']), latched=state['latched']))
