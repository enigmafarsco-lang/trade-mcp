# Installed model mismatch resolution — glm-5.3-flash pricing (fail-closed)

- **Report id:** `20261120T134500Z-glm-pricing-failclosed`
- **Generated (UTC):** 2026-11-20T13:45:00Z
- **Base:** `734b8d8c5c32f9a1c4b5ba833546174b594ade4e` (`arena/f81b6b27-trade-mcp`)

> `execution_authorized_by_report: false`. Conditional explicit selection only if verified zero-cost (input/output). No inference, no budget activation, no paid request, no order. No visual browser verification claimed.

## 1) Sanitized installed metadata (exact provider/model, no inference, no credentials)
- **Provider identity:** `opencode` (“Personal / OpenCode”)
- **Installed model ID observed:** `glm-5.3-flash` (provider `opencode`)
- **Requested exact ID:** `opencode/glm-5.3-flash-free` — **not listed** in installed OpenCode (exact mismatch)
- **Pricing (installed metadata):** `input = 0.15`, `output = 0.5`, `cache.read = 0.03`, `cache.write = 0`
- **Separate reasoning price field:** **not listed** in cost metadata (reasoning appears in variants/capabilities, not as a priced cost field). This is **not** treated as zero/unknown in a way that permits use given explicit input/output pricing.
- **Verdict:** **nonzero (not verified zero-cost)** for input/output. Missing/absent separate reasoning-price field does not override nonzero input/output; pricing not inferred from a `-free` suffix. **Model must not be used.** No other model selected automatically.

## 2) Implementation / validation (conditional)
- Pricing **not verified zero-cost** → **no model-support implementation added**, no allowlist/validation bypass, all currently supported IDs preserved. No source/tests/docs changes for model enablement.

## 3) Independent verification before restart (broker/guardian)
- **DEMO/USD, flat:** `positions=0`, `orders=0`.
- **Durable guard:** `unresolved intents=0`, not latched. No open position protection to preserve. Console restarted only in this safe state.

## 4) Web backend configuration (fail-closed, no direct Starimg)
- Console restarted (pid `133776`) with **effective backend `opencode-session`**, verified loopback OpenCode URL configured in process env, **no valid allowlisted model** (model validation blocked: nonzero/unusable pricing and exact requested ID not listed) → **`model_configured=false`, UNCONFIGURED and fail-closed**.
- **Direct Starimg not active / no fallback:** direct-provider credentials removed **from this console process environment only**; secure files and rollback settings preserved. Effective provider/model empty/unconfigured.
- **Safety state:** `trading=false`, `paper_trading=false`, demo cash policy **off**, model budget **revoked** (not allowed), entry/page cycles **stopped**.

## 5) Session-health (separate from configuration)
- **Configuration outcome:** backend set to `opencode-session` on verified loopback but **unconfigured/fail-closed** as required (model validation blocked). Direct Starimg inactive.
- **OpenCode server identity/health (non-generation):** previously verified **healthy, API v2** on verified loopback (no text generation).
- **Runtime web session-health validation for requested model/config:** **not run** — app session probe requires a configured allowlisted model; backend intentionally left unconfigured/fail-closed. **No inference, no budget activation, no paid request, no order.**

## 6) Tests/lint and VCS
- **ruff:** pass. **pytest:** pass (all collected tests; environment skips as applicable). No source/tests/docs changes required for blocked (nonzero-priced) model.
- Local raw evidence (owner-only, `700/600`, git-ignored): `logs/session-20261120T134500Z-glm-pricing-failclosed/state.json` (sanitized; no secrets/credentials).
- Preserved commits: `6053277`, `734b8d8`. New sanitized report committed; push attempted (see result).
