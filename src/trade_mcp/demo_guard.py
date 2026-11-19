"""Opt-in USD demo-only cash exits and durable three-loss entry latch.

Only nonce-tagged entries reserved by this journal are owned. No adoption of
historical/manual trades. Unknown execution/close outcomes require reconciliation,
not automatic re-submission. Does not select, retrain, or reset strategies.
"""
import copy
import hashlib
import json
import math
import time
import uuid

MAGIC = 73104219
LOSS_USD = 10.0
TARGET_USD = 2.0


def amount(value):
    if isinstance(value, bool):
        raise ValueError('Invalid monetary field')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Nonfinite monetary field')
    return value


def account_key(account):
    if account.get('trade_mode') != 'demo' or account.get('currency') != 'USD':
        raise ValueError('USD demo account required')
    if not account.get('login') or not account.get('server'):
        raise ValueError('Account identity missing')
    return hashlib.sha256(json.dumps([str(account['login']),account['server']]).encode()).hexdigest()


def state_for(store, key):
    return store.cash_guard_update(key, lambda state: copy.deepcopy(state))


def reserve_entry(mt5, cfg, store):
    account = mt5.account_info(fresh=True)
    key = account_key(account)
    ownership = 'magic_comment'
    if cfg.backend == 'native':
        capability = mt5.request('cash_policy_capabilities')
        if capability.get('ownership_supported') is not True or capability.get('ownership_method') != 'comment_reservation':
            raise ValueError('Native unique comment parameter required')
        ownership = 'comment_reservation'
        samples = mt5.deals(date_from=int(time.time())-86400,date_to=int(time.time()))
        if getattr(cfg, 'cost_snapshot_path', ''):
            from .cost_reconciliation import correlate_deal_costs
            from .guard_costs import load_guard_snapshot
            snapshot = load_guard_snapshot(mt5, cfg, account)
            # Pre-entry compatibility proof uses actual shared recent tickets,
            # not unrelated older native history or an invented zero cost.
            ids = {str(d['ticket']) for d in snapshot['deals']}
            shared = [d for d in samples if str(d['ticket']) in ids and d.get('type') in {'buy', 'sell'}][-20:]
            if not shared:
                raise ValueError('No shared native/snapshot cost samples')
            samples = correlate_deal_costs(shared, snapshot)['deals']
        samples = [d for d in samples if d.get('type') in {'buy','sell'}][-20:]
        if not samples or not all(d.get('costs_complete') is True for d in samples):
            raise ValueError('Native cost completeness must be demonstrated before entry')
    # Reconcile exits before accepting a new entry. No old results are reset.
    inspect_positions(mt5, cfg, store, execute=False)
    if mt5.positions() or mt5.orders():
        raise ValueError('One position at a time; existing positions/orders block entries')
    marker = 'tmcp:'+uuid.uuid4().hex[:20]
    def change(state):
        if state['latched'] or any(i['status'] != 'closed' for i in state['intents'].values()):
            raise ValueError('Loss latch or unresolved entry/exit blocks trading')
        state['intents'][marker] = {'status':'reserved','started_at':time.time(), 'position_id':None, 'ownership':ownership}
    store.cash_guard_update(key, change)
    return key, marker


def entry_result(store, key, marker, accepted):
    # Only definitely rejected requests may release a reservation. Network
    # exceptions leave it reserved until actual broker evidence is reconciled.
    def change(state):
        state['intents'][marker]['status'] = 'awaiting_position' if accepted else 'closed'
    store.cash_guard_update(key, change)


def inspect_positions(mt5, cfg, store, execute=False):
    if not cfg.demo_cash_policy:
        return {'ok':True,'enabled':False}
    account = mt5.account_info(fresh=True)
    key = account_key(account)
    positions = mt5.positions()
    state = state_for(store, key)
    intents = state['intents']
    if not intents:
        return {'ok':True,'enabled':True,'latched':state['latched'],'loss_streak':state['streak'], 'positions':[]}
    oldest = min(i['started_at'] for i in intents.values())
    deals = mt5.deals(date_from=max(0,int(oldest)-60), date_to=int(time.time())+1)
    if len(deals) >= 10000:
        raise ValueError('History may be truncated; trading blocked')
    snapshot = None
    if cfg.backend == 'native' and getattr(cfg, 'cost_snapshot_path', ''):
        from .guard_costs import enrich_owned_history, load_guard_snapshot
        snapshot = load_guard_snapshot(mt5, cfg, account)
        deals, positions = enrich_owned_history(deals, positions, snapshot, intents)
    # Unknown ownership/entry types must not be guessed from ticket or symbol.
    def position_id(p):
        return int(p.get('identifier') or p.get('ticket') or 0)
    active = {position_id(p):p for p in positions}
    groups = {}
    seen = set()
    for deal in deals:
        ticket = int(deal.get('ticket') or 0)
        if not ticket or ticket in seen:
            raise ValueError('Missing/duplicate deal ticket')
        seen.add(ticket)
        groups.setdefault(int(deal.get('position_id') or 0), []).append(deal)
    discovered = {}
    closed = []
    rows = []
    for marker, intent in intents.items():
        def owns(item, marker=marker, intent=intent):
            return item.get('comment') == marker and (intent.get('ownership') == 'comment_reservation'
                                                     or item.get('magic') == MAGIC)
        matches = [p for p in positions if owns(p)]
        ids = {position_id(p) for p in matches}
        ids.update(pid for pid, items in groups.items() if any(
            owns(d) and d.get('entry') == 'in' for d in items))
        if intent.get('position_id'):
            ids.add(intent['position_id'])
        if len(ids) != 1 or 0 in ids:
            if intent['status'] != 'closed':
                rows.append({'state':'unresolved','marker':marker})
            continue
        pid = ids.pop()
        discovered[marker] = pid
        items = groups.get(pid, [])
        if not items or not any(d.get('entry') == 'in' for d in items):
            raise ValueError('Opening deal history required to account for costs')
        if intent.get('ownership') == 'comment_reservation' and not any(
                owns(d) and d.get('entry') == 'in' and amount(d['time']) >= intent['started_at']-5 for d in items):
            raise ValueError('Reserved opening comment and time must match position history')
        if any(d.get('entry') not in {'in','out','out_by'} for d in items):
            raise ValueError('Reversal/unknown deal entry requires manual reconciliation')
        if any(d.get('costs_complete') is False for d in items):
            raise ValueError('Explicit deal cost fields required')
        net = sum(amount(d[k]) for d in items for k in ('profit','swap','commission','fee'))
        if pid not in active:
            opened = sum(amount(d['volume']) for d in items if d['entry'] == 'in')
            exited = sum(amount(d['volume']) for d in items if d['entry'] in {'out','out_by'})
            if opened <= 0 or abs(opened-exited) > 1e-8:
                raise ValueError('Incomplete closed-position volume history')
            closed.append((max(amount(d['time']) for d in items), pid, marker, net))
            continue
        p = active[pid]
        if not owns(p):
            raise ValueError('Position identity no longer matches owned entry')
        if amount(p.get('sl',0)) <= 0:
            raise ValueError('Broker-side protective SL required')
        if p.get('pnl_complete') is False:
            raise ValueError('Explicit floating P/L and swap required')
        net += amount(p['profit'])+amount(p['swap'])
        row = {'ticket':p['ticket'], 'net_usd':net, 'state':intent['status']}
        rows.append(row)
        if net <= -LOSS_USD or net >= TARGET_USD:
            if not execute or not cfg.allow_trading:
                row['exit_due'] = True
                continue
            def claim(current, marker=marker):
                item = current['intents'][marker]
                if item['status'] not in {'reserved','awaiting_position','open'}:
                    return False
                item['status'] = 'close_requested'
                return True
            if store.cash_guard_update(key, claim):
                # Durable claim BEFORE side effect. No automatic resubmission on
                # timeout, partial close or rejection: unresolved state blocks entry.
                if snapshot is not None:
                    # Freshness/account recheck immediately before the side effect.
                    # No blind retry: a failed check leaves the durable close claim.
                    from .guard_costs import verify_close_target
                    verify_close_target(mt5, p)
                    if account_key(mt5.account_info(fresh=True)) != key:
                        raise ValueError('Account changed before close')
                    if not 0 <= time.time()-snapshot['generated_at'] <= 10:
                        raise ValueError('Cost source aged before close')
                result = mt5.close_position(int(p['ticket']), deviation=cfg.deviation_points)
                row['close_retcode'] = result.get('retcode')
    def reconcile(current):
        for marker, pid in discovered.items():
            current['intents'][marker]['position_id'] = pid
            if current['intents'][marker]['status'] in {'reserved','awaiting_position'} and pid in active:
                current['intents'][marker]['status'] = 'open'
        for _, _, marker, net in sorted(closed):
            current['intents'][marker].update(status='closed', net_usd=net)
        # Recompute full owned closed history so repeated polls and restarts
        # cannot double-count. Latch remains set even after later wins.
        streak = 0
        for _, _, _, net in sorted(closed):
            streak = streak+1 if net < 0 else 0
            if streak >= 3:
                current['latched'] = True
        current['streak'] = streak
        return {'latched':current['latched'],'loss_streak':streak}
    summary = store.cash_guard_update(key, reconcile)
    return {'ok':True,'enabled':True, **summary, 'positions':rows,
            'review_required':summary['latched']}


class DemoGuardian:
    """Independent polling, not blocked by a long LLM call's console lock."""
    def __init__(self, app):
        import threading
        self.app = app
        self.stop_event = threading.Event()
        self.thread = None
        self.last = {'ok':True,'enabled':getattr(getattr(app,'cfg',None),'demo_cash_policy',False),'running':False}

    def start(self):
        import threading
        if not getattr(getattr(self.app,'cfg',None),'demo_cash_policy',False):
            return
        self.thread = threading.Thread(target=self._run, name='demo-cash-guardian', daemon=True)
        self.thread.start()

    def _run(self):
        from .store import Store
        store = Store(str(self.app.store.path))  # Separate SQLite connection for this thread.
        try:
            while not self.stop_event.is_set():
                try:
                    self.last = {**inspect_positions(self.app.mt5,self.app.cfg,store,execute=True),
                                 'running':True,'checked_at':time.time()}
                except Exception:
                    self.last = {'ok':False,'enabled':True,'running':True,
                                 'error':'cash_guard_reconciliation_failed','checked_at':time.time()}
                self.stop_event.wait(2)
        finally:
            store.close()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
