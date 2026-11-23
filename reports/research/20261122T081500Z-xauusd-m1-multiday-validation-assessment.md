# XAUUSD M1 momentum — bounded multi-day validation assessment

- Branch: `arena/f81b6b27-trade-mcp`, HEAD: `043a125`
- Trading OFF, cloud budget revoked. No orders, installs, restarts, paid/model/Binance calls, live-setting changes. No tick data requested.

## Fixed strategy (preserved, not optimized)
- `consecutive_changes=3`, `0.02` lot research scenario, gross cash exits `-2/+2 USD`, one position, three-loss latch. No reset/bypass.

## Inspected evidence
- Archived tick sample: `logs/research-inputs/xauusd-ticks-corrected-20261122T073154Z/raw-response.json` — **1015 rows / 5 minutes**, 3 interior minutes in replay, bid/ask two-sided present, `time_ms` ISO-like string no TZ (**UTC not verified**). Not sufficient for meaningful multi-day test.
- Native MCP read-only tools (schema verified): `get_chart_ticks_history` (required datetime_from/datetime_to/symbol, optional limit, schema default 100000, read-only); `get_chart_history` M1 bars (insufficient for tick-momentum Bid/Ask). 
- Read-only metadata call made: `get_marketwatch_symbols` for `XAUUSD_i` (1 call). Cost-related fields present: `swap_long/swap_short/swap_mode/tick_value/tick_size/contract_size/currency_profit`; **commission field not present**. Swap application incomplete for net PnL.

## Pre-defined windows/limits (recorded before any data request)
- Eval window (fixed): `2026-11-18T00:00:00`–`2026-11-19T00:00:00` broker wall-clock labels. Final test (fixed): `2026-11-19T00:00:00`–`2026-11-20T00:00:00` broker wall-clock labels. Timestamp semantics: **broker wall-clock labels, unknown/not UTC**.
- Explicit limits: `per_request_max_rows_limit=10000`, `max_requests_allowed=0` for this assessment (no new tick request made). 
- **Bounds enforceable for meaningful multi-day tick validation: NO.** Observed density ~203 rows/min implies ~292k rows/day, exceeding per-request limit; covering 1+ days requires many bounded requests and cannot be enforced as a meaningful multi-day validation within explicit bounded request/row limits without retries/fallback.

## Existing offline scenario (valid small-sample input)
- Already executed/reported: `reports/research/20261122T075608Z-xauusd-cash-offline.json` (5-min sample, 3 interior minutes; gross model only, `net_pnl=null`, not profitability proof). Not rerun.

## Exact verified limitation
Insufficient historical Bid/Ask tick data for a meaningful multi-day test under enforceable bounds. Actual commission unavailable and swap application incomplete; unknown costs remain **NULL** and gross results cannot be labeled net profitability. No tick data requested; no protections bypassed.
