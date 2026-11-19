# Opt-in USD demo cash policy

**Default: OFF. No account, launcher, agent mode, trading switch or cloud budget
is enabled by deploying this code.** This is demo software, not a loss guarantee.

The requested policy is a planned maximum stop loss of USD 10 per position,
a target near USD 2, and no new entries after three consecutive net losing
completed positions. A USD 2 win versus USD 10 loss needs over 83.33% wins to
break even before costs. No profitability claim is made.

## Explicit configuration and scope

`TRADE_MCP_DEMO_CASH_POLICY=true` opts into the policy. It requires a fresh USD
account explicitly identified as demo, with login and server available. Equity
from that account becomes the sizing basis instead of the practice-budget cap.
Agent entries still obey existing max-risk-percent, max-lot, symbol allowlist,
spread, stop-distance, margin, proposal expiry and post-response freshness gates.
The dollar risk passed to sizing is capped at USD 10, NOT 1% of the whole account.
A lower existing agent/global percent cap remains lower. The original protective
SL is not tightened to manufacture a permissible minimum lot.

For agent proposals, the price-distance target is calculated for approximately
USD 2 gross P/L using broker tick-value arithmetic and normalized volume. It is
not a net-profit guarantee, exact broker currency-conversion calculation, or a
reason to ignore minimum stop distances. Broker SL and TP remain in the order.
Confirmation revalidates monetary loss on current price and refuses stale
pre-policy proposals or account changes. No entry occurs while ANY position or
pending order exists; no pyramiding, martingale, or manual-position netting.

To permit automated agent confirmation, a user must separately configure
`ALLOW_TRADING=true` AND choose guarded/auto agent mode. With this policy off,
web models still always require human confirmation. Advisory mode never executes.
Cloud request budgets are unchanged: no unlimited or auto-renewing cloud loop is
introduced. Workspace-managed child consoles intentionally still force trading
false. Do not misreport that enabling this policy alone starts a trading bot.

## Ownership and monitor

The console starts an independent two-second guardian thread only when opted in.
It has its own SQLite connection and does not wait for the console's LLM lock.
Native bridge requests are serialized and can themselves be slow. This is not
hard real-time and needs a running, connected console. The guardian only sends
closes when `ALLOW_TRADING=true` and the current account passes the USD demo check.

Each accepted policy entry has a durable journal reservation and random 25-character
`tmcp:` comment. Native orders use the documented comment parameter, not an
unsupported magic argument. Other supported transports retain dedicated magic
73104219 as an additional check. Ownership mode is stored per reservation;
existing magic-based reservations are not silently downgraded. Only positions
matched to those stored reservations are owned. Historical agent trades, manual positions and another
robot's positions are NOT adopted or closed. Account-scoped state is keyed by
hashed login/server. Position identifiers (not just order/deal tickets) reconcile
fills, restart state and deal history. For native comment ownership, the opening deal must also match the reserved
comment and opening time, and its position ID must agree with the position.
Broker-modified comments/unknown mappings fail closed rather than guessing ownership. An unresolved reservation blocks
new entries, including after an ambiguous open outcome.

For an owned open position, net-to-date is broker floating profit + current swap
+ all matched realized profit, swap, commission and fee fields. At <= -10 or >= +2
USD the guardian durably claims and submits one full close request. Exit costs
not yet posted and future slippage are unknowable. A broker TP may close near
USD 2 gross before the net threshold. SL can slip beyond USD 10 on gaps. Neither
threshold nor polling guarantees a particular fill or caps worst-case loss.

Close claims survive restarts. On timeout, rejection or partial close, there is
NO blind automatic re-submission; unresolved state blocks entry and requires
broker reconciliation/operator review. A missing protective SL, missing costs,
unknown/reversal deal entry, duplicate deal IDs or potentially truncated history
also raises a reconciliation error. Do not declare the monitor healthy in this
state; broker-side SL/TP remain the fallback. Native history now preserves a
separate fee, cost-completeness flag, deal-entry classification and position
identifier. If the terminal bridge cannot supply them, this feature is blocked,
not validated by an offline passing test.

## Three-loss latch

Only fully closed owned positions count, aggregating partial exits and all
entry/exit costs. A partial exit does not count as one completed trade. A net
zero or positive position breaks the consecutive-loss streak. Three net losses
set a persistent entry latch, which does NOT clear on restart, a later win, or
new cloud budget. Protective management of already-owned positions can continue.
The entry path and pre-model agent path enforce the latch. State is in the
existing journal's `demo_cash_guard` table; history is never deleted.

There is intentionally no automatic strategy mutation, risk escalation, timed
reset or reset endpoint. `review_required=true` means stop, examine losses,
propose/test a strategy revision separately, and obtain explicit authorization
before implementing any reviewed reset mechanism. Editing/deleting guard state
is not a supported recovery procedure.

## Verification without arming

```sh
.venv/bin/python -m pytest tests/test_demo_guard.py tests/test_agent_safety.py \
  tests/test_agents.py tests/test_execution.py tests/test_native_bridge.py -v
```

All tests use fixtures/mock brokers, never cloud generation or real orders.
Coverage includes -9/-10/-11 and +2 thresholds, costs, ownership, independent
accounts, partial closure, durable latch/restart, missing financial data,
ambiguous close outcomes/no duplicate submission, account-equity sizing with
cash cap, and nonce-tagged mock confirmation. Real native execution remains
unverified until a separate explicitly authorized demo acceptance test.

## Native compatibility hardening after read-only deployment checks

Real terminal history was observed to omit most swap/commission/fee fields.
Those observations do NOT establish that charges are zero. Native normalization
now preserves absent/invalid financial fields as null (explicit numeric zero is
known). A `costs_complete` flag requires finite numeric values, not merely key
presence. Missing floating position P/L is also unknown.

Before reserving an entry on the native backend, the policy requires a documented
string comment input accepting the full 25-character nonce on the native order
tool and complete monetary fields in recent trading-deal samples. No history/sample proof means no policy entry.
This read-only capability test must precede any controlled execution acceptance.
It deliberately blocks the currently reported incomplete terminal data.

Native `trade_*` MCP tool calls no longer reconnect-and-resubmit after transport
failure. Their outcome is unknown and must be reconciled. Read-only tool calls
retain one reconnect attempt. Magic is no longer silently dropped: a nonzero
magic is sent only under the tool schema's explicitly documented parameter and
type; absent support fails before submission. Arbitrary extra arguments are not
used to guess undocumented broker functionality. This does not prove the server
honors the field. Native cash-policy orders intentionally send no magic; later
readback must match nonce, opening history/time and position identifier. Merely
seeing a `tmcp:` comment without a journal reservation never establishes ownership.

## Optional native v2 cost source (source integration, not runtime acceptance)

When the explicitly enabled demo policy runs on `native` and
`TRADE_MCP_COST_SNAPSHOT_PATH` is configured, the guardian now uses the v2 adapter
in `guard_costs.py`. This changes that path from diagnostic-only to an available
**opt-in policy data source**. Defaults and trading permissions are unchanged;
configuring the path alone, or getting a passing diagnostic, does not start it.

The adapter requires USD demo identity, snapshot v2, at most 10 seconds of age,
and unchanged live account identity after reading. V1, stale/missing data and
unallocated account-level charges block; there is no silent native fallback.
Before entry, genuine shared native/snapshot samples must match the explicit cost
fields. The whole unrelated native day need not fit the snapshot: per-owned-
position ticket sets, however, must match exactly in BOTH directions. This is
scoping by persisted ownership, not dropping unmatched owned trades to pass.

For reserved positions the adapter checks deal direction/entry/price profit,
known costs, volumes, opening comments, unique journal owner, chronological volume
coverage, live ticket/identifier/direction/comment/volume, and remaining volume.
Unowned additions and reversals block. Floating profit/swap come from official
snapshot fields only after position identity and volume agree. Existing SL checks,
net thresholds, durable close claim, no replay, and persistent loss latch remain.
Account and snapshot age are checked again immediately before a requested close.
Read/identity failure after claiming a close leaves it unresolved, never blindly
resubmitted. Missing cost data does not trigger a guessed monetary exit; the
broker-side protective SL remains essential during outages.

No native or historical positions are adopted without this account's persisted
random reservation. Existing policy history must remain fully covered; shortening
the exporter window can block old owned reconciliation and must not reset it.
Opening time checks still use native normalized history; server timezone behavior
must be validated. A new entry is not an instruction to widen SL or risk caps.

### What this does not guarantee

The 5-second exporter plus serialized native calls is not hard real-time; the
10-second freshness limit is not a latency or loss bound. Open exits use currently
booked costs and floating P/L, not unknown future closing commission/slippage.
Later broker charges may revise booked results; separate unallocatable charges
block rather than being assigned arbitrarily. MT5 snapshots are not atomic
transactions and some changes can preserve row counts. Net -10/+2 is therefore
an approximate monitored trigger, not a guaranteed broker cash stop/target.

Offline fake-broker tests verify source integration, refusal of mismatched/partial
owned evidence, no adoption, close once at known net threshold, dry-run no orders,
and the v2 three-loss latch across journal reopen. They are not live execution
acceptance. User-local v2 validation so far had zero open positions, so live
floating P/L and actual close submission remain unverified. Do not mark them pass
or automatically activate based on the existence of this implementation.
