# Pre-registration: XAUUSD sparse historical screen v1

> Post-execution notice (not part of the original pre-registration): v1 stopped
> on its first request and is closed. Do not rerun it, even if its workstation
> marker is absent. The command below is historical, not current authorization.
> See the [sanitized report](../reports/research/20261122T090500Z-xauusd-sparse-screen-v1.md).

This is a **new, separately bounded screen**, not the earlier assessment's zero-request
budget. Earlier report's statement that bounded multiple distinct requests necessarily
constitute retries was incorrect. The earlier observed density is not evidence that
other windows are complete or incomplete.

The earlier assessment fixed November 18–19 for a different full-day assessment;
that earlier experiment remains unchanged. This new sparse screen uses November
9–10 because the stated session date and workspace clock disagree, so November
18–19 cannot safely be asserted historical from the session date. This is not
a continuation or final test of the earlier assessment.

Freeze this document and the code in Git **before** any v1 history request. Do not
change windows/settings based on results. Maximum six distinct history tool calls,
one per window, each at most 2,000 requested rows, total at most 12,000 requested
rows. Stop on first provider, transport, schema, validation or replay error, zero
rows, out-of-window time labels, or response at/above 2,000 rows. No retries,
pagination, alternate endpoints, silent fallback or resubmission. Each completed
and failed attempt is recorded before the request; a fixed output directory
prevents rerunning after interruption. If it exists, stop and inspect it; do not
delete or rename it to repeat. Max applies to history calls; MCP initialize and
schema discovery also occur. Native MCP must be on loopback. No cloud calls.

All windows are **naive broker-wall-clock labels, not verified UTC**; only returned
timestamps within the requested labels are accepted. Endpoint inclusion may be
inclusive; do not infer tick completeness from a result below the cap.

| Partition | Start | End |
|---|---|---|
| evaluation | 2026-11-09T09:00:00 | 2026-11-09T09:05:00 |
| evaluation | 2026-11-09T13:00:00 | 2026-11-09T13:05:00 |
| evaluation | 2026-11-09T19:00:00 | 2026-11-09T19:05:00 |
| final | 2026-11-10T09:00:00 | 2026-11-10T09:05:00 |
| final | 2026-11-10T13:00:00 | 2026-11-10T13:05:00 |
| final | 2026-11-10T19:00:00 | 2026-11-10T19:05:00 |

Existing saved symbol-economics snapshot is reused: no account/symbol metadata
or tick requests beyond the above, and no new costs invented. Captured snapshot
was reported for XAUUSD_i, but its file does not itself independently bind the
symbol and cannot verify historical contract rules. Reject unsupported or missing
fields before connecting. Net PnL/commission/swap remain unknown/null.

Each window is an **independent hypothetical**: starts flat, with a fresh
simulated latch because there is no continuous tick history across gaps. This is
not the state of the real persistent latch and cannot demonstrate what a continuous
strategy would have done. Never reset or modify the actual latch. Within each
window use the existing TickMomentum and research simulator without optimization:
three consecutive changes, fixed 0.02 lots, next-quote simulated fills,
bid/ask spread, gross ±2 USD exit thresholds with observed overshoot, one
position and three-loss latch. Drop first/last potentially partial minutes.
Open positions at each end are marked, not forced closed. Never add per-window
results into a continuous account equity curve or imply a validated final test.

Report separate windows and partitions, including stops, missing windows, rows,
entries, wins/losses, model gross realized/open mark and latch status. Any
outcomes are highly sparse and model-dependent, **not representative multi-day
validation, a trading authorization, or a profitability claim**. Trading stays
OFF, cloud budget revoked, all real protection/journal state unchanged.

## Execution on the local MT5 machine by OpenCode

Fetch and fast-forward on `arena/f81b6b27-trade-mcp` to the commit containing
this pre-registration, then execute once from the repository root:

```bash
.venv/bin/python scripts/xauusd_sparse_screen.py
```

The fixed output is `logs/research-inputs/xauusd-sparse-screen-20261109-10-v1/`.
Raw responses and detailed files are owner-only and gitignored. No new orders,
installs, restarts, paid/model calls or modification to live settings. Do not
inspect or print raw provider payloads, quotes, credentials or account data.

Read only the sanitized summary; create a sanitized report in `reports/research`
listing every completed window and the exact fixed stop status if incomplete.
Preserve partial results and the attempt count, never retry. Commit/push only the
report on the current branch. After a push error, fetch/check origin before
claiming the report is absent. Return one Persian factual execution report.
