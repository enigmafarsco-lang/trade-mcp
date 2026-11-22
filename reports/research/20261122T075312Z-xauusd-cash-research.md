# XAUUSD cash-scenario research job (executed once)

- Branch: `arena/f81b6b27-trade-mcp`
- HEAD at execution: `16cfd85`
- Command executed (exact): `.venv/bin/python scripts/run_xauusd_cash_research.py logs/research-inputs/xauusd-ticks-corrected-20261122T073154Z/raw-response.json "logs/research-inputs/xauusd-cash-research-20261122T075312Z" --symbol XAUUSD_i`
- Execution count: **1**. New tick request: **none**. Metadata tool calls: **1** (read-only symbol metadata). Trading OFF, cloud budget revoked.

## Actual result
- Status: **blocked_invalid_input_metadata_or_unsupported_profit_model**.
- `missing_economic_fields=[]` (required fields present in captured economics: `trade_contract_size, currency_profit, trade_calc_mode, volume_min, volume_max, volume_step`).
- Reported `trade_calc_mode`: **"cfd leverage"** (unsupported by job; no default substituted).
- Gross cash-exit scenario produced: **no**. Net PnL: **null** (commission/swap not assumed zero).

## Blocker (verified)
- Profit calculation mode reported by symbol metadata (`cfd leverage`) is unsupported by the offline gross cash-exit research job; job stopped with factual summary. No economics invented, no additional calls made.

Raw/symbol-economics files remain owner-only under gitignored `logs/research-inputs/xauusd-cash-research-20261122T075312Z/`; not committed.
