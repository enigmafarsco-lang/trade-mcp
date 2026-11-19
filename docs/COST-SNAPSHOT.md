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
