# Persisted Iceman web setup and bounded validation (quota_exhausted)

- **Report id:** `20261120T140500Z-iceman-persist-validate`
- **Generated (UTC):** 2026-11-20T14:05:00Z
- **Base:** `6e0acd94d08b9ba3ac9a2426faf503d278f57d86` (`arena/f81b6b27-trade-mcp`)
- **Preserved commits include:** `6053277`, `734b8d8`, `c900999`, `6e0acd9`

> `execution_authorized_by_report: false`. No inference beyond the single authorized bounded request; no retries/fallback/paid provider/extra window; no budget activation beyond the window; no trading.

## 1) Persistence (supported local mechanism, no ephemeral port hard-coded)
- **Mechanism:** local `.env` (gitignored, **not committed**) persists `TRADE_MCP_WEB_MODEL_BACKEND=opencode-session` and `TRADE_MCP_OPENCODE_MODEL=opencode/iceman-free` **without** `TRADE_MCP_OPENCODE_URL`. A narrow adapter rediscovery path uses supported `discover_desktop()` to obtain the verified loopback URL/password at client creation when URL is missing.
- **No ephemeral loopback port hard-coded.** Secrets preserved securely; **no local configuration committed**. Console restarted and **did not restore Starimg**.
- **Effective after restart:** backend `opencode-session`, provider `opencode-session`, model `opencode/iceman-free`, **configured=true** (rediscovered).

### Source/tests changes (committed)
- `src/trade_mcp/web_opencode.py`: rediscover verified desktop when URL missing for allowlisted opencode-session model (supported local persistence; no hard-coded port).
- `src/trade_mcp/console.py`: updated opencode-session unconfigured message to reflect discoverable server.
- `tests/test_web_opencode.py`: regression tests for rediscovery success and fail-closed failure.
- Preserves existing model IDs and budget/permission controls. **ruff:** pass. **pytest:** pass (full suite).

## 2) Bounded read-only web-analysis validation window (authorized)
- **Window:** 300s, **max 5 application attempts** total. **1** application analysis request sent, **0** mandatory model tests, **0** retries, **no fallback**, **0** paid provider requests, **no extra window**. **Stopped on first error; budget revoked afterward.**
- **Request:** `POST /api/terminal/explain` (`symbol=XAUUSD`, model `opencode/iceman-free`, opencode-session). **Minimal redacted market context only** (`symbol`, `quote`, `bias`, `scenario`, `warnings`) — **no account/journal identifiers**. Trading/demo cash policy **OFF**, entry cycles **stopped**.
- **Result (actual):** **provider error** `quota_exhausted` (HTTP 502). Transport: `stage=session_generate`, `generation_submitted=true`, `permissions_acknowledged=true`, `model_acknowledged=true`.
  - **Genuine model response:** **none** (request failed at provider quota).
  - **Rules/error vs WAIT:** **provider error** (not WAIT, not a genuine model response). Reported separately from deterministic/rules output.

## 3) Actual usage (recorded)
- `request_kind=explain`, `attempt_index=1`, `provider=opencode-session`, `model=opencode/iceman-free`, `ok=0`, `error_code=quota_exhausted`
- Tokens/cost: **`input_tokens=null`, `cached_input_tokens=null`, `output_tokens=null`, `reasoning_tokens=null`, `cost_amount=null`, `cost_currency=null`** (unavailable), `usage_source=unknown`, `cost_source=unknown`, `accounting_convention=unknown`, `retry_after_ms=null`.

## 4) Post-state / safety
- `trading_enabled=false`, `paper_trading=false`, demo cash policy off, `budget=revoked` (not allowed), page/entry cycles stopped. Broker **DEMO/USD flat** (`positions=0`, `orders=0`), unresolved intents **0**.

## 5) Evidence and VCS
- Local raw evidence (owner-only, `700/600`, git-ignored): `logs/session-20261120T140500Z-iceman-persist-validate/state.json` (sanitized). No local config/secrets committed.
- Source/tests and sanitized report committed; push attempted (see result).
