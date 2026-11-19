"""Bounded cloud decision loop with paper-only execution; no arbitrary tool dispatch."""
import argparse
import json
import math
import os
import threading
import time
from datetime import datetime, timezone

from .agent_safety import post_response_check
from .engines.live import analyze_broker
from .paper import PaperLedger
from .risk import loss_per_lot, validate_order
from .risk_manager import calculate_position_size


def paper_cycle(app, llm, ledger, symbol, timeframe='M15'):
    """One combined evidence request, one model response, at most one paper entry."""
    started = time.monotonic()
    evidence = analyze_broker(app.mt5, app.cfg, symbol, timeframe)
    if not evidence.get('ok') or not (evidence.get('technical') or {}).get('ready'):
        return {'ok': False, 'error': 'technical_not_ready', 'paper': True}
    symbol = evidence['symbol']
    quote = app.mt5.tick(symbol)
    tick_age = time.time()-float(quote['time'])
    if not -2 <= tick_age <= 10:
        return {'ok': False, 'paper': True, 'error': 'stale_paper_quote'}
    ledger.mark(symbol=symbol, bid=quote['bid'], ask=quote['ask'])
    status = ledger.status()
    if status['open_position'] or status['latched']:
        return {'ok': True, 'paper': True, 'action': 'HOLD', 'reason': 'paper_position_or_loss_latch'}
    quote = {**quote, 'tick_time': datetime.fromtimestamp(float(quote['time']), timezone.utc).isoformat()}
    decision = llm.chat_json([
        {'role': 'system', 'content': 'Return only JSON with action buy/sell/wait, confidence '
         'between 0 and 1, stop_loss and take_profit. Decide from the supplied closed-bar '
         'evidence. HOLD on uncertainty. No tool calls, code, provider changes or risk changes.'},
        {'role': 'user', 'content': json.dumps({'technical': evidence, 'quote': quote}, allow_nan=False)},
    ])
    if not isinstance(decision, dict) or decision.get('action') not in ('buy', 'sell', 'wait'):
        raise ValueError('Invalid structured model decision')
    confidence = decision.get('confidence')
    if isinstance(confidence, bool) or confidence is None or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
        raise ValueError('Invalid model confidence')
    if decision['action'] == 'wait' or float(confidence) < .7:
        return {'ok': True, 'paper': True, 'action': 'HOLD', 'source': 'actual_model_response'}
    info = app.mt5.symbol_info(symbol, fresh=True)
    tick = app.mt5.tick(symbol)
    check = post_response_check(quote=quote, tick=tick, info=info, decision=decision,
                               elapsed=time.monotonic()-started, now=time.time(),
                               max_spread_points=app.cfg.max_spread_points)
    if not check['ok']:
        return {'ok': False, 'paper': True, 'error': 'post_response_check_failed'}
    entry = check['checked_entry']
    volume = calculate_position_size(status['balance'], min(app.cfg.max_risk_percent, 2),
        entry, decision['stop_loss'], loss_per_price_unit=loss_per_lot(info, 1),
        min_size=info['volume_min'], max_size=min(app.cfg.max_lot, info['volume_max']),
        step=info['volume_step'], max_cash_risk=10)
    # Paper evaluation uses existing guards with a simulated USD demo account.
    cfg = app.cfg.with_overrides(allow_trading=True, demo_cash_policy=True)
    issues = validate_order(cfg=cfg, info=info, symbol=symbol, side=decision['action'],
        volume=volume, entry=entry, stop=float(decision['stop_loss']),
        take_profit=float(decision['take_profit']),
        account={'trade_mode': 'demo', 'currency': 'USD', 'equity': status['balance']})
    if any(issue['severity'] == 'blocker' for issue in issues):
        return {'ok': False, 'paper': True, 'error': 'paper_risk_blocked'}
    return ledger.open(symbol=symbol, side=decision['action'], volume=volume, entry=entry,
                       stop=decision['stop_loss'], target=decision['take_profit'],
                       multiplier=loss_per_lot(info, 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol', default='')
    parser.add_argument('--timeframe', default='M15')
    parser.add_argument('--interval', type=int, default=900)
    parser.add_argument('--duration', type=int, default=300)
    parser.add_argument('--max-cycles', type=int, default=1)
    parser.add_argument('--paper-log', default='logs/trades_log.json')
    parser.add_argument('--paper-balance', type=float, default=100)
    parser.add_argument('--consent-model-requests', action='store_true')
    args = parser.parse_args()
    from .config import load_dotenv
    load_dotenv()
    paper = os.environ.get('TRADE_MCP_PAPER_TRADING', os.environ.get('PAPER_TRADING', 'true')).lower()
    if paper not in ('true', '1', 'yes'):
        parser.error('Standalone runner is paper-only; live orders use the existing guarded agent runtime')
    if not args.consent_model_requests:
        parser.error('Explicit model-request consent required; no request was sent')
    if not 1 <= args.max_cycles <= 5 or not 1 <= args.duration <= 300 or args.interval < 30:
        parser.error('Maximum 5 cycles / 300 seconds; interval at least 30 seconds')
    from .agents import LLMClient
    from .app import build_app
    app = build_app()
    try:
        llm = LLMClient.from_config(app.cfg, mode='web', store=app.store, request_kind='agent_run')
        if not llm.configured:
            raise ValueError('Model not configured')
        ledger = PaperLedger(args.paper_log, args.paper_balance)
        deadline = time.monotonic()+args.duration
        for _ in range(args.max_cycles):
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                break
            if not app.store.model_budget_status().get('allowed'):
                print('{"ok":false,"paper":true,"error":"active_model_budget_required"}')
                return 2
            llm.timeout = min(60, remaining)
            result = paper_cycle(app, llm, ledger, args.symbol or app.cfg.default_symbol, args.timeframe)
            print(json.dumps(result, allow_nan=False))  # CLI only, never MCP stdio.
            if not result.get('ok'):
                break
            remaining = deadline-time.monotonic()
            if remaining <= 0 or args.interval >= remaining:
                break
            threading.Event().wait(args.interval)
    except Exception:
        print('{"ok":false,"error":"runner_stopped","details_withheld":true}')
        return 1
    finally:
        app.close()
    return 0
