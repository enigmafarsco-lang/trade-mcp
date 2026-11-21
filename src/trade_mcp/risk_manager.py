"""Unit-aware sizing primitives; MT5 orders still require risk.validate_order."""
import math
from decimal import ROUND_FLOOR, Decimal

HARD_MAX_RISK_PERCENT = 2.0


def calculate_position_size(total_balance, risk_percentage, entry_price, stop_loss_price,
                            *, loss_per_price_unit=1.0, min_size=0.00000001,
                            max_size=1000000.0, step=0.00000001, max_cash_risk=None):
    """Return rounded-down units, not MT5 lots unless a broker multiplier is supplied.

    Risk percentage is percentage points (1 means 1%). Never rounds up to a
    minimum. MT5 callers must supply broker min/max/step and tick-value multiplier.
    """
    values = (total_balance, risk_percentage, entry_price, stop_loss_price,
              loss_per_price_unit, min_size, max_size, step)
    if any(isinstance(v, bool) or not math.isfinite(float(v)) or float(v) <= 0 for v in values):
        raise ValueError('Positive finite sizing inputs required')
    if float(risk_percentage) > HARD_MAX_RISK_PERCENT or float(min_size) > float(max_size):
        raise ValueError('Risk or size limit exceeded')
    def d(v):
        return Decimal(str(v))
    distance = abs(d(entry_price)-d(stop_loss_price))
    if not distance:
        raise ValueError('Stop distance must be nonzero')
    budget = d(total_balance)*d(risk_percentage)/100
    if max_cash_risk is not None:
        if isinstance(max_cash_risk, bool) or not math.isfinite(float(max_cash_risk)) or float(max_cash_risk) <= 0:
            raise ValueError('Invalid cash cap')
        budget = min(budget, d(max_cash_risk))
    unit_loss = distance*d(loss_per_price_unit)
    size = min(budget/unit_loss, d(max_size))
    size = (size/d(step)).to_integral_value(rounding=ROUND_FLOOR)*d(step)
    return float(size) if size >= d(min_size) else 0.0
