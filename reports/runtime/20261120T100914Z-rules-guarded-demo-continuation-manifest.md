# Archive manifest — rules-guarded DEMO acceptance continuation `20261120T100914Z`

Continuation of session `20261120T092406Z`. Prior local report commit `302ad14b67ce926181139b7e37e74b1468db4db3` and its evidence/reports preserved (not overwritten).

Local archive directory: `logs/session-20261120T092406Z-continuation/` (git-ignored, mode `700`, files mode `600`, owner-only). Nothing in `logs/` is committed.

Legend: **L** = preserved locally (owner-only, never committed) · **S** = included in sanitized form in `reports/runtime/` · **—** = not applicable/unavailable.

| # | Evidence category | Local archive file (owner-only) | S | L |
|---|---|---|---|---|
| 1 | Publisher diagnosis/recovery (charts/experts, verified monitor params, attach, snapshot updates, reconciliation) | `recovery-notes.txt` | S (§1, JSON.recovery) | L |
| 2 | Pre-call fresh preflight (DEMO/USD, 0/0, guard clean, snapshot fresh, recon ok, budget revoked, flags/env verified) | `precall_preflight.json` | S (§2, JSON.checks) | L |
| 3 | Single `run_rules_guarded` invocation request/response (HTTP 200, rules, sell/conf=63, cancelled by agent lot cap) | `rules_guarded_run.json` | S (§3, §4, JSON.outcomes) | L |
| 4 | Post-invocation broker/positions/orders state (flat) | `account_state_final.json` | S (§4, §5; identifiers redacted) | L |
| 5 | Cost reconciliation after recovery/end state | `cost_reconciliation_final.json` | S (§1, §2) | L |
| 6 | Budget state (revoked, no cloud request) | `model_budget_final.json` | S (§2, §5) | L |
| 7 | Final safe-console overview (flags false, scheduler stopped) | `overview_final.json` | S (§5) | L |
| 8 | Console logs (test run, recovery, final safe) | `console_test_20261120T092406Z.log`, `console_safe_recovery2.log`, `console_safe_final.log` | S (summarized) | L |
| 9 | Strategy signal / risk checks / proposal / confirmation / broker response (separate) | — | S (§4, JSON.outcomes) | — |
| 10 | Owned-position reconciliation / floating net-P/L / exit | — | S: **not applicable** (flat, no position opened) | — |
| 11 | Credentials, `.env`, API keys/tokens, auth headers, raw provider payloads, raw account identifiers/balances | — | **excluded entirely** | local only, never copied |

## Sanitized files committed in this change

- `reports/runtime/20261120T100914Z-rules-guarded-demo-continuation.json`
- `reports/runtime/20261120T100914Z-rules-guarded-demo-continuation.md`
- `reports/runtime/20261120T100914Z-rules-guarded-demo-continuation-manifest.md`

## Review statement

Staged content reviewed for secrets and unintended files before staging. No credentials, `.env` values, key material, tokens, raw account identifiers, database copies, raw logs or raw provider payloads are included. Only the three sanitized continuation report paths above are intended for staging; `git add .` was not used and no ignored file was force-added. Prior commit 302ad14 and its reports remain unchanged.
