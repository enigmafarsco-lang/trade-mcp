# Archive manifest — rules-guarded DEMO acceptance attempt `20261120T092406Z`

Local archive directory: `logs/session-20261120T092406Z/` (git-ignored, mode `700`, files mode `600`, owner-only). Nothing in `logs/` is committed.

Legend: **L** = preserved locally (owner-only, never committed) · **S** = included in sanitized form in `reports/runtime/` · **—** = not applicable/unavailable.

| # | Evidence category | Local archive file (owner-only) | S | L |
|---|---|---|---|---|
| 1 | Fresh preflight snapshot (flags/broker/guard/budget/agents/cost/snapshot age) | `fresh_preflight_now.json` | S (§2, JSON) | L |
| 2 | Safe console overview after restart (flags false, scheduler stopped) | `overview_safe.json` | S (§5) | L |
| 3 | Broker account/positions/orders state (demo/USD) | `account_state_safe.json` | S (§2, §5; identifiers redacted) | L |
| 4 | Cost reconciliation result (failed, stage snapshot) | `cost_reconciliation.json` | S (§2, JSON) | L |
| 5 | Model budget state (revoked) | `model_budget.json` | S (§2, JSON) | L |
| 6 | Saved agents list (chosen agent verified single-symbol/guarded/limits) | `agents.json` | S (§2 summary) | L |
| 7 | Safe console startup log (flags false) | `console_safe_20261120T092406Z.log` | S (§5) | L |
| 8 | Test run evidence (`tests/test_rules_action.py` pass; full suite 100% progress/5 skips observed) | — (terminal output) | S (§1) | — |
| 9 | `run_rules_guarded` invocation/request/response | — | S: **not performed** (preflight failed) | — |
| 10 | Strategy signal / risk checks / proposal / confirmation / broker response | — | S: **not executed / none** | — |
| 11 | Owned-position reconciliation / floating net-P/L / exit | — | S: **not applicable** (flat) | — |
| 12 | Credentials, `.env`, API keys/tokens, auth headers, raw provider payloads, raw account identifiers/balances | — | **excluded entirely** | local only, never copied |

## Sanitized files committed in this change

- `reports/runtime/20261120T092406Z.json` — machine-readable status/count/outcome report (`execution_authorized_by_report: false`).
- `reports/runtime/20261120T092406Z-rules-guarded-demo-acceptance-session.md` — narrative session report.
- `reports/runtime/20261120T092406Z-rules-guarded-demo-acceptance-manifest.md` — this manifest.

## Review statement

Staged content reviewed for secrets and unintended files before staging. No credentials, `.env` values, key material, tokens, raw account identifiers, database copies, raw logs or raw provider payloads are included. Only the three sanitized report paths above are intended for staging; `git add .` was not used and no ignored file was force-added.
