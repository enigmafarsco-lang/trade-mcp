"""Research-only next-quote cash-exit scenario. Not broker execution validation."""
import math
from collections import Counter
from datetime import datetime

from .tick_momentum import TickMomentum
from .tick_replay import replay_signals

LINEAR_MODES = {'0', '3', '5', '6', '32', 'forex', 'cfd', 'forex_no_leverage',
                'cfdleverage', 'cfd_leverage', 'exch_stocks'}


def economics(info):
    """Require actual reported fields; no point/contract/volume/currency defaults."""
    required = ('trade_contract_size', 'volume_min', 'volume_max', 'volume_step',
                'currency_profit', 'trade_calc_mode')
    missing = [k for k in required if info.get(k) is None or info.get(k) == '']
    if missing:
        raise ValueError('Missing actual symbol fields: '+','.join(missing))
    if str(info['currency_profit']).upper() != 'USD':
        raise ValueError('USD profit currency required; no invented currency conversion')
    raw_mode = info['trade_calc_mode']
    if isinstance(raw_mode, bool):
        raise ValueError('Invalid profit calculation mode')
    if isinstance(raw_mode, float) and math.isfinite(raw_mode) and raw_mode.is_integer():
        raw_mode = int(raw_mode)
    mode = '_'.join(str(raw_mode).strip().lower().split()).removeprefix('symbol_calc_mode_')
    if mode not in LINEAR_MODES:
        raise ValueError('Unsupported profit calculation mode; no invented PnL formula')
    values = {}
    for key in required[:4]:
        raw = info[key]
        if isinstance(raw, bool):
            raise ValueError('Invalid symbol economics')
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid symbol economics')
        values[key] = value
    volume = .02  # Fixed research scenario, not account-risk-sized order authority.
    if (not values['volume_min'] <= volume <= values['volume_max']
            or not math.isclose(volume/values['volume_step'], round(volume/values['volume_step']),
                                abs_tol=1e-8)):
        raise ValueError('Fixed 0.02 lot scenario not supported by actual volume limits')
    return values['trade_contract_size']*volume


def replay_cash_scenario(rows, info):
    dollars_per_price = economics(info)
    signal_report = replay_signals(rows)  # Strict original sample validation.
    first = datetime.fromisoformat(rows[0]['time_ms']).replace(second=0, microsecond=0)
    last = datetime.fromisoformat(rows[-1]['time_ms']).replace(second=0, microsecond=0)
    strategy = TickMomentum(consecutive=3)
    opened = minute = position = pending = None
    streak = 0
    latched = False
    closes, counts = [], Counter()
    equity = peak = drawdown = 0.0
    unrealized = None

    def pnl(pos, bid, ask):
        # Buy pays ask, liquidates at bid. Sell receives bid, covers at ask.
        mark = bid if pos['side'] == 'buy' else ask
        return (mark-pos['entry'])*dollars_per_price*(1 if pos['side'] == 'buy' else -1)

    for row in rows:
        dt = datetime.fromisoformat(row['time_ms'])
        start = dt.replace(second=0, microsecond=0)
        if start in (first, last):
            continue
        bid, ask = float(row['bid']), float(row['ask'])
        if start != minute:
            minute, opened = start, bid
        t = (dt-first).total_seconds()
        bar = (start-first).total_seconds()
        signal = strategy.observe({'time_msc': t*1000, 'bid': bid, 'ask': ask},
                                  {'time': bar, 'time_utc': bar, 'open': opened}, now=t)
        # Fill only on a subsequent observed quote, at that quote's opposite side.
        if pending:
            if start == pending['minute'] and 0 < t-pending['time'] <= 2 and not latched:
                position = {'side': pending['side'], 'entry': ask if pending['side'] == 'buy' else bid}
                counts['simulated_entries'] += 1
            else:
                counts['expired_pending_entries'] += 1
            pending = None
        if position:
            unrealized = pnl(position, bid, ask)
            equity_mark = equity+unrealized
            peak = max(peak, equity_mark)
            drawdown = max(drawdown, peak-equity_mark)
            if unrealized >= 2 or unrealized <= -2:
                closes.append(unrealized)  # Never clip overshoot to +/-2.
                counts['cash_profit_exits' if unrealized >= 2 else 'cash_loss_exits'] += 1
                equity += unrealized
                streak = streak+1 if unrealized < 0 else 0
                latched = latched or streak >= 3
                position, unrealized = None, None
            # No same-quote re-entry, including the quote which closes a position.
            continue
        if signal['action'] in ('buy', 'sell'):
            if latched:
                counts['signals_blocked_by_simulated_loss_latch'] += 1
            else:
                pending = {'side': signal['action'], 'minute': start, 'time': t}
    return {'schema': 'xauusd-next-quote-cash-scenario/v1',
            'signal_replay': signal_report, 'model': 'linear_USD_profit_contract_size_times_0.02_lot',
            'thresholds_gross_usd': {'loss': -2, 'profit': 2}, 'counts': dict(counts),
            'closed_scenarios': len(closes), 'gross_realized_usd_model': sum(closes),
            'gross_open_mark_usd_model': unrealized,
            'gross_total_mark_usd_model': sum(closes)+(unrealized if unrealized is not None else 0),
            'gross_mark_drawdown_usd_model': drawdown,
            'closed_wins': sum(p > 0 for p in closes), 'closed_losses': sum(p < 0 for p in closes),
            'position_open_at_sample_end': position is not None,
            'pending_entry_at_sample_end': pending is not None,
            'simulated_three_consecutive_loss_latch': latched,
            'commission': None, 'swap': None, 'net_pnl': None,
            'spread_included': True, 'additional_slippage_modeled': False,
            'broker_profit_calculation_verified': False, 'historical_contract_rules_verified': False,
            'account_risk_and_execution_guards_replayed': False,
            'validated_for_trading': False, 'execution_authorized': False,
            'profitability_proven': False,
            'limitations': ['Five-minute source / three interior minutes; not representative',
                            'Next observed quote fills assumed, not demonstrated executable prices',
                            'Uses current reported linear contract metadata, not historical rule verification',
                            'Fees unknown: net PnL unavailable; gross cash thresholds only',
                            'No account equity/margin or 0.5 percent risk sizing modeled',
                            'No forced close at sample end; mark is last replayed quote',
                            'UTC/feed completeness and true minute open remain unverified']}
