# Execute corrected local tick-history sample once

**Historical instruction, already completed; do not repeat this history
request or treat the command below as a new authorization.** See the
[final-boundary research](XAUUSD-FINAL-BOUNDARY-REPLAY.md) for later offline results.

Use the existing checkout /media/joshua/SATA500/Trade/trade-mcp and branch
arena/f81b6b27-trade-mcp. Fetch and fast-forward only. The earlier report commit
275b2d6 is already on origin; do not spend further time repairing its push.

The prior malformed call yielded no data. This is a newly corrected, explicitly
bounded invocation, not an automatic retry loop. Execute ONCE:

```bash
.venv/bin/python scripts/export_xauusd_tick_sample.py \
  "logs/research-inputs/xauusd-ticks-corrected-$(date -u +%Y%m%dT%H%M%SZ)" \
  --symbol XAUUSD_i \
  --from 2026-11-20T19:23:00 --to 2026-11-20T19:28:00 \
  --time-basis broker_wall_clock --limit 10000
```

These bounds are explicitly broker wall-clock labels, not verified UTC instants.
Do not append Z, change timeframe, symbol, bounds, limits or time basis to hunt
for results. No retry/fallback on any error or empty response.

The script checks the live input schema before calling the one exact read tool.
Tool name is sent separately from its four arguments. No account, orders or
live quote sampling is performed. Raw response and safe summary are owner-only
under ignored logs. Keep raw-response.json out of Git/chat.

Inspect the safe summary and, locally only, raw response. If a response was
received, distinguish isError, empty data, valid bid/ask rows, actual timestamp
representation, truncation and interval coverage. Clock semantics remain
unverified unless response has explicit documented evidence; do not infer UTC
from a numeric timestamp or workstation timezone. Do not infer complete tick
coverage simply from valid bid/ask prices. Do not claim profitability/backtest.

Keep trading OFF, cloud budget revoked, Wine/MT5/guardian/settings unchanged.
No installs, restarts, orders, paid/model/Binance calls, duplicate monitor writer
or fabricated ticks. Do not clear journals, latches or unresolved intents.

Commit and push only a sanitized factual result under reports/research, on this
same branch. On push error, check origin before reporting authentication trouble.
Return one Persian completion report with actual status, counts, timestamp
verification or exact blocker and sanitized report commit; no planning handoff.
