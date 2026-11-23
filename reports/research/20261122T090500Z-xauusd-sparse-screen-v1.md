# XAUUSD sparse historical screen v1 (pre-registered, bounded)

- Branch: `arena/f81b6b27-trade-mcp`
- HEAD at execution: `b044aa1`
- Command executed (once): `.venv/bin/python scripts/xauusd_sparse_screen.py`
- Trading OFF, cloud budget revoked. No orders/installs/restarts/model calls. No retry; fixed output dir not removed.

## Result
- Status: **stopped_history_provider_error**
- Symbol: `XAUUSD_i`, time basis: `broker_wall_clock_labels_utc_unverified`
- Request limit: 2000, max history calls: 6, **attempted history calls: 1**
- In-progress window at stop: **eval-1** (2026-11-09T09:00:00–09:05:00 per pre-registration)
- Completed windows: **none**

| Window | Partition | Status |
|---|---|---|
| eval-1 | evaluation | stopped (history provider error) |
| eval-2 | evaluation | not attempted |
| eval-3 | evaluation | not attempted |
| final-1 | final | not attempted |
| final-2 | final | not attempted |
| final-3 | final | not attempted |

## Flags/limits
- `complete_tick_coverage_verified=false`, `net_pnl=null`, `execution_authorized=false`, `profitability_proven=false`
- Window independence: each window starts flat with a fresh hypothetical latch; not continuous. **Not a continuous backtest or profitability proof.**

Raw/detailed local outputs remain owner-only under gitignored `logs/research-inputs/xauusd-sparse-screen-20261109-10-v1/`; not committed.
