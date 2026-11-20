# Local web provider handoff (no paid DeepSeek)

The existing supported web transport is `WEB_MODEL_BACKEND=opencode-session`.
It uses a separate restricted session, not the coding conversation, and requires
an explicitly selected supported free model from `FREE_MODELS` in opencode.py.
Changing the OpenCode desktop default alone does not change the web model.
Do not silently fall back to direct Starimg/DeepSeek or switch free models after
an error. A server must support this repository's v2 session API.

Authorized local operator steps:

1. Fetch/fast-forward current branch, preserve all data/settings/secrets.
2. Freshly inspect broker positions/orders/intents before any console restart.
   If a position exists, preserve protection and do not interrupt the guardian.
3. In a safe console process configure:
   - TRADE_MCP_WEB_MODEL_BACKEND=opencode-session
   - TRADE_MCP_OPENCODE_URL=the verified loopback OpenCode server URL
   - TRADE_MCP_OPENCODE_MODEL=the user's current supported FREE model selector
   Preserve authentication securely; do not print environment or copy secrets.
   If the selected model/API is unsupported, report the exact blocker, do not
   bypass the supported model list or guess an endpoint.
4. Verify effective backend/provider/model and non-generation session health.
   Keep trading/policy OFF and budget revoked. No inference or automatic entries
   are authorized by this configuration handoff; no paid provider requests.
5. Verify the four colored scenario markers in the chart using fresh broker data.
6. Archive raw evidence owner-only; commit/push only sanitized runtime report.

## Frequency and execution limits

This change does not implement or start a 300-order burst. Existing one-position,
SL, cash-risk, ownership and durable loss-latch gates remain mandatory. A timer
must not manufacture a signal, close/reopen positions or skip unresolved intents
to satisfy a requested order frequency. Polling once per second is not a promise
to trade once per second. The current scheduler remains unchanged (minimum 30s);
no new scheduler or trading window is started by this patch.
