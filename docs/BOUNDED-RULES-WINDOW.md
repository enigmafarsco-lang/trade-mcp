# Five-minute guarded rules evaluation window

This is an explicit execution endpoint, not a forecast model or paper simulator:

`POST /api/agents/{id}` with
`{"action":"run_rules_window","duration_seconds":300}`.

It evaluates no faster than once per second for at most 300 seconds / 300
completed evaluations. Slow calls skip slots; no catch-up bursts or retry after
exception. An in-flight broker/analysis call cannot be hard cancelled, so wall
clock completion can overrun the deadline. This synchronous HTTP request must
not be resubmitted on client timeout. Only one window per console process is
allowed. Restart does not resume it. Do not run multiple consoles against one
journal. `stop_rules_window` on the same route stops subsequent evaluations,
not existing position protection or an already in-flight call.

The saved enabled agent must contain exactly one symbol. Calls force guarded
rules decisions through existing AgentEngine/propose/confirm checks. An open
position/order skips entry. Persistent ownership/risk/loss-latch gates still
apply. This does NOT promise one order per second; WAIT and zero trades are valid.
Other manual entry calls must not be made during the window.

Before starting locally: verify DEMO/USD, flat broker, no unresolved intents or
loss latch, fresh cost snapshot/reconciliation and healthy independent guardian.
Keep cloud budget revoked, disable scheduler and page cycle. Only then enable
existing trading + demo cash policy in the test console process. The endpoint
checks execution flags, demo identity, independent guardian health/freshness and
agent shape before each evaluation. It does not enable trading itself and never
calls a configured cloud model. At completion, preserve guardian for any open
position. Return flags false only after flat/no orders/no unresolved intents.

Archive evidence owner-only and report actual orders/ownership/exits separately.
No new model-generated price prediction is implemented by this change. The old
zero-drift volatility overlay is now OFF by default because it must not be
mistaken for predicted prices. The comparison table remains explicitly labeled
as a volatility scenario. Optional diagnostic display can be enabled locally via
`localStorage.setItem('trade-mcp-show-volatility-scenarios','true')`; this is not
an order signal. Do not claim a validated prediction without out-of-sample
horizon-specific evaluation and real model provenance.
