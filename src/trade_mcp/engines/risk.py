"""Bounded sizing facade using the existing broker-aware risk implementation."""
import math

from ..config import sizing_balance
from ..demo_guard import LOSS_USD, account_key
from ..risk import lots_for_risk


def size_position(mt5, cfg, symbol, entry, stop, risk_percent):
    if any(isinstance(v, bool) or not math.isfinite(float(v)) or float(v) <= 0
           for v in (entry, stop, risk_percent)):
        raise ValueError('Positive finite entry, stop and risk percent required')
    account = mt5.account_info(fresh=True)
    equity = sizing_balance(account, cfg)
    if equity <= 0:
        return dict(ok=False, error='sizing_balance_unavailable')
    pct = min(float(risk_percent), cfg.max_risk_percent)
    if cfg.demo_cash_policy:
        account_key(account)
        pct = min(pct, LOSS_USD/equity*100)
    resolved = mt5.resolve_symbol(symbol or cfg.default_symbol)
    info = mt5.symbol_info(resolved)
    result = lots_for_risk(equity, pct, float(entry), float(stop), info, max_lot=cfg.max_lot)
    return dict(ok=bool(result['ok']), symbol=resolved, applied_risk_percent=pct,
                sizing=result, execution_authorized=False,
                note='Planned price loss only; commissions, gaps and slippage may add loss')
