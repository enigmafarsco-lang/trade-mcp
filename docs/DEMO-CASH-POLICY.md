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

Each accepted policy entry has a durable journal reservation, random `tmcp:`
comment and dedicated magic 73104219. Only positions matched to those stored
reservations are owned. Historical agent trades, manual positions and another
robot's positions are NOT adopted or closed. Account-scoped state is keyed by
hashed login/server. Position identifiers (not just order/deal tickets) reconcile
fills, restart state and deal history. Broker-modified comments/unknown mappings
fail closed rather than guessing ownership. An unresolved reservation blocks
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
magic/magic_number input on the native order tool and complete monetary fields
in recent trading-deal samples. No history/sample proof means no policy entry.
This read-only capability test must precede any controlled execution acceptance.
It deliberately blocks the currently reported incomplete terminal data.

Native `trade_*` MCP tool calls no longer reconnect-and-resubmit after transport
failure. Their outcome is unknown and must be reconciled. Read-only tool calls
retain one reconnect attempt. Magic is no longer silently dropped: a nonzero
magic is sent only under the tool schema's explicitly documented parameter and
type; absent support fails before submission. Arbitrary extra arguments are not
used to guess undocumented broker functionality. This does not prove the server
honors the field; later live readback must still match nonce/magic/identifier.
