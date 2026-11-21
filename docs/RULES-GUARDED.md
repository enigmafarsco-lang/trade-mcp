# Explicit one-shot rules / guarded action

The console supports `POST /api/agents/{id}` with JSON:

```json
{"action":"run_rules_guarded"}
```

This explicitly selects the existing independent internal scenario/rules decision
branch. It is NOT an imported strategy, AI decision, hourly volatility forecast,
or fallback after cloud failure. Normal `run`, scheduler and web behavior remain
unchanged and fail closed on cloud errors. No provider client is configured for
this action, no cloud budget is activated or spent, and saved agent settings are
not changed. Exactly one symbol is required. Guarded mode is forced for this call.
The same AgentEngine risk/proposal/confirmation, ownership and cash-policy gates
apply. A valid request can create a real order when existing execution policy
permits: this is not a read-only endpoint. WAIT is a valid result.

This is one invocation, NOT a durable one-entry-per-session lock. Do not schedule
or repeat it during a one-entry acceptance session. No endpoint automatically
checks that an independent guardian thread is healthy: the authorized local
operator must verify that before enabling trading. Existing gates do not imply
successful broker execution or completed live guardian acceptance.

## Authorized local DEMO acceptance handoff

1. Fast-forward current branch, preserve local changes/secrets/data. Run tests.
2. Independently verify demo/USD, zero positions/orders/unresolved intents,
   no loss latch, fresh v2 snapshot and account/cost reconciliation.
3. Inspect the chosen saved agent: exactly one allowed symbol; preserve lot/risk
   limits, thresholds and SL. Keep scheduler/page cycles OFF and budget revoked.
4. Only for the authorized DEMO test, start console with trading + demo cash policy
   enabled and PAPER_TRADING=false in process environment only. Verify independent
   guardian healthy and repeat broker preflight before the call.
5. Invoke the documented action ONCE. No retry, separate confirmation, raw broker
   tool, forced signal, threshold change or second invocation. WAIT means no order.
6. Reconcile uncertain results before any action. If owned position exists, retain
   guardian + broker SL and existing net -10/+2 policy. Do not force closure to claim
   acceptance. Report OPEN if still open; do not wait indefinitely.
7. When no positions/orders/unresolved intents remain, restart only console with
   both trading flags false. Never stop protection while a position remains.
8. Archive local raw evidence securely; commit/push only sanitized report. Distinguish
   signal, proposal, broker acceptance, ownership and actual exit; unknown is not pass.
