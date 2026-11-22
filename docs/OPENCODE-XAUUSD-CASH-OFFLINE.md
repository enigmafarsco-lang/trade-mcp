# Execute corrected cash scenario using existing files only, ZERO network/MCP calls

**Historical instruction, already completed; do not repeat it.** The command
below is retained as a record, not a new authorization. Its published result
used the legacy horizon; see [final-boundary research](XAUUSD-FINAL-BOUNDARY-REPLAY.md)
for the corrected comparison and current default.

Keep current branch arena/f81b6b27-trade-mcp. Fetch/fast-forward only.
The previous sanitized result b924bd2 is already on origin; do not repair its push.
The reported mode "cfd leverage" is now normalized to the existing recognized
"cfd_leverage" model; this changes display-label parsing, not the PnL formula.

Run once locally:

```bash
.venv/bin/python scripts/run_xauusd_cash_research.py \
  logs/research-inputs/xauusd-ticks-corrected-20261122T073154Z/raw-response.json \
  "logs/research-inputs/xauusd-cash-offline-$(date -u +%Y%m%dT%H%M%SZ)" \
  --symbol XAUUSD_i \
  --economics logs/research-inputs/xauusd-cash-research-20261122T075312Z/symbol-economics.json
```

Do not edit/recreate either raw input. Check they are the actual original files
from the stated XAUUSD_i captures; the old economics JSON lacks a symbol field,
so the script explicitly does not claim independent symbol binding verification.
Use original archive provenance. No defaults, renamed currency or hand-adjusted
contract size. If a file is absent or invalid, stop with the actual blocker.
Do not execute the older metadata-fetching command; --economics is mandatory here.

Inspect the actual returned scenario, save only sanitized findings in
reports/research and commit/push current branch. Preserve owner-only raw and
full local outputs. Check origin after any push error before reporting auth failure.
Return actual simulated entries/closed outcomes/gross PnL/open mark/latch and
net=NULL, or exact blocker. No plans, new input requests or profitability claims.

Keep trading OFF, budget revoked and protections unchanged. This job must make
ZERO MT5/network calls and perform no installs/restarts/orders/cloud/model calls.
The summary metadata_tool_calls must equal0. Costs stay unknown, not0. Results
remain next-quote gross model scenarios on a tiny sample, not broker-validated
fills/PnL, account-risk-sizing validation or evidence of profitability.
