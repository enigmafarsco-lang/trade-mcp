# Read-only MT5 cost snapshot (experimental)

`mt5-connector/TradeMCPCostSnapshot.mq5` is a one-shot MQL5 script, not an
Expert Advisor and not an order transport. It uses official history getters to
export explicit profit, swap, commission and fee for every deal in a selected
terminal-time interval. It does not call OrderSend, modify positions, access a
network endpoint, invoke an LLM, or change any trade-mcp setting.

Compile the script in MetaEditor and run it as a Script in the target terminal,
with a deliberately bounded `From` and `MaxDeals`. Algo-trading permission is not
required by its code. Do not enable trading to run it. This repository's Linux
tests cannot compile MQL5 or verify broker field semantics; deployment is not
claimed. The default start is only an example; set it for the intended interval.

Output: terminal sandbox `MQL5/Files/trade-mcp-costs.tsv`. A temporary file is
published after all history getters succeed. Exceeding the maximum or a failed
getter aborts publication. An old output can remain: readers must reject stale
data. The file contains account login/server and financial data: keep it local,
restrict access using the host OS, never commit or post its contents. There are
no credentials in the format. Do not move snapshots into a public web directory.

`trade_mcp.cost_snapshot.read_cost_snapshot` requires account login, server and
currency obtained independently from the live account. It rejects mismatches,
future/stale timestamps (default 30 seconds), malformed/nonfinite amounts,
missing fields, truncated/excess rows and duplicate deal IDs. Account numbers
and 64-bit tickets are not converted through floating-point representations.
Snapshot epochs for history are terminal/server-time values, not necessarily UTC;
generated_at uses TimeGMT for freshness. Explicit zero differs from missing.

## Safety boundary

This is an inspection component, **not yet wired into the native trading path**.
The reader always returns `execution_eligible=False`. Existing incomplete-cost
entry gates are unchanged. It neither adopts positions nor resets the loss latch.

Even complete per-deal fields do not prove complete lifecycle net P/L: the interval
may omit an opening leg, charges may be separate balance deals, and a broker may
book fees later. The exporter intentionally preserves all deal types, including
position_id=0 account charges; these cannot be arbitrarily allocated to a position.
Floating P/L, opening-volume/closing-volume reconciliation, timestamp conversion,
account-switch-during-export detection and late-booked costs need independent
validation before execution integration. Do not treat this snapshot as a hard
monetary stop or activation authorization. No automated live-account use.

## Periodic read-only publication

`TradeMCPCostMonitor.mq5` is an optional timer EA that republishes the same strict
v1 snapshot every five seconds (configurable 2–20). It has no trading functions;
leave its Allow Algo Trading permission disabled. The name EA describes MT5's
lifecycle/timer mechanism, not permission to execute trades. It does not need
ticks to refresh. No program is installed, attached or activated automatically.

Both the script and monitor now include `TradeMCPCostExport.mqh`. Copy that header
beside the source when compiling: Scripts for the one-shot script, or Experts for
the timer monitor. Compile the monitor separately; previous successful compilation
of the standalone script does not verify this revision or the monitor.
Use a dedicated empty chart; do not replace an existing trading EA. Run **only one
snapshot writer per terminal**, never the script and monitor concurrently.

The exporter captures account identity at start and rechecks it and connection
state before publication, rejects exports taking over 25 seconds, checks writes,
and returns publication failure instead of reporting success. It retains the
previous output on abort, which the Python reader will eventually reject as stale.
These checks reduce, but cannot provide transactional isolation from, terminal
account/history changes. A switch away and back during export is not detectable.
All prior execution restrictions and lifecycle/late-charge limitations remain.
No freshness threshold is increased. Full history is rewritten on every interval;
keep the interval bounded and MaxDeals capped. This is not an incremental ledger.

Removing the monitor from its chart stops its timer. The remaining file is not
proof that the monitor is running. Host file permissions and a single trusted
writer remain mandatory. No Python execution adapter consumes these files yet.

## Read-only native cost correlation

`trade_mcp.cost_reconciliation.correlate_deal_costs(native_deals, snapshot)` now
supports diagnostic exact-ticket correlation. Obtain `snapshot` only by calling
`read_cost_snapshot` with independent live identity; never deserialize arbitrary
JSON and treat it as a validated snapshot. Recheck account identity after querying
native history as well; the correlation function is not an account verifier.

The join requires equal ticket, position ID, direction, entry classification and
price profit. Existing explicit native costs must agree. Nonfinite values,
duplicates, missing native matches, stale snapshots and unallocated non-trade
account charges fail closed. Terminal/server timestamps are not silently treated
as UTC. Snapshot-only record count is surfaced because interval coverage can
differ. Neither input is modified. The result still explicitly denies execution
eligibility and lifecycle completeness. No guardian wiring or entry-gate change
is included. This module is test-covered Python logic, not a claimed local runtime
success or a fix for late broker charges.

## Web diagnostic integration

Set `TRADE_MCP_COST_SNAPSHOT_PATH` in the existing local console environment to
the absolute path of the monitor's `trade-mcp-costs.tsv`. This is optional and
has no default auto-discovery. Preserve other settings; do not enable trading.
The web workspace now has a Persian cost-health panel and an explicit check
button, backed by `GET /api/cost-reconciliation` (no cloud or order calls).

The check verifies USD demo identity, reads a fresh account-matching snapshot,
queries at most one day of native history, correlates costs, and rechecks account
identity. Empty/possibly truncated native history is blocked. A shorter snapshot
interval may not cover that day and correctly fail. Returned data is restricted
to status/stage and aggregate counts/age; raw rows and exception bodies are never
sent to the browser. No automatic periodic history requests are added. The
existing console lock serializes this check with other console operations.

This wires diagnostics into the web app, **not** cost data into the execution
guardian. `execution_eligible` and `lifecycle_complete` remain false even on success.

### Diagnosing a failed correlation without exporting private records

The endpoint now includes a fixed `reason` code for recognized correlation
failures. It does not include ticket IDs, money values, account data or exceptions.
Examples:

- `unallocated_account_charge`: a non-buy/sell or zero-position record exists;
  allocation needs review. Do not narrow the interval solely to hide the charge.
- `native_ticket_outside_snapshot`: native history includes a ticket absent from
  the bounded snapshot. This is coverage mismatch, not evidence of zero fees.
- `native_ticket_duplicate`: native history repeats a ticket.
- `deal_identity_mismatch`: position/direction/entry differs between sources.
- `financial_source_mismatch`: explicit financial values disagree.
- `snapshot_stale`: history retrieval took long enough to invalidate freshness.

Only the first failure is returned; its presence does not prove other checks pass.
Unexpected failures remain generic. A fixed reason is diagnostic evidence, not
execution authorization. To test locally after deployment, call the same GET
endpoint once and return its sanitized JSON. No new monitor compile is needed for
this Python-only diagnostic revision. No cloud request or order is involved.

### Ticket coverage evidence and read ordering

The web check now queries native history **before** reading the latest published
snapshot, reducing the avoidable race in which new deals appeared after the file
was read. This does not guarantee a monitor refresh has happened, and never
suppresses unmatched tickets. No automatic retry or wait was added.

Correlation responses now include `coverage` counts: native/snapshot records,
ticket overlap, native-only and snapshot-only records, duplicate counts, and
unallocated snapshot records. Counts are computed locally and expose no IDs or
financial amounts. Identity is checked again before returning correlation failure
counts. These counts distinguish no overlap from partial coverage, but do not
prove why a ticket is missing or whether a broker has booked all charges. The
endpoint does not recommend dropping records or narrowing the period to force
success. Timezone assumptions and lifecycle checks remain unresolved separately.

Local OpenCode verification: deploy the current Python revision, restart only the
console with its existing environment, and call GET /api/cost-reconciliation once.
Return `reason` and `coverage` only (the endpoint already sanitizes these fields).
No monitor recompile, input change, cloud request or order is needed.

### Native identifier normalization correction

Coverage showing thousands of rows but only one unique native ticket can result
from missing identifiers being normalized to zero. It is not proof of a window
mismatch. The native adapter now accepts explicit top-level `ticket`,
`deal_ticket`, `deal_id`, `deal`, or `id` fields only. It rejects missing, zero,
boolean, floating-point, fractional, conflicting or duplicate identifiers with
fixed safe reason codes. It never borrows a nested order/position identifier.
Integer strings are parsed directly without float rounding (including 64-bit
identifiers). No records are deduplicated or assigned invented tickets.

The health endpoint surfaces the corresponding `native_deal_ticket_*` reason at
the history stage. If this terminal build uses an unrecognized shape, missing
identifiers now block earlier instead of silently producing 0 for every record.
Support for these aliases is adapter compatibility, not a claim about this
terminal's actual output until local verification. Deploy Python only, restart
the console with its unchanged environment and repeat the read-only check once.

## Version 2: explicit volumes and floating-position evidence

The shared MQL5 exporter now writes v2. **This revision requires recompiling the
monitor with the updated header.** Prior runtime compilation/validation covers
v1 only. The Python reader still accepts v1; deployment can update Python before
the monitor without inventing absent volume/position fields.

V2 retains the identity and four monetary fields, adding:
- Header: exact declared position count after deal count.
- Deal rows: explicit `DEAL_VOLUME` and UTF-8 hex encoded `DEAL_COMMENT`.
- Position rows: ticket, persistent identifier, direction, opening server time,
  current volume, `POSITION_PROFIT`, `POSITION_SWAP`, SL, TP, encoded comment.

The official getter boolean result is checked; missing values abort publication.
Comments are encoded to prevent tabs/newlines/quotes corrupting the TSV format;
hex is encoding, **not privacy protection**. Keep all snapshots local and private.
All declared rows must be present, positions come after deals, duplicate position
identifiers/tickets are rejected, and financial values must be finite. Trading
volumes must be positive. Unknown floating costs are not filled from native data.
`pnl_complete` means the two explicit floating fields were read, **not** that net
lifecycle P/L or all future closing charges are known. Historical deal swaps and
remaining-position swaps require lifecycle reconciliation before combined use.

Exporter checks total positions before/after the sample as well as history count,
account and connection. This still is not an atomic terminal transaction: changes
that preserve counts, same-account close/reopen, and later broker charges remain
possible. No live guardian execution adapter is enabled by this schema extension.
Both v1 and v2 correlation keep execution_eligible=false; v1 cannot supply the new
evidence and never gets synthesized volume or position P/L.

Local OpenCode handoff:

```text
Deploy current branch preserving all data and console environment.
Update TradeMCPCostExport.mqh beside TradeMCPCostMonitor.mq5 in active Experts.
Back up old source/header/binary locally. Stop only the read-only monitor while
replacing/compiling it; do not disturb any unrelated EA or Wine/MT5 session.
Compile in existing MetaEditor (request exact GUI F7 action only if unavoidable).
Reattach only after successful compilation, preserving bounded From/MaxDeals/5s,
single writer, and Allow Algo Trading disabled. Never use a live order to test.
Verify two fresh v2 snapshots with independent live identity and default max_age.
Report only version, completeness/counts, unique IDs and advancing generation.
If positions=0, floating live data is NOT verified; do not open a test position.
Do not publish comments, tickets, account identity, raw amounts or file contents.
Save and push only a sanitized measured-status report to the current branch.
No cloud/model requests, dependency installs or order activation.
```

### Guardian integration update

The opt-in native demo guardian can now consume v2 through `guard_costs.py` when
both the existing demo policy and its configured snapshot path are selected.
Earlier statements that no guardian adapter exists describe older revisions.
Snapshot/diagnostic responses still return execution_eligible=false: they are not
activation tokens. The complete current guard contract and runtime limitations
are in `docs/DEMO-CASH-POLICY.md` under Optional native v2 cost source. No default
activation, provider call, new order endpoint or broker action is added.
