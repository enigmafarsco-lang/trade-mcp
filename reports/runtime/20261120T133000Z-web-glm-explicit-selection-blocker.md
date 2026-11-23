# Web provider explicit selection — glm-5.3-flash-free (blocked: not listed)

- **Report id:** `20261120T133000Z-web-glm-explicit-selection-blocker`
- **Generated (UTC):** 2026-11-20T13:30:00Z
- **Base:** `6053277d1ca04b94c718c3a7aa86677c788c98d6` (`arena/f81b6b27-trade-mcp`, preserved)

> `execution_authorized_by_report: false`. Explicit user-authorized selection (not silent fallback). No inference, no budget activation, no paid request, no order.

## 1) Git
- Fetched current branch. HEAD/origin both `6053277` (already up to date); no fast-forward needed. Local commits/data/secrets preserved. Preserved report commit `6053277`.

## 2) Installed OpenCode verification (exact model)
- **Requested (explicit):** `opencode/glm-5.3-flash-free` for trade-mcp WEB (`opencode-session`), no Starimg, coding conversation model unchanged.
- **Codebase allowlist:** model present in `FREE_MODELS`.
- **Installed OpenCode (loopback, v2, healthy):** **exact model NOT listed**.
  - Queried provider/model endpoints (`/api/provider`, `/api/config`, `/api/model`): **`opencode/glm-5.3-flash-free` not found**.
  - Related `opencode/glm-5.3-flash` (without `-free` suffix) appears in installed listings, but this is **not** the exact requested model ID; **no substitution** performed.
- **Result:** `unavailable_in_installed_opencode` (specific). Per instructions, no other model selected.

## 3) Fresh pre-restart inspection
- DEMO/USD, **positions = 0, pending orders = 0, unresolved intents = 0**, not latched. No open position protection required.

## 4–5) Console configuration / restart / effective model
- **Not applied / not restarted** due to step 2 blocker (exact requested model unavailable in installed OpenCode). Console remains in prior safe state: effective **openai-compatible / direct ai.starimg.ru** (unchanged), **trading/demo policy false**, **budget revoked**, entry cycles stopped. No reconfigured process started.

## 6) Outcomes (separate)
- **Configuration success:** **false** (blocked: exact requested model unavailable in installed OpenCode).
- **Supported non-generation session-health check (requested model):** **not run** (configuration not applicable/applied). No inference, no budget activation, no paid request, no order.

## Evidence / VCS
- Local raw evidence (owner-only, `700/600`, git-ignored): `logs/session-20261120T133000Z-web-glm-explicit-selection-blocker/state.json`. No secrets/credentials.
- Sanitized report committed; push attempted (see result). Commit `6053277` preserved.
