# Web opencode-session configured to current coding model (Iceman)

- **Report id:** `20261120T135200Z-iceman-web-session`
- **Generated (UTC):** 2026-11-20T13:52:00Z
- **Base:** `c900999065c74220da4220a1864c1953ec8d5671` (`arena/f81b6b27-trade-mcp`)

> `execution_authorized_by_report: false`. No inference, no paid request, no budget activation, no trading. Coding conversation model not switched.

## 1) Current coding session model (actual)
- **Actual providerID/modelID:** `opencode` / `iceman-free` (`opencode/iceman-free`), read from the actual OpenCode coding session model metadata (and harness context). Not guessed; saved default not used.

## 2) Installed OpenCode availability and pricing (verified)
- Installed OpenCode server: **healthy, API v2** (verified loopback). Exact model **`opencode/iceman-free`** (“Iceman Free”) is **available** via installed API.
- **Cost (explicit):** `input=0`, `output=0`, `cache.read=0`, `cache.write=0`; no separate reasoning-price field listed. **Explicit zero-cost for input/output verified** (pricing not missing). Model is in existing `FREE_MODELS` allowlist.

## 3) Configuration (web opencode-session)
- Pre-checks: **DEMO/USD flat** (`positions=0`, `orders=0`), **unresolved intents=0**, not latched.
- Configured running console (pid `133776`, in-memory, not persisted) via supported connect flow (`/api/model-connect-opencode`, consented, local discovery) to **`opencode-session`** with exact model **`opencode/iceman-free`** (`model_configured=true`). No source/tests/docs changes required (already supported). Direct Starimg not active.

## 4) Non-generation session-health validation (separate)
- `POST /api/model-session-check` (consented): **`ok=true`**, `generation_requested=false`, `transport`: `stage=session_validation`, `generation_submitted=false`, `permissions_acknowledged=true`, `model_acknowledged=true`.
- **No inference, no paid request, no budget activation, no order.**

## 5) Effective runtime / safety
- **Effective backend:** `opencode-session`; **provider:** `opencode-session`; **model:** `opencode/iceman-free`; **configured:** `true`.
- `trading_enabled=false`, `paper_trading=false`, demo cash policy off, budget **revoked/not allowed**, page/entry cycles **stopped**. All existing data/commits/secrets/guards preserved.

## 6) VCS
- Preserved commits: `6053277`, `734b8d8`, `c900999`. No source/tests changes. Sanitized report committed; push attempted (see result). Local raw evidence (owner-only, `700/600`, git-ignored): `logs/session-20261120T135200Z-iceman-web-session/state.json`.
