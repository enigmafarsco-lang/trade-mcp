"""Sanitized, read-only cost-source health check for the local web console."""
import time
from pathlib import Path

from .account_operations import account_operation_summary
from .bridge.base import BridgeError
from .cost_reconciliation import (
    CostCorrelationError,
    correlate_deal_costs,
    coverage_summary,
    identity_summary,
)
from .cost_snapshot import read_cost_snapshot
from .demo_guard import account_key


def cost_health(mt5, cfg):
    base = {'ok': False, 'execution_eligible': False, 'lifecycle_complete': False}
    path = getattr(cfg, 'cost_snapshot_path', '')
    if not path:
        return {**base, 'error': 'cost_snapshot_not_configured'}
    if cfg.backend != 'native':
        return {**base, 'error': 'native_backend_required'}
    stage = 'account'
    try:
        before = mt5.account_info(fresh=True)
        identity = account_key(before)  # USD demo only; never returned.
        stage = 'history'
        # Bounded UTC native query. The join does not assume server-time epochs
        # are UTC; any coverage mismatch fails or is explicitly counted.
        end = int(time.time())
        deals = mt5.deals(date_from=end-86400, date_to=end)
        if len(deals) >= 10000 or not deals:
            return {**base, 'error': 'history_empty_or_possibly_truncated'}
        stage = 'snapshot'
        snapshot = read_cost_snapshot(Path(path).expanduser(), login=str(before['login']),
                                      server=before['server'], currency=before['currency'])
        coverage = coverage_summary(deals, snapshot)
        identities = identity_summary(deals, snapshot)
        # Recheck even if correlation fails; do not attribute mixed-account
        # coverage counts to the original account.
        stage = 'account_recheck'
        if account_key(mt5.account_info(fresh=True)) != identity:
            return {**base, 'error': 'account_changed'}
        stage = 'correlation'
        operations = account_operation_summary(snapshot)
        try:
            joined = correlate_deal_costs(deals, snapshot)
        except CostCorrelationError as exc:
            return {**base, 'error': 'cost_check_failed', 'stage': stage,
                    'reason': exc.code, 'coverage': coverage, 'account_operations': operations,
                    'identity_comparison': identities}

        stage = 'account_recheck'
        if account_key(mt5.account_info(fresh=True)) != identity:
            return {**base, 'error': 'account_changed'}
        return {**base, 'ok': True, 'coverage': coverage, 'account_operations': operations,
                'identity_comparison': identities, 'matched_deals': joined['matched'],
                'snapshot_only_deals': joined['snapshot_only_deals'],
                'snapshot_age_seconds': round(time.time()-snapshot['generated_at'], 2),
                'note': 'diagnostic_only_no_order_authorization'}
    except CostCorrelationError as exc:
        return {**base, 'error': 'cost_check_failed', 'stage': stage, 'reason': exc.code}
    except BridgeError as exc:
        safe_codes = {'native_deal_ticket_missing', 'native_deal_ticket_invalid',
                      'native_deal_ticket_ambiguous', 'native_deal_ticket_duplicate',
                      'native_deal_side_ambiguous', 'native_deal_entry_ambiguous'}
        return {**base, 'error': 'cost_check_failed', 'stage': stage,
                'reason': exc.code if exc.code in safe_codes else 'native_history_unavailable'}
    except Exception:
        # Do not leak paths, identifiers, transport/provider bodies or raw rows.
        return {**base, 'error': 'cost_check_failed', 'stage': stage}
