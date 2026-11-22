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


def replay_cash_scenario(rows, info, *, consecutive_changes=3, max_spread_cost_usd=None,
                         manage_final_boundary=False):
    """Compare offline hypotheses only; defaults reproduce the original scenario.

    The optional boundary mode manages an existing position using the final
    partial minute, but does not generate signals or enter new positions there.
    It remains a hypothetical gross model and never changes live trading.
    """
    if type(manage_final_boundary) is not bool:
        raise ValueError('Final-boundary mode must be boolean')
    if type(consecutive_changes) is not int or consecutive_changes not in (2, 3):
        raise ValueError('Offline scenario requires two or three consecutive quote changes')
    if (max_spread_cost_usd is not None
            and (isinstance(max_spread_cost_usd, bool)
                 or not isinstance(max_spread_cost_usd, (int, float))
                 or not math.isfinite(max_spread_cost_usd) or max_spread_cost_usd < 0)):
        raise ValueError('Invalid offline spread cap')
    dollars_per_price = economics(info)
    signal_report = replay_signals(rows, consecutive=consecutive_changes)
    first = datetime.fromisoformat(rows[0]['time_ms']).replace(second=0, microsecond=0)
    last = datetime.fromisoformat(rows[-1]['time_ms']).replace(second=0, microsecond=0)
    strategy = TickMomentum(consecutive=consecutive_changes)
    opened = minute = position = pending = None
    streak = 0
    latched = False
    closes, counts = [], Counter()
    equity = peak = drawdown = 0.0
    unrealized = None
    boundary_ticks = boundary_position_ticks = 0

    def pnl(pos, bid, ask):
        # Buy pays ask, liquidates at bid. Sell receives bid, covers at ask.
        mark = bid if pos['side'] == 'buy' else ask
        return (mark-pos['entry'])*dollars_per_price*(1 if pos['side'] == 'buy' else -1)

    def spread_allowed(bid, ask):
        return (max_spread_cost_usd is None
                or (ask-bid)*dollars_per_price <= max_spread_cost_usd+1e-12)

    def mark_position(pos, bid, ask):
        nonlocal equity, peak, drawdown, streak, latched
        mark = pnl(pos, bid, ask)
        equity_mark = equity + mark
        peak = max(peak, equity_mark)
        drawdown = max(drawdown, peak - equity_mark)
        if mark >= 2 or mark <= -2:
            closes.append(mark)  # Never clip overshoot to +/-2.
            kind = 'cash_profit_exits' if mark >= 2 else 'cash_loss_exits'
            counts[kind] += 1
            equity += mark
            streak = streak + 1 if mark < 0 else 0
            latched = latched or streak >= 3
            return None, None, kind
        return pos, mark, None

    for row in rows:
        dt = datetime.fromisoformat(row['time_ms'])
        start = dt.replace(second=0, microsecond=0)
        if start == first:
            continue
        if start == last:
            if manage_final_boundary:
                boundary_ticks += 1
                # An entry signal is only valid in its own interior minute.
                if pending:
                    counts['expired_pending_entries'] += 1
                    counts['final_boundary_expired_pending'] += 1
                    pending = None
                # The partial minute cannot safely create new signals, but its
                # bid/ask can still mark or exit an already-open position.
                if position:
                    boundary_position_ticks += 1
                    bid, ask = float(row['bid']), float(row['ask'])
                    position, unrealized, exit_kind = mark_position(position, bid, ask)
                    if exit_kind:
                        counts['final_boundary_' + exit_kind] += 1
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
                if spread_allowed(bid, ask):
                    position = {'side': pending['side'], 'entry': ask if pending['side'] == 'buy' else bid}
                    counts['simulated_entries'] += 1
                else:
                    counts['pending_entries_blocked_by_spread'] += 1
            else:
                counts['expired_pending_entries'] += 1
            pending = None
        if position:
            position, unrealized, _ = mark_position(position, bid, ask)
            # No same-quote re-entry, including the quote which closes a position.
            continue
        if signal['action'] in ('buy', 'sell'):
            if latched:
                counts['signals_blocked_by_simulated_loss_latch'] += 1
            elif not spread_allowed(bid, ask):
                counts['signals_blocked_by_spread'] += 1
            else:
                pending = {'side': signal['action'], 'minute': start, 'time': t}
    report = {'schema': 'xauusd-next-quote-cash-scenario/v1',
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
    if consecutive_changes != 3 or max_spread_cost_usd is not None:
        report['research_variant'] = {'consecutive_changes': consecutive_changes,
                                      'max_spread_cost_usd': max_spread_cost_usd,
                                      'spread_checked_at_signal_and_next_quote': max_spread_cost_usd is not None}
    if manage_final_boundary:
        report['final_boundary_management'] = {
            'scope': 'existing_hypothetical_position_only_no_new_signals_or_entries',
            'boundary_quotes_seen': boundary_ticks,
            'position_marked_quotes': boundary_position_ticks,
            'cash_profit_exits': counts.get('final_boundary_cash_profit_exits', 0),
            'cash_loss_exits': counts.get('final_boundary_cash_loss_exits', 0),
            'pending_entries_expired': counts.get('final_boundary_expired_pending', 0),
            'mark_basis': 'last_observed_quote_if_still_open',
            'complete_tick_coverage_verified': False,
        }
    return report
