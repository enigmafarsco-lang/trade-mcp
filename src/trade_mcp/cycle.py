"""One page-owned advisory loop. No heartbeat means no new model requests."""
from __future__ import annotations

import secrets
import threading
import time

from .agents import AgentEngine


class PageCycle:
    lease_seconds = 20

    def __init__(self, state):
        self.state = state
        self.lock = threading.RLock()
        self.owner = None
        self.deadline = 0
        self.agent_id = None
        self.interval = 60
        self.next_run = 0
        self.busy = False
        self.reason = 'چرخه شروع نشده است.'
        self.thread = None
        self.shutdown = threading.Event()

    def status(self):
        with self.lock:
            self._expire()
            return {'active': bool(self.owner), 'agent_id': self.agent_id, 'busy': self.busy,
                    'interval': self.interval, 'reason': self.reason,
                    'next_run': self.next_run if self.owner else None}

    def _expire(self):
        if self.owner and time.time() >= self.deadline:
            self.owner = None
            self.reason = 'چرخه به علت نبود heartbeat صفحه متوقف شد.'

    def command(self, payload):
        with self.lock:
            self._expire()
            action = payload.get('action')
            if action == 'start':
                if self.owner or self.busy:
                    return {'ok': False, 'error': 'cycle_already_active_or_finishing'}
                if self.state.scheduler.running:
                    return {'ok': False, 'error': 'stop_background_scheduler_first'}
                cfg = self.state.cfg
                test = self.state.model_test
                if (not cfg.opencode_url or test.get('state') != 'ready'
                        or tuple(test.get('model_key', ())) != (cfg.opencode_model, cfg.opencode_variant)):
                    return {'ok': False, 'error': 'successful_model_test_required'}
                if payload.get('consent_model_requests') is not True:
                    return {'ok': False, 'error': 'model_usage_consent_required'}
                agent = self.state.store.get_agent(str(payload.get('agent_id') or ''))
                if not agent:
                    return {'ok': False, 'error': 'unknown_agent'}
                interval = max(30, min(3600, int(payload.get('interval') or 60)))
                self.owner = secrets.token_urlsafe(24)
                self.agent_id, self.interval = agent['id'], interval
                self.deadline, self.next_run = time.time()+self.lease_seconds, time.time()
                self.reason = 'تحلیل دوره‌ای مشورتی فعال است؛ هیچ معامله‌ای خودکار اجرا نمی‌شود.'
                if self.thread is None or not self.thread.is_alive():
                    self.thread = threading.Thread(target=self._loop, daemon=True, name='page-advisory-cycle')
                    self.thread.start()
                return {'ok': True, 'lease': self.owner, 'status': self.status()}
            if not self.owner or payload.get('lease') != self.owner:
                return {'ok': False, 'error': 'cycle_owner_mismatch'}
            if action == 'heartbeat':
                self.deadline = time.time()+self.lease_seconds
            elif action == 'stop':
                self.owner = None
                self.reason = 'چرخه توسط صفحه متوقف شد؛ درخواست در حال انجام ممکن است پایان یابد، ولی معامله نمی‌کند.'
            else:
                return {'ok': False, 'error': 'invalid_cycle_action'}
            return {'ok': True, 'status': self.status()}

    def tick(self):
        with self.lock:
            self._expire()
            if not self.owner or self.busy or time.time() < self.next_run:
                return
            owner, agent_id = self.owner, self.agent_id
            self.busy = True
            self.next_run = time.time()+self.interval
        try:
            with self.state.lock:
                with self.lock:
                    self._expire()
                    if self.owner != owner:
                        return
                agent = self.state.store.get_agent(agent_id)
                if not agent:
                    raise ValueError('agent removed')
                # Never duplicate an existing pending signal for the selected symbol.
                pending = self.state.store.list_proposals(limit=100)
                if any(p['status'] == 'pending' and p['expires_at'] > time.time()
                       and (p['payload'].get('symbol') in agent['symbols']
                            or p['payload'].get('comment') == 'agent:'+agent_id) for p in pending):
                    with self.lock:
                        self.reason = 'پیشنهاد معتبر موجود است؛ منتظر تأیید شما یا انقضا هستیم.'
                    return
                report = AgentEngine(cfg=self.state.cfg, mt5=self.state.app.mt5,
                                     store=self.state.store).run({**agent, 'mode': 'advisory', 'require_open_market': True})
            errors = [d.get('model_error') for d in report.get('decisions', []) if d.get('model_error')]
            with self.lock:
                if errors or report.get('errors'):
                    self.owner = None
                    self.state.model_test = {**(errors[0] if errors else {}), 'state': 'failed',
                                             'message': errors[0]['message'] if errors else 'خطای تحلیل؛ بررسی و آزمایش مجدد لازم است.',
                                             'tested_at': time.time(),
                                             'model_key': (self.state.cfg.opencode_model, self.state.cfg.opencode_variant)}
                    self.reason = errors[0]['message'] if errors else 'چرخه به علت خطای تحلیل متوقف شد؛ قبل از شروع مجدد بررسی کنید.'
                elif self.owner == owner:
                    self.reason = 'گزارش ذخیره شد؛ تا نوبت بعدی یا تأیید شما منتظر می‌مانیم.'
        except Exception:
            with self.lock:
                self.owner = None
                self.reason = 'خطای چرخه؛ تحلیل متوقف شد و تأییدی ارسال نشد.'
        finally:
            with self.lock:
                self.busy = False

    def _loop(self):
        while not self.shutdown.wait(.5):
            self.tick()

    def close(self):
        self.shutdown.set()
        with self.lock:
            self.owner = None
