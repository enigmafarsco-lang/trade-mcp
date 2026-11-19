"""Privacy-safe inventory of MT5 non-trade records, not an allocation policy.

A balance operation is not automatically a deposit or an ignorable fee. Names
refer only to MT5 DEAL_TYPE values; no economic purpose is inferred from comments.
"""
import math

from .cost_snapshot import CostSnapshotError

NONTRADE_TYPES = {
    2: 'balance', 3: 'credit', 4: 'charge', 5: 'correction', 6: 'bonus',
    7: 'commission', 8: 'commission_daily', 9: 'commission_monthly',
    10: 'commission_agent_daily', 11: 'commission_agent_monthly',
    12: 'interest', 13: 'buy_canceled', 14: 'sell_canceled',
    15: 'dividend', 16: 'dividend_franked', 17: 'tax',
}


def account_operation_summary(snapshot):
    """Count classified records only; never return comments, IDs, amounts or dates."""
    counts = dict.fromkeys((*NONTRADE_TYPES.values(), 'unknown', 'trade_without_position'), 0)
    fields = dict.fromkeys(('profit', 'swap', 'commission', 'fee'), 0)
    signs = dict.fromkeys(('positive_profit', 'negative_profit', 'zero_profit'), 0)
    total = 0
    for row in snapshot['deals']:
        kind = row['type']
        if kind in (0, 1) and int(row['position_id']) > 0:
            continue
        name = 'trade_without_position' if kind in (0, 1) else NONTRADE_TYPES.get(kind, 'unknown')
        counts[name] += 1
        total += 1
        for field in fields:
            value = row[field]
            if isinstance(value, bool) or value is None or not math.isfinite(float(value)):
                raise CostSnapshotError('Account operation fields incomplete')
            fields[field] += int(float(value) != 0)
        profit = float(row['profit'])
        signs['positive_profit' if profit > 0 else 'negative_profit' if profit < 0 else 'zero_profit'] += 1
    return {'unallocated_records': total, 'by_deal_type': counts,
            'nonzero_field_counts': fields, 'profit_sign_counts': signs,
            'allocation_verified': False}


class UnallocatedOperationsError(CostSnapshotError):
    """Blocked source with a fixed reason and count-only diagnostic payload."""

    def __init__(self, summary):
        self.code = 'unallocated_account_operations'
        self.summary = summary
        super().__init__('Unallocated account operations require allocation review')
