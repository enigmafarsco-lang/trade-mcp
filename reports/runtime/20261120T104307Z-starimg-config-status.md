# Starimg provider configuration — sanitized status report

- **Report:** `20261120T104307Z-starimg-config-status.md`
- **Generated (UTC):** 2026-11-20T10:43:07Z
- **Branch:** `arena/f81b6b27-trade-mcp`
- **Scope:** local configuration/validation only. No inference request sent. No cloud budget activated. No trading enabled. Vendor installer not executed. Old exposed key not used.

## Configured values (non-secret)
- **Endpoint (base URL):** `https://ai.starimg.ru/v1`
- **API model ID (vendor-advertised alias; upstream identity not independently verified):** `deepseek-v4.1-flash-1017`
- **OpenCode selector (provider/model):** `starimg/deepseek-v4.1-flash-1017` (API model ID sent: `deepseek-v4.1-flash-1017`)

## Status by required category

| Category | Status | Observed result |
|---|---|---|
| Configuration edited | **yes** | OpenCode global config merged with `providers.starimg` (OpenAI-compatible package, base URL and modelID as above, API key referenced via local file-secret path) while preserving existing MCP server entries and other settings. trade-mcp `.env` (gitignored, not committed) updated with Starimg endpoint/model and `web_model_backend=direct`; no API key set; existing non-LLM settings preserved. |
| Schema validated (installed OpenCode parser) | **yes** | `opencode debug config` loaded the global config including `providers.starimg` and preserved `mcp.servers.trade`; no parse/schema errors observed. `providers/package/settings/modelID` and file-secret reference accepted by installed OpenCode. |
| Model selectable | **yes (selector/catalog)** | `opencode models` listed `starimg/deepseek-v4.1-flash-1017`. Selectable as a configured model ID; successful authenticated use not verified (credentials missing). |
| Credentials missing/present | **MISSING** | Local key placeholder files exist but are empty (0 bytes). trade-mcp loader reports API key not configured. Old exposed key not used. Authenticated access cannot be claimed. |
| Authenticated metadata checked | **not_run** | Not run (no replacement credentials). Unauthenticated metadata probe only: `GET https://ai.starimg.ru/v1/models` returned HTTP 401 (`missing_authorization_header`) — endpoint reachable, authentication required; model access not validated. |
| Inference | **not_run** | No inference request sent. |
| Console runtime configuration checked | **checked** | Running console settings showed LLM not configured (process started before `.env` update). trade-mcp actual loader (`Config.from_env` after `load_dotenv`) accepted endpoint/model with no API key configured and trading/budgets disabled. No process observed pairing the new Starimg endpoint with an old OpenRouter key; no request sent. |

## Security and operational notes (observed/required)
- Owner-only backups of prior OpenCode and trade-mcp configuration/key material were created locally before edits.
- Replacement credentials must be entered locally by the user (not in chat/command arguments/shell history). A secure local entry helper was prepared and a GUI terminal launch was attempted (best effort) to prompt for hidden input writing to the local key files (mode 600). If not completed, enter the new key locally into the prepared secure key files before any authenticated validation.
- **Required externally:** revoke the previously exposed API key and rotate the exposed account access code on the vendor side if supported (not performed here).
- `.env`, key files, and OpenCode configuration were not staged or committed.
