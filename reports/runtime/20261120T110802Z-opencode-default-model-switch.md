# OpenCode default model switch to Starimg — sanitized status report (append)

- **Report:** `20261120T110802Z-opencode-default-model-switch.md`
- **Generated (UTC):** 2026-11-20T11:08:02Z
- **Branch:** `arena/f81b6b27-trade-mcp`
- **Prior reports preserved (unchanged):** `20261120T104307Z-starimg-config-status.md` (`f659d55`), `20261120T105607Z-starimg-credential-runtime-status.md` (`cb57fbb`)
- **Authorization:** user authorized exactly **one** short greeting inference to verify the model selection. No trading, no orders, no trade-mcp cloud budget activation. No fallback to another provider/model.

## Configured values (non-secret)
- **Default OpenCode selector:** `starimg/deepseek-v4.1-flash-1017`
- **Endpoint (existing validated provider):** `https://ai.starimg.ru/v1`
- **API model ID (vendor-advertised alias; upstream identity not independently verified):** `deepseek-v4.1-flash-1017`
- **Credential reference:** existing secure local file reference (not printed, not changed)

## Status by category

| Category | Status | Observed result |
|---|---|---|
| Default model saved via supported mechanism | **yes** | Top-level `model` set to `starimg/deepseek-v4.1-flash-1017` in the global OpenCode config. Owner-only backup of the pre-change config created first (local, outside repo). |
| MCP servers / permissions / other providers preserved | **yes** | `opencode debug config` still resolves `mcp.servers.trade` (unchanged command/cwd/env) and the existing provider set; only the default `model` field was added/changed. |
| Schema/parser validation (installed OpenCode v2.3.2) | **pass** | `opencode debug config` parses with model `{"providerID":"starimg","model":"deepseek-v4.1-flash-1017"}`; `opencode models` still lists `starimg/deepseek-v4.1-flash-1017`. |
| Active model verified (real inference) | **pass (observed)** | One short greeting request executed with explicit model selection; completed with `reason=stop`, `cost=0` in the client report, tokens input 8807 / output 9. Session record for that run shows `providerID=starimg`, model `deepseek-v4.1-flash-1017`. No fallback provider/model observed. |
| Model switch in the running interactive conversation | **not applied** | The active OpenCode desktop conversation keeps its current model; the saved default applies to new sessions. Start a new session to use `starimg/deepseek-v4.1-flash-1017`. |
| trade-mcp runtime settings | **unchanged** | Console LLM config unchanged (`model=deepseek-v4.1-flash-1017`, key configured), `trading=false`, cloud budget `revoked` (`used_attempts=1/5`, no activation), broker `positions=0, orders=0`, scheduler/page cycles stopped. |

## Notes
- Only one inference request was sent (the authorized greeting); no trade-mcp model test, no agent inference, no budget activation.
- Vertex/vendor installer not executed; Ling-specific parameters not applied.
- Configuration file and key files were not staged or committed. No credentials, config dumps or raw logs are included here.
