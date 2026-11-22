# XAUUSD offline gross cash-exit scenario (existing ticks + cached economics)

- Branch: `arena/f81b6b27-trade-mcp`
- HEAD at execution: `043a125`
- Command executed (exact): `.venv/bin/python scripts/run_xauusd_cash_research.py logs/research-inputs/xauusd-ticks-corrected-20261122T073154Z/raw-response.json "logs/research-inputs/xauusd-cash-offline-20261122T075608Z" --symbol XAUUSD_i --economics logs/research-inputs/xauusd-cash-research-20261122T075312Z/symbol-economics.json`
- Execution count: **1**. New MT5/network calls: **0** (`metadata_tool_calls=0`). Trading OFF, cloud budget revoked.

## Actual gross-model outcomes (not profitability proof)
- Status: **gross_scenario_executed_net_costs_unknown**. Economics source: existing local reported symbol snapshot; symbol binding independently verified: **false**. Missing economic fields: **[]**.
- Model: `linear_USD_profit_contract_size_times_0.02_lot`, thresholds gross USD `loss=-2, profit=2`.
- Counts: `simulated_entries=5`, `cash_loss_exits=4`, `cash_profit_exits=1`, `signals_blocked_by_simulated_loss_latch=13`.
- Closed: `closed_scenarios=5` (`wins=1, losses=4`).
- Gross: `gross_realized_usd_model=-6.239999999999782`, `gross_open_mark_usd_model=null`, `gross_total_mark_usd_model=-6.239999999999782`, `gross_mark_drawdown_usd_model=7.359999999999673`.
- End state: `position_open_at_sample_end=false`, `pending_entry_at_sample_end=false`, `simulated_three_consecutive_loss_latch=true`.
- Costs/net: `commission=null`, `swap=null`, `net_pnl=null` (costs unknown, not zero). `spread_included=true`, `additional_slippage_modeled=false`.
- Verification flags: `broker_profit_calculation_verified=false`, `historical_contract_rules_verified=false`, `account_risk_and_execution_guards_replayed=false`, `validated_for_trading=false`, `execution_authorized=false`, `profitability_proven=false`.

Raw/full local outputs remain owner-only under gitignored `logs/research-inputs/xauusd-cash-offline-20261122T075608Z/`; not committed.
