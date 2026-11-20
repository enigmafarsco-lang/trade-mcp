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


class DemoGuardBlocked(ValueError):
    """Sanitized, durable guard state that must block new entries."""

    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


def amount(value):
    if isinstance(value, bool):
        raise ValueError('Invalid monetary field')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Nonfinite monetary field')
    return value


def _valid_epoch(value):
    """Return a finite epoch seconds value or None when unavailable/unreliable."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _unresolved_count(state):
    intents = (state or {}).get('intents') or {}
    return sum(1 for intent in intents.values() if intent.get('status') != 'closed')


def account_key(account):
    if account.get('trade_mode') != 'demo' or account.get('currency') != 'USD':
        raise ValueError('USD demo account required')
    if not account.get('login') or not account.get('server'):
        raise ValueError('Account identity missing')
    return hashlib.sha256(json.dumps([str(account['login']), account['server']]).encode()).hexdigest()


def state_for(store, key):
    return store.cash_guard_update(key, lambda state: copy.deepcopy(state))


def _blocker_code(exc):
    code = getattr(exc, 'code', None)
    if isinstance(code, str) and code and all(ch.isalnum() or ch in '._-' for ch in code):
        return code
    return 'cash_guard_reconciliation_failed'


def reserve_entry(mt5, cfg, store):
    account = mt5.account_info(fresh=True)
    key = account_key(account)
    ownership = 'magic_comment'
    if cfg.backend == 'native':
        capability = mt5.request('cash_policy_capabilities')
        if capability.get('ownership_supported') is not True or capability.get('ownership_method') != 'comment_reservation':
            raise ValueError('Native unique comment parameter required')
        ownership = 'comment_reservation'
        samples = mt5.deals(date_from=int(time.time())-86400, date_to=int(time.time()))
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
        samples = [d for d in samples if d.get('type') in {'buy', 'sell'}][-20:]
        if not samples or not all(d.get('costs_complete') is True for d in samples):
            raise ValueError('Native cost completeness must be demonstrated before entry')
    # Reconcile exits before accepting a new entry. No old results are reset.
    inspect_positions(mt5, cfg, store, execute=False)
    if mt5.positions() or mt5.orders():
        raise DemoGuardBlocked('open_position_or_order_blocks_entry')
    loss_limit = min(LOSS_USD, amount(getattr(cfg, 'demo_cash_loss_usd', LOSS_USD)))
    if loss_limit <= 0:
        raise DemoGuardBlocked('invalid_intent_loss_limit')
    marker = 'tmcp:' + uuid.uuid4().hex[:20]
    def change(state):
        if state['latched']:
            raise DemoGuardBlocked('loss_latch_blocks_entry')
        if any(i['status'] != 'closed' for i in state['intents'].values()):
            raise DemoGuardBlocked('unresolved_intent_blocks_entry')
        state['intents'][marker] = {'status': 'reserved', 'started_at': time.time(), 'position_id': None, 'ownership': ownership,
                                   'loss_usd': loss_limit}
    store.cash_guard_update(key, change)
    return key, marker


def entry_result(store, key, marker, accepted):
    # Only definitely rejected requests may release a reservation. Network
    # exceptions leave it reserved until actual broker evidence is reconciled.
    def change(state):
        intent = state['intents'].get(marker)
        if intent is None:
            return
        if accepted:
            intent['status'] = 'awaiting_position'
        elif intent.get('status') == 'reserved':
            intent['status'] = 'closed'
    store.cash_guard_update(key, change)


def inspect_positions(mt5, cfg, store, execute=False, reconcile_existing=False):
    account = mt5.account_info(fresh=True)
    key = account_key(account)
    state = state_for(store, key)
    intents = state['intents']
    unresolved_count = _unresolved_count(state)
    enabled = bool(getattr(cfg, 'demo_cash_policy', False))
    if not enabled and not (reconcile_existing and intents):
        return {'ok': True, 'enabled': enabled, 'latched': state['latched'],
                'loss_streak': state['streak'], 'positions': [],
                'unresolved_intent_count': unresolved_count,
                'review_required': bool(state.get('latched') or unresolved_count),
                'blocker_code': ('unresolved_intent' if unresolved_count else
                                 ('loss_latch' if state.get('latched') else None))}

    positions = mt5.positions()
    if not intents:
        return {'ok': True, 'enabled': enabled, 'latched': state['latched'],
                'loss_streak': state['streak'], 'positions': [],
                'unresolved_intent_count': 0,
                'review_required': bool(state.get('latched')),
                'blocker_code': 'loss_latch' if state.get('latched') else None}
    oldest = min(i['started_at'] for i in intents.values())
    deals = mt5.deals(date_from=max(0, int(oldest)-60), date_to=int(time.time())+1)
    if len(deals) >= 10000:
        raise DemoGuardBlocked('history_may_be_truncated')
    snapshot = None
    if cfg.backend == 'native' and getattr(cfg, 'cost_snapshot_path', ''):
        from .guard_costs import enrich_owned_history, load_guard_snapshot
        snapshot = load_guard_snapshot(mt5, cfg, account)
        deals, positions = enrich_owned_history(deals, positions, snapshot, intents)
    # Unknown ownership/entry types must not be guessed from ticket or symbol.
    def position_id(p):
        return int(p.get('identifier') or p.get('ticket') or 0)
    active = {position_id(p): p for p in positions}
    groups = {}
    seen = set()
    for deal in deals:
        ticket = int(deal.get('ticket') or 0)
        if not ticket or ticket in seen:
            raise DemoGuardBlocked('missing_or_duplicate_deal_ticket')
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
                rows.append({'state': 'unresolved'})
            continue
        pid = ids.pop()
        discovered[marker] = pid
        items = groups.get(pid, [])
        if not items or not any(d.get('entry') == 'in' for d in items):
            raise DemoGuardBlocked('opening_deal_history_required')
        if intent.get('ownership') == 'comment_reservation':
            matched_openings = [d for d in items if owns(d) and d.get('entry') == 'in']
            if not matched_openings:
                raise DemoGuardBlocked('reserved_opening_identity_missing')
            # Unique broker-supported identity is the marker/ownership match on the
            # opening deal for this reserved position. Deal timestamps may be
            # unavailable/unparseable in normalized history; never adopt by
            # symbol/side and never fail solely on unverifiable time.
            if not any(
                    _valid_epoch(d.get('time')) is None or
                    _valid_epoch(d.get('time')) >= _valid_epoch(intent.get('started_at')) - 5
                    for d in matched_openings):
                raise DemoGuardBlocked('reserved_opening_time_mismatch')
        if any(d.get('entry') not in {'in', 'out', 'out_by'} for d in items):
            raise DemoGuardBlocked('reversal_or_unknown_entry')
        if any(d.get('costs_complete') is False for d in items):
            raise DemoGuardBlocked('explicit_cost_fields_required')
        net = sum(amount(d[k]) for d in items for k in ('profit', 'swap', 'commission', 'fee'))
        if pid not in active:
            opened = sum(amount(d['volume']) for d in items if d['entry'] == 'in')
            exited = sum(amount(d['volume']) for d in items if d['entry'] in {'out', 'out_by'})
            if opened <= 0 or abs(opened-exited) > 1e-8:
                raise DemoGuardBlocked('incomplete_closed_position_volume')
            close_time = max((_valid_epoch(d.get('time')) or 0.0 for d in items), default=0.0)
            closed.append((close_time, pid, marker, net))
            continue
        p = active[pid]
        if not owns(p):
            raise DemoGuardBlocked('position_identity_mismatch')
        if amount(p.get('sl', 0)) <= 0:
            raise DemoGuardBlocked('broker_sl_required')
        if p.get('pnl_complete') is False:
            raise DemoGuardBlocked('floating_pnl_incomplete')
        net += amount(p['profit']) + amount(p['swap'])
        row = {'net_usd': net, 'state': intent['status']}
        rows.append(row)
        cash_loss = amount(intent.get('loss_usd', LOSS_USD))
        if not 0 < cash_loss <= LOSS_USD:
            raise DemoGuardBlocked('invalid_intent_loss_limit')
        if net <= -cash_loss or net >= TARGET_USD:
            if (not execute or not cfg.allow_trading or getattr(cfg, 'paper_trading', False)
                    or not enabled):
                row['exit_due'] = True
                continue
            def claim(current, marker=marker):
                item = current['intents'][marker]
                if item['status'] not in {'reserved', 'awaiting_position', 'open'}:
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
                        raise DemoGuardBlocked('account_changed_before_close')
                    if not 0 <= time.time()-snapshot['generated_at'] <= 10:
                        raise DemoGuardBlocked('cost_source_aged_before_close')
                result = mt5.close_position(int(p['ticket']), deviation=cfg.deviation_points)
                row['close_retcode'] = result.get('retcode')
    def reconcile(current):
        for marker, pid in discovered.items():
            current['intents'][marker]['position_id'] = pid
            if current['intents'][marker]['status'] in {'reserved', 'awaiting_position'} and pid in active:
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
        return {'latched': current['latched'], 'loss_streak': streak,
                'unresolved_intent_count': _unresolved_count(current)}
    summary = store.cash_guard_update(key, reconcile)
    unresolved_intent_count = summary.get('unresolved_intent_count', _unresolved_count(state))
    has_unresolved = any(row.get('state') == 'unresolved' for row in rows) or unresolved_intent_count > 0
    blocker = None
    if has_unresolved:
        blocker = 'unresolved_intent'
    elif any(row.get('exit_due') for row in rows):
        blocker = 'exit_due'
    elif summary.get('latched'):
        blocker = 'loss_latch'
    return {'ok': True, 'enabled': enabled, **summary, 'positions': rows,
            'review_required': bool(summary['latched'] or has_unresolved),
            'unresolved_intent_count': unresolved_intent_count,
            'blocker_code': blocker}


class DemoGuardian:
    """Independent polling, not blocked by a long LLM call's console lock."""
    def __init__(self, app):
        import threading
        self.app = app
        self.stop_event = threading.Event()
        self.thread = None
        self._lock = threading.Lock()
        self.last = self._health(
            ok=True,
            enabled=getattr(getattr(app, 'cfg', None), 'demo_cash_policy', False),
            running=False,
            blocker_code=None,
            unresolved_intent_count=0,
            last_poll_at=None,
            last_success_at=None,
        )

    def status(self):
        """Return sanitized guardian health, enriched from durable state when available."""
        health = self.health()
        try:
            from .store import Store
            store = Store(str(self.app.store.path))
            try:
                account = self.app.mt5.account_info(fresh=True)
                key = account_key(account)
                state = state_for(store, key)
                unresolved = _unresolved_count(state)
                health['unresolved_intent_count'] = unresolved
                health['latched'] = bool(state.get('latched'))
                health['loss_streak'] = state.get('streak', 0)
                blocker = health.get('current_blocker_code')
                if unresolved and blocker in (None, 'loss_latch') and not state.get('latched'):
                    blocker = 'unresolved_intent'
                elif state.get('latched') and blocker in (None,):
                    blocker = 'loss_latch'
                health['current_blocker_code'] = blocker
            finally:
                store.close()
        except Exception:
            # keep last known sanitized health; no raw errors
            health.setdefault('unresolved_intent_count', 0)
            health.setdefault('latched', False)
            health.setdefault('loss_streak', 0)
        return health

    @staticmethod
    def _health(*, ok, enabled, running, blocker_code, unresolved_intent_count,
                last_poll_at, last_success_at):
        return {
            'ok': bool(ok),
            'enabled': bool(enabled),
            'running': bool(running),
            'last_poll_at': last_poll_at,
            'last_successful_reconciliation_at': last_success_at,
            'current_blocker_code': blocker_code,
            'unresolved_intent_count': int(unresolved_intent_count or 0),
        }

    def _publish(self, **fields):
        with self._lock:
            current = dict(self.last)
            data = {
                'ok': fields.get('ok', current.get('ok', True)),
                'enabled': fields.get('enabled', current.get('enabled', False)),
                'running': fields.get('running', current.get('running', False)),
                'blocker_code': fields.get('blocker_code', current.get('current_blocker_code')),
                'unresolved_intent_count': fields.get(
                    'unresolved_intent_count', current.get('unresolved_intent_count', 0)),
                'last_poll_at': fields.get('last_poll_at', current.get('last_poll_at')),
                'last_success_at': fields.get(
                    'last_success_at', current.get('last_successful_reconciliation_at')),
            }
            self.last = self._health(**data)
            return dict(self.last)

    def health(self):
        with self._lock:
            return dict(self.last)

    def reconcile_now(self, *, execute=False, store=None):
        """One-shot supported reconciliation. Read-only when execute is False.

        In no-order mode this reconciles existing durable intents without
        starting the polling thread and without submitting orders.
        """
        cfg = self.app.cfg
        own_store = store is None
        if own_store:
            from .store import Store
            store = Store(str(self.app.store.path))
        try:
            now = time.time()
            enabled = bool(getattr(cfg, 'demo_cash_policy', False))
            try:
                result = inspect_positions(self.app.mt5, cfg, store, execute=execute,
                                           reconcile_existing=True)
                blocker = result.get('blocker_code')
                return self._publish(
                    ok=True, enabled=enabled,
                    running=bool(self.thread and self.thread.is_alive()),
                    blocker_code=blocker,
                    unresolved_intent_count=result.get('unresolved_intent_count', 0),
                    last_poll_at=now, last_success_at=now,
                ), result
            except Exception as exc:  # noqa: BLE001 - sanitized health only
                blocker = _blocker_code(exc)
                # unresolved count best-effort from durable state
                unresolved = self.last.get('unresolved_intent_count', 0)
                try:
                    account = self.app.mt5.account_info(fresh=True)
                    key = account_key(account)
                    state = state_for(store, key)
                    unresolved = _unresolved_count(state)
                except Exception:
                    pass
                # preserve last known blocker details only if sanitized; raw exceptions never exposed
                health = self._publish(
                    ok=False, enabled=enabled,
                    running=bool(self.thread and self.thread.is_alive()),
                    blocker_code=blocker,
                    unresolved_intent_count=unresolved,
                    last_poll_at=now, last_success_at=self.last.get('last_successful_reconciliation_at'),
                )
                return health, None
        finally:
            if own_store:
                store.close()

    def start(self):
        import threading
        if not getattr(getattr(self.app, 'cfg', None), 'demo_cash_policy', False):
            return
        self.thread = threading.Thread(target=self._run, name='demo-cash-guardian', daemon=True)
        self.thread.start()

    def _run(self):
        from .store import Store
        store = Store(str(self.app.store.path))  # Separate SQLite connection for this thread.
        try:
            while not self.stop_event.is_set():
                self.reconcile_now(execute=True, store=store)
                self.stop_event.wait(2)
        finally:
            store.close()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
