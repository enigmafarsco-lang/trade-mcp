# XAUUSD corrected bounded historical tick sample (executed once)

- Branch: `arena/f81b6b27-trade-mcp`
- HEAD at execution: `afcd4ea`
- Command executed (exact): `.venv/bin/python scripts/export_xauusd_tick_sample.py "logs/research-inputs/xauusd-ticks-corrected-20261122T073154Z" --symbol XAUUSD_i --from 2026-11-20T19:23:00 --to 2026-11-20T19:28:00 --time-basis broker_wall_clock --limit 10000`
- Execution count: **1** (no retry/fallback). Trading OFF, cloud budget revoked, no installs/restarts/orders.

## Actual result
- Status: **response_received** (`isError=false`, `ok=true`, `symbol=XAUUSD_i`, `period=tick`).
- Returned rows: **1015**. Valid two-sided rows (bid & ask present, positive, ask>=bid): **1015**. Truncated at limit: **no** (`1015 < 10000`).
- Time field: `time_ms` present in **1015/1015** rows, type **string ISO-like with no TZ/offset/Z**. Ordering: nondecreasing=true, strictly increasing=true, unique=1015, zero-diff=0, diff min=0.036s, diff max=1.321s.
- Request time basis: **broker_wall_clock** (labels as requested; not verified UTC instants). Response time semantics verified: **false**. Clock semantics: **unknown (not UTC)**.
- Complete tick coverage verified: **false**. Backtest executed: **false**. Execution authorized: **false**.

## Raw data
- Raw response kept owner-only under gitignored `logs/research-inputs/xauusd-ticks-corrected-20261122T073154Z/` (0600). Not committed, not printed.
