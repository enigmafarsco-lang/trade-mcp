# XAUUSD offline signal replay (existing 1015-row sample)

- Branch: `arena/f81b6b27-trade-mcp`
- HEAD at execution: `5f5b1da`
- Command executed (exact): `.venv/bin/python scripts/replay_xauusd_tick_sample.py logs/research-inputs/xauusd-ticks-corrected-20261122T073154Z/raw-response.json "logs/research-inputs/xauusd-signal-replay-20261122T073857Z.json"`
- Execution count: **1**. New MT5/network request: **none**. Trading OFF, cloud budget revoked.

## Actual results (signal replay, not profitability test)
- Source: `existing_TickMomentum.observe`, `consecutive_changes=3`.
- Input rows: **1015**. Replayed rows: **614**. Boundary rows discarded: **401**. Interior minutes seen: **3**.
- Signal counts: **sell=17, buy=16**.
- Wait reason counts: **warming_up=3, insufficient_aligned_ticks=578**.
- Time basis: **relative_broker_wall_clock_not_utc**. Candle open source: **first_returned_bid_in_each_interior_broker_minute**.
- Verified flags: `complete_tick_coverage_verified=false`, `utc_verified=false`, `signal_replay_executed=true`, `profitability_backtest_executed=false`, `execution_guards_replayed=false`, `execution_authorized=false`, `pnl=null`, `fees=null`.

## Limitations
- Tiny historical sample, not representative trading evidence.
- Missing ticks cannot be excluded; sampled minute open may differ from true open.
- No fills, positions, sizing, broker guards or cash exits simulated.
- Internal `time_utc` argument holds relative offsets only, never UTC instants.

Raw archive preserved locally (gitignored owner-only); sanitized report only committed.
