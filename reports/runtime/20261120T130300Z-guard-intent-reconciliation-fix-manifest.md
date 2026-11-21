# Archive manifest — `20261120T130300Z-guard-intent-reconciliation-fix`

- **Branch:** `arena/f81b6b27-trade-mcp`
- **Base:** `9e51d7c76bafd0bf7646f2eca14aab1c8fceb8f7` (preserved; `91b61d1` also preserved)
- Local raw archive (git-ignored, owner-only `700/600`): `logs/session-20261120T130300Z-guard-fix-reconcile/`

| Category | Local raw (L) | Sanitized (S) | Notes |
|---|---:|---:|---|
| Safe console deploy, read-only reconcile action result, final broker/guard state (flat, intent closed, 0 unresolved, no latch) | `final_state_sanitized.json`, `console_safe_tail.log` | S (JSON/MD §1,4) | No tickets/exact prices/amounts/credentials |
| Verification checks (ruff, pytest, regressions, py_compile, JS note) | `checks.json` | S (§5) | — |
| Source/tests/docs changes implementing fix and regressions | — | committed in git (explicit files) | — |

**Sanitized files:** `20261120T130300Z-guard-intent-reconciliation-fix.json`, `.md`, `-manifest.md`. `execution_authorized_by_report:false`.
