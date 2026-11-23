# Execute offline replay on the EXISTING 1015-row sample; no new data request

**Historical instruction, already completed; do not rerun this job.** The
command below is retained as a record, not a new authorization.

Stay on arena/f81b6b27-trade-mcp in the existing local checkout. Fetch and
fast-forward only. Commit33dd179 is already on origin; no push repair needed.

Run exactly once with the archived raw response already obtained:

```bash
.venv/bin/python scripts/replay_xauusd_tick_sample.py \
  logs/research-inputs/xauusd-ticks-corrected-20261122T073154Z/raw-response.json \
  "logs/research-inputs/xauusd-signal-replay-$(date -u +%Y%m%dT%H%M%SZ).json"
```

If the raw archive filename differs, identify the actual saved response in that
same directory locally; do not download/refetch anything or fabricate input.
If parser/schema fails, stop and report only the structural mismatch (field names
and types), not raw prices/payloads. No guessing timestamp units or timezone.

This reuses actual TickMomentum.observe with consecutive=3, on a relative broker
clock axis. First and last minute are excluded. Each interior minute uses its
first returned bid as an approximate open; true first-tick completeness is NOT
proved. This is signal counting, NOT cash-exit/position/profitability backtesting.
Never convert the internal relative time_utc argument into a UTC claim or live
signal. No orders, fills, lot sizing, fees, PnL or execution guards are simulated.

Review returned counts, rows, waits, warnings; preserve the raw archive. Write
only a sanitized factual result under reports/research. Commit/push intended
sanitized files on this branch, verify origin after any misleading push error.
Trading OFF and budget revoked; no network/MCP requests, installs, Wine/MT5
restarts, cloud calls, paid services or protection/journal changes.
Return the actual signal counts and limitations, not a plan or profit claim.
