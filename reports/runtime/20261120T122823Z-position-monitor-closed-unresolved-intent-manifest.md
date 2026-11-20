# Archive manifest — `20261120T122823Z-position-monitor-closed-unresolved-intent`

- **Branch:** `arena/f81b6b27-trade-mcp`
- **Base:** `91b61d1a13ec7d2a9e2bf816a90a80c474d2aea4` (preserved)
- Local raw archive (git-ignored, owner-only `700/600`): `logs/session-20261120T122823Z-monitor/`

| Category | Local raw (L) | Sanitized (S) | Notes |
|---|---:|---:|---|
| Fresh broker/guard observation (flat broker, intent state, budget/recon flags) | `observation.json`, `guard_state.json` | S (JSON/MD §1,2,5) | No credentials |
| Actual broker exit evidence (owned position closed by TP) | `exit_evidence_local_only.json` | S summary only (§3) | **Tickets, exact times/prices, amounts, raw financial records excluded from Git**; local-only |
| Guardian process/flags, blocker | — | S (§2,4) | unresolved intent retained; console not restarted |

**Sanitized files:** `20261120T122823Z-position-monitor-closed-unresolved-intent.json`, `.md`, `-manifest.md`. `execution_authorized_by_report:false`.
