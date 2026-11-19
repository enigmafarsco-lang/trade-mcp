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
