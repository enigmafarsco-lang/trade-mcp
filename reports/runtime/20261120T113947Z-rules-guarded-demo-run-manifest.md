# Archive manifest — `20261120T113947Z-rules-guarded-demo-run`

- **Branch:** `arena/f81b6b27-trade-mcp`
- **Base commit:** `276b7c22110bd887c93c160424d16d0018a0411c`
- Prior reports/commits preserved (including `20261120T112900Z-scope-completion-session`).

Local raw archive (git-ignored, owner-only `700/600`): `logs/session-20261120T113947Z-rules-guarded/`

| Category | Local raw (L) | Sanitized (S) | Notes |
|---|---:|---:|---|
| Native MT5 tool discovery + chart/expert inspection + monitor restore + snapshot updates + reconciliation | `monitor_restore.json` | S (JSON/MD §1) | No tokens/secrets |
| Fresh pre-call preflight (DEMO/USD, 0/0, guard clean, recon ok, budget revoked, agent limits, flags) | `precall_preflight.json` | S (§2) | Sanitized |
| Single `run_rules_guarded` invocation response (HTTP 200) | `run_rules_guarded.json` | S (§3,4) | No cloud payloads |
| Post-state: proposal executed, broker acceptance, open position, ownership/guard intent, floating P/L snapshot, console log | `post_state.json`, `console_test.log` | S (§4,5) | Position OPEN; protection retained; no realized exit |
| Credentials, `.env`, keys, tokens, DBs, journals raw, raw provider payloads | — | **excluded** | never committed |

**Sanitized files:** `20261120T113947Z-rules-guarded-demo-run.json`, `.md`, `-manifest.md`. `execution_authorized_by_report:false`.
